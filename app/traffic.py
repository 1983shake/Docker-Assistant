"""流量统计 + 拉取历史 + 待定拉取管理。"""

import logging
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select, func

from app.db import (
    engine,
    TrafficStats,
    PullHistory,
    ProxyNode,
    get_shanghai_time,
)

logger = logging.getLogger("dockerassistant.traffic")

# 内存缓存
_recent_pulls: dict[str, datetime] = {}
_MAX_RECENT_PULLS = 500
_DEDUP_WINDOW = 60

# 失败/取消的短窗口去重（防止 Docker daemon HEAD+GET 或短时间重试造成重复）
_recent_failures: dict[str, datetime] = {}
_FAIL_DEDUP_WINDOW = 5  # 秒
_MAX_RECENT_FAILURES = 500

# 待定拉取：manifest 成功登记，等 blob 到达后提升为正式记录
# key = f"{client_ip}_{norm_image}"
_pending_pulls: dict[str, dict] = {}
_PENDING_TTL = 300  # 5 分钟：仅探测、无 blob，自动过期丢弃
_INFLIGHT_TTL = 3600  # 1 小时：已提升，等待整次拉取的所有 blob 完成

# 网络速率滑动窗口
_SPEED_WINDOW = 5.0
_recent_speeds: deque = deque(maxlen=1000)


def _normalize_image(image: str) -> str:
    """规范化镜像名，去掉常见 registry 前缀，便于去重比较。"""
    if not image:
        return image
    for prefix in (
        "ghcr.io/",
        "ghcr/",
        "docker.io/",
        "docker/",
        "library/",
        "quay.io/",
        "quay/",
        "gcr.io/",
        "gcr/",
        "registry.k8s.io/",
        "k8s.gcr.io/",
        "mcr.microsoft.com/",
        "mcr/",
        "nvcr.io/",
        "nvcr/",
        "docker.elastic.co/",
        "elastic/",
    ):
        if image.startswith(prefix):
            return image[len(prefix) :]
    return image


def log_traffic(bytes_downloaded: int = 0, bytes_uploaded: int = 0, node_id: int = None):
    if bytes_downloaded <= 0 and bytes_uploaded <= 0 and node_id is None:
        return

    if bytes_downloaded > 0 or bytes_uploaded > 0:
        _recent_speeds.append((time.time(), max(0, int(bytes_downloaded)), max(0, int(bytes_uploaded))))

    today_str = get_shanghai_time().date().isoformat()

    with Session(engine) as session:
        stats = session.exec(select(TrafficStats).where(TrafficStats.date == today_str)).first()

        if not stats:
            stats = TrafficStats(date=today_str)
            session.add(stats)

        stats.download_bytes += max(0, bytes_downloaded)
        stats.upload_bytes += max(0, bytes_uploaded)
        stats.request_count += 1

        if node_id is not None and bytes_downloaded > 0:
            node = session.get(ProxyNode, node_id)
            if node:
                node.download_bytes += bytes_downloaded
                session.add(node)

        session.commit()


def get_network_speed(window: float = _SPEED_WINDOW) -> dict:
    now = time.time()
    cutoff = now - window

    while _recent_speeds and _recent_speeds[0][0] < cutoff:
        _recent_speeds.popleft()

    if not _recent_speeds:
        return {"down_bps": 0.0, "up_bps": 0.0, "total_bps": 0.0, "window": window, "samples": 0}

    total_down = 0
    total_up = 0
    for _, d, u in _recent_speeds:
        total_down += d
        total_up += u

    first_ts = _recent_speeds[0][0]
    span = now - first_ts
    if span < 0.5:
        span = 0.5
    elif span > window:
        span = window

    down_bps = total_down / span
    up_bps = total_up / span

    return {
        "down_bps": round(down_bps, 2),
        "up_bps": round(up_bps, 2),
        "total_bps": round(down_bps + up_bps, 2),
        "window": window,
        "samples": len(_recent_speeds),
    }


# ============================================================
#  待定拉取管理
# ============================================================


def _cleanup_pending(now: datetime = None):
    now = now or datetime.now()
    for k in list(_pending_pulls.keys()):
        v = _pending_pulls[k]
        ttl = _INFLIGHT_TTL if v.get("pull_id") else _PENDING_TTL
        if (now - v["ts"]).total_seconds() > ttl:
            _pending_pulls.pop(k, None)
            logger.debug(f"[pull-pending] 过期丢弃: {k}")


