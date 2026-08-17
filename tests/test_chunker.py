from langchain_core.documents import Document

from app.core.config import settings
from app.ingestion.chunker import chunk_id, content_hash, split_documents


def _doc(texto: str, **meta) -> Document:
    return Document(page_content=texto, metadata={"source_path": "/data/raw/canvas/x.pdf", **meta})


def test_split_preserva_metadata_de_origem():
    chunks = split_documents([_doc("parágrafo. " * 500, page=3, assunto="canvas")])

    assert len(chunks) > 1
    assert all(c.metadata["assunto"] == "canvas" for c in chunks)
    assert all(c.metadata["page"] == 3 for c in chunks)


def test_split_respeita_chunk_size():
    chunks = split_documents([_doc("palavra " * 2000)])

    # o splitter pode estourar um pouco ao não quebrar palavras; a margem cobre isso
    assert all(len(c.page_content) <= settings.chunk_size * 1.2 for c in chunks)


def test_split_descarta_chunks_vazios():
    assert split_documents([_doc("   \n\n   ")]) == []


def test_chunk_id_e_deterministico_e_unico_por_chunk():
    chunks = split_documents([_doc("conteúdo. " * 500)])
    ids = [chunk_id(c) for c in chunks]

    assert len(set(ids)) == len(ids)
    # reingerir o mesmo arquivo gera os mesmos ids -> upsert, não duplicata
    assert ids == [chunk_id(c) for c in split_documents([_doc("conteúdo. " * 500)])]


def test_content_hash_ignora_espaco_e_caixa():
    assert content_hash("Envie a atividade  até sexta") == content_hash("envie a atividade até sexta")


def test_content_hash_e_igual_para_o_mesmo_texto_em_fontes_diferentes():
    # é essa igualdade que permite achar o mesmo aviso colado em dois PDFs
    a = split_documents([_doc("mesmo aviso. " * 200, source_path="/data/raw/canvas/a.pdf")])
    b = split_documents([_doc("mesmo aviso. " * 200, source_path="/data/raw/canvas/b.pdf")])

    assert [c.metadata["content_hash"] for c in a] == [c.metadata["content_hash"] for c in b]
    # mas os ids continuam distintos, porque dependem do source_path
    assert [chunk_id(c) for c in a] != [chunk_id(c) for c in b]
