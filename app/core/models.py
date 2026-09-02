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

    # Override do modelo, no formato `[provider:]modelo` (ver
    # `app/providers/chain.cadeia_para_modelo`). `None` = a cadeia normal, com
    # fallback — que é o caminho de toda pergunta de aluno.
    #
    # Existe para avaliar um modelo contra a base real sem editar o `.env` nem
    # reiniciar o processo: é a diferença entre comparar dois modelos em cinco
    # minutos e em dois deploys. Por padrão a resposta NÃO passa pelo cache
    # (nem lê nem grava) — misturar respostas de modelos diferentes na mesma
    # chave serviria a resposta de um modelo experimental para o aluno
    # seguinte, que é a classe de bug que a T2.4 existiu para fechar.
    # `settings.modelo_override_cache_enabled` liga o cache também para
    # overrides; o modelo entra na chave nesse caso, então cada um tem sua
    # própria entrada — ver `responder._cache_key`.
    modelo: str | None = None


@dataclass
class RetrievedChunk:
    document: Document
    score: float  # relevância 0..1 (quanto maior, mais relevante)

    # Score do 1º estágio (bi-encoder E5) quando o reranker cross-encoder rodou
    # sobre este chunk — `score` acima passa a ser o do cross-encoder, noutra
    # escala. `None` quando não houve rerank (`RERANKER_ENABLED=false`), que é
    # informação, não falta de dado. Ver `app/retrieval/reranker.py`.
    score_bruto: float | None = None

    @property
    def citation(self) -> str:
        meta = self.document.metadata
        origem = meta.get("source_name", "desconhecido")
        pagina = meta.get("page")
        return f"{origem}, p. {pagina + 1}" if pagina is not None else origem

    @property
    def is_web(self) -> bool:
        """Chunk indexado a partir de uma página crawlada da allowlist (KB-3),
        não de um PDF interno.

        O `ANSWER_PROMPT` da base trata todo CONTEXTO como material interno
        revisado; este flag é o que devolve a ressalva de "página pública, não
        revisada" que antes só o `SYSTEM_WEB` dava (KB-6). Ver `source_type` em
        `ingestion/pipeline._enrich` e `scripts/crawl._documento`.
        """
        return self.document.metadata.get("source_type") == "web"


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

    # Esta resposta saiu da `resposta_cache` (nenhum token de API foi gasto).
    # Já existia na telemetria (`Registro.cache_hit`), que é um destino de
    # observabilidade e não é lido por quem chama; aqui é propriedade da própria
    # resposta, e a borda HTTP a expõe (T3.1) — na demo é o que torna o cache
    # visível: a mesma pergunta repetida volta em milissegundos e o front diz
    # por quê, em vez de parecer que o agente ficou magicamente rápido.
    cached: bool = False
