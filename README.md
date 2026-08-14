# Agente de IA — Suporte Acadêmico EAD (v1)

Agente de RAG que responde dúvidas de alunos e funcionários sobre o Canvas e
sobre procedimentos acadêmicos, usando como base de conhecimento os PDFs e
textos colocados em `data/raw/<assunto>/`.

Escopo da v1: apenas arquivos locais, sem dados sigilosos do aluno.
Ver [arquitetura-agente-ia-suporte-ead-v0.md](arquitetura-agente-ia-suporte-ead-v0.md).

## Como rodar

```bash
# 1. dependências
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt

# 2. configuração
copy .env.example .env          # preencha GOOGLE_API_KEY

# 3. banco (Postgres + pgvector)
docker compose up -d

# 4. coloque os PDFs/textos em data/raw/canvas/ e data/raw/puc-digital/

# 5. ingestão
python -m scripts.ingest canvas puc-digital -v

# 6. pergunte
python -m scripts.ask "Como envio uma atividade no Canvas?"
python -m scripts.ask "Como acesso o portal?" --assunto puc-digital --debug
```

API HTTP (opcional): `uvicorn app.main:app --reload` → `POST /ask`.

Testes: `pytest` (não precisa de banco nem de chave de API).

## Fluxo de dados

```
data/raw/<assunto>/*.pdf|txt|md
   │  loaders/registry.py      (fonte -> Document)
   ▼
pipeline.py ── chunker.py ── embeddings Gemini ── pgvector
                                                     │
pergunta ── preprocess.py ── retriever.py ───────────┘
                                 │
                                 ▼
                       responder.py + prompts.py ── Gemini ── resposta + fontes
```

## Organização

| Caminho | Responsabilidade |
|---|---|
| `app/core/` | configuração e contratos (`Query`, `Answer`, `RetrievedChunk`) |
| `app/providers/gemini.py` | **único** ponto que conhece o provedor de IA |
| `app/ingestion/loaders/` | fonte de dados → `Document` |
| `app/ingestion/chunker.py` | divisão em chunks (função pura) |
| `app/ingestion/pipeline.py` | orquestra load → chunk → embed → indexa |
| `app/retrieval/retriever.py` | busca por similaridade + filtro por assunto |
| `app/agent/` | pré-processamento, prompt e geração da resposta |
| `app/db/vector_store.py` | conexão com pgvector |
| `scripts/` | CLIs de ingestão e de pergunta |

## Pontos de extensão (features fora da v1)

Cada feature futura tem um lugar já definido — nenhuma exige reescrever a base:

| Feature | Onde entra | O que muda |
|---|---|---|
| **Web scraping** do site da instituição | `app/ingestion/loaders/registry.py` | registrar um `WebBaseLoader`; o resto do pipeline é o mesmo |
| **APIs públicas** (catálogo, calendário) | mesmo registry, ou fonte de contexto extra em `responder.py` | novo loader, ou tool calling tendo `retrieve` como tool |
| **Interpretação de print/imagem** | `app/agent/preprocess.py` | anexo → descrição textual via Gemini multimodal → mesma `Query` |
| **Reranking / busca híbrida** | `app/retrieval/retriever.py` | entre a busca e o corte por limiar |
| **Canal de atendimento** (portal, WhatsApp) | `app/main.py` | mais rotas; a lógica já está em `responder.py` |
| **Classificação de intenção / escalonamento** | antes do `retrieve` em `responder.py` | roteia entre responder e encaminhar para humano |

## Decisões e trade-offs

- **Metadata desde o dia 1** (`assunto`, `source_type`, `source_uri`, `page`):
  o índice já convive com conteúdo de scraping/API depois, sem migração.
- **Ingestão idempotente**: id determinístico por chunk + remoção dos chunks
  antigos do arquivo antes de reindexar. Reingerir não duplica nem deixa órfãos.
- **Dimensão do embedding não fixada**: a coluna é criada como `vector` sem
  dimensão, então trocar de modelo não exige migração. Em troca, o índice HNSW
  só entra depois — o que só importa acima de ~50k chunks.
- **Guardrail de relevância** (`RELEVANCE_THRESHOLD`): abaixo do limiar o agente
  responde que não encontrou, em vez de alucinar um procedimento acadêmico.
  Também serve de termômetro de quais documentos faltam na base.
- **Passos explícitos no `responder.py`** em vez de uma chain fechada: dá para
  inspecionar o contexto recuperado (`--debug`) antes da chamada ao LLM.

## Próximos passos sugeridos

1. Ingerir um conjunto pequeno de PDFs reais e calibrar `CHUNK_SIZE` e
   `RELEVANCE_THRESHOLD` com `scripts/ask.py --debug`.
2. Montar um conjunto de ~20 perguntas reais de alunos como teste de regressão
   do retrieval.
3. Fixar a dimensão do embedding e criar índice HNSW quando o corpus crescer.
