"""Docker-Assistant 统一入口：镜像加速 + 容器更新器。"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from sqlmodel import Session, select

from app import APP_NAME, APP_TAGLINE, __version__
from app.config import config
from app.database import create_db_and_tables, engine, upgrade_db
from app.models import ProxyNode
from app.routers import docker_proxy, updater, web_ui
from app.services import proxy_manager
from app.services.log_handler import log_handler
from app.services import updater_service

# ========== 日志配置 ==========
handlers = [logging.StreamHandler()]
try:
    os.makedirs(os.path.dirname(config.logging.file) or "data", exist_ok=True)
    handlers.append(
        RotatingFileHandler(
            config.logging.file,
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
    )
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, config.logging.level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)


class _AccessLogFilter(logging.Filter):
    _SUPPRESS_PATHS = ("/api/tasks/status", "/api/updater/containers")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self._SUPPRESS_PATHS)


_third_level = getattr(logging, str(config.logging.third_party_level).upper(), logging.WARNING)
for _noisy_logger in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
    logging.getLogger(_noisy_logger).setLevel(_third_level)
logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())

logger = logging.getLogger("dockerassistant")

scheduler = AsyncIOScheduler()
_bg_task: asyncio.Task | None = None


def _get_node_count() -> int:
    with Session(engine) as session:
        return len(session.exec(select(ProxyNode)).all())


async def _background_startup():
    """后台执行首次拉取、在线检测、速度测试，不阻塞 Web 页面启动。"""
    try:
        node_count = _get_node_count()
        if node_count == 0:
            if config.auto_fetch.enabled:
                logger.info("数据库无节点，执行首次节点拉取...")
                try:
                    added = await proxy_manager.fetch_and_update_proxies()
                    logger.info(f"首次节点拉取完成，新增 {added} 个节点")
                except Exception as e:
                    logger.error(f"首次节点拉取失败: {e}")
            else:
                logger.warning("数据库无节点，但 auto_fetch.enabled=false，跳过拉取")
        else:
            logger.info(f"数据库已有 {node_count} 个节点，跳过首次拉取")

        logger.info("执行初始在线检测...")
        try:
            await proxy_manager.run_health_check()
        except Exception as e:
            logger.error(f"初始在线检测失败: {e}")

        if config.speed_test.enabled:
            logger.info("执行初始速度测试...")
            try:
                await proxy_manager.run_speed_test()
            except Exception as e:
                logger.error(f"初始速度测试失败: {e}")
        else:
            logger.info("速度测试已禁用，跳过初始速度测试")

        # 触发一次容器检查（如果更新器启用）
        if config.updater.enabled:
            try:
                logger.info("执行初始容器检查...")
                await updater_service.run_check_all()
            except Exception as e:
                logger.error(f"初始容器检查失败: {e}")
    except asyncio.CancelledError:
        logger.info("后台初始化任务被取消")
        raise
    except Exception as e:
        logger.error(f"后台初始化任务异常: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bg_task

    logger.info("=" * 60)
    logger.info(f"  {APP_NAME}  {APP_TAGLINE}  v{__version__}")
    logger.info("=" * 60)

    # 1. 初始化数据库
    logger.info("初始化数据库...")
    create_db_and_tables()
    upgrade_db()

    # 2. 加载自定义节点
    logger.info("加载配置中的自定义节点...")
    proxy_manager.init_proxies()

    # 3. 启动定时任务调度器
    logger.info("启动定时任务调度器...")

    # 3.1 镜像代理任务
    if config.auto_fetch.enabled:
        scheduler.add_job(
            proxy_manager.fetch_and_update_proxies,
            "interval",
            minutes=config.auto_fetch.interval_minutes,
            id="auto_fetch",
            replace_existing=True,
        )
    scheduler.add_job(
        proxy_manager.run_health_check,
        "interval",
        minutes=config.health_check.interval_minutes,
        id="health_check",
        replace_existing=True,
    )
    if config.speed_test.enabled:
        scheduler.add_job(
            proxy_manager.run_speed_test,
            "interval",
            minutes=config.speed_test.interval_minutes,
            id="speed_test",
            replace_existing=True,
        )
    scheduler.add_job(
        proxy_manager.cleanup_caches,
        "interval",
        minutes=10,
        id="cleanup_caches",
        replace_existing=True,
    )

    # 3.2 容器更新器任务
    if config.updater.enabled:
        scheduler.add_job(
            updater_service.run_check_all,
            "interval",
            minutes=config.updater.check_interval_minutes,
            id="updater_check",
            replace_existing=True,
        )
        scheduler.add_job(
            updater_service.run_log_cleanup,
            "interval",
            minutes=60,
            id="updater_log_cleanup",
            replace_existing=True,
        )

    scheduler.start()

    # 4. 后台执行初始化（不阻塞服务启动）
    _bg_task = asyncio.create_task(_background_startup())

    logger.info("服务已就绪，Web 页面可访问")
    logger.info("  - 镜像加速 UI:    http://<host>:8000/")
    logger.info("  - 容器更新器 UI:  http://<host>:8000/updater")
    logger.info("  - 代理入口:       http://<host>:8000/v2/")

    yield

    logger.info("关闭调度器...")
    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
        try:
            await _bg_task
        except (asyncio.CancelledError, Exception):
            pass
    scheduler.shutdown()


app = FastAPI(
    title=APP_NAME,
    description=f"{APP_TAGLINE} —— 镜像代理加速 + 容器镜像更新一体化平台",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(web_ui.router)
app.include_router(docker_proxy.router)
app.include_router(updater.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=config.server.debug,
        workers=1,
    )
