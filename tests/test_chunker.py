from langchain_core.documents import Document

from app.core.config import settings
from app.ingestion.chunker import (
    chunk_id,
    content_hash,
    deduplicar_similares,
    split_documents,
)


def _doc(texto: str, **meta) -> Document:
    return Document(page_content=texto, metadata={"source_path": "/data/raw/canvas/x.pdf", **meta})


def _chunk(texto: str) -> Document:
    return Document(page_content=texto, metadata={})


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


# --- RET-6: dedup de chunks quase idênticos ---------------------------------


# Um "chunk" do tamanho de uma página real do Canvas_Student_Guide (~90 palavras):
# o texto que muda de página para página (número do rodapé) é uma fração pequena,
# então a similaridade fica bem acima do limiar. É esse o caso que a RET-6 mira.
_BASE = (
    "Para enviar uma atividade no Canvas você deve primeiro abrir o curso no menu "
    "lateral e localizar a seção de Atividades dentro da navegação do curso. Ao "
    "encontrar a atividade desejada clique no título dela para ver as instruções "
    "completas o prazo de entrega e a pontuação atribuída pelo professor. Em "
    "seguida use o botão Enviar Tarefa no canto superior direito da página anexe "
    "o arquivo do seu computador ou escolha uma entrega por texto e revise tudo "
    "antes de confirmar. Depois de confirmar o Canvas mostra a data e a hora do "
    "envio e você pode reenviar caso o professor permita novas tentativas."
)


def test_dedup_descarta_quase_copia_que_o_hash_exato_nao_pega():
    """O caso do Canvas_Student_Guide: mesma instrução repetida em várias páginas,
    diferindo só no número da página / rodapé."""
    chunks = [
        _chunk(_BASE + " Página 12 de 1108."),
        _chunk(_BASE + " Página 47 de 1108."),
        _chunk(_BASE + " Página 900 de 1108."),
    ]
    # content_hash NÃO pega (os textos diferem)
    assert len({content_hash(c.page_content) for c in chunks}) == 3

    mantidos = deduplicar_similares(chunks, limiar=0.9)
    assert [c.page_content for c in mantidos] == [chunks[0].page_content]  # 1ª vence


def test_dedup_preserva_chunks_de_conteudo_distinto():
    chunks = [
        _chunk("Para enviar uma atividade no Canvas, use o botão Enviar Tarefa."),
        _chunk("Para ver suas notas no Canvas, abra o menu Notas dentro do curso."),
        _chunk("O calendário acadêmico com prazos de prova fica na Secretaria Geral."),
    ]
    assert len(deduplicar_similares(chunks, limiar=0.9)) == 3


def test_dedup_desligado_com_limiar_zero():
    chunks = [_chunk(_BASE), _chunk(_BASE), _chunk(_BASE)]
    assert len(deduplicar_similares(chunks, limiar=0)) == 3


def test_dedup_integrado_no_split_documents(monkeypatch):
    monkeypatch.setattr(settings, "ingest_dedup_similaridade", 0.9)
    # duas "páginas" com o mesmo parágrafo repetido
    docs = [_doc(_BASE + " Rodapé A.", page=1), _doc(_BASE + " Rodapé B.", page=2)]

    chunks = split_documents(docs)

    assert len(chunks) == 1
    # chunk_index é reindexado sobre a lista já filtrada
    assert chunks[0].metadata["chunk_index"] == 0
