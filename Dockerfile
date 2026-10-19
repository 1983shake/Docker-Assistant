# ============================================================
# 基础镜像：默认走国内公共代理（DaoCloud）
# 可通过 docker-compose.yml 的 build.args.BASE_IMAGE 覆盖
# ============================================================
ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.12.10-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=15 \
    PIP_RETRIES=1 \
    ALIYUN_PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    ALIYUN_PIP_HOST=mirrors.aliyun.com

# ============================================================
# 第一步：apt 安装基础工具
#
# 策略：先阿里源 → 探测不可达 → 回退官方 Debian 源
# ============================================================
RUN set -eux; \
    # 1) 默认把 Debian 源换成阿里云
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    \
    # 2) 探测阿里源是否可达；不可达则回滚为官方源
    if python -c "import urllib.request; urllib.request.urlopen('http://mirrors.aliyun.com/debian/dists/bookworm/Release', timeout=5).read(1)" 2>/dev/null; then \
    echo ">>> [apt] 使用阿里源"; \
    else \
    echo ">>> [apt] 阿里源不可达，回退官方 Debian 源"; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i 's|mirrors.aliyun.com|deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
    sed -i 's|mirrors.aliyun.com|deb.debian.org|g' /etc/apt/sources.list; \
    fi; \
    fi; \
    \
    # 3) 安装
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata curl ca-certificates; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# ============================================================
# 第二步：pip 安装依赖
#
# 策略：先阿里源 → 失败熔断 → 回退官方 PyPI
#
# 用标记文件在同一 RUN 层内共享熔断状态：
# 第一次阿里源失败后，后续所有安装都直接走官方 PyPI。
# ============================================================
RUN set -eux; \
    CB=/tmp/.pip_fallback; \
    rm -f "$CB"; \
    \
    pip_install() { \
    if [ ! -f "$CB" ]; then \
    echo ">>> [pip] 尝试阿里源"; \
    if pip install \
    -i "$ALIYUN_PIP_INDEX" \
    --trusted-host "$ALIYUN_PIP_HOST" \
    --timeout 30 --retries 2 "$@"; then \
    echo ">>> [pip] 阿里源成功"; \
    return 0; \
    fi; \
    echo ">>> [pip] 阿里源失败，切换官方 PyPI"; \
    touch "$CB"; \
    else \
    echo ">>> [pip] 已切换官方 PyPI"; \
    fi; \
    pip install --timeout 30 --retries 3 "$@"; \
    }; \
    \
    pip_install --upgrade pip setuptools wheel; \
    pip_install -r requirements.txt; \
    \
    if [ -f "$CB" ]; then \
    echo ">>> [pip] 最终状态：使用官方 PyPI"; \
    else \
    echo ">>> [pip] 最终状态：使用阿里源"; \
    fi

COPY app ./app

RUN mkdir -p /app/config /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/system/health || exit 1

CMD ["python", "-m", "app.main"]