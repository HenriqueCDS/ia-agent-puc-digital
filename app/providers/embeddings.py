"""Fábrica do modelo de embeddings.

Vive em módulo próprio (e não mais em `gemini.py`) porque não tem nada a ver com
o provedor de chat: os embeddings rodam LOCALMENTE, via HuggingFace/
sentence-transformers, para não depender de cota de API — na ingestão é uma
chamada por chunk, contra uma por pergunta no chat. Enquanto a única fábrica do
arquivo era o Gemini isso passava; com vários provedores de chat na cadeia
(`app/providers/chain.py`), manter `get_embeddings` dentro de `gemini.py` seria
só o nome errado no import de quem usa.
"""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings

from app.core.config import settings


class PrefixoDeInstrucao(Embeddings):
    """Aplica o prefixo de instrução do modelo, diferente para pergunta e documento.

    Existe por uma limitação concreta do `HuggingFaceEmbeddings`: o
    `embed_query` dele é literalmente `self.embed_documents([text])[0]`, e há um
    único `encode_kwargs` para os dois caminhos. Não há como pedir a ele um
    prefixo para a pergunta e outro para o chunk — que é exatamente o que a
    família e5 exige (`query: ` vs `passage: `).

    O modo de falha que isto evita é silencioso, e é o motivo de valer uma
    classe: sem os prefixos o modelo continua produzindo vetores, a busca
    continua respondendo e nada aparece em log — só o ranking piora. Bug de
    qualidade não levanta exceção.

    COMPOSIÇÃO, e não herança de `HuggingFaceEmbeddings`: aquela classe é um
    modelo pydantic com `extra="forbid"`, então herdar para acrescentar dois
    campos arrasta a validação dela junto. Aqui só o contrato `Embeddings` do
    LangChain importa — que é o que o PGVector e o `web_fallback` consomem.
    """

    def __init__(self, base: Embeddings, prefixo_pergunta: str, prefixo_documento: str):
        self._base = base
        self._prefixo_pergunta = prefixo_pergunta
        self._prefixo_documento = prefixo_documento

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._base.embed_documents([self._prefixo_documento + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._base.embed_query(self._prefixo_pergunta + text)


def _modelo_local() -> HuggingFaceEmbeddings:
    """Modelo local. Tenta primeiro em modo offline (sem bater no HF Hub) —

    depois do primeiro download o modelo já está em cache, então checar a rede
    a cada execução só custa latência. Só volta a acessar a rede (e baixa/
    atualiza o cache) se ainda não tiver nada salvo localmente — nesse caso,
    usa `hf_token` (se configurado) para maior rate limit no download.
    """
    model_kwargs = {"token": settings.hf_token} if settings.hf_token else {}
    try:
        return HuggingFaceEmbeddings(
            model_name=settings.embedding_model,
            model_kwargs={**model_kwargs, "local_files_only": True},
        )
    except OSError:
        return HuggingFaceEmbeddings(
            model_name=settings.embedding_model,
            model_kwargs=model_kwargs,
        )


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """O modelo de embeddings do processo, já com o prefixo do modelo aplicado.

    Sem prefixo configurado, devolve o modelo cru em vez de um wrapper que só
    concatenaria strings vazias: modelo que não usa instrução (BGE-M3, os
    `paraphrase-*`) é caso legítimo, não exceção — ver
    `settings.embedding_query_prefix`.
    """
    base = _modelo_local()
    if not (settings.embedding_query_prefix or settings.embedding_passage_prefix):
        return base
    return PrefixoDeInstrucao(
        base,
        prefixo_pergunta=settings.embedding_query_prefix,
        prefixo_documento=settings.embedding_passage_prefix,
    )
