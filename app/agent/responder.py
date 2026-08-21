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
    eh_insuficiente,
    separar_topico,
)
from app.agent.triagem import classificar
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


# Diferenças que não mudam a resposta: caixa, espaço repetido e a pontuação
# final. Normalizar isso preserva o cache hit entre "Como envio a tarefa?" e
# "como envio a tarefa" sem misturar perguntas de fato diferentes.
_PONTUACAO_FINAL = " ?!.,;:"


def _normalizar_pergunta(texto: str) -> str:
    return " ".join(texto.split()).casefold().rstrip(_PONTUACAO_FINAL)


def _cache_key(query: Query, chunks: list[RetrievedChunk], alta_confianca: bool) -> str:
    """Chave pelo TEXTO da pergunta + o conjunto de chunks recuperados.

    `document.id` é o id determinístico gravado na ingestão (`chunk_id` em
    ingestion/chunker.py) — o PGVector devolve esse mesmo id em cada busca.
    Reingerir um arquivo alterado muda os ids recuperados e invalida a chave
    sozinho, sem precisar de nenhuma limpeza manual de cache.

    T2.4 — a pergunta normalizada entra na chave. Antes, a chave era só
    `assunto + alta_confiança + ids`, apostando em "mesmo conjunto de chunks ⇒
    mesma resposta". Isso vale para paráfrase, e SÓ para paráfrase: com
    `top_k=5` e limiar 0.35, "como envio uma tarefa no Canvas?" e "onde vejo a
    nota da tarefa no Canvas?" plausivelmente recuperam os mesmos 5 chunks — e
    a segunda recebia a resposta da primeira. Servir a resposta de outra
    pergunta é o pior modo de falha possível num agente de suporte: parece
    certo e não deixa rastro.

    O custo é hit rate: paráfrase que não sobrevive à normalização acima passa
    a pagar uma chamada ao LLM. Trade escolhido de propósito — a alternativa
    considerada (cachear só no ramo `alta_confianca`) preservaria mais hits mas
    deixaria o mesmo erro de pé no ramo comum, que é onde ele acontece.
    """
    ids = sorted(c.document.id or "" for c in chunks)
    base = f"{_normalizar_pergunta(query.text)}|{query.assunto or ''}|{alta_confianca}|{','.join(ids)}"
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
    if eh_insuficiente(texto):
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

        # Rede de segurança, e o único ponto por onde TODA resposta passa: se um
        # marcador de recusa escapou, ele vira o encaminhamento para a
        # secretaria em vez de ir cru para o aluno.
        #
        # Cobre um CAMINHO que não vetou, não uma forma de marcador que a
        # detecção não reconhece — `eh_insuficiente` é a mesma função dos vetos,
        # então o que ela não vê aqui ela também não viu lá. O valor está em ser
        # o funil único: hoje o veto está duplicado em `_tentar_base` e
        # `_responder_pela_web`, e cada fonte de contexto nova (tool calling,
        # APIs em tempo real — ver o PONTO DE EXTENSÃO em `_responder`) é mais
        # um lugar onde esquecer o veto é possível. Aqui não tem como esquecer.
        #
        # A forma do marcador é problema da detecção, e foi endurecida em
        # `prompts.eh_insuficiente` depois de um modelo real traduzir
        # "INSUFICIENTE" para "INSUFFICIENT" e o texto cru chegar ao aluno.
        if resultado.origem != "nenhuma" and eh_insuficiente(resultado.text):
            logger.warning(
                "marcador de recusa escapou do veto de origem=%s; encaminhando para a secretaria",
                resultado.origem,
            )
            registro.veto_escapou = True
            resultado = _encaminhar_para_secretaria()

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

    São dois caminhos: o normal (contexto da base indexada, ver `_tentar_base`) e
    o de fallback, em que a resposta vem de páginas públicas oficiais (ver
    `_responder_pela_web`). O fallback nunca é escolhido pelo LLM — o gatilho é
    um dos dois `if` abaixo: `not chunks` (retrieval não achou nada acima do
    limiar) ou `_tentar_base` devolvendo `None` (achou chunks, mas o LLM decidiu
    que nenhum cobre a pergunta).

    Antes dos dois vem a triagem por assunto (ver `app/agent/triagem.py`): a
    pergunta que é de outro departamento sai aqui, sem tocar em nenhuma das
    fontes.

    PONTO DE EXTENSÃO — quando entrarem APIs públicas consultadas em tempo real
    (calendário acadêmico, API do Canvas), elas viram mais uma fonte de contexto
    concatenada aqui. Com três ou mais fontes assim, o roteamento passa a valer
    como tool calling, tendo `retrieve` e `buscar_na_web` como tools.
    """
    # PRIMEIRA etapa, antes do retrieval: assunto de outro departamento não
    # deve nem tentar o RAG. Não é só economia de chamada — é o que impede o LLM
    # de responder sobre boleto/diploma a partir de um chunk fraco que passou
    # raspando no `relevance_threshold`. Ver o módulo de triagem.
    if settings.triagem_enabled:
        categoria = classificar(query.text)
        if categoria is not None:
            logger.info("triagem: assunto '%s' é de outro departamento", categoria.assunto)
            _registrar_assunto(registro, categoria.assunto, "triagem")
            return Answer(
                text=categoria.resposta, sources=[], grounded=False, origem="encaminhado"
            )

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

    resultado = _tentar_base(query, llm, chunks, alta_confianca, registro)
    if resultado is not None:
        return resultado

    # A base tinha chunks acima do limiar (por isso não caiu no `if not chunks`
    # acima), mas o próprio LLM decidiu que nenhum deles cobre a pergunta
    # (`CONTEXTO_INSUFICIENTE`) — mesmo roteador de antes da secretaria, só que
    # o motivo agora vem do LLM, não do retriever. É o gatilho que faltava: sem
    # ele, um chunk fraco (score só um pouco acima de `relevance_threshold`)
    # produzia um "não encontrei" em texto livre direto pro aluno, sem nunca
    # tentar a busca externa.
    if settings.web_fallback_enabled:
        return _responder_pela_web(query, llm, registro)
    return _encaminhar_para_secretaria()


def _tentar_base(
    query: Query,
    llm: BaseChatModel | None,
    chunks: list[RetrievedChunk],
    alta_confianca: bool,
    registro: telemetry.Registro,
) -> Answer | None:
    """Responde com o contexto da base, ou None se o LLM considerar insuficiente.

    None é o sinal para `_responder` tentar a busca externa antes da secretaria
    — mesmo veto que `_responder_pela_web` já aplicava do lado da web
    (`CONTEXTO_INSUFICIENTE`, ver prompts.py), agora também do lado da base.
    """
    prompt = ANSWER_PROMPT_ALTA_CONFIANCA if alta_confianca else ANSWER_PROMPT

    # Cache pelo conjunto de chunks (ver _cache_key), checado só agora — depois
    # do guardrail em _responder — para não mascarar uma pergunta sem contexto e
    # não exigir GOOGLE_API_KEY configurada quando a resposta já está cacheada.
    cache_key = _cache_key(query, chunks, alta_confianca) if settings.cache_enabled else None
    bruto = None
    if cache_key:
        bruto = get_cached_answer(cache_key)
        registro.cache_hit = bruto is not None
        if bruto is not None:
            logger.info("cache hit (%s...)", cache_key[:8])

    if bruto is None:
        llm = llm or get_chat_model()
        mensagens = prompt.format_messages(
            contexto=_format_context(chunks),
            pergunta=query.text,
        )
        with telemetry.cronometro(registro, "ms_llm"):
            resposta = llm.invoke(mensagens)
        registro.somar_tokens(resposta)
        bruto = str(resposta.content)

        # Guarda o texto COM o marcador: assim um cache hit recupera o tópico
        # junto, sem coluna nova e sem reclassificar (`separar_topico` roda de
        # novo na leitura). Isso vale também para um `bruto` igual a
        # `CONTEXTO_INSUFICIENTE`: um cache hit futuro repete o veto e volta a
        # tentar a web, em vez de mostrar o marcador cru para o aluno — a
        # checagem abaixo roda igual nos dois casos (cache hit ou chamada
        # nova).
        if cache_key:
            set_cached_answer(cache_key, registro.assunto, bruto)

    texto, registro.topico = separar_topico(bruto)

    if eh_insuficiente(texto):
        logger.info("base: LLM considerou os %d chunks recuperados insuficientes", len(chunks))
        registro.base_insuficiente = True
        return None

    return Answer(text=texto, sources=chunks, grounded=True)
