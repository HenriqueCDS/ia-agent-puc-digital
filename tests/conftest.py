"""Isolamento global da suíte.

O cache PRÉ-RETRIEVAL (`app/db/pre_retrieval_cache.py`) abre sessão no Postgres —
para ler um hit, gravar a resposta da base e para a limpeza que a ingestão dispara.
Nenhum teste pode tocar o banco real (é a regra da suíte: dublês para vector
store, LLM, cache, busca e relógio).

Esta fixture autouse neutraliza o cache pré-retrieval em TODA a suíte, no estado
"como se a feature não existisse": leitura sempre miss, escrita e limpeza no-op.
Assim nenhum teste anterior a ela muda de comportamento. Quem precisa do
comportamento real:

- `test_pre_retrieval_cache.py` chama as funções do módulo direto, com um store
  falso — a fixture não mexe no módulo de origem (`app.db.pre_retrieval_cache`);
- os testes de `responder` que exercem o cache instalam um dict em memória por
  cima (fixture explícita roda depois da autouse).
"""

import sys

import pytest

# O módulo de origem das funções: os testes dele usam as versões reais.
_MODULO_ORIGEM = "app.db.pre_retrieval_cache"

_NOOP_CACHE = {
    "get_cached_pre_retrieval": lambda *a, **k: None,
    "set_cached_pre_retrieval": lambda *a, **k: None,
    "clear_pre_retrieval_cache": lambda *a, **k: 0,
}


@pytest.fixture(autouse=True)
def _pre_retrieval_cache_isolado(monkeypatch):
    """Substitui `get/set/clear_pre_retrieval` por no-ops em todo módulo `app.*`
    ou `scripts.*` que as tenha importado. Cobre `responder` (runtime) e os
    scripts de admin (`ingest`/`crawl`/`remove_ingested`/`clear_cache`) sem
    precisar listar cada um — patcha o que já está em `sys.modules`, que é
    exatamente o que o arquivo de teste em execução pôde importar."""
    for nome, modulo in list(sys.modules.items()):
        if modulo is None or nome == _MODULO_ORIGEM:
            continue
        if not (nome.startswith("app.") or nome.startswith("scripts.")):
            continue
        for func, noop in _NOOP_CACHE.items():
            if hasattr(modulo, func):
                monkeypatch.setattr(modulo, func, noop)
