"""应用配置：从环境变量 / .env 读取，并提供可用的本地默认值。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARKSWARM_", env_file=".env", extra="ignore")

    app_name: str = "ArkSwarm"
    data_dir: Path = BACKEND_ROOT / "data"
    database_url: str | None = None
    secret_key: str | None = None
    workspace_dir: Path | None = None
    cors_origins: str = "*"
    #: 一次运行的默认时限（秒）：协作域经团队预算的 step_timeout_seconds 覆盖（0 = 不限），
    #: 对话域沿用这里的全局默认。
    run_timeout_seconds: int = 3600
    #: 安装/更新/卸载智能体运行时（CLI 内核）的单次上限；比一次对话更宽松。
    provision_timeout_seconds: int = 900
    #: 网络出口过滤模式：off（不限制）/ block（全部阻断）/ allowlist（只放行白名单）
    egress_mode: str = "off"
    #: 出口白名单（逗号分隔域名），allowlist 模式下生效；网关地址运行时自动加入
    egress_allowed_hosts: str = ""
    #: 自更新来源仓库（owner/repo）；默认官方仓库，私有部署可指到自己的 fork
    update_repo: str | None = None
    #: GitHub API 基址；GitHub Enterprise 部署可换自己的域名
    update_github_api: str | None = None
    #: 需要取回对话工作区的图片 URL 前缀（逗号分隔），例如 MCP 网关图床
    #: ``http://192.168.2.168:3050``。命中前缀的图片会在助手消息落库前下载到
    #: ``generated-images/`` 并改写为相对路径，让对话历史自包含。
    image_ingest_prefixes: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'arkswarm.db'}"

    @property
    def workspaces_root(self) -> Path:
        return (self.workspace_dir or (self.data_dir / "workspaces")).resolve()

    @property
    def agent_homes_root(self) -> Path:
        """每个对话一份的内核隔离配置目录（技能「严格仅分配」就物化在这里）。

        放在数据目录里而不是临时目录：内核的会话记录也写在自己的配置目录下，
        每次运行都换目录会导致多轮对话无法续接。
        """
        return (self.data_dir / "agent-homes").resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def egress_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.egress_allowed_hosts.split(",") if h.strip()]

    @property
    def image_ingest_prefix_list(self) -> list[str]:
        return [p.strip() for p in self.image_ingest_prefixes.split(",") if p.strip()]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.agent_homes_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
