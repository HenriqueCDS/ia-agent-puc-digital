"""Reranker cross-encoder — 2º estágio do retrieval (RET-3).

O retrieval de hoje é **bi-encoder** (`intfloat/multilingual-e5-base`): pergunta e
chunk viram vetores SEPARADAMENTE e a relevância é o cosseno entre eles. O vetor
do chunk é pré-calculado na ingestão. Efeito medido (eval de 26/28-08-2026): o E5
pontua ~0.82 para QUALQUER par de textos em português, então nenhum limiar
absoluto sobre `score_top` separa "a base cobre" de "não cobre" (ver
`eval/future_feature/cross-encoder.md` §1).

O **cross-encoder** cola pergunta e chunk num input só (`[pergunta] [SEP] [chunk]`)
e roda o transformer inteiro sobre os dois, devolvendo UM número: "quão bem este
trecho responde esta pergunta". A atenção cruza as duas metades — o que o
bi-encoder não faz. O preço: não dá para pré-calcular (precisa da pergunta, que
só existe em runtime), são N forward passes por pergunta. Por isso ele é 2º
estágio: o E5 faz a peneira grossa (`RERANKER_CANDIDATES` candidatos, barato) e o
cross-encoder reordena só esses (caro, mas sobre poucos itens). Ver
`app/retrieval/retriever.retrieve`.

Espelha `app/providers/embeddings.py`: modelo local, carga offline-first,
`@lru_cache` de processo. A diferença de contrato: o score que sai daqui está na
ESCALA DO CROSS-ENCODER (sigmoid do logit), não é comparável com o score de E5
histórico na telemetria — ver `RERANKER_THRESHOLD` em `app/core/config.py` e o
campo `score_top_bruto` em `app/core/telemetry.Registro`.

Se `RERANKER_ENABLED=false` (o padrão), nada aqui é carregado — o `retrieve`
segue o caminho bi-encoder de sempre e `get_reranker()` nunca é chamado.
"""

import logging
import math

from functools import lru_cache
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.models import RetrievedChunk

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)


def _carregar_modelo() -> "CrossEncoder":
    """Modelo local. Tenta primeiro offline (sem bater no HF Hub) — igual
    `embeddings._modelo_local`.

    Depois do primeiro download o modelo já está em cache; checar a rede a cada
    boot só custa latência. Só volta a acessar a rede se ainda não houver nada
    salvo localmente.
    """
    from sentence_transformers import CrossEncoder

    try:
        return CrossEncoder(settings.reranker_model, local_files_only=True)
    except (OSError, ValueError):
        return CrossEncoder(settings.reranker_model)


@lru_cache(maxsize=1)
def get_reranker() -> "CrossEncoder":
    """O cross-encoder do processo. Carregado uma vez, no warm-up
    (`db/vector_store.aquecer`, só quando `RERANKER_ENABLED=true`)."""
    logger.info("warm-up: carregando reranker cross-encoder (%s)...", settings.reranker_model)
    return _carregar_modelo()


def _sigmoid(x: float) -> float:
    # `CrossEncoder.predict` do modelo padrão devolve logit cru (faixa aberta);
    # a sigmoid traz para 0..1 e mantém o contrato de `RetrievedChunk.score`
    # ("relevância 0..1"). Um modelo que já aplique sigmoid na cabeça (raro nos
    # rerankers de MS MARCO) faria isto comprimir para ~0.5..0.73 — a calibração
    # de `RERANKER_THRESHOLD` (T-1) pegaria o desvio na hora.
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def rerank(
    pergunta: str,
    chunks: list[RetrievedChunk],
    modelo: "CrossEncoder | None" = None,
) -> list[RetrievedChunk]:
    """Reordena `chunks` pela relevância cross-encoder à `pergunta`.

    Função PURA: monta os pares `(pergunta, chunk.page_content)`, chama
    `CrossEncoder.predict` (batched), devolve uma NOVA lista ordenada por score
    decrescente com `RetrievedChunk.score` reescrito para a escala da sigmoid e
    `score_bruto` guardando o score de E5 que veio do 1º estágio. Não conhece o
    pgvector nem o agente. `modelo` injetável para o dublê dos testes.

    Lista vazia devolve lista vazia (o `retrieve` corta antes de chegar aqui só
    quando não há candidato nenhum).
    """
    if not chunks:
        return []

    modelo = modelo or get_reranker()
    pares = [(pergunta, c.document.page_content) for c in chunks]
    scores = modelo.predict(pares)

    rerankeados = [
        RetrievedChunk(document=c.document, score=_sigmoid(float(s)), score_bruto=c.score)
        for c, s in zip(chunks, scores)
    ]
    rerankeados.sort(key=lambda c: c.score, reverse=True)
    return rerankeados
