"""Testes do cache de respostas (ver app/db/response_cache.py).

Sem banco: o que precisa ser travado aqui é o COMPORTAMENTO de `_ensure_table`
— que ele reaja a uma tabela que sumiu em runtime, em vez de garantir a criação
uma única vez por processo (INF-11).
"""

from app.db import response_cache


class _FakeResult:
    def __init__(self, valor):
        self._valor = valor

    def first(self):
        return self._valor

    # `set_cached_answer`/`clear_cache` leem `.rowcount`; não é o foco aqui.
    rowcount = 0


class _FakeSession:
    """Sessão falsa: responde à checagem de catálogo por um estado mutável e
    conta quantas DDL rodaram."""

    def __init__(self, estado: dict):
        self._estado = estado
        self.ddl = 0
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "information_schema.columns" in sql:
            return _FakeResult(1 if self._estado["pronta"] else None)
        if "CREATE TABLE" in sql or "ALTER TABLE" in sql:
            self.ddl += 1
            return _FakeResult(None)
        return _FakeResult(None)

    def commit(self):
        self.commits += 1


class _FakeStore:
    def __init__(self, estado: dict):
        self._estado = estado
        self.sessao = _FakeSession(estado)

    def session_maker(self):
        return self.sessao


def test_ensure_table_nao_roda_ddl_quando_a_tabela_ja_esta_pronta():
    store = _FakeStore({"pronta": True})

    response_cache._ensure_table(store)

    assert store.sessao.ddl == 0
    assert store.sessao.commits == 0


def test_ensure_table_cria_tabela_e_migra_coluna_quando_falta():
    store = _FakeStore({"pronta": False})

    response_cache._ensure_table(store)

    assert store.sessao.ddl == 2  # CREATE TABLE + ALTER TABLE ADD COLUMN
    assert store.sessao.commits == 1


def test_ensure_table_recria_apos_drop_em_runtime():
    """INF-11: o motivo de largar o `@lru_cache`. Uma tabela dropada em runtime
    (teste, manutenção) é recriada no acesso seguinte — antes o processo ficava
    quebrado até o restart porque a DDL nunca mais rodava."""
    estado = {"pronta": False}
    store = _FakeStore(estado)

    response_cache._ensure_table(store)
    assert store.sessao.ddl == 2

    estado["pronta"] = True  # criada com sucesso
    response_cache._ensure_table(store)
    assert store.sessao.ddl == 2  # nada a fazer

    estado["pronta"] = False  # alguém dropou a tabela
    response_cache._ensure_table(store)
    assert store.sessao.ddl == 4  # rodou a DDL de novo


def test_checagem_e_pela_coluna_modelo_e_pelo_schema_corrente():
    """Guarda contra "simplificar" para um `to_regclass` da tabela só: isso
    reabriria o buraco da migração da coluna `modelo` numa base anterior a ela,
    e casaria uma tabela homônima de outro schema."""
    sql = str(response_cache._TABELA_PRONTA)
    assert "column_name = 'modelo'" in sql
    assert "current_schema()" in sql
