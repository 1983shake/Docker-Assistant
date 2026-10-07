"""Web 路由：管理页面 + 节点 API + 配置 API + 容器更新 API。"""

import asyncio
import secrets
import shutil
from datetime import datetime

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import APP_INFO, get_app_config_dict, get_changelog
from app.config import CONFIG_PATH, AppConfig, config, reload_config, save_config
from app.core import log_handler
from app.db import HealthCheckLog, ProxyNode, engine
from app.docker_service import docker_service
from app import proxy_manager, updater_service
from app import traffic as traffic_logger

security = HTTPBasic(auto_error=False)


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if config.admin.user and config.admin.pass_:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="Restricted Area"'},
            )
        ok_user = secrets.compare_digest(credentials.username, config.admin.user)
        ok_pass = secrets.compare_digest(credentials.password, config.admin.pass_)
        if not (ok_user and ok_pass):
            raise HTTPException(
                status_code=401,
                detail="用户名或密码错误",
                headers={"WWW-Authenticate": 'Basic realm="Restricted Area"'},
            )
    return True


router = APIRouter(dependencies=[Depends(verify_auth)])
templates = Jinja2Templates(directory="app/templates")


# ============================================================
#  页面
# ============================================================


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    proxies = proxy_manager.get_all_proxies()
    stats = traffic_logger.get_traffic_stats()
    pull_stats = traffic_logger.get_pull_stats()
    pull_history = traffic_logger.get_pull_history(limit=200)
    total_download = sum(s.download_bytes for s in stats)

    popup_duration = getattr(config.updater, "progress_popup_duration", 3) or 3

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": APP_INFO["name"],
            "app_tagline": APP_INFO["tagline"],
            "app_version": APP_INFO["version"],
            "start_year": APP_INFO["start_year"],
            "repo": APP_INFO["repo"],
            "current_year": datetime.now().year,
            "proxies": [p.model_dump(mode="json") for p in proxies],
            "stats": [s.model_dump(mode="json") for s in stats],
            "total_download": total_download,
            "pull_stats": pull_stats,
            "pull_history": [p.model_dump(mode="json") for p in pull_history],
            "popup_duration": popup_duration,
            "updater_enabled": config.updater.enabled,
            "updater_settings": {
                "enabled": config.updater.enabled,
                "check_interval_minutes": config.updater.check_interval_minutes,
                "check_concurrency": config.updater.check_concurrency,
                "auto_update": config.updater.auto_update,
                "mirrors": config.updater.mirrors,
                "use_direct": config.updater.use_direct,
                "pull_use_mirror": config.updater.pull_use_mirror,
                "registry_username": config.updater.registry_username,
                "registry_password": config.updater.registry_password,
                "log_max_entries": config.updater.log_max_entries,
                "log_retention_days": config.updater.log_retention_days,
                "log_display_level": config.updater.log_display_level,
            },
        },
    )


@router.get("/updater")
async def updater_page():
    return RedirectResponse(url="/?view=containers", status_code=307)


# ============================================================
#  节点 API
# ============================================================


@router.get("/api/proxies")
async def list_proxies():
    return [p.model_dump(mode="json") for p in proxy_manager.get_all_proxies()]


