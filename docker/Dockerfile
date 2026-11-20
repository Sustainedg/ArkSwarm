# ArkSwarm 镜像：封装 Python / Node / git 环境，并内置一份当前代码 + venv 兜底；
# 应用版本仍放 /app 卷由更新器管理。
#
# 版本级更新永远不需要重建镜像：界面「设置 → 关于 → 检测更新」下载 Release 落进
# /app/releases 并 os.execv 原地重启（uvicorn 是 PID 1，容器不退出）。
# 只有换 python 大版本、加系统依赖时才需要重建本镜像。
#
# 构建：docker build -t arkswarm -f docker/Dockerfile .
# 运行：见 docker/docker-compose.yml（或手动挂载 /app 与 /app/data 两个卷）。
# 内核安装态、用户配置与会话都在 HOME 下；这里把 HOME 指进数据卷，
# 让 Claude Code / Codex / Hermes / Gemini 的安装与 ~/.gemini 等不再随容器重建丢失。

# ---- 前端构建阶段 ----
# frontend/dist 被 .gitignore 忽略；用源码在镜像里现构建，全新 clone 也能直接
# docker compose up --build，不依赖本地先跑一次 npm run build。
FROM node:24-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 运行阶段 ----
FROM python:3.11-slim

# curl：entrypoint 首启下载 Release 与 Node.js；git：Hermes / 部分内核的安装脚本要用；
# ca-certificates：上面两者走 HTTPS 都要。
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates git \
  && rm -rf /var/lib/apt/lists/*

# Node.js 24 LTS：DeepSeek Harness 要求 Node >= 24.9，Claude Code 要求 >= 22，
# Gemini CLI >= 20，Codex >= 16。用官方二进制包而不是 NodeSource：少一个外部 apt 源，
# 也便于按构建架构取对应包。
# 内核（Claude Code / Codex / Gemini / Hermes / DeepSeek Harness）由运行时的 entrypoint 或
# 「设置 → 内核管理」安装到 HOME（数据卷），不再烘进镜像。
ARG TARGETARCH
RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) node_arch="x64" ;; \
      arm64) node_arch="arm64" ;; \
      *) echo "不支持的架构：$arch" >&2; exit 1 ;; \
    esac; \
    node_version="v24.22.3"; \
    tarball="node-${node_version}-linux-${node_arch}.tar.gz"; \
    curl -fsSLo "/tmp/${tarball}" "https://nodejs.org/dist/${node_version}/${tarball}"; \
    curl -fsSLo /tmp/SHASUMS256.txt "https://nodejs.org/dist/${node_version}/SHASUMS256.txt"; \
    grep " ${tarball}\$" /tmp/SHASUMS256.txt | (cd /tmp && sha256sum -c -); \
    tar -xzf "/tmp/${tarball}" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "/tmp/${tarball}" /tmp/SHASUMS256.txt; \
    node --version; \
    npm --version

# 应用代码：**构建时就打进去**，空卷首启直接用它跑（「构建什么就运行什么」）。
# 之后版本升级统一走界面「设置 → 关于」的更新机制：下载 Release → releases/<版本>/
# → 原子切 current → execv 原地重启，不需要重建镜像。
# 设 ARKSWARM_FETCH_RELEASE_ON_EMPTY=1 可恢复「空卷先联网拉最新 Release」的旧行为。
COPY backend/app /image-app/backend/app
COPY backend/requirements.txt /image-app/backend/requirements.txt
COPY backend/.env.example /image-app/backend/.env.example
# 前端产物：由上面的 frontend 构建阶段现构建（不再依赖仓库里的 frontend/dist）。
COPY --from=frontend /src/dist /image-app/frontend/dist
COPY scripts/serve.sh /image-app/scripts/serve.sh
# 平台自带的「平台通用」MCP 服务器（图片生成 / 系统管理），分配给智能体即可使用。
COPY scripts/mcp /image-app/scripts/mcp

# 预装 Python 依赖到 /image-venv：首启直接复制，离线也能启动。
# 构建期也允许走代理 / 国内镜像；只影响构建，不进入运行期环境变量。
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
RUN python3 -m venv /image-venv \
  && /image-venv/bin/pip -q install -U pip \
  && /image-venv/bin/pip -q install -r /image-app/backend/requirements.txt \
  && sha256sum /image-app/backend/requirements.txt | cut -d' ' -f1 > /image-venv/.requirements-stamp

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 应用代码卷（版本目录 + venv 都在这里，容器重建不丢）；数据在 /app/data 子卷。
# HOME 也落在数据卷里：npm 全局包（NPM_CONFIG_PREFIX）与各内核的 ~/.xxx
# 因此都会写进 /app/data/home，容器重建后仍可继续使用，不必重新安装 / 登录。
VOLUME ["/app"]

ENV ARKSWARM_HOST=0.0.0.0 \
    ARKSWARM_PORT=8010 \
    ARKSWARM_DATA_DIR=/app/data \
    HOME=/app/data/home \
    NPM_CONFIG_PREFIX=/app/data/home/.local \
    PATH=/app/data/home/.local/bin:$PATH

EXPOSE 8010

# 健康检查：容器内探 /api/health，端口跟随 ARKSWARM_PORT（默认 8010）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${ARKSWARM_PORT:-8010}/api/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
