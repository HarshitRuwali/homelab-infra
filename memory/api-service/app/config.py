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
    # Empty: llama.cpp's native POST /embedding. Set: OpenAI-style
    # POST /v1/embeddings for that model id (LM Studio, vLLM, llama-server).
    embed_model: str = ""

    @property
    def llm_url(self) -> str:
        return f"http://{self.ai_vm_host}:{self.llm_port}"

    @property
    def embed_url(self) -> str:
        return f"http://{self.ai_vm_host}:{self.embed_port}"

    def embed_request(self, text: str | list[str]) -> tuple[str, dict]:
        """URL and JSON body for one embedding call, in the configured API shape.

        ``text`` may be a list: both shapes embed one vector per entry.
        """
        if self.embed_model:
            return f"{self.embed_url}/v1/embeddings", {"model": self.embed_model, "input": text}
        return f"{self.embed_url}/embedding", {"content": text}

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
