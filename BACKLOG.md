# Backlog — API multi-plataforma e demo

Prioridades de trabalho para expor o agente via API integrável (AVA da
instituição e outros canais) e ter um frontend simples de demonstração.

Contexto e estado atual: [README.md](README.md) e
[arquitetura-agente-ia-suporte-ead-v0.md](arquitetura-agente-ia-suporte-ead-v0.md).

**Premissa da priorização:** o núcleo do agente (`answer` → `retrieve` →
prompt → LLM) já está desacoplado de HTTP e não precisa mudar. Praticamente
todo o esforço abaixo está na borda (`app/api/`) e em provar o pipeline
contra um banco real.

Legenda: ⚠️ = decisão em aberto, ver [Decisões pendentes](#decisões-pendentes).

---

## Sprint 0 — Fundação: provar o pipeline (bloqueia tudo)

Nada abaixo faz sentido endurecer antes disso. Hoje `delete_by_source` e a
criação da `resposta_cache` nunca tocaram um banco real.

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| **T0.1** | Subir Postgres+pgvector e configurar `GOOGLE_API_KEY` | `.env`, `docker-compose.yml` | `docker compose up -d` healthy; `psql` conecta na 5433 | 30min |
| **T0.2** | Rodar `scripts/ingest.py` num assunto real e conferir os chunks | — | `list_ingested.py` mostra os arquivos; metadata completa (`assunto`, `page`, `content_hash`) | 30min |
| **T0.3** | Provar idempotência: reingerir o mesmo arquivo e um arquivo encolhido | `app/db/vector_store.py` | Contagem de chunks não duplica; `delete_by_source` remove órfãos | 30min |
| **T0.4** | Rodar `scripts/ask.py --debug` nos 3 caminhos: base, web, secretaria | — | Os 3 ramos de `origem` observados na prática | 1h |
| **T0.5** | Provar o cache: mesma pergunta 2x, ver a `resposta_cache` sendo criada e o hit | `app/db/response_cache.py` | Log `cache hit`; tabela criada sob demanda | 30min |
| **T0.6** | **Calibrar `RELEVANCE_THRESHOLD` com dados reais** | `.env` | 10-15 perguntas reais anotadas: quantas caem certo na base vs. vazam pra web | 2h |

> **T0.6 é o mais subestimado da lista.** O `0.35` é um chute até existir
> corpus indexado — e ele decide sozinho quanto do tráfego vai para o
> fallback web, que é o caminho lento e caro.

---

## Sprint 1 — Borda HTTP: estrutura e contrato ✅ concluída (2026-08-21)

Refactor só na camada HTTP. `app/agent/`, `app/retrieval/` e `app/db/` não
são tocados (exceção declarada no próprio T1.7: `vector_store.py` ganhou
`list_assuntos`).

Validado de ponta a ponta contra Postgres e Gemini reais (`TestClient` com
`lifespan`, não só testes com dublê): boot com warm-up 26,4s, `/v1/health`
10ms, assunto inválido barrado em 12ms sem chamar retrieval/LLM, pergunta real
completando com `request_id` batendo no header e no corpo. Suíte inteira:
94/94.

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| ✅ **T1.1** | Criar `app/api/` com `create_app()`, `routers/v1.py`, `schemas.py`, `deps.py` | novos + `app/main.py` | `POST /v1/ask` e `GET /v1/health` respondendo; `main.py` vira 3 linhas | 2h |
| ✅ **T1.2** | `lifespan` com warm-up: carrega embeddings e testa conexão no startup | `app/api/app.py` | Primeira request pós-boot < 3s (hoje pode passar de 60s com download do modelo) | 1h |
| ✅ **T1.3** | **`SourceOut` rico**: `titulo`, `tipo` (`documento`/`web`), `url`, `pagina`, `score` | `app/api/schemas.py` | Resposta com `origem="web"` traz a URL clicável — hoje ela é jogada fora | 2h |
| ✅ **T1.4** | `origem` como `Literal["base","web","nenhuma"]` + `request_id` na resposta | `app/api/schemas.py` | OpenAPI documenta os 3 valores; cliente gerado por schema sabe o que tratar | 30min |
| ✅ **T1.5** | Envelope de erro padronizado + exception handlers globais | `app/api/errors.py` | Pergunta vazia→422, banco fora→503, sem `GOOGLE_API_KEY`→503, anexo→501. Nenhum 500 com stack | 2h |
| ✅ **T1.6** | Validação de entrada: `min_length=3`, `max_length=1000` na pergunta | `app/api/schemas.py` | Payload de 2 MB rejeitado antes de virar prompt | 30min |
| ✅ **T1.7** | `GET /v1/assuntos` + validação de `assunto` contra a lista real | `app/api/routers/v1.py`, `app/db/vector_store.py` | `assunto="Canvas"` (maiúscula) → 422 explícito, não zero-chunks silencioso | 2h |
| ✅ **T1.8** | Testes de API com `TestClient` (sem banco, com `answer` dublado) | `tests/test_api.py` | Cobre os 3 valores de `origem`, cada código de erro e a validação | 3h |

### Por que cada item

- **T1.3** — `fontes: list[str]` achata o `RetrievedChunk` numa string
  (`"manual.pdf, p. 3"`). Quando `origem == "web"` a resposta veio de uma
  página pública e **a URL não chega ao consumidor**, mesmo existindo
  `source_uri` na metadata. É o que dá confiança na resposta dentro do AVA.
- **T1.5** — hoje `NotImplementedError` de anexos, Postgres fora do ar e
  `GOOGLE_API_KEY` ausente viram todos **500**. Uma integração externa não
  consegue distinguir "sua pergunta é inválida" de "o serviço caiu" — e a
  diferença define se ela deve fazer retry.
- **T1.7 é maior do que parece** — hoje um `assunto` inválido não dá erro:
  passa o filtro `$eq`, retorna zero chunks e a pergunta cai no fallback
  web. Falha invisível que parece funcionamento normal.
- **Achado ao implementar T1.7**: o Postgres tem 2 coleções (`base_conhecimento`,
  da época do embedding via Gemini, e a ativa `base_conhecimento_hf`) — e nada
  em `vector_store.py` filtrava por coleção. Hoje a antiga está vazia (0
  linhas), então nunca vazou nada, mas `list_assuntos`/`list_ingested_sources`/
  `delete_by_source`/`delete_by_assunto` misturariam as duas se ela voltasse a
  ter dados. Corrigido com uma subquery por `collection_id` reaproveitada nas
  4 funções; provado contra o banco real inserindo linha de teste nas duas
  coleções e confirmando que só a ativa é lida/apagada.

---

## Sprint 2 — Segurança e correção

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| **T2.1** | Auth por API key (`X-API-Key`), chaves por integração no `.env` | `app/api/deps.py`, `app/core/config.py` | Sem chave→401; chave inválida→401; `/health` continua público | 2h |
| **T2.2** | CORS com origens por env (nunca `*` junto com auth por header) | `app/api/app.py`, `.env.example` | Front em outra origem chama a API; origem não listada é bloqueada | 1h |
| **T2.3** | Rate limit por API key + teto global diário | `app/api/deps.py` | Estourou→429 com `Retry-After`. Protege cota do Gemini e o buscador do 429 | 3h |
| **T2.4** | ⚠️ **Corrigir `_cache_key`** — hash da pergunta normalizada na chave | `app/agent/responder.py`, `tests/test_responder.py` | Duas perguntas distintas com o mesmo top-5 recebem respostas distintas | 1h |
| **T2.5** | Timeout na chamada ao Gemini | `app/providers/gemini.py` | Request pendurada não segura thread do pool indefinidamente | 30min |
| **T2.6** | `/v1/health` (liveness) + `/v1/ready` (checa Postgres + chave) | `app/api/routers/v1.py` | Com o banco derrubado, `/ready` responde 503 — hoje `/health` mente "ok" | 1h |

### Por que cada item

- **T2.1** — cada `/ask` que não bate cache é uma chamada paga ao Gemini +
  até 5 buscas web. O endpoint aberto é um proxy grátis para a cota da
  instituição. Sem identificar o consumidor também não há como aplicar
  quota nem atribuir custo por canal, e rate limit só por IP é inútil numa
  rede institucional atrás de NAT.
- **T2.4** — `_cache_key` é `assunto + alta_confiança + ids dos chunks`; o
  **texto da pergunta não entra na chave**. Duas perguntas *diferentes* que
  recuperam o mesmo top-5 recebem a mesma resposta — ex.: "como envio uma
  tarefa no Canvas?" e "onde vejo a nota da tarefa no Canvas?"
  plausivelmente recuperam os mesmos 5 chunks com `top_k=5` e limiar 0.35.
  A invalidação por id continua valendo; o que quebra é a premissa "mesmo
  conjunto de chunks ⇒ mesma resposta", verdadeira só para paráfrases.

---

## Sprint 3 — Demo e sinal de produto

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| **T3.1** | Frontend estático (1 arquivo HTML+JS, sem build) servido em `/demo` | `app/static/index.html` | Pergunta → resposta + **badge de origem** + fontes com link + latência | 4h |
| **T3.2** | Log estruturado de interação (`pergunta`, `assunto`, `origem`, `grounded`, latência, chunks, consumidor) | `app/db/interacoes.py` | Toda request registrada; `request_id` correlaciona log e resposta | 3h |
| **T3.3** | Relatório de lacunas: perguntas com `grounded=False` agrupadas | `scripts/lacunas.py` | Lista priorizada do que falta indexar — vira o roadmap de ingestão | 2h |
| **T3.4** | Política LGPD: retenção, hash do identificador, alerta de RA/CPF na pergunta | `app/db/interacoes.py`, README | Nada de identificável em claro na tabela | 2h |

### Por que cada item

- **T3.2/T3.3 têm o maior valor de produto da lista inteira.**
  `grounded=False` é literalmente a lista de documentos que faltam na base,
  e hoje ela é descartada a cada request. Custa quase nada (mesma infra
  Postgres) e vira o roadmap de ingestão.
- **T3.4 anda junto com T3.2** — pergunta de aluno contém RA e CPF com
  frequência; não dá para começar a persistir sem política de retenção.
- **T3.1** — o badge de origem não é enfeite: é o que demonstra o guardrail
  funcionando e separa este agente de um chatbot genérico. Servir da mesma
  origem via `StaticFiles` evita CORS na demo (T2.2 continua necessário
  para o AVA de verdade).

#### Escopo do frontend de demonstração

Um arquivo HTML estático com `fetch`, sem framework e sem build (~150 linhas):

- campo de pergunta + `<select>` de assunto populado por `GET /v1/assuntos`;
- a resposta;
- **badge de origem** com cor: `base` (verde, material interno) / `web`
  (amarelo, página pública) / `nenhuma` (cinza, encaminhado à secretaria);
- lista de fontes, com link quando houver URL;
- latência e indicador de cache hit;
- `<details>` com o JSON cru — ajuda em apresentação para banca/gestor.

---

## Backlog posterior (não priorizar agora)

| # | Tarefa | Quando isso vira prioridade |
|---|---|---|
| T4.1 | `conversation_id` + reescrita de pergunta com histórico | Assim que a demo mostrar follow-up quebrando ("e no celular?" → 13 chars pro pgvector) |
| T4.2 | Streaming SSE (`POST /v1/ask/stream`) | Quando a UX de chat no AVA for definida |
| T4.3 | Dockerfile da app + decisão workers vs. RAM do modelo local | Antes do primeiro deploy fora da máquina de desenvolvimento |
| T4.4 | Reranking / busca híbrida | Se T0.6 mostrar recall ruim — ponto de extensão já marcado em `app/retrieval/retriever.py` |
| T4.5 | Índice HNSW | Acima de ~50k chunks (ver `app/db/vector_store.py`) |
| T4.6 | Interpretação de print de tela | Já mapeado em `app/agent/preprocess.py` |
| T4.7 | OAuth/JWT com identidade de aluno | Só quando o agente for consultar dados pessoais — hoje o escopo exclui isso |

---

## Resumo de esforço

| Sprint | Esforço | Destrava |
|---|---|---|
| S0 — Fundação | ~5h | Tudo |
| S1 — Borda HTTP | ~13h | Integração multi-plataforma |
| S2 — Segurança | ~8h | Exposição fora da máquina de desenvolvimento |
| S3 — Demo + sinal | ~11h | Demonstração e roadmap de conteúdo |

**Caminho mais curto até uma demo defensável:**
T0 completo → T1.1, T1.2, T1.3, T1.5 → T2.2 → T3.1.
Cerca de 12h, e já dá para mostrar funcionando com fontes clicáveis.

---

## Decisões pendentes

1. **T2.4 — como corrigir a `_cache_key`?**
   - (a) somar hash da pergunta normalizada à chave — perde hit rate,
     elimina o erro, comportamento previsível;
   - (b) cachear só no ramo `alta_confianca` — preserva mais hits e também
     elimina o erro.

   Sem decisão em contrário, seguir com **(a)**.

2. **Ordem entre S2 e S3.** Como está, S2 vem antes. Se a prioridade for
   demonstrar rápido para alguém, inverter — mas **T2.2 (CORS) sobe junto
   com S3 de qualquer forma**, porque sem ele o front não chama a API.

---

## Diagnóstico que originou este backlog

Avaliação da API atual ([app/main.py](app/main.py), 49 linhas) para uso
multi-plataforma. Cada item virou uma tarefa acima.

| Achado | Tarefa |
|---|---|
| Contrato de resposta perde a URL da fonte web e o score | T1.3 |
| `origem: str` sem `Literal` — OpenAPI não documenta os valores | T1.4 |
| Erro tem formato diferente do sucesso; falhas de infra viram 500 | T1.5 |
| Sem autenticação — endpoint aberto é proxy grátis para o Gemini | T2.1 |
| Sem versionamento de rota — a primeira melhoria de contrato quebra quem integrou | T1.1 |
| CORS ausente — o front de demo não consegue chamar a API | T2.2 |
| Sem limite de tamanho na pergunta; `assunto` é texto livre | T1.6, T1.7 |
| `/health` retorna "ok" incondicionalmente | T2.6 |
| Embeddings carregam lazy no primeiro request (modelo de ~1 GB) | T1.2 |
| `llm.invoke()` sem timeout | T2.5 |
| Rate limiting ausente | T2.3 |
| Interações não são registradas — sinal de lacuna é descartado | T3.2, T3.3 |
| Cache pode servir a resposta de outra pergunta | T2.4 |
| Sem memória — follow-up quebra o retrieval | T4.1 |

**A estrutura atual do FastAPI comporta isso?** O núcleo sim, a borda não —
e isso é uma boa notícia, porque o custo fica baixo.

Já está certo e não muda: `answer()` é função pura de orquestração sem nada
de HTTP; `Query`/`Answer` são contratos próprios; `settings` centralizado;
provedor de IA isolado num arquivo; `retrieve` e `buscar_na_web` já têm
assinatura de tool. Dá para plugar auth, CORS, rate limit e schema novo sem
tocar em uma linha do agente.

O que não comporta:

| Hoje | Problema |
|---|---|
| `app = FastAPI(...)` no import do módulo | Sem `lifespan`, não há onde fazer warm-up nem checagem de dependências; sem `create_app()` fica difícil testar com config alternativa |
| Schemas Pydantic inline no `main.py` | Não dá para versionar dois formatos de resposta em paralelo durante uma migração |
| Rota registrada direto no `app` | Sem `APIRouter`, prefixar `/v1` depois exige mexer em cada rota |
| Nenhum middleware, nenhuma dependency | Auth e rate limit não têm onde entrar sem virar `if` dentro do handler |
| `try/except ValueError` local | Tratamento de erro não é reaproveitável entre rotas |
