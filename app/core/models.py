"""Contratos do runtime do agente.

`Document` (do LangChain) é o contrato da ingestão; aqui ficam só os contratos do
lado da pergunta/resposta.

PONTO DE EXTENSÃO — `Query.attachments` existe hoje sempre vazio. Quando entrar
interpretação de print de tela, o pré-processamento (`app/agent/preprocess.py`)
converte os anexos em texto e preenche `Query.text`; retriever e responder não
mudam.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from langchain_core.documents import Document

# De onde veio a resposta. Definido AQUI e importado por quem serializa
# (`app/api/schemas.py`) de propósito: quando era uma `Literal` reescrita à mão
# no schema HTTP, adicionar "encaminhado" no agente passou despercebido e a API
# quebrou em runtime com literal_error, num caminho que os testes do agente
# cobriam mas os da API não. Com uma definição só, o contrato não tem como
# divergir do que o agente devolve.
Origem = Literal["base", "web", "encaminhado", "nenhuma"]


@dataclass
class Query:
    """Pergunta já normalizada para texto."""

    text: str
    assunto: str | None = None  # filtra o retrieval por pasta/assunto
    attachments: list[Path] = field(default_factory=list)


@dataclass
class RetrievedChunk:
    document: Document
    score: float  # relevância 0..1 (quanto maior, mais relevante)

    @property
    def citation(self) -> str:
        meta = self.document.metadata
        origem = meta.get("source_name", "desconhecido")
        pagina = meta.get("page")
        return f"{origem}, p. {pagina + 1}" if pagina is not None else origem


@dataclass
class Answer:
    text: str
    sources: list[RetrievedChunk] = field(default_factory=list)
    grounded: bool = True  # False quando nada relevante foi encontrado NA BASE
    # De onde veio a resposta: "base" (RAG), "web" (fallback de busca externa,
    # ver app/agent/web_fallback.py), "encaminhado" (assunto de outro
    # departamento, barrado na triagem antes do retrieval — ver
    # app/agent/triagem.py) ou "nenhuma" (nada encontrado em nenhuma fonte).
    # Campo separado de `grounded` de propósito: uma resposta vinda da web tem
    # fonte citável, mas continua sendo um caso de "não estava na base" — que é
    # o sinal usado para descobrir que documento falta indexar.
    #
    # "encaminhado" e "nenhuma" são separados pelo mesmo motivo: os dois mandam
    # o aluno para outro contato, mas só "nenhuma" significa documento faltando
    # na base. Misturar os dois estragaria essa métrica, porque assunto de outro
    # departamento nunca vai ser indexado aqui.
    origem: Origem = "base"
