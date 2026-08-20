"""Persistência da telemetria na mesma base Postgres da ingestão.

Mesmo motivo de `response_cache.py`: tabela própria, isolada aqui, para que uma
mudança no acesso a ela não vaze para o resto do projeto. Sem dependência nova —
o Postgres já está de pé para o pgvector, e a CLI não responde nada sem ele.

Por que JSONB e não uma coluna por campo: o registro (`telemetry.Registro`) é
novo e vai ganhar campos (tool calling, APIs em tempo real). Com JSONB, campo
novo não é migração — e o SQL continua completo: `dados->>'origem'`,
`(dados->>'input_tokens')::int`.

RETENÇÃO — linhas mais velhas que `TELEMETRY_RETENTION_DAYS` (7 por padrão) são
apagadas. A limpeza roda junto da escrita, no máximo uma vez por hora por
processo: não precisa de cron, e uma pergunta paga o custo de vez em quando.
"""

import logging
import time

from sqlalchemy import text

from app.core import telemetry
from app.core.config import settings
from app.db.vector_store import get_vector_store

logger = logging.getLogger(__name__)

# Guarda da limpeza: perf_counter da última execução neste processo.
_ultima_limpeza: float | None = None
_INTERVALO_LIMPEZA_S = 3600.0

_CRIAR_TABELA = """
CREATE TABLE IF NOT EXISTS telemetria (
    id BIGSERIAL PRIMARY KEY,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    dados JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetria_criado_em_idx ON telemetria (criado_em);
"""

_tabela_pronta = False


def _garantir_tabela(session) -> None:
    """Idempotente e barata depois da primeira vez (flag de processo).

    Não usa `lru_cache` como `response_cache._ensure_table` porque aqui o DDL
    roda dentro da sessão de quem chama — evita abrir uma segunda transação só
    para conferir algo que já é `IF NOT EXISTS`.
    """
    global _tabela_pronta
    if _tabela_pronta:
        return
    session.execute(text(_CRIAR_TABELA))
    _tabela_pronta = True


def _limpar_expirados(session) -> None:
    """Apaga o que passou da retenção. No máximo 1x/hora por processo."""
    global _ultima_limpeza
    agora = time.perf_counter()
    if _ultima_limpeza is not None and agora - _ultima_limpeza < _INTERVALO_LIMPEZA_S:
        return

    resultado = session.execute(
        text(
            "DELETE FROM telemetria "
            "WHERE criado_em < now() - make_interval(days => :dias)"
        ),
        {"dias": settings.telemetry_retention_days},
    )
    _ultima_limpeza = agora
    if resultado.rowcount:
        logger.info("telemetria: %d registros expirados removidos", resultado.rowcount)


def salvar(dados: dict) -> None:
    """Grava um registro. Nunca levanta.

    A telemetria não pode derrubar uma resposta que já ficou pronta: se o banco
    estiver fora, a linha em stderr continua saindo e o INSERT é perdido de
    propósito. É por isso que a persistência é um destino *adicional*, e não um
    substituto do log.
    """
    try:
        store = get_vector_store()
        with store.session_maker() as session:
            _garantir_tabela(session)
            session.execute(
                text("INSERT INTO telemetria (dados) VALUES (CAST(:dados AS JSONB))"),
                {"dados": telemetry.serializar(dados)},
            )
            _limpar_expirados(session)
            session.commit()
    except Exception:
        # `exception` e não `raise`: perder telemetria é aceitável, perder a
        # resposta do aluno não.
        logger.warning("telemetria: falha ao gravar no Postgres", exc_info=True)


def habilitar() -> None:
    """Liga a persistência. Chamado pelos entrypoints (CLI, /ask).

    Explícito, e não no import, para que `pytest` e qualquer uso do agente como
    biblioteca continuem sem tocar no banco. É `app/db` que importa
    `app/core/telemetry`, nunca o contrário — o core não conhece o banco.
    """
    if not settings.telemetry_db_enabled:
        return
    telemetry.configurar_persistencia(salvar)
