"""Prefixo de instrução do modelo de embeddings (`app/providers/embeddings.py`).

Sem modelo de verdade: o dublê registra o texto que RECEBERIA para embedar, que
é exatamente o que precisa ser verificado. Carregar o e5 aqui custaria minutos e
não provaria nada a mais — o que pode quebrar é a montagem do prefixo, não a
multiplicação de matriz do sentence-transformers.

Estes testes existem porque a falha que eles pegam é silenciosa. Um e5 sem os
prefixos `query: `/`passage: ` continua devolvendo vetores, a busca continua
respondendo e nenhum log muda — só o ranking piora. Sem teste, a regressão só
apareceria como "o agente respondia melhor mês passado".
"""

import pytest
from langchain_core.embeddings import Embeddings

from app.providers import embeddings as embeddings_mod
from app.providers.embeddings import PrefixoDeInstrucao


class EspiaoDeEmbeddings(Embeddings):
    """Guarda os textos recebidos e devolve um vetor qualquer, do tamanho certo."""

    def __init__(self):
        self.documentos: list[str] = []
        self.perguntas: list[str] = []

    def embed_documents(self, texts):
        self.documentos.extend(texts)
        return [[0.0, 1.0] for _ in texts]

    def embed_query(self, text):
        self.perguntas.append(text)
        return [0.0, 1.0]


@pytest.fixture
def espiao():
    return EspiaoDeEmbeddings()


def test_pergunta_e_documento_recebem_prefixos_DIFERENTES(espiao):
    """O ponto inteiro da classe. e5 é treinado com instrução assimétrica, e é
    justamente isso que o `HuggingFaceEmbeddings` não consegue fazer sozinho —
    o `embed_query` dele delega para `embed_documents`, com um `encode_kwargs`
    só para os dois caminhos."""
    com_prefixo = PrefixoDeInstrucao(
        espiao, prefixo_pergunta="query: ", prefixo_documento="passage: "
    )

    com_prefixo.embed_documents(["Para enviar a atividade, acesse Tarefas."])
    com_prefixo.embed_query("como envio a atividade?")

    assert espiao.documentos == ["passage: Para enviar a atividade, acesse Tarefas."]
    assert espiao.perguntas == ["query: como envio a atividade?"]


def test_todo_documento_do_lote_recebe_o_prefixo(espiao):
    """A ingestão embeda em lote (um `embed_documents` por arquivo): um prefixo
    aplicado só ao primeiro do lote deixaria a base inteira inconsistente."""
    com_prefixo = PrefixoDeInstrucao(espiao, prefixo_pergunta="q: ", prefixo_documento="p: ")

    com_prefixo.embed_documents(["um", "dois", "tres"])

    assert espiao.documentos == ["p: um", "p: dois", "p: tres"]


def test_vetor_e_devolvido_intacto(espiao):
    """O wrapper mexe no texto de entrada, nunca no vetor de saída."""
    com_prefixo = PrefixoDeInstrucao(espiao, prefixo_pergunta="q: ", prefixo_documento="p: ")

    assert com_prefixo.embed_query("x") == [0.0, 1.0]
    assert com_prefixo.embed_documents(["x", "y"]) == [[0.0, 1.0], [0.0, 1.0]]


# --- A escolha entre modelo cru e modelo com prefixo ------------------------


@pytest.fixture
def sem_cache_de_modelo(monkeypatch, espiao):
    """`get_embeddings` é `lru_cache`; sem limpar, o 1º teste fixaria o resultado
    dos demais. Também dubla o carregamento do modelo — nenhum teste aqui pode
    baixar o e5 nem tocar em disco."""
    monkeypatch.setattr(embeddings_mod, "_modelo_local", lambda: espiao)
    embeddings_mod.get_embeddings.cache_clear()
    yield espiao
    embeddings_mod.get_embeddings.cache_clear()


def test_modelo_com_instrucao_vem_embrulhado(sem_cache_de_modelo, monkeypatch):
    monkeypatch.setattr(embeddings_mod.settings, "embedding_query_prefix", "query: ")
    monkeypatch.setattr(embeddings_mod.settings, "embedding_passage_prefix", "passage: ")

    assert isinstance(embeddings_mod.get_embeddings(), PrefixoDeInstrucao)


def test_modelo_sem_instrucao_nao_e_embrulhado(sem_cache_de_modelo, monkeypatch):
    """Trocar o e5 por um modelo que não usa instrução (BGE-M3, `paraphrase-*`)
    é edição de `.env`, não de código: com os dois prefixos vazios o wrapper sai
    de cena em vez de concatenar string vazia em todo chunk da base."""
    monkeypatch.setattr(embeddings_mod.settings, "embedding_query_prefix", "")
    monkeypatch.setattr(embeddings_mod.settings, "embedding_passage_prefix", "")

    assert embeddings_mod.get_embeddings() is sem_cache_de_modelo
