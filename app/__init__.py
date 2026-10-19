"""Docker-Assistant：镜像加速 + 容器更新一体化管理平台。

本模块是应用**元信息**的唯一来源（Single Source of Truth）：
  - 版本号、名称、标语、起始年份、仓库地址、许可证
  - 结构化元信息 APP_INFO（供 API 直接返回）

版本变更历史已迁移至仓库根目录的 `CHANGELOG.md`（人类可读，亦为运行时
`/api/updater/changelog` 的数据来源）。本模块**不再内嵌** changelog 记录，
新增 / 修改变更请直接编辑仓库根目录的 `CHANGELOG.md`：
  - 正式发版前，改动累积在 `## [Unreleased]`（测试版本记录）段落；
  - 正式发版时，把该段重命名为 `## [x.y.z] - YYYY-MM-DD`，并新增一个空的
    `## [Unreleased]` 段落到顶部。

其他模块**不得**重复定义元信息，一律通过 `from app import ...`
引用，避免多版本号散落导致不一致。
"""

from __future__ import annotations

import os
import re

# ============================================================
#  版本号（唯一来源：发布新版本时只需修改这里）
# ============================================================
__version__ = "1.3.0"


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
#    已迁移至仓库根目录 CHANGELOG.md（单一来源）。
#    运行时 /api/updater/changelog 通过 get_changelog() 解析该文件。
#    本模块不再内嵌 changelog 记录。
# ============================================================
_CHANGELOG_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGELOG.md"
)

_VERSION_RE = re.compile(r"^##\s+\[([^\]]+)\](?:\s*-\s*(\S+))?\s*$")
_CHANGE_RE = re.compile(r"^-\s+(.*)$")


# ============================================================
#  辅助函数
# ============================================================
def get_app_info() -> dict:
    """返回应用元信息副本（防止调用方意外修改 APP_INFO）。"""
    return dict(APP_INFO)


def get_changelog(limit: int = 0) -> list[dict]:
    """返回变更历史（从仓库根目录 CHANGELOG.md 解析）。

    limit > 0 时只返回最近 limit 个版本；limit <= 0 时返回全部。
    返回值是深拷贝，调用方可安全修改。
    文件缺失或解析失败一律返回空列表，不影响接口可用性。
    """
    import copy

    items = _parse_changelog()
    if limit and limit > 0:
        items = items[:limit]
    return copy.deepcopy(items)


def _parse_changelog() -> list[dict]:
    """解析 Keep a Changelog 格式的 CHANGELOG.md。

    版本块格式：
        ## [<version>] - <date>      （date 可省略，如 Unreleased）
        <可选标题行>
        - 变更条目
        - 变更条目
    """
    try:
        with open(_CHANGELOG_MD, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []

    entries: list[dict] = []
    cur = None
    for line in lines:
        m = _VERSION_RE.match(line)
        if m:
            if cur is not None:
                entries.append(cur)
            cur = {
                "version": m.group(1),
                "date": m.group(2) or "",
                "title": "",
                "changes": [],
            }
            continue
        if cur is None:
            continue
        cm = _CHANGE_RE.match(line)
        if cm:
            cur["changes"].append(cm.group(1).strip())
        elif line.strip() and not line.startswith(("#", ">", "<")):
            # 版本头之后、首个变更条目之前的单行 = 标题
            # 跳过 blockquote（>）与 HTML 注释（<）等辅助行
            if not cur["title"]:
                cur["title"] = line.strip()
    if cur is not None:
        entries.append(cur)
    return entries


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