def mark_pending_pull(
    image: str,
    tag: str,
    client_ip: str,
    node_id: int = None,
    node_name: str = None,
):
    """manifest 请求成功时调用。写入内存待定区，等 blob 流量确认拉取。"""
    if not image or not client_ip:
        return
    norm = _normalize_image(image)
    if not norm:
        return

    key = f"{client_ip}_{norm}"
    _pending_pulls[key] = {
        "image": image,
        "tag": tag,
        "client_ip": client_ip,
        "node_id": node_id,
        "node_name": node_name,
        "ts": datetime.now(),
        "pull_id": None,
    }
    _cleanup_pending()
    logger.debug(f"[pull-pending] 记录待定拉取: {key} tag={tag}")


def ensure_pull_record(image: str, client_ip: str) -> Optional[int]:
    """确保存在一条拉取记录（首次调用创建，后续调用复用 pull_id）。"""
    if not image or not client_ip:
        return None
    norm = _normalize_image(image)
    if not norm:
        return None

    key = f"{client_ip}_{norm}"
    pending = _pending_pulls.get(key)
    if not pending:
        logger.debug(f"[pull-pending] 未命中待定条目: {key}")
        return None

    if pending.get("pull_id"):
        pending["ts"] = datetime.now()
        return pending["pull_id"]

    with Session(engine) as session:
        pull = PullHistory(
            image=pending["image"],
            tag=pending["tag"],
            client_ip=pending["client_ip"],
            node_id=pending["node_id"],
            node_name=pending["node_name"],
            status="success",
        )
        session.add(pull)
        session.commit()
        session.refresh(pull)

    pending["pull_id"] = pull.id
    pending["ts"] = datetime.now()
    logger.info(f"[pull] 待定拉取提升为记录 id={pull.id} " f"image={pending['image']} tag={pending['tag']} " f"client={pending['client_ip']}")
    return pull.id


def add_bytes_to_pull(pull_id: int, bytes_added: int):
    """按 pull_id 精确累加字节，每个 blob 请求一次。"""
    if not pull_id or bytes_added <= 0:
        return
    try:
        with Session(engine) as session:
            pull = session.get(PullHistory, pull_id)
            if pull:
                pull.download_bytes = (pull.download_bytes or 0) + bytes_added
                session.add(pull)
                session.commit()
    except Exception as e:
        logger.error(f"累加拉取字节失败: {e}")


# ============================================================
#  拉取记录（成功 / 失败 / 取消）
# ============================================================


def _cleanup_recent_failures(now: datetime):
    if len(_recent_failures) <= _MAX_RECENT_FAILURES:
        return
    cutoff = now - timedelta(seconds=_FAIL_DEDUP_WINDOW * 4)
    for k in [k for k, v in _recent_failures.items() if v < cutoff]:
        _recent_failures.pop(k, None)


