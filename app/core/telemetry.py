"""Telemetria: uma linha JSON por pergunta respondida.

Fica em `answer()` (e não espalhada por retriever/providers) porque só o
orquestrador vê a pergunta inteira: quanto custou, por qual caminho saiu e com
que qualidade o retrieval respondeu. Instrumentar módulo a módulo obrigaria a
repetir o mesmo trabalho em cada ponto de extensão futuro (tool calling, APIs
públicas em tempo real).

Sem dependência nova de propósito: é `logging` + `json`. O mesmo dicionário vai
para dois destinos independentes: a linha em stderr (sempre) e, quando o
entrypoint liga, a tabela `telemetria` no Postgres com retenção de 7 dias (ver
`app/db/telemetry_store.py`). O core não conhece o banco — a persistência entra
por injeção, via `configurar_persistencia`.

PRIVACIDADE — o texto da pergunta NUNCA entra no registro, só `assunto` e um
hash truncado. Perguntas de aluno passam por assuntos sensíveis (ver
`ENCAMINHAMENTOS` em app/core/config.py: boleto, matrícula, bolsa). O hash ainda
serve ao propósito principal: agrupar perguntas repetidas que a base não
respondeu, para descobrir qual documento falta indexar.

Não basta não gravar a pergunta (T3.4): `topico` é escrito pelo LLM A PARTIR
dela e pode repetir um RA ou CPF que estava lá. Todo campo derivado passa por
`pii.mascarar` no momento da emissão — um funil só, em `registrar`, pelo mesmo
motivo da rede de segurança em `responder.answer`: cada ponto de extensão futuro
é mais um lugar onde dá para esquecer. `Registro.pii` guarda só o RÓTULO do que
foi encontrado ("cpf", "ra"), nunca o valor, e é o que torna o vazamento
contável em vez de invisível.

Ler os registros:
    python -m scripts.ask "..." 2> telemetria.jsonl
    jq -s 'map(select(.origem=="nenhuma")) | group_by(.pergunta_hash)' telemetria.jsonl
"""

import hashlib
import json
import logging
import sys
import time

from app.core import pii
from app.core.config import settings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

# Logger próprio, sem propagar: o registro estruturado não se mistura com os
# INFO em texto do resto do app, e ligar telemetria não liga log de tudo.
#
# Este stream contém SÓ linhas JSON — nada mais escreve nele. É o que permite
# `2>> telemetria.jsonl` e ler o arquivo com qualquer parser sem tratar exceção.
# Falha *da própria* telemetria vai para `logger_interno`, que é o logger comum
# do módulo: um traceback no meio do JSONL quebraria quem lê o arquivo.
logger = logging.getLogger("telemetria")
logger_interno = logging.getLogger(__name__)

# Quem originou a pergunta. ContextVar em vez de parâmetro porque é do
# entrypoint, não da lógica — `answer()` não deveria precisar saber.
_canal: ContextVar[str] = ContextVar("canal", default="desconhecido")

# Id da request HTTP que originou a pergunta (T3.2). Mesma razão do `_canal`
# para ser ContextVar: é do entrypoint, e a CLI simplesmente não tem um.
# Default `None` significa "não veio de HTTP", que é informação, não falta dela.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Registro da pergunta em curso. ContextVar pelo mesmo motivo de `_canal`:
# é uma sub-etapa que roda DENTRO de `answer()` mas fora do orquestrador
# (hoje só o rerank em `app/retrieval/retriever.py`, quando o 2º estágio está
# ligado) que precisa somar sua latência a um campo do `Registro` — sem
# recebê-lo por parâmetro e sem que `retriever`/`reranker` passem a montar e
# emitir telemetria. `None` fora de `answer()`; aí `etapa()` é um no-op.
_registro_atual: ContextVar["Registro | None"] = ContextVar("registro_atual", default=None)

# Destino adicional do registro (hoje: Postgres). `None` = só o log em stderr.
_persistir: Callable[[dict], None] | None = None


def set_canal(nome: str) -> None:
    """Chamado pelos entrypoints (CLI, /ask)."""
    _canal.set(nome)


