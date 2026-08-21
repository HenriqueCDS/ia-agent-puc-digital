"""Busca externa restrita — acionada só quando o retrieval não devolve nada.

Não é uma tool no sentido de function calling: o gatilho aqui não é ambíguo
("o retrieval voltou vazio" é um `if`), então quem decide é o
`app/agent/responder.py`, não o LLM. A assinatura de `buscar_na_web` já é a de
uma tool, para o dia em que houver várias e a escolha entre elas virar de fato
uma decisão do modelo.

A restrição aos domínios oficiais tem duas camadas:

1. direcionar — uma query `site:<host> <termos>` por entrada da allowlist,
   porque `site:a OR site:b` no DuckDuckGo devolve resultado fora do escopo
   em silêncio;
2. validar — toda URL devolvida é reconferida contra (host, path_prefix) da
   allowlist antes de virar contexto.

A camada 2 é a que garante a restrição; a 1 é só recall. Nada que venha do
DuckDuckGo é confiado sem passar pela 2.

A filtragem de relevância reusa as embeddings locais que já estão carregadas
(`providers.gemini.get_embeddings`) — custo zero de API e critério consistente
com o corte por limiar do retriever.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from urllib.parse import parse_qs, unquote, urlparse

from langchain_core.documents import Document

from app.agent.triagem import classificar
from app.core.config import WEB_ALLOWLIST, FonteWeb, settings
from app.core.models import Query, RetrievedChunk
from app.providers.gemini import get_embeddings

logger = logging.getLogger(__name__)


def assunto_bloqueado(pergunta: str) -> bool:
    """Registro acadêmico e financeiro nunca vão para a busca externa.

    A resposta certa para "qual minha nota" ou "como faço a rematrícula" depende
    do caso concreto do aluno; uma página pública responderia com confiança
    aparente e conteúdo errado. Bloquear aqui devolve o encaminhamento para a
    secretaria, que é a resposta correta para esse grupo de perguntas.

    Redundante com a triagem na entrada (`responder._responder` já encaminha
    esses assuntos antes do retrieval, então eles nem chegam aqui) e mantido de
    propósito: é defesa em profundidade para o caso de `TRIAGEM_ENABLED=false`
    ou de um caminho futuro que chame `buscar_na_web` direto, e custa uma
    comparação de substring.
    """
    return classificar(pergunta) is not None


def termo_bloqueado(pergunta: str) -> str | None:
    """Assunto fora de escopo que a pergunta casou, ou None.

    Mesma checagem de `assunto_bloqueado`, devolvendo o rótulo em vez de um
    bool: é o único rótulo de assunto disponível para o caminho que não chama o
    LLM (`origem="nenhuma"`), e sai sem custo nenhum. Ver `app/core/telemetry.py`.
    """
    categoria = classificar(pergunta)
    return categoria.assunto if categoria else None


def _fontes_para(assunto: str | None) -> tuple[FonteWeb, ...]:
    """Filtra a allowlist pelo assunto da pergunta — menos queries, menos latência."""
    if not assunto:
        return WEB_ALLOWLIST
    do_assunto = tuple(f for f in WEB_ALLOWLIST if f.assunto == assunto)
    return do_assunto or WEB_ALLOWLIST


def _desembrulhar(url: str) -> str:
    """Resolve o redirect do DuckDuckGo (`duckduckgo.com/l/?uddg=<url>`).

    Sem isso, o host visto pela validação seria o do DuckDuckGo e todo resultado
    seria descartado.
    """
    partes = urlparse(url)
    if "duckduckgo.com" in (partes.hostname or "") and partes.path.startswith("/l/"):
        destino = parse_qs(partes.query).get("uddg")
        if destino:
            return unquote(destino[0])
    return url


def fonte_permitida(url: str) -> FonteWeb | None:
    """Devolve a entrada da allowlist que autoriza a URL, ou None."""
    partes = urlparse(_desembrulhar(url))
    if partes.scheme not in ("http", "https"):
        return None

    host = (partes.hostname or "").lower().removeprefix("www.")
    caminho = (partes.path or "/").lower()

    for fonte in WEB_ALLOWLIST:
        alvo = fonte.host.lower().removeprefix("www.")
        casa_host = host == alvo or (fonte.subdominios and host.endswith("." + alvo))
        if casa_host and caminho.startswith(fonte.path_prefix.lower()):
            return fonte
    return None


def _buscar_em(fonte: FonteWeb, pergunta: str) -> list[dict]:
    """Uma consulta ao DuckDuckGo, restrita a uma fonte. Nunca levanta exceção.

    O DuckDuckGo não tem API oficial: `ddgs` raspa HTML e pode falhar por rate
    limit ou por mudança do upstream. Uma busca que falha vira lista vazia — o
    agente cai no encaminhamento para a secretaria, nunca em erro para o usuário.
    """
    from ddgs import DDGS  # import local: sem busca externa, a lib nem é carregada

    consulta = " ".join(p for p in (f"site:{fonte.host}", fonte.termos, pergunta) if p)
    try:
        return DDGS().text(
            consulta,
            region=settings.web_search_region,
            safesearch="moderate",
            max_results=settings.web_search_max_results,
            # Explícito: o default do ddgs é "auto", que também consulta Yahoo,
            # Brave, Startpage e Wikipedia (ver settings.web_search_backend).
            backend=settings.web_search_backend,
        )
    except Exception as exc:  # noqa: BLE001 - degradar é o comportamento desejado
        logger.warning("busca externa falhou em %s: %s", fonte.host, exc)
        return []


def _coletar(fontes: tuple[FonteWeb, ...], pergunta: str) -> list[dict]:
    """Consulta as fontes em paralelo, dentro do orçamento de tempo total.

    Estourado o orçamento, devolve o que já chegou em vez de esperar: uma
    resposta parcial rápida vale mais que a completa lenta neste caminho, que
    por definição já é o caminho degradado.
    """
    brutos: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=len(fontes))
    try:
        futuros = [pool.submit(_buscar_em, fonte, pergunta) for fonte in fontes]
        for futuro in as_completed(futuros, timeout=settings.web_search_timeout):
            brutos.extend(futuro.result())
    except FuturesTimeout:
        logger.warning("busca externa estourou %.1fs", settings.web_search_timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return brutos


def _validos(brutos: list[dict]) -> list[dict]:
    """Aplica a allowlist e remove duplicatas, preservando a ordem de chegada."""
    vistos: set[str] = set()
    saida: list[dict] = []
    for bruto in brutos:
        url = _desembrulhar(str(bruto.get("href") or bruto.get("url") or ""))
        snippet = str(bruto.get("body") or "").strip()
        if not url or not snippet or url in vistos:
            continue
        if fonte_permitida(url) is None:
            logger.debug("descartado (fora da allowlist): %s", url)
            continue
        vistos.add(url)
        saida.append(
            {
                "url": url,
                "titulo": str(bruto.get("title") or "").strip(),
                "snippet": snippet,
            }
        )
    return saida


def _cosseno(a: list[float], b: list[float]) -> float:
    norma = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if not norma:
        return 0.0
    return max(0.0, sum(x * y for x, y in zip(a, b)) / norma)


def _relevantes(pergunta: str, resultados: list[dict]) -> list[RetrievedChunk]:
    """Pontua cada snippet contra a pergunta e corta pelo limiar.

    Mesmo modelo de embedding da base, já em memória: nenhuma chamada de API e
    nenhum token gasto para descobrir que a busca não trouxe nada de útil.
    """
    embeddings = get_embeddings()
    vetor_pergunta = embeddings.embed_query(pergunta)
    vetores = embeddings.embed_documents([r["snippet"] for r in resultados])

    chunks = [
        RetrievedChunk(
            document=Document(
                id=resultado["url"],
                page_content=f"{resultado['titulo']}\n{resultado['snippet']}".strip(),
                # `source_name` é o que vira RetrievedChunk.citation — para a web,
                # a citação é a própria URL. Assim Answer.sources e a resposta da
                # API seguem com o mesmo formato do caminho normal. `titulo` só
                # é consumido pela API (app/api/schemas.SourceOut) — o texto
                # que o LLM lê continua sendo `page_content`, sem essa metadata.
                metadata={
                    "source_name": resultado["url"],
                    "origem": "web",
                    "titulo": resultado["titulo"],
                },
            ),
            score=_cosseno(vetor_pergunta, vetor),
        )
        for resultado, vetor in zip(resultados, vetores)
    ]

    acima = [c for c in chunks if c.score >= settings.web_relevance_threshold]
    acima.sort(key=lambda c: c.score, reverse=True)
    return acima[: settings.web_max_chunks]


def _buscar_nas_fontes(fontes: tuple[FonteWeb, ...], query: Query) -> list[RetrievedChunk]:
    """Uma rodada completa (coleta + allowlist + corte por relevância) num conjunto de fontes."""
    resultados = _validos(_coletar(fontes, query.text))
    if not resultados:
        logger.info(
            "busca externa: nenhum resultado dentro da allowlist (%s)",
            ", ".join(f.host for f in fontes),
        )
        return []

    relevantes = _relevantes(query.text, resultados)
    logger.info(
        "busca externa: %d resultados na allowlist, %d acima do limiar (%s)",
        len(resultados),
        len(relevantes),
        ", ".join(f.host for f in fontes),
    )
    return relevantes


def buscar_na_web(query: Query) -> list[RetrievedChunk]:
    """Resultados de páginas oficiais relevantes para a pergunta, ou lista vazia.

    Lista vazia é resposta legítima e frequente: significa "não achei nada
    confiável", e quem chama encaminha para a secretaria.

    Duas rodadas quando há assunto: a primeira restrita às fontes daquele
    assunto (menos queries, menos latência — ver `_fontes_para`); se ela não
    achar nada, uma segunda rodada consulta a allowlist inteira antes de
    desistir. O assunto da pergunta pode estar errado ou a resposta pode estar
    numa fonte de outro assunto — restringir para sempre à primeira tentativa
    descartaria esses casos em silêncio.
    """
    if assunto_bloqueado(query.text):
        logger.info("busca externa bloqueada por assunto sensível")
        return []

    fontes = _fontes_para(query.assunto)
    relevantes = _buscar_nas_fontes(fontes, query)

    if not relevantes and fontes is not WEB_ALLOWLIST:
        logger.info("busca externa: nada em %s, ampliando para a allowlist inteira",
                     ", ".join(f.host for f in fontes))
        relevantes = _buscar_nas_fontes(WEB_ALLOWLIST, query)

    return relevantes
