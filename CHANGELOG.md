# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **单一来源（Single Source of Truth）**：本文件即是版本变更记录的权威来源，也是运行时
> `/api/updater/changelog` API 的数据来源——`app/__init__.py` 的 `get_changelog()` 在请求时解析本文件。
> 代码内不再内嵌 changelog 记录，所有变更直接编辑本文件即可。

## 变更记录规范

- **未正式发版（测试阶段）**：所有改动先累积在顶部 `## [Unreleased]` 段落，即「测试版本记录」。
- **正式发版时**：将 `## [Unreleased]` 整段重命名为 `## [x.y.z] - YYYY-MM-DD`，并在顶部新增一个空的 `## [Unreleased]` 段落承接后续改动。
- 每条变更以 `-` 开头；建议前缀：`新增` / `修复` / `改进` / `优化` / `重构` / `安全` / `工程`。
- 版本头之后可跟一行标题（解析器会作为该版本的 `title`），标题与首个变更条目之间留空行。

## [Unreleased]

> 测试版本记录：正式发版前的改动累积于此。发版时整段转为正式版本记录。

## [1.3.1] - 2026-10-19

拉取统计无感刷新修复 · Docker 构建去依赖 CHANGELOG.md

- 修复：顶部「N 次拉取（成功/失败/取消）」统计更新镜像后不无感刷新——模板误用 Jinja 定界符 `{{ }}`（服务端渲染成静态文本），改为 Vue 定界符 `[[ ]]` 恢复响应式
- 工程：Docker 构建不再依赖 `CHANGELOG.md`（`Dockerfile` 移除 `COPY`，运行时文件缺失由 `get_changelog()` 容错返回空）

## [1.3.0] - 2026-10-16

取消强制关闭 · 拉取记录无感刷新 · 构建缓存明细清理 · 全流程优化

- 新增：取消更新时强制关闭拉取连接（独立客户端 + 看门狗线程），不再出现任务一直挂着
- 修复：镜像更新后最近拉取记录无感刷新（更新成功 / 失败 / 取消均局部刷新，不整页重载）
- 新增：更新拉取记录补记——未走本地代理的拉取（直连 / 外部镜像源 / 非 Docker Hub）也会生成记录，与代理记录自动去重
- 改进：打开拉取记录浮窗时总是拉取最新数据
- 改进：顶栏网速仅显示下载速率，去掉上传速率显示
- 新增：镜像管理页构建缓存明细（ID / 类型 / 构建步骤 / 大小 / 使用中 / 最近使用），支持勾选清理与全选
- 新增：Docker Engine API v1.51+ 按 ID 精确清理构建缓存，旧版本自动降级为清理全部未使用并提示
- 新增：GET `/api/updater/images/build-cache`、POST `/api/updater/images/prune-build-cache` 端点
- 新增：GET `/api/pulls/stats` 端点，返回最近拉取统计（次数 / 总大小 / 总耗时）
- 优化：加速节点心跳与测速共享 HTTP 连接池，落库改为按批一次事务
- 优化：容器检测本地 / 远程 digest 并行查询，单容器检查耗时约减半
- 优化：新增 `get_container_info` 按名直查容器，替换全量列表扫描；registry 查询改为线程级 Session 复用
- 修复：Vue `data()` 中 `pull_stats` 未定义导致页头拉取统计不渲染
- 工程：激活 `.gitignore`（原 `gitignore.txt` 不生效），补充 `.backups/` 等忽略规则，清理 `__pycache__`

## [1.2.0] - 2026-10-05

架构精简 · 版本信息集中

- 重构：删除 `services/` 子包，Python 文件从 17 个精简到 11 个
- 重构：`database.py` + `models.py` 合并为 `db.py`
- 重构：`log_handler.py` + `progress.py` 合并为 `core.py`
- 重构：`registry_client.py` + `search_service.py` 合并为 `registry.py`
- 重构：`web_ui.py` + `updater.py` 合并为 `routers/web.py`
- 重构：`docker_proxy.py` 迁移为 `routers/proxy.py`
- 重构：`traffic_logger.py` 重命名为 `traffic.py`
- 重构：`docker_service.py` / `proxy_manager.py` / `updater_service.py` 从 `services/` 上移到 `app/`
- 改进：应用版本、名称、标语、起始年份、变更历史统一收口到 `app/__init__.py`
- 改进：所有模块统一从 `app` 导入元信息，消除多处硬编码
- 改进：`/api/updater/meta` 返回结构化 `APP_INFO`（含 `start_year` / `repo` / `license`）
- 新增：`/api/updater/changelog` 端点，对外提供版本变更历史
- 安全：`/api/updater/*` 端点纳入 HTTP Basic 认证保护
- 改进：前端 footer 年份使用 `start_year` 动态渲染

## [1.1.1] - 2026-08-20

StreamConsumed 修复 · 代理前缀标签清理

- 修复：blob 预热探测后 httpx 抛 `StreamConsumed`，导致每个 blob 选到快节点后立刻断流
- 改进：预热阶段创建的 aiter 直接返回给 `iter_response` 复用，全程只消费一次
- 改进：拉取镜像后自动清理 `"<mirror>/<image>"` 代理前缀 tag（保留原始 tag）
- 改进：镜像列表展示层剥离代理前缀，避免暴露内部地址
- 改进：新增镜像「使用中 / 未使用」多级匹配（ID / 短 ID / tag / Docker 兜底字段）
- 改进：容器重建时按容器内目标路径去重挂载点，避免 `Duplicate mount point`
- 改进：新增低速切换（预热探测 + 中途低速监测 + 熔断切换）
- 改进：更新 API 返回前同步执行一次版本检测，UI 立即显示最新状态

## [1.1.0] - 2026-07-15

容器更新融合进主页面

- 改进：容器更新器与镜像加速器融合为单一 Web 页面（四大标签页）
- 改进：新增顶栏实时统计（需更新 / 错误 / 未使用 / 悬空镜像）
- 改进：新增统一状态浮窗，展示所有任务进度
- 改进：任务完成后仅刷新当前标签页，不再整页重载
- 改进：容器检测前置条件可配置为「等待镜像加速就绪」

## [1.0.0] - 2026-06-01

首个正式版本

- 镜像加速：多 registry 支持（Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR）
- 镜像加速：按速度排序，流式转发，智能重定向跟随
- 容器更新：digest 比对，一键更新，安全重建
- Web 后台：镜像加速 / 容器列表 / 镜像管理 / 运行日志
- 单容器、单端口、单配置文件，首次启动自动生成 `config.yaml`