@router.post("/api/proxies")
async def add_proxy_node(
    name: str = Form(...),
    url: str = Form(...),
    registry_type: str = Form("dockerhub"),
    route_prefix: str = Form(None),
    username: str = Form(None),
    password: str = Form(None),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL 无效")
    node = proxy_manager.add_proxy(name, url, registry_type, route_prefix, username, password)
    await proxy_manager._check_one_alive(node.id)
    with Session(engine) as session:
        node = session.get(ProxyNode, node.id)
    if node and node.enabled:
        await proxy_manager._test_one_speed(node)
    return {"status": "ok", "node": node.model_dump(mode="json") if node else None}


@router.put("/api/proxies/{proxy_id}")
async def update_proxy_node(
    proxy_id: int,
    name: str = Form(...),
    url: str = Form(...),
    registry_type: str = Form("dockerhub"),
    route_prefix: str = Form(None),
    username: str = Form(None),
    password: str = Form(None),
):
    if not url.startswith("http"):
        raise HTTPException(400, "URL 无效")
    node = proxy_manager.update_proxy(proxy_id, name, url, registry_type, route_prefix, username, password)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok"}


@router.delete("/api/proxies/{proxy_id}")
async def delete_proxy_node(proxy_id: int):
    if not proxy_manager.delete_proxy(proxy_id):
        raise HTTPException(404, "节点不存在")
    return {"status": "ok"}


@router.post("/api/proxies/{proxy_id}/disable")
async def disable_proxy(proxy_id: int, reason: str = Form("")):
    node = proxy_manager.set_manual_disable(proxy_id, True, reason)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok", "node": node.model_dump(mode="json")}


@router.post("/api/proxies/{proxy_id}/enable")
async def enable_proxy(proxy_id: int):
    node = proxy_manager.set_manual_disable(proxy_id, False)
    if not node:
        raise HTTPException(404, "节点不存在")
    return {"status": "ok", "node": node.model_dump(mode="json")}


@router.post("/api/proxies/{proxy_id}/test")
async def test_single_proxy(proxy_id: int):
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
        if not node:
            raise HTTPException(404, "节点不存在")
    await proxy_manager._check_one_alive(proxy_id)
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
    if node and node.enabled:
        await proxy_manager._test_one_speed(node)
    with Session(engine) as session:
        node = session.get(ProxyNode, proxy_id)
    return node.model_dump(mode="json") if node else {}


# ============================================================
#  批量操作
# ============================================================


@router.post("/api/proxies/fetch")
async def fetch_proxies():
    count = await proxy_manager.fetch_and_update_proxies()
    await proxy_manager.run_health_check()
    await proxy_manager.run_speed_test()
    return {"status": "ok", "added": count}


@router.post("/api/test-health")
async def trigger_health_check(request: Request):
    ids = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            ids = data.get("ids") or None
    except Exception:
        pass
    await proxy_manager.run_health_check(ids=ids)
    return {"status": "ok"}


@router.post("/api/test-speed")
async def trigger_speed_test(request: Request):
    ids = None
    try:
        data = await request.json()
        if isinstance(data, dict):
            ids = data.get("ids") or None
    except Exception:
        pass
    await proxy_manager.run_health_check(ids=ids)
    await proxy_manager.run_speed_test(ids=ids)
    return {"status": "ok"}


@router.post("/api/proxies/batch-disable")
async def batch_disable(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    reason = data.get("reason", "")
    for pid in ids:
        proxy_manager.set_manual_disable(pid, True, reason)
    return {"status": "ok", "count": len(ids)}


@router.post("/api/proxies/batch-enable")
async def batch_enable(request: Request):
    data = await request.json()
    ids = data.get("ids", [])
    for pid in ids:
        proxy_manager.set_manual_disable(pid, False)
    return {"status": "ok", "count": len(ids)}


# ============================================================
#  导入导出
# ============================================================


@router.get("/api/proxies/export")
async def export_proxies():
    proxies = proxy_manager.get_all_proxies()
    content = [p.model_dump(mode="json") for p in proxies]
    return JSONResponse(
        content=content,
        headers={"Content-Disposition": "attachment; filename=proxies.json"},
    )


@router.post("/api/proxies/import")
async def import_proxies(request: Request):
    try:
        data = await request.json()
        if not isinstance(data, list):
            raise ValueError("需要节点列表")
        imported = 0
        for item in data:
            if not item.get("url"):
                continue
            proxy_manager.add_proxy(
                name=item.get("name", "imported"),
                url=item["url"],
                registry_type=item.get("registry_type", "dockerhub"),
                route_prefix=item.get("route_prefix"),
                username=item.get("username"),
                password=item.get("password"),
            )
            imported += 1
        return {"status": "ok", "imported": imported}
    except Exception as e:
        raise HTTPException(400, f"导入失败: {e}")


# ============================================================
#  拉取记录
# ============================================================


@router.get("/api/pulls")
async def get_pulls(limit: int = 500):
    pulls = traffic_logger.get_pull_history(limit=limit)
    return [p.model_dump(mode="json") for p in pulls]


@router.delete("/api/pulls")
async def clear_pulls():
    traffic_logger.clear_pull_history()
    return {"status": "ok"}


# ============================================================
#  在线检测日志
# ============================================================


@router.get("/api/health-logs/{node_id}")
async def get_health_logs(node_id: int, limit: int = 50):
    with Session(engine) as session:
        logs = session.exec(
            select(HealthCheckLog).where(HealthCheckLog.node_id == node_id).order_by(HealthCheckLog.check_time.desc()).limit(limit)
        ).all()
    return [log.model_dump(mode="json") for log in logs]


# ============================================================
#  任务进度
# ============================================================


@router.get("/api/tasks/status")
async def tasks_status():
    return proxy_manager.get_progress()


# ============================================================
#  网络速率（供顶栏实时展示）
# ============================================================


@router.get("/api/network-speed")
async def network_speed():
    return traffic_logger.get_network_speed()


# ============================================================
#  配置文件管理 API
# ============================================================


@router.get("/api/config")
async def get_config():
    if not CONFIG_PATH.exists():
        raise HTTPException(404, f"配置文件不存在: {CONFIG_PATH}")

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"读取配置失败: {e}")

    try:
        data = yaml.safe_load(text) or {}
    except Exception as e:
        raise HTTPException(500, f"解析配置失败: {e}")

    data["app"] = get_app_config_dict()
    data.pop("search", None)

    return {
        "yaml": text,
        "config": data,
        "path": str(CONFIG_PATH.resolve()),
        "restart_required_fields": [
            "server.host",
            "server.port",
            "server.debug",
            "logging.*",
            "auto_fetch.interval_minutes",
            "health_check.interval_minutes",
            "speed_test.interval_minutes",
            "updater.check_interval_minutes",
        ],
    }


@router.put("/api/config")
async def update_config(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"请求体不是合法 JSON: {e}")

    yaml_text = None
    parsed = None

    if isinstance(body.get("config"), dict):
        parsed = body["config"]
    elif isinstance(body.get("yaml"), str) and body["yaml"].strip():
        try:
            parsed = yaml.safe_load(body["yaml"])
        except yaml.YAMLError as e:
            raise HTTPException(400, f"YAML 语法错误: {e}")
    else:
        raise HTTPException(400, "请求体必须包含 'config' 或 'yaml' 字段")

    if not isinstance(parsed, dict):
        raise HTTPException(400, "配置根节点必须是字典（mapping）")

    parsed["app"] = get_app_config_dict()
    parsed.pop("search", None)

    try:
        AppConfig(**parsed)
    except Exception as e:
        raise HTTPException(400, f"配置校验失败: {e}")

    try:
        yaml_text = yaml.dump(parsed, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as e:
        raise HTTPException(400, f"序列化配置失败: {e}")

    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    backup_ok = False
    if CONFIG_PATH.exists():
        try:
            shutil.copy2(CONFIG_PATH, backup_path)
            backup_ok = True
        except Exception:
            pass

    try:
        CONFIG_PATH.write_text(yaml_text, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"写入配置失败: {e}")

    try:
        reload_config()
    except Exception as e:
        if backup_ok:
            try:
                shutil.copy2(backup_path, CONFIG_PATH)
                reload_config()
            except Exception:
                pass
        raise HTTPException(500, f"配置重载失败（已回滚）: {e}")

    try:
        proxy_manager.init_proxies()
    except Exception:
        pass

    return {
        "status": "ok",
        "message": "配置已保存并重载",
        "backup": str(backup_path) if backup_ok else None,
        "restart_required": True,
    }


@router.post("/api/config/reload")
async def reload_config_endpoint():
    try:
        reload_config()
        proxy_manager.init_proxies()
    except Exception as e:
        raise HTTPException(500, f"重载失败: {e}")
    return {"status": "ok", "message": "配置已重载"}


@router.get("/api/config/backup")
async def download_backup():
    backup_path = CONFIG_PATH.with_suffix(".yaml.bak")
    if not backup_path.exists():
        raise HTTPException(404, "没有备份文件")
    try:
        text = backup_path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"读取备份失败: {e}")
    return JSONResponse(content={"yaml": text, "path": str(backup_path)})


# ============================================================
#  容器更新 API
# ============================================================


@router.get("/api/updater/meta")
async def api_meta():
    return {**APP_INFO, "current_year": datetime.now().year}


@router.get("/api/updater/changelog")
async def api_changelog(limit: int = 0):
    return {"changelog": get_changelog(limit)}


@router.get("/api/updater/health")
async def api_health():
    ok = docker_service.ping()
    return {"docker": ok, "time": datetime.now().isoformat(timespec="seconds")}


@router.get("/api/system/health")
async def api_system_health():
    return {"status": "ok"}


@router.get("/api/updater/logs")
async def api_logs(after: int = 0, limit: int = 500, min_level: str = "INFO"):
    limit = max(1, min(int(limit), 1000))
    return log_handler.snapshot(after=max(0, int(after)), limit=limit, min_level=min_level)


@router.delete("/api/updater/logs")
async def api_clear_logs():
    log_handler.clear()
    return {"ok": True}


@router.get("/api/updater/containers")
async def api_containers():
    loop = asyncio.get_running_loop()
    containers = await loop.run_in_executor(None, docker_service.list_containers)

    policies = config.updater.containers or {}
    for c in containers:
        p = policies.get(c["name"]) or {}
        c["strategy"] = p.get("strategy") or "track"
        c["target_tag"] = p.get("target_tag") or ""
        c["updating"] = updater_service.running_updates.get(c["name"])
        c["cancelling"] = bool(updater_service.cancel_requested.get(c["name"]))
        c["checking"] = bool(updater_service.container_checking.get(c["name"]))
        c["check"] = updater_service.check_results.get(c["name"])
    return containers


@router.post("/api/updater/containers/{name}/check")
async def api_check_one(name: str):
    if updater_service.container_checking.get(name):
        raise HTTPException(status_code=409, detail=f"{name} 正在检测中")

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

    return {"ok": True, "result": result}


@router.post("/api/updater/update/{name}/cancel")
async def api_cancel_update(name: str):
    accepted = updater_service.request_cancel(name)
    if not accepted:
        raise HTTPException(status_code=404, detail=f"{name} 当前没有正在进行的更新")
    return {"ok": True, "name": name, "message": "取消请求已受理"}


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
    loop = asyncio.get_running_loop()
    prefixes = updater_service.get_local_mirror_prefixes()
    return await loop.run_in_executor(
        None,
        lambda: docker_service.list_images(mirror_prefixes=prefixes),
    )


@router.delete("/api/updater/images")
async def api_remove_image(
    image: str = Query(..., description="镜像 ID 或 name:tag"),
    force: bool = Query(False),
    noprune: bool = Query(False),
):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: docker_service.remove_image(image, force=force, noprune=noprune))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/updater/images/prune")
async def api_prune_images():
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, docker_service.prune_images)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/updater/images/prune-unused")
async def api_prune_unused_images():
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, docker_service.prune_unused_images)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/updater/images/cleanup-mirror-tags")
async def api_cleanup_mirror_tags():
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, updater_service.cleanup_mirror_tags)
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
