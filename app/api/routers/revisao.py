"""Revisão manual de fidelidade — agora lendo do banco, não de arquivo.

Antes esta rota servia o HTML e listava os JSONs de `eval/resultados/`. Agora
serve o HTML e uma API de LEITURA sobre o banco:

    GET  /revisao                     a página (dashboard + conferência)
    GET  /revisao/chart.js            Chart.js vendorizado (não CDN — mesma origem)
    GET  /revisao/dados               dataset × última execução × veredito + agregados
    GET  /revisao/serie               execuções por dia (gráfico de tendência)
    PATCH /revisao/pergunta/{id}      ajusta a expectativa (origem_esperada / também_ok)
    PUT  /revisao/veredicto           grava o veredito de UMA resposta
    DELETE /revisao/veredicto/{id}    desfaz

A fonte é `telemetria` (canal `eval`) + `exemplo_perguntas`, cruzadas por
`pergunta_hash` em `app/db/revisao_store.py`. Nada aqui chama `/v1/ask` nem
gasta LLM. A EXPECTATIVA de uma pergunta não se ajusta por aqui — é um
`PATCH /v1/perguntas/{id}` (ver `routers/perguntas.py`); esta rota só mostra
o resultado.

PRIVACIDADE: a `telemetria` não guarda o texto da pergunta nem PII. O texto
vem de `exemplo_perguntas` (dataset sintético). A `resposta` vem da telemetria,
mas só existe lá no canal `eval` (ver `telemetry.Registro.resposta`).

`REVISAO_ENABLED=false` tira tudo isto do ar.
"""

import logging
import statistics

from fastapi import APIRouter, Body, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

from app.db import perguntas_store, revisao_store

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC = Path(__file__).resolve().parents[2] / "static"
_PAGINA = _STATIC / "revisao.html"
# Chart.js vendorizado (não CDN): a página continua abrindo numa máquina sem
# internet e sem depender de host de terceiro — mesma promessa da demo. Servido
# aqui, mesma origem, em vez de montar `app/static` inteira.
_CHART_JS = _STATIC / "vendor" / "chart.umd.min.js"

_CANAL_PADRAO = "eval"


@router.get("/revisao", response_class=HTMLResponse, include_in_schema=False)
def revisao() -> HTMLResponse:
    return HTMLResponse(_PAGINA.read_text(encoding="utf-8"))


