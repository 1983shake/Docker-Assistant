"""Registry v2 客户端（查询远程 digest）。"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from app.config import config

logger = logging.getLogger("dockerassistant.registry")

DOCKER_HUB_REGISTRIES = {"registry-1.docker.io", "docker.io", "index.docker.io"}

ACCEPT_HEADERS = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v1+json",
    ]
)


# ============================================================
#  解析工具
# ============================================================


def parse_image_reference(ref: str) -> Tuple[str, str, str]:
    """拆解镜像引用为 (registry, repository, tag)。"""
    ref = (ref or "").strip()
    if "@" in ref:
        ref = ref.split("@", 1)[0]

    tag = "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)

    parts = ref.split("/")
    if len(parts) == 1:
        registry, repository = "registry-1.docker.io", f"library/{ref}"
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry, repository = parts[0], "/".join(parts[1:])
    else:
        registry, repository = "registry-1.docker.io", ref
    return registry, repository, tag


def normalize_mirror(mirror: str) -> str:
    m = (mirror or "").strip()
    m = re.sub(r"^https?://", "", m)
    return m.rstrip("/")


def is_dockerhub(image_ref: str) -> bool:
    registry, _, _ = parse_image_reference(image_ref)
    return registry in DOCKER_HUB_REGISTRIES


# ============================================================
#  熔断器
# ============================================================


class CircuitBreaker:
    """按镜像源维护失败计数，连续失败达阈值后进入冷却。"""

    def __init__(self, threshold: int = 3, cooldown: int = 300) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown = max(10, int(cooldown))
        self._lock = threading.RLock()
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}

    def _state_locked(self, key: str) -> str:
        opened = self._opened_at.get(key)
        if opened is None:
            return "closed"
        if time.time() - opened >= self.cooldown:
            return "half-open"
        return "open"

    def state(self, key: str) -> str:
        with self._lock:
            return self._state_locked(key)

    def allow(self, key: str) -> bool:
        return self.state(key) != "open"

    def remaining(self, key: str) -> int:
        with self._lock:
            opened = self._opened_at.get(key)
            if opened is None:
                return 0
            return max(0, int(self.cooldown - (time.time() - opened)))

    def record_success(self, key: str) -> None:
        with self._lock:
            was_open = key in self._opened_at
            self._failures.pop(key, None)
            self._opened_at.pop(key, None)
        if was_open:
            logger.warning("[熔断] 镜像源 [%s] 探测成功，已恢复启用", key)

    def record_failure(self, key: str) -> None:
        with self._lock:
            was_open = key in self._opened_at
            count = self._failures.get(key, 0) + 1
            if was_open or count >= self.threshold:
                self._failures[key] = 0
                self._opened_at[key] = time.time()
                tripped = True
            else:
                self._failures[key] = count
                tripped = False
        if was_open:
            logger.warning("[熔断] 镜像源 [%s] 半开探测失败，继续熔断 %d 秒", key, self.cooldown)
        elif tripped:
            logger.warning("[熔断] 镜像源 [%s] 连续失败 %d 次，冷却 %d 秒", key, count, self.cooldown)


breaker = CircuitBreaker(threshold=3, cooldown=300)


# ============================================================
#  Registry v2 查询
# ============================================================


def _fetch_auth(session, scheme, registry, repository, username, password) -> Optional[str]:
    base = f"{scheme}://{registry}"
    try:
        r = session.get(f"{base}/v2/", timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 401:
        return None
    challenge = r.headers.get("WWW-Authenticate", "")
    low = challenge.lower()

    if low.startswith("bearer"):
        params = {}
        for part in re.split(r",\s*", challenge[len("bearer ") :]):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k.strip()] = v.strip().strip('"')
        realm = params.get("realm")
        if not realm:
            return None
        auth = (username, password) if username and password else None
        try:
            tr = session.get(
                realm,
                params={
                    "service": params.get("service", ""),
                    "scope": params.get("scope", f"repository:{repository}:pull"),
                },
                auth=auth,
                timeout=15,
            )
            tr.raise_for_status()
            data = tr.json()
        except Exception as e:
            logger.debug("token 获取失败: %s", e)
            return None
        token = data.get("token") or data.get("access_token")
        return f"Bearer {token}" if token else None

    if low.startswith("basic") and username and password:
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {creds}"
    return None


def get_remote_digest(image_ref, mirror=None, username=None, password=None, timeout=20) -> Optional[str]:
    target = f"{normalize_mirror(mirror)}/{image_ref}" if mirror else image_ref
    registry, repository, tag = parse_image_reference(target)

    for scheme in ("https", "http"):
        try:
            session = requests.Session()
            headers = {"Accept": ACCEPT_HEADERS}
            auth = _fetch_auth(session, scheme, registry, repository, username, password)
            if auth:
                headers["Authorization"] = auth
            url = f"{scheme}://{registry}/v2/{repository}/manifests/{tag}"
            r = session.head(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 405:
                r = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                digest = r.headers.get("Docker-Content-Digest")
                if digest:
                    return digest
                if r.content:
                    return "sha256:" + hashlib.sha256(r.content).hexdigest()
            if r.status_code in (400, 401, 403, 404):
                continue
        except requests.RequestException as e:
            logger.debug("请求 %s 失败 (%s): %s", target, scheme, e)
            continue
    return None


def _probe_source(key, image_ref, mirror, username, password) -> Optional[str]:
    if not breaker.allow(key):
        logger.warning("[跳过] 镜像源 [%s] 熔断中（剩余 %d 秒）", key, breaker.remaining(key))
        return None
    state = breaker.state(key)
    if state == "half-open":
        logger.info("[熔断] 镜像源 [%s] 冷却结束，半开探测：%s", key, image_ref)
    else:
        logger.info("[检测] 镜像源 [%s] 查询 %s", key, image_ref)

    digest = get_remote_digest(image_ref, mirror=mirror, username=username, password=password)
    if digest:
        breaker.record_success(key)
        logger.info("[成功] 镜像源 [%s] 返回 digest %s（%s）", key, digest, image_ref)
    else:
        breaker.record_failure(key)
    return digest


def get_remote_digests_multi(
    image_ref: str,
    mirrors: List[str],
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_direct: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """依次尝试所有镜像源，再直连。返回 (digest, source)。"""
    for m in mirrors or []:
        m = (m or "").strip()
        if not m:
            continue
        key = normalize_mirror(m)
        d = _probe_source(key, image_ref, m, username, password)
        if d:
            return d, key

    if use_direct:
        key = parse_image_reference(image_ref)[0]
        d = _probe_source(key, image_ref, None, username, password)
        if d:
            return d, key

    return None, None
