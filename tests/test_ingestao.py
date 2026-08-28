"""KB-4 — a ingestão precisa conseguir LER todo arquivo colocado em data/raw/.

Não é um teste que *reprova* fonte inválida: a decisão de KB-1 foi manter o
`modelos_resposta_chunks.xlsx` indexado de propósito (modelos reais de
atendimento extraídos do e-mail). O que este teste garante é o contrário — que
nada dentro de `data/raw/` fique de fora da ingestão em silêncio por falta de
loader registrado (`pipeline.ingest_assunto` só faz `report.ignorados.append`,
sem falhar).

Se um arquivo novo cair aqui sem loader, uma de duas coisas é verdade: ou ele
não deveria estar em `data/raw/` (mover para outro lugar), ou falta registrar o
loader em `app/ingestion/loaders/registry.py`.
"""

from pathlib import Path

import pytest

from app.core.config import DATA_RAW_DIR
from app.ingestion.loaders.registry import loader_for, supported_extensions

# Arquivos de controle do Git / do editor: existem para versionar a pasta vazia
# ou são lixo de sistema, e não são conteúdo a indexar.
_IGNORAR = {".gitkeep", ".gitignore", ".DS_Store", "Thumbs.db"}


def _arquivos_de_conteudo() -> list[Path]:
    if not DATA_RAW_DIR.is_dir():
        return []
    return [
        p
        for p in sorted(DATA_RAW_DIR.rglob("*"))
        if p.is_file() and p.name not in _IGNORAR
    ]


def test_todo_arquivo_em_data_raw_tem_loader():
    arquivos = _arquivos_de_conteudo()
    if not arquivos:
        pytest.skip("data/raw/ sem arquivos de conteúdo neste ambiente")

    sem_loader = [str(p.relative_to(DATA_RAW_DIR)) for p in arquivos if loader_for(p) is None]

    assert not sem_loader, (
        "arquivo(s) em data/raw/ sem loader registrado — seriam ignorados na "
        f"ingestão sem erro: {sem_loader}. Extensões suportadas: "
        f"{sorted(supported_extensions())}"
    )
