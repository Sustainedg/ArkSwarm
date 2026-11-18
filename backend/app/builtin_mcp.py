"""平台内置 MCP 服务器的声明。

与「从内核配置扫描出来的服务器」不同，内置服务器是 ArkSwarm **随包分发**的一份预设：
启动时由 runtime 自动为每个用户播种成一条可编辑的平台通用服务器。用户不用自己找
脚本、拼启动命令，只需要在「MCP」页填写业务参数（网关地址、模型名、密钥…）。

这里只放声明，不碰数据库 / HTTP / 运行时；落库、分配与物化在
:mod:`app.runtime.mcp`。新增一个内置服务器只需要：

1. 在 :data:`BUILTIN_SERVERS` 里加一条 :class:`BuiltinServer`；
2. 确保它的 ``script`` 随 Release 一起打包（见 ``scripts/release.sh`` 与
   ``docker/Dockerfile``）。

「网关地址 / 模型名」这类会随部署变化的配置一律声明成 :class:`BuiltinParam`，
参数值由用户填写、经 :mod:`app.security` 加密保存，**不写死任何模型**。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 仓库 / 版本目录的根：本文件位于 ``<root>/backend/app/builtin_mcp.py``。
#: 本地开发是仓库根；Docker 里是 ``/app/releases/<版本>``（或镜像自带的 ``/image-app``）。
_ROOT = Path(__file__).resolve().parent.parent.parent

#: 内置生图服务器的脚本。release.sh / Dockerfile 负责把它一起打包，
#: 否则升级后的版本目录里找不到脚本。
IMAGE_GEN_SCRIPT = _ROOT / "scripts" / "mcp" / "image_mcp_server.py"


@dataclass(frozen=True, slots=True)
class BuiltinParam:
    """内置服务器的一个业务参数（最终以环境变量注入 stdio 进程）。"""

    key: str
    label: str
    help: str = ""
    placeholder: str = ""
    required: bool = False
    secret: bool = False
    #: 初始值；默认空串 ＝ 不预设（网关 / 模型必须由用户按自己的部署填写）。
    default: str = ""


@dataclass(frozen=True, slots=True)
class BuiltinServer:
    """一个内置 MCP 服务器的声明。

    连接方式（启动命令 / 脚本参数）由平台维护；用户只填 ``params``。
    """

    key: str
    name: str
    slug: str
    description: str
    script: Path
    params: tuple[BuiltinParam, ...] = field(default_factory=tuple)

    @property
    def command(self) -> str:
        """启动命令：用当前解释器，避免依赖 PATH 里有没有 ``python3``。"""
        return sys.executable or "python3"

    @property
    def args(self) -> tuple[str, ...]:
        return (str(self.script),)

    def env_defaults(self) -> dict[str, str]:
        """参数的初始值。默认都为空：网关 / 模型不写死，由用户填写。"""
        return {param.key: param.default for param in self.params}

    def params_view(self, masked: dict[str, str | None]) -> list[dict[str, object]]:
        """给界面的参数说明；``masked`` 是已保存值的掩码，不含明文。"""
        return [
            {
                "key": param.key,
                "label": param.label,
                "help": param.help,
                "placeholder": param.placeholder,
                "required": param.required,
                "secret": param.secret,
                "default": param.default,
                "masked": masked.get(param.key),
            }
            for param in self.params
        ]

    def missing_required(self, env: dict[str, str]) -> list[str]:
        """返回还没填的必填参数标签（用于运行前的提示）。"""
        return [
            param.label
            for param in self.params
            if param.required and not (env.get(param.key) or "").strip()
        ]


IMAGE_GEN = BuiltinServer(
    key="image-gen",
    name="图片生成",
    slug="image-gen",
    description=(
        "平台内置的生图工具：调用 OpenAI 兼容网关的 /v1/images/generations 生成图片，"
        "带参考图时走 /v1/images/edits 编辑图片；结果保存到对话工作目录的 generated-images/。"
        "分配前请在「参数」里填写网关地址与模型名。"
    ),
    script=IMAGE_GEN_SCRIPT,
    params=(
        BuiltinParam(
            key="IMAGE_GEN_GATEWAY",
            label="网关地址",
            help="OpenAI 兼容的生图网关；带不带 /v1 都可以（会自动归一化）。",
            placeholder="https://api.example.com",
            required=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_MODEL",
            label="模型名称",
            help="网关上的生图模型名，例如 flux-dev / gpt-image-1；不写死，按你的网关填写。",
            placeholder="flux-dev",
            required=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_API_KEY",
            label="网关密钥",
            help="需要鉴权的网关才填；无鉴权的本地网关可以留空。",
            secret=True,
        ),
        BuiltinParam(
            key="IMAGE_GEN_TIMEOUT",
            label="超时秒数",
            help="单次生图最长等待时间；留空按 300 秒。",
            placeholder="300",
        ),
    ),
)

#: 当前内置的全部服务器；新增内置服务器只需要在这里加一条。
BUILTIN_SERVERS: tuple[BuiltinServer, ...] = (IMAGE_GEN,)

_BY_KEY = {server.key: server for server in BUILTIN_SERVERS}


def builtin_by_key(key: str | None) -> BuiltinServer | None:
    """按稳定标识取内置声明；普通服务器（``None``）返回 ``None``。"""
    return _BY_KEY.get(key or "")


__all__ = [
    "BUILTIN_SERVERS",
    "BuiltinParam",
    "BuiltinServer",
    "IMAGE_GEN",
    "IMAGE_GEN_SCRIPT",
    "builtin_by_key",
]