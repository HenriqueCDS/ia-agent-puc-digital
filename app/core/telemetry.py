"""Telemetria: uma linha JSON por pergunta respondida.

Fica em `answer()` (e não espalhada por retriever/providers) porque só o
orquestrador vê a pergunta inteira: quanto custou, por qual caminho saiu e com
que qualidade o retrieval respondeu. Instrumentar módulo a módulo obrigaria a
repetir o mesmo trabalho em cada ponto de extensão futuro (tool calling, APIs
públicas em tempo real).

Sem dependência nova de propósito: é `logging` + `json`. O mesmo dicionário
emitido aqui é o que alimenta, depois, uma tabela `telemetria` no Postgres que
já existe ou atributos de span do OpenTelemetry — o formato não fecha porta.

PRIVACIDADE — o texto da pergunta NUNCA entra no registro, só `assunto` e um
hash truncado. Perguntas de aluno passam por assuntos sensíveis (ver
`WEB_BLOCKLIST` em app/core/config.py: boleto, matrícula, bolsa). O hash ainda
serve ao propósito principal: agrupar perguntas repetidas que a base não
respondeu, para descobrir qual documento falta indexar.

Ler os registros:
    python -m scripts.ask "..." 2> telemetria.jsonl
    jq -s 'map(select(.origem=="nenhuma")) | group_by(.pergunta_hash)' telemetria.jsonl
"""

import hashlib
import json
import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Iterator

# Logger próprio, sem propagar: o registro estruturado não se mistura com os
# INFO em texto do resto do app, e ligar telemetria não liga log de tudo.
logger = logging.getLogger("telemetria")

# Quem originou a pergunta. ContextVar em vez de parâmetro porque é do
# entrypoint, não da lógica — `answer()` não deveria precisar saber.
_canal: ContextVar[str] = ContextVar("canal", default="desconhecido")


def set_canal(nome: str) -> None:
    """Chamado pelos entrypoints (CLI, /ask)."""
    _canal.set(nome)


def configurar_logs(destino=sys.stderr) -> None:
    """Liga a saída da telemetria. stderr por padrão: na CLI, o stdout é a
    resposta ao aluno — `python -m scripts.ask ... 2> telemetria.jsonl` separa
    os dois sem nenhum parser."""
    if logger.handlers:
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

    # LLM vetou os snippets da web (M7): ver WEB_INSUFICIENTE em prompts.py.
    web_insuficiente: bool | None = None

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
    """Mede uma etapa e grava em `registro.<campo>`, mesmo se ela levantar."""
    inicio = time.perf_counter()
    try:
        yield
    finally:
        setattr(registro, campo, round((time.perf_counter() - inicio) * 1000, 1))


@contextmanager
def registrar(assunto: str | None, pergunta: str, chat_model: str) -> Iterator[Registro]:
    """Abre o registro da pergunta e o emite ao final, com ou sem exceção.

    Uma falha é justamente o caso em que a telemetria mais importa, então o
    `finally` emite igual — com `erro` preenchido e as etapas que chegaram a
    rodar já cronometradas."""
    registro = Registro(
        canal=_canal.get(),
        assunto=assunto,
        pergunta_hash=hash_pergunta(pergunta),
        chat_model=chat_model,
    )
    inicio = time.perf_counter()
    try:
        yield registro
    except Exception as exc:
        registro.erro = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        registro.ms_total = round((time.perf_counter() - inicio) * 1000, 1)
        # Nunca deixar a telemetria derrubar uma resposta que já ficou pronta.
        try:
            logger.info(json.dumps(asdict(registro), ensure_ascii=False))
        except Exception:  # pragma: no cover
            logger.exception("falha ao emitir telemetria")
