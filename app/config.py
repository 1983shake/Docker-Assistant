"""统一配置管理：自动创建 config.yaml，同时承载代理与容器更新器两部分配置。"""

import logging
import shutil
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import APP_NAME, APP_TAGLINE, __version__

logger = logging.getLogger("dockerassistant.config")

# 目录（可通过环境变量覆盖，便于容器挂载）
import os

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CONFIG_PATH = CONFIG_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = CONFIG_DIR / "config.example.yaml"

# 内置默认配置（供自动创建 config.yaml 使用）
DEFAULT_CONFIG_DICT: dict[str, Any] = {
    "app": {"name": APP_NAME, "tagline": APP_TAGLINE, "version": __version__},
    "server": {"host": "0.0.0.0", "port": 8000, "debug": False},
    "admin": {"user": "admin", "pass": "change_me"},
    "proxy": {
        "timeout": 10.0,
        "timeout_by_path": {"probe": 3.0, "manifests": 5.0, "blobs": 10.0},
        "blob_read_timeout": None,
        "blob_header_timeout": 30.0,
        "blob_first_byte_timeout": 30.0,
        "stream_chunk_size": 1048576,
        "max_redirects": 5,
        "fail_cooldown": 60,
        "timeout_fail_cooldown": 300,
        "forbidden_fail_cooldown": 600,
        "server_err_fail_cooldown": 180,
        "probe_timeout": 2.0,
        "follow_redirects": True,
        "follow_redirects_max": 5,
        "follow_redirect_fail_cooldown": 300,
        "blob_fail_cooldown": 600,
        "prefer_recent_success": True,
        "recent_success_window": 120,
        "affinity_window": 120,
        "probe_node_window": 300,
    },
    "access": {
        "ip_whitelist": [],
        "image_whitelist_regex": "",
        "image_blacklist_regex": "",
    },
    "auto_fetch": {
        "enabled": True,
        "interval_minutes": 1440,
        "api_url": "https://status.anye.xyz",
        "registry_types": ["hub", "ghcr", "quay", "mcr", "gcr", "elastic", "nvcr"],
        "filters": {"selectable": True, "access": "public"},
    },
    "health_check": {
        "interval_minutes": 60,
        "timeout_seconds": 5.0,
        "concurrent_batch": 5,
        "latency_threshold": 500.0,
        "disable_threshold": 9999.0,
        "auto_recover": True,
        "recover_after_minutes": 120,
    },
    "speed_test": {
        "enabled": True,
        "interval_minutes": 720,
        "duration_seconds": 5.0,
        "tag": "latest",
        "concurrent_batch": 5,
        "test_images_by_type": {
            "dockerhub": "library/python",
            "ghcr": "stefanprodan/podinfo",
            "gcr": "distroless/base",
            "quay": "prometheus/prometheus",
            "mcr": "dotnet/runtime",
            "elastic": "beats/filebeat",
            "nvcr": "nvidia/cuda",
        },
    },
    "logging": {
        "level": "INFO",
        "file": "data/dockermirrorflow.log",
        "max_bytes": 10485760,
        "backup_count": 5,
        "third_party_level": "WARNING",
    },
    "manually_disabled": [],
    "custom_nodes": [],
    "route_aliases": {},
    "search": {
        "enabled": True,
        "page_size": 25,
        "timeout": 10.0,
        "upstreams": [],
    },
    # ---------- 新增：容器更新器 ----------
    "updater": {
        "enabled": True,
        "check_interval_minutes": 60,
        "check_concurrency": 2,
        "auto_update": False,
        "mirrors": [
            "https://docker.m.daocloud.io",
            "https://docker.1panel.live",
            "https://hub.rat.dev",
        ],
        "use_direct": True,
        "pull_use_mirror": True,
        "registry_username": "",
        "registry_password": "",
        "log_max_entries": 5000,
        "log_retention_days": 7,
        "log_display_level": "INFO",
        "containers": {},
    },
}