@router.get("/revisao/chart.js", include_in_schema=False)
def chart_js() -> Response:
    return Response(
        _CHART_JS.read_bytes(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _percentil(valores: list[float], p: float) -> float | None:
    if not valores:
        return None
    ordenado = sorted(valores)
    if len(ordenado) == 1:
        return round(ordenado[0], 1)
    # Interpolação linear — `statistics.quantiles` não deixa pedir um p avulso.
    k = (len(ordenado) - 1) * p
    baixo = int(k)
    frac = k - baixo
    alto = min(baixo + 1, len(ordenado) - 1)
    return round(ordenado[baixo] + frac * (ordenado[alto] - ordenado[baixo]), 1)


def _serializar(linha: revisao_store.LinhaRevisao) -> dict:
    t = linha.telemetria
    return {
        "pergunta_hash": linha.pergunta_hash,
        "pergunta_id": linha.pergunta_id,
        "grupo": linha.grupo,
        "pergunta": linha.pergunta,
        "assunto": linha.assunto,
        "origem_esperada": linha.origem_esperada,
        "origem_tambem_ok": linha.origem_tambem_ok,
        "criterio": linha.criterio,
        "telemetria_id": linha.telemetria_id,
        "criado_em": linha.criado_em.isoformat() if linha.criado_em else None,
        "origem_obtida": linha.origem_obtida,
        "acertou": linha.acertou,
        "grounded": linha.grounded,
        "resposta": linha.resposta,
        "provider": linha.provider,
        "chat_model": linha.chat_model,
        "cache_hit": linha.cache_hit,
        "score_top": linha.score_top,
        "score_min": linha.score_min,
        "score_mean": linha.score_mean,
        "score_top_bruto": t.get("score_top_bruto"),
        "margem_relativa": linha.margem_relativa,
        "n_chunks": linha.n_chunks,
        "input_tokens": linha.input_tokens,
        "output_tokens": linha.output_tokens,
        "ms_total": linha.ms_total,
        "ms_retrieve": linha.ms_retrieve,
        "ms_rerank": t.get("ms_rerank"),
        "ms_llm": linha.ms_llm,
        "ms_web": t.get("ms_web"),
        "topico": t.get("topico"),
        "assunto_origem": t.get("assunto_origem"),
        "reranker_aplicado": t.get("reranker_aplicado"),
        "base_insuficiente": t.get("base_insuficiente"),
        "web_insuficiente": t.get("web_insuficiente"),
        "recusa_modelo": t.get("recusa_modelo"),
        "contexto_suspeito": t.get("contexto_suspeito"),
        "pii": t.get("pii"),
        "erro": t.get("erro"),
        "veredicto": linha.veredicto,
        "nota": linha.nota,
        "executada": linha.telemetria_id is not None,
        "no_dataset": linha.pergunta_id is not None,
    }


def _contagem(itens, chave, rotulo_nulo: str = "—") -> dict[str, int]:
    """`{valor: n}` de um campo — descartando `None` como `rotulo_nulo`."""
    saida: dict[str, int] = {}
    for valor in (chave(l) for l in itens):
        k = rotulo_nulo if valor in (None, "") else str(valor)
        saida[k] = saida.get(k, 0) + 1
    return dict(sorted(saida.items(), key=lambda kv: -kv[1]))


def _flag(itens, campo: str) -> dict[str, int]:
    """`{sim, nao, indef}` de um campo booleano do JSONB de telemetria.

    `indef` (nulo/ausente) é uma categoria de verdade — `base_insuficiente`,
    `veto_escapou` etc. são `None` no caminho normal, não `False`."""
    sim = nao = indef = 0
    for l in itens:
        v = l.telemetria.get(campo)
        if v is True:
            sim += 1
        elif v is False:
            nao += 1
        else:
            indef += 1
    return {"sim": sim, "nao": nao, "indef": indef}


def _stats(valores: list[float]) -> dict:
    limpos = [v for v in valores if v is not None]
    if not limpos:
        return {"n": 0, "media": None, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "n": len(limpos),
        "media": round(statistics.fmean(limpos), 2),
        "p50": _percentil(limpos, 0.50),
        "p95": _percentil(limpos, 0.95),
        "min": round(min(limpos), 4),
        "max": round(max(limpos), 4),
    }


def _resumo(linhas: list[revisao_store.LinhaRevisao]) -> dict:
    """Agregados do dashboard, calculados em Python.

    O volume é pequeno (o dataset tem ~160 itens, uma linha por pergunta) e as
    três fontes já foram cruzadas no store. Os campos numéricos crus de cada
    execução vão em `fluxo` — o front (Chart.js) monta histograma e gráfico de
    etapas a partir dele, em vez de o servidor pré-binar tudo.
    """
    executadas = [l for l in linhas if l.telemetria_id is not None]

    grupos: dict[str, dict] = {}
    for l in linhas:
        g = l.grupo or "(sem grupo)"
        d = grupos.setdefault(g, {"total": 0, "acertou": 0, "divergiu": 0, "indeterminado": 0})
        d["total"] += 1
        if l.acertou is True:
            d["acertou"] += 1
        elif l.acertou is False:
            d["divergiu"] += 1
        else:
            d["indeterminado"] += 1

    veredictos = {"satisfeito": 0, "insatisfeito": 0, "pulado": 0, "sem_veredicto": 0}
    for l in executadas:
        veredictos[l.veredicto if l.veredicto in veredictos else "sem_veredicto"] += 1

    cruz = {"certo_sat": 0, "certo_insat": 0, "divergiu_sat": 0, "divergiu_insat": 0}
    for l in executadas:
        if l.veredicto == "satisfeito":
            cruz["certo_sat" if l.acertou else "divergiu_sat"] += 1
        elif l.veredicto == "insatisfeito":
            cruz["certo_insat" if l.acertou else "divergiu_insat"] += 1

    por_provider: dict[str, dict] = {}
    for l in executadas:
        d = por_provider.setdefault(l.provider or "—", {"execucoes": 0, "input": 0, "output": 0})
        d["execucoes"] += 1
        d["input"] += l.input_tokens or 0
        d["output"] += l.output_tokens or 0

    topicos = _contagem(
        (l for l in executadas if l.telemetria.get("topico")),
        lambda l: l.telemetria.get("topico"),
    )

    fluxo = [
        {
            "rotulo": (l.pergunta or l.pergunta_hash)[:60],
            "grupo": l.grupo,
            "origem": l.origem_obtida,
            "acertou": l.acertou,
            "veredicto": l.veredicto,
            "ms_total": l.ms_total,
            "ms_retrieve": (l.ms_retrieve or 0) - (l.telemetria.get("ms_rerank") or 0),
            "ms_rerank": l.telemetria.get("ms_rerank"),
            "ms_llm": l.ms_llm,
            "ms_web": l.telemetria.get("ms_web"),
            "score_top": l.score_top,
            "score_min": l.score_min,
            "score_top_bruto": l.telemetria.get("score_top_bruto"),
            "n_chunks": l.n_chunks,
            "input_tokens": l.input_tokens,
            "output_tokens": l.output_tokens,
        }
        for l in executadas
    ]

    acertos = sum(1 for l in linhas if l.acertou is True)
    avaliaveis = sum(1 for l in linhas if l.acertou is not None)
    ms_total = [l.ms_total for l in executadas]

    return {
        "total": len(linhas),
        "executadas": len(executadas),
        "nao_executadas": len(linhas) - len(executadas),
        "acerto_geral": {
            "acertou": acertos,
            "avaliaveis": avaliaveis,
            "taxa": round(acertos / avaliaveis, 3) if avaliaveis else None,
        },
        "por_origem": _contagem(executadas, lambda l: l.origem_obtida),
        "por_grupo": grupos,
        "por_assunto": _contagem(executadas, lambda l: l.assunto or l.telemetria.get("assunto")),
        "por_assunto_origem": _contagem(executadas, lambda l: l.telemetria.get("assunto_origem")),
        "por_provider": por_provider,
        "por_modelo": _contagem(executadas, lambda l: l.chat_model),
        "veredictos": veredictos,
        "roteamento_x_fidelidade": cruz,
        "topicos": [[t, n] for t, n in list(topicos.items())[:12]],
        "flags": {
            "grounded": _flag(executadas, "grounded"),
            "cache_hit": _flag(executadas, "cache_hit"),
            "pii": {
                "sim": sum(1 for l in executadas if l.telemetria.get("pii")),
                "nao": sum(1 for l in executadas if not l.telemetria.get("pii")),
                "indef": 0,
            },
            "erro": {
                "sim": sum(1 for l in executadas if l.telemetria.get("erro")),
                "nao": sum(1 for l in executadas if not l.telemetria.get("erro")),
                "indef": 0,
            },
            "base_insuficiente": _flag(executadas, "base_insuficiente"),
            "web_insuficiente": _flag(executadas, "web_insuficiente"),
            "reranker_aplicado": _flag(executadas, "reranker_aplicado"),
            "recusa_modelo": _flag(executadas, "recusa_modelo"),
            "contexto_suspeito": _flag(executadas, "contexto_suspeito"),
            "veto_escapou": _flag(executadas, "veto_escapou"),
        },
        "pii_categorias": _contagem(
            (cat for l in executadas for cat in (l.telemetria.get("pii") or [])),
            lambda c: c,
        ),
        "medias": {
            "n_chunks": _stats([l.n_chunks for l in executadas])["media"],
            "input_tokens": _stats([l.input_tokens for l in executadas])["media"],
            "output_tokens": _stats([l.output_tokens for l in executadas])["media"],
            "score_top": _stats([l.score_top for l in executadas])["media"],
            "score_min": _stats([l.score_min for l in executadas])["media"],
            "margem_relativa": _stats([l.margem_relativa for l in executadas])["media"],
        },
        "latencia_ms": {
            "p50": _percentil([m for m in ms_total if m is not None], 0.50),
            "p95": _percentil([m for m in ms_total if m is not None], 0.95),
            "media": round(statistics.fmean([m for m in ms_total if m is not None]), 1)
            if any(m is not None for m in ms_total) else None,
        },
        "tempo_por_etapa": {
            etapa: _stats([f[etapa] for f in fluxo])["media"]
            for etapa in ("ms_retrieve", "ms_rerank", "ms_llm", "ms_web")
        },
        "distribuicoes": {
            campo: _stats([f[campo] for f in fluxo])
            for campo in (
                "ms_total", "ms_retrieve", "ms_rerank", "ms_llm", "ms_web",
                "score_top", "score_min", "score_top_bruto", "n_chunks",
            )
        },
        "fluxo": fluxo,
        "cache_hits": sum(1 for l in executadas if l.cache_hit),
        "nao_grounded": sum(1 for l in executadas if l.grounded is False),
    }


@router.get("/revisao/dados", include_in_schema=False)
def dados(
    dias: int = 90,
    canal: str = _CANAL_PADRAO,
    grupo: str | None = None,
    origem: str | None = None,
) -> JSONResponse:
    """Tudo que a página precisa numa chamada: as linhas + os agregados + as
    opções de filtro (grupos e origens que de fato existem)."""
    linhas = revisao_store.linhas_da_rodada(dias=dias, canal=canal, grupo=grupo, origem=origem)
    return JSONResponse(
        {
            "filtros": {
                "dias": dias,
                "canal": canal,
                "grupo": grupo,
                "origem": origem,
                "grupos_disponiveis": perguntas_store.grupos(),
            },
            "resumo": _resumo(linhas),
            "linhas": [_serializar(l) for l in linhas],
        }
    )


@router.get("/revisao/serie", include_in_schema=False)
def serie(dias: int = 90, canal: str = _CANAL_PADRAO) -> JSONResponse:
    pontos = revisao_store.serie_temporal(dias=dias, canal=canal)
    return JSONResponse(
        {
            "pontos": [
                {
                    "dia": p.dia.date().isoformat(),
                    "execucoes": p.execucoes,
                    "nao_grounded": p.nao_grounded,
                    "cache_hits": p.cache_hits,
                    "ms_total_medio": p.ms_total_medio,
                }
                for p in pontos
            ]
        }
    )


@router.patch("/revisao/pergunta/{pergunta_id}", include_in_schema=False)
def ajustar_expectativa(
    pergunta_id: int,
    origem_esperada: str | None = Body(None),
    origem_tambem_ok: list[str] | None = Body(None),
    criterio: str | None = Body(None),
) -> JSONResponse:
    """Ajusta a EXPECTATIVA de uma pergunta a partir da tela de revisão.

    Rota própria, e não `PATCH /v1/perguntas/{id}`: aquela exige o consumidor
    de escrita de avaliação (para uso programático / externo), e injetar essa
    chave no HTML de `/revisao` a tornaria pública. Aqui o controle de acesso é
    o mesmo do resto da tela — `REVISAO_ENABLED` e quem alcança a rota. Só os
    campos de expectativa; `pergunta`/`grupo` não se editam por aqui (mudar o
    texto mudaria o `pergunta_hash` e desligaria a linha da telemetria).
    """
    campos: dict = {}
    if origem_esperada is not None:
        campos["origem_esperada"] = origem_esperada
    if origem_tambem_ok is not None:
        campos["origem_tambem_ok"] = origem_tambem_ok
    if criterio is not None:
        campos["criterio"] = criterio

    p = perguntas_store.atualizar(pergunta_id, campos)
    if p is None:
        return JSONResponse({"erro": "nao_encontrado"}, status_code=404)
    return JSONResponse(
        {
            "id": p.id,
            "origem_esperada": p.origem_esperada,
            "origem_tambem_ok": p.origem_tambem_ok,
            "criterio": p.criterio,
        }
    )


@router.put("/revisao/veredicto", include_in_schema=False)
def gravar_veredicto(
    telemetria_id: int = Body(...),
    pergunta_hash: str = Body(...),
    veredicto: str = Body(...),
    nota: str | None = Body(None),
) -> JSONResponse:
    if veredicto not in revisao_store.VEREDICTOS_VALIDOS:
        return JSONResponse(
            {
                "erro": "veredicto_invalido",
                "detalhe": f"válidos: {list(revisao_store.VEREDICTOS_VALIDOS)}",
            },
            status_code=422,
        )
    revisao_store.salvar_veredicto(telemetria_id, pergunta_hash, veredicto, nota)
    return JSONResponse({"ok": True})


@router.delete("/revisao/veredicto/{telemetria_id}", include_in_schema=False)
def apagar_veredicto(telemetria_id: int) -> JSONResponse:
    apagou = revisao_store.apagar_veredicto(telemetria_id)
    return JSONResponse({"ok": apagou})
