"""Testes do cache pré-retrieval (ver app/db/pre_retrieval_cache.py).

Sem banco: dublê de store que responde à checagem de catálogo por um estado
mutável e registra as instruções executadas. Trava o que importa aqui — que
`_ensure_table` reaja a uma tabela dropada em runtime (INF-11, igual
`response_cache`), e o round-trip de `fontes` (dict -> JSONB -> dict).
"""

import json

from app.db import pre_retrieval_cache


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows) if isinstance(rows, list) else 0

    def first(self):
        return self._rows[0] if self._rows else None


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSession:
    def __init__(self, estado: dict):
        self._estado = estado
        self.ddl = 0
        self.commits = 0
        self.gravado: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "information_schema.tables" in sql:
            return _FakeResult([_Row(ok=1)] if self._estado["pronta"] else [])
        if "CREATE TABLE" in sql:
            self.ddl += 1
            return _FakeResult([])
        if sql.strip().startswith("INSERT"):
            self.gravado = dict(params)
            self._estado.setdefault("linhas", {})[params["cache_key"]] = params
            return _FakeResult([])
        if "SELECT resposta" in sql:
            linha = self._estado.get("linhas", {}).get(params["cache_key"])
            if linha is None:
                return _FakeResult([])
            return _FakeResult([_Row(resposta=linha["resposta"], fontes=linha["fontes"])])
        if sql.strip().startswith("DELETE"):
            n = len(self._estado.get("linhas", {}))
            self._estado["linhas"] = {}
            return _FakeResult([None] * n)
        return _FakeResult([])

    def commit(self):
        self.commits += 1


class _FakeStore:
    def __init__(self, estado: dict):
        self._estado = estado
        self.sessao = _FakeSession(estado)

    def session_maker(self):
        return self.sessao


def test_ensure_table_nao_roda_ddl_quando_ja_existe():
    store = _FakeStore({"pronta": True})

    pre_retrieval_cache._ensure_table(store)

    assert store.sessao.ddl == 0
    assert store.sessao.commits == 0


def test_ensure_table_cria_quando_falta():
    store = _FakeStore({"pronta": False})

    pre_retrieval_cache._ensure_table(store)

    assert store.sessao.ddl == 1
    assert store.sessao.commits == 1


def test_ensure_table_recria_apos_drop_em_runtime():
    """INF-11: mesmo motivo de `response_cache` para não usar `@lru_cache` — uma
    tabela dropada em runtime é recriada no acesso seguinte."""
    estado = {"pronta": False}
    store = _FakeStore(estado)

    pre_retrieval_cache._ensure_table(store)
    assert store.sessao.ddl == 1

    estado["pronta"] = True
    pre_retrieval_cache._ensure_table(store)
    assert store.sessao.ddl == 1  # nada a fazer

    estado["pronta"] = False  # alguém dropou
    pre_retrieval_cache._ensure_table(store)
    assert store.sessao.ddl == 2


def test_set_e_get_faz_round_trip_das_fontes():
    estado = {"pronta": True}
    store = _FakeStore(estado)
    fontes = [{"id": "c1", "page_content": "trecho", "metadata": {"page": 0}, "score": 0.9}]

    pre_retrieval_cache.set_cached_pre_retrieval(
        "chave", "como envio", "canvas", "Resposta.\n#TOPICO: envio", fontes, "gemini:x", store
    )
    # O que foi para o driver: fontes serializadas como texto JSON (CAST no SQL).
    assert json.loads(store.sessao.gravado["fontes"]) == fontes

    resultado = pre_retrieval_cache.get_cached_pre_retrieval("chave", store)
    assert resultado == ("Resposta.\n#TOPICO: envio", fontes)


def test_get_devolve_none_quando_nao_ha_entrada():
    store = _FakeStore({"pronta": True})

    assert pre_retrieval_cache.get_cached_pre_retrieval("inexistente", store) is None


def test_get_aceita_fontes_ja_desserializadas_pelo_driver():
    """psycopg devolve JSONB como list/dict; o SQLite-like devolve str. O getter
    tem que aceitar os dois."""
    estado = {"pronta": True, "linhas": {"k": {"resposta": "R", "fontes": [{"score": 0.5}]}}}
    store = _FakeStore(estado)

    resposta, fontes = pre_retrieval_cache.get_cached_pre_retrieval("k", store)
    assert fontes == [{"score": 0.5}]


def test_clear_apaga_tudo_e_conta():
    estado = {"pronta": True}
    store = _FakeStore(estado)
    pre_retrieval_cache.set_cached_pre_retrieval("a", "p", None, "R", [], None, store)
    pre_retrieval_cache.set_cached_pre_retrieval("b", "p", None, "R", [], None, store)

    assert pre_retrieval_cache.clear_pre_retrieval_cache(store) == 2
    assert pre_retrieval_cache.get_cached_pre_retrieval("a", store) is None
