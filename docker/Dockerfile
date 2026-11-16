# ArkSwarm 镜像：封装「环境 + 自带内核」，应用代码放 /app 卷由更新器管理。
#
# 版本级更新永远不需要重建镜像：界面「设置 → 关于 → 检测更新」下载 Release 落进
# /app/releases 并 os.execv 原地重启（uvicorn 是 PID 1，容器不退出）。
# 只有换 python / node 大版本、加系统依赖时才需要重建本镜像。
#
# 构建：docker build -t arkswarm -f docker/Dockerfile .
# 运行：docker run -d -p 8010:8010 -v arkswarm-app:/app -v arkswarm-data:/app/data arkswarm
#       或：docker compose up -d（见 docker/docker-compose.yml）

# ---- 前端构建阶段 ----
# 仓库不保存 frontend/dist（被 .gitignore 忽略），用源码在镜像里现构建，
# 「clone 下来就能 compose up」；也避免兜底 UI 落后于 backend 代码。
FROM node:20-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 运行阶段 ----
FROM python:3.11-slim

# 系统依赖：
#   curl ca-certificates gnupg  entrypoint 首启下载 Release、Hermes 安装脚本、NodeSource 源
#   git                       智能体运行时装内核 / clone 仓库可能要用
# Node.js 20 LTS（NodeSource）：Claude Code / Codex / Gemini CLI 都是 npm 全局包，
# Debian 自带的 nodejs 版本太老，用官方源装 Node 20 LTS 保证内核能装能跑。
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates gnupg git \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# 自带内核：镜像里预装三个 npm 全局 CLI + Hermes，容器起来就能用，不必在界面里再点「安装」。
# 装在镜像的 npm 全局目录，不占 /app 卷、不受版本更新影响。
# 用户若在界面里卸载某个内核，只是删掉卷里的链接；重启后 entrypoint 会补回来。
# 版本刻意跟随 latest（与界面里的「安装 / 更新」一致），重建镜像即刷新内核。
RUN npm install -g \
      @anthropic-ai/claude-code \
      @openai/codex \
      @google/gemini-cli \
  || echo "[build] npm 内核预装部分失败，容器启动时 entrypoint 会重试"

# Hermes：curl 脚本安装（不是 npm 包），装到 ~/.local/share/hermes-agent + ~/.local/bin/hermes。
# 它的安装脚本要 clone 仓库 + 下 uv + 下 Python + 装依赖，网络不稳时容易卡住，
# 所以加 5 分钟超时：超时就跳过，容器启动时 entrypoint 会再试一次（或界面里手动装）。
RUN timeout 300 bash -c 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash' \
  && command -v hermes >/dev/null 2>&1 \
  && echo "[build] Hermes 预装成功" \
  || echo "[build] Hermes 预装失败或超时，容器启动时 entrypoint 会重试"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 镜像自带一份当前代码作离线兜底：entrypoint 在 GitHub 下载失败（没发 Release / 离线 / 私有）
# 时把这份复制进 /app/releases/<version>/，让容器无论有没有 Release 都能起来。
# 有 Release 时优先用 Release（支持界面点击更新）；这份只是首启兜底，不参与版本更新。
COPY backend/app        /image-app/backend/app
COPY backend/requirements.txt /image-app/backend/requirements.txt
COPY backend/.env.example     /image-app/backend/.env.example
COPY --from=frontend /src/dist /image-app/frontend/dist
COPY scripts/serve.sh  /image-app/scripts/serve.sh

# 预装 Python 依赖到镜像的一个 venv 模板：entrypoint 首启直接复制它，避免容器里
# 再联网 pip install（离线 / PyPI 慢 / 需代理时也能起来）。requirements 变了重建镜像即可。
RUN python3 -m venv /image-venv \
  && /image-venv/bin/pip -q install -U pip \
  && /image-venv/bin/pip -q install -r /image-app/backend/requirements.txt \
  && cat /image-app/backend/requirements.txt | sha256sum | cut -d' ' -f1 > /image-venv/.requirements-stamp

# 应用代码卷（版本目录 + venv 都在这里，容器重建不丢）；数据在 /app/data 子卷
VOLUME ["/app"]

ENV ARKSWARM_HOST=0.0.0.0 \
    ARKSWARM_PORT=8010 \
    ARKSWARM_DATA_DIR=/app/data \
    HOME=/root

EXPOSE 8010
ENTRYPOINT ["/entrypoint.sh"]
