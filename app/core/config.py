"""Configuração central, carregada do .env."""

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"


@dataclass(frozen=True)
class FonteWeb:
    """Entrada da allowlist da busca externa (ver `app/agent/web_fallback.py`).

    `termos` são palavras injetadas na query do DuckDuckGo para direcionar o
    recall; `host`/`path_prefix` são o que de fato *restringe*: toda URL
    devolvida é revalidada contra eles antes de chegar ao LLM.
    """

    host: str
    path_prefix: str = "/"
    subdominios: bool = False
    termos: str = ""
    assunto: str | None = None  # casa com a pasta em data/raw (filtro do retrieval)


# Allowlist da busca externa. Fica no código (e não no .env) porque é regra de
# segurança: o que muda aqui muda o que o agente pode citar como fonte oficial.
#
# Sobre o path do Instructure: as três URLs de guia que originaram esta lista
# (.../kb/canvas-lms-{basics,instructor,student}-guide) são páginas-índice — os
# artigos com o conteúdo real ficam em /en/kb/articles/<id>-<slug>. Restringir o
# path ao slug do guia descartaria justamente as páginas úteis, então o corte
# duro é `/en/kb/`: a base de conhecimento oficial inteira, que já exclui o
# fórum de usuários (/t5/, /en/community/). Os três guias ficam cobertos por uma
# entrada só — em teste real, buscar o slug de cada guia separadamente não
# melhorou os resultados e triplicava as consultas ao mesmo host, o que acelera
# o rate limit do buscador. Uma entrada por host é a regra aqui.
WEB_ALLOWLIST: tuple[FonteWeb, ...] = (
    FonteWeb(
        host="puc-campinas.edu.br",
        subdominios=True,  # o conteúdo institucional se espalha por subdomínios
        assunto="puc-digital",
    ),
    FonteWeb(
        host="community.instructure.com",
        path_prefix="/en/kb/",
        assunto="canvas",
    ),
)

# Assuntos que nunca vão para a busca externa, mesmo com o RAG vazio: registro
# acadêmico e financeiro dependem do caso concreto do aluno, e responder isso a
# partir de uma página pública é pior que encaminhar para a secretaria.
WEB_BLOCKLIST: tuple[str, ...] = (
    "boleto",
    "bolsa",
    "cobranca",
    "diploma",
    "fies",
    "financeiro",
    "historico escolar",
    "matricula",
    "mensalidade",
    "minha nota",
    "prouni",
    "rematricula",
    "trancamento",
)


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

    # --- Telemetria (ver app/core/telemetry.py) ---
    # Mostra (ou não) a linha JSON de cada pergunta no terminal. Desligar aqui
    # não desliga a gravação no banco: são destinos independentes.
    telemetry_stderr_enabled: bool = True
    # Além da linha JSON em stderr, grava cada registro na tabela `telemetria`
    # do Postgres que já existe. Com False, só o log — útil para rodar a CLI
    # contra uma base indisponível ou para não sujar um banco de teste.
    telemetry_db_enabled: bool = True
    # Janela de retenção. Registros mais velhos são apagados na escrita (no
    # máximo 1x/hora por processo, sem cron). Curta de propósito: o valor deste
    # dado é operacional — custo, latência e documento faltando na semana —, e
    # guardar hash de pergunta de aluno indefinidamente não se justifica.
    telemetry_retention_days: int = 7

    # --- Fallback de busca externa (ver app/agent/web_fallback.py) ---
    # Kill switch: com False, o guardrail volta a se comportar como antes
    # (responde "não encontrei na base" sem chamar nada externo nem o LLM).
    web_fallback_enabled: bool = True
    web_search_region: str = "br-pt"
    # Backends do `ddgs`, tentados na ordem. O default da lib é "auto", que
    # espalha a pergunta do aluno por Yahoo, Startpage, Wikipedia, Grokipedia e
    # outros — fan-out desnecessário e lento (medido: ~57s no pior caso).
    #
    # "duckduckgo" sozinho não é opção hoje: em teste real ele responde "No
    # results found" depois de ~25s de retry (bloqueia cliente raspador com
    # frequência), e o Brave sozinho começa a devolver 429 depois de poucas
    # buscas seguidas. Os demais entram só como reserva, em ordem, quando o
    # anterior falha — não é fan-out: o primeiro que responder encerra.
    #
    # Trocar de buscador não afeta a garantia de restrição: a allowlist é
    # aplicada sobre a URL devolvida, seja qual for a origem do resultado.
    # (Wikipedia e Grokipedia ficam de fora: nunca servem a allowlist.)
    web_search_backend: str = "duckduckgo,brave,mojeek,startpage,yahoo"
    # Resultados pedidos por fonte da allowlist.
    web_search_max_results: int = 4
    # Orçamento total da busca, em segundos. Estourou, usa o que já chegou.
    web_search_timeout: float = 8.0
    # Similaridade mínima (embeddings locais) entre pergunta e snippet para o
    # resultado chegar ao LLM. Espelha `relevance_threshold`, mas em escala
    # própria: snippet de web é curto e pontua mais baixo que um chunk da base.
    # Calibrado em busca real: acertos ficaram em 0.52-0.65 e o melhor
    # falso-positivo de uma pergunta fora de escopo ("receita de bolo" casando
    # com um curso de gastronomia) ficou em 0.37 — 0.45 separa os dois grupos.
    web_relevance_threshold: float = 0.45
    # Quantos resultados no máximo entram no prompt.
    web_max_chunks: int = 4


settings = Settings()
