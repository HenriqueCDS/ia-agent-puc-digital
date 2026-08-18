# Arquitetura — Agente de IA para Suporte ao Aluno EAD (v0 — modelo inicial simplificado)

## Visão geral

Modelo inicial simplificado: RAG (Retrieval-Augmented Generation) consumindo
apenas arquivos PDF e texto como base de conhecimento, sem web scraping,
interpretação de print ou escalonamento. Este documento é o diagrama e a lista
de componentes; o estado de implementação, contagem de testes e instruções de
uso ficam no [README.md](README.md) — mantenha os dois em sincronia ao mexer
na estrutura.

```mermaid
flowchart TD
    subgraph Fontes["Fontes de Dados"]
        A1[Arquivos PDF]
        A2[Arquivos de texto/.md/.txt]
    end

    subgraph Ingestao["Pipeline de Ingestão"]
        B1[Extração de conteúdo - PDF → texto]
        B2[Chunking + Metadata]
        B3[Embeddings locais - HuggingFace]
    end

    subgraph Armazenamento["Armazenamento (Postgres)"]
        C1[(Vector Store - pgvector)]
        C2[(Cache de resposta - resposta_cache)]
    end

    subgraph Agente["Camada do Agente"]
        D1[Recepção da pergunta - texto]
        D2[Retriever - busca no Vector Store]
        D4{Cache hit? - assunto+confiança+chunks}
        D3[LLM Gemini - resposta com contexto recuperado]
    end

    A1 --> B1 --> B2 --> B3 --> C1
    A2 --> B2

    D1 --> D2 --> C1
    D2 --> D4
    D4 -- não --> D3 --> C2
    D4 -- sim --> C2
```

## Componentes

### 1. Ingestão
- Extrair texto de PDFs (`pypdf`)
- Ler arquivos `.txt`/`.md` diretamente
- Dividir o conteúdo em chunks (por parágrafo ou tamanho fixo com overlap)
- Gerar embeddings dos chunks com um modelo local (HuggingFace/
  sentence-transformers, multilíngue — sem depender de cota de API) e indexar
  no vector store

### 2. Vector Store
- Armazena chunks + embeddings + metadata (origem do arquivo, página)
- Opção mais simples para começar local: pgvector

### 3. Agente (runtime)
- Recebe a pergunta do usuário (texto)
- Busca os chunks mais relevantes no vector store (retrieval)
- Se nenhum chunk recuperado passa do limiar de relevância, responde que não
  encontrou na base em vez de chamar o LLM
- Antes de chamar o LLM, verifica o cache de resposta pela chave
  `assunto + confiança + ids dos chunks recuperados` (não pelo texto da
  pergunta — ver componente 4); em hit, devolve a resposta cacheada com as
  fontes do retrieval atual
- Em miss, envia pergunta + contexto recuperado para o LLM (Gemini) gerar a
  resposta, e grava o resultado no cache

### 4. Cache de resposta
- Evita chamar o LLM de novo quando perguntas diferentes (inclusive
  paráfrases) recuperam o mesmo conjunto de chunks no retrieval
- Chave = `assunto + nível de confiança (is_exact_match) + ids ordenados dos
  chunks recuperados` — não o texto da pergunta, para não depender de
  similaridade textual/embedding e não arriscar falso-positivo entre
  perguntas parecidas mas com resposta diferente
- Guardado numa tabela própria (`resposta_cache`) no mesmo Postgres da
  ingestão — nenhum serviço novo. Reingerir um arquivo alterado muda os ids
  dos chunks recuperados e invalida a chave automaticamente

## Fora do escopo (por enquanto)
- Web scraping do site da PUC
- Interpretação de print/imagem
- Classificador de intenção / escalonamento para humano
- Canal de atendimento (WhatsApp, portal, etc.)

## Stack mínima sugerida
- Python + FastAPI (endpoint simples de pergunta/resposta)
- `pypdf` para extrair texto do PDF
- pgvector como vector store e como armazenamento do cache de resposta (roda
  local, sem infra extra)
- Embeddings locais (HuggingFace/sentence-transformers) e API do Gemini para
  geração da resposta

## Estrutura atual
agente-suporte-ead/
├── app/
│   ├── main.py                    # FastAPI - entrypoint, POST /ask e GET /health
│   ├── core/
│   │   ├── config.py               # configs via .env (chunk size, thresholds, CACHE_ENABLED etc.)
│   │   └── models.py                # contratos: Query, RetrievedChunk, Answer
│   ├── providers/
│   │   └── gemini.py                # único ponto que conhece o provedor de IA
│   │                                 #   (embeddings locais HuggingFace + chat Gemini)
│   ├── ingestion/
│   │   ├── loaders/
│   │   │   └── registry.py          # fonte (pdf/txt/md) -> Document, por extensão
│   │   ├── chunker.py                # divide em chunks + content_hash/chunk_id (função pura)
│   │   └── pipeline.py               # orquestra load -> chunk -> embed -> indexa (idempotente)
│   ├── retrieval/
│   │   └── retriever.py              # busca por similaridade + filtro por assunto + is_exact_match
│   ├── agent/
│   │   ├── preprocess.py             # normaliza a Query (ponto de entrada p/ anexos no futuro)
│   │   ├── prompts.py                 # templates de prompt (base e alta confiança)
│   │   └── responder.py               # orquestra retrieval -> cache -> prompt -> LLM
│   └── db/
│       ├── vector_store.py            # conexão com pgvector + operações de schema da ingestão
│       └── response_cache.py          # cache de resposta por conjunto de chunks (mesmo Postgres)
│
├── data/
│   └── raw/
│       ├── canvas/                    # PDFs/texto sobre o Canvas
│       └── puc-digital/               # PDFs/texto sobre PUC Digital
│
├── scripts/
│   ├── ingest.py                      # CLI: indexa um ou mais assuntos
│   ├── ask.py                         # CLI: pergunta, com --debug para chunks/scores
│   └── list_ingested.py               # CLI: lista arquivos já indexados
│
├── tests/
│   ├── test_chunker.py                # chunking, content_hash, chunk_id determinístico
│   ├── test_retrieval.py              # corte por limiar, filtro por assunto, is_exact_match
│   └── test_responder.py              # guardrail, prompt de alta confiança, hit/miss de cache
│
├── docker-compose.yml                 # Postgres + pgvector
├── requirements.txt
├── .env.example                        # DATABASE_URL, GOOGLE_API_KEY, CACHE_ENABLED etc.
└── README.md