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

from dataclasses import dataclass
from datetime import datetime

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


@dataclass(frozen=True)
class Lacuna:
    """Um tema que a base não respondeu, no relatório de lacunas (T3.3)."""

    rotulo: str  # tópico escrito pelo LLM, ou `hash:xxxx` quando não houve LLM
    assuntos: str  # todos os assuntos em que o tema apareceu, separados por vírgula
    ocorrencias: int
    perguntas_distintas: int
    # Quantas das ocorrências saíram SEM resposta nenhuma (`origem='nenhuma'`),
    # em oposição às que a busca externa cobriu. É a diferença entre "o aluno
    # foi para a secretaria" e "respondemos, só que devagar e de fora".
    sem_resposta: int
    ultima_vez: datetime


# `grounded=false` é o critério, e não `origem='nenhuma'`: uma resposta que veio
# da web TAMBÉM é uma lacuna — a informação existia, mas não na nossa base. É
# justamente a lacuna mais fácil de fechar, porque a página que respondeu já diz
# qual documento indexar.
#
# `origem='encaminhado'` fica de fora: assunto de outro departamento (boleto,
# diploma) nunca vai ser indexado aqui, então contá-lo como lacuna encheria o
# topo do relatório com trabalho que não existe.
#
# Linhas com `erro` também saem: são falha de infraestrutura, não ausência de
# conteúdo — misturar as duas faria uma queda do Postgres virar "documento
# faltando".
# O agrupamento é SÓ pelo tema, e isso foi uma correção: agrupar por
# `(tema, assunto, origem)` fragmentava a mesma lacuna em várias linhas de peso
# 1 — o mesmo "envio de atividade no Canvas" aparecia duas vezes por ter sido
# rotulado ora `canvas`, ora `puc-digital`, e mais uma por ter caído na web numa
# das vezes. Isso destrói justamente a ordenação por frequência, que é o único
# motivo de o relatório existir. Assunto e origem viram agregados da linha.
#
# `lower()` no agrupamento: o tema é texto livre do LLM, e "Acesso ao Canvas" e
# "acesso ao canvas" são o mesmo trabalho de ingestão. Não resolve variação de
# redação ("Como acessar o Canvas") — para isso seria preciso agrupar por
# embedding do tema, que é desproporcional para um relatório semanal lido por
# uma pessoa. `exemplo` devolve uma das grafias originais, para exibição.
_CONSULTAR_LACUNAS = """
SELECT
    LOWER(COALESCE(dados->>'topico', 'hash:' || (dados->>'pergunta_hash'))) AS rotulo,
    MIN(dados->>'topico')                                                   AS exemplo,
    COUNT(*)                                                                AS ocorrencias,
    COUNT(DISTINCT dados->>'pergunta_hash')                                 AS perguntas_distintas,
    COUNT(*) FILTER (WHERE dados->>'origem' = 'nenhuma')                    AS sem_resposta,
    STRING_AGG(DISTINCT dados->>'assunto', ', ')                            AS assuntos,
    MAX(criado_em)                                                          AS ultima_vez
FROM telemetria
WHERE criado_em >= now() - make_interval(days => :dias)
  AND dados->>'erro' IS NULL
  AND (dados->>'grounded') = 'false'
  AND COALESCE(dados->>'origem', '') <> 'encaminhado'
GROUP BY 1
ORDER BY sem_resposta DESC, ocorrencias DESC, ultima_vez DESC
LIMIT :limite
"""

_CONTAR_RESPONDIDAS = """
SELECT
    COUNT(*) FILTER (WHERE dados->>'erro' IS NULL)                          AS total,
    COUNT(*) FILTER (WHERE dados->>'erro' IS NULL
                       AND (dados->>'grounded') = 'false'
                       AND COALESCE(dados->>'origem', '') <> 'encaminhado')  AS lacunas
FROM telemetria
WHERE criado_em >= now() - make_interval(days => :dias)
"""


def consultar_lacunas(dias: int, limite: int) -> list[Lacuna]:
    """Perguntas que a base NÃO respondeu, agrupadas e ordenadas por frequência.

    O agrupamento é pelo `topico` (escrito pelo LLM: "envio de atividade com
    prazo expirado"), não pelo hash da pergunta — o hash agrupa só repetição
    literal, e duas formas de perguntar a mesma coisa apareceriam como duas
    lacunas de peso 1 em vez de uma de peso 2. Quando não houve chamada ao LLM
    (`origem='nenhuma'`) não existe tópico, e aí o hash é o único rótulo
    disponível; ele aparece com o prefixo `hash:` para não se confundir com um
    tópico de verdade.
    """
    with get_vector_store().session_maker() as sessao:
        # `commit` logo depois do DDL: numa sessão só de leitura, sem ele o
        # CREATE seria descartado no fim do bloco — e `_tabela_pronta` já teria
        # ficado True, fazendo a próxima chamada pular a criação e falhar.
        _garantir_tabela(sessao)
        sessao.commit()
        linhas = sessao.execute(
            text(_CONSULTAR_LACUNAS), {"dias": dias, "limite": limite}
        ).all()

    return [
        Lacuna(
            # `exemplo` é a grafia original do tema; `rotulo` é a chave em caixa
            # baixa. Para as linhas sem tópico (sem chamada ao LLM) `exemplo` é
            # nulo e o próprio `hash:...` é o que resta para exibir.
            rotulo=linha.exemplo or linha.rotulo,
            assuntos=linha.assuntos or "—",
            ocorrencias=linha.ocorrencias,
            perguntas_distintas=linha.perguntas_distintas,
            sem_resposta=linha.sem_resposta,
            ultima_vez=linha.ultima_vez,
        )
        for linha in linhas
    ]


def contar_perguntas(dias: int) -> tuple[int, int]:
    """`(total de perguntas, quantas foram lacuna)` na janela.

    O relatório precisa dos dois para dizer "18% do tráfego não é respondido
    pela base": a lista de lacunas sozinha não distingue 30 lacunas em 40
    perguntas de 30 em 3000.
    """
    with get_vector_store().session_maker() as sessao:
        _garantir_tabela(sessao)
        sessao.commit()
        linha = sessao.execute(text(_CONTAR_RESPONDIDAS), {"dias": dias}).one()
    return linha.total or 0, linha.lacunas or 0


def habilitar() -> None:
    """Liga a persistência. Chamado pelos entrypoints (CLI, /ask).

    Explícito, e não no import, para que `pytest` e qualquer uso do agente como
    biblioteca continuem sem tocar no banco. É `app/db` que importa
    `app/core/telemetry`, nunca o contrário — o core não conhece o banco.
    """
    if not settings.telemetry_db_enabled:
        return
    telemetry.configurar_persistencia(salvar)
