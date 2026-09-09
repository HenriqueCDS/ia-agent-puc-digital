"""Orquestração da resposta: pergunta -> retrieval -> prompt -> LLM."""

import hashlib
import logging

from dataclasses import replace

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel

from app.agent.preprocess import normalize
from app.agent.prompts import (
    ANSWER_PROMPT,
    ANSWER_PROMPT_WEB,
    SEM_CONTEXTO,
    eh_fora_de_escopo,
    eh_insuficiente,
    eh_recusa_de_compliance,
    sem_marcador_topico,
    separar_topico,
)
from app.agent import guardrail
from app.agent.triagem import classificar
from app.agent.web_fallback import buscar_na_web, fonte_permitida, termo_bloqueado
from app.core import pii, telemetry
from app.core.config import CONTATO_PADRAO, settings
from app.core.models import Answer, Query, RetrievedChunk
from app.db.pre_retrieval_cache import get_cached_pre_retrieval, set_cached_pre_retrieval
from app.db.response_cache import get_cached_answer, set_cached_answer
from app.providers.chain import cadeia_para_modelo, get_chat_model
from app.retrieval.retriever import retrieve

logger = logging.getLogger(__name__)


def _conteudo_limitado(texto: str) -> str:
    """Corta uma fonte de contexto no teto de caracteres (ver o setting).

    Uma página densa de PDF entra no retrieval como um "chunk" só (o splitter
    não divide página — ver `config.embedding_model`), e a busca web às vezes
    devolve um `body` de milhares de caracteres. Cinco desses no mesmo prompt
    estouram o limite de token por requisição de provider de tier gratuito
    (HTTP 413). O corte é por caractere, no fim, com marca — perder o rodapé de
    uma página é melhor que a requisição inteira falhar.
    """
    teto = settings.prompt_context_item_max_chars
    if teto and len(texto) > teto:
        return texto[:teto].rstrip() + "\n[...trecho truncado]"
    return texto


def _format_context(chunks: list[RetrievedChunk]) -> str:
    # KB-6 — a marca distingue conteúdo crawlado da allowlist (página pública,
    # não revisada) de PDF interno. O `SYSTEM` tem a regra que lê essa marca.
    return "\n\n---\n\n".join(
        f"[{'Fonte web pública indexada' if c.is_web else 'Fonte'}: {c.citation}]\n"
        f"{_conteudo_limitado(c.document.page_content)}"
        for c in chunks
    )


def _resolver_llm(query: Query, llm: BaseChatModel | None) -> BaseChatModel:
    """O LLM desta pergunta, na ordem: injetado > override da query > cadeia.

    O injetado vence sempre porque é o dublê dos testes e o ponto de extensão de
    quem usa o agente como biblioteca — um override vindo do request não pode
    passar por cima dele.

    Resolvido aqui, e não nos dois pontos de chamada, para que `query.modelo`
    valha igual no caminho da base e no da web. Espalhar isso seria repetir a
    mesma decisão em cada fonte de contexto nova (ver o PONTO DE EXTENSÃO em
    `_responder`), e é exatamente onde se esquece.
    """
    if llm is not None:
        return llm
    if query.modelo:
        return cadeia_para_modelo(query.modelo)
    return get_chat_model()


