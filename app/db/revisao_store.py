"""Revisão manual de fidelidade — lê da `telemetria` + `exemplo_perguntas` e
guarda o veredito humano (tabela `revisao_veredictos`).

Antes a tela `/revisao` lia os JSONs estáticos de `eval/resultados/`. Agora a
fonte é o banco: a `telemetria` (canal `eval`) diz o que o agente respondeu, a
`exemplo_perguntas` diz o que era esperado, e a junção é por `pergunta_hash` —
a única ponte possível, porque a `telemetria` NUNCA guarda o texto da pergunta
(ver o cabeçalho de app/core/telemetry.py). A resposta em si a telemetria
guarda, mas só no canal `eval` (dataset sintético, sem aluno por trás).

POR QUE O VEREDITO É CHAVEADO POR `telemetria_id`, E NÃO POR `pergunta_hash`:
o veredito é sobre UMA RESPOSTA. Cada rodada de `eval_run` gera linhas novas de
telemetria (ids novos) para as mesmas perguntas; um "insatisfeito" dado à
resposta da rodada de segunda NÃO deve valer para a resposta possivelmente
diferente da rodada de sexta. Guardamos `pergunta_hash` junto só para o
veredito sobreviver legível à expiração da linha de telemetria (retenção de
`TELEMETRY_RETENTION_DAYS_EVAL`) — é rótulo, não chave.

A EXPECTATIVA ajustada na tela NÃO vem para cá: ela é sobre a PERGUNTA, não
sobre a resposta, e vira um UPDATE em `exemplo_perguntas` via o CRUD de
`/v1/perguntas`. Isso elimina o "porte à mão para o perguntas.jsonc" que a
versão em arquivo exigia.
"""

import logging

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from app.core import telemetry
from app.db import perguntas_store
from app.db.vector_store import get_vector_store

logger = logging.getLogger(__name__)

VEREDICTOS_VALIDOS = ("satisfeito", "insatisfeito", "pulado")
_LISTA_SQL = ", ".join(f"'{v}'" for v in VEREDICTOS_VALIDOS)

_CRIAR = text(
    f"""
    CREATE TABLE IF NOT EXISTS revisao_veredictos (
        telemetria_id BIGINT PRIMARY KEY,
        pergunta_hash TEXT NOT NULL,
        veredicto TEXT NOT NULL,
        nota TEXT,
        criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT revisao_veredictos_valido CHECK (veredicto IN ({_LISTA_SQL}))
    );
    CREATE INDEX IF NOT EXISTS revisao_veredictos_hash_idx
        ON revisao_veredictos (pergunta_hash);
    """
)

_TABELA_PRONTA = text(
    """
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = current_schema() AND table_name = 'revisao_veredictos'
    """
)


def _ensure_table(store=None) -> None:
    store = store or get_vector_store()
    with store.session_maker() as sessao:
        if sessao.execute(_TABELA_PRONTA).first():
            return
        sessao.execute(_CRIAR)
        sessao.commit()


@dataclass(frozen=True)
class LinhaRevisao:
    """Uma pergunta do dataset cruzada com a última execução dela na telemetria.

    `telemetria_id is None` → a pergunta existe no dataset mas nunca rodou na
    janela (nada para revisar ainda). `pergunta_id is None` → a telemetria tem
    a execução mas a pergunta saiu do dataset (editada/desativada) — mostramos
    `hash:xxxx` e sem expectativa. Os dois casos são explícitos de propósito;
    esconder qualquer um faria a contagem da tela divergir do que ela lista.
    """

    pergunta_hash: str
    # lado do dataset (exemplo_perguntas)
    pergunta_id: int | None
    grupo: str | None
    pergunta: str | None
    assunto: str | None
    origem_esperada: str | None
    origem_tambem_ok: list[str]
    criterio: str | None
    # lado da telemetria (última linha da janela, canal eval)
    telemetria_id: int | None
    criado_em: datetime | None
    origem_obtida: str | None
    grounded: bool | None
    resposta: str | None
    provider: str | None
    chat_model: str | None
    cache_hit: bool | None
    score_top: float | None
    score_min: float | None
    score_mean: float | None
    n_chunks: int | None
    input_tokens: int | None
    output_tokens: int | None
    ms_total: float | None
    ms_retrieve: float | None
    ms_llm: float | None
    # o registro de telemetria cru (JSONB `dados`), para o dashboard ler
    # qualquer campo sem precisar de uma coluna tipada por métrica. `{}` quando
    # a pergunta não executou na janela. Os campos tipados acima continuam para
    # o cartão de revisão e para `acertou`/`margem_relativa`.
    telemetria: dict
    # lado do veredito humano (revisao_veredictos)
    veredicto: str | None
    nota: str | None

    @property
    def acertou(self) -> bool | None:
        """`origem_obtida ∈ (esperada + também_ok)`. `None` sem os dois lados.

        Mesma regra de `eval_run._origens_aceitas` — roteamento, não qualidade.
        """
        if self.origem_obtida is None or self.origem_esperada is None:
            return None
        return self.origem_obtida in {self.origem_esperada, *self.origem_tambem_ok}

    @property
    def margem_relativa(self) -> float | None:
        if self.score_top is None or self.score_min is None:
            return None
        return round(self.score_top - self.score_min, 4)


