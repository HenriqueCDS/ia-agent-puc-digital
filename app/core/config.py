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
    recall; `host`/`path_prefixes` são o que de fato *restringe*: toda URL
    devolvida é revalidada contra eles antes de chegar ao LLM.

    `path_prefixes` é uma tupla: uma URL é aceita se o PATH dela começa por
    QUALQUER um dos prefixos. `("/",)` (o default) libera o host inteiro; uma
    lista de páginas curadas (`("/calendario/", "/biblioteca/")`) restringe a
    busca ao que a equipe conferiu — ver KB-2 no comentário abaixo.

    `seeds` é uma lista FECHADA de URLs para o crawler (`scripts/crawl.py`):
    quando preenchida, o crawler NÃO descobre por sitemap — ele indexa exatamente
    essas páginas (ainda revalidadas por `fonte_permitida`, e o prune trata seed
    removida daqui como remoção intencional). Existe para host sem sitemap útil:
    `support.microsoft.com` declara um índice com 2266 sub-sitemaps (um por
    tópico×idioma) e `/pt-br/teams/` sozinho tem 777 URLs, quase nenhuma do caso
    de uso do agente. Não afeta a busca ao vivo (`web_fallback` usa só
    `host`/`path_prefixes`).
    """

    host: str
    path_prefixes: tuple[str, ...] = ("/",)
    subdominios: bool = False
    termos: str = ""
    assunto: str | None = None  # casa com a pasta em data/raw (filtro do retrieval)
    seeds: tuple[str, ...] = ()  # crawler: lista fechada de URLs, em vez de sitemap


# Allowlist da busca externa: o conjunto FECHADO de páginas que o agente pode
# consultar e citar quando a base indexada não cobre a pergunta (ver
# `app/agent/web_fallback.py`). Fica no código, e não no .env, porque é regra de
# segurança — cada host aqui é uma fonte que o agente passa a tratar como
# oficial.
#
# REGRAS AO EDITAR:
#
# 1. UMA entrada por host. Cada `FonteWeb` vira uma query `site:<host>` no
#    DuckDuckGo; o mesmo host repetido em 5 entradas dispara a mesma busca 5x e
#    só acelera o rate limit do scraper. Um caminho específico (/pos/, /secretaria/)
#    NÃO precisa de entrada própria: entra como mais um item de `path_prefixes`
#    na entrada do host.
#
# 2. `path_prefixes` do Instructure é `("/en/kb/",)`, não o slug do guia. As
#    páginas .../canvas-lms-student-guide são índices SEM conteúdo; os artigos
#    ficam em /en/kb/articles/<id>-<slug>, e um prefixo no slug do guia
#    rejeitaria justamente esses na revalidação de URL (`fonte_permitida`).
#    `/en/kb/` cobre a KB oficial inteira e já exclui o fórum de usuários (/t5/,
#    /en/community/).
#
# 3. `path_prefixes` casa contra o PATH da URL, nunca contra o fragmento
#    (`#...`) — um valor como "/x/#y" nunca casa nada.
#
# 4. `assunto` casa com a PASTA em data/raw (`canvas`, `puc-digital`) ou é None.
#    É o rótulo gravado na telemetria quando a web responde (`_assunto_da_web`) e
#    o filtro de `_fontes_para`. Texto livre ("Pós Graduação", "Teams") polui o
#    relatório de lacunas e quebra o filtro.
#
# 5. KB-2 — o portal institucional (`www.puc-campinas.edu.br`) NÃO entra inteiro.
#    `subdominios=True` + `path_prefixes=("/",)` cobria vestibular, avaliação
#    institucional, notícias, landing pages de campanha e PDFs soltos em
#    /wp-content/ — conteúdo que respondia com confiança aparente sobre assunto
#    que não é do agente (Q7 "nota mínima PUC" citou página de vestibular; ver
#    eval/backlog-problemas.md KB-2). O que entra agora: uma LISTA de caminhos
#    conferidos do portal (`/calendario/`, `/secretaria-geral/`, `/biblioteca/`).
#    NÃO existe subdomínio `pucdigital.puc-campinas.edu.br` — o conteúdo da PUC
#    Digital fica no portal. Caminho novo só entra depois de alguém abrir e
#    conferir.
WEB_ALLOWLIST: tuple[FonteWeb, ...] = (
    # Portal institucional: SÓ as páginas curadas (ver regra 5 acima). Sem
    # `subdominios` — vestibular/pos/educacional são outros subdomínios e cada um
    # traria o próprio ruído. `host` sem `www.`: `fonte_permitida` normaliza os
    # dois lados removendo o prefixo, então casa `www.puc-campinas.edu.br`.
    FonteWeb(
        host="puc-campinas.edu.br",
        path_prefixes=(
            "/pos/",                  # pós-graduação: calendário acadêmico, datas de prova/recesso
            "/secretaria-geral/",     # competência da secretaria, procedimentos oficiais
            "/biblioteca/",           # serviços e regulamentos da biblioteca
            "/mestrado-e-doutorado/",  # stricto sensu (/relacionamento/mestrado-e-doutorado/ redireciona pra cá)
            "/atualizacao/",          # cursos de atualização/extensão
        ),
        termos="PUC Digital estudante",
        assunto="puc-digital",
    ),
    # Base de conhecimento oficial do Canvas (guias do estudante, do professor e
    # dos apps mobile — todos sob /en/kb/).
    FonteWeb(
        host="community.instructure.com",
        # `/en/kb/` e `/pt/kb/` (a KB oficial, EN e PT-BR) — NÃO o slug do guia.
        # Os guias .../canvas-lms-*-guide são índices SEM conteúdo; os artigos
        # ficam em /{en,pt}/kb/articles/<id>-<slug>, e um prefixo no slug do guia
        # rejeitaria justamente esses na revalidação (`fonte_permitida`). `/kb/`
        # já exclui o fórum de usuários (/t5/, /en/community/). Ver regra 2 acima.
        path_prefixes=("/en/kb/", "/pt/kb/"),
        assunto="canvas",
    ),
    # As aulas ao vivo da PUC Digital acontecem em salas do Teams (ver
    # AulasAoVivo_v2-2.pdf, p.6). A documentação oficial da Microsoft cobre o que
    # a base interna não detalha: "não consigo entrar na reunião", áudio/câmera,
    # entrar como convidado, e a conta corporativa/de estudante (o aluno usa a
    # conta da PUC para entrar no Teams).
    #
    # PATHS (verificados — support.microsoft.com NÃO tem sitemap e NÃO é
    # WordPress, então isto vale só para a busca ao vivo / seeds, não para o
    # crawler por sitemap):
    #  - `/pt-br/teams/<cat>/<slug>` serve o artigo de Teams em PT-BR
    #    (ex.: /pt-br/teams/meetings/join-a-meeting-in-microsoft-teams).
    #  - `/en-us/teams/<cat>/<slug>` é o mesmo em inglês — rede para o artigo
    #    sem versão PT (a URL sem locale, `/teams/...`, só redireciona para cá,
    #    nunca aparece como resultado).
    #  - `/pt-br/accounts-billing/work-school/` (plural, "accounts"): verificação
    #    em duas etapas / app autenticador da conta corporativa ou de estudante.
    # NÃO usar `/pt-br/office/` (é a árvore de TODO o Office — Word, Excel,
    # Outlook): `/pt-br/teams/` já traz o conteúdo de Teams em português (KB-2).
    # `assunto=None`: um resultado desses não é "canvas" nem "puc-digital" —
    # deixa o rótulo vir da própria pergunta. UMA entrada por host (regra 1).
    #
    # CRAWLER: `seeds` (lista fechada) em vez de sitemap. O índice de sitemap da
    # MS (declarado no robots.txt) tem 2266 sub-sitemaps e `/pt-br/teams/`
    # sozinho tem 777 URLs ("o que é o Teams", "breakout rooms", ...), quase
    # nenhuma do caso de uso do agente. As seeds são os artigos que um aluno da
    # PUC de fato precisa: entrar na reunião (conta/convidado/telefone/somente
    # exibição), áudio e câmera, e a senha da conta corporativa. Artigo novo
    # entra aqui depois de conferido; seed removida some do índice no próximo
    # `--prune`. `web_fallback` ao vivo continua cobrindo o que não está nas
    # seeds. Confira as URLs com: `python -m scripts.crawl --host support.microsoft.com --dry-run`.
    FonteWeb(
        host="support.microsoft.com",
        path_prefixes=(
            "/pt-br/teams/",
            "/en-us/teams/",
            "/pt-br/accounts-billing/work-school/",
        ),
        seeds=(
            # entrar na reunião
            "https://support.microsoft.com/pt-br/teams/meetings/join-a-meeting-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/join-a-meeting-without-an-account-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/join-a-meeting-as-a-view-only-attendee-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings-events/join-a-teams-meeting-by-phone",
            "https://support.microsoft.com/pt-br/teams/meetings/i-can-t-join-a-meeting-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/use-meeting-controls-in-microsoft-teams",
            # áudio e câmera
            "https://support.microsoft.com/pt-br/teams/meetings/manage-audio-settings-in-microsoft-teams-meetings",
            "https://support.microsoft.com/pt-br/teams/meetings/my-microphone-isn-t-working-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/my-camera-isn-t-working-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/use-video-in-microsoft-teams",
            "https://support.microsoft.com/pt-br/teams/meetings/change-your-background-in-microsoft-teams-meetings",
            "https://support.microsoft.com/pt-br/teams/meetings/reduce-background-noise-in-microsoft-teams-meetings",
            "https://support.microsoft.com/pt-br/teams/meetings/raise-your-hand-in-microsoft-teams-meetings",
            # conta corporativa / de estudante (login da PUC no Teams)
            "https://support.microsoft.com/pt-br/accounts-billing/work-school/change-your-work-or-school-account-password",
            "https://support.microsoft.com/pt-br/accounts-billing/work-school/reset-your-microsoft-work-or-school-account-password-using-security-info",
            "https://support.microsoft.com/pt-br/accounts-billing/work-school/common-problems-with-two-step-verification-for-a-work-or-school-account",
        ),
        termos="Teams reunião aula entrar conta",
        assunto=None,
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
        # "trancamento" (forma nominal) e "trancar" (verbo): o léxico é preso à
        # forma, então "quero trancar o curso" não casava "trancamento". As duas
        # formas listadas em vez de normalizar por radical `tranc` — mais
        # explícito e sem risco de casar palavra não relacionada. "trancar" não
        # é substring de "trancamento" nem vice-versa, a ordem entre eles não
        # importa (ver regra de substring no topo de ENCAMINHAMENTOS).
        termos=("rematricula", "historico escolar", "trancamento", "trancar"),
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

    # RET-5 — 1000 → 700. `CHUNK_SIZE` é em CARACTERES e só tem efeito para
    # BAIXO: o `PyPDFLoader` entrega 1 Document por página e o splitter nunca
    # junta páginas, então subir não junta nada (ver `embedding_model` acima).
    # Medido no corpus atual com `python -m scripts.chunk_stats` (5371 páginas):
    # página mediana = 403 chars / 103 tokens E5, p75 = 695, p90 = 1073.
    # 700 ≈ p75: mantém ~75% das páginas como 1 chunk (a mediana fica folgada
    # dentro) e quebra só o quartil denso — que costuma misturar mais de um
    # assunto. Índice cresce ~18% (6174 → ~7300 chunks); p99 do chunk fica em
    # ~185 tokens, bem abaixo do teto de 512 do E5. **Exige reingestão**
    # (`python -m scripts.ingest`). Se uma rodada de eval piorar, volte a 1000.
    chunk_size: int = 700
    chunk_overlap: int = 150

    # RET-6 — na ingestão, descarta um chunk se ele for pelo menos ESTE tão
    # parecido (Jaccard de shingles de 4 palavras) com outro chunk já mantido do
    # mesmo lote. O `content_hash` só pega repetição EXATA; documentos como o
    # `Canvas_Student_Guide.pdf` (1108 páginas) repetem o mesmo procedimento em
    # dezenas de páginas com texto quase igual (número de página, cabeçalho
    # mudam), e o retrieval acabava devolvendo 5 chunks idênticos — margem
    # relativa zero (RET-2), resposta parecendo mais coberta do que é (Q11/Q23).
    # 0.9 é conservador: só casa quase-cópia, não parágrafos que dividem jargão.
    # 0 desliga. Só afeta a ingestão; não reindexa o que já está no banco.
    ingest_dedup_similaridade: float = 0.9

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
    # RET-1 — subido de 0.35 (inerte: o E5 pontua ~0.82 para QUALQUER par de
    # textos em português, então 0.35 nunca cortava nada — ver
    # eval/analises/analise-telemetria-2026-08-28.md §2.2). Não separa "a base
    # cobre" de "não cobre" (isso é a margem relativa, RET-2/RET-3) — é só uma
    # REDE contra lixo óbvio: corta a Q4 (fotossíntese, 0.8215) e pares fora de
    # domínio parecidos, enquanto o reranker não vem. O menor score de acerto
    # real registrado até 28-08 foi 0.8451, então 0.85 raspa esse limite —
    # baixar para 0.80/0.78 se uma rodada mostrar acerto perdido nessa faixa.
    # Não exige reingestão. Espelha RELEVANCE_THRESHOLD no .env.example.
    relevance_threshold: float = 0.85

    # --- Reranker cross-encoder (RET-3, ver app/retrieval/reranker.py) ---
    # Kill switch. `false` = o retrieval segue bi-encoder puro (o caminho de
    # hoje): busca `TOP_K` candidatos no E5, corta por `RELEVANCE_THRESHOLD`.
    # `true` = 2 estágios: o E5 traz `RERANKER_CANDIDATES`, o cross-encoder
    # reordena e o corte passa a ser por `RERANKER_THRESHOLD`. Entra ligado só
    # depois da calibração contra a suíte de fidelidade (T-1) — ver §6 de
    # `eval/future_feature/cross-encoder.md`. Mesmo desenho de kill switch de
    # `triagem_enabled` / `guardrail_enabled`.
    reranker_enabled: bool = False
    # Multilíngue (~120MB), treinado em mmarco — lida com pergunta PT × documento
    # EN (os guias Canvas). ~130–260ms para rerank de 20 pares em CPU. Trocar por
    # `BAAI/bge-reranker-v2-m3` é mais forte, mas 2.3GB + ~1–2s/rerank e aperta a
    # VM junto do E5 (~1GB). Modelo local, offline-first, igual `EMBEDDING_MODEL`.
    reranker_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    # `k` do 1º estágio (bi-encoder) quando o reranker está ligado. Recall amplo
    # para o cross-encoder ter o que reordenar; o corte final continua em
    # `TOP_K`. É ~linear no custo do rerank — 30 pares ≈ ~200–400ms em CPU.
    reranker_candidates: int = 30
    # Limiar sobre o score do cross-encoder (saída da sigmoid, 0..1). SEPARADO de
    # `RELEVANCE_THRESHOLD` porque a escala é outra — não comparável com o ~0.82
    # do E5. Default 0.0 (não corta nada; o `TOP_K` ainda limita) porque o valor
    # real só sai da calibração da T-1; subir daqui é o entregável dessa rodada.
    # Só vale com `RERANKER_ENABLED=true`; senão vale `RELEVANCE_THRESHOLD`.
    reranker_threshold: float = 0.0

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

    # Redis para os contadores de rate limit (INF-10). VAZIO (padrão) = contador
    # em memória, correto só com 1 worker do uvicorn. Setado (ex.:
    # `redis://localhost:6379/0`, `rediss://...` com TLS) = contadores
    # compartilhados entre todos os workers/réplicas. Redis fora do ar → o rate
    # limit LIBERA a requisição com WARNING (ver app/api/ratelimit.py).
    redis_url: str = ""

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

    # Teto de tokens de SAÍDA por resposta, aplicado em todo provider da cadeia
    # (VET-3). Sem isto, nada trava a geração: rodadas reais de 2026-08-28
    # produziram 1450 (Q10) e 1729 (Q25) tokens de saída antes de o veto de
    # contexto pegar — e é a única defesa concreta contra "liste todos os
    # procedimentos / repita N vezes" (OWASP consumo ilimitado, ver
    # o grupo `owasp-2` de eval/perguntas/perguntas.jsonc).
    #
    # 1400: uma resposta de suporte acadêmico bem formada cabe com folga em
    # ~800–1000 tokens; a margem extra evita cortar no meio da frase a resposta
    # legítima longa (procedimento com muitos passos). `finish_reason="length"`
    # recorrente na telemetria é o sinal de que ficou apertado — subir aqui, não
    # deixar o aluno com texto truncado.
    #
    # As duas famílias de SDK usam nomes diferentes (`max_output_tokens` no
    # langchain-google-genai, `max_tokens` no da OpenAI); a tradução é feita em
    # cada provider, como já acontece com `llm_tentativas_por_provider`.
    llm_max_tokens: int = 1400

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
