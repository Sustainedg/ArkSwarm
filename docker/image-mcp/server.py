"""图片生成 MCP 的独立 HTTP 容器入口。

生图逻辑仍然复用仓库里的 ``scripts/mcp/image_mcp_server.py``（stdio 版本），
这里只补三件事：

* 用 MCP SDK 的 Streamable HTTP 传输把同一个 ``generate_image`` 工具暴露成
  ``POST /mcp``，方便 Magpie 等网关集中登记；
* 用 ``GET /images/<文件名>`` 直接托管生成的图片，替代原来单独的 nginx 图床；
* 可选的 Bearer 鉴权与定时清理中转图片。

环境变量与 stdio 版本完全一致，另外增加：

* ``IMAGE_MCP_HOST`` / ``IMAGE_MCP_PORT``：监听地址，默认 ``0.0.0.0:8000``
* ``IMAGE_MCP_DATA_DIR``：工作目录，默认 ``/data``（图片落在 ``generated-images/``）
* ``IMAGE_MCP_TOKEN``：设置后 ``/mcp`` 需要 Bearer（``/images`` 与 ``/health`` 保持公开）
* ``IMAGE_MCP_ALLOWED_HOSTS``：逗号分隔的 Host 白名单（SDK 的 DNS rebinding 防护）
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from http_support import (
    bearer_matches,
    content_type_for,
    is_safe_image_name,
    parse_allowed_hosts,
    tool_arguments,
)

SERVER_NAME = "coda-image-gen-http"
SERVER_VERSION = "1.0.0"
SCRIPT = Path(os.environ.get("IMAGE_MCP_SCRIPT", "/app/image_mcp_server.py"))
DATA_DIR = Path(os.environ.get("IMAGE_MCP_DATA_DIR", "/data"))
IMAGE_DIR = DATA_DIR / "generated-images"
HOST = os.environ.get("IMAGE_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("IMAGE_MCP_PORT", "8000"))
TOKEN = (os.environ.get("IMAGE_MCP_TOKEN") or "").strip()
MCP_PATH = "/mcp"
PRUNE_INTERVAL_SECONDS = 300


def load_image_module() -> Any:
    """加载生图脚本本体；失败时直接退出，避免容器「起来了但不能生图」。"""
    if not SCRIPT.is_file():
        raise SystemExit(f"找不到生图脚本：{SCRIPT}")
    spec = importlib.util.spec_from_file_location("image_mcp_server", SCRIPT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载生图脚本：{SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


image_mcp = load_image_module()


def prune_forever() -> None:
    """后台定时清理过期中转图片；保留期为 0 时脚本自身什么也不做。"""
    while True:
        try:
            image_mcp.prune_expired_images()
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响生图
            print(f"清理过期图片失败：{exc!r}", file=sys.stderr)
        time.sleep(PRUNE_INTERVAL_SECONDS)


class BearerAuthMiddleware:
    """只保护 MCP 端点的纯 ASGI 中间件；不包 BaseHTTPMiddleware，避免影响 SSE。"""

    def __init__(self, app: Any, *, token: str, path: str) -> None:
        self.app = app
        self.token = token
        self.path = path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not (path == self.path or path.startswith(f"{self.path}/")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if not bearer_matches(headers, self.token):
            response = JSONResponse(
                {"error": "unauthorized", "message": "缺少或错误的 Bearer 令牌"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_server() -> FastMCP:
    allowed_hosts = parse_allowed_hosts(os.environ.get("IMAGE_MCP_ALLOWED_HOSTS", ""))
    # 内网独立服务：默认不拦 Host；显式配置白名单时打开 SDK 的 DNS rebinding 防护。
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
    )
    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Coda 图片生成 MCP：用 generate_image 生成或编辑图片。"
            "工具返回的图片地址可直接写进 Markdown 的 ![](...)。"
        ),
        host=HOST,
        port=PORT,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=False,
        log_level="INFO",
        transport_security=security,
    )

    @server.tool(name=image_mcp.TOOL_NAME, description=image_mcp.TOOL_DESCRIPTION)
    async def generate_image(
        prompt: Annotated[
            str,
            Field(description="要生成的图片内容描述；编辑图片时写清要改成什么样（必填）"),
        ],
        image: Annotated[
            str | None,
            Field(
                description=(
                    "要编辑的参考图：工作目录内的相对路径（如 attachments/a.png、"
                    "generated-images/b.png）或 http(s) 图片地址。提供后走图片编辑（图生图），"
                    "不提供就是文生图"
                )
            ),
        ] = None,
        images: Annotated[
            list[str] | None,
            Field(
                description=(
                    f"多张参考图（最多 {image_mcp.MAX_INPUT_IMAGES} 张），与 image 可同时使用；"
                    "同样支持工作目录相对路径或 http(s) 地址"
                )
            ),
        ] = None,
        size: Annotated[
            str | None,
            Field(description="图片尺寸，格式为 宽x高（如 1024x1024）；不填由模型决定"),
        ] = None,
        n: Annotated[
            int,
            Field(description="一次生成几张（1-4，默认 1）", ge=1, le=4),
        ] = 1,
    ) -> str:
        arguments = tool_arguments(prompt, image, images, size, n)
        try:
            return await anyio.to_thread.run_sync(
                lambda: image_mcp.generate_image(arguments, image_mcp.load_config())
            )
        except (image_mcp.ConfigError, image_mcp.GatewayError, ValueError) as exc:
            raise ToolError(f"生成图片失败：{exc}") from exc

    return server


async def health(_: Request) -> Response:
    """健康检查：给容器 HEALTHCHECK 与运维用，不暴露密钥。"""
    return JSONResponse(
        {
            "status": "ok",
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "tool": image_mcp.TOOL_NAME,
            "data_dir": str(DATA_DIR),
            "image_dir": str(IMAGE_DIR),
            "retention_minutes": image_mcp.retention_minutes(),
            "auth": bool(TOKEN),
        }
    )


async def serve_image(request: Request) -> Response:
    """只提供本服务生成的图片；目录穿越与非 image-* 文件名一律 404。"""
    name = request.path_params.get("name", "")
    if not is_safe_image_name(name):
        return Response("not found", status_code=404, media_type="text/plain")
    path = (IMAGE_DIR / name).resolve()
    if path.parent != IMAGE_DIR.resolve() or not path.is_file():
        return Response("not found", status_code=404, media_type="text/plain")
    return FileResponse(
        path,
        media_type=content_type_for(name),
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def build_app(server: FastMCP) -> Starlette:
    """把 MCP 端点与图片/健康检查拼成一个 ASGI 应用。

    ``FastMCP.custom_route`` 只接受静态路径，图片名需要路径参数，因此这里用外层
    Starlette 路由挂载 MCP 子应用；lifespan 沿用子应用的，保证会话管理器能启动。
    """
    mcp_app = server.streamable_http_app()
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/images/{name:path}", serve_image, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.router.lifespan_context,
    )



def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(DATA_DIR)
    # 启动时先清一次：容器重启后不会把上一次的过期图片继续暴露出去。
    try:
        image_mcp.prune_expired_images()
    except Exception as exc:  # noqa: BLE001 - 同上，清理不是启动前提
        print(f"启动清理失败：{exc!r}", file=sys.stderr)
    threading.Thread(target=prune_forever, name="image-prune", daemon=True).start()

    server = build_server()
    app = build_app(server)
    if TOKEN:
        app = BearerAuthMiddleware(app, token=TOKEN, path=MCP_PATH)
    print(
        f"{SERVER_NAME} 监听 {HOST}:{PORT}{MCP_PATH}，图片目录 {IMAGE_DIR}，"
        f"鉴权 {'开' if TOKEN else '关'}",
        file=sys.stderr,
    )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
