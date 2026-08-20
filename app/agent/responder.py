"""Orquestração da resposta: pergunta -> retrieval -> prompt -> LLM."""

import hashlib
import logging

from langchain_core.language_models import BaseChatModel

from app.agent.preprocess import normalize
from app.agent.prompts import (
    ANSWER_PROMPT,
    ANSWER_PROMPT_ALTA_CONFIANCA,
    ANSWER_PROMPT_WEB,
    SEM_CONTEXTO,
    WEB_INSUFICIENTE,
    separar_topico,
)
from app.agent.web_fallback import buscar_na_web, fonte_permitida, termo_bloqueado
from app.core import telemetry
from app.core.config import settings
from app.core.models import Answer, Query, RetrievedChunk
from app.db.response_cache import get_cached_answer, set_cached_answer
from app.providers.gemini import get_chat_model
from app.retrieval.retriever import is_exact_match, retrieve

logger = logging.getLogger(__name__)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n---\n\n".join(
        f"[Fonte: {c.citation}]\n{c.document.page_content}" for c in chunks
    )


def _cache_key(query: Query, chunks: list[RetrievedChunk], alta_confianca: bool) -> str:
    """Chave pelo conjunto de chunks recuperados, não pelo texto da pergunta.

    `document.id` é o id determinístico gravado na ingestão (`chunk_id` em
    ingestion/chunker.py) — o PGVector devolve esse mesmo id em cada busca.
    Perguntas parafraseadas que recuperam o mesmo topo caem na mesma chave, e
    reingerir um arquivo alterado muda os ids recuperados e invalida a chave
    sozinho, sem precisar de nenhuma limpeza manual de cache.
    """
    ids = sorted(c.document.id or "" for c in chunks)
    base = f"{query.assunto or ''}|{alta_confianca}|{','.join(ids)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _assunto_dos_chunks(chunks: list[RetrievedChunk]) -> str | None:
    """Assunto do documento que respondeu — gravado na ingestão (pipeline._enrich).

    Preferido a qualquer inferência: diz qual pasta de fato respondeu, em vez de
    um palpite sobre a pergunta. Percorre na ordem de relevância e para no
    primeiro que tiver o campo.
    """
    return next((c.document.metadata.get("assunto") for c in chunks
                 if c.document.metadata.get("assunto")), None)


def _assunto_da_web(chunks: list[RetrievedChunk]) -> str | None:
    """Assunto pela allowlist: cada FonteWeb declara o seu (ver config.WEB_ALLOWLIST).

    A URL já foi revalidada contra a allowlist antes de chegar aqui, então isto
    é só reler o casamento — nenhuma chamada, nenhum token.
    """
    for chunk in chunks:
        fonte = fonte_permitida(chunk.document.metadata.get("source_name", ""))
        if fonte and fonte.assunto:
            return fonte.assunto
    return None


def _registrar_assunto(registro: telemetry.Registro, valor: str | None, origem: str) -> None:
    """Só preenche se o usuário não informou — o que ele passou tem precedência."""
    if registro.assunto_origem == "informado" or not valor:
        return
    registro.assunto = valor
    registro.assunto_origem = origem


def _encaminhar_para_secretaria() -> Answer:
    return Answer(text=SEM_CONTEXTO, sources=[], grounded=False, origem="nenhuma")


def _responder_pela_web(
    query: Query, llm: BaseChatModel | None, registro: telemetry.Registro
) -> Answer:
    """Fallback: sintetiza a resposta a partir de páginas públicas oficiais.

    `grounded` continua False mesmo quando a web responde — a informação de fato
    não estava na base, e é esse sinal que revela qual documento falta indexar.
    Quem quer distinguir os dois casos lê `Answer.origem`.

    Sem cache aqui de propósito: a `_cache_key` do caminho normal depende de ids
    de chunk, que não existem para resultado de web, e conteúdo externo muda sem
    aviso enquanto a tabela `resposta_cache` não tem TTL. É o caminho raro.
    """
    with telemetry.cronometro(registro, "ms_web"):
        resultados = buscar_na_web(query)
    if not resultados:
        return _encaminhar_para_secretaria()

    llm = llm or get_chat_model()
    mensagens = ANSWER_PROMPT_WEB.format_messages(
        contexto=_format_context(resultados),
        pergunta=query.text,
    )
    with telemetry.cronometro(registro, "ms_llm"):
        resposta = llm.invoke(mensagens)
    registro.somar_tokens(resposta)

    # Antes de qualquer outro uso do texto: o marcador de tópico é do sistema e
    # não pode chegar ao aluno nem atrapalhar o veto logo abaixo (que testa o
    # início da string).
    texto, topico = separar_topico(str(resposta.content))
    registro.topico = topico
    _registrar_assunto(registro, _assunto_da_web(resultados), "allowlist")

    # Último filtro, e o único que enxerga a pergunta e os trechos juntos: o
    # prompt autoriza o modelo a vetar snippets que passaram pelos cortes
    # anteriores mas não respondem de fato.
    if texto.upper().startswith(WEB_INSUFICIENTE):
        logger.info("busca externa: LLM considerou os trechos insuficientes")
        registro.web_insuficiente = True
        return _encaminhar_para_secretaria()

    return Answer(text=texto, sources=resultados, grounded=False, origem="web")


