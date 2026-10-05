"""容器更新核心逻辑。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import DATA_DIR, config, save_config
from app.core import progress, log_handler
from app.db import engine, ProxyNode
from app.docker_service import docker_service, PullCancelled
from app.registry import get_remote_digests_multi, parse_image_reference

logger = logging.getLogger("dockerassistant.updater")

STATE_FILE = DATA_DIR / "updater_results.json"

# 全局状态
state_lock = asyncio.Lock()
check_results: Dict[str, Dict[str, Any]] = {}
last_check_time: Optional[str] = None
running_updates: Dict[str, str] = {}
check_in_progress: bool = False
container_checking: Dict[str, bool] = {}

# 更新取消标志：{容器名: True}
cancel_requested: Dict[str, bool] = {}


# ============================================================
#  registry 类型判断
# ============================================================

_KNOWN_REGISTRY_PREFIXES: Dict[str, str] = {
    "registry-1.docker.io": "dockerhub",
    "docker.io": "dockerhub",
    "index.docker.io": "dockerhub",
    "ghcr.io": "ghcr",
    "gcr.io": "gcr",
    "k8s.gcr.io": "gcr",
    "registry.k8s.io": "gcr",
    "quay.io": "quay",
    "mcr.microsoft.com": "mcr",
    "nvcr.io": "nvcr",
    "docker.elastic.co": "elastic",
}


def _detect_registry_type(image_ref: str) -> Optional[str]:
    try:
        registry, _, _ = parse_image_reference(image_ref)
    except Exception:
        return None

    if not registry:
        return None

    return _KNOWN_REGISTRY_PREFIXES.get(registry.lower())


def _resolve_check_source(image_ref: str) -> Tuple[List[str], bool, str]:
    registry_type = _detect_registry_type(image_ref)
    raw_mirrors = [m.strip() for m in (config.updater.mirrors or []) if (m or "").strip()]

    if registry_type is not None:
        prioritized: List[str] = []
        if "local" in raw_mirrors:
            prioritized.append("local")
        for m in raw_mirrors:
            if m != "local":
                prioritized.append(m)
        if not prioritized:
            prioritized = ["local"]

        use_direct = bool(config.updater.use_direct)
        reason = f"已知 registry [{registry_type}]，走节点路由"
        logger.info("[检测策略] %s → %s（mirrors=%s, use_direct=%s）", image_ref, reason, prioritized, use_direct)
        return prioritized, use_direct, reason

    reason = "未知 registry，直接使用直连"
    logger.info("[检测策略] %s → %s", image_ref, reason)
    return [], True, reason


def _expand_mirrors_for_check(mirrors: List[str]) -> List[str]:
    """检查阶段：把 'local' 展开为内置代理地址。"""
    local_url = f"http://127.0.0.1:{config.server.port}"
    out: List[str] = []
    for m in mirrors or []:
        m = (m or "").strip()
        if not m:
            continue
        if m == "local":
            out.append(local_url)
        else:
            out.append(m)
    return out


# ============================================================
#  拉取阶段：本代理在 daemon 视角下的可达地址
# ============================================================
def _resolve_local_mirror_for_daemon() -> str:
    """计算 daemon 视角下本代理的可达 host:port（不含协议）。"""
    explicit = (getattr(config.updater, "local_mirror_url", "") or "").strip()
    if explicit:
        return re.sub(r"^https?://", "", explicit).rstrip("/")

    host = (config.server.host or "").strip()
    port = int(config.server.port or 8000)

    if not host or host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"

    host = re.sub(r"^https?://", "", host).rstrip("/")

    if ":" in host and not host.startswith("["):
        return host

    return f"{host}:{port}"


def _expand_mirrors_for_pull(mirrors: List[str]) -> List[str]:
    """拉取阶段：把 'local' 展开为 daemon 视角下本代理的 host:port。"""
    out: List[str] = []
    for m in mirrors or []:
        m = (m or "").strip()
        if not m:
            continue
        if m == "local":
            local = _resolve_local_mirror_for_daemon()
            if local:
                out.append(local)
        else:
            out.append(m)
    return out


# ============================================================
#  代理前缀标签：收集 + 清理
# ============================================================
def get_local_mirror_prefixes() -> List[str]:
    """返回所有可能作为本代理前缀的 host:port 列表。"""
    prefixes: List[str] = []

    local = _resolve_local_mirror_for_daemon()
    if local:
        prefixes.append(local)

    port = int(config.server.port or 8000)
    for h in ("127.0.0.1", "localhost"):
        p = f"{h}:{port}"
        if p not in prefixes:
            prefixes.append(p)

    explicit = (getattr(config.updater, "local_mirror_url", "") or "").strip()
    if explicit:
        p = re.sub(r"^https?://", "", explicit).rstrip("/")
        if p and p not in prefixes:
            prefixes.append(p)

    seen: set = set()
    out: List[str] = []
    for p in prefixes:
        p = p.strip().rstrip("/")
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def cleanup_mirror_tags() -> Dict[str, Any]:
    """清理所有镜像上以本地代理地址为前缀的 tag。"""
    prefixes = get_local_mirror_prefixes()
    logger.info("开始清理代理前缀标签，前缀列表：%s", prefixes)
    return docker_service.cleanup_mirror_prefix_tags(prefixes)


# ============================================================
#  镜像加速就绪判断
# ============================================================
def _has_available_proxy() -> bool:
    try:
        with Session(engine) as session:
            node = session.exec(
                select(ProxyNode)
                .where(ProxyNode.enabled == True)  # noqa: E712
                .where(ProxyNode.manually_disabled == False)  # noqa: E712
                .where(ProxyNode.latency < config.health_check.disable_threshold)
            ).first()
            return node is not None
    except Exception:
        return False


# ============================================================
#  容器状态统计
# ============================================================
def get_container_summary() -> Dict[str, int]:
    total = len(check_results)
    updatable = 0
    error = 0
    for r in check_results.values():
        if r.get("error"):
            error += 1
        elif r.get("update_available"):
            updatable += 1
    return {
        "total": total,
        "updatable": updatable,
        "error": error,
        "checking": sum(1 for v in container_checking.values() if v),
    }


# ============================================================
#  启动时判断
# ============================================================
def should_run_check() -> bool:
    interval_minutes = max(5, int(config.updater.check_interval_minutes or 60))

    if not last_check_time:
        logger.info("[startup] 未找到容器检测记录，触发首次容器检测")
        return True

    try:
        last_dt = datetime.fromisoformat(last_check_time)
    except Exception as e:
        logger.warning(f"[startup] last_check_time 解析失败（{last_check_time}）：{e}，触发容器检测")
        return True

    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone(timedelta(hours=8)))

    now = datetime.now(timezone(timedelta(hours=8)))
    try:
        elapsed_minutes = (now - last_dt).total_seconds() / 60
    except Exception:
        return True

    logger.info(f"[startup] 上次容器检测：{last_check_time}，" f"距现在 {elapsed_minutes:.1f} 分钟，周期 {interval_minutes} 分钟")
    return elapsed_minutes >= interval_minutes


# ============================================================
#  状态持久化
# ============================================================
def _load_state() -> None:
    global last_check_time
    if not STATE_FILE.exists():
        logger.info("[startup] 未发现容器检测状态文件，视作首次检测")
        return
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f) or {}
        lt = data.get("last_check_time")
        if isinstance(lt, str) and lt:
            last_check_time = lt
        res = data.get("results")
        if isinstance(res, dict):
            check_results.clear()
            for k, v in res.items():
                if isinstance(v, dict):
                    check_results[k] = v
        logger.info(f"[startup] 已恢复容器检测状态（last_check={last_check_time}，" f"{len(check_results)} 条记录）")
    except Exception as e:
        logger.warning(f"[startup] 读取容器检测状态失败: {e}")


def _save_state() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {"last_check_time": last_check_time, "results": check_results},
                f,
                ensure_ascii=False,
            )
        tmp.replace(STATE_FILE)
    except Exception:
        pass


# ============================================================
#  辅助
# ============================================================
def _short_digest(d: Optional[str]) -> str:
    if not d:
        return "-"
    return d[7:19] if d.startswith("sha256:") else d[:12]


def _target_reference(current_image: str, entry: Dict[str, Any]) -> str:
    entry = entry or {}
    strategy = (entry.get("strategy") or "track").lower()
    registry, repo, current_tag = parse_image_reference(current_image)

    if registry in ("registry-1.docker.io", "docker.io", "index.docker.io"):
        short = repo[len("library/") :] if repo.startswith("library/") else repo
        base = short
    else:
        base = f"{registry}/{repo}"

    if strategy == "pin":
        tag = (entry.get("target_tag") or "").strip()
        return f"{base}:{tag}" if tag else f"{base}:{current_tag}"
    if strategy == "latest":
        return f"{base}:latest"
    return f"{base}:{current_tag}"


# ============================================================
#  取消控制
# ============================================================
def request_cancel(container_name: str) -> bool:
    if container_name not in running_updates:
        return False
    cancel_requested[container_name] = True
    logger.info("[%s] 收到取消请求（当前阶段：%s）", container_name, running_updates.get(container_name))
    return True


def _is_cancelled(container_name: str) -> bool:
    return bool(cancel_requested.get(container_name))


# ============================================================
#  单容器检测
# ============================================================
async def check_one(
    container: Dict[str, Any],
    cfg,
    slot: Optional[int] = None,
    manual: bool = False,
) -> Dict[str, Any]:
    name = container["name"]
    prefix = "[手动]" if manual else (f"[C{slot}]" if slot else "")
    task_key = f"upd_check_one:{name}"

    current_image = container.get("image") or ""
    entry = (cfg.updater.containers or {}).get(name) or {}
    strategy = (entry.get("strategy") or "track").lower()
    target_ref = _target_reference(current_image, entry)

    result: Dict[str, Any] = {
        "name": name,
        "id": container["id"],
        "short_id": container["short_id"],
        "current_image": current_image,
        "target_image": target_ref,
        "strategy": strategy,
        "target_tag": entry.get("target_tag") or "",
        "status": container["status"],
        "running": container["running"],
        "local_digest": None,
        "remote_digest": None,
        "source": None,
        "update_available": False,
        "note": None,
        "error": None,
        "slot": slot,
    }

    if container.get("is_self"):
        result["note"] = "自身容器，跳过检查"
        logger.info("%s[%s] 自身容器，跳过检查", prefix, name)
        return result

    if manual:
        progress.start(task_key, f"检测 {name}", total=100, message="查询本地镜像…")

    try:
        loop = asyncio.get_running_loop()
        local_digest = await loop.run_in_executor(None, docker_service.get_local_digest, target_ref)
        result["local_digest"] = local_digest

        if manual:
            progress.update(task_key, done=30, message="查询远程 digest…")

        raw_mirrors, use_direct, reason = _resolve_check_source(target_ref)
        mirrors_for_check = _expand_mirrors_for_check(raw_mirrors)

        remote_digest, source = await loop.run_in_executor(
            None,
            lambda: get_remote_digests_multi(
                target_ref,
                mirrors_for_check,
                (cfg.updater.registry_username or "").strip() or None,
                (cfg.updater.registry_password or "").strip() or None,
                use_direct,
            ),
        )
        result["remote_digest"] = remote_digest
        result["source"] = source

        if manual:
            progress.update(task_key, done=80, message="比较 digest…")

        if remote_digest is None:
            result["error"] = "无法获取远程 digest（网络或 registry 不可达）"
        elif local_digest is None:
            exists = await loop.run_in_executor(None, docker_service.image_exists, target_ref)
            if not exists:
                result["update_available"] = True
                result["note"] = "本地不存在目标镜像，可拉取"
            else:
                result["note"] = "本地镜像缺少 RepoDigest，状态未知"
        else:
            result["update_available"] = local_digest != remote_digest

        if result.get("error"):
            logger.warning("%s[%s] 检查失败：%s", prefix, name, result["error"])
        elif result.get("update_available"):
            logger.info(
                "%s[%s] 发现新版本 %s -> %s (%s) [%s]",
                prefix,
                name,
                _short_digest(local_digest),
                _short_digest(remote_digest),
                source or "direct",
                reason,
            )
        elif result.get("note"):
            logger.info("%s[%s] %s", prefix, name, result["note"])
        else:
            logger.info("%s[%s] 已是最新 (%s)", prefix, name, _short_digest(remote_digest))
    except Exception as e:
        logger.exception("%s[%s] 检查异常", prefix, name)
        result["error"] = str(e)
    finally:
        if manual:
            if result.get("error"):
                progress.finish(task_key, "检测失败")
            elif result.get("update_available"):
                progress.finish(task_key, "发现新版本")
            elif result.get("note"):
                progress.finish(task_key, result["note"])
            else:
                progress.finish(task_key, "已是最新")

    return result


# ============================================================
#  全容器检测
# ============================================================
async def run_check_all() -> List[Dict[str, Any]]:
    global last_check_time, check_in_progress

    async with state_lock:
        check_in_progress = True
        try:
            if config.updater.wait_for_proxy_ready:
                if not _has_available_proxy():
                    wait_total = max(1, int(config.updater.proxy_ready_timeout_minutes or 15))
                    logger.info("[容器检测] 镜像加速尚未就绪，最多等待 %d 分钟…", wait_total)
                    progress.start("updater_wait", "等待镜像加速", total=wait_total * 60, message="等待节点检测完成…")
                    deadline = time.time() + wait_total * 60
                    ok = False
                    start_ts = time.time()
                    while time.time() < deadline:
                        if _has_available_proxy():
                            ok = True
                            break
                        elapsed = int(time.time() - start_ts)
                        progress.update("updater_wait", done=min(elapsed, wait_total * 60), message=f"已等待 {elapsed}s / {wait_total * 60}s")
                        await asyncio.sleep(5)

                    if ok:
                        elapsed = int(time.time() - start_ts)
                        logger.info("[容器检测] 镜像加速已就绪（等待 %ds），开始容器检测", elapsed)
                        progress.finish("updater_wait", f"就绪（等待 {elapsed}s）")
                    else:
                        logger.warning("[容器检测] 等待镜像加速超时（%d 分钟），仍继续执行容器检测", wait_total)
                        progress.finish("updater_wait", "超时，继续执行")
                else:
                    logger.info("[容器检测] 镜像加速已就绪，开始容器检测")

            cfg = config
            concurrency = max(1, min(int(cfg.updater.check_concurrency or 2), 20))
            loop = asyncio.get_running_loop()
            containers = await loop.run_in_executor(None, docker_service.list_containers)
            logger.info("开始检查容器，共 %d 个（并发 %d）", len(containers), concurrency)

            progress.start("updater_check", "容器检测", total=len(containers), message=f"共 {len(containers)} 个容器")

            slots: "asyncio.Queue[int]" = asyncio.Queue()
            for i in range(1, concurrency + 1):
                slots.put_nowait(i)

            done_count = 0
            progress_lock = asyncio.Lock()

            async def _do(c: Dict[str, Any]) -> Dict[str, Any]:
                nonlocal done_count
                slot = await slots.get()
                try:
                    r = await check_one(c, cfg, slot=slot)
                finally:
                    slots.put_nowait(slot)
                    async with progress_lock:
                        done_count += 1
                        progress.update("updater_check", done=done_count)
                return r

            results = list(await asyncio.gather(*[_do(c) for c in containers]))

            check_results.clear()
            for r in results:
                check_results[r["name"]] = r
            last_check_time = datetime.now().isoformat(timespec="seconds")
            _save_state()

            updatable = sum(1 for r in results if r.get("update_available") and not r.get("error"))
            err_count = sum(1 for r in results if r.get("error"))
            logger.info("检查完成：%d 个容器，%d 个可更新，%d 个出错", len(results), updatable, err_count)
            progress.finish("updater_check", f"完成，{updatable} 个可更新")

            if cfg.updater.auto_update:
                for r in results:
                    if r.get("update_available") and not r.get("error") and r["name"] not in running_updates:
                        logger.info("[%s] 触发自动更新", r["name"])
                        try:
                            await perform_update(r["name"], cfg)
                        except Exception:
                            logger.exception("自动更新 %s 失败", r["name"])

            return results
        finally:
            check_in_progress = False


# ============================================================
#  容器更新
# ============================================================
async def perform_update(container_name: str, cfg=None) -> Dict[str, Any]:
    if container_name in running_updates:
        raise HTTPException(status_code=409, detail=f"{container_name} 正在更新中")

    cfg = cfg or config
    entry = (cfg.updater.containers or {}).get(container_name) or {}

    task_key = f"upd_pull:{container_name}"
    cancel_requested.pop(container_name, None)
    running_updates[container_name] = "pulling"
    progress.start(task_key, f"更新 {container_name}", total=100, message="读取容器信息…")
    logger.info("[%s] 开始更新流程", container_name)
    try:
        loop = asyncio.get_running_loop()

        def _prepare():
            container = docker_service.client.containers.get(container_name)
            current_image = container.attrs["Config"]["Image"]
            target = _target_reference(current_image, entry)

            raw_mirrors, _use_direct, reason = _resolve_check_source(target)
            pull_use_mirror = bool(cfg.updater.pull_use_mirror)

            if pull_use_mirror:
                mirrors = _expand_mirrors_for_pull(raw_mirrors)
            else:
                mirrors = []

            return container, target, mirrors, reason, pull_use_mirror

        container, target, mirrors, reason, pull_use_mirror = await loop.run_in_executor(None, _prepare)

        if _is_cancelled(container_name):
            raise PullCancelled()

        progress.update(task_key, done=15, message="拉取镜像…")
        logger.info(
            "[%s] 拉取镜像 %s（策略：%s；加速源：%s）",
            container_name,
            target,
            reason,
            ", ".join(mirrors) if (pull_use_mirror and mirrors) else "未使用（仅 direct）",
        )

        running_updates[container_name] = "pulling"

        _PULL_DONE_MIN = 15
        _PULL_DONE_MAX = 80
        _EMIT_INTERVAL = 0.4

        _pull_state: Dict[str, float] = {
            "max_current": 0,
            "max_total": 0,
            "last_done": _PULL_DONE_MIN,
            "last_emit": 0.0,
        }

        def _fmt_bytes(n: int) -> str:
            if not n or n <= 0:
                return "0 B"
            units = ("B", "KB", "MB", "GB", "TB")
            v = float(n)
            i = 0
            while v >= 1024 and i < len(units) - 1:
                v /= 1024.0
                i += 1
            if i == 0:
                return f"{int(v)} {units[i]}"
            return f"{v:.1f} {units[i]}"

        def _on_pull_progress(ev: Dict[str, Any]) -> None:
            if not isinstance(ev, dict):
                return

            cur = int(ev.get("current") or 0)
            tot = int(ev.get("total") or 0)

            if tot > _pull_state["max_total"]:
                _pull_state["max_total"] = tot
            if cur > _pull_state["max_current"]:
                _pull_state["max_current"] = cur

            now = time.time()
            if now - _pull_state["last_emit"] < _EMIT_INTERVAL:
                return
            _pull_state["last_emit"] = now

            total_b = int(_pull_state["max_total"])
            cur_b = int(_pull_state["max_current"])

            if total_b <= 0:
                progress.update(task_key, message="拉取镜像（准备中…）")
                return

            ratio = cur_b / total_b
            if ratio > 1.0:
                ratio = 1.0
            done = _PULL_DONE_MIN + int(ratio * (_PULL_DONE_MAX - _PULL_DONE_MIN))

            if done < _pull_state["last_done"]:
                done = int(_pull_state["last_done"])
            else:
                _pull_state["last_done"] = done

            progress.update(
                task_key,
                done=done,
                message=f"拉取镜像 {_fmt_bytes(cur_b)} / {_fmt_bytes(total_b)}",
            )

        def _pull():
            return docker_service.pull_image(
                target,
                mirrors,
                bool(cfg.updater.use_direct),
                should_cancel=lambda: _is_cancelled(container_name),
                on_progress=_on_pull_progress,
            )

        image_ref, source = await loop.run_in_executor(None, _pull)

        progress.update(task_key, done=_PULL_DONE_MAX, message="拉取完成，准备重建…")

        if _is_cancelled(container_name):
            logger.info("[%s] 拉取完成，但已在重建前收到取消请求", container_name)
            raise PullCancelled()

        running_updates[container_name] = "recreating"
        progress.update(task_key, done=85, message="重建容器…")
        logger.info("[%s] 重建容器（镜像 %s，来源 %s）", container_name, image_ref, source)

        info = await loop.run_in_executor(
            None,
            lambda: docker_service.recreate_container(container, image_ref),
        )
        info["source"] = source
        info["target_image"] = target

        logger.info("[%s] 容器已重建", container_name)

        try:
            _maybe_clear_pin_policy(container_name, target)
        except Exception as e:
            logger.warning("[%s] 自动清理 pin 策略失败: %s", container_name, e)

        progress.update(task_key, done=95, message="校验更新结果（查询远程 digest）…")
        logger.info("[%s] 开始更新后版本校验", container_name)

        verify_result: Optional[Dict[str, Any]] = None
        try:
            verify_result = await _refresh_one(container_name, task_key=task_key)
        except Exception as e:
            logger.warning("[%s] 更新后校验异常: %s", container_name, e)

        if verify_result is None:
            logger.warning("[%s] 更新后校验：容器列表中未找到该容器", container_name)
            info["verified"] = None
            progress.finish(task_key, "更新完成（校验跳过）")
        elif verify_result.get("error"):
            logger.warning("[%s] 更新后校验失败：%s", container_name, verify_result.get("error"))
            info["verified"] = False
            info["verify_error"] = verify_result.get("error")
            progress.finish(task_key, f"更新完成（校验失败：{verify_result.get('error')}）")
        elif verify_result.get("update_available"):
            logger.warning(
                "[%s] 更新后校验：仍检测到新版本（本地=%s 远程=%s，上游可能刚发布）",
                container_name,
                _short_digest(verify_result.get("local_digest")),
                _short_digest(verify_result.get("remote_digest")),
            )
            info["verified"] = False
            info["verify_note"] = "上游在更新期间发布了新版本"
            progress.finish(task_key, "更新完成（⚠️ 远程又有新版本）")
        else:
            logger.info(
                "[%s] 更新后校验通过：本地与远程 digest 一致（%s）",
                container_name,
                _short_digest(verify_result.get("remote_digest")),
            )
            info["verified"] = True
            progress.finish(task_key, "更新完成，已是最新")

        return info
    except PullCancelled:
        logger.info("[%s] 更新已取消", container_name)
        progress.finish(task_key, "已取消")
        return {
            "ok": True,
            "cancelled": True,
            "name": container_name,
            "target_image": entry.get("target_tag") or "",
        }
    except Exception:
        logger.exception("[%s] 更新失败", container_name)
        progress.finish(task_key, "更新失败")
        raise
    finally:
        running_updates.pop(container_name, None)
        cancel_requested.pop(container_name, None)


def _maybe_clear_pin_policy(container_name: str, new_image_ref: str) -> None:
    try:
        entry = (config.updater.containers or {}).get(container_name) or {}
        if (entry.get("strategy") or "track").lower() != "pin":
            return
        pin_tag = (entry.get("target_tag") or "").strip()
        if not pin_tag:
            return
        _, _, new_tag = parse_image_reference(new_image_ref or "")
        if pin_tag != new_tag:
            return

        containers = dict(config.updater.containers or {})
        containers.pop(container_name, None)
        config.updater.containers = containers
        save_config()
        logger.info(
            "[%s] pin 策略已自动清理（target_tag=%s 与更新后的实际 tag 一致），恢复为跟随当前 tag",
            container_name,
            pin_tag,
        )
    except Exception as e:
        logger.warning("[%s] 自动清理 pin 策略异常: %s", container_name, e)


async def _refresh_one(
    name: str,
    task_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    try:
        cfg = config
        loop = asyncio.get_running_loop()
        containers = await loop.run_in_executor(None, docker_service.list_containers)
        for c in containers:
            if c["name"] == name:
                if task_key:
                    progress.update(
                        task_key,
                        done=96,
                        message="校验更新结果（比对本地 / 远程 digest）…",
                    )
                r = await check_one(c, cfg)
                check_results[name] = r
                _save_state()
                return r
    except Exception:
        logger.exception("刷新 %s 状态失败", name)
    return None


async def run_log_cleanup() -> None:
    try:
        retention = int(config.updater.log_retention_days or 7)
        max_entries = int(config.updater.log_max_entries or 5000)
        stat = log_handler.cleanup_file(retention_days=retention, max_entries=max_entries)
        if stat.get("removed_expired") or stat.get("removed_overflow"):
            logger.info(
                "日志清理：读取 %d 条，保留 %d 条，按时间清理 %d 条，按上限清理 %d 条",
                stat["read"],
                stat["kept"],
                stat["removed_expired"],
                stat["removed_overflow"],
            )
    except Exception:
        logger.exception("日志清理失败")