# ============================================================
#  固定应用元信息
# ============================================================
class AppMeta(BaseModel):
    name: str = APP_NAME
    tagline: str = APP_TAGLINE
    version: str = __version__

    @field_validator("name", mode="before")
    @classmethod
    def _lock_name(cls, _v):
        return APP_NAME

    @field_validator("tagline", mode="before")
    @classmethod
    def _lock_tagline(cls, _v):
        return APP_TAGLINE

    @field_validator("version", mode="before")
    @classmethod
    def _lock_version(cls, _v):
        return __version__


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False


class AdminConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user: str = "admin"
    pass_: str = Field(default="change_me", alias="pass")


class TimeoutByPath(BaseModel):
    probe: float = 3.0
    manifests: float = 5.0
    blobs: float = 10.0


class ProxyConfig(BaseModel):
    timeout: float = 10.0
    timeout_by_path: TimeoutByPath = TimeoutByPath()
    max_redirects: int = 5
    stream_chunk_size: int = 1048576
    fail_cooldown: int = 60
    timeout_fail_cooldown: int = 300
    forbidden_fail_cooldown: int = 600
    server_err_fail_cooldown: int = 180
    probe_timeout: float = 2.0
    follow_redirects: bool = True
    follow_redirects_max: int = 5
    follow_redirect_fail_cooldown: int = 300
    blob_fail_cooldown: int = 600
    blob_read_timeout: Optional[float] = None
    blob_header_timeout: Optional[float] = 30.0
    blob_first_byte_timeout: Optional[float] = 30.0
    prefer_recent_success: bool = True
    recent_success_window: int = 120
    affinity_window: int = 120
    probe_node_window: int = 300


