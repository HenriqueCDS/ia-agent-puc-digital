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

@dataclass(frozen=True)
class CategoriaEncaminhada:
    """Assunto que não é do agente, e para onde ele deve ser encaminhado.

    `termos` são casados como substring contra a pergunta sem acento e em caixa
    baixa (ver `app/agent/triagem.py`), então "matricula" também casa
    "Matrícula" e "matriculas".

    `excecoes` desarmam o encaminhamento quando a pergunta também traz um desses
    termos, e existem para o termo que é ambíguo por natureza. "minha nota" é o
    caso: pedir o valor da nota é da secretaria, mas "onde vejo minhas notas no
    Canvas" é procedimento que a base responde bem. A exceção é o que permite
    listar o termo sem perder essas perguntas.

    Termo ambíguo vai em entrada PRÓPRIA, nunca junto de termos inequívocos: as
    exceções valem para a categoria inteira, e desarmariam os outros termos
    junto. Alternativa mais simples, quando serve: não listar o termo ambíguo —
    é o que se faz com "matrícula", em que só "rematricula" é listado.
    """

    assunto: str  # rótulo na telemetria (Registro.assunto)
    resposta: str
    termos: tuple[str, ...]
    excecoes: tuple[str, ...] = ()


_CONTATO = "Sobre esse tipo de assunto, recomendo verificar diretamente com a \
secretaria acadêmica ou com o suporte da instituição ({email})."

# Assuntos que o agente NÃO trata: dependem do caso concreto do aluno e são de
# outro departamento. A pergunta é encaminhada antes do retrieval, sem gastar
# RAG nem LLM (ver `app/agent/triagem.py` e `responder._responder`).
#
# A ORDEM IMPORTA — primeiro match vence: "boleto da rematrícula" casa
# financeiro e acadêmico, que têm e-mails diferentes, e financeiro vem primeiro
# porque quem cobra responde por cobrança.
#
# A regra vale para termo que seja substring de outro. Se um dia "matricula"
# entrar aqui, ele tem que vir DEPOIS de "rematricula" (e com `excecoes`),
# senão toda rematrícula cairia na entrada errada.
ENCAMINHAMENTOS: tuple[CategoriaEncaminhada, ...] = (
    CategoriaEncaminhada(
        assunto="financeiro",
        resposta=_CONTATO.format(email="dcr@puc-campinas.edu.br"),
        termos=(
            "boleto",
            "bolsa",
            "cobranca",
            "fies",
            "financeiro",
            "mensalidade",
            "prouni",
        ),
    ),
    CategoriaEncaminhada(
        assunto="diplomas",
        resposta=_CONTATO.format(email="diplomas@puc-campinas.edu.br"),
        termos=("diploma", "certificado"),
    ),
    CategoriaEncaminhada(
        assunto="academico",
        resposta=_CONTATO.format(email="puc.digital@puc-campinas.edu.br"),
        termos=("rematricula", "historico escolar", "trancamento"),
    ),
    # Ambígua, por isso em entrada PRÓPRIA (mesmo assunto e mesmo e-mail da
    # anterior, e não termo dela): "minha nota" tanto pede o valor da nota, que
    # depende do registro do aluno e é da secretaria, quanto pergunta como vê-la
    # no Canvas ou na área do aluno — procedimento que os guias oficiais
    # documentam e o agente responde bem.
    #
    # Entrada separada porque `excecoes` vale para a categoria inteira: se
    # estivesse junto de "rematricula", "como faço a rematrícula no portal do
    # aluno" seria desarmado por engano — e rematrícula é da secretaria
    # independente da plataforma citada.
    #
    # As exceções são de dois tipos, porque o que desfaz a ambiguidade também é:
    # a plataforma citada, e o verbo de procedimento ("onde vejo") em oposição
    # ao pedido do valor ("qual é").
    CategoriaEncaminhada(
        assunto="academico",
        resposta=_CONTATO.format(email="puc.digital@puc-campinas.edu.br"),
        # "minhas notas" não contém "minha nota" (o `s` quebra a substring),
        # por isso as duas formas.
        termos=("minha nota", "minhas notas"),
        excecoes=(
            "canvas",
            "area do aluno",
            "portal do aluno",
            "plataforma",
            "onde vejo",
            "onde encontro",
            "onde consulto",
            "como vejo",
            "como ver",
            "como acesso",
            "como consulto",
        ),
    ),
)


