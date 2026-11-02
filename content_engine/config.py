"""Application settings; importing this module does not create files."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent
MIN_RESEARCH_SOURCES = 5
MAX_RESEARCH_SOURCES = 8


@dataclass(frozen=True)
class Settings:
    """Application paths and provider settings; credentials stay out of repr."""

    data_dir: Path = PROJECT_DIR / "data"
    max_revisions: int = 2
    nebius_api_key: str = field(default="", repr=False)
    keenable_api_key: str = field(default="", repr=False)
    nebius_model: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    nebius_max_tokens: int = 6000
    nebius_timeout_seconds: int = 300
    max_research_passes: int = 2
    max_human_revisions: int = 5
    node_binary: str = "node"

    def __post_init__(self) -> None:
        if self.max_revisions < 0:
            raise ValueError("max_revisions must be zero or greater")
        if self.max_research_passes < 1 or self.max_human_revisions < 1:
            raise ValueError("Research and human revision budgets must be positive")
        if self.nebius_max_tokens <= 0:
            raise ValueError("NEBIUS_MAX_TOKENS must be greater than zero")
        if self.nebius_timeout_seconds <= 0:
            raise ValueError("NEBIUS_TIMEOUT_SECONDS must be greater than zero")

    @classmethod
    def from_env(cls) -> "Settings":
        """Read this project's .env; existing environment variables take priority."""
        load_dotenv(PROJECT_DIR / ".env", override=False)
        return cls(
            nebius_api_key=os.getenv("NEBIUS_API_KEY", "").strip(),
            keenable_api_key=os.getenv("KEENABLE_API_KEY", "").strip(),
            nebius_model=os.getenv("NEBIUS_MODEL", "").strip(),
            nebius_base_url=os.getenv("NEBIUS_BASE_URL", cls.nebius_base_url).strip(),
            nebius_max_tokens=int(os.getenv("NEBIUS_MAX_TOKENS", "6000")),
            nebius_timeout_seconds=int(os.getenv("NEBIUS_TIMEOUT_SECONDS", "300")),
            node_binary=os.getenv("NODE_BINARY", "node").strip(),
        )

    def require_live_credentials(self) -> None:
        required = {
            "NEBIUS_API_KEY": self.nebius_api_key,
            "NEBIUS_MODEL": self.nebius_model,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Set {', '.join(missing)} in {PROJECT_DIR / '.env'} or your environment")

    @property
    def articles_db(self) -> Path:
        return self.data_dir / "articles.sqlite3"

    @property
    def checkpoints_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite3"

    @property
    def metrics_db(self) -> Path:
        return self.data_dir / "evaluations.sqlite3"