def set_request_id(valor: str | None) -> None:
    """Chamado pela borda HTTP (T3.2).

    É o que liga uma reclamação pontual ("recebi resposta errada, o id era
    a3f2...") à linha exata da tabela `telemetria`: o mesmo id sai no header
    `X-Request-Id`, no corpo da resposta e no registro. Sem ele, correlacionar
    dependia de casar horário com hash de pergunta na mão.
    """
    _request_id.set(valor)


def configurar_persistencia(sink: Callable[[dict], None] | None) -> None:
    """Registra quem guarda o registro além do log. Ver `db/telemetry_store.habilitar`.

    Inversão de dependência de propósito: assim `app/core` não importa `app/db`,
    e rodar o agente sem banco (testes, uso como biblioteca) continua sendo o
    comportamento padrão — basta não chamar isto.
    """
    global _persistir
    _persistir = sink


def persistencia_atual() -> Callable[[dict], None] | None:
    """O sink instalado agora, ou `None`. Existe para poder ENCADEAR um destino.

    Sem isto, quem quisesse observar os registros (é o caso de
    `scripts/eval_run.py`, que quer o registro da pergunta no arquivo de
    resultado) teria de escolher entre o Postgres e a sua própria captura — ou
    alcançar a global privada. Com isto, encadear é explícito:

        anterior = telemetry.persistencia_atual()
        telemetry.configurar_persistencia(lambda d: (guardar(d), anterior and anterior(d)))

    Um sink só, e composto por quem instala, em vez de uma lista de observadores
    aqui dentro: quem encadeia decide a ordem e o que fazer se um destino falhar,
    e este módulo continua com um destino opcional, não com um mecanismo de
    eventos.
    """
    return _persistir


def serializar(dados: dict) -> str:
    """JSON canônico do registro. Um só lugar para o log e para o banco."""
    return json.dumps(dados, ensure_ascii=False)


def configurar_logs(destino=sys.stderr) -> None:
    """Liga a saída da telemetria no terminal. Respeita TELEMETRY_STDERR_ENABLED.

    stderr por padrão: na CLI, o stdout é a resposta ao aluno — `python -m
    scripts.ask ... 2> telemetria.jsonl` separa os dois sem nenhum parser.

    Desligar aqui não desliga a persistência: com `TELEMETRY_STDERR_ENABLED=false`
    e `TELEMETRY_DB_ENABLED=true`, o registro continua indo para o Postgres sem
    poluir o terminal — que é o modo esperado para uso normal da CLI.
    """
    if not settings.telemetry_stderr_enabled or logger.handlers:
        return
    handler = logging.StreamHandler(destino)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)


def hash_pergunta(texto: str) -> str:
    """12 hex chars: suficiente para agrupar repetições, curto para ler no log."""
    return hashlib.sha256(texto.strip().lower().encode("utf-8")).hexdigest()[:12]


