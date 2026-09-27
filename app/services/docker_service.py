"""Docker 交互层：列容器、查本地 digest、拉取、重建容器、镜像管理。"""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict, List, Optional, Tuple

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from .registry_client import is_dockerhub, normalize_mirror

logger = logging.getLogger(__name__)


class DockerService:
    def __init__(self) -> None:
        self._client: Optional[docker.DockerClient] = None
        self.self_id = socket.gethostname()

    # ------------------------------------------------------------------ #
    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def ping(self) -> bool:
        try:
            self.client.ping()
            return True
        except Exception:
            self._client = None
            return False

    def is_self(self, container_id: str) -> bool:
        return container_id.startswith(self.self_id[:12])

    # ------------------------------------------------------------------ #
    def list_containers(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in self.client.containers.list(all=True):
            attrs = c.attrs
            cfg = attrs.get("Config") or {}
            labels = cfg.get("Labels") or {}
            out.append(
                {
                    "id": c.id,
                    "short_id": c.id[:12],
                    "name": c.name,
                    "image": cfg.get("Image"),
                    "image_id": attrs.get("Image"),
                    "status": c.status,
                    "running": c.status == "running",
                    "created": attrs.get("Created"),
                    "labels": labels,
                    "compose_project": labels.get("com.docker.compose.project"),
                    "compose_service": labels.get("com.docker.compose.service"),
                    "is_self": self.is_self(c.id),
                }
            )
        return out

    # ------------------------------------------------------------------ #
    def image_exists(self, image_ref: str) -> bool:
        try:
            self.client.images.get(image_ref)
            return True
        except ImageNotFound:
            return False

    def get_local_digest(self, image_ref: str) -> Optional[str]:
        try:
            img = self.client.images.get(image_ref)
        except ImageNotFound:
            return None
        for d in img.attrs.get("RepoDigests") or []:
            if "@sha256:" in d:
                return d.split("@", 1)[-1]
        return None

    # ------------------------------------------------------------------ #
    def pull_image(self, image_ref: str, mirrors: List[str], use_direct: bool = True) -> Tuple[str, str]:
        """拉取镜像，返回 (最终使用的镜像引用, 源标签)。"""
        candidates: List[Tuple[str, str]] = []
        if is_dockerhub(image_ref):
            for m in mirrors or []:
                m = m.strip()
                if not m:
                    continue
                candidates.append((f"{normalize_mirror(m)}/{image_ref}", normalize_mirror(m)))
        if use_direct or not candidates:
            candidates.append((image_ref, "direct"))

        errors: List[str] = []
        for src, label in candidates:
            try:
                logger.info("从 %s 拉取 %s", label, src)
                self.client.images.pull(src)
                if src != image_ref:
                    img = self.client.images.get(src)
                    img.tag(image_ref)
                return image_ref, label
            except Exception as e:
                logger.warning("从 %s 拉取 %s 失败: %s", label, src, e)
                errors.append(f"{label}: {e}")
        raise RuntimeError("所有拉取源均失败 -> " + "; ".join(errors))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_create_kwargs(attrs: Dict[str, Any], image: str) -> Dict[str, Any]:
        cfg = attrs.get("Config") or {}
        hc = attrs.get("HostConfig") or {}
        ns = attrs.get("NetworkSettings") or {}

        kwargs: Dict[str, Any] = {"detach": True}

        if cfg.get("Env"):
            kwargs["environment"] = cfg["Env"]
        if cfg.get("Cmd"):
            kwargs["command"] = cfg["Cmd"]
        if cfg.get("Entrypoint"):
            kwargs["entrypoint"] = cfg["Entrypoint"]
        if cfg.get("WorkingDir"):
            kwargs["working_dir"] = cfg["WorkingDir"]
        if cfg.get("User"):
            kwargs["user"] = cfg["User"]
        if cfg.get("Hostname"):
            kwargs["hostname"] = cfg["Hostname"]
        if cfg.get("Labels"):
            kwargs["labels"] = cfg["Labels"]
        kwargs["tty"] = bool(cfg.get("Tty"))
        kwargs["stdin_open"] = bool(cfg.get("OpenStdin"))
        if cfg.get("StopSignal"):
            kwargs["stop_signal"] = cfg["StopSignal"]

        # HostConfig
        rp = hc.get("RestartPolicy") or {}
        if rp.get("Name"):
            kwargs["restart_policy"] = {
                "Name": rp["Name"],
                "MaximumRetryCount": int(rp.get("MaximumRetryCount") or 0),
            }
        if hc.get("Privileged"):
            kwargs["privileged"] = True
        if hc.get("CapAdd"):
            kwargs["cap_add"] = hc["CapAdd"]
        if hc.get("CapDrop"):
            kwargs["cap_drop"] = hc["CapDrop"]
        if hc.get("Dns"):
            kwargs["dns"] = hc["Dns"]
        if hc.get("Sysctls"):
            kwargs["sysctls"] = hc["Sysctls"]
        if hc.get("ReadonlyRootfs"):
            kwargs["read_only"] = True
        if hc.get("ExtraHosts"):
            kwargs["extra_hosts"] = list(hc["ExtraHosts"])
        if hc.get("Devices"):
            kwargs["devices"] = [f"{d['PathOnHost']}:{d['PathInContainer']}:{d['CgroupPermissions']}" for d in hc["Devices"]]

        # 网络
        net_mode = hc.get("NetworkMode")
        custom_nets = [n for n in (ns.get("Networks") or {}).keys() if n not in ("bridge", "host", "none")]
        if net_mode in ("host", "none"):
            kwargs["network_mode"] = net_mode
        elif custom_nets:
            kwargs["network"] = custom_nets[0]

        # 端口映射
        port_bindings = hc.get("PortBindings") or {}
        ports: Dict[str, Any] = {}
        for cport, binds in port_bindings.items():
            if not binds:
                continue
            if len(binds) == 1:
                b = binds[0]
                hp = b.get("HostPort")
                hi = b.get("HostIp") or ""
                if hp:
                    ports[cport] = (hi, int(hp)) if hi else int(hp)
                else:
                    ports[cport] = None
            else:
                entries = []
                for b in binds:
                    hp = b.get("HostPort")
                    hi = b.get("HostIp") or ""
                    if hp:
                        entries.append((hi, int(hp)) if hi else int(hp))
                if entries:
                    ports[cport] = entries
        if ports:
            kwargs["ports"] = ports

        # 挂载卷（保留绑定与命名卷，不重建匿名卷内容之外的东西）
        volumes: Dict[str, Any] = {}
        for m in attrs.get("Mounts") or []:
            src = m.get("Source") or m.get("Name")
            dest = m.get("Destination")
            if not src or not dest:
                continue
            mode = "rw" if m.get("RW", True) else "ro"
            volumes[src] = {"bind": dest, "mode": mode}
        for b in hc.get("Binds") or []:
            parts = b.split(":")
            if len(parts) == 2:
                volumes.setdefault(parts[0], {"bind": parts[1], "mode": "rw"})
            elif len(parts) >= 3:
                volumes.setdefault(parts[0], {"bind": parts[1], "mode": parts[2]})
        if volumes:
            kwargs["volumes"] = volumes

        return kwargs

    # ------------------------------------------------------------------ #
    def recreate_container(self, container, new_image: str) -> Dict[str, Any]:
        """安全重建：先用临时名创建新容器，成功后再移除旧容器并改名启动。"""
        attrs = container.attrs
        name = container.name
        was_running = container.status == "running"
        ns = attrs.get("NetworkSettings") or {}

        kwargs = self._build_create_kwargs(attrs, new_image)

        tmp_name = f"{name}-updater-tmp"
        try:
            stale = self.client.containers.get(tmp_name)
            logger.warning("清理残留临时容器 %s", tmp_name)
            stale.remove(force=True)
        except NotFound:
            pass

        logger.info("用新镜像 %s 创建临时容器 %s", new_image, tmp_name)
        new_container = self.client.containers.create(image=new_image, name=tmp_name, **kwargs)

        # 连接额外网络
        custom_nets = [n for n in (ns.get("Networks") or {}).keys() if n not in ("bridge", "host", "none")]
        first = kwargs.get("network")
        for n in custom_nets:
            if n == first:
                continue
            try:
                self.client.networks.get(n).connect(new_container)
            except Exception as e:
                logger.warning("连接网络 %s 失败: %s", n, e)

        # 停止/删除旧容器
        if was_running:
            logger.info("停止容器 %s", name)
            try:
                container.stop(timeout=30)
            except Exception as e:
                logger.warning("停止容器失败: %s", e)

        logger.info("删除旧容器 %s", name)
        container.remove(force=True)

        # 重命名并启动
        new_container.rename(name)
        if was_running:
            logger.info("启动新容器 %s", name)
            new_container.start()

        return {
            "id": new_container.id,
            "name": name,
            "image": new_image,
            "started": was_running,
        }

    # ================================================================== #
    #  镜像管理
    # ================================================================== #
    def _image_container_counts(self) -> Dict[str, int]:
        """统计每个镜像 ID 当前被多少容器（含已停止）引用。"""
        counts: Dict[str, int] = {}
        try:
            for c in self.client.containers.list(all=True):
                img_id = (c.attrs or {}).get("Image") or ""
                if not img_id:
                    continue
                counts[img_id] = counts.get(img_id, 0) + 1
        except Exception as e:
            logger.warning("统计镜像引用容器数量失败: %s", e)
        return counts

    def list_images(self) -> List[Dict[str, Any]]:
        """列出本地镜像，每个镜像一条记录（包含全部标签与真实容器引用数）。"""
        counts = self._image_container_counts()

        out: List[Dict[str, Any]] = []
        for img in self.client.images.list():
            attrs = img.attrs or {}
            tags = list(img.tags or [])
            try:
                size = int(attrs.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0

            containers = counts.get(img.id)
            if containers is None:
                try:
                    containers = int(attrs.get("Containers") or 0)
                except (TypeError, ValueError):
                    containers = 0

            out.append(
                {
                    "id": img.id,
                    "short_id": (img.short_id or "").replace("sha256:", "")[:12],
                    "tags": tags,
                    "repo_digests": list(attrs.get("RepoDigests") or []),
                    "size": size,
                    "created": attrs.get("Created") or "",
                    "containers": containers,
                    "dangling": len(tags) == 0,
                }
            )
        return out

    def _containers_using_image(self, image_ref: str) -> List[Dict[str, str]]:
        """列出引用了指定镜像的容器（按 ID 或 name:tag 匹配）。"""
        out: List[Dict[str, str]] = []
        if not image_ref:
            return out
        try:
            for c in self.client.containers.list(all=True):
                attrs = c.attrs or {}
                img_id = attrs.get("Image") or ""
                cfg_image = (attrs.get("Config") or {}).get("Image") or ""
                match = False
                if img_id:
                    if img_id == image_ref:
                        match = True
                    elif image_ref.startswith("sha256:") and img_id.startswith(image_ref):
                        match = True
                    elif img_id.startswith(image_ref):
                        match = True
                if not match and cfg_image == image_ref:
                    match = True
                if match:
                    out.append(
                        {
                            "name": c.name,
                            "id": c.id[:12],
                            "status": c.status,
                        }
                    )
        except Exception as e:
            logger.debug("枚举镜像引用容器失败: %s", e)
        return out

    def remove_image(self, image_ref: str, force: bool = False, noprune: bool = False) -> Dict[str, Any]:
        """删除镜像（按 ID 或 name:tag）。

        被容器引用时给出友好提示；不再向上抛出原始 Docker 堆栈。
        """
        using = self._containers_using_image(image_ref)

        try:
            self.client.images.remove(image=image_ref, force=force, noprune=noprune)
        except APIError as e:
            msg = str(e)
            low = msg.lower()
            # Docker 在镜像被容器占用时会返回 409 Conflict
            if "conflict" in low or "image is being used" in low:
                if using:
                    detail = "、".join(f"{c['name']}（{c['status']}）" for c in using)
                    raise RuntimeError(f"镜像正在被以下容器引用，无法删除：{detail}。请先删除对应容器。") from e
                raise RuntimeError("镜像正在被容器引用，无法删除。请先删除对应容器。") from e
            raise RuntimeError(f"删除镜像失败：{msg}") from e
        except Exception as e:
            raise RuntimeError(f"删除镜像失败：{e}") from e

        logger.info("已删除镜像 %s (force=%s, noprune=%s)", image_ref, force, noprune)
        return {"ok": True, "image": image_ref, "force": force}

    def prune_images(self) -> Dict[str, Any]:
        """清理悬空镜像（<none>:<none>），返回删除明细与释放空间。"""
        result = self.client.images.prune(filters={"dangling": True}) or {}
        deleted = result.get("ImagesDeleted") or []
        untagged = [d.get("Untagged") for d in deleted if d.get("Untagged")]
        removed = [d.get("Deleted") for d in deleted if d.get("Deleted")]
        reclaimed = int(result.get("SpaceReclaimed") or 0)
        logger.info(
            "镜像清理完成：删除 %d 个，取消标签 %d 个，释放 %.2f MB",
            len(removed),
            len(untagged),
            reclaimed / 1024 / 1024,
        )
        return {
            "ok": True,
            "deleted": removed,
            "untagged": untagged,
            "deleted_count": len(removed),
            "space_reclaimed": reclaimed,
        }


docker_service = DockerService()
