"""不拉取镜像即可查询远程 manifest digest 的 Registry v2 客户端。"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DOCKER_HUB_REGISTRIES = {"registry-1.docker.io", "docker.io", "index.docker.io"}
AUTH_URL = "https://auth.docker.io/token"

ACCEPT_HEADERS = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v1+json",
    ]
)


# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """按镜像源维护失败计数：连续失败达阈值后进入冷却（熔断），冷却结束半开探测。

    - closed    ：正常
    - open      ：熔断中，跳过
    - half-open ：冷却结束，允许一次探测
    """

    def __init__(self, threshold: int = 3, cooldown: int = 300) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown = max(10, int(cooldown))
        self._lock = threading.RLock()
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
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
            logger.warning(
                "[熔断] 镜像源 [%s] 连续失败 %d 次，进入熔断（冷却 %d 秒）",
                key,
                count,
                self.cooldown,
            )
        else:
            logger.info("[熔断] 镜像源 [%s] 失败计数 %d/%d", key, count, self.threshold)

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        with self._lock:
            keys = set(self._failures) | set(self._opened_at)
            return {
                k: {
                    "state": self._state_locked(k),
                    "failures": self._failures.get(k, 0),
                    "remaining": self.remaining(k),
                }
                for k in keys
            }


# 全局熔断器：连续失败 3 次熔断 5 分钟
breaker = CircuitBreaker(threshold=3, cooldown=300)


# --------------------------------------------------------------------------- #
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


def _fetch_auth(
    session: requests.Session,
    scheme: str,
    registry: str,
    repository: str,
    username: Optional[str],
    password: Optional[str],
) -> Optional[str]:
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


def get_remote_digest(
    image_ref: str,
    mirror: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = 20,
) -> Optional[str]:
    """返回远程 manifest digest（形如 sha256:...），失败返回 None。"""
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
            logger.debug("registry %s 返回 %s (%s)", target, r.status_code, scheme)
            if r.status_code in (400, 401, 403, 404):
                continue
        except requests.RequestException as e:
            logger.debug("请求 %s 失败 (%s): %s", target, scheme, e)
            continue
    return None


# --------------------------------------------------------------------------- #
def _probe_source(
    key: str,
    image_ref: str,
    mirror: Optional[str],
    username: Optional[str],
    password: Optional[str],
) -> Optional[str]:
    """带熔断保护地探测单个镜像源；日志中提示源状态。"""
    if not breaker.allow(key):
        logger.warning(
            "[跳过] 镜像源 [%s] 处于熔断状态（剩余 %d 秒），跳过 %s",
            key,
            breaker.remaining(key),
            image_ref,
        )
        return None

    state = breaker.state(key)
    if state == "half-open":
        logger.info("[熔断] 镜像源 [%s] 冷却结束，进行半开探测：%s", key, image_ref)
    else:
        logger.info("[检测] 使用镜像源 [%s] 查询 %s", key, image_ref)

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
    """依次尝试加速源、再直连（均带熔断保护）。返回 (digest, source)。"""
    if is_dockerhub(image_ref):
        for m in mirrors or []:
            m = (m or "").strip()
            if not m:
                continue
            key = normalize_mirror(m)
            d = _probe_source(key, image_ref, m, username, password)
            if d:
                return d, key

        # Docker Hub 官方兜底
        key = "registry-1.docker.io"
        d = _probe_source(key, image_ref, None, username, password)
        if d:
            return d, key
        return None, None

    if use_direct:
        key = parse_image_reference(image_ref)[0]
        d = _probe_source(key, image_ref, None, username, password)
        if d:
            return d, key
    return None, None