def _sem_pii(query: Query) -> Query:
    """Mascara identificador pessoal / credencial do TEXTO da pergunta antes de
    ele sair da máquina — o prompt do provedor de LLM (EUA) e a query da busca
    web (PII-1 / PII-2).

    `pii.mascarar` já rodava, mas só nos campos DERIVADOS e persistidos
    (`topico`, `erro`, `resposta`, em `telemetry.registrar`). A pergunta CRUA
    seguia para `llm.invoke` e `buscar_na_web` com CPF, RA, e-mail, telefone e a
    senha que o aluno cola no texto ("minha senha é Aluno@2026, não entra").

    A DETECÇÃO continua sobre o texto original: `telemetry.registrar` roda
    `pii.detectar` antes daqui, então `registro.pii` e o WARNING de auditoria
    não se perdem. Aqui é só contenção de saída.

    PII-3 — roda DEPOIS do guardrail e da triagem, não antes: os dois são `if`
    léxico sobre a pergunta e nenhum faz egress, então devem ver o texto ORIGINAL
    (um padrão futuro preso a um trecho que `pii.mascarar` consome — e-mail, ID —
    deixaria de casar em silêncio). Todo egress (`llm.invoke`, `buscar_na_web`)
    vem depois do retrieval, e daqui pra frente tudo já opera sobre a versão
    limpa. Ver a ordem em `_responder` e o teste que a trava.

    Mascarar, não recusar (decisão do backlog): "não consigo acessar, meu RA é
    [ra]" é perfeitamente respondível, e barrar toda pergunta com RA deixaria o
    agente inútil — é a metade das perguntas de acesso. `dataclasses.replace`
    em vez de mutar: o objeto original ainda é o que a telemetria referencia.
    """
    limpo = pii.mascarar(query.text)
    if limpo == query.text:
        return query
    logger.info("PII na pergunta mascarada antes de sair para provedor/web")
    return replace(query, text=limpo)


# Diferenças que não mudam a resposta: caixa, espaço repetido e a pontuação
# final. Normalizar isso preserva o cache hit entre "Como envio a tarefa?" e
# "como envio a tarefa" sem misturar perguntas de fato diferentes.
_PONTUACAO_FINAL = " ?!.,;:"


def _normalizar_pergunta(texto: str) -> str:
    return " ".join(texto.split()).casefold().rstrip(_PONTUACAO_FINAL)


