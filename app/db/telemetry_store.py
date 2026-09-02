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

    Aqui o DDL roda dentro da sessão de quem chama, então a flag de processo
    evita abrir I/O extra para reconferir algo que já é `IF NOT EXISTS`. Ao
    contrário de `response_cache._ensure_table` (que abre a própria sessão e
    checa o catálogo a cada acesso, ver INF-11), o custo de uma tabela de
    telemetria dropada em runtime é perder registros, não derrubar resposta —
    o restart do processo reconstrói a flag.
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


_ORIGEM_POR_HASH = """
SELECT DISTINCT ON (dados->>'pergunta_hash')
    dados->>'pergunta_hash' AS pergunta_hash,
    dados->>'origem'        AS origem,
    (dados->>'grounded')::boolean AS grounded,
    (dados->>'score_top')::float  AS score_top,
    (dados->>'score_min')::float  AS score_min,
    (dados->>'score_mean')::float AS score_mean,
    (dados->>'n_chunks')::int     AS n_chunks,
    (dados->>'cache_hit')::boolean AS cache_hit,
    dados->>'resposta'    AS resposta,
    dados->>'chat_model'  AS chat_model,
    dados->>'provider'    AS provider
FROM telemetria
WHERE dados->>'canal' = :canal
  AND dados->>'pergunta_hash' = ANY(:hashes)
  AND criado_em >= now() - make_interval(days => :dias)
ORDER BY dados->>'pergunta_hash', criado_em DESC
"""


@dataclass(frozen=True)
class ResultadoTelemetria:
    """O que a telemetria registrou, de fato, para uma pergunta (via hash)."""

    origem: str | None
    grounded: bool | None
    score_top: float | None
    score_min: float | None
    score_mean: float | None
    n_chunks: int | None
    cache_hit: bool | None
    resposta: str | None
    chat_model: str | None
    provider: str | None

    @property
    def margem_relativa(self) -> float | None:
        """`score_top − score_min` — feature de confiança da RET-2.

        Derivada, não gravada: a telemetria guarda só os brutos (ver
        `telemetry.Registro`). Exposta aqui para `scripts.eval_report` acumular a
        mediana por item ao longo de N rodadas sem reprocessar o `.jsonl`.
        """
        if self.score_top is None or self.score_min is None:
            return None
        return round(self.score_top - self.score_min, 4)

    @property
    def modelo(self) -> str | None:
        """`"provider:modelo"`, igual `responder._rotulo_do_modelo` — `None` quando
        não houve chamada ao LLM (cache hit, triagem, guardrail sem fallback)."""
        if not self.chat_model:
            return None
        return f"{self.provider}:{self.chat_model}" if self.provider else self.chat_model


def origem_por_hash(
    hashes: list[str], dias: int, canal: str = "eval"
) -> dict[str, ResultadoTelemetria]:
    """Para cada hash de pergunta, a linha MAIS RECENTE gravada na janela.

    Usado por `scripts.eval_report` para comparar a `origem_esperada` de um
    dataset de teste (scripts.eval_run) com o que ficou de fato gravado na
    telemetria — a mesma fonte de verdade de `scripts.lacunas`. `canal='eval'`
    isola essas linhas do tráfego real (ver `telemetry.set_canal` em
    `scripts/eval_run.py`), e "mais recente" resolve pergunta repetida em duas
    rodadas de calibração dentro da mesma janela, pegando a última.
    """
    if not hashes:
        return {}

    with get_vector_store().session_maker() as sessao:
        _garantir_tabela(sessao)
        sessao.commit()
        linhas = sessao.execute(
            text(_ORIGEM_POR_HASH), {"hashes": hashes, "dias": dias, "canal": canal}
        ).all()

    return {
        linha.pergunta_hash: ResultadoTelemetria(
            origem=linha.origem,
            grounded=linha.grounded,
            score_top=linha.score_top,
            score_min=linha.score_min,
            score_mean=linha.score_mean,
            n_chunks=linha.n_chunks,
            cache_hit=linha.cache_hit,
            resposta=linha.resposta,
            chat_model=linha.chat_model,
            provider=linha.provider,
        )
        for linha in linhas
    }


_EXPORTAR_CANAL = """
SELECT criado_em, dados
FROM telemetria
WHERE dados->>'canal' = :canal
  AND criado_em >= now() - make_interval(days => :dias)
ORDER BY criado_em
"""


def exportar_canal(canal: str, dias: int) -> list[tuple[datetime, dict]]:
    """Todas as linhas de um canal na janela, em ordem cronológica.

    `dados` cru (não passa por `ResultadoTelemetria`) de propósito: existe para
    inspeção externa (`scripts.eval_export`, auditoria manual), onde o
    consumidor pode querer QUALQUER campo do JSONB, não só os que os relatórios
    de hoje leem. Ver `origem_por_hash` para o caso de uso que já filtra/resume.
    """
    with get_vector_store().session_maker() as sessao:
        _garantir_tabela(sessao)
        sessao.commit()
        linhas = sessao.execute(text(_EXPORTAR_CANAL), {"canal": canal, "dias": dias}).all()
    return [(linha.criado_em, linha.dados) for linha in linhas]


def limpar_telemetria() -> int:
    """Apaga todos os registros de telemetria. Usado pela CLI de limpeza (base zerada p/ testes)."""
    with get_vector_store().session_maker() as sessao:
        _garantir_tabela(sessao)
        resultado = sessao.execute(text("DELETE FROM telemetria"))
        sessao.commit()
        return resultado.rowcount or 0


def habilitar() -> None:
    """Liga a persistência. Chamado pelos entrypoints (CLI, /ask).

    Explícito, e não no import, para que `pytest` e qualquer uso do agente como
    biblioteca continuem sem tocar no banco. É `app/db` que importa
    `app/core/telemetry`, nunca o contrário — o core não conhece o banco.
    """
    if not settings.telemetry_db_enabled:
        return
    telemetry.configurar_persistencia(salvar)
