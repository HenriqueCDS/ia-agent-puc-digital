"""Loader de `modelos_resposta_chunks.xlsx` (app/ingestion/loaders/xlsx_modelos_resposta.py).

Cada teste escreve um `.xlsx` sintético num diretório temporário — nada de
banco, nada do arquivo real do projeto. O que precisa ser travado é a
TRANSFORMAÇÃO (linha de planilha -> `Document`), não o conteúdo de um arquivo
específico.
"""

import datetime

import numpy as np
import pandas as pd
import pytest
from langchain_core.documents import Document

from app.ingestion.loaders.registry import loader_for
from app.ingestion.loaders.xlsx_modelos_resposta import ModelosRespostaXlsxLoader

_COLUNAS = [
    "chunk_id", "modelo_id", "tipo", "parte", "assunto", "assuntos_variantes",
    "slots", "redirecionado", "n_ocorrencias", "n_threads", "primeira_data",
    "ultima_data", "ano", "n_chars", "tokens_aprox", "texto",
]


def _linha(**overrides) -> dict:
    base = {
        "chunk_id": "MOD0001-001",
        "modelo_id": "MOD0001",
        "tipo": "modelo",
        "parte": np.int64(1),
        "assunto": "Urgente - Matricula pendente",
        "assuntos_variantes": "Urgente - Matricula pendente; RE: Urgente - Matricula pendente",
        "slots": "nome; valor",
        "redirecionado": False,
        "n_ocorrencias": np.int64(56),
        "n_threads": np.int64(4),
        "primeira_data": pd.Timestamp("2026-04-17 00:43:27"),
        "ultima_data": pd.Timestamp("2026-08-06 16:50:11"),
        "ano": np.int64(2026),
        "n_chars": np.int64(728),
        "tokens_aprox": np.int64(182),
        "texto": "Boa tarde, prezado, {{nome}}!\n\nInformamos que sua matrícula...",
    }
    base.update(overrides)
    return base


def _xlsx(tmp_path, linhas: list[dict], aba: str = "chunks"):
    caminho = tmp_path / "modelos_resposta_chunks.xlsx"
    df = pd.DataFrame(linhas, columns=_COLUNAS)
    with pd.ExcelWriter(caminho, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=aba, index=False)
        # Segunda aba, como no arquivo real (`modelos`) — o loader tem que
        # ignorá-la, não só saber que `chunks` existe.
        pd.DataFrame({"modelo_id": ["MOD0001"], "texto_modelo": ["x"]}).to_excel(
            xw, sheet_name="modelos", index=False
        )
    return caminho


def test_uma_linha_vira_um_document_com_texto_como_page_content(tmp_path):
    caminho = _xlsx(tmp_path, [_linha()])

    docs = ModelosRespostaXlsxLoader(str(caminho)).load()

    assert len(docs) == 1
    assert docs[0].page_content == _linha()["texto"]


def test_cada_linha_vira_um_document_na_ordem_da_planilha(tmp_path):
    caminho = _xlsx(tmp_path, [
        _linha(chunk_id="MOD0001-001", texto="primeiro"),
        _linha(chunk_id="MOD0001-002", parte=np.int64(2), texto="segundo"),
    ])

    docs = ModelosRespostaXlsxLoader(str(caminho)).load()

    assert [d.page_content for d in docs] == ["primeiro", "segundo"]
    assert [d.metadata["chunk_id"] for d in docs] == ["MOD0001-001", "MOD0001-002"]


def test_coluna_assunto_e_renomeada_para_nao_colidir_com_o_topico_da_pasta(tmp_path):
    """`ingestion/pipeline._enrich` grava `metadata["assunto"]` com o TÓPICO da
    pasta (`email_modelos`) via `dict.update`, que sobrescreve sem avisar. Se o
    loader entregasse a coluna como `assunto`, o assunto do e-mail original
    desapareceria no instante da ingestão — silencioso, sem exceção."""
    caminho = _xlsx(tmp_path, [_linha(assunto="Urgente - Matricula pendente")])

    metadata = ModelosRespostaXlsxLoader(str(caminho)).load()[0].metadata

    assert metadata["assunto_email"] == "Urgente - Matricula pendente"
    assert "assunto" not in metadata


def test_texto_nao_e_duplicado_na_metadata(tmp_path):
    caminho = _xlsx(tmp_path, [_linha()])

    metadata = ModelosRespostaXlsxLoader(str(caminho)).load()[0].metadata

    assert "texto" not in metadata


# --- Segurança de tipo para o jsonb (o motivo de existir `_json_seguro`) ----


def test_metadata_nao_tem_tipos_do_numpy(tmp_path):
    """`json.dumps` (o que grava `cmetadata` no Postgres) não serializa
    `numpy.int64`/`numpy.bool_`. Sem a conversão, isso só falharia dentro de
    `store.add_documents`, depois do embedding já pago — o pior lugar para
    descobrir um tipo errado."""
    caminho = _xlsx(tmp_path, [_linha()])

    metadata = ModelosRespostaXlsxLoader(str(caminho)).load()[0].metadata

    for chave, valor in metadata.items():
        assert not isinstance(valor, np.generic), f"{chave} ainda é {type(valor)}"
    assert isinstance(metadata["parte"], int)
    assert isinstance(metadata["n_ocorrencias"], int)
    assert isinstance(metadata["redirecionado"], bool)


def test_datas_viram_string_iso(tmp_path):
    caminho = _xlsx(tmp_path, [_linha(
        primeira_data=pd.Timestamp("2026-04-17 00:43:27"),
        ultima_data=pd.Timestamp("2026-08-06 16:50:11"),
    )])

    metadata = ModelosRespostaXlsxLoader(str(caminho)).load()[0].metadata

    assert metadata["primeira_data"] == datetime.datetime(2026, 4, 17, 0, 43, 27).isoformat()
    assert isinstance(metadata["ultima_data"], str)


def test_slot_vazio_vira_none_e_nao_nan(tmp_path):
    """`slots` é NaN (float) para templates sem campo a preencher — 62 das 200
    linhas do arquivo real. `float('nan')` não é `null` em JSON válido; sem a
    conversão, o valor gravado dependeria de como o driver lida com NaN."""
    caminho = _xlsx(tmp_path, [_linha(slots=float("nan"))])

    metadata = ModelosRespostaXlsxLoader(str(caminho)).load()[0].metadata

    assert metadata["slots"] is None


# --- Falha alta e clara quando o arquivo não é o formato esperado ----------


def test_aba_chunks_ausente_da_erro_claro_em_vez_de_stack_do_pandas(tmp_path):
    caminho = tmp_path / "outro.xlsx"
    pd.DataFrame({"x": [1]}).to_excel(caminho, sheet_name="outra_aba", index=False)

    with pytest.raises(ValueError, match="chunks"):
        ModelosRespostaXlsxLoader(str(caminho)).load()


def test_arquivo_sem_linhas_devolve_lista_vazia_sem_explodir(tmp_path, caplog):
    caminho = _xlsx(tmp_path, [])

    with caplog.at_level("WARNING"):
        docs = ModelosRespostaXlsxLoader(str(caminho)).load()

    assert docs == []
    assert "nenhuma linha" in caplog.text


# --- Fio até o registry (é por aqui que a ingestão de verdade descobre o loader) ---


def test_registry_reconhece_xlsx_e_devolve_o_loader_certo(tmp_path):
    caminho = _xlsx(tmp_path, [_linha()])

    loader = loader_for(caminho)

    assert isinstance(loader, ModelosRespostaXlsxLoader)
    assert loader.load()[0].page_content == _linha()["texto"]
