# Docker-Assistant

> **镜像加速 · 容器更新** —— 一体化 Docker 管理平台

Docker-Assistant 是一个把「镜像代理加速」与「容器镜像更新」合并到单个容器的自托管工具。既可作为 Docker 镜像拉取加速节点，又能自动检测本地容器是否有新镜像并一键更新，全部功能通过一个 Web 后台统一管理。

---

## 功能特点

### 镜像加速

- **多 registry 支持**：Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR
- **多源聚合**：自动从上游 API 拉取免费镜像节点，一键同步
- **按速度排序**：定期速度测试，拉取时按速度降序尝试所有节点
- **在线检测**：只探测 `/v2/`，快速判断节点可达性；离线自动熔断、冷却后恢复
- **流式转发**：1MB 分块流式返回，支持大镜像续传
- **智能重定向跟随**：自动跟随 3xx，兼容 CDN 节点
- **低速切换**：预热探测 + 中途监测，低速自动换节点
- **访问控制**：IP 白名单、镜像白/黑名单正则

### 容器更新

- **自动检测**：比对本地 digest 与远程 digest，判断是否有新版本
- **更新策略**：`track`（跟随当前 tag）/ `latest`（固定 latest）/ `pin`（指定版本）
- **一键更新**：拉取新镜像 + 安全重建容器
- **批量更新**：一次勾选多个容器并发更新
- **镜像管理**：列出、删除、清理悬空/未使用镜像

### 运行日志

- 镜像加速与容器更新共享同一日志
- 支持按级别过滤、自动滚动、清空、保留天数配置

### 一体化体验

- **单容器、单端口、单配置文件**，无外部依赖（SQLite 内嵌）
- **首次启动自动生成配置**，无需手动准备
- **Web 管理后台**：镜像加速 / 容器列表 / 镜像管理 / 运行日志
- **顶栏实时统计**：拉取状态、容器状态、网络速率
- **配置在线编辑**：修改后自动备份并立即重载

---

## 快速部署

### 1. 准备目录

```bash
mkdir -p /opt/docker-assistant && cd /opt/docker-assistant
```

把项目代码放到该目录下，最终结构：

```
/opt/docker-assistant/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
├── config/                # 空目录，运行时自动生成 config.yaml
└── app/
    └── ...
```

> **无需提前准备 `config.yaml`**：首次启动时程序会自动生成一份带完整注释的默认配置。

### 2. 启动

```bash
docker compose up -d --build
```

首次启动会自动完成：

1. 生成 `config/config.yaml`（内置默认值 + 说明注释）
2. 初始化 SQLite 数据库
3. 后台拉取免费镜像节点
4. 后台执行首次在线检测、速度测试
5. 后台执行首次容器检测

### 3. 访问

| 功能                   | 地址                     |
| ---------------------- | ------------------------ |
| Web 管理后台           | `http://<host>:8000/`    |
| 镜像代理入口           | `http://<host>:8000`     |
| 代理协议入口（v2 API） | `http://<host>:8000/v2/` |

默认管理后台账号：`admin` / `change_me`（**请务必修改**）。

### 4. docker-compose.yml 参考

```yaml
services:
  docker-assistant:
    build:
      context: .
      args:
        # 基础镜像。默认走国内公共代理（DaoCloud）。
        #
        # 换成其他可用地址（按需替换）：
        #   - DaoCloud 代理：       docker.m.daocloud.io/library/python:3.12.10-slim
        #   - 1ms 代理：            docker.1ms.run/library/python:3.12.10-slim
        #   - 1Panel 代理：         docker.1panel.live/library/python:3.12.10-slim
        #   - 阿里云个人加速器：     <your-id>.mirror.aliyuncs.com/library/python:3.12.10-slim
        #   - 直连官方 Docker Hub： python:3.12.10-slim
        BASE_IMAGE: docker.m.daocloud.io/library/python:3.12.10-slim
    image: docker-assistant:latest
    container_name: docker-assistant
    restart: unless-stopped
    ports:
      # 若 8000 端口冲突，改这一行的左侧
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./config:/app/config
      - ./data:/app/data
    environment:
      # 若需改时区，改这一行
      - TZ=Asia/Shanghai
      - CONFIG_DIR=/app/config
      - DATA_DIR=/app/data
```

### 5. 快速验证

