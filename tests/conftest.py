"""Isolamento global da suíte.

Os dois caches em Postgres — PRÉ-RETRIEVAL (`app/db/pre_retrieval_cache.py`) e
de RESPOSTA (`app/db/response_cache.py`, usado por `_tentar_base`) — abrem
sessão no banco e, no caminho de resposta, isso passa por `get_vector_store()`
-> `get_embeddings()`: sem dublê, um teste que só queria exercitar o guardrail/
triagem acaba tentando baixar o modelo e5 de verdade (rede/HF Hub). Nenhum
teste pode tocar banco nem rede real (é a regra da suíte: dublês para vector
store, LLM, cache, busca e relógio).

Esta fixture autouse neutraliza OS DOIS caches em TODA a suíte, no estado
"como se a feature não existisse": leitura sempre miss, escrita e limpeza
no-op. Assim nenhum teste anterior a ela muda de comportamento. Quem precisa do
comportamento real:

- `test_pre_retrieval_cache.py`/testes de `response_cache` chamam as funções do
  módulo direto, com um store falso — a fixture não mexe nos módulos de origem;
- os testes de `responder` que exercem um dos caches instalam um dict em
  memória por cima (fixture explícita roda depois da autouse).
"""

import sys

import pytest

# Os módulos de origem das funções: os testes deles usam as versões reais.
_MODULOS_ORIGEM = {"app.db.pre_retrieval_cache", "app.db.response_cache"}

_NOOP_CACHE = {
    "get_cached_pre_retrieval": lambda *a, **k: None,
    "set_cached_pre_retrieval": lambda *a, **k: None,
    "clear_pre_retrieval_cache": lambda *a, **k: 0,
    "get_cached_answer": lambda *a, **k: None,
    "set_cached_answer": lambda *a, **k: None,
    "clear_cache": lambda *a, **k: 0,
}


@pytest.fixture(autouse=True)
def _cache_isolado(monkeypatch):
    """Substitui as funções de cache acima por no-ops em todo módulo `app.*`
    ou `scripts.*` que as tenha importado. Cobre `responder` (runtime) e os
    scripts de admin (`ingest`/`crawl`/`remove_ingested`/`clear_cache`) sem
    precisar listar cada um — patcha o que já está em `sys.modules`, que é
    exatamente o que o arquivo de teste em execução pôde importar."""
    for nome, modulo in list(sys.modules.items()):
        if modulo is None or nome in _MODULOS_ORIGEM:
            continue
        if not (nome.startswith("app.") or nome.startswith("scripts.")):
            continue
        for func, noop in _NOOP_CACHE.items():
            if hasattr(modulo, func):
                monkeypatch.setattr(modulo, func, noop)