def _cache_key(query: Query, chunks: list[RetrievedChunk]) -> str:
    """Chave pelo TEXTO da pergunta + o conjunto de chunks recuperados.

    `document.id` é o id determinístico gravado na ingestão (`chunk_id` em
    ingestion/chunker.py) — o PGVector devolve esse mesmo id em cada busca.
    Reingerir um arquivo alterado muda os ids recuperados e invalida a chave
    sozinho, sem precisar de nenhuma limpeza manual de cache.

    T2.4 — a pergunta normalizada entra na chave. Antes, a chave era só
    `assunto + ids`, apostando em "mesmo conjunto de chunks ⇒ mesma resposta".
    Isso vale para paráfrase, e SÓ para paráfrase: com `top_k=5` e limiar 0.35,
    "como envio uma tarefa no Canvas?" e "onde vejo a nota da tarefa no Canvas?"
    plausivelmente recuperam os mesmos 5 chunks — e a segunda recebia a resposta
    da primeira. Servir a resposta de outra pergunta é o pior modo de falha
    possível num agente de suporte: parece certo e não deixa rastro.

    O custo é hit rate: paráfrase que não sobrevive à normalização acima passa
    a pagar uma chamada ao LLM. Trade escolhido de propósito.

    `query.modelo` também entra na chave (sempre, mesmo quando vazio) — é o que
    torna `settings.modelo_override_cache_enabled` seguro de ligar: um override
    (`groq:x`) nunca compartilha entrada com a cadeia normal nem com outro
    override (`gemini:y`). Sem isso, ligar o switch reintroduziria exatamente o
    bug que desligá-lo por padrão evitava — servir a resposta de um modelo para
    a pergunta feita a outro.
    """
    ids = sorted(c.document.id or "" for c in chunks)
    base = (
        f"{_normalizar_pergunta(query.text)}|{query.assunto or ''}|"
        f"{','.join(ids)}|{query.modelo or ''}"
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# Teto do trecho de cada fonte guardado no cache pré-retrieval. O
# `page_content` completo só serve para montar o prompt do LLM — que o hit
# pré-retrieval justamente pula. Guardar ~400 chars mantém o `scripts/ask.py
# --debug` legível sem inchar a linha do JSONB.
_TRECHO_FONTE_MAX = 400


def _pre_retrieval_cache_key(query: Query) -> str:
    """Chave do cache PRÉ-RETRIEVAL: só pergunta normalizada + assunto, sem os
    ids dos chunks (que não existem antes do retrieval — esse é o ponto).

    `query` aqui já passou por `_sem_pii`, igual em `_cache_key`: o texto que
    entra no hash é o mascarado, então PII nunca vira chave. Sem `query.modelo`
    na chave de propósito — o caminho que usa este cache já exige
    `query.modelo is None` (ver `_responder`).
    """
    base = f"{_normalizar_pergunta(query.text)}|{query.assunto or ''}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _fonte_para_json(chunk: RetrievedChunk) -> dict:
    """Serializa um `RetrievedChunk` para o `fontes` JSONB do cache pré-retrieval.

    Guarda o suficiente para reconstruir o que roda DEPOIS de `answer()`:
    `schemas._fonte_para_source_out` (lê `metadata` e `score`), `RetrievedChunk.
    citation`/`is_web` (lê `metadata`) e o `--debug` da CLI (lê um trecho do
    `page_content`). O texto completo do chunk não é guardado — ver
    `_TRECHO_FONTE_MAX`.
    """
    return {
        "id": chunk.document.id,
        "page_content": chunk.document.page_content[:_TRECHO_FONTE_MAX],
        "metadata": chunk.document.metadata,
        "score": chunk.score,
        "score_bruto": chunk.score_bruto,
    }


def _fonte_de_json(dados: dict) -> RetrievedChunk:
    """Inverso de `_fonte_para_json`."""
    return RetrievedChunk(
        document=Document(
            id=dados.get("id"),
            page_content=dados.get("page_content", ""),
            metadata=dados.get("metadata") or {},
        ),
        score=dados["score"],
        score_bruto=dados.get("score_bruto"),
    )


def _registrar_scores(registro: telemetry.Registro, chunks: list[RetrievedChunk]) -> None:
    """Preenche os campos de qualidade do retrieval (M5 / RET-3) a partir dos
    chunks — a MESMA lógica no caminho normal e no hit pré-retrieval (onde os
    chunks vêm da entrada cacheada, com os scores da vez em que foi gerada)."""
    registro.n_chunks = len(chunks)
    registro.score_top = round(chunks[0].score, 4) if chunks else None
    # Dispersão do top-k junto do topo: é o par que permite testar a margem
    # relativa depois, sem guardar os k scores. Ver `telemetry.Registro`.
    if chunks:
        registro.score_min = round(chunks[-1].score, 4)
        registro.score_mean = round(sum(c.score for c in chunks) / len(chunks), 4)
        # RET-3 — quando o reranker rodou, `chunks[*].score` é do cross-encoder
        # (outra escala); `score_bruto` traz o score de E5 do 1º estágio, e é o
        # que mantém a série `score_top` histórica comparável.
        if chunks[0].score_bruto is not None:
            registro.reranker_aplicado = True
            registro.score_top_bruto = round(chunks[0].score_bruto, 4)


def _rotulo_do_modelo(registro: telemetry.Registro) -> str:
    """`"provider:modelo"` que gerou esta resposta, para gravar junto do cache.

    `registro.provider` só é preenchido quando a resposta passou pela
    `ProviderChain` (o carimbo vem de `chain._carimbar`, lido em
    `Registro.somar_tokens`) — que é o caminho real da aplicação. Sem ele
    (dublê de teste, LLM injetado direto), sobra só `chat_model`, que nesse
    caso é o valor configurado (`settings.chat_model`) passado para
    `telemetry.registrar` no início de `answer()`.
    """
    if registro.provider:
        return f"{registro.provider}:{registro.chat_model}"
    return registro.chat_model


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


def _encaminhar_por_guardrail(registro: telemetry.Registro) -> Answer:
    """Desfecho do guardrail para um abuso reconhecido TARDE — o modelo emitiu
    `#FORA_DE_ESCOPO#` (regra do `SYSTEM`, TRI-4) porque o léxico de entrada não
    pegou a paráfrase / a injeção indireta.

    Mesmo texto e rótulo do guardrail de entrada; `recusa_modelo=True` porque a
    causa é a mesma do VET-2 e a pauta é calibrar `guardrail._PADROES`, não
    indexar. Chamado dos dois vetos (`_tentar_base`, `_responder_pela_web`) —
    antes do veto de contexto, para o abuso não ser confundido com "a base não
    cobre" — e da rede final de `answer()`.
    """
    registro.recusa_modelo = True
    _registrar_assunto(registro, guardrail.ASSUNTO, "guardrail")
    return Answer(text=CONTATO_PADRAO, sources=[], grounded=False, origem="encaminhado")


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

    llm = _resolver_llm(query, llm)
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

    # TRI-4 — antes do veto de contexto: o pedido é abuso que o guardrail léxico
    # não pegou e o modelo o marcou. Encaminha como o guardrail, não como "a web
    # não cobriu".
    if eh_fora_de_escopo(texto):
        logger.warning("busca externa: modelo marcou o pedido como fora de escopo")
        return _encaminhar_por_guardrail(registro)

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

        # TRI-4 — rede FINAL para o marcador `#FORA_DE_ESCOPO#`: os dois vetos
        # (`_tentar_base`, `_responder_pela_web`) já o checam antes do veto de
        # contexto; esta é a garantia de funil único para uma fonte de contexto
        # futura que esqueça de checar (mesmo papel do `eh_insuficiente` abaixo).
        if resultado.origem not in ("nenhuma", "encaminhado") and eh_fora_de_escopo(
            resultado.text
        ):
            logger.warning(
                "modelo marcou o pedido como fora de escopo (origem=%s) e o marcador "
                "escapou dos vetos; encaminhando",
                resultado.origem,
            )
            resultado = _encaminhar_por_guardrail(registro)

        # VET-2 — rede de segurança para a recusa de COMPLIANCE: o modelo se
        # negou a obedecer o pedido (quase sempre em inglês) e esse texto passou
        # como resposta. É um jailbreak/abuso que furou o guardrail léxico (que
        # não pega paráfrase nem outro idioma). Ao contrário do veto de contexto
        # abaixo, a web não ajudaria — então o desfecho é o mesmo do guardrail:
        # `origem="encaminhado"`, texto PT-BR de contato, e o assunto "fora de
        # escopo" com `assunto_origem="guardrail"` (que `scripts.lacunas` já
        # filtra). Checado aqui, no funil único, porque a recusa pode vir tanto
        # do ramo da base quanto do da web.
        if resultado.origem not in ("nenhuma", "encaminhado") and eh_recusa_de_compliance(
            resultado.text
        ):
            logger.warning(
                "modelo recusou o pedido em texto livre (origem=%s) — provável jailbreak "
                "que passou pelo guardrail; encaminhando",
                resultado.origem,
            )
            resultado = _encaminhar_por_guardrail(registro)

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

        # VET-6 — rede final para o marcador de tópico: `separar_topico` já o
        # extrai nos dois ramos, mas se o modelo o pôs numa forma que a extração
        # não reconheceu (inline, com markdown), ele não pode chegar ao aluno.
        # Reusa o mesmo padrão; sem marcador, o texto volta idêntico.
        texto_sem_marcador = sem_marcador_topico(resultado.text)
        if texto_sem_marcador != resultado.text:
            logger.warning(
                "marcador de tópico escapou de separar_topico (origem=%s)", resultado.origem
            )
            resultado = replace(resultado, text=texto_sem_marcador)

        registro.origem = resultado.origem
        registro.grounded = resultado.grounded
        # Só o canal de avaliação (scripts/eval_run.py, dataset sintético sem
        # aluno real) grava o texto da resposta — ver o campo `resposta` em
        # `telemetry.Registro` para o porquê de isto não valer para os demais.
        if registro.canal == "eval":
            registro.resposta = resultado.text
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
    # ANTES DA TRIAGEM: pedido de ataque/abuso (injeção de prompt, exfiltração de
    # segredo, execução não autorizada, código de exploit) é encaminhado para o
    # suporte sem tocar em RAG, web nem LLM. Mesmo desfecho da triagem
    # (`origem="encaminhado"`) — a decisão de 2026-08-27 foi consolidar toda
    # recusa nesse caminho em vez de criar uma origem nova. Ver o módulo
    # `guardrail` e eval/analise-telemetria-2026-08-27.md §8.
    if settings.guardrail_enabled:
        termo = guardrail.deve_encaminhar(query.text)
        if termo is not None:
            logger.info("guardrail: pergunta casou padrão de abuso (%r)", termo)
            _registrar_assunto(registro, guardrail.ASSUNTO, "guardrail")
            return Answer(
                text=CONTATO_PADRAO, sources=[], grounded=False, origem="encaminhado"
            )

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

    # PII-3 — só AGORA mascara CPF/RA/e-mail/telefone/senha do texto: guardrail
    # e triagem acima inspecionaram o texto original (são `if` léxico e não
    # fazem egress); daqui pra frente — retrieval, base, web — tudo opera sobre a
    # versão limpa, e cada fonte de contexto nova (ver o PONTO DE EXTENSÃO acima)
    # herda a contenção de graça. A detecção para a telemetria já rodou sobre o
    # original em `telemetry.registrar`.
    query = _sem_pii(query)

    # Cache PRÉ-RETRIEVAL: um hit numa pergunta já respondida pela base devolve a
    # resposta sem tocar em pgvector nem no cross-encoder — mata o `retrieve()`
    # inteiro, não só a chamada ao LLM (que o cache pós-retrieval já poupa). Só o
    # caminho `origem="base"` bem-sucedido é gravado (ver `_tentar_base`).
    #
    # Fica DEPOIS de guardrail/triagem/PII (léxico sobre o texto original, sem
    # egress) e ANTES do retrieval. A chave não tem os ids dos chunks, então a
    # invalidação é explícita: cada reingestão limpa a tabela
    # (`ingestion.pipeline._indexar_chunks`). Desligado no canal `eval` (a suíte
    # precisa medir retrieval + rerank) e com `query.modelo` (a chave não carrega
    # o modelo — mesma razão do cache pós-retrieval).
    pre_cache_key = None
    if (
        settings.cache_enabled
        and settings.pre_retrieval_cache_enabled
        and not query.modelo
        and registro.canal != "eval"
    ):
        pre_cache_key = _pre_retrieval_cache_key(query)
        cacheado = get_cached_pre_retrieval(pre_cache_key)
        if cacheado is not None:
            bruto, fontes_json = cacheado
            logger.info("cache pré-retrieval hit (%s...)", pre_cache_key[:8])
            registro.cache_hit = True
            registro.cache_pre_retrieval = True
            fontes = [_fonte_de_json(f) for f in fontes_json]
            _registrar_scores(registro, fontes)
            _registrar_assunto(registro, _assunto_dos_chunks(fontes), "metadata")
            texto, registro.topico = separar_topico(bruto)
            return Answer(text=texto, sources=fontes, grounded=True, cached=True)

    with telemetry.cronometro(registro, "ms_retrieve"):
        chunks = retrieve(query)

    _registrar_scores(registro, chunks)
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

    # TRI-3 — o guardrail de entrada só vê a pergunta. Um payload de injeção pode
    # vir DENTRO de um chunk recuperado (injeção indireta, OWASP LLM01). O corpus
    # é curado (PDFs + crawl da allowlist), então aqui é MEDIÇÃO, não bloqueio:
    # um hit é quase sempre falso positivo, e derrubar a resposta inteira seria
    # martelo demais. O `SYSTEM` já instrui o modelo a tratar CONTEXTO como dado;
    # este flag é para achar, na telemetria, o chunk que vale abrir à mão.
    if settings.guardrail_enabled:
        suspeito = next(
            (t for c in chunks
             if (t := guardrail.deve_encaminhar(c.document.page_content))),
            None,
        )
        if suspeito is not None:
            logger.warning(
                "contexto recuperado casou padrão de abuso (%r) — possível injeção "
                "indireta num chunk; a resposta segue, isto é só sinal",
                suspeito,
            )
            registro.contexto_suspeito = True

    resultado = _tentar_base(query, llm, chunks, registro, pre_cache_key)
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
    registro: telemetry.Registro,
    pre_cache_key: str | None = None,
) -> Answer | None:
    """Responde com o contexto da base, ou None se o LLM considerar insuficiente.

    None é o sinal para `_responder` tentar a busca externa antes da secretaria
    — mesmo veto que `_responder_pela_web` já aplicava do lado da web
    (`CONTEXTO_INSUFICIENTE`, ver prompts.py), agora também do lado da base.

    `pre_cache_key`, quando não-nulo, é onde a resposta bem-sucedida também é
    gravada no cache PRÉ-RETRIEVAL — para a próxima ocorrência da mesma pergunta
    pular o `retrieve()` inteiro. Só o desfecho de sucesso é gravado (veto de
    contexto e `#FORA_DE_ESCOPO#` não).
    """
    prompt = ANSWER_PROMPT

    # Cache pelo conjunto de chunks (ver _cache_key), checado só agora — depois
    # do guardrail em _responder — para não mascarar uma pergunta sem contexto e
    # não exigir GOOGLE_API_KEY configurada quando a resposta já está cacheada.
    #
    # `query.modelo` desliga o cache por padrão (nem lê nem grava): o override
    # existe para avaliar um modelo, e sem `MODELO_OVERRIDE_CACHE_ENABLED`
    # ligado, cachear a resposta do modelo experimental entregaria ela ao
    # próximo aluno com a mesma pergunta — mesma classe de bug que a T2.4
    # fechou ao colocar a pergunta na chave. Com o switch ligado, o próprio
    # modelo entra na chave (ver `_cache_key`), então cada override tem sua
    # entrada própria e não contamina a cadeia normal nem outro modelo.
    usar_cache = settings.cache_enabled and (
        not query.modelo or settings.modelo_override_cache_enabled
    )
    cache_key = _cache_key(query, chunks) if usar_cache else None
    bruto = None
    if cache_key:
        bruto = get_cached_answer(cache_key)
        registro.cache_hit = bruto is not None
        if bruto is not None:
            logger.info("cache hit (%s...)", cache_key[:8])

    if bruto is None:
        llm = _resolver_llm(query, llm)
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
            set_cached_answer(cache_key, registro.assunto, bruto, _rotulo_do_modelo(registro))

    texto, registro.topico = separar_topico(bruto)

    # TRI-4 — antes do veto de contexto: o modelo marcou o pedido como abuso /
    # fora de escopo (regra do `SYSTEM`) porque o guardrail léxico não pegou.
    # Encaminha como o guardrail, não deixa virar "a base não cobre" → web.
    if eh_fora_de_escopo(texto):
        logger.warning("base: modelo marcou o pedido como fora de escopo (#FORA_DE_ESCOPO#)")
        return _encaminhar_por_guardrail(registro)

    if eh_insuficiente(texto):
        logger.info("base: LLM considerou os %d chunks recuperados insuficientes", len(chunks))
        registro.base_insuficiente = True
        return None

    # Cache PRÉ-RETRIEVAL da resposta bem-sucedida (ver `_pre_retrieval_cache_key`
    # e o hit em `_responder`). Grava mesmo num hit do cache pós-retrieval: ali o
    # retrieval + rerank ainda rodaram, e popular aqui elimina esse custo na
    # próxima vez — útil, por ex., logo depois de uma reingestão ter limpado só
    # este cache mas não o pós-retrieval (ids do doc inalterados).
    if pre_cache_key is not None:
        set_cached_pre_retrieval(
            pre_cache_key,
            _normalizar_pergunta(query.text),
            registro.assunto,
            bruto,
            [_fonte_para_json(c) for c in chunks],
            _rotulo_do_modelo(registro),
        )

    return Answer(text=texto, sources=chunks, grounded=True, cached=bool(registro.cache_hit))
