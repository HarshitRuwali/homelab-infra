from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # PostgreSQL
    postgres_user: str
    postgres_password: str
    postgres_host: str
    postgres_port: int
    postgres_db: str

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Qdrant
    qdrant_host: str
    qdrant_port: int
    qdrant_collection: str
    vector_dim: int

    # LLM and embedding services
    ai_vm_host: str
    llm_port: int
    embed_port: int

    @property
    def llm_url(self) -> str:
        return f"http://{self.ai_vm_host}:{self.llm_port}"

    @property
    def embed_url(self) -> str:
        return f"http://{self.ai_vm_host}:{self.embed_port}"

    # Redis
    redis_host: str
    redis_port: int

    # App
    app_host: str
    app_port: int
    log_level: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
