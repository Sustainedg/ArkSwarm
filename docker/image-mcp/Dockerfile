# 独立图片生成 MCP：把 scripts/mcp/image_mcp_server.py 以 Streamable HTTP 暴露。
# 构建上下文必须是仓库根目录：
#   docker build -f docker/image-mcp/Dockerfile -t arkswarm-image-mcp .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY docker/image-mcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
      --index-url https://mirrors.aliyun.com/pypi/simple/ \
      -r /app/requirements.txt

COPY docker/image-mcp/http_support.py /app/http_support.py
COPY docker/image-mcp/server.py /app/server.py
COPY scripts/mcp/image_mcp_server.py /app/image_mcp_server.py

RUN mkdir -p /data/generated-images

WORKDIR /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "/app/server.py"]
