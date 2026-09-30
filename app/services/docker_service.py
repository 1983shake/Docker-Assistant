"""Docker 交互层：列容器、查本地 digest、拉取、重建容器、镜像管理。"""

from __future__ import annotations

import logging
import socket
from typing import Any, Callable, Dict, List, Optional, Tuple

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from .registry_client import is_dockerhub, normalize_mirror

logger = logging.getLogger(__name__)


class PullCancelled(Exception):
    """拉取被用户主动取消。"""

    pass


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
    def pull_image(
        self,
        image_ref: str,
        mirrors: List[str],
        use_direct: bool = True,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> Tuple[str, str]:
        """拉取镜像，返回 (最终使用的镜像引用, 源标签)。

        若提供 should_cancel，则使用流式 API，并在每个进度事件前检查取消；
        一旦取消，会立即停止读取事件流并抛出 PullCancelled。
        Docker 守护进程会自行丢弃未完成的 layer，不影响后续重试。
        """
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
            # 每个候选开始前检查一次
            if should_cancel and should_cancel():
                raise PullCancelled()

            try:
                logger.info("从 %s 拉取 %s", label, src)
                if should_cancel:
                    # 流式拉取以便中途取消
                    for event in self.client.api.pull(src, stream=True, decode=True):
                        if should_cancel():
                            logger.info("拉取 %s 被取消", src)
                            raise PullCancelled()
                        if isinstance(event, dict):
                            err = event.get("error") or (event.get("errorDetail") or {}).get("message")
                            if err:
                                raise RuntimeError(err)
                else:
                    self.client.images.pull(src)

                if src != image_ref:
                    img = self.client.images.get(src)
                    img.tag(image_ref)
                return image_ref, label
            except PullCancelled:
                raise
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

        # ------------------------------------------------------------------
        # 端口映射
        #
        # 关键点：只有 network_mode 与端口映射「兼容」时才拼装 ports。
        #
        # docker-py 的 HostConfig.__init__ 内置了如下硬性检查：
        #   network_mode == 'host' 或 network_mode.startswith('container:')
        #   → 不允许携带 port_bindings，否则抛 InvalidArgument。
        #
        # 原因：
        #   - host 模式：端口直接使用宿主机网络栈，不存在 NAT，端口映射无意义
        #   - container:<id> 模式：共享目标容器的网络命名空间，端口同样不可指定
        #
        # 原实现无条件拼装 ports，导致 network_mode=host 的容器
        # （如 hermes-webui）重建时报
        #   "host" network_mode is incompatible with port_bindings
        # ------------------------------------------------------------------
        _net_mode_str = (net_mode or "").strip().lower()
        _ports_compatible = not (_net_mode_str == "host" or _net_mode_str.startswith("container:"))

        if _ports_compatible:
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
        else:
            if hc.get("PortBindings"):
                logger.info(
                    "network_mode=%s 与端口映射不兼容，已跳过 PortBindings（共 %d 条）",
                    net_mode,
                    len(hc.get("PortBindings") or {}),
                )

        # ------------------------------------------------------------------
        # 挂载卷
        #
        # 关键点：以「容器内目标路径」(Destination) 去重，而不是以「源」去重。
        #
        # 原实现分别遍历 Mounts 和 Binds，volumes 的 key 是「源」，两个循环
        # 独立进行，同一个 Destination 若在 Mounts 和 Binds 里各自出现一次
        # （源写法不同，例如一个来自 Docker 内部卷路径、一个来自 compose 原始
        # bind），就会拼出两个 entry，最终 Binds 数组里出现两条指向同一目标
        # 的记录，Docker daemon 报 "Duplicate mount point"。
        #
        # 修复策略：
        #   1) 优先使用 Mounts —— 这是 Docker 已解析的最终挂载状态，完整且可靠。
        #      - Type=volume → 用 Name（卷名），否则 docker-py 会当成 bind mount
        #      - Type=bind   → 用 Source（宿主机绝对路径）
        #      - Type=tmpfs  → 跳过（无法通过 volumes 参数传递）
        #   2) Binds 只用于补充 Mounts 中未覆盖的目标路径，绝不覆盖已处理的 dest。
        # ------------------------------------------------------------------
        volumes: Dict[str, Any] = {}
        covered_dests: set = set()

        for m in attrs.get("Mounts") or []:
            mtype = (m.get("Type") or "bind").lower()
            dest = m.get("Destination")
            if not dest:
                continue
            if mtype == "tmpfs":
                # tmpfs 需要走 HostConfig.Tmpfs 参数，这里跳过
                continue
            if mtype == "volume":
                # 命名卷：用 Name（卷名），docker-py 会识别为卷挂载
                src = m.get("Name") or m.get("Source")
            else:
                # bind / 其他：用宿主机路径
                src = m.get("Source") or m.get("Name")
            if not src:
                continue
            if dest in covered_dests:
                # 同一个容器内目标路径重复 → 只保留第一个（Docker 也不允许重复）
                logger.warning(
                    "容器挂载目标 %s 重复出现（Mounts 内），已跳过源=%s",
                    dest,
                    src,
                )
                continue
            covered_dests.add(dest)
            mode = "rw" if m.get("RW", True) else "ro"
            volumes[src] = {"bind": dest, "mode": mode}

        # Binds 只补充 Mounts 未覆盖的目标路径
        for b in hc.get("Binds") or []:
            parts = b.split(":")
            if len(parts) < 2:
                continue
            src = parts[0]
            dest = parts[1]
            mode = parts[2] if len(parts) >= 3 else "rw"
            if not src or not dest:
                continue
            if dest in covered_dests:
                # 该目标已被 Mounts 覆盖（或已被前面的 Binds 处理），跳过
                continue
            covered_dests.add(dest)
            volumes[src] = {"bind": dest, "mode": mode}

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
        try:
            container.remove(force=True)
        except Exception as e:
            # 删除旧容器失败：清理临时容器，避免残留
            logger.error("删除旧容器失败，回滚临时容器: %s", e)
            try:
                new_container.remove(force=True)
            except Exception as e2:
                logger.error("回滚临时容器也失败: %s", e2)
            raise

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
    def _image_reference_counts(self) -> Dict[str, int]:
        """
        统计容器对镜像的引用，返回 {镜像ID: 引用数}。

        只使用容器 attrs["Image"]（容器实际使用的镜像 ID）。
        不再统计 Config.Image（tag）——因为：
          - 容器创建后，Config.Image 不会随 tag 转移而变化
          - 如果 tag 已经转到新镜像（例如 docker pull 更新了 tag），
            旧容器的 Config.Image 依然指向这个 tag，会造成 tag 匹配误判
          - 结果就是：新镜像被误判为"使用中"，实际上容器仍在用旧镜像 ID

        只按 ID 匹配才能如实反映容器真正使用哪个镜像。
        """
        counts: Dict[str, int] = {}
        try:
            for c in self.client.containers.list(all=True):
                attrs = c.attrs or {}
                img_id = attrs.get("Image") or ""
                if img_id:
                    counts[img_id] = counts.get(img_id, 0) + 1
        except Exception as e:
            logger.warning("统计镜像引用容器数量失败: %s", e)
        return counts

    @staticmethod
    def _resolve_container_count(img, counts: Dict[str, int]) -> int:
        """
        根据引用计数表，计算某个镜像被多少容器引用。

        匹配优先级（任一级命中即返回）：
          1. img.id 完整匹配 counts 的 key
          2. img.id 短 ID（前 12 位）匹配 counts 的 key 短 ID
          3. 兜底：Docker 自带的 attrs["Containers"]（>= 0）

        注意：不进行 tag 匹配。tag 匹配会把"tag 已转移到新镜像但容器仍用旧 ID"
        的场景误判为"新镜像使用中"，掩盖容器未真正使用新镜像的事实。
        """
        # 1. 按完整镜像 ID 匹配
        if img.id and img.id in counts:
            return counts[img.id]

        # 2. 按短 ID 匹配（只对看起来像 ID 的 key 生效，避免误匹配 tag）
        if img.id:
            target_short = img.id.replace("sha256:", "")[:12]
            for k, v in counts.items():
                if k.startswith("sha256:"):
                    k_short = k[7:19]
                elif len(k) >= 12 and all(ch in "0123456789abcdef" for ch in k[:12].lower()):
                    k_short = k[:12]
                else:
                    continue
                if k_short == target_short:
                    return v

        # 3. 兜底：Docker 自带的 attrs["Containers"]
        #    -1 表示"未计算"，视作 0
        try:
            n = int((img.attrs or {}).get("Containers") or 0)
            return max(0, n)
        except (TypeError, ValueError):
            return 0

    def list_images(self) -> List[Dict[str, Any]]:
        """列出本地镜像，每个镜像一条记录（包含全部标签与真实容器引用数）。"""
        counts = self._image_reference_counts()

        out: List[Dict[str, Any]] = []
        for img in self.client.images.list():
            attrs = img.attrs or {}
            tags = list(img.tags or [])
            try:
                size = int(attrs.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0

            containers = self._resolve_container_count(img, counts)

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
        """列出引用了指定镜像的容器（按镜像 ID / 短 ID 匹配）。

        与 _image_reference_counts 保持一致：只用 ID 匹配。
        """
        out: List[Dict[str, str]] = []
        if not image_ref:
            return out
        try:
            ref_short = image_ref.replace("sha256:", "")[:12]
            for c in self.client.containers.list(all=True):
                attrs = c.attrs or {}
                img_id = attrs.get("Image") or ""
                if not img_id:
                    continue
                match = False
                if img_id == image_ref:
                    match = True
                elif image_ref.startswith("sha256:") and img_id.startswith(image_ref):
                    match = True
                elif img_id.startswith(image_ref):
                    match = True
                elif img_id.replace("sha256:", "")[:12] == ref_short:
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

    def prune_unused_images(self) -> Dict[str, Any]:
        """清理未使用镜像（有 tag 但无任何容器引用）。

        与 prune_images 的区别：
          - prune_images 删除的是悬空镜像（<none>:<none>，无 tag）
          - prune_unused_images 删除的是有 tag、但没有任何容器（含已停止）引用的镜像

        实现方式：遍历镜像列表，逐个 tag 调用 remove。
        多 tag 的镜像会依次取消每个 tag；全部成功则视为该镜像已被删除，计入释放空间。
        删除过程遇到失败（权限、竞态等）不会中断，会记录到 failed 列表。
        """
        counts = self._image_reference_counts()

        deleted_tags: List[str] = []
        failed: List[Dict[str, str]] = []
        freed = 0
        image_count = 0

        try:
            images = list(self.client.images.list())
        except Exception as e:
            raise RuntimeError(f"获取镜像列表失败：{e}") from e

        for img in images:
            tags = [t for t in (img.tags or []) if t]
            # 悬空镜像交给 prune_images 处理，这里跳过
            if not tags:
                continue
            # 有容器（含已停止）引用，跳过（使用多级匹配，避免误删使用中的镜像）
            if self._resolve_container_count(img, counts) > 0:
                continue

            try:
                size = int((img.attrs or {}).get("Size") or 0)
            except Exception:
                size = 0

            img_failed = False
            for tag in tags:
                try:
                    self.client.images.remove(image=tag, force=False, noprune=False)
                    deleted_tags.append(tag)
                except Exception as e:
                    failed.append({"image": tag, "error": str(e)})
                    img_failed = True

            if not img_failed:
                image_count += 1
                freed += size

        logger.info(
            "未使用镜像清理完成：删除 %d 个镜像（%d 个 tag），失败 %d 个，约释放 %.2f MB",
            image_count,
            len(deleted_tags),
            len(failed),
            freed / 1024 / 1024,
        )
        return {
            "ok": True,
            "deleted": deleted_tags,
            "failed": failed,
            "deleted_count": image_count,
            "deleted_tag_count": len(deleted_tags),
            "failed_count": len(failed),
            "space_reclaimed": freed,
        }


docker_service = DockerService()
