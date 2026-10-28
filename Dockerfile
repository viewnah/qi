# syntax=docker/dockerfile:1

# Ubuntu 24.04 自带 Python 3.12,满足 qi-lab 的 >=3.12 要求。
FROM ubuntu:24.04

# 让容器用户与宿主机用户对齐,避免 bind mount 的工作区文件被写成 root:
#   UID=$(id -u) GID=$(id -g) docker compose build
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/qilab

# 编码 agent 在容器里要跑 bash / git / grep;ripgrep 给 core 的搜索工具用。
# Python 装进独立 venv(/opt/venv),不碰 Ubuntu 的 externally-managed 系统环境。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        python3 \
        python3-venv \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$GID" qilab \
    && useradd --create-home --uid "$UID" --gid "$GID" --shell /bin/bash qilab \
    && mkdir -p /home/qilab/.qi \
    && chown -R qilab:qilab /home/qilab \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip

WORKDIR /app

# 只复制构建 wheel 必需的内容。内置扩展在 extensions/,前端产物已在 src/qi_lab/static/。
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY extensions ./extensions

RUN /opt/venv/bin/pip install . \
    && rm -rf /app \
    && mkdir -p /workspace \
    && chown -R qilab:qilab /workspace

USER qilab
WORKDIR /workspace

EXPOSE 3456

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD /opt/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3456/api/health', timeout=3)" || exit 1

ENTRYPOINT ["/opt/venv/bin/qi-lab"]
CMD ["--cwd", "/workspace", "--hostname", "0.0.0.0", "--no-open"]
