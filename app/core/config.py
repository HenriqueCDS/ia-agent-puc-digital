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
    # Opcional: só evita o aviso de "unauthenticated requests" e dá rate limit
    # maior no download do modelo de embeddings. Sem token, tudo funciona igual.
    hf_token: str = ""
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agente_ead"

    # Local (HuggingFace/sentence-transformers) para não depender de cota de API.
    # Multilingual porque o conteúdo é majoritariamente em português.
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    chat_model: str = "gemini-3.6-flash"

    chunk_size: int = 1000
    chunk_overlap: int = 150

    top_k: int = 5
    relevance_threshold: float = 0.35

    # Acima disso, as 2 fontes do topo são tratadas como alta confiança (ver
    # retrieval/retriever.is_exact_match): a base tem muita informação repetida,
    # então 2 fontes fortes concordando já é sinal de resposta certa.
    exact_match_threshold: float = 0.90

    # Nome da coleção no pgvector. Trocar isola um índice novo do antigo.
    # Sufixo "_hf" porque a dimensão do vetor mudou ao trocar Gemini por embeddings
    # locais (HuggingFace) — misturar as duas na mesma coleção quebraria a busca.
    collection_name: str = "base_conhecimento_hf"

    # Desliga o cache de resposta (ver agent/responder._cache_key). Útil ao
    # iterar em prompts.py: evita servir uma resposta antiga enquanto se ajusta
    # o prompt para o mesmo conjunto de chunks.
    cache_enabled: bool = True


settings = Settings()
