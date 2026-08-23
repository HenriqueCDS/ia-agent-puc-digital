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
    chat_model: str

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
    alta_confianca: bool | None = None

    # Custo (M1/M2): cache hit é resposta com zero token de API.
    cache_hit: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    # Latência por etapa (M3): perfis diferentes — CPU local, rede, rede lenta.
    ms_retrieve: float | None = None
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

    erro: str | None = None

    def somar_tokens(self, resposta) -> None:
        """Extrai o uso de token da resposta do LangChain.

        Soma em vez de atribuir porque o caminho da web pode gerar mais de uma
        chamada no futuro. Tolera ausência: dublês de teste e provedores que não
        reportam uso devolvem `usage_metadata` vazio."""
        uso = getattr(resposta, "usage_metadata", None) or {}
        entrada, saida = uso.get("input_tokens"), uso.get("output_tokens")
        if entrada is not None:
            self.input_tokens = (self.input_tokens or 0) + entrada
        if saida is not None:
            self.output_tokens = (self.output_tokens or 0) + saida


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
    try:
        yield registro
    except Exception as exc:
        registro.erro = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        registro.ms_total = round((time.perf_counter() - inicio) * 1000, 1)
        # Último ponto antes de o registro sair para qualquer destino, e por
        # isso o único lugar onde o mascaramento precisa existir (T3.4).
        # `topico` é escrito pelo LLM a partir da pergunta e é o campo que pode
        # repetir o RA que o aluno digitou; `erro` carrega `str(exc)`, e uma
        # exceção do driver pode trazer o valor que a causou.
        registro.topico = pii.mascarar(registro.topico)
        registro.erro = pii.mascarar(registro.erro)
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
