"""数据库引擎 + ORM 模型。"""

from __future__ import annotations

import enum
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, create_engine

from app.config import DATA_DIR

logger = logging.getLogger("dockerassistant.db")

os.makedirs(str(DATA_DIR), exist_ok=True)
engine = create_engine(
    f"sqlite:///{DATA_DIR}/docker-assistant.db",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def get_shanghai_time() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


class NodeStatus(str, enum.Enum):
    ONLINE = "online"
    SLOW = "slow"
    OFFLINE = "offline"
    DISABLED = "disabled"


class ProxyNode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str = Field(unique=True, index=True)
    registry_type: str = Field(default="dockerhub")
    route_prefix: Optional[str] = Field(default=None, index=True)
    enabled: bool = True
    latency: float = Field(default=9999.0)
    speed: float = Field(default=0.0)
    last_check: Optional[datetime] = None
    is_default: bool = False
    is_custom: bool = False
    manually_disabled: bool = False
    manual_disable_reason: Optional[str] = None
    manual_disable_at: Optional[datetime] = None
    username: Optional[str] = None
    password: Optional[str] = None
    failure_reason: Optional[str] = None
    download_bytes: int = Field(default=0)
    created_at: datetime = Field(default_factory=get_shanghai_time)
    updated_at: datetime = Field(default_factory=get_shanghai_time)

    @property
    def status(self) -> NodeStatus:
        if self.manually_disabled or not self.enabled:
            return NodeStatus.DISABLED
        if self.latency >= 9999:
            return NodeStatus.OFFLINE
        if self.latency >= 500:
            return NodeStatus.SLOW
        return NodeStatus.ONLINE


class TrafficStats(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True)
    download_bytes: int = Field(default=0)
    upload_bytes: int = Field(default=0)
    request_count: int = Field(default=0)


class PullHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_time: datetime = Field(default_factory=get_shanghai_time, index=True)
    image: str = Field(index=True)
    tag: str
    client_ip: str
    node_id: Optional[int] = Field(default=None, foreign_key="proxynode.id")
    node_name: Optional[str] = None
    status: str = Field(default="success")
    error_message: Optional[str] = None
    download_bytes: int = Field(default=0)


class HealthCheckLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: int = Field(foreign_key="proxynode.id", index=True)
    node_name: str
    check_time: datetime = Field(default_factory=get_shanghai_time, index=True)
    success: bool
    latency: float
    error_message: Optional[str] = None


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def upgrade_db():
    """检查缺失的列并添加（自动迁移）。"""
    try:
        inspector = inspect(engine)

        if inspector.has_table("proxynode"):
            columns = [c["name"] for c in inspector.get_columns("proxynode")]
            new_columns = {
                "registry_type": "VARCHAR DEFAULT 'dockerhub'",
                "route_prefix": "VARCHAR",
                "failure_reason": "VARCHAR",
                "download_bytes": "INTEGER NOT NULL DEFAULT 0",
                "is_custom": "BOOLEAN DEFAULT 0",
                "manually_disabled": "BOOLEAN DEFAULT 0",
                "manual_disable_reason": "VARCHAR",
                "manual_disable_at": "DATETIME",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
                "speed": "REAL DEFAULT 0",
            }
            with engine.connect() as conn:
                for col, col_type in new_columns.items():
                    if col not in columns:
                        logger.info(f"迁移: 添加 {col} 列到 proxynode")
                        conn.execute(text(f"ALTER TABLE proxynode ADD COLUMN {col} {col_type}"))
                conn.commit()

        if inspector.has_table("pullhistory"):
            columns = [c["name"] for c in inspector.get_columns("pullhistory")]
            new_columns = {
                "status": "VARCHAR DEFAULT 'success'",
                "error_message": "VARCHAR",
                "download_bytes": "INTEGER NOT NULL DEFAULT 0",
            }
            with engine.connect() as conn:
                for col, col_type in new_columns.items():
                    if col not in columns:
                        logger.info(f"迁移: 添加 {col} 列到 pullhistory")
                        conn.execute(text(f"ALTER TABLE pullhistory ADD COLUMN {col} {col_type}"))
                conn.commit()

        if not inspector.has_table("healthchecklog"):
            SQLModel.metadata.create_all(engine)
    except Exception as e:
        logger.error(f"迁移失败: {e}")
