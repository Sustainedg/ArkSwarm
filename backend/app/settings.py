"""应用配置：从环境变量 / .env 读取，并提供可用的本地默认值。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGENTA_", env_file=".env", extra="ignore")

    app_name: str = "Magenta Agents"
    data_dir: Path = BACKEND_ROOT / "data"
    database_url: str | None = None
    secret_key: str | None = None
    workspace_dir: Path | None = None
    cors_origins: str = "*"
    run_timeout_seconds: int = 3600
    #: 安装/更新/卸载智能体运行时（CLI 内核）的单次上限；比一次对话更宽松。
    provision_timeout_seconds: int = 900
    default_agent_type: str = "claude-code"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'magenta.db'}"

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

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.agent_homes_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
