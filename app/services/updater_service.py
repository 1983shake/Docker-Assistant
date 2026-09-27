"""容器更新器核心逻辑（合并自 DockerImageUpdater/main.py）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.config import DATA_DIR, config, save_config
from app.services.docker_service import docker_service
from app.services.log_handler import log_handler
from app.services.registry_client import get_remote_digests_multi, parse_image_reference

STATE_FILE = DATA_DIR / "updater_results.json"

# 全局状态
state_lock = asyncio.Lock()
check_results: Dict[str, Dict[str, Any]] = {}
last_check_time: Optional[str] = None
running_updates: Dict[str, str] = {}
check_in_progress: bool = False
container_checking: Dict[str, bool] = {}


# --------------------------------------------------------------------------- #
def _load_state() -> None:
    global last_check_time
    if not STATE_FILE.exists():
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
    except Exception:
        pass


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


# --------------------------------------------------------------------------- #
async def check_one(
    container: Dict[str, Any],
    cfg,
    slot: Optional[int] = None,
    manual: bool = False,
) -> Dict[str, Any]:
    import logging

    logger = logging.getLogger("dockerassistant.updater")

    name = container["name"]
    prefix = "[手动]" if manual else (f"[C{slot}]" if slot else "")

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

    try:
        loop = asyncio.get_running_loop()
        local_digest = await loop.run_in_executor(None, docker_service.get_local_digest, target_ref)
        result["local_digest"] = local_digest

        remote_digest, source = await loop.run_in_executor(
            None,
            lambda: get_remote_digests_multi(
                target_ref,
                cfg.updater.mirrors or [],
                (cfg.updater.registry_username or "").strip() or None,
                (cfg.updater.registry_password or "").strip() or None,
                bool(cfg.updater.use_direct),
            ),
        )
        result["remote_digest"] = remote_digest
        result["source"] = source

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
                "%s[%s] 发现新版本 %s -> %s (%s)",
                prefix,
                name,
                _short_digest(local_digest),
                _short_digest(remote_digest),
                source or "direct",
            )
        elif result.get("note"):
            logger.info("%s[%s] %s", prefix, name, result["note"])
        else:
            logger.info("%s[%s] 已是最新 (%s)", prefix, name, _short_digest(remote_digest))
    except Exception as e:
        logger.exception("%s[%s] 检查异常", prefix, name)
        result["error"] = str(e)

    return result


async def run_check_all() -> List[Dict[str, Any]]:
    global last_check_time, check_in_progress
    import logging

    logger = logging.getLogger("dockerassistant.updater")

    async with state_lock:
        check_in_progress = True
        try:
            cfg = config
            concurrency = max(1, min(int(cfg.updater.check_concurrency or 2), 20))
            loop = asyncio.get_running_loop()
            containers = await loop.run_in_executor(None, docker_service.list_containers)
            logger.info("开始检查容器，共 %d 个（并发 %d）", len(containers), concurrency)

            slots: "asyncio.Queue[int]" = asyncio.Queue()
            for i in range(1, concurrency + 1):
                slots.put_nowait(i)

            async def _do(c: Dict[str, Any]) -> Dict[str, Any]:
                slot = await slots.get()
                try:
                    return await check_one(c, cfg, slot=slot)
                finally:
                    slots.put_nowait(slot)

            results = list(await asyncio.gather(*[_do(c) for c in containers]))

            check_results.clear()
            for r in results:
                check_results[r["name"]] = r
            last_check_time = datetime.now().isoformat(timespec="seconds")
            _save_state()

            updatable = sum(1 for r in results if r.get("update_available") and not r.get("error"))
            logger.info("检查完成：%d 个容器，%d 个可更新", len(results), updatable)

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


# --------------------------------------------------------------------------- #
async def perform_update(container_name: str, cfg=None) -> Dict[str, Any]:
    import logging

    logger = logging.getLogger("dockerassistant.updater")

    if container_name in running_updates:
        raise HTTPException(status_code=409, detail=f"{container_name} 正在更新中")

    cfg = cfg or config
    entry = (cfg.updater.containers or {}).get(container_name) or {}

    running_updates[container_name] = "pulling"
    logger.info("[%s] 开始更新流程", container_name)
    try:
        loop = asyncio.get_running_loop()

        def _work() -> Dict[str, Any]:
            container = docker_service.client.containers.get(container_name)
            current_image = container.attrs["Config"]["Image"]
            target = _target_reference(current_image, entry)

            pull_use_mirror = bool(cfg.updater.pull_use_mirror)
            mirrors = (cfg.updater.mirrors or []) if pull_use_mirror else []

            running_updates[container_name] = "pulling"
            logger.info(
                "[%s] 拉取镜像 %s（镜像加速源：%s）",
                container_name,
                target,
                "启用" if pull_use_mirror and mirrors else "未使用",
            )
            image_ref, source = docker_service.pull_image(
                target,
                mirrors,
                bool(cfg.updater.use_direct),
            )

            running_updates[container_name] = "recreating"
            logger.info("[%s] 重建容器（镜像 %s，来源 %s）", container_name, image_ref, source)
            info = docker_service.recreate_container(container, image_ref)
            info["source"] = source
            info["target_image"] = target
            return info

        result = await loop.run_in_executor(None, _work)
        logger.info("[%s] 更新完成", container_name)
        return result
    except Exception:
        logger.exception("[%s] 更新失败", container_name)
        raise
    finally:
        running_updates.pop(container_name, None)


async def _refresh_one(name: str) -> None:
    import logging

    logger = logging.getLogger("dockerassistant.updater")
    try:
        cfg = config
        loop = asyncio.get_running_loop()
        containers = await loop.run_in_executor(None, docker_service.list_containers)
        for c in containers:
            if c["name"] == name:
                check_results[name] = await check_one(c, cfg)
                _save_state()
                break
    except Exception:
        logger.exception("刷新 %s 状态失败", name)


async def run_log_cleanup() -> None:
    import logging

    logger = logging.getLogger("dockerassistant.updater")
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
