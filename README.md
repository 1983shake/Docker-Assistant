# Docker-Assistant

> **镜像加速 · 容器更新** —— 一体化 Docker 管理平台

Docker-Assistant 是一个把「镜像代理加速」与「容器镜像更新」合并到单个容器的自托管工具。它既可作为 Docker 镜像拉取加速节点（支持 Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR 等多 registry），又能自动检测本地容器是否有新镜像并一键更新，全部功能通过一个 Web 后台统一管理。

- **项目名称**：Docker-Assistant
- **当前版本**：v1.0.0
- **运行环境**：Python 3.12.10（Docker 镜像内已内置）
- **授权协议**：GNU General Public License v3.0（GPL-3.0）

---

## 目录

- [一、特点功能](#一特点功能)
- [二、Docker Compose 部署（推荐）](#二docker-compose-部署推荐)
- [三、外部接入镜像源的方法](#三外部接入镜像源的方法)
- [四、需要修改点](#四需要修改点)
- [五、需要注意事项](#五需要注意事项)
- [六、常见问题（FAQ）](#六常见问题faq)
- [七、授权协议](#七授权协议)

---

## 一、特点功能

### 1. 镜像加速（多源聚合，流式加速）

- **多 registry 支持**：Docker Hub、GHCR、GCR、Quay、MCR、Elastic、NVCR
- **多源聚合**：自动从上游 API 拉取免费镜像节点，一键同步
- **按速度排序**：定期速度测试，拉取时按 `speed` 降序依次尝试所有节点
- **在线检测**：只探测 `/v2/`，快速判断节点可达性；离线自动熔断、冷却后自动恢复
- **节点粘性**：镜像级 / 心跳级粘性窗口，减少节点切换
- **流式转发**：1MB 分块流式返回，支持大镜像续传场景
- **智能重定向跟随**：自动跟随 3xx，兼容会重定向到 CDN 的节点
- **访问控制**：IP 白名单、镜像白/黑名单正则
- **镜像搜索**：Web 后台直接搜索 Docker Hub，复制拉取命令

### 2. 容器更新（镜像检测 + 一键更新）

- **自动检测**：比对本地 digest 与远程 digest，判断是否有新版本
- **更新策略**：`track`（跟随当前 tag）/ `latest`（固定 latest）/ `pin`（指定版本）
- **一键更新**：拉取新镜像 + 安全重建容器（临时名创建 → 停止旧容器 → 删除 → 改名 → 启动）
- **批量更新**：一次勾选多个容器并发更新
- **智能更新源策略**：
  - **有路由的 registry**（Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR）→ **优先走节点路由**（内置 `local` 加速代理）
  - **无路由的 registry**（自定义 / 私有）→ **直接直连**
- **检测前置条件**：可配置为「等待镜像加速就绪后再检测」，避免刚启动时误判
- **镜像管理**：列出本地镜像、删除镜像（带占用提示）、清理悬空镜像
- **自动更新**（可选）：发现新版本时自动拉取并重建

### 3. 运行日志（共用）

- 镜像加速与容器更新共享同一内存环形缓冲 + JSONL 本地持久化
- 支持按级别过滤（DEBUG / INFO / WARNING / ERROR / CRITICAL）
- 支持自动滚动、清空、保留天数/条数配置

### 4. 一体化体验

- **单容器、单端口、单配置文件**，无外部依赖（SQLite 内嵌）
- **首次启动自动生成 `config/config.yaml`**，无需手动准备任何示例文件
- **Web 管理后台**：镜像加速 / 容器列表 / 镜像管理 / 运行日志 四大标签页
- **顶栏实时统计**：拉取状态 + 容器状态（需更新 / 错误 / 悬空镜像），点击可跳转
- **配置在线编辑**：修改后自动备份旧配置并立即重载
- **任务进度浮层**：节点拉取 / 在线检测 / 速度测试 / 容器检测 / 镜像更新，所有任务统一展示

---

## 二、Docker Compose 部署（推荐）

### 1. 目录准备

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
5. 后台执行首次容器检测（若已到达检测周期）

### 3. 访问

| 功能                                 | 地址                     |
| ------------------------------------ | ------------------------ |
| Web 管理后台                         | `http://<host>:8000/`    |
| 镜像代理入口（配置到 Docker Daemon） | `http://<host>:8000`     |
| 代理协议入口（v2 API）               | `http://<host>:8000/v2/` |

默认管理后台账号：`admin` / `change_me`（**请务必修改**）。

### 4. `docker-compose.yml` 参考

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

## 三、外部接入镜像源的方法

Docker-Assistant 的代理入口是标准的 **Registry v2 协议**，任何支持该协议的客户端都能接入。以下按客户端类型分别说明。

### 3.1 单台 Docker 主机（本机 / 局域网）

#### 方式 A：作为 Docker Hub 镜像加速器（推荐）

编辑宿主机 `/etc/docker/daemon.json`（Windows / macOS 请在 Docker Desktop → Settings → Docker Engine 中编辑）：

```json
{
  "registry-mirrors": ["http://<host>:8000"]
}
```

然后重启 Docker：

```bash
# Linux
sudo systemctl restart docker

# Docker Desktop：点击 Apply & Restart
```

之后所有 Docker Hub 官方镜像都会自动走本代理：

```bash
docker pull nginx:latest          # 实际会走 <host>:8000
docker pull library/redis:7       # 同上
```

> **注意**：`registry-mirrors` 只对 **Docker Hub** 生效。GHCR / GCR / Quay 等必须用下面的「显式路径」方式。

#### 方式 B：显式路径（所有 registry 通用）

不修改 Docker 配置，直接使用带前缀的路径：

```bash
# Docker Hub
docker pull <host>:8000/library/nginx:latest

# GHCR
docker pull <host>:8000/ghcr.io/owner/image:tag

# GCR
docker pull <host>:8000/gcr.io/project-id/image:tag
docker pull <host>:8000/registry.k8s.io/pause:3.9

# Quay
docker pull <host>:8000/quay.io/organization/image:tag

# MCR
docker pull <host>:8000/mcr.microsoft.com/dotnet/runtime:8.0

# Elastic
docker pull <host>:8000/docker.elastic.co/beats/filebeat:8.0.0

# NVCR
docker pull <host>:8000/nvcr.io/nvidia/cuda:12.0.0-base
```

也支持简写前缀（内置路由别名）：

```bash
docker pull <host>:8000/ghcr/owner/image:tag
docker pull <host>:8000/gcr/project/image:tag
docker pull <host>:8000/quay/org/image:tag
```

> 别名列表可在 `config/config.yaml` 的 `route_aliases` 中自定义；留空 `{}` 使用内置默认。

#### 方式 C：HTTP 明文接入（非 HTTPS 环境必需）

Docker 默认要求 registry 使用 HTTPS，除非是 `localhost`。如果你的代理是 **HTTP**（默认就是 HTTP），需要在 `daemon.json` 中把主机加入 `insecure-registries`：

```json
{
  "registry-mirrors": ["http://192.168.1.100:8000"],
  "insecure-registries": ["192.168.1.100:8000"]
}
```

重启 Docker 后生效。

> **生产环境建议**：使用 Nginx / Caddy 加一层 HTTPS（见 3.5），这样无需 `insecure-registries`，也更安全。

### 3.2 局域网其他主机接入

其他主机只要**网络可达**本服务，接入方式与 3.1 完全一致：

```bash
# 在另一台主机上
docker pull 192.168.1.100:8000/library/nginx:latest
```

**确保本服务监听所有网卡**（默认已满足）：

```yaml
# config/config.yaml
server:
  host: "0.0.0.0" # 不要改成 127.0.0.1
  port: 8000
```

**确保防火墙放行**：

```bash
# 以 ufw 为例
sudo ufw allow 8000/tcp

# 以 firewalld 为例
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

**可选：设置 IP 白名单**（推荐生产环境）：

```yaml
# config/config.yaml
access:
  ip_whitelist:
    - "192.168.1.0/24" # 允许整个局域网
    - "10.0.0.0/8"
```

留空 `[]` 表示不限制。

### 3.3 Kubernetes / containerd 接入

#### containerd（K8s 1.24+ 默认运行时）

编辑 `/etc/containerd/config.toml`：

```toml
[plugins."io.containerd.grpc.v1.cri".registry.mirrors]
  # Docker Hub 官方镜像
  [plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
    endpoint = ["http://192.168.1.100:8000"]

[plugins."io.containerd.grpc.v1.cri".registry.configs]
  # 若代理为 HTTP，需允许非安全传输
  [plugins."io.containerd.grpc.v1.cri".registry.configs."192.168.1.100:8000".tls]
    insecure_skip_verify = true
```

重启 containerd：

```bash
sudo systemctl restart containerd
```

> containerd 的 `mirrors` 也只对 Docker Hub 生效。其他 registry 需在 Pod spec 中显式使用 `<host>:8000/ghcr.io/...` 形式。

#### 显式路径拉取（所有 registry 通用）

在 Pod / Deployment 的 `image` 字段直接写代理路径：

```yaml
spec:
  containers:
    - name: app
      image: 192.168.1.100:8000/ghcr.io/owner/image:tag
```

### 3.4 Podman / nerdctl / 其他

**Podman**：编辑 `/etc/containers/registries.conf`：

```toml
[[registry]]
prefix = "docker.io"
location = "192.168.1.100:8000"
insecure = true
```

重启 Podman 后，`podman pull nginx:latest` 会自动走代理。

**nerdctl**：与 containerd 共用配置，参考 3.3。

**其他支持 Registry v2 的客户端**：只要能自定义 registry endpoint，指向 `http://<host>:8000` 即可。

### 3.5 HTTPS 反向代理（生产推荐）

用 Nginx / Caddy 给代理加一层 HTTPS，可以避免 `insecure-registries` 配置，也更安全。

#### 使用 Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name docker.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    # 大镜像上传/下载需要放开 body 大小
    client_max_body_size 0;
    chunked_transfer_encoding on;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 流式传输关键：关闭缓冲，让 blob 直接透传
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

#### 使用 Caddy（更简单，自动 HTTPS）

```caddyfile
docker.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
```

#### 客户端接入（HTTPS 场景）

```bash
docker pull docker.example.com/library/nginx:latest
docker pull docker.example.com/ghcr.io/owner/image:tag
```

`daemon.json` 不需要 `insecure-registries`，直接：

```json
{
  "registry-mirrors": ["https://docker.example.com"]
}
```

> **注意**：使用 HTTPS 域名后，`registry-mirrors` 里的地址是域名；其他 registry 的显式路径也要用域名，例如 `docker.example.com/ghcr.io/...`。

### 3.6 接入方式对比

| 场景                 | 推荐方式                                   | 是否需改 Docker 配置 | 支持其他 registry  |
| -------------------- | ------------------------------------------ | :------------------: | :----------------: |
| 单机 Docker Hub 加速 | `registry-mirrors`                         |          是          |         否         |
| 单机全 registry 加速 | 显式路径                                   |          否          |         是         |
| 局域网多主机         | `registry-mirrors` + `insecure-registries` |          是          |         否         |
| K8s / containerd     | `mirrors` 或 Pod image 显式路径            |       是 / 否        |       视方式       |
| 生产 / 公网          | Nginx / Caddy + HTTPS                      |          否          | 否（仍需显式路径） |

### 3.7 验证是否接入成功

```bash
# 1. 先确认本服务可访问
curl http://<host>:8000/v2/ -I
# 期望：HTTP/1.1 200 OK 或 401 Unauthorized

# 2. 拉取一个镜像测试
docker pull <host>:8000/library/hello-world

# 3. 查看本服务的「运行日志」标签页，应能看到本次拉取记录
#    或在「最近拉取记录」浮窗中查看
```

如果拉取成功但日志无记录：

- 检查「配置文件 → 日志」的 `logging.level` 是否为 `INFO`
- 检查代理是否被反向代理的日志拦截（未真正请求到本服务）

---

## 四、需要修改点

部署后建议按以下顺序调整，全部在 Web 后台完成：

### 1. 修改管理后台账号（必做）

打开「配置文件 → 基础」，修改：

- `admin.user`
- `admin.pass`

> 生产环境务必修改，默认 `admin / change_me` 非常危险。

### 2. 端口冲突（按需）

若 8000 端口被占用：

- **方式一（推荐）**：编辑 `docker-compose.yml` 中的 `ports` 行：

  ```yaml
  ports:
    - "9000:8000" # 左侧改为宿主机想要的端口
  ```

  然后 `docker compose up -d`。

- **方式二**：修改 `config.yaml` 里的 `server.port`，同时同步修改 `docker-compose.yml` 的 `ports`（内外都要改），**需重启服务**。

### 3. 镜像加速节点（一般无需修改）

- 默认已启用自动拉取，上游 API 为 `https://status.anye.xyz`
- 如需自定义，在「配置文件 → 调度 → 自动拉取」中调整
- 手动禁用个别节点：在「加速节点」列表中点「手动禁用」

### 4. 容器更新源（默认已配置）

- 默认 `updater.mirrors = ["local"]`，即检测时走内置镜像加速
- **检测/拉取策略**：
  - **有路由的 registry**（Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR）→ 走节点路由
  - **无路由的 registry**（自定义 / 私有）→ 直连
- 若需添加自定义源：在「配置文件 → 容器更新」的「更新源列表」中追加 URL
- 检测请求按顺序尝试；拉取时 `"local"` 会自动跳过（Docker daemon 无法回连容器内部）

### 5. 检测/测速周期（可选）

- 在线检测周期：`health_check.interval_minutes`，默认 60 分钟
- 速度测试周期：`speed_test.interval_minutes`，默认 720 分钟
- 容器检测周期：`updater.check_interval_minutes`，默认 60 分钟
- **修改 `interval_minutes` 需重启服务**

### 6. 日志与访问控制（按需）

- 日志：`logging.level`、`logging.third_party_level`
- 访问控制：`access.ip_whitelist`、`access.image_whitelist_regex`、`access.image_blacklist_regex`

### 7. 时区（按需）

编辑 `docker-compose.yml`：

```yaml
environment:
  - TZ=Asia/Shanghai # 改为你的时区
```

### 8. 构建时基础镜像（按需）

若默认的 DaoCloud 代理不可用，编辑 `docker-compose.yml`：

```yaml
build:
  args:
    BASE_IMAGE: docker.1ms.run/library/python:3.12.10-slim
```

然后重新构建。

---

## 五、需要注意事项

### 1. Docker Socket 权限

容器更新功能需要挂载 `/var/run/docker.sock`。**这等效于宿主机 root 权限**，请务必：

- 仅在可信网络中部署
- 通过反向代理（Nginx / Caddy）加一层 HTTPS 与访问控制
- 设置强口令（见「需要修改点 #1」）

如果**只需要镜像加速**、不需要容器更新功能：

- 移除 `docker-compose.yml` 中 `/var/run/docker.sock` 挂载
- 在 Web 后台「配置文件 → 容器更新」中关闭 `updater.enabled`

### 2. 首次启动较慢是正常的

首次启动会拉取节点、做在线检测和速度测试，节点越多耗时越长。Web 页面可以立即访问，后台任务在右下角浮层展示进度。

### 3. 部分字段需要重启才能生效

以下配置**修改后需重启服务**（Web 后台会提示）：

- `server.*`（host / port / debug）
- `logging.*`
- `auto_fetch.interval_minutes`
- `health_check.interval_minutes`
- `speed_test.interval_minutes`
- `updater.check_interval_minutes`

其他字段（代理超时、熔断、粘性、访问控制、镜像列表等）**保存即生效**。

### 4. 无 `.env` 文件也能正常工作

本项目**没有 `.env.example`，也不依赖 `.env` 文件**。所有容器级参数（端口、时区、基础镜像）都硬编码在 `docker-compose.yml` 里，需要修改时直接编辑该文件。

应用级配置（代理节点、容器更新策略、日志等）统一放在 `config/config.yaml`，首次启动自动生成，之后通过 Web 后台或直接编辑修改。

### 5. 数据持久化

- `config/`：`config.yaml` 与备份 `config.yaml.bak`
- `data/`：SQLite 数据库、日志、容器检测状态

升级或迁移时，保留 `config/` 与 `data/` 目录即可。

### 6. 构建时的网络与镜像源策略

Dockerfile 内置了「**先镜像源，失败回退官方源**」的多层策略，无需任何手动配置：

#### 基础镜像

- 通过 `docker-compose.yml` 的 `build.args.BASE_IMAGE` 控制
- **默认走 DaoCloud 公共代理**（`docker.m.daocloud.io/library/python:3.12.10-slim`）
- 可改为 1ms / 1Panel / 阿里云个人加速器 / 直连官方

#### apt 源

1. **默认切换阿里源**：`deb.debian.org` → `mirrors.aliyun.com`
2. **探测阿里源可达性**：5 秒 HTTP 探测
3. **不可达回滚官方源**：`mirrors.aliyun.com` → `deb.debian.org`

#### pip 源

1. **默认走阿里源**：`-i https://mirrors.aliyun.com/pypi/simple/`
2. **阿里源失败熔断**：写入标记文件，后续所有 pip 安装跳过阿里源
3. **官方 PyPI 回退**：走 `pypi.org`

**预期构建日志**（国内常见）：

```
>>> [apt] 使用阿里源
>>> [pip] 尝试阿里源
>>> [pip] 阿里源成功
>>> [pip] 尝试阿里源
>>> [pip] 阿里源成功
>>> [pip] 最终状态：使用阿里源
```

**预期构建日志**（阿里源不可用，罕见）：

```
>>> [apt] 阿里源不可达，回退官方 Debian 源
>>> [pip] 尝试阿里源
>>> [pip] 阿里源失败，切换官方 PyPI
>>> [pip] 已切换官方 PyPI
>>> [pip] 最终状态：使用官方 PyPI
```

### 7. 反向代理场景的客户端 IP

如果前面挂了 Nginx / Caddy，`request.client.host` 会变成反代 IP。若使用了 IP 白名单，请确保反代设置了：

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

本服务已识别 `X-Forwarded-For` 头。

### 8. 删除 `config.yaml` 会重新生成默认配置

`config/config.yaml` 若被删除，下次启动会重新生成**默认配置**（包含 `admin / change_me`）。

若你在该文件中做了大量自定义，**请先备份再删除**。

---

## 六、常见问题（FAQ）

### Q1：容器列表里没有我的容器？

- 确认已挂载 `/var/run/docker.sock` 且权限正确（可 `docker compose logs -f` 查看是否报 Docker 连接错误）
- 点击「刷新」按钮
- Docker-Assistant 自身的容器会被识别为「自身容器」，跳过检测（这是正常的）

### Q2：容器显示「有更新」，但点击更新失败？

常见原因：

- **私有镜像**：未配置 `updater.registry_username` / `updater.registry_password`
- **镜像源不通**：尝试关闭 `updater.pull_use_mirror`，直连 registry
- **容器名冲突**：重建过程中若出现同名容器残留，手动 `docker rm <name>` 后重试
- **查看详细错误**：切到「运行日志」标签，搜索容器名

### Q3：检测一直显示「等待镜像加速」，很久不动？

说明镜像加速尚未就绪（无可用节点）。可能原因：

- 上游 API 不通，节点未成功拉取
- 所有节点都在线检测中失败

处理方式：

- 在「加速节点」列表检查是否有可用节点
- 手动点击「获取免费节点」
- 若长期不可用，可在「配置文件 → 容器更新」中关闭 `wait_for_proxy_ready`，让容器检测与节点检测并行进行

### Q4：运行日志为空或很少？

- 检查「配置文件 → 日志」的 `logging.level`，默认 `INFO`
- 「运行日志」标签页共用同一日志缓冲，代理和容器更新日志都会出现
- 若刚重启，日志会从磁盘 `data/logs.jsonl` 恢复

### Q5：Docker 报 `http: server gave HTTP response to HTTPS client`？

代理默认是 HTTP，但 Docker 默认要求 HTTPS。三种解决方案（任选其一）：

1. **加 `insecure-registries`**（最简单）：

   ```json
   {
     "insecure-registries": ["192.168.1.100:8000"]
   }
   ```

2. **使用 `localhost`**：Docker 对 localhost 默认允许 HTTP，但仅限本机。

3. **配置 HTTPS 反向代理**（推荐生产）：见「三、3.5」。

### Q6：速度测试很慢，能不能关闭？

可以。在「配置文件 → 调度 → 速度测试」中关闭 `speed_test.enabled`。

关闭后：

- 节点排序将退化为「延迟升序」
- 拉取时仍会依次尝试所有可用节点，只是不再按速度优先

### Q7：如何完全关闭容器更新功能？

- 在「配置文件 → 容器更新」中关闭 `updater.enabled`
- 或编辑 `config.yaml`：`updater.enabled: false`
- 然后**重启服务**

容器更新相关的定时任务将不再注册，Web 后台的「容器列表 / 镜像管理」标签页仍可查看，但不会自动检测。

### Q8：升级到新版本会丢失配置吗？

不会。

- `config/config.yaml` 与 `data/` 在宿主机挂载，容器重建不受影响
- 数据库表结构变更通过 `upgrade_db()` 自动迁移
- 配置新增字段会使用默认值（除非显式修改）

升级步骤：

```bash
git pull                      # 或替换代码
docker compose build --no-cache
docker compose up -d
```

### Q9：为什么容器检测结果是「未检测」？

- 首次启动时，容器检测是后台任务，稍等片刻
- 若上次检测周期未到，会跳过；手动点「立即检查」可强制触发
- 若「容器更新」被禁用，则永远不会检测

### Q10：能同时加速多个 registry 吗？

可以。项目内置支持 Docker Hub / GHCR / GCR / Quay / MCR / Elastic / NVCR，拉取时使用如下格式：

```
docker pull <host>:8000/ghcr.io/owner/image:tag
docker pull <host>:8000/gcr.io/project/image:tag
docker pull <host>:8000/quay.io/org/image:tag
docker pull <host>:8000/mcr.microsoft.com/dotnet/runtime:tag
```

### Q11：局域网其他主机拉取很慢或超时？

- 确认本服务监听 `0.0.0.0`（`config.yaml` 中 `server.host`）
- 确认防火墙放行 8000 端口
- 确认客户端主机能 `curl http://<host>:8000/v2/ -I`
- 若跨网段，检查是否被路由/ACL 拦截

### Q12：反向代理后访问管理后台 401？

- 确认反代传了 `Authorization` 头（Nginx 默认会传）
- 若使用 Basic Auth，反代不要覆盖 `Authorization`
- 检查 `config.yaml` 的 `admin.user` / `admin.pass` 是否正确

### Q13：Docker Hub 的 `library/` 前缀怎么写？

代理与 Docker 官方一致，两种写法都支持：

```bash
docker pull <host>:8000/library/nginx:latest    # 显式
docker pull <host>:8000/nginx:latest            # 简写（自动补 library/）
```

### Q14：没有 `.env` 文件，怎么改端口 / 时区？

直接编辑 `docker-compose.yml`：

```yaml
services:
  docker-assistant:
    ports:
      - "9000:8000" # 改左侧即可
    environment:
      - TZ=Asia/Shanghai # 改这一行
```

保存后执行：

```bash
docker compose up -d
```

### Q15：构建时卡在拉基础镜像或 pip / apt？

按以下顺序排查：

1. **基础镜像拉不下来**（`FROM` 阶段报错）：
   - 检查 `docker-compose.yml` 的 `build.args.BASE_IMAGE` 是否可访问
   - 换一个代理，如 `docker.1ms.run/library/python:3.12.10-slim`
   - 若都不可用，改为 `python:3.12.10-slim` 直连 Docker Hub

2. **apt / pip 阶段卡住**：
   - Dockerfile 已内置「先阿里源，失败回滚官方」策略，理论上无需手动干预
   - 若阿里源本身不可用，会自动回滚官方源
   - 检查构建日志中的 `>>> [apt] ...` / `>>> [pip] ...` 输出

3. **宿主机 daemon.json 里的镜像加速器失效**（常见于免费第三方源）：
   - 编辑 `/etc/docker/daemon.json`，删除失效的加速器
   - 换为可用源，例如：

     ```json
     {
       "registry-mirrors": [
         "https://docker.1ms.run",
         "https://docker.m.daocloud.io"
       ]
     }
     ```

   - `sudo systemctl daemon-reload && sudo systemctl restart docker`

### Q16：`config.yaml` 删了会怎样？

下次启动会重新生成一份**默认配置**（`admin / change_me`）。如果你的自定义配置较多，**先备份再删除**。

---

## 七、授权协议

本项目基于 **GNU General Public License v3.0（GPL-3.0）** 发布。

```
Copyright (C) 2026 Docker-Assistant Contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```

- 协议全文：<https://www.gnu.org/licenses/gpl-3.0.html>
- 中文参考：<https://www.gnu.org/licenses/gpl-3.0.zh-cn.html>

**这意味着**：

- 你可以自由使用、修改、分发本软件
- 分发修改版时**必须开源**，并以相同协议授权
- 必须保留原作者版权声明
- 作者不对使用本软件产生的任何后果负责

---
