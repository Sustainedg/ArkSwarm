# ArkSwarm 镜像：只封装「环境」（python + 系统工具），应用代码放 /app 卷由更新器管理。
#
# 版本级更新永远不需要重建镜像：界面「设置 → 关于 → 检测更新」下载 Release 落进
# /app/releases 并 os.execv 原地重启（uvicorn 是 PID 1，容器不退出）。
# 只有换 python 大版本、加系统依赖时才需要重建本镜像。
#
# 构建：docker build -t arkswarm -f docker/Dockerfile .
# 运行：docker run -d -p 8010:8010 -v arkswarm-app:/app -v arkswarm-data:/app/data arkswarm
FROM python:3.11-slim

# curl：entrypoint 首启下载 Release 用；git：智能体运行时装内核（npm 等）可能要用
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 应用代码卷（版本目录 + venv 都在这里，容器重建不丢）；数据在 /app/data 子卷
VOLUME ["/app"]

ENV ARKSWARM_HOST=0.0.0.0 \
    ARKSWARM_PORT=8010 \
    ARKSWARM_DATA_DIR=/app/data \
    HOME=/root

EXPOSE 8010
ENTRYPOINT ["/entrypoint.sh"]
