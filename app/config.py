"""Environment-driven settings (pydantic-settings).

All secrets/config come from the environment / `.env`. Nothing here is hardcoded.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Integrations ──────────────────────────────────────────
    anthropic_api_key: str = ""          # not used in Phase 0 (captions are Phase 1)
    twelvelabs_api_key: str = ""

    # ── Cloudflare R2 (S3-compatible) ─────────────────────────
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "trial-studio"
    r2_endpoint: str = ""

    # ── Infra ─────────────────────────────────────────────────
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # ── App knobs ─────────────────────────────────────────────
    twelvelabs_index_name: str = "trial-studio"
    tl_marengo_model: str = "marengo3.0"          # index embedding model (2.7 retired)
    tl_pegasus_model: str = "pegasus1.2"          # generative model for summary/tags
    enable_marengo_embedding: bool = True         # store the per-clip Marengo vector (extra cost)
    work_dir: str = "/tmp/trial-studio"          # transient per-clip working space
    min_resolution: int = 1080                   # QC: reject if min(w,h) < this
    min_fps: float = 29.9                         # QC: reject if fps < this

    @property
    def sqlalchemy_url(self) -> str:
        """Normalize a bare Postgres URL (e.g. Railway's) to the psycopg driver."""
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
