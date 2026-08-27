"""Configuração central, carregada do .env."""

import logging

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_RAW_DIR = PROJECT_ROOT / "data" / "raw"

_logger = logging.getLogger(__name__)


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
    FonteWeb(
            host="learn.microsoft.com",
            path_prefix="/pt-br",
            assunto="microsoft",
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
            "cobranca",
            "fies",
            "financeiro",
            "mensalidade",
            "prouni",
        ),
    ),
    # "bolsa" é ambíguo, e por isso está aqui e não junto dos termos acima —
    # mesmo padrão de "minha nota": entrada PRÓPRIA (mesmo assunto e mesmo
    # e-mail da anterior) porque `excecoes` vale para a categoria inteira, e
    # dentro da entrada acima elas desarmariam "boleto"/"fies"/"mensalidade"
    # junto.
    #
    # Os dois sentidos: pedir o benefício do próprio aluno ("ainda tenho
    # direito à bolsa?", "perdi minha bolsa") é da cobrança; bolsa de
    # iniciação científica, pesquisa, monitoria ou extensão é acadêmico/
    # pesquisa, e não tem nada a ver com o setor financeiro.
    #
    # Isto foi uma CORREÇÃO, não um refinamento preventivo: na rodada de
    # avaliação de 2026-08-26, "processo seletivo de bolsas de iniciação
    # científica" foi encaminhada para a cobrança em 1.3ms, sem tocar no RAG
    # (ver eval/analise-telemetria-2026-08-26.md §5).
    #
    # As exceções valem também para a busca externa: `web_fallback` consulta a
    # mesma `classificar`, então uma pergunta de IC passa a poder ser
    # pesquisada nos domínios oficiais em vez de morrer aqui.
    CategoriaEncaminhada(
        assunto="financeiro",
        resposta=_CONTATO.format(email="dcr@puc-campinas.edu.br"),
        termos=("bolsa",),
        excecoes=(
            "iniciacao cientifica",
            "pesquisa",
            "monitoria",
            "extensao",
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

# Encaminhamento genérico, sem departamento específico: usado pelo guardrail de
# entrada (ver app/agent/guardrail.py), quando a pergunta não é de outro setor —
# é um pedido de abuso/ataque que não deve tocar RAG, web nem LLM. Mesmo e-mail
# do suporte acadêmico da entrada "academico".
CONTATO_PADRAO = _CONTATO.format(email="puc.digital@puc-campinas.edu.br")


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
    # Nome novo da MESMA chave. `GOOGLE_API_KEY` era o nome quando os embeddings
    # também eram do Google; agora que só o chat é, `GEMINI_API_KEY` diz melhor
    # o que ela paga — e casa com `GROQ_API_KEY`/`OPENROUTER_API_KEY`. As duas
    # continuam válidas (ver `chave_gemini`): um `.env` existente não quebra.
    gemini_api_key: str = ""
    # Usado em DOIS lugares: (1) rate limit maior no download do modelo local de
    # embeddings — sem token, tudo funciona igual, só um pouco mais devagar; (2)
    # autenticação do provider `huggingface` na cadeia de chat (Inference
    # Providers), que fala a mesma API da OpenAI atrás de
    # `https://router.huggingface.co/v1`. Sem token, o provider simplesmente
    # sai da cadeia (ver `providers/chain._huggingface`), igual a Groq/OpenRouter
    # sem chave.
    hf_token: str = ""
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/agente_ead"

    # Local (HuggingFace/sentence-transformers) para não depender de cota de API.
    # Multilingual porque o conteúdo é majoritariamente em português.
    #
    # Substitui o antigo paraphrase-multilingual-mpnet-base-v2, que truncava em
    # 128 tokens (`max_seq_length` do próprio model card). Medido neste corpus
    # (1289 chunks de 3 PDFs, tokenizer do e5): média 108 tokens, mediana 92,
    # p99 233, máximo 254 — mas 34,4% dos chunks passavam de 128 e eram cortados
    # antes de virar vetor. O texto cortado seguia no `page_content` (o LLM lia),
    # só não influenciava a BUSCA: um terço da base com vetor incompleto, sem
    # erro nenhum em log.
    #
    # e5-base e não BGE-M3 (a primeira escolha) por causa do hardware: sem GPU,
    # os 568M de parâmetros do BGE-M3 levaram >23min só na primeira metade da
    # ingestão, e o mesmo custo se paga em CADA pergunta (`embed_query`) e em
    # cada snippet do fallback web. Os 8192 tokens dele eram 32x de folga sobre
    # o chunk máximo real (254): 512 cobre tudo com margem de 2x.
    #
    # O máximo de 254 tokens é bem menor que os 1000 CARACTERES do chunk_size
    # porque o `PyPDFLoader` entrega um Document por PÁGINA e o splitter nunca
    # junta páginas — na prática quem limita o chunk é a página, não o
    # `chunk_size`. Vale lembrar disso antes de mexer no chunking.
    #
    # Trocar de modelo muda a dimensão do vetor — `collection_name` abaixo tem
    # sufixo próprio por isso, e a reingestão é obrigatória.
    embedding_model: str = "intfloat/multilingual-e5-base"
    # Prefixos do E5, e eles NÃO são decorativos: a família e5 é treinada com
    # instrução assimétrica (pergunta e documento entram com marcas diferentes),
    # e usá-la sem os prefixos degrada o ranking de forma silenciosa — os vetores
    # saem, a busca "funciona", só devolve pior.
    #
    # Vivem aqui, e não fixos no código, porque são propriedade do MODELO: quem
    # trocar `embedding_model` por um que não use instrução (BGE-M3, os
    # `paraphrase-*`) esvazia os dois e o wrapper vira no-op, sem editar código.
    # Ver `providers/embeddings.get_embeddings`.
    embedding_query_prefix: str = "query: "
    embedding_passage_prefix: str = "passage: "
    chat_model: str = "gemini-3.6-flash"
    # Modelo do provider `huggingface` (Inference Providers), 2º elo da cadeia —
    # ver LLM_PROVIDERS e providers/chain._huggingface. Nome de campo (e de env
    # var, HF_MODEL) espelha GROQ_MODEL/OPENROUTER_MODEL de propósito. Catálogo
    # muda com frequência: confirme com
    # `python -m scripts.modelos --provider huggingface`.
    hf_model: str = "meta-llama/Llama-3.3-70B-Instruct"
    # Vazio = a URL padrão do router da HF (ver providers/openai_compat.py).
    hf_base_url: str = ""

    chunk_size: int = 1000
    chunk_overlap: int = 150

    # Teto de caracteres POR fonte de contexto (chunk da base ou snippet da web)
    # ao montar o prompt — ver `responder._format_context`. Guarda contra o caso
    # documentado em `embedding_model` acima (o `PyPDFLoader` entrega uma página
    # inteira como um "chunk", e uma página densa passa de 8 mil caracteres) e
    # contra um `body` gigante devolvido pela busca web: cinco desses no mesmo
    # prompt já estouraram o limite de tokens por requisição de provider de tier
    # gratuito (HTTP 413 `request_too_large` — ver `providers/base.py` e
    # eval/analise-telemetria-2026-08-27.md §10). 6000 ≈ 1500 tokens; o chunk
    # típico deste corpus tem ~1000 caracteres, bem abaixo, então só o caso
    # patológico é cortado. 0 desliga o corte.
    prompt_context_item_max_chars: int = 6000

    top_k: int = 5
    relevance_threshold: float = 0.35

    # Acima disso, as 2 fontes do topo são tratadas como alta confiança (ver
    # retrieval/retriever.is_exact_match): a base tem muita informação repetida,
    # então 2 fontes fortes concordando já é sinal de resposta certa.
    exact_match_threshold: float = 0.90

    # Nome da coleção no pgvector. Trocar isola um índice novo do antigo.
    # Sufixo pelo MODELO de embedding: a dimensão do vetor muda a cada troca
    # (768 do antigo paraphrase-multilingual-mpnet-base-v2, 768 do e5-base atual)
    # — e a dimensão IGUAL é justamente o caso perigoso, porque nada quebra: a
    # busca roda e devolve lixo, comparando vetores de espaços vetoriais
    # diferentes. Coleção nova a cada troca de modelo, sempre.
    collection_name: str = "base_conhecimento_e5_base"

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

    # --- Guardrail de entrada (ver app/agent/guardrail.py) ---
    # Kill switch de rollback: com False, o pedido de ataque/abuso (injeção,
    # exfiltração de segredo, execução não autorizada, código de exploit) volta
    # a seguir para o RAG em vez de ser encaminhado na entrada. Mesmo motivo do
    # `triagem_enabled`: desarmar rápido um padrão mal calibrado sem deploy.
    guardrail_enabled: bool = True

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
    # Teto diário POR consumidor, no formato `nome:limite,outro:limite`. Só
    # entra quem for listado; os demais respondem ao teto global.
    #
    # Existe por causa da demo (T3.1): a chave dela vive num HTML público, e o
    # teto global sozinho significa que quem abrir o DevTools pode consumir o
    # orçamento do dia inteiro — inclusive o do AVA. Com um teto próprio, o pior
    # caso da demo é a demo parar.
    rate_limit_diario_por_consumidor: str = ""

    # --- Demo web (T3.1) ---
    # Serve `app/static/index.html` em `/demo`. Desligar tira a rota do ar sem
    # mexer em nada da v1.
    demo_enabled: bool = True
    # Nome (em API_KEYS) da integração que a demo usa. A chave é injetada no
    # HTML pelo servidor — é pública para quem abrir o DevTools, e é por isso
    # que ela é uma integração PRÓPRIA: revogá-la ou estourar o teto dela não
    # afeta o AVA. Referenciar por NOME, e não repetir a chave numa segunda
    # variável, evita o modo de falha silencioso de rotacionar API_KEYS e
    # esquecer da cópia.
    demo_consumidor: str = "demo"

    # --- Chamada ao LLM: cadeia de providers (T4.1) ---
    # Ordem de PRIORIDADE, separada por vírgula. É o único lugar que decide quem
    # é tentado e em que ordem — reordenar, tirar um provedor da cadeia ou rodar
    # com um só é edição de .env, sem deploy. Provider sem chave configurada sai
    # da cadeia sozinho (ver providers/chain.construir_providers), então
    # desligar o Groq é apagar `GROQ_API_KEY` OU tirá-lo daqui.
    #
    # A ordem padrão não é arbitrária: Gemini é o modelo com que os prompts em
    # app/agent/prompts.py foram calibrados; HuggingFace vem em segundo porque
    # reaproveita o HF_TOKEN que já existe para os embeddings, sem chave nova a
    # gerenciar — um fallback "de graça" antes de sair para Groq; Groq vem
    # depois por ser o mais rápido dos dois restantes (um fallback só é bom se o
    # aluno não perceber); o OpenRouter `:free` fica por último porque é o de
    # cota mais apertada — é rede de segurança, não provedor de regime.
    llm_providers: str = "gemini,huggingface,groq,openrouter"

    groq_api_key: str = ""
    openrouter_api_key: str = ""
    # Catálogo dos dois muda com frequência (modelo saído de linha vira 404 —
    # ver `providers/base._STATUS_DE_CONFIGURACAO` e `scripts/modelos.py`).
    # Estes defaults foram confirmados no catálogo em 2026-08; se um deles
    # sumir, `python -m scripts.modelos` diz qual e mostra o resto do catálogo.
    groq_model: str = "qwen/qwen3.6-27b"
    openrouter_model: str = "z-ai/glm-5.2:free"
    # Vazio = a URL padrão do SDK (ver providers/openai_compat.py). Existe para
    # apontar a cadeia a um proxy/gateway interno sem tocar em código.
    groq_base_url: str = ""
    openrouter_base_url: str = ""

    # Timeout POR PROVIDER, em segundos. Sem isto, uma request pendurada no LLM
    # segura uma thread do pool do uvicorn indefinidamente (as rotas são `def`,
    # não `async def`) — poucas dessas e o servidor inteiro para de responder.
    #
    # Pior caso de espera de um `/ask` = llm_timeout * nº de providers na cadeia
    # * llm_tentativas_por_provider. Com os padrões: 30s * 4 * 1 = 120s.
    llm_timeout: float = 30.0
    # Tentativas DENTRO de um provider antes de cair para o próximo. 1 de
    # propósito: com vários provedores em fila, insistir com quem acabou de
    # devolver 429 é gastar o orçamento de latência do aluno com a resposta que
    # já se sabe. Retry é insistir com quem falhou; fallback é perguntar a
    # outro — e é o segundo que esta cadeia faz.
    #
    # O nome fala em TENTATIVAS, e não em retries, porque os dois SDKs usam
    # `max_retries` com significados opostos (total de chamadas no
    # langchain-google-genai, repetições depois da primeira no da OpenAI). A
    # tradução é feita em cada provider; ver os comentários lá.
    llm_tentativas_por_provider: int = 1

    # Permite que o corpo de `/ask` traga `modelo` e escolha com que modelo
    # responder (ver `app/core/models.Query.modelo`). DESLIGADO por padrão, e o
    # padrão é a parte importante: a chave é nossa, então quem pode escolher o
    # modelo pode escolher o mais caro do catálogo do provedor — o teto diário
    # em requisições (`rate_limit_diario_global`) conta chamadas, não custo, e
    # não protege contra isso. Ligue em desenvolvimento e para avaliar modelo;
    # em produção, deixe `false` e mude o `.env` quando a escolha for definitiva.
    #
    # A CLI (`scripts/ask.py --modelo`) não passa por aqui: ela roda com o
    # `.env` na mão, então já pode tudo o que este switch protege.
    ask_modelo_override_enabled: bool = False

    # Com `--modelo`/`modelo` o cache fica DESLIGADO por padrão (nem lê nem
    # grava) — ver `_cache_key` em app/agent/responder.py. Ligue este switch se
    # quiser que um override repetido (mesmo modelo, mesma pergunta) sirva do
    # cache em vez de pagar o LLM de novo a cada chamada.
    #
    # Seguro por construção: o MODELO entra na chave do cache (ver
    # `_cache_key`), então ligar isto não corre o risco que o desligamento
    # original evitava — testar `groq:x` nunca serve a resposta cacheada do
    # `gemini:y`, nem a de outro modelo do mesmo provider. Cada combinação
    # (pergunta, chunks, modelo) tem sua própria entrada.
    modelo_override_cache_enabled: bool = False

    @property
    def chave_gemini(self) -> str:
        """`GEMINI_API_KEY`, com `GOOGLE_API_KEY` como nome legado.

        O nome novo vence quando os dois estão preenchidos: quem acabou de
        configurar `GEMINI_API_KEY` espera que ela seja usada, e uma
        `GOOGLE_API_KEY` velha e revogada esquecida no .env viraria um 401
        misterioso.
        """
        return self.gemini_api_key or self.google_api_key

    @property
    def llm_providers_lista(self) -> list[str]:
        """Nomes da cadeia, normalizados e sem repetição, na ordem declarada.

        Deduplicar importa: `gemini,groq,gemini` construiria dois clientes do
        Gemini e faria a mesma chave 429 ser tentada duas vezes antes do Groq —
        exatamente o retry que esta cadeia existe para não fazer.
        """
        vistos: dict[str, None] = {}
        for nome in _csv(self.llm_providers):
            vistos.setdefault(nome.casefold(), None)
        return list(vistos)

    @property
    def api_keys_por_chave(self) -> dict[str, str]:
        """`{chave: nome do consumidor}` — a busca é sempre pela chave recebida."""
        mapa: dict[str, str] = {}
        for entrada in _csv(self.api_keys):
            nome, _, chave = entrada.partition(":")
            if chave:
                mapa[chave.strip()] = nome.strip()
        return mapa

    def chave_do_consumidor(self, nome: str) -> str | None:
        """Chave registrada para uma integração, ou None se ela não existe.

        Busca inversa de `api_keys_por_chave` — usada só pela demo (T3.1), que
        é o único lugar do sistema que precisa PRODUZIR uma chave em vez de
        conferir uma recebida.
        """
        return next(
            (chave for chave, registrado in self.api_keys_por_chave.items() if registrado == nome),
            None,
        )

    @property
    def tetos_diarios_por_consumidor(self) -> dict[str, int]:
        """`{nome: teto}`. Entrada malformada é ignorada com log, não derruba o boot."""
        tetos: dict[str, int] = {}
        for entrada in _csv(self.rate_limit_diario_por_consumidor):
            nome, _, limite = entrada.partition(":")
            try:
                tetos[nome.strip()] = int(limite)
            except ValueError:
                _logger.warning(
                    "RATE_LIMIT_DIARIO_POR_CONSUMIDOR: entrada ignorada (%r); "
                    "formato esperado `nome:limite`",
                    entrada,
                )
        return tetos

    @property
    def cors_origins_lista(self) -> list[str]:
        """Origens explícitas. "*" é descartado: com autenticação por header,
        liberar qualquer origem significa que qualquer página pode gastar a
        chave de quem a estiver usando. Origem nova entra no .env, uma a uma."""
        return [origem for origem in _csv(self.cors_origins) if origem != "*"]


settings = Settings()
