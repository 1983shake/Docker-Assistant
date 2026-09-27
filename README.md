# Docker-Assistant（Docker助手）v1.0.0

**镜像加速 · 容器更新** —— 一体化 Docker 管理平台。

- **镜像代理加速**（来自 DockerMirrorFlow）：多源聚合、按速度自动排序、多 registry 支持（Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR），一键部署作为 Docker 镜像拉取加速节点。
- **容器镜像更新器**（合并自 DockerImageUpdater）：自动比对本地与远程 digest、发现更新、一键/批量拉取新镜像并安全重建容器。

## 快速开始

### 1. 准备目录

```bash
mkdir -p /opt/docker-assistant && cd /opt/docker-assistant
```

把本项目文件复制到该目录（`docker-compose.yml`、`Dockerfile`、`requirements.txt`、`app/`、`config/`）。

### 2. 启动

```bash
docker compose up -d --build
```

首次启动会自动：

- 创建 `config/config.yaml`（从 `config.example.yaml` 复制）
- 创建 `data/` 与 SQLite 数据库
- 后台拉取免费加速节点并做健康检测

### 3. 访问

| 功能                             | 地址                         |
| -------------------------------- | ---------------------------- |
| 镜像加速 UI                      | `http://<host>:8000/`        |
| 容器更新器 UI                    | `http://<host>:8000/updater` |
| 代理入口（配置到 Docker Daemon） | `http://<host>:8000`         |

默认管理后台账号：`admin` / `change_me`（**请务必在 config.yaml 或 Web 后台中修改**）。

### 4. 使用代理拉取镜像

```bash
docker pull <host>:8000/library/nginx:latest
docker pull <host>:8000/ghcr.io/owner/image:tag
```

或把 `<host>:8000` 加入 Docker Daemon 的 `registry-mirrors`。

## 关于 .env

`.env` 是**完全可选**的，只用于调整容器层面的参数（端口、时区、pip 源）。
应用配置统一放在 `config/config.yaml`。不创建 `.env` 时全部使用默认值。

如需自定义：

```bash
cp .env.example .env
# 修改 APP_PORT / TZ / PIP_INDEX_URL 等
docker compose up -d
```

## 目录结构

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── config/
│   └── config.example.yaml     # 首次启动自动复制为 config.yaml
├── data/                       # 运行时：SQLite + 日志 + 状态
└── app/
    ├── main.py
    ├── config.py
    ├── database.py
    ├── models.py
    ├── routers/
    │   ├── docker_proxy.py     # /v2/ 代理
    │   ├── web_ui.py           # 镜像加速 UI & API
    │   └── updater.py          # 容器更新器 UI & API
    ├── services/
    │   ├── proxy_manager.py
    │   ├── search_service.py
    │   ├── traffic_logger.py
    │   ├── docker_service.py
    │   ├── registry_client.py
    │   ├── log_handler.py
    │   └── updater_service.py
    └── templates/
        ├── index.html
        └── updater.html
```

## 安全提示

- 容器更新器需要挂载 `/var/run/docker.sock`，**这等效于宿主机 root 权限**，请仅在可信网络中部署，并务必设置 `admin.user` / `admin.pass`。
- 建议在反向代理（Nginx / Caddy）后启用 HTTPS。
