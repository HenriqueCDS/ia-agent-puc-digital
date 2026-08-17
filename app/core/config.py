"""Configuração central, carregada do .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    google_api_key: str = ""
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agente_ead"

    embedding_model: str = "models/gemini-embedding-001"
    chat_model: str = "gemini-3.6-flash"

    chunk_size: int = 1000
    chunk_overlap: int = 150

    top_k: int = 5
    relevance_threshold: float = 0.35

    # Nome da coleção no pgvector. Trocar isola um índice novo do antigo.
    collection_name: str = "base_conhecimento"


settings = Settings()