@dataclass
class Registro:
    """Campos preenchidos ao longo de `answer()`; emitidos uma vez no final.

    `None` significa "esta etapa não aconteceu" (ex.: `ms_llm` nulo em cache
    hit, `ms_web` nulo no caminho normal) — é informação, não falta de dado.
    """

    canal: str
    assunto: str | None
    pergunta_hash: str
    # Modelo que DE FATO respondeu. Começa com o configurado (o topo da cadeia)
    # e é corrigido em `somar_tokens` quando a resposta chega — com uma cadeia
    # de fallback, o configurado é só um palpite.
    chat_model: str

    # Provider que respondeu ("gemini", "groq", "openrouter"). Nulo quando não
    # houve chamada ao LLM (cache hit, triagem, guardrail sem fallback).
    #
    # É a métrica que torna o fallback contável: com só o log de WARNING dá para
    # ver que caiu, mas não "quanto do tráfego da semana saiu do Groq" — que é a
    # pergunta que decide se a cota do Gemini precisa ser aumentada. A tabela é
    # JSONB (ver app/db/telemetry_store.py), então campo novo não é migração:
    # `dados->>'provider'` já funciona.
    provider: str | None = None

    # Id da request HTTP (T3.2). O MESMO valor que sai no header `X-Request-Id`
    # e no corpo da resposta — é a ponte entre a reclamação do aluno e a linha
    # do banco. `None` quando a pergunta não veio de HTTP (CLI).
    request_id: str | None = None

    # Categorias de identificador pessoal encontradas NA PERGUNTA (T3.4):
    # ["cpf"], ["ra", "email"]... Só o rótulo, nunca o valor — ver app/core/pii.py.
    # `None` quando não havia nenhum. Serve para dois usos: medir quanto do
    # tráfego traz dado pessoal (e justificar a política de retenção) e provar,
    # numa auditoria, que o alerta existe e que o valor não foi guardado.
    pii: list[str] | None = None

    # De onde saiu o `assunto` acima: "informado" (o usuário passou), "metadata"
    # (pasta do documento que respondeu), "allowlist" (domínio da fonte web),
    # "triagem" (categoria de outro departamento, ver app/agent/triagem.py),
    # "guardrail" (pedido de abuso barrado na entrada, ver app/agent/guardrail.py),
    # "blocklist" (termo sensível reconhecido) ou None. Sem isso, um assunto
    # derivado fica indistinguível de um informado, e a métrica perde o sentido.
    assunto_origem: str | None = None
    # Tema da pergunta em poucas palavras, escrito pelo próprio modelo na última
    # linha da resposta (ver `prompts.separar_topico`). Enquanto `assunto` diz
    # "canvas", isto diz "envio de atividade com prazo expirado" — é o que vira
    # pauta de trabalho. Nulo quando não houve chamada ao LLM.
    topico: str | None = None

    origem: str | None = None
    grounded: bool | None = None

    # Qualidade do retrieval (M5): queda sustentada = drift de base ou de query.
    n_chunks: int | None = None
    score_top: float | None = None

    # RET-3 — o 2º estágio (cross-encoder) rodou nesta pergunta? `None` quando
    # `RERANKER_ENABLED=false` (o caminho bi-encoder de sempre).
    reranker_aplicado: bool | None = None
    # `score_top` do BI-ENCODER (E5), antes do rerank. Mantém a série histórica
    # comparável (o `score_top` abaixo passa a ser o do cross-encoder quando ele
    # roda, noutra escala) e permite medir depois "o rerank mudou a ordem?".
    # `None` quando não houve rerank. Ver `app/retrieval/reranker.py`.
    score_top_bruto: float | None = None

    # DISPERSÃO do top-k, e não mais só o topo. Existe para responder a uma
    # pergunta que `score_top` sozinho não responde: a base COBRE este tema?
    #
    # Medido em 2026-08-26 (ver eval/analise-telemetria-2026-08-26.md §3.1):
    # o score do topo não separa "a base respondeu" de "a base não respondeu" —
    # média 0.8694 contra 0.8663, faixas sobrepostas, e o MAIOR score da rodada
    # era de uma pergunta que a base não cobria. Com o E5, tudo cai numa faixa
    # estreita e alta, então nenhum limiar absoluto sobre `score_top` funciona.
    #
    # A hipótese a testar é a margem RELATIVA: quando a base cobre o tema, o
    # topo se destaca dos demais; quando não cobre, os k chegam empatados. Esses
    # dois campos são o mínimo para calcular isso depois (`score_top -
    # score_min`, e a distância do topo à média) sem guardar os k scores.
    #
    # É deliberadamente só INSTRUMENTAÇÃO: nada no roteamento lê estes campos
    # ainda. Primeiro acumular dado real, depois decidir o critério — que é o
    # mesmo caminho que `triagem.py` documenta para a segunda camada dela.
    score_min: float | None = None
    score_mean: float | None = None

    # Custo (M1/M2): cache hit é resposta com zero token de API.
    cache_hit: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Latência por etapa (M3): perfis diferentes — CPU local, rede, rede lenta.
    ms_retrieve: float | None = None
    # INF-9 — só o 2º estágio do retrieval (cross-encoder), medido à parte para
    # o A/B `RERANKER_ENABLED` false×true conseguir isolar o custo do rerank.
    # É um SUBCONJUNTO de `ms_retrieve` (o cross-encoder roda dentro da janela
    # cronometrada em `responder._responder`) — o custo do 1º estágio é
    # `ms_retrieve - ms_rerank`. `None` quando o reranker não rodou (o padrão).
    ms_rerank: float | None = None
    ms_llm: float | None = None
    ms_web: float | None = None
    ms_total: float | None = None

    # LLM vetou o contexto recuperado (M7): achou chunks/snippets acima do
    # limiar, mas nenhum de fato respondia — ver CONTEXTO_INSUFICIENTE em
    # prompts.py. `base_insuficiente=True` é o que aciona o fallback de busca
    # externa em `responder._responder`; `web_insuficiente=True` é o mesmo veto
    # já do lado da web, que encaminha para a secretaria.
    base_insuficiente: bool | None = None
    web_insuficiente: bool | None = None

    # A rede de segurança de `responder.answer` teve que agir: um marcador de
    # recusa passou pelos vetos e só foi barrado no fim. Sempre nulo em operação
    # normal — se aparecer, o modelo está emitindo o marcador numa forma que os
    # vetos não reconhecem (foi assim que "INSUFFICIENT" traduzido apareceu), e
    # a resposta virou encaminhamento à secretaria sem tentar a busca externa.
    veto_escapou: bool | None = None

    # VET-2 — o modelo se RECUSOU a obedecer o pedido (não a responder por falta
    # de contexto) e essa recusa passou como resposta; a rede de segurança de
    # `responder.answer` a converteu no encaminhamento do guardrail. Nulo em
    # operação normal. Se aparecer, é um jailbreak / pedido abusivo que furou o
    # guardrail léxico (paráfrase ou outro idioma) — pauta de calibração do
    # `guardrail._PADROES`, não de indexação.
    recusa_modelo: bool | None = None

    erro: str | None = None

    # Texto da resposta do agente. `None` em operação normal — preencher isto
    # para tráfego de aluno reabriria exatamente o vazamento que a PRIVACIDADE
    # no topo deste módulo existe para evitar (a resposta pode ecoar RA/CPF que
    # veio da pergunta). Só `scripts/eval_run.py` preenche, e só porque o
    # dataset de teste (`eval/perguntas/perguntas.jsonc`) é sintético, sem aluno
    # real por trás — ver o `if registro.canal == "eval"` em
    # `responder.answer`. Existe para que `scripts.eval_report` mostre
    # pergunta+resposta+esperado+obtido+modelo sem precisar reexecutar nada.
    resposta: str | None = None

    def somar_tokens(self, resposta) -> None:
        """Absorve o que a resposta do LangChain traz de custo e de origem.

        Soma os tokens em vez de atribuir porque o caminho da web pode gerar
        mais de uma chamada no futuro. Tolera ausência: dublês de teste e
        provedores que não reportam uso devolvem `usage_metadata` vazio.

        O provider vem junto, e não num método separado, de propósito: este é o
        único ponto por onde TODA resposta de LLM já passa (base e web), então
        aqui não tem como esquecer — mesmo motivo da rede de segurança em
        `responder.answer`. O carimbo é posto pela cadeia
        (`providers/chain._carimbar`); um LLM injetado direto nos testes não tem
        `response_metadata` e os campos ficam como estavam.
        """
        uso = getattr(resposta, "usage_metadata", None) or {}
        entrada, saida = uso.get("input_tokens"), uso.get("output_tokens")
        if entrada is not None:
            self.input_tokens = (self.input_tokens or 0) + entrada
        if saida is not None:
            self.output_tokens = (self.output_tokens or 0) + saida

        metadata = getattr(resposta, "response_metadata", None) or {}
        # A ÚLTIMA chamada vence quando há duas na mesma resposta (base vetou ->
        # web): é a que produziu o texto que o aluno recebeu.
        self.provider = metadata.get("provider") or self.provider
        self.chat_model = metadata.get("provider_model") or self.chat_model