def _csv(valor: str) -> list[str]:
    """Divide uma lista separada por vírgula do .env, ignorando espaços e vazios."""
    return [item.strip() for item in valor.split(",") if item.strip()]


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

    # --- Triagem por assunto (ver app/agent/triagem.py) ---
    # Kill switch de rollback: com False, a pergunta de assunto fora de escopo
    # volta a seguir para o RAG em vez de ser encaminhada na entrada. Existe
    # para desarmar rápido um termo mal calibrado em ENCAMINHAMENTOS sem
    # precisar de deploy.
    triagem_enabled: bool = True

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
    # Orçamento por rodada de busca, em segundos. Estourou, usa o que já chegou.
    # Pergunta com assunto cuja primeira rodada (fontes daquele assunto) não
    # acha nada paga até 2x isso: `buscar_na_web` tenta de novo com a allowlist
    # inteira antes de desistir (ver app/agent/web_fallback.py).
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

    # --- Borda HTTP: autenticação (T2.1) ---
    # Kill switch para desenvolvimento local. Em produção fica `true`: cada
    # `/ask` que não bate cache é uma chamada paga ao Gemini mais até 5 buscas
    # web, então o endpoint aberto é um proxy grátis para a cota da instituição.
    api_auth_enabled: bool = True
    # Uma chave POR INTEGRAÇÃO, no formato `nome:chave,outro:chave`. O nome vai
    # para a telemetria (`canal`), que é o que permite atribuir custo por canal
    # e, depois, revogar só o consumidor que abusou — com uma chave única para
    # todo mundo, revogar derruba todas as integrações de uma vez.
    api_keys: str = ""

    # --- Borda HTTP: CORS (T2.2) ---
    # Origens permitidas, separadas por vírgula (ex.: https://ava.puc-campinas.edu.br).
    # Vazio = nenhuma origem cruzada; a demo servida pela própria API não precisa
    # de CORS. "*" é ignorado de propósito — ver `cors_origins_lista`.
    cors_origins: str = ""

    # --- Borda HTTP: rate limit (T2.3) ---
    # Janela deslizante de 60s por consumidor. Protege a cota do Gemini e evita
    # que o buscador externo comece a devolver 429 (ver web_search_backend).
    rate_limit_por_minuto: int = 20
    # Teto do processo inteiro, por dia (UTC). É o limite de custo: mesmo com
    # várias integrações bem-comportadas, a soma não passa disto.
    rate_limit_diario_global: int = 2000

    # --- Chamada ao LLM (T2.5) ---
    # Sem isto, uma request pendurada no Gemini segura uma thread do pool do
    # uvicorn indefinidamente (as rotas são `def`, não `async def`) — poucas
    # dessas e o servidor inteiro para de responder.
    gemini_timeout: float = 30.0
    # A lib tenta 6 vezes por padrão; com timeout, isso vira 6x o tempo de
    # espera no pior caso. 2 cobre a falha transitória sem virar um bloqueio.
    gemini_max_retries: int = 2

    @property
    def api_keys_por_chave(self) -> dict[str, str]:
        """`{chave: nome do consumidor}` — a busca é sempre pela chave recebida."""
        mapa: dict[str, str] = {}
        for entrada in _csv(self.api_keys):
            nome, _, chave = entrada.partition(":")
            if chave:
                mapa[chave.strip()] = nome.strip()
        return mapa

    @property
    def cors_origins_lista(self) -> list[str]:
        """Origens explícitas. "*" é descartado: com autenticação por header,
        liberar qualquer origem significa que qualquer página pode gastar a
        chave de quem a estiver usando. Origem nova entra no .env, uma a uma."""
        return [origem for origem in _csv(self.cors_origins) if origem != "*"]


settings = Settings()