# `DISTINCT ON (pergunta_hash) ... ORDER BY pergunta_hash, criado_em DESC` =
# a linha MAIS RECENTE de cada pergunta na janela. Se alguém rodou `--grupo
# teste` hoje e `--grupo teste2` ontem, a tela mostra o estado combinado do
# dataset — que é o que se quer para "como está a rodada agora".
_LINHAS_TELEMETRIA = """
SELECT DISTINCT ON (dados->>'pergunta_hash')
    id                              AS telemetria_id,
    criado_em,
    dados,
    dados->>'pergunta_hash'         AS pergunta_hash,
    dados->>'origem'                AS origem_obtida,
    (dados->>'grounded')::boolean   AS grounded,
    dados->>'resposta'              AS resposta,
    dados->>'provider'              AS provider,
    dados->>'chat_model'            AS chat_model,
    (dados->>'cache_hit')::boolean  AS cache_hit,
    (dados->>'score_top')::float    AS score_top,
    (dados->>'score_min')::float    AS score_min,
    (dados->>'score_mean')::float   AS score_mean,
    (dados->>'n_chunks')::int       AS n_chunks,
    (dados->>'input_tokens')::int   AS input_tokens,
    (dados->>'output_tokens')::int  AS output_tokens,
    (dados->>'ms_total')::float     AS ms_total,
    (dados->>'ms_retrieve')::float  AS ms_retrieve,
    (dados->>'ms_llm')::float       AS ms_llm
FROM telemetria
WHERE dados->>'canal' = :canal
  AND criado_em >= now() - make_interval(days => :dias)
  {filtro_origem}
ORDER BY dados->>'pergunta_hash', criado_em DESC
"""


def linhas_da_rodada(
    dias: int = 90,
    canal: str = "eval",
    grupo: str | None = None,
    origem: str | None = None,
    store=None,
) -> list[LinhaRevisao]:
    """Cruza dataset × última execução × veredito.

    O cruzamento é feito em Python, não num JOIN gigante, porque as três fontes
    vivem em módulos diferentes (cada um dono do seu SQL, como o resto de
    `app/db`) e o volume é pequeno: o dataset tem ~160 itens, e o canal `eval` é
    só as rodadas de calibração, não o tráfego real.
    """
    _ensure_table(store)
    store = store or get_vector_store()

    filtro_origem = "AND dados->>'origem' = :origem" if origem else ""
    params: dict = {"canal": canal, "dias": dias}
    if origem:
        params["origem"] = origem

    with store.session_maker() as sessao:
        exec_rows = sessao.execute(
            text(_LINHAS_TELEMETRIA.format(filtro_origem=filtro_origem)), params
        ).all()
        vered_rows = sessao.execute(
            text("SELECT telemetria_id, veredicto, nota FROM revisao_veredictos")
        ).all()

    veredictos = {r.telemetria_id: (r.veredicto, r.nota) for r in vered_rows}
    execucoes = {r.pergunta_hash: r for r in exec_rows}

    # Dataset primeiro: garante que uma pergunta ainda não executada apareça.
    perguntas = perguntas_store.listar(grupo=grupo, apenas_ativas=True, store=store)
    por_hash: dict[str, perguntas_store.PerguntaExemplo] = {}
    for p in perguntas:
        # Empate de hash entre grupos: o filtro por `grupo` já resolve; sem
        # filtro, o primeiro (menor id) representa — a tela mostra o grupo dele.
        por_hash.setdefault(p.pergunta_hash, p)

    linhas: list[LinhaRevisao] = []
    hashes_vistos: set[str] = set()

    for h, p in por_hash.items():
        e = execucoes.get(h)
        hashes_vistos.add(h)
        linhas.append(_montar(h, p, e, veredictos))

    # Execuções órfãs (pergunta saiu do dataset) — só quando não há filtro de
    # grupo (com filtro, "órfã" não tem grupo para casar).
    if grupo is None:
        for h, e in execucoes.items():
            if h in hashes_vistos:
                continue
            linhas.append(_montar(h, None, e, veredictos))

    linhas.sort(key=lambda x: (x.grupo or "~", x.pergunta_id or 1 << 62))
    return linhas


