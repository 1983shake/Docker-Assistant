"""Docker 交互层：列容器、查本地 digest、拉取、重建容器、镜像管理。"""

from __future__ import annotations

import logging
import socket
from typing import Any, Callable, Dict, List, Optional, Tuple

import docker
from docker.errors import APIError, ImageNotFound, NotFound

from app.registry import is_dockerhub, normalize_mirror

logger = logging.getLogger("dockerassistant.docker")


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
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[str, str]:
        """拉取镜像，返回 (最终使用的镜像引用, 源标签)。

        【v1.1.1 修复】
          1. 按 layer 聚合，向 on_progress 上报累计字节（用于进度条）。
          2. 走加速源拉取时，Docker 会自动为镜像打上
             "<mirror>/<image_ref>" 的前缀 tag。拉取成功后，
             本函数**立即取消该前缀 tag**，只保留 image_ref，
             避免镜像列表里出现 "127.0.0.1:8000/xxx:latest" 这类内部地址。
        """
        candidates: List[Tuple[str, str]] = []
        if is_dockerhub(image_ref):
            for m in mirrors or []:
                m = m.strip()
                if not m:
                    continue
                nm = normalize_mirror(m)
                candidates.append((f"{nm}/{image_ref}", nm))
        if use_direct or not candidates:
            candidates.append((image_ref, "direct"))

        if candidates:
            logger.info(
                "拉取源候选（顺序尝试）：%s",
                " | ".join(f"{lbl} -> {src}" for src, lbl in candidates),
            )

        errors: List[str] = []
        for src, label in candidates:
            if should_cancel and should_cancel():
                raise PullCancelled()

            # 按 layer 聚合：{layer_id: {"current": int, "total": int, "done": bool}}
            layers: Dict[str, Dict[str, Any]] = {}
            last_total_reported = 0

            try:
                logger.info("从 %s 拉取 %s", label, src)

                for event in self.client.api.pull(src, stream=True, decode=True):
                    if should_cancel and should_cancel():
                        logger.info("拉取 %s 被取消", src)
                        raise PullCancelled()

                    if not isinstance(event, dict):
                        continue

                    err = event.get("error") or (event.get("errorDetail") or {}).get("message")
                    if err:
                        raise RuntimeError(err)

                    status = (event.get("status") or "").strip()
                    lid = event.get("id")
                    detail = event.get("progressDetail") or {}
                    cur = detail.get("current")
                    tot = detail.get("total")

                    if lid and isinstance(cur, int) and isinstance(tot, int) and tot > 0:
                        entry = layers.setdefault(lid, {"current": 0, "total": tot, "done": False})
                        entry["total"] = tot
                        if cur > entry["current"]:
                            entry["current"] = cur
                    elif lid and status in ("Download complete", "Pull complete"):
                        entry = layers.setdefault(lid, {"current": 0, "total": 0, "done": False})
                        entry["done"] = True
                        if entry["total"] > 0:
                            entry["current"] = entry["total"]
                    elif lid and status == "Already exists":
                        entry = layers.setdefault(lid, {"current": 0, "total": 0, "done": True})

                    if on_progress is None:
                        continue

                    if layers:
                        total_b = 0
                        cur_b = 0
                        done_cnt = 0
                        for v in layers.values():
                            t = int(v.get("total") or 0)
                            c = int(v.get("current") or 0)
                            if t <= 0:
                                continue
                            total_b += t
                            if v.get("done"):
                                cur_b += t
                                done_cnt += 1
                            else:
                                cur_b += min(c, t)
                        if total_b > 0:
                            last_total_reported = total_b
                            try:
                                on_progress(
                                    {
                                        "phase": "downloading",
                                        "current": cur_b,
                                        "total": total_b,
                                        "layers_total": sum(1 for v in layers.values() if int(v.get("total") or 0) > 0),
                                        "layers_done": done_cnt,
                                        "status": status,
                                        "layer_id": lid,
                                    }
                                )
                            except Exception:
                                pass
                    elif status:
                        try:
                            on_progress({"phase": "status", "status": status})
                        except Exception:
                            pass

                if on_progress and last_total_reported > 0:
                    try:
                        on_progress(
                            {
                                "phase": "downloading",
                                "current": last_total_reported,
                                "total": last_total_reported,
                                "layers_total": len(layers),
                                "layers_done": len(layers),
                                "status": "Pull complete",
                                "layer_id": None,
                            }
                        )
                    except Exception:
                        pass

                # ========================================================
                #  【v1.1.1 关键修复】清理代理前缀 tag
                # ========================================================
                if src != image_ref:
                    try:
                        img = self.client.images.get(src)
                    except ImageNotFound:
                        img = None

                    if img is None:
                        logger.warning("拉取完成但未找到镜像 %s（src），跳过 tag 修正", src)
                    else:
                        try:
                            img.tag(image_ref)
                        except Exception as e:
                            logger.error("添加原始标签 %s 失败: %s", image_ref, e)
                            raise

                        try:
                            self.client.images.remove(image=src, force=False, noprune=False)
                            logger.info("已移除代理前缀标签 %s（保留原始标签 %s）", src, image_ref)
                        except Exception as e:
                            logger.warning("移除代理前缀标签 %s 失败: %s（镜像本体保留）", src, e)

                return image_ref, label

            except PullCancelled:
                raise
            except Exception as e:
                err_msg = str(e)
                logger.warning("从 %s 拉取 %s 失败: %s", label, src, err_msg)

                low = err_msg.lower()
                if (
                    "server gave http response to https client" in low
                    or ("https" in low and "http response" in low)
                    or ("tls" in low and "handshake" in low)
                ):
                    err_msg += (
                        "（提示：目标为 HTTP 镜像源，需要在 daemon.json 的 "
                        "insecure-registries 中加入该地址并重启 Docker。"
                        "若使用 127.0.0.1:<port>，Docker 默认已允许 HTTP，无需额外配置。）"
                    )
                errors.append(f"{label}: {err_msg}")

        raise RuntimeError("所有拉取源均失败 -> " + "; ".join(errors))

    # ================================================================== #
    #  代理前缀标签工具
    # ================================================================== #
    @staticmethod
    def _strip_mirror_prefix(tag: str, prefixes: List[str]) -> Optional[str]:
        """从 tag 剥离已知前缀，返回原始 tag；无匹配返回 None。"""
        if not tag or not prefixes:
            return None
        t = tag
        t_lower = t.lower()
        for p in prefixes:
            if not p:
                continue
            p_lower = p.lower().rstrip("/")
            if t_lower.startswith(p_lower + "/"):
                return t[len(p_lower) + 1 :]
        return None

    @staticmethod
    def _is_mirror_prefix_tag(tag: str, prefixes: List[str]) -> bool:
        """判断 tag 是否以某个代理地址前缀开头。"""
        return DockerService._strip_mirror_prefix(tag, prefixes) is not None

    def cleanup_mirror_prefix_tags(self, mirror_prefixes: List[str]) -> Dict[str, Any]:
        """扫描所有镜像，删除以指定代理前缀开头的 tag。"""
        prefixes = [p.strip().rstrip("/") for p in (mirror_prefixes or []) if p and p.strip()]
        if not prefixes:
            return {"ok": True, "removed": [], "failed": [], "skipped": []}

        removed: List[str] = []
        failed: List[Dict[str, str]] = []
        skipped: List[str] = []

        try:
            images = list(self.client.images.list())
        except Exception as e:
            return {"ok": False, "error": str(e), "removed": [], "failed": [], "skipped": []}

        for img in images:
            tags = [t for t in (img.tags or []) if t]
            if not tags:
                continue

            prefix_tags = [t for t in tags if self._is_mirror_prefix_tag(t, prefixes)]
            if not prefix_tags:
                continue

            non_prefix_tags = [t for t in tags if t not in prefix_tags]
            if not non_prefix_tags:
                skipped.extend(prefix_tags)
                continue

            for t in prefix_tags:
                try:
                    self.client.images.remove(image=t, force=False, noprune=False)
                    removed.append(t)
                except Exception as e:
                    failed.append({"tag": t, "error": str(e)})

        logger.info(
            "代理前缀标签清理完成：删除 %d 个，跳过 %d 个（仅剩前缀），失败 %d 个",
            len(removed),
            len(skipped),
            len(failed),
        )
        return {"ok": True, "removed": removed, "failed": failed, "skipped": skipped}

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

        net_mode = hc.get("NetworkMode")
        custom_nets = [n for n in (ns.get("Networks") or {}).keys() if n not in ("bridge", "host", "none")]
        if net_mode in ("host", "none"):
            kwargs["network_mode"] = net_mode
        elif custom_nets:
            kwargs["network"] = custom_nets[0]

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

        volumes: Dict[str, Any] = {}
        covered_dests: set = set()

        for m in attrs.get("Mounts") or []:
            mtype = (m.get("Type") or "bind").lower()
            dest = m.get("Destination")
            if not dest:
                continue
            if mtype == "tmpfs":
                continue
            if mtype == "volume":
                src = m.get("Name") or m.get("Source")
            else:
                src = m.get("Source") or m.get("Name")
            if not src:
                continue
            if dest in covered_dests:
                logger.warning("容器挂载目标 %s 重复出现（Mounts 内），已跳过源=%s", dest, src)
                continue
            covered_dests.add(dest)
            mode = "rw" if m.get("RW", True) else "ro"
            volumes[src] = {"bind": dest, "mode": mode}

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
                continue
            covered_dests.add(dest)
            volumes[src] = {"bind": dest, "mode": mode}

        if volumes:
            kwargs["volumes"] = volumes

        return kwargs

    # ------------------------------------------------------------------ #
    def recreate_container(self, container, new_image: str) -> Dict[str, Any]:
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

        custom_nets = [n for n in (ns.get("Networks") or {}).keys() if n not in ("bridge", "host", "none")]
        first = kwargs.get("network")
        for n in custom_nets:
            if n == first:
                continue
            try:
                self.client.networks.get(n).connect(new_container)
            except Exception as e:
                logger.warning("连接网络 %s 失败: %s", n, e)

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
            logger.error("删除旧容器失败，回滚临时容器: %s", e)
            try:
                new_container.remove(force=True)
            except Exception as e2:
                logger.error("回滚临时容器也失败: %s", e2)
            raise

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
        if img.id and img.id in counts:
            return counts[img.id]

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

        try:
            n = int((img.attrs or {}).get("Containers") or 0)
            return max(0, n)
        except (TypeError, ValueError):
            return 0

    def list_images(
        self,
        mirror_prefixes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """列出本地镜像，每个镜像一条记录。

        【v1.1.1 增强】mirror_prefixes 提供时，会把每个 tag 上的代理前缀剥离。
        剥离仅影响展示层，不修改 Docker 存储（存储层由 pull_image 清理）。
        """
        counts = self._image_reference_counts()
        prefixes = [p.strip().rstrip("/") for p in (mirror_prefixes or []) if p and p.strip()]

        out: List[Dict[str, Any]] = []
        for img in self.client.images.list():
            attrs = img.attrs or {}
            raw_tags = list(img.tags or [])

            display_tags: List[str] = []
            seen: set = set()
            for t in raw_tags:
                stripped = self._strip_mirror_prefix(t, prefixes) if prefixes else None
                effective = stripped or t
                if effective and effective not in seen:
                    seen.add(effective)
                    display_tags.append(effective)

            try:
                size = int(attrs.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0

            containers = self._resolve_container_count(img, counts)

            out.append(
                {
                    "id": img.id,
                    "short_id": (img.short_id or "").replace("sha256:", "")[:12],
                    "tags": display_tags,
                    "raw_tags": raw_tags,
                    "repo_digests": list(attrs.get("RepoDigests") or []),
                    "size": size,
                    "created": attrs.get("Created") or "",
                    "containers": containers,
                    "dangling": len(raw_tags) == 0,
                }
            )
        return out

    def _containers_using_image(self, image_ref: str) -> List[Dict[str, str]]:
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
                    out.append({"name": c.name, "id": c.id[:12], "status": c.status})
        except Exception as e:
            logger.debug("枚举镜像引用容器失败: %s", e)
        return out

    def remove_image(self, image_ref: str, force: bool = False, noprune: bool = False) -> Dict[str, Any]:
        using = self._containers_using_image(image_ref)

        try:
            self.client.images.remove(image=image_ref, force=force, noprune=noprune)
        except APIError as e:
            msg = str(e)
            low = msg.lower()
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
            if not tags:
                continue
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
