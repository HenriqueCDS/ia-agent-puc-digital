"""Loader para `modelos_resposta_chunks.xlsx` — modelos de e-mail da assistente
acadêmica, já sanitizados e pré-chunkados pelo notebook `ler_dados_pst`
(`scripts/Ler_dados_exportados_email/ler_dados_pst.ipynb`).

NÃO é um loader genérico de `.xlsx` — não existe hoje uma segunda forma de
planilha na ingestão, e um loader "qualquer Excel" seria abstração sem segundo
caso de uso para justificá-la. Este entende UMA forma específica: a aba
`chunks` desse arquivo, onde cada LINHA já é um chunk pronto (1 linha = 1
`Document`), e não um documento a ser cortado do zero como PDF/DOCX/TXT.

Por que a aba `chunks`, e não `modelos` (a outra aba do mesmo arquivo): o
notebook gera as duas a partir do mesmo dado — `modelos` é 1 linha por
template inteiro (para revisão humana), `chunks` é a mesma informação já
dividida em pedaços de tamanho seguro para embedding (o notebook já quebra
template longo em `parte` 1..N). Indexar `modelos` duplicaria o conteúdo de
`chunks` em unidades maiores e sem a garantia de tamanho — o próprio notebook
recomenda `chunks` para uso em RAG.

O restante do pipeline (chunk -> embedding -> pgvector) não muda: como
qualquer loader, este entrega `Document`s e devolve o controle para
`ingestion/pipeline.py`. As linhas já vêm pequenas (`tokens_aprox` medido
50-312 no arquivo real, bem dentro do limite de qualquer embedder atual do
projeto), então o `RecursiveCharacterTextSplitter` de `chunker.py` não faz
nada na maioria — só re-corta a minoria acima de `CHUNK_SIZE`, do mesmo jeito
que faria com um parágrafo longo de PDF.
"""

import logging
from pathlib import Path
from typing import Any

import pandas as pd
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

_ABA = "chunks"

# Coluna do e-mail original (o ASSUNTO da mensagem, ex.: "Urgente - Matricula
# pendente") renomeada na metadata. É renomeada, e não descartada, porque tem
# valor real (é o que aparece no `texto` como "Assunto tipico: ..." e ajuda a
# achar o template certo por busca de metadata no futuro).
#
# A renomeação é OBRIGATÓRIA, não estilo: `ingestion/pipeline._enrich` grava
# `metadata["assunto"]` com o TÓPICO da pasta (`email_modelos`, o que filtra o
# retrieval — ver `app/retrieval/retriever.retrieve`) via `dict.update`, que
# SOBRESCREVE sem avisar. Sem a renomeação, todo assunto de e-mail seria
# apagado em silêncio no instante em que vira chunk indexado — um bug
# indetectável por teste que só olha o `page_content`.
_COLUNA_ASSUNTO_EMAIL = "assunto_email"


def _json_seguro(valor: Any) -> Any:
    """Valor de célula -> tipo que o `jsonb` do Postgres aceita sem reclamar.

    Existe porque `pandas.read_excel` devolve tipos do numpy (`int64`,
    `bool_`) e `pandas.Timestamp` para datas — nenhum dos dois é serializável
    por `json.dumps`, que é o que `psycopg`/`langchain-postgres` usa para
    gravar `cmetadata`. Sem esta conversão, a falha não aparece na ingestão:
    aparece como `TypeError: Object of type int64 is not JSON serializable`
    no meio de `store.add_documents`, depois do embedding já ter sido pago —
    o pior lugar possível para descobrir um tipo errado.
    """
    if pd.isna(valor):
        return None
    if isinstance(valor, pd.Timestamp):
        return valor.isoformat()
    if hasattr(valor, "item"):  # escalar do numpy (int64, float64, bool_) -> nativo
        return valor.item()
    return valor


class ModelosRespostaXlsxLoader(BaseLoader):
    """Um `Document` por linha da aba `chunks` — o texto pronto do modelo de
    resposta, mais o resto da linha como metadata."""

    def __init__(self, caminho: str):
        self._caminho = caminho

    def load(self) -> list[Document]:
        try:
            df = pd.read_excel(self._caminho, sheet_name=_ABA)
        except ValueError as exc:
            # `read_excel` levanta ValueError quando a aba não existe — mensagem
            # crua não diz QUAL arquivo nem O QUE esperava, e quem vai ler isso é
            # quem colocou um `.xlsx` de outra forma na pasta `email_modelos/`.
            raise ValueError(
                f"{self._caminho}: esperava a aba {_ABA!r} (formato de "
                "modelos_resposta_chunks.xlsx, gerado por ler_dados_pst.ipynb). "
                f"Abas encontradas: {pd.ExcelFile(self._caminho).sheet_names}"
            ) from exc

        nome_arquivo = Path(self._caminho).name
        documentos = []
        for _, linha in df.iterrows():
            metadata = {
                (_COLUNA_ASSUNTO_EMAIL if coluna == "assunto" else coluna): _json_seguro(valor)
                for coluna, valor in linha.items()
                if coluna != "texto"
            }
            documentos.append(
                Document(page_content=str(linha["texto"]), metadata=metadata)
            )

        if not documentos:
            # Aba existe mas está vazia — não é erro (o notebook pode gerar um
            # arquivo sem template algum se não achou padrão repetido nenhum),
            # mas silêncio total também esconderia um export vazio por engano.
            logger.warning(
                "%s: aba %r não tem nenhuma linha — nenhum modelo de e-mail será indexado.",
                nome_arquivo,
                _ABA,
            )

        return documentos