@contextmanager
def cronometro(registro: Registro, campo: str) -> Iterator[None]:
    """Mede uma etapa e ACRESCENTA em `registro.<campo>`, mesmo se ela levantar.

    Soma em vez de atribuir pelo mesmo motivo de `Registro.somar_tokens`: uma
    resposta pode passar pela mesma etapa mais de uma vez (`ms_llm` no caminho
    base -> insuficiente -> web faz duas chamadas ao LLM na mesma resposta) — a
    segunda passagem não pode apagar o custo da primeira.
    """
    inicio = time.perf_counter()
    try:
        yield
    finally:
        decorrido = round((time.perf_counter() - inicio) * 1000, 1)
        setattr(registro, campo, round((getattr(registro, campo) or 0) + decorrido, 1))


@contextmanager
def etapa(campo: str) -> Iterator[None]:
    """Cronometra uma sub-etapa no registro da pergunta em curso, se houver.

    Para código que roda dentro de `answer()` mas não no orquestrador — hoje só
    o 2º estágio do retrieval (`app/retrieval/retriever.py`, quando
    `RERANKER_ENABLED=true`) — somar sua latência a um campo do `Registro` sem
    recebê-lo por parâmetro. Mesma ideia dos ContextVars `_canal`/`_request_id`:
    informação que a lógica não deveria ter que carregar.

    Fora de `answer()` (ingestão, teste de unidade do retriever/reranker) não há
    registro aberto e isto é um no-op — a etapa roda igual, só não é medida.
    """
    registro = _registro_atual.get()
    if registro is None:
        yield
    else:
        with cronometro(registro, campo):
            yield


