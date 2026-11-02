from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ai-support-platform"
    debug: bool = False
    cors_origins: str = "http://localhost:5173"

    database_url: str = "postgresql+psycopg://support:support@localhost:5432/support"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 60  # sliding: the web app calls /auth/refresh while open
    bcrypt_rounds: int = 12  # tests lower this; never below 12 in real deployments

    llm_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_model: str = "gemini-2.5-flash"
    llm_timeout: float = 60.0
    embedding_model: str = "gemini-embedding-001"
    # Must equal the chunks.embedding column size (1536).
    embedding_dimensions: int = 1536

    # Rate limits (requests per minute); protect the LLM quota
    chat_rate_per_user: int = 20
    chat_rate_per_tenant: int = 120
    ingest_rate_per_user: int = 30
    ingest_rate_per_tenant: int = 120
    retrieval_test_rate_per_user: int = 30
    login_attempts_per_email: int = 10  # per 5 minutes
    login_attempts_per_ip: int = 40  # per 5 minutes

    api_rate_per_tenant: int = 600  # public API requests per minute for a whole workspace

    # Outbound connections to company systems
    secrets_key: str = ""  # Fernet key(s), comma separated; the first encrypts. Set in production.
    allow_private_connections: bool = False  # dev only: lets connections target localhost/private IPs
    connection_allowed_ports: str = "443"
    enable_demo_company_api: bool = False  # mounts a fake company API at /demo-company for the demo

    # Background work: "embedded" = a worker thread inside the API (dev default), "external" = run
    # `python -m app.worker` separately (production), "sync" = run jobs immediately (tests).
    jobs_mode: Literal["embedded", "external", "sync"] = "embedded"
    # Behind a trusted reverse proxy, read the caller address from X-Forwarded-For (API key IP allow-lists).
    trust_proxy_headers: bool = False
    # Tokens all agents of a workspace may use per UTC day; 0 = no workspace-wide ceiling.
    tenant_daily_token_budget: int = 0
    approvals_ttl_hours: int = 72
    tool_result_ttl_minutes: int = 15

    # Document intake limits
    max_upload_bytes: int = 10 * 1024 * 1024
    max_documents_per_tenant: int = 200
    max_pasted_chars: int = 500_000

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