def log_pull(
    image: str,
    tag: str,
    client_ip: str,
    node_id: int = None,
    node_name: str = None,
    status: str = "success",
    error_message: str = None,
) -> Optional[int]:
    """记录/更新一次拉取历史。

    - status="success"：
        · 60 秒内同 key 已有记录 → 返回 None（不重复创建）
        · 否则 → 创建新记录
    - status="failed"/"cancelled"：
        · 5 秒内同 key 同状态已记录过 → 返回 None（防止 HEAD+GET/重试重复）
        · 优先按 pending 的 pull_id 更新现有记录
        · 否则查找 60 秒内同 key 的 success 记录并更新
        · 都没有 → 创建新记录（带状态）
    """
    if not image or not client_ip:
        return None
    norm = _normalize_image(image) or image

    # 尝试从 pending 补充 tag（blob 请求路径里没有 tag 信息）
    pending_key = f"{client_ip}_{norm}"
    pending = _pending_pulls.get(pending_key)
    if not tag and pending:
        tag = pending.get("tag") or "latest"
    if not tag:
        tag = "latest"

    now = datetime.now()
    cache_key = f"{client_ip}_{norm}_{tag}"

    # ---------- 失败 / 取消：先做短窗口去重 ----------
    if status in ("failed", "cancelled"):
        fail_key = f"{client_ip}_{norm}_{tag}_{status}"
        last_fail = _recent_failures.get(fail_key)
        if last_fail and (now - last_fail).total_seconds() < _FAIL_DEDUP_WINDOW:
            logger.debug(f"[pull] 失败记录去重（{_FAIL_DEDUP_WINDOW}s 内）: {fail_key}")
            return None
        _recent_failures[fail_key] = now
        _cleanup_recent_failures(now)

        # 1a) pending 里已有 pull_id → 更新该记录
        if pending and pending.get("pull_id"):
            try:
                with Session(engine) as session:
                    pull = session.get(PullHistory, pending["pull_id"])
                    if pull and pull.status == "success":
                        pull.status = status
                        pull.error_message = error_message
                        session.add(pull)
                        session.commit()
                        logger.info(f"[pull] 更新状态 id={pull.id} -> {status}（{error_message}）")
                        return pull.id
            except Exception as e:
                logger.error(f"更新拉取状态失败: {e}")

        # 1b) DB 里查找最近 60 秒同 key 的 success 记录 → 更新
        try:
            with Session(engine) as session:
                cutoff = get_shanghai_time() - timedelta(seconds=_DEDUP_WINDOW)
                existing = session.exec(
                    select(PullHistory)
                    .where(PullHistory.client_ip == client_ip)
                    .where(PullHistory.tag == tag)
                    .where(PullHistory.status == "success")
                    .where(PullHistory.request_time > cutoff)
                    .where(PullHistory.image.contains(norm))
                    .order_by(PullHistory.request_time.desc())
                    .limit(1)
                ).first()
                if existing:
                    existing.status = status
                    existing.error_message = error_message
                    session.add(existing)
                    session.commit()
                    logger.info(f"[pull] 更新状态 id={existing.id} -> {status}（{error_message}）")
                    return existing.id
        except Exception as e:
            logger.error(f"查找并更新拉取记录失败: {e}")

        # 1c) 无记录可更新 → 直接创建失败/取消记录
        with Session(engine) as session:
            pull = PullHistory(
                image=image,
                tag=tag,
                client_ip=client_ip,
                node_id=node_id,
                node_name=node_name,
                status=status,
                error_message=error_message,
            )
            session.add(pull)
            session.commit()
            session.refresh(pull)
            logger.info(f"[pull] 新增记录 id={pull.id} image={image} tag={tag} " f"client={client_ip} status={status}")
            if pending:
                pending["pull_id"] = pull.id
            _recent_pulls[cache_key] = now
            return pull.id

    # ---------- 成功：沿用原有"仅创建一次"逻辑 ----------
    if len(_recent_pulls) > _MAX_RECENT_PULLS:
        cutoff_mem = now - timedelta(seconds=_DEDUP_WINDOW * 2)
        for k in [k for k, v in _recent_pulls.items() if v < cutoff_mem]:
            _recent_pulls.pop(k, None)

    last_seen = _recent_pulls.get(cache_key)
    if last_seen is not None and (now - last_seen).total_seconds() < _DEDUP_WINDOW:
        return None

    with Session(engine) as session:
        pull = PullHistory(
            image=image,
            tag=tag,
            client_ip=client_ip,
            node_id=node_id,
            node_name=node_name,
            status="success",
        )
        session.add(pull)
        session.commit()
        session.refresh(pull)
        logger.info(f"[pull] 新增记录 id={pull.id} image={image} tag={tag} " f"client={client_ip} status=success")
        _recent_pulls[cache_key] = now
        return pull.id


# ============================================================
#  查询 / 清理
# ============================================================


def get_pull_history(limit: int = 100):
    with Session(engine) as session:
        return session.exec(select(PullHistory).order_by(PullHistory.request_time.desc()).limit(limit)).all()


def get_total_pull_count() -> int:
    with Session(engine) as session:
        return session.exec(select(func.count(PullHistory.id))).one()


def get_pull_stats() -> dict:
    with Session(engine) as session:
        total = session.exec(select(func.count(PullHistory.id))).one()
        success = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "success")).one()
        failed = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "failed")).one()
        cancelled = session.exec(select(func.count(PullHistory.id)).where(PullHistory.status == "cancelled")).one()
    return {
        "total": total or 0,
        "success": success or 0,
        "failed": failed or 0,
        "cancelled": cancelled or 0,
    }


def get_traffic_stats(days: int = 30):
    with Session(engine) as session:
        return session.exec(select(TrafficStats).order_by(TrafficStats.date.desc()).limit(days)).all()


def clear_pull_history():
    _recent_pulls.clear()
    _pending_pulls.clear()
    _recent_failures.clear()
    with Session(engine) as session:
        session.exec(PullHistory.__table__.delete())
        session.commit()