@contextmanager
def registrar(assunto: str | None, pergunta: str, chat_model: str) -> Iterator[Registro]:
    """Abre o registro da pergunta e o emite ao final, com ou sem exceção.

    Uma falha é justamente o caso em que a telemetria mais importa, então o
    `finally` emite igual — com `erro` preenchido e as etapas que chegaram a
    rodar já cronometradas."""
    encontrado = pii.detectar(pergunta)
    if encontrado:
        # WARNING e não INFO: é a linha que uma auditoria procura, e o operador
        # precisa vê-la sem ligar log verboso. Só as categorias — o valor não
        # aparece aqui pela mesma razão que não vai para o banco.
        logger_interno.warning(
            "pergunta contém identificador pessoal (%s); nada disso é persistido",
            ", ".join(encontrado),
        )

    registro = Registro(
        canal=_canal.get(),
        assunto=assunto,
        assunto_origem="informado" if assunto else None,
        pergunta_hash=hash_pergunta(pergunta),
        chat_model=chat_model,
        request_id=_request_id.get(),
        pii=encontrado or None,
    )
    inicio = time.perf_counter()
    token = _registro_atual.set(registro)
    try:
        yield registro
    except Exception as exc:
        registro.erro = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _registro_atual.reset(token)
        registro.ms_total = round((time.perf_counter() - inicio) * 1000, 1)
        # Último ponto antes de o registro sair para qualquer destino, e por
        # isso o único lugar onde o mascaramento precisa existir (T3.4).
        # `topico` é escrito pelo LLM a partir da pergunta e é o campo que pode
        # repetir o RA que o aluno digitou; `erro` carrega `str(exc)`, e uma
        # exceção do driver pode trazer o valor que a causou.
        registro.topico = pii.mascarar(registro.topico)
        registro.erro = pii.mascarar(registro.erro)
        registro.resposta = pii.mascarar(registro.resposta)
        dados = asdict(registro)
        # Nunca deixar a telemetria derrubar uma resposta que já ficou pronta.
        try:
            logger.info(serializar(dados))
        except Exception:  # pragma: no cover
            logger_interno.exception("falha ao emitir telemetria")
        # Destino separado e opcional: o log em stderr já saiu acima, então uma
        # falha aqui não perde o registro — só a cópia consultável por SQL.
        if _persistir is not None:
            try:
                _persistir(dados)
            except Exception:
                logger_interno.exception("falha ao persistir telemetria")