```bash
# 本机直接验证代理是否工作
docker pull <host>:8000/library/nginx:latest
```

---

## 客户端接入

### 单台 Docker 主机（本机 / 局域网）

#### 方式 A：作为 Docker Hub 镜像加速器（推荐）

编辑宿主机 `/etc/docker/daemon.json`：

```json
{
  "registry-mirrors": ["http://<host>:8000"]
}
```

然后重启 Docker：

```bash
sudo systemctl restart docker
```

之后所有 Docker Hub 官方镜像都会自动走本代理：

```bash
docker pull nginx:latest
docker pull library/redis:7
```

> **注意**：`registry-mirrors` 只对 **Docker Hub** 生效。GHCR / GCR / Quay 等必须用下面的「显式路径」方式。

#### 方式 B：显式路径（所有 registry 通用）

```bash
# Docker Hub
docker pull <host>:8000/library/nginx:latest

# GHCR
docker pull <host>:8000/ghcr.io/owner/image:tag

# GCR
docker pull <host>:8000/gcr.io/project-id/image:tag

# Quay
docker pull <host>:8000/quay.io/organization/image:tag

# MCR
docker pull <host>:8000/mcr.microsoft.com/dotnet/runtime:8.0

# Elastic
docker pull <host>:8000/docker.elastic.co/beats/filebeat:8.0.0

# NVCR
docker pull <host>:8000/nvcr.io/nvidia/cuda:12.0.0-base
```

#### 方式 C：HTTP 明文接入

Docker 默认要求 registry 使用 HTTPS，代理默认是 HTTP，需要在 `daemon.json` 中加入：

```json
{
  "registry-mirrors": ["http://192.168.1.100:8000"],
  "insecure-registries": ["192.168.1.100:8000"]
}
```

重启 Docker 后生效。

> **生产环境建议**：使用 Nginx / Caddy 加一层 HTTPS。

### Kubernetes / containerd

编辑 `/etc/containerd/config.toml`：

```toml
[plugins."io.containerd.grpc.v1.cri".registry.mirrors]
  [plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
    endpoint = ["http://192.168.1.100:8000"]

[plugins."io.containerd.grpc.v1.cri".registry.configs]
  [plugins."io.containerd.grpc.v1.cri".registry.configs."192.168.1.100:8000".tls]
    insecure_skip_verify = true
```

重启 containerd：

```bash
sudo systemctl restart containerd
```

### Podman / nerdctl

**Podman**：编辑 `/etc/containers/registries.conf`：

```toml
[[registry]]
prefix = "docker.io"
location = "192.168.1.100:8000"
insecure = true
```

重启 Podman 后生效。

---

## 需要修改点

部署后建议按以下顺序调整，全部在 Web 后台完成：

### 1. 修改管理后台账号（必做）

打开「配置文件 → 通用 → 管理后台认证」，修改：

- `用户名`
- `密码`

> 生产环境务必修改，默认 `admin / change_me` 非常危险。

### 2. 端口冲突（按需）

若 8000 端口被占用，编辑 `docker-compose.yml`：

```yaml
ports:
  - "9000:8000" # 左侧改为宿主机想要的端口
```

然后 `docker compose up -d`。

### 3. 时区（按需）

编辑 `docker-compose.yml`：

```yaml
environment:
  - TZ=Asia/Shanghai # 改为你的时区
```

### 4. 构建时基础镜像（按需）

若默认的 DaoCloud 代理不可用，编辑 `docker-compose.yml`：

```yaml
build:
  args:
    BASE_IMAGE: docker.1ms.run/library/python:3.12.10-slim
```

然后重新构建。

---

## 需要注意事项

### 1. Docker Socket 权限

容器更新功能需要挂载 `/var/run/docker.sock`。**这等效于宿主机 root 权限**，请务必：

- 仅在可信网络中部署
- 通过反向代理（Nginx / Caddy）加一层 HTTPS 与访问控制
- 设置强口令

如果**只需要镜像加速**、不需要容器更新功能：

- 移除 `docker-compose.yml` 中 `/var/run/docker.sock` 挂载
- 在 Web 后台「配置文件 → 容器列表」中关闭「启用容器更新」

### 2. 首次启动较慢是正常的

首次启动会拉取节点、做在线检测和速度测试，节点越多耗时越长。Web 页面可以立即访问，后台任务在右下角浮层展示进度。