def _montar(pergunta_hash, p, e, veredictos) -> LinhaRevisao:
    ver, nota = veredictos.get(e.telemetria_id, (None, None)) if e else (None, None)
    return LinhaRevisao(
        pergunta_hash=pergunta_hash,
        pergunta_id=p.id if p else None,
        grupo=p.grupo if p else None,
        pergunta=p.pergunta if p else None,
        assunto=p.assunto if p else None,
        origem_esperada=p.origem_esperada if p else None,
        origem_tambem_ok=list(p.origem_tambem_ok) if p else [],
        criterio=p.criterio if p else None,
        telemetria_id=e.telemetria_id if e else None,
        criado_em=e.criado_em if e else None,
        origem_obtida=e.origem_obtida if e else None,
        grounded=e.grounded if e else None,
        resposta=e.resposta if e else None,
        provider=e.provider if e else None,
        chat_model=e.chat_model if e else None,
        cache_hit=e.cache_hit if e else None,
        score_top=e.score_top if e else None,
        score_min=e.score_min if e else None,
        score_mean=e.score_mean if e else None,
        n_chunks=e.n_chunks if e else None,
        input_tokens=e.input_tokens if e else None,
        output_tokens=e.output_tokens if e else None,
        ms_total=e.ms_total if e else None,
        ms_retrieve=e.ms_retrieve if e else None,
        ms_llm=e.ms_llm if e else None,
        telemetria=dict(e.dados) if e and e.dados else {},
        veredicto=ver,
        nota=nota,
    )


_SERIE = """
SELECT
    date_trunc('day', criado_em)                                   AS dia,
    COUNT(*)                                                       AS execucoes,
    COUNT(*) FILTER (WHERE (dados->>'grounded') = 'false')         AS nao_grounded,
    COUNT(*) FILTER (WHERE (dados->>'cache_hit') = 'true')         AS cache_hits,
    AVG((dados->>'ms_total')::float)                               AS ms_total_medio
FROM telemetria
WHERE dados->>'canal' = :canal
  AND criado_em >= now() - make_interval(days => :dias)
GROUP BY 1
ORDER BY 1
"""


@dataclass(frozen=True)
class PontoSerie:
    dia: datetime
    execucoes: int
    nao_grounded: int
    cache_hits: int
    ms_total_medio: float | None


def serie_temporal(dias: int = 90, canal: str = "eval", store=None) -> list[PontoSerie]:
    """Execuções por dia — para o gráfico de tendência do dashboard.

    Conta TODAS as linhas (não `DISTINCT ON` hash): aqui o que interessa é o
    volume de cada rodada ao longo do tempo, não o estado atual do dataset.
    """
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        linhas = sessao.execute(text(_SERIE), {"canal": canal, "dias": dias}).all()
    return [
        PontoSerie(
            dia=r.dia,
            execucoes=r.execucoes,
            nao_grounded=r.nao_grounded,
            cache_hits=r.cache_hits,
            ms_total_medio=round(r.ms_total_medio, 1) if r.ms_total_medio is not None else None,
        )
        for r in linhas
    ]


# --- veredito humano -------------------------------------------------------


def salvar_veredicto(
    telemetria_id: int,
    pergunta_hash: str,
    veredicto: str,
    nota: str | None = None,
    store=None,
) -> None:
    """UPSERT do veredito de UMA resposta. Idempotente por `telemetria_id`."""
    if veredicto not in VEREDICTOS_VALIDOS:
        raise ValueError(
            f"veredicto inválido: {veredicto!r}. Válidos: {list(VEREDICTOS_VALIDOS)}"
        )
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        sessao.execute(
            text(
                """
                INSERT INTO revisao_veredictos
                    (telemetria_id, pergunta_hash, veredicto, nota)
                VALUES (:id, :hash, :veredicto, :nota)
                ON CONFLICT (telemetria_id) DO UPDATE SET
                    veredicto = EXCLUDED.veredicto,
                    nota = EXCLUDED.nota,
                    pergunta_hash = EXCLUDED.pergunta_hash,
                    atualizado_em = now()
                """
            ),
            {"id": telemetria_id, "hash": pergunta_hash, "veredicto": veredicto, "nota": nota},
        )
        sessao.commit()


def apagar_veredicto(telemetria_id: int, store=None) -> bool:
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        resultado = sessao.execute(
            text("DELETE FROM revisao_veredictos WHERE telemetria_id = :id"),
            {"id": telemetria_id},
        )
        sessao.commit()
    return bool(resultado.rowcount)


def limpar(store=None) -> int:
    _ensure_table(store)
    with (store or get_vector_store()).session_maker() as sessao:
        resultado = sessao.execute(text("DELETE FROM revisao_veredictos"))
        sessao.commit()
        return resultado.rowcount or 0
