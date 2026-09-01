"""Reranker cross-encoder (`app/retrieval/reranker.py`) — a função pura.

Sem modelo de verdade: um dublê `FakeCrossEncoder` devolve scores por uma regra
do teste, à la `EspiaoDeEmbeddings` em test_embeddings.py. Carregar o
cross-encoder real custaria minutos e não provaria nada além do que o
sentence-transformers já testa — o que pode quebrar aqui é a montagem dos pares,
a sigmoid e a reordenação.
"""

import math

from langchain_core.documents import Document

from app.core.models import RetrievedChunk
from app.retrieval.reranker import _sigmoid, rerank


class FakeCrossEncoder:
    """Dublê: guarda os pares recebidos e devolve um logit por par.

    `regra(pergunta, texto) -> float` decide o logit; o default pontua pelo
    tamanho do texto, o bastante para uma ordem determinística nos testes.
    """

    def __init__(self, regra=None):
        self.regra = regra or (lambda p, t: float(len(t)))
        self.pares_recebidos = None

    def predict(self, pares):
        self.pares_recebidos = list(pares)
        return [self.regra(p, t) for p, t in self.pares_recebidos]


def _chunk(texto, score):
    return RetrievedChunk(document=Document(page_content=texto), score=score)


def test_monta_pares_pergunta_chunk():
    modelo = FakeCrossEncoder()
    chunks = [_chunk("resposta A", 0.81), _chunk("resposta B", 0.82)]

    rerank("como faço X?", chunks, modelo=modelo)

    assert modelo.pares_recebidos == [
        ("como faço X?", "resposta A"),
        ("como faço X?", "resposta B"),
    ]


def test_reordena_por_score_decrescente_e_reescreve_score():
    # logit = -1 para "curto", +2 para "texto bem mais longo" -> o longo sobe.
    modelo = FakeCrossEncoder(regra=lambda p, t: 2.0 if len(t) > 10 else -1.0)
    chunks = [_chunk("curto", 0.90), _chunk("texto bem mais longo", 0.10)]

    out = rerank("q", chunks, modelo=modelo)

    assert [c.document.page_content for c in out] == ["texto bem mais longo", "curto"]
    assert out[0].score == _sigmoid(2.0)
    assert out[1].score == _sigmoid(-1.0)


def test_preserva_o_score_do_e5_em_score_bruto():
    modelo = FakeCrossEncoder(regra=lambda p, t: 0.0)
    chunks = [_chunk("a", 0.83), _chunk("b", 0.79)]

    out = rerank("q", chunks, modelo=modelo)

    assert sorted(c.score_bruto for c in out) == [0.79, 0.83]
    assert all(0.0 <= c.score <= 1.0 for c in out)


def test_lista_vazia_devolve_vazia():
    assert rerank("q", [], modelo=FakeCrossEncoder()) == []


def test_sigmoid_traz_logit_para_0_1():
    assert _sigmoid(0.0) == 0.5
    assert _sigmoid(10.0) > 0.99
    assert _sigmoid(-10.0) < 0.01
    # não estoura para logit muito negativo (o ramo `x < 0` evita overflow)
    assert _sigmoid(-1000.0) == 0.0
    assert math.isclose(_sigmoid(1000.0), 1.0)


# --- warm-up (INF-8 + RET-3) ----------------------------------------------


class _FakeStore:
    def session_maker(self):
        class _Ctx:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def execute(self_, *a):
                return None

        return _Ctx()


def _preparar_aquecer(monkeypatch):
    import app.db.vector_store as vs
    import app.retrieval.reranker as reranker_mod

    monkeypatch.setattr(vs, "get_vector_store", _FakeStore)
    carregou = []
    monkeypatch.setattr(reranker_mod, "_carregar_modelo", lambda: carregou.append(True) or object())
    reranker_mod.get_reranker.cache_clear()
    return vs, carregou


def test_aquecer_carrega_o_reranker_quando_ligado(monkeypatch):
    vs, carregou = _preparar_aquecer(monkeypatch)
    monkeypatch.setattr(vs.settings, "reranker_enabled", True)

    vs.aquecer()

    assert carregou == [True]


def test_aquecer_nao_toca_no_reranker_quando_desligado(monkeypatch):
    vs, carregou = _preparar_aquecer(monkeypatch)
    monkeypatch.setattr(vs.settings, "reranker_enabled", False)

    vs.aquecer()

    assert carregou == []