class AccessConfig(BaseModel):
    ip_whitelist: list[str] = []
    image_whitelist_regex: str = ""
    image_blacklist_regex: str = ""

    @field_validator("ip_whitelist", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class AutoFetchConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 1440
    api_url: str = "https://status.anye.xyz"
    registry_types: list[str] = ["hub", "ghcr", "quay", "mcr", "gcr", "elastic", "nvcr"]
    filters: dict[str, Any] = {"selectable": True, "access": "public"}

    @field_validator("registry_types", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("filters", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class HealthCheckConfig(BaseModel):
    interval_minutes: int = 60
    timeout_seconds: float = 5.0
    latency_threshold: float = 500.0
    disable_threshold: float = 9999.0
    concurrent_batch: int = 5
    auto_recover: bool = True
    recover_after_minutes: int = 120


DEFAULT_SPEED_TEST_IMAGES: dict[str, str] = {
    "dockerhub": "library/python",
    "ghcr": "stefanprodan/podinfo",
    "gcr": "distroless/base",
    "quay": "prometheus/prometheus",
    "mcr": "dotnet/runtime",
    "elastic": "beats/filebeat",
    "nvcr": "nvidia/cuda",
}


class SpeedTestConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 720
    duration_seconds: float = 5.0
    tag: str = "latest"
    concurrent_batch: int = 5
    test_images_by_type: dict[str, str] = DEFAULT_SPEED_TEST_IMAGES.copy()

    @field_validator("test_images_by_type", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/dockermirrorflow.log"
    max_bytes: int = 10485760
    backup_count: int = 5
    third_party_level: str = "WARNING"


class CustomNode(BaseModel):
    name: str
    url: str
    registry_type: str = "dockerhub"
    route_prefix: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    enabled: bool = True


class ManuallyDisabledNode(BaseModel):
    url: str
    reason: str = ""
    disabled_at: str = ""


class SearchUpstream(BaseModel):
    name: str
    url: str


class SearchConfig(BaseModel):
    enabled: bool = True
    page_size: int = 25
    timeout: float = 10.0
    upstreams: list[SearchUpstream] = []

    @field_validator("upstreams", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class UpdaterConfig(BaseModel):
    """容器更新器配置（从 DockerImageUpdater 合并而来）。"""

    enabled: bool = True
    check_interval_minutes: int = 60
    check_concurrency: int = 2
    auto_update: bool = False
    mirrors: list[str] = [
        "https://docker.m.daocloud.io",
        "https://docker.1panel.live",
        "https://hub.rat.dev",
    ]
    use_direct: bool = True
    pull_use_mirror: bool = True
    registry_username: str = ""
    registry_password: str = ""
    log_max_entries: int = 5000
    log_retention_days: int = 7
    log_display_level: str = "INFO"
    # container_name -> {"strategy": "track"|"latest"|"pin", "target_tag": "..."}
    containers: dict[str, dict[str, str]] = {}

    @field_validator("mirrors", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("containers", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppMeta = AppMeta()
    server: ServerConfig = ServerConfig()
    admin: AdminConfig = AdminConfig()
    proxy: ProxyConfig = ProxyConfig()
    access: AccessConfig = AccessConfig()
    auto_fetch: AutoFetchConfig = AutoFetchConfig()
    health_check: HealthCheckConfig = HealthCheckConfig()
    speed_test: SpeedTestConfig = SpeedTestConfig()
    logging: LoggingConfig = LoggingConfig()
    custom_nodes: list[CustomNode] = []
    manually_disabled: list[ManuallyDisabledNode] = []
    route_aliases: dict[str, list[str]] = {}
    search: SearchConfig = SearchConfig()
    updater: UpdaterConfig = UpdaterConfig()

    @field_validator("custom_nodes", "manually_disabled", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []

    @field_validator("route_aliases", mode="before")
    @classmethod
    def _none_to_dict(cls, v):
        return v or {}


# ============================================================
#  自动创建 / 加载 / 保存
# ============================================================
def ensure_config_exists() -> bool:
    """
    确保 CONFIG_DIR/config.yaml 存在。
    首次启动时按以下顺序尝试创建：
      1. 若 CONFIG_DIR/config.example.yaml 存在 → 复制它
      2. 否则 → 写入内置默认配置
    返回 True 表示是本次新建的。
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        return False

    logger.info(f"未发现 {CONFIG_PATH}，正在自动创建...")

    if CONFIG_EXAMPLE_PATH.exists():
        try:
            shutil.copy2(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            logger.info(f"已从 {CONFIG_EXAMPLE_PATH} 复制生成 config.yaml")
            return True
        except Exception as e:
            logger.warning(f"复制 example 失败，改用内置默认配置：{e}")

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(
                DEFAULT_CONFIG_DICT,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        logger.info(f"已写入内置默认配置到 {CONFIG_PATH}")
        return True
    except Exception as e:
        raise RuntimeError(f"无法创建配置文件 {CONFIG_PATH}: {e}") from e


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    ensure_config_exists()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 应用名称/标语/版本固定
    data["app"] = {"name": APP_NAME, "tagline": APP_TAGLINE, "version": __version__}

    for key in ("custom_nodes", "manually_disabled"):
        if data.get(key) is None:
            data[key] = []
    if data.get("route_aliases") is None:
        data["route_aliases"] = {}
    if data.get("search") is None:
        data["search"] = {}
    if data.get("speed_test") is None:
        data["speed_test"] = {}
    if data.get("updater") is None:
        data["updater"] = {}

    return AppConfig(**data)


def reload_config(path: Path = CONFIG_PATH) -> AppConfig:
    new_config = load_config(path)
    for field_name in AppConfig.model_fields.keys():
        try:
            setattr(config, field_name, getattr(new_config, field_name))
        except Exception as e:
            logger.error(f"重载字段 {field_name} 失败: {e}")
    logger.info(f"配置已重载: {path}")
    return config


def save_config(path: Path = CONFIG_PATH) -> None:
    """把内存中的 config 落盘（保持 app 段固定值）。"""
    data = config.model_dump(mode="json", by_alias=True)
    data["app"] = {"name": APP_NAME, "tagline": APP_TAGLINE, "version": __version__}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp.replace(path)


# 全局配置对象
config = load_config()