### 3. 部分字段需要重启才能生效

以下配置**修改后需重启服务**（Web 后台会提示）：

- 服务器（host / port / debug）
- 日志（级别、文件等）
- 自动拉取间隔 / 在线检测间隔 / 速度测试间隔 / 容器检测间隔

其他字段（代理超时、熔断、粘性、访问控制等）**保存即生效**。

### 4. 无 `.env` 文件也能正常工作

本项目**没有 `.env.example`，也不依赖 `.env` 文件**。所有容器级参数（端口、时区、基础镜像）都硬编码在 `docker-compose.yml` 里，需要修改时直接编辑该文件。

应用级配置统一放在 `config/config.yaml`，首次启动自动生成，之后通过 Web 后台或直接编辑修改。

### 5. 数据持久化

- `config/`：`config.yaml` 与备份 `config.yaml.bak`
- `data/`：SQLite 数据库、日志、容器检测状态

升级或迁移时，保留 `config/` 与 `data/` 目录即可。

### 6. 升级步骤

```bash
git pull                      # 或替换代码
docker compose build --no-cache
docker compose up -d
```

**不会丢失配置**：`config/` 与 `data/` 在宿主机挂载，容器重建不受影响。

---

## 常见问题（FAQ）

### Q1：容器列表里没有我的容器？

- 确认已挂载 `/var/run/docker.sock` 且权限正确（可 `docker compose logs -f` 查看是否报 Docker 连接错误）
- 点击「刷新」按钮
- Docker-Assistant 自身的容器会被识别为「自身容器」，跳过检测（这是正常的）

### Q2：容器显示「有更新」，但点击更新失败？

常见原因：

- **私有镜像**：未配置「配置文件 → 容器列表 → 私有仓库认证」
- **镜像源不通**：尝试关闭「拉取走加速源」，直连 registry
- **容器名冲突**：重建过程中若出现同名容器残留，手动 `docker rm <name>` 后重试
- **查看详细错误**：切到「运行日志」标签，搜索容器名

### Q3：检测一直显示「等待镜像加速」，很久不动？

说明镜像加速尚未就绪（无可用节点）。处理方式：

- 在「加速节点」列表检查是否有可用节点
- 手动点击「获取免费节点」
- 或在「配置文件 → 容器列表」中关闭「等待镜像加速就绪」

### Q4：Docker 报 `http: server gave HTTP response to HTTPS client`？

代理默认是 HTTP，但 Docker 默认要求 HTTPS。三种解决方案（任选其一）：

1. **加 `insecure-registries`**（最简单）：

   ```json
   {
     "insecure-registries": ["192.168.1.100:8000"]
   }
   ```

2. **使用 `localhost`**：Docker 对 localhost 默认允许 HTTP，但仅限本机

3. **配置 HTTPS 反向代理**（推荐生产）

### Q5：升级到新版本会丢失配置吗？

不会。

- `config/config.yaml` 与 `data/` 在宿主机挂载，容器重建不受影响
- 配置新增字段会使用默认值（除非显式修改）

### Q6：拉取记录里为什么有重复条目？

正常情况下一拉取只记录一条。若发现大量重复，请检查：

- 是否为多次独立拉取（Docker daemon 会重试）
- 若仍有重复，可到「运行日志」搜索 `[pull]` 关键字确认

### Q7：`config.yaml` 删了会怎样？

下次启动会重新生成一份**默认配置**（`admin / change_me`）。如果你的自定义配置较多，**先备份再删除**。

### Q8：更新容器时报 `Duplicate mount point`？

**当前版本已修复**。重建时会按「容器内目标路径」去重。

若仍遇到：

- 确认使用的镜像是最新构建（`docker compose build --no-cache` 后重启）
- 检查容器本身是否真的配置了重复挂载

---

## 授权协议

本项目基于 **GNU General Public License v3.0（GPL-3.0）** 发布。

- 你可以自由使用、修改、分发本软件
- 分发修改版时**必须开源**，并以相同协议授权
- 必须保留原作者版权声明
- 作者不对使用本软件产生的任何后果负责

- 协议全文：<https://www.gnu.org/licenses/gpl-3.0.html>
- 中文参考：<https://www.gnu.org/licenses/gpl-3.0.zh-cn.html>