def answer(query: Query, llm: BaseChatModel | None = None) -> Answer:
    """Responde à pergunta e registra uma linha de telemetria por chamada.

    A instrumentação mora aqui, e não dentro de `retrieve`/`buscar_na_web`,
    porque só este nível vê a pergunta inteira: custo, caminho e qualidade do
    retrieval na mesma linha. Ver `app/core/telemetry.py` — o texto da pergunta
    nunca é registrado, só `assunto` e hash.

    `_responder` faz o trabalho; este envelope existe para que `origem` e
    `grounded` sejam gravados uma vez só, sem repetir em cada `return`.
    """
    query = normalize(query)

    with telemetry.registrar(
        assunto=query.assunto, pergunta=query.text, chat_model=settings.chat_model
    ) as registro:
        resultado = _responder(query, llm, registro)
        registro.origem = resultado.origem
        registro.grounded = resultado.grounded
        return resultado


def _responder(
    query: Query, llm: BaseChatModel | None, registro: telemetry.Registro
) -> Answer:
    """Responde à pergunta usando apenas o que foi recuperado da base.

    Os passos ficam explícitos (em vez de uma chain fechada) porque é aqui que a
    qualidade do RAG é depurada: dá para inspecionar o contexto recuperado antes
    da chamada ao LLM.

    São dois caminhos: o normal (contexto da base indexada) e o de fallback, em
    que o retrieval não achou nada e a resposta vem de páginas públicas oficiais
    (ver `_responder_pela_web`). O segundo nunca é escolhido pelo LLM — o gatilho
    é o `if not chunks` abaixo.

    PONTO DE EXTENSÃO — quando entrarem APIs públicas consultadas em tempo real
    (calendário acadêmico, API do Canvas), elas viram mais uma fonte de contexto
    concatenada aqui. Com três ou mais fontes assim, o roteamento passa a valer
    como tool calling, tendo `retrieve` e `buscar_na_web` como tools.
    """
    with telemetry.cronometro(registro, "ms_retrieve"):
        chunks = retrieve(query)

    registro.n_chunks = len(chunks)
    registro.score_top = round(chunks[0].score, 4) if chunks else None
    _registrar_assunto(registro, _assunto_dos_chunks(chunks), "metadata")

    # Guardrail: em suporte acadêmico, não responder é melhor que alucinar um
    # procedimento. Também sinaliza quais documentos faltam na base.
    #
    # O guardrail deixou de ser um beco sem saída e virou um roteador: antes de
    # encaminhar para a secretaria, tenta as páginas públicas oficiais. Só este
    # ramo paga a latência da busca externa — as perguntas que a base responde
    # bem seguem pelo caminho de sempre.
    if not chunks:
        # Sem chunks não há metadata; o termo da blocklist é o único rótulo
        # disponível de graça, e cobre justamente o caminho `origem="nenhuma"`,
        # que não faz chamada nenhuma ao LLM (logo, não tem tópico).
        _registrar_assunto(registro, termo_bloqueado(query.text), "blocklist")
        if settings.web_fallback_enabled:
            return _responder_pela_web(query, llm, registro)
        return _encaminhar_para_secretaria()

    alta_confianca = is_exact_match(chunks)
    registro.alta_confianca = alta_confianca
    if alta_confianca:
        logger.info("alta confiança: %.2f e %.2f nas 2 fontes do topo", chunks[0].score, chunks[1].score)
    prompt = ANSWER_PROMPT_ALTA_CONFIANCA if alta_confianca else ANSWER_PROMPT

    # Cache pelo conjunto de chunks (ver _cache_key), checado só agora — depois
    # do guardrail acima — para não mascarar uma pergunta sem contexto e não
    # exigir GOOGLE_API_KEY configurada quando a resposta já está cacheada.
    cache_key = _cache_key(query, chunks, alta_confianca) if settings.cache_enabled else None
    if cache_key:
        cache_hit = get_cached_answer(cache_key)
        registro.cache_hit = cache_hit is not None
        if cache_hit is not None:
            logger.info("cache hit (%s...)", cache_key[:8])
            texto, registro.topico = separar_topico(cache_hit)
            return Answer(text=texto, sources=chunks, grounded=True)

    llm = llm or get_chat_model()
    mensagens = prompt.format_messages(
        contexto=_format_context(chunks),
        pergunta=query.text,
    )
    with telemetry.cronometro(registro, "ms_llm"):
        resposta = llm.invoke(mensagens)
    registro.somar_tokens(resposta)

    bruto = str(resposta.content)
    texto, registro.topico = separar_topico(bruto)

    # Guarda o texto COM o marcador: assim um cache hit recupera o tópico junto,
    # sem coluna nova e sem reclassificar (`separar_topico` roda de novo na
    # leitura). Entradas gravadas antes desta versão não têm marcador e
    # simplesmente devolvem tópico nulo.
    if cache_key:
        set_cached_answer(cache_key, registro.assunto, bruto)

    return Answer(text=texto, sources=chunks, grounded=True)
