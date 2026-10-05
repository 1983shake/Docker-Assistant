"""Docker-Assistant：镜像加速 + 容器更新一体化管理平台。

本模块是应用元信息的唯一来源（Single Source of Truth）：
  - 版本号、名称、标语、起始年份、仓库地址、许可证
  - 结构化元信息 APP_INFO（供 API 直接返回）
  - 版本变更历史 CHANGELOG

其他模块**不得**再重复定义上述信息，一律通过 `from app import ...`
引用，避免多版本号散落导致不一致。
"""

from __future__ import annotations

# ============================================================
#  版本号（唯一来源：发布新版本时只需修改这里）
# ============================================================
__version__ = "1.2.0"


# ============================================================
#  应用元信息常量
# ============================================================
APP_NAME = "Docker-Assistant"
APP_TAGLINE = "镜像加速 · 容器更新"
APP_START_YEAR = 2026
APP_REPO = "https://github.com/1983shake/Docker-Assistant"
APP_LICENSE = "GPL-3.0"


# ============================================================
#  结构化元信息（供 API / 模板直接使用）
# ============================================================
APP_INFO: dict = {
    "name": APP_NAME,
    "tagline": APP_TAGLINE,
    "version": __version__,
    "start_year": APP_START_YEAR,
    "repo": APP_REPO,
    "license": APP_LICENSE,
}


# ============================================================
#  版本变更历史
#    新增版本时，在列表**头部**插入一条（最新版本在最前）。
# ============================================================
CHANGELOG: list[dict] = [
    {
        "version": "1.2.0",
        "date": "2026-10-05",
        "title": "架构精简 · 版本信息集中",
        "changes": [
            "重构：删除 services/ 子包，Python 文件从 17 个精简到 11 个",
            "重构：database.py + models.py 合并为 db.py",
            "重构：log_handler.py + progress.py 合并为 core.py",
            "重构：registry_client.py + search_service.py 合并为 registry.py",
            "重构：web_ui.py + updater.py 合并为 routers/web.py",
            "重构：docker_proxy.py 迁移为 routers/proxy.py",
            "重构：traffic_logger.py 重命名为 traffic.py",
            "重构：docker_service.py / proxy_manager.py / updater_service.py 从 services/ 上移到 app/",
            "改进：应用版本、名称、标语、起始年份、变更历史统一收口到 app/__init__.py",
            "改进：所有模块统一从 app 导入元信息，消除多处硬编码",
            "改进：/api/updater/meta 返回结构化 APP_INFO（含 start_year / repo / license）",
            "新增：/api/updater/changelog 端点，对外提供版本变更历史",
            "安全：/api/updater/* 端点纳入 HTTP Basic 认证保护",
            "改进：前端 footer 年份使用 start_year 动态渲染",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-08-20",
        "title": "StreamConsumed 修复 · 代理前缀标签清理",
        "changes": [
            "修复：blob 预热探测后 httpx 抛 StreamConsumed，导致每个 blob 选到快节点后立刻断流",
            "改进：预热阶段创建的 aiter 直接返回给 iter_response 复用，全程只消费一次",
            '改进：拉取镜像后自动清理 "<mirror>/<image>" 代理前缀 tag（保留原始 tag）',
            "改进：镜像列表展示层剥离代理前缀，避免暴露内部地址",
            "改进：新增镜像「使用中 / 未使用」多级匹配（ID / 短 ID / tag / Docker 兜底字段）",
            "改进：容器重建时按容器内目标路径去重挂载点，避免 Duplicate mount point",
            "改进：新增低速切换（预热探测 + 中途低速监测 + 熔断切换）",
            "改进：更新 API 返回前同步执行一次版本检测，UI 立即显示最新状态",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-07-15",
        "title": "容器更新融合进主页面",
        "changes": [
            "改进：容器更新器与镜像加速器融合为单一 Web 页面（四大标签页）",
            "改进：新增顶栏实时统计（需更新 / 错误 / 未使用 / 悬空镜像）",
            "改进：新增统一状态浮窗，展示所有任务进度",
            "改进：任务完成后仅刷新当前标签页，不再整页重载",
            "改进：容器检测前置条件可配置为「等待镜像加速就绪」",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-06-01",
        "title": "首个正式版本",
        "changes": [
            "镜像加速：多 registry 支持（Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR）",
            "镜像加速：按速度排序，流式转发，智能重定向跟随",
            "容器更新：digest 比对，一键更新，安全重建",
            "Web 后台：镜像加速 / 容器列表 / 镜像管理 / 运行日志",
            "单容器、单端口、单配置文件，首次启动自动生成 config.yaml",
        ],
    },
]


# ============================================================
#  辅助函数
# ============================================================
def get_app_info() -> dict:
    """返回应用元信息副本（防止调用方意外修改 APP_INFO）。"""
    return dict(APP_INFO)


def get_changelog(limit: int = 0) -> list[dict]:
    """返回变更历史。

    limit > 0 时只返回最近 limit 个版本；limit <= 0 时返回全部。
    返回值是深拷贝，调用方可安全修改。
    """
    import copy

    items = CHANGELOG[:limit] if limit and limit > 0 else CHANGELOG
    return copy.deepcopy(items)


def get_app_config_dict() -> dict:
    """返回写入 config.yaml 的 `app` 字段（只含 name / tagline / version）。

    config.yaml 里不应包含 start_year / repo / license 等运行时无关字段，
    因此单独提供一个精简版本，而非直接使用 APP_INFO。
    """
    return {
        "name": APP_NAME,
        "tagline": APP_TAGLINE,
        "version": __version__,
    }
