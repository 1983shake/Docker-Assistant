"""容器更新：API（页面已融合进主页）。"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app import APP_NAME, APP_TAGLINE, __version__
from app.config import config, save_config
from app.services import updater_service
from app.services.docker_service import docker_service
from app.services.log_handler import log_handler

router = APIRouter()


@router.get("/updater")
async def updater_page():
    return RedirectResponse(url="/?view=containers", status_code=307)


@router.get("/api/updater/meta")
async def api_meta():
    return {
        "name": APP_NAME,
        "tagline": APP_TAGLINE,
        "version": __version__,
        "start_year": 2026,
        "current_year": datetime.now().year,
    }


@router.get("/api/updater/health")
async def api_health():
    ok = docker_service.ping()
    return {"docker": ok, "time": datetime.now().isoformat(timespec="seconds")}


@router.get("/api/system/health")
async def api_system_health():
    return {"status": "ok"}


# ============================================================
#  日志
# ============================================================
@router.get("/api/updater/logs")
async def api_logs(after: int = 0, limit: int = 500, min_level: str = "INFO"):
    limit = max(1, min(int(limit), 1000))
    return log_handler.snapshot(after=max(0, int(after)), limit=limit, min_level=min_level)


@router.delete("/api/updater/logs")
async def api_clear_logs():
    log_handler.clear()
    return {"ok": True}


# ============================================================
#  容器
# ============================================================
@router.get("/api/updater/containers")
async def api_containers():
    import asyncio

    loop = asyncio.get_running_loop()
    containers = await loop.run_in_executor(None, docker_service.list_containers)

    policies = config.updater.containers or {}
    for c in containers:
        p = policies.get(c["name"]) or {}
        c["strategy"] = p.get("strategy") or "track"
        c["target_tag"] = p.get("target_tag") or ""
        c["updating"] = updater_service.running_updates.get(c["name"])
        c["checking"] = bool(updater_service.container_checking.get(c["name"]))
        c["check"] = updater_service.check_results.get(c["name"])
    return containers


@router.post("/api/updater/containers/{name}/check")
async def api_check_one(name: str):
    if updater_service.container_checking.get(name):
        raise HTTPException(status_code=409, detail=f"{name} 正在检测中")

    import asyncio

    loop = asyncio.get_running_loop()
    containers = await loop.run_in_executor(None, docker_service.list_containers)
    target = next((c for c in containers if c["name"] == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"未找到容器 {name}")
    if target.get("is_self"):
        raise HTTPException(status_code=400, detail="自身容器，无需检测")

    updater_service.container_checking[name] = True
    try:
        result = await updater_service.check_one(target, config, slot=None, manual=True)
        updater_service.check_results[name] = result
        updater_service._save_state()
    finally:
        updater_service.container_checking.pop(name, None)
    return {"ok": True, "result": result}


@router.post("/api/updater/check")
async def api_check_all():
    results = await updater_service.run_check_all()
    return {"last_check": updater_service.last_check_time, "results": results}


@router.get("/api/updater/results")
async def api_results():
    return {
        "last_check": updater_service.last_check_time,
        "results": list(updater_service.check_results.values()),
        "checking": updater_service.check_in_progress,
    }


@router.post("/api/updater/update/{name}")
async def api_update(name: str):
    try:
        result = await updater_service.perform_update(name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    import asyncio

    asyncio.create_task(updater_service._refresh_one(name))
    return {"ok": True, "result": result}


class ContainerPolicyPayload(BaseModel):
    strategy: str = Field("track", pattern="^(track|latest|pin)$")
    target_tag: str = ""


@router.put("/api/updater/containers/{name}/policy")
async def api_put_policy(name: str, payload: ContainerPolicyPayload):
    containers = config.updater.containers or {}
    containers[name] = {
        "strategy": payload.strategy,
        "target_tag": payload.target_tag.strip(),
    }
    config.updater.containers = containers
    save_config()
    return {"ok": True, "policy": containers[name]}


# ============================================================
#  镜像管理
# ============================================================
@router.get("/api/updater/images")
async def api_images():
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, docker_service.list_images)


@router.delete("/api/updater/images")
async def api_remove_image(
    image: str = Query(..., description="镜像 ID 或 name:tag"),
    force: bool = Query(False),
    noprune: bool = Query(False),
):
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: docker_service.remove_image(image, force=force, noprune=noprune))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/updater/images/prune")
async def api_prune_images():
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, docker_service.prune_images)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
#  设置
# ============================================================
class UpdaterSettingsPayload(BaseModel):
    enabled: bool = True
    check_interval_minutes: int = Field(60, ge=5, le=10080)
    check_concurrency: int = Field(2, ge=1, le=20)
    auto_update: bool = False
    # 【新增】等待镜像加速就绪
    wait_for_proxy_ready: bool = True
    proxy_ready_timeout_minutes: int = Field(15, ge=1, le=180)
    mirrors: list[str] = []
    use_direct: bool = True
    pull_use_mirror: bool = True
    registry_username: str = ""
    registry_password: str = ""
    log_max_entries: int = Field(5000, ge=100, le=100000)
    log_retention_days: int = Field(7, ge=1, le=365)


@router.get("/api/updater/settings")
async def api_get_settings():
    u = config.updater
    return {
        "enabled": u.enabled,
        "check_interval_minutes": u.check_interval_minutes,
        "check_concurrency": u.check_concurrency,
        "auto_update": u.auto_update,
        "wait_for_proxy_ready": u.wait_for_proxy_ready,
        "proxy_ready_timeout_minutes": u.proxy_ready_timeout_minutes,
        "mirrors": u.mirrors,
        "use_direct": u.use_direct,
        "pull_use_mirror": u.pull_use_mirror,
        "registry_username": u.registry_username,
        "registry_password": u.registry_password,
        "log_max_entries": u.log_max_entries,
        "log_retention_days": u.log_retention_days,
        "log_display_level": u.log_display_level,
    }


@router.put("/api/updater/settings")
async def api_put_settings(payload: UpdaterSettingsPayload):
    u = config.updater
    u.enabled = payload.enabled
    u.check_interval_minutes = payload.check_interval_minutes
    u.check_concurrency = payload.check_concurrency
    u.auto_update = payload.auto_update
    u.wait_for_proxy_ready = payload.wait_for_proxy_ready
    u.proxy_ready_timeout_minutes = payload.proxy_ready_timeout_minutes
    u.mirrors = [m.strip() for m in payload.mirrors if m.strip()]
    u.use_direct = payload.use_direct
    u.pull_use_mirror = payload.pull_use_mirror
    u.registry_username = payload.registry_username.strip()
    u.registry_password = payload.registry_password
    u.log_max_entries = payload.log_max_entries
    u.log_retention_days = payload.log_retention_days
    save_config()

    try:
        log_handler.cleanup_file(
            retention_days=payload.log_retention_days,
            max_entries=payload.log_max_entries,
        )
    except Exception:
        pass

    return {"ok": True}
