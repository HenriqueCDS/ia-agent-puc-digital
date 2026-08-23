# Agente de IA — Suporte Acadêmico EAD (v1)

Agente de RAG que responde dúvidas de alunos e funcionários sobre o Canvas e
sobre procedimentos acadêmicos, usando como base de conhecimento os PDFs e
textos colocados em `data/raw/<assunto>/`.

Escopo da v1: apenas arquivos locais, sem dados sigilosos do aluno.
Ver [arquitetura-agente-ia-suporte-ead-v0.md](arquitetura-agente-ia-suporte-ead-v0.md).

## Estado atual do projeto

Esqueleto da v1 implementado e executável localmente. O que existe hoje:

**Ingestão** — leitura de `.pdf` (via `pypdf`), `.txt` e `.md` a partir de
`data/raw/<assunto>/`, divisão em chunks com overlap
(`RecursiveCharacterTextSplitter`), geração de embeddings com um modelo local
(HuggingFace/sentence-transformers, multilíngue — sem depender de cota de API)
e indexação no pgvector. A metadata de origem (`assunto`, `source_type`,
`source_uri`, `source_path`, `source_name`, `page`, `chunk_index`) é gravada em
todo chunk. A ingestão é idempotente: cada chunk tem id determinístico e os
chunks antigos do arquivo são removidos antes da reindexação, então rodar o
ingest de novo não duplica nem deixa conteúdo órfão.

**Retrieval** — busca por similaridade de cosseno no pgvector, com filtro
opcional por assunto e corte por limiar de relevância (`RELEVANCE_THRESHOLD`).

**Agente** — monta o prompt com os trechos recuperados e suas citações, chama o
Gemini e devolve a resposta com as fontes (`arquivo, página`). Quando nada passa
do limiar, não chama o LLM com contexto vazio: cai no fallback de busca externa
abaixo.

**Fallback de busca externa** — quando o retrieval não devolve nada, o agente
procura a resposta em páginas públicas oficiais antes de encaminhar para a
secretaria. A busca é restrita por uma allowlist (`WEB_ALLOWLIST` em
`app/core/config.py`): site da PUC-Campinas e a base de conhecimento oficial do
Canvas (`community.instructure.com/en/kb/`). A restrição tem duas camadas — uma
query `site:<host>` por fonte, e a revalidação de **toda** URL devolvida contra
`(host, path_prefix)`, porque o operador `site:` do buscador vaza resultado fora
do escopo em silêncio. Os snippets ainda passam por um corte de similaridade
(mesmo modelo de embedding local da base, sem custo de API) e por um veto final
do LLM, que responde com o marcador `#SEM_COBERTURA#` quando os trechos não
bastam. Nada relevante encontrado → resposta com o contato da secretaria.
Liga/desliga com `WEB_FALLBACK_ENABLED`; ver `app/agent/web_fallback.py`.

**Triagem por assunto** — antes de qualquer coisa, a pergunta que é de outro
departamento (cobrança, diploma, rematrícula…) é encaminhada com o contato certo,
sem tocar no retrieval nem no LLM. É guardrail, não economia: sem ela, uma
pergunta sobre boleto pode recuperar um chunk fraco e o LLM responderia sobre
dinheiro a partir de um documento que não é sobre isso. `matrícula` tem exceções
explícitas (Canvas, disciplina, plataforma) para não perder o que a base
responde bem. Liga/desliga com `TRIAGEM_ENABLED`; as categorias e os e-mails
estão em `ENCAMINHAMENTOS` (`app/core/config.py`); ver `app/agent/triagem.py`.

**Cache de resposta** — perguntas que recuperam o mesmo conjunto de chunks no
retrieval (mesmo sendo uma paráfrase uma da outra) reaproveitam a resposta já
gerada, sem chamar o Gemini de novo. A chave não é o texto da pergunta, é
`assunto + nível de confiança + ids dos chunks recuperados`; assim, reingerir
um arquivo alterado muda os ids e invalida o cache sozinho, sem lógica extra de
limpeza. Guardado numa tabela própria (`resposta_cache`) no mesmo Postgres da
ingestão — nenhuma infra nova. Liga/desliga com `CACHE_ENABLED`
(`app/core/config.py`); ver `app/agent/responder.py` (`_cache_key`) e
`app/db/response_cache.py`.

**Entrypoints** — `scripts/ingest.py` (indexa um ou mais assuntos) e
`scripts/ask.py` (pergunta, com `--debug` para inspecionar os chunks e scores).
`app/main.py` monta a API em `app/api/` (FastAPI): `POST /v1/ask`,
`GET /v1/assuntos`, `GET /v1/health` (liveness) e `GET /v1/ready` (readiness) —
caminho já pronto para plugar um front ou o AVA da instituição depois.

**Segurança da borda** — `/ask` e `/assuntos` exigem `X-API-Key`, com uma chave
por integração (`API_KEYS=nome:chave,...`): é o que permite atribuir o custo do
Gemini por canal e revogar um consumidor sem derrubar os outros. Cada `/ask`
que não bate cache é uma chamada paga mais até 5 buscas web, então o endpoint
aberto seria um proxy grátis para a cota da instituição. Sobre isso vêm rate
limit por consumidor (janela deslizante de 60s) e um teto diário do processo,
CORS restrito às origens do `.env`, e timeout na chamada ao Gemini. Ver
`app/api/deps.py`, `app/api/ratelimit.py` e `app/api/app.py`.

**Infra** — `docker-compose.yml` com `pgvector/pgvector:pg16`; configuração via
`.env` (`pydantic-settings`).

**Testes** — 174 testes cobrindo chunking, ids determinísticos, corte por
limiar, filtro por assunto, formatação de citação, o guardrail do agente, o
hit/miss do cache de resposta, o fallback de busca externa (allowlist, domínio
sósia, redirect do buscador, corte por similaridade, blocklist de assunto
sensível e degradação em caso de rate limit) e a borda HTTP inteira — contrato
de resposta, cada código de erro, autenticação, CORS, a janela deslizante do
rate limit e liveness vs. readiness. Rodam sem banco, sem chave de API e **sem
rede**, usando dublês de vector store, de LLM, de cache, de busca e de relógio.

### Ainda não executado de ponta a ponta

O caminho completo `ingest → embeddings locais → pgvector → retrieve` ainda não
foi rodado contra um banco real. Falta subir o Postgres e configurar a
`GOOGLE_API_KEY`; os pontos que só ficam provados aí são o `DELETE` de
`delete_by_source` e a criação sob demanda da tabela `resposta_cache` (ambos em
`app/db/`), únicos trechos acoplados ao schema/SQL direto por fora do
`langchain-postgres`.

### Fora do escopo desta fase

Ingestão por web scraping (o fallback busca em tempo real, não indexa),
interpretação de print/imagem, consumo de outras APIs públicas, classificação de
intenção/escalonamento e canais de atendimento. Nenhum deles está implementado —
cada um tem o lugar de encaixe definido na tabela de
[pontos de extensão](#pontos-de-extensão-features-fora-da-v1).

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

API HTTP (opcional): `uvicorn app.main:app --reload`. Warm-up (modelo de
embeddings + conexão com o Postgres) roda no boot, não na 1ª request — ver
`app/api/app.py`.

| Rota | Auth | Rate limit | Para quê |
|---|---|---|---|
| `POST /v1/ask` | `X-API-Key` | sim | A pergunta. É a rota que custa dinheiro |
| `GET /v1/assuntos` | `X-API-Key` | não | Popula o `<select>` de assunto; DISTINCT barato |
| `GET /v1/health` | público | não | Liveness: o processo responde. Não checa dependência |
| `GET /v1/ready` | público | não | Readiness: Postgres + chave do LLM. 503 se algo faltar |
| `GET /demo` | pública | — | Frontend de demonstração (ver abaixo) |

`/health` e `/ready` são coisas diferentes de propósito: reiniciar a app porque
o Postgres caiu não conserta o Postgres — só derruba junto o que ainda saía do
cache. Quem decide reiniciar lê `/health`; quem decide mandar tráfego lê `/ready`.

```bash
# cadastre ao menos uma chave no .env antes (API_KEYS=demo:...); gere com:
python -c "import secrets; print(secrets.token_urlsafe(32))"

curl -s localhost:8000/v1/ready
curl -s -X POST localhost:8000/v1/ask \
  -H "X-API-Key: $CHAVE" -H "Content-Type: application/json" \
  -d '{"pergunta": "Como envio uma atividade no Canvas?"}'
```

Em desenvolvimento local, `API_AUTH_ENABLED=false` desliga a autenticação. Com
ela ligada e `API_KEYS` vazia, as rotas protegidas respondem **503**, nunca
liberam o acesso: um erro de configuração que abre o endpoint não aparece em
teste nenhum — só na fatura.

Testes: `pytest` (não precisa de banco nem de chave de API).

### Demo web (`/demo`)

Suba a API e abra <http://localhost:8000/demo> (a raiz `/` redireciona para lá).
É um arquivo HTML estático (`app/static/index.html`), sem build, sem framework e
**sem nenhum recurso externo** — abre numa máquina sem internet e não manda a
pergunta do aluno para terceiro nenhum.

O que a demo mostra, e por que cada coisa está lá:

- **badge de origem** — o guardrail ficando visível. `base` (verde, material
  interno), `web` (amarelo, página pública oficial), `encaminhado` (azul, outro
  departamento) e `nenhuma` (cinza, o agente preferiu não responder). É o que
  separa este agente de um chatbot genérico;
- **fontes com link e score** — de onde a resposta saiu;
- **latência e cache hit** — o custo por trás de cada pergunta;
- **JSON cru** num `<details>` — o contrato da API sem abrir o Swagger.

A demo usa uma integração **própria** em `API_KEYS` (`DEMO_CONSUMIDOR`, default
`demo`), cuja chave o servidor injeta no HTML. Ela é pública para quem abrir o
DevTools — isso não tem solução dentro de um arquivo estático, então o que se
controla é o estrago: revogá-la não afeta o AVA, e ela tem teto diário próprio
(`RATE_LIMIT_DIARIO_POR_CONSUMIDOR=demo:200`), de modo que o pior caso de abuso
é a demo parar em vez de gastar o orçamento do dia inteiro. `DEMO_ENABLED=false`
tira a rota do ar sem mexer em nada da v1.

## Observabilidade

Cada pergunta respondida gera **um registro** (`app/core/telemetry.py`), com dois
destinos independentes:

1. **linha JSON em `stderr`** — sempre, separada da resposta (que sai em `stdout`);
2. **tabela `telemetria` no Postgres** — a mesma base do pgvector, com **retenção
   de 7 dias** (`app/db/telemetry_store.py`).

```bash
python -m scripts.ask "Como envio uma atividade?"        # registro vai para o banco
python -m scripts.ask "Como envio uma atividade?" 2>> telemetria.jsonl   # e/ou arquivo
```

```json
{"canal":"api:ava","request_id":"78bbd705-e16c-417b-a546-b8b08f6b5e13","pii":null,
 "assunto":"canvas","assunto_origem":"metadata","topico":"envio de atividade",
 "pergunta_hash":"8efa09547286","chat_model":"gemini-3.6-flash",
 "origem":"base","grounded":true,"n_chunks":2,"score_top":0.95,"alta_confianca":true,
 "cache_hit":false,"input_tokens":120,"output_tokens":30,
 "ms_retrieve":41.2,"ms_llm":880.5,"ms_web":null,"ms_total":925.0,
 "web_insuficiente":null,"erro":null}
```

`request_id` é o **mesmo** valor que sai no header `X-Request-Id` e no corpo da
resposta. É o que liga uma reclamação pontual ("recebi resposta errada, o id era
78bbd705…") à linha exata da tabela — sem ele, correlacionar dependia de casar
horário com hash de pergunta na mão. É `null` quando a pergunta veio da CLI.

### Relatório de lacunas

Cada resposta com `grounded=false` é um **documento que falta indexar**. O
relatório transforma a telemetria já gravada no roadmap de ingestão, priorizado
por quantas vezes a pergunta apareceu:

```bash
python -m scripts.lacunas                    # últimos 7 dias
python -m scripts.lacunas --dias 7 --json    # para pipeline
```

```
    n  dist  assunto        situação          tema
  ------------------------------------------------------------------------------
    2     1  canvas, puc-di sem resposta      Envio de atividade no Canvas
    1     1  canvas         coberta pela web  Acesso ao Canvas
```

A ordem é a de prioridade: primeiro o que ficou **sem resposta nenhuma** (o
aluno foi para a secretaria), depois o que a busca externa cobriu — lacuna mais
barata, porque a página que respondeu já diz qual documento indexar. Ficam de
fora os encaminhamentos por triagem (boleto, diploma: outro departamento, nunca
vão ser indexados aqui) e as linhas com `erro`, que são falha de infraestrutura
e não ausência de conteúdo.

A janela útil é a da retenção (7 dias por padrão); pedir mais que isso avisa em
vez de devolver uma janela vazia que pareceria "semana tranquila".

### `assunto` e `topico`

Dois campos com papéis diferentes, e **nenhum deles custa uma chamada extra**:

| Campo | Exemplo | De onde vem |
|---|---|---|
| `assunto` | `canvas` | derivado de graça, em 4 camadas (abaixo) |
| `topico` | `envio de atividade com prazo expirado` | última linha da própria resposta do modelo |

O `assunto` é derivado nesta ordem, e `assunto_origem` registra qual camada
acertou — sem isso, um valor derivado ficaria indistinguível de um informado:

| `assunto_origem` | Quando | Fonte |
|---|---|---|
| `informado` | usuário passou `--assunto` | tem precedência sobre tudo |
| `metadata` | o retrieval achou chunks | pasta gravada na ingestão (`pipeline._enrich`) |
| `allowlist` | respondeu pela web | `FonteWeb.assunto` do domínio (`WEB_ALLOWLIST`) |
| `triagem` | assunto de outro departamento | categoria casada em `ENCAMINHAMENTOS` |
| `blocklist` | nada encontrado, tema sensível | categoria casada, com `TRIAGEM_ENABLED=false` |
| `null` | nada acima | pergunta fora de escopo |

O `topico` vem do marcador `#TOPICO:` que os prompts pedem na **última linha** da
resposta (`app/agent/prompts.py`). Ele é removido antes de qualquer uso do texto
— o aluno nunca o vê, e ele não entra na citação de fontes. Custo: ~10 tokens de
saída, contra uma segunda chamada à API que dobraria o custo que esta telemetria
existe para medir.

Detalhes que valem saber:

- **O marcador vai no fim, nunca no início.** Ele é separado do texto antes do
  veto de contexto insuficiente (`prompts.eh_insuficiente`), que examina o resto
  da resposta.
- **O texto é cacheado *com* o marcador** e reprocessado na leitura: um cache hit
  recupera o `topico` sem coluna nova e sem reclassificar.
- **Marcador ausente** (o modelo esqueceu, ou é uma resposta cacheada de antes
  desta versão) → `topico: null` e resposta intacta.
- **`topico` é frase livre do modelo**, então pode conter fragmento da pergunta —
  ao contrário de `assunto`, que é categoria fechada. Vale ao decidir a retenção.

Responde às quatro perguntas que importam para eficiência:

| Pergunta | Campos |
|---|---|
| Quanto custa? | `input_tokens`, `output_tokens`, `cache_hit` (hit = zero token de API) |
| Onde está a lentidão? | `ms_retrieve` (CPU local), `ms_llm` (rede), `ms_web` (rede lenta) |
| O guardrail dispara quanto? | `origem` = `base` / `web` / `encaminhado` / `nenhuma`, `base_insuficiente`, `web_insuficiente` |
| A qualidade caiu? | `n_chunks`, `score_top`, `alta_confianca` ao longo do tempo |

Consultas (a coluna `dados` é `JSONB` — campo novo no registro não exige migração):

```sql
-- documentos que faltam indexar: perguntas repetidas que a base não respondeu
SELECT dados->>'pergunta_hash' AS pergunta, dados->>'assunto' AS assunto, count(*) AS vezes
FROM telemetria
WHERE dados->>'origem' <> 'base'
GROUP BY 1, 2 ORDER BY vezes DESC LIMIT 20;

-- custo e cache nos últimos 7 dias
SELECT sum(COALESCE((dados->>'input_tokens')::int, 0))  AS tokens_entrada,
       sum(COALESCE((dados->>'output_tokens')::int, 0)) AS tokens_saida,
       avg((dados->>'cache_hit')::boolean::int)         AS cache_hit_rate
FROM telemetria;

-- onde está a lentidão, por etapa (mediana)
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY (dados->>'ms_retrieve')::float) AS p50_retrieve,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (dados->>'ms_llm')::float)      AS p50_llm,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (dados->>'ms_total')::float)    AS p50_total
FROM telemetria;

-- qualidade do retrieval caindo? (drift)
SELECT date_trunc('day', criado_em) AS dia,
       avg((dados->>'score_top')::float) AS score_medio,
       count(*) FILTER (WHERE dados->>'origem' = 'base')::float / count(*) AS taxa_base
FROM telemetria GROUP BY 1 ORDER BY 1;
```

### Privacidade e LGPD

**O texto da pergunta nunca é registrado** — só `assunto` e um hash truncado.
Perguntas de aluno tocam assuntos sensíveis (ver `ENCAMINHAMENTOS` em
`app/core/config.py`); o hash preserva o que interessa, que é agrupar perguntas
repetidas sem resposta.

**Não gravar a pergunta não basta**, e esse foi o principal achado de T3.4:
`topico` é escrito **pelo LLM a partir da pergunta**, e nada impede o modelo de
repetir o RA que o aluno digitou ("acesso ao Canvas do RA 12345678"). Esse campo
é persistido e é justamente o que o relatório de lacunas lê. O mesmo vale para
`erro`, que carrega `str(exc)`. Os dois passam por `pii.mascarar` num ponto
único, na emissão do registro (`app/core/telemetry.py`) — pela mesma razão da
rede de segurança em `responder.answer`: cada ponto de extensão futuro é mais um
lugar onde dá para esquecer.

| Mecanismo | O que faz | Onde |
|---|---|---|
| Hash da pergunta | Agrupa repetição sem guardar o texto | `telemetry.hash_pergunta` |
| Alerta de PII | `pii: ["cpf","ra"]` — a **categoria**, nunca o valor | `app/core/pii.py` |
| Mascaramento | `topico` e `erro` viram `... do RA [ra]` antes de persistir | `telemetry.registrar` |
| Retenção | 7 dias, apagados na própria escrita | `app/db/telemetry_store.py` |

O detector cobre CPF, RA/matrícula, e-mail e celular. Ele é calibrado para
**precisão, não recall**: um alerta que dispara em número de protocolo ou em ano
("2024 2025") vira ruído e é ignorado em duas semanas — aí o vazamento de
verdade passa junto. Por isso CPF sem pontuação é validado pelos dígitos
verificadores, RA exige a palavra que o nomeia por perto, e telefone fixo ficou
de fora de propósito.

**Limite conhecido:** a tabela `resposta_cache` guarda o texto gerado pelo LLM,
que em tese poderia ecoar um identificador vindo da pergunta. Não é mascarado
porque isso corromperia a resposta servida ao aluno, e o risco é baixo: desde
T2.4 a chave do cache inclui a pergunta, então uma entrada com dado pessoal só é
recuperada por quem digitar exatamente o mesmo texto.

**Ligar/desligar:** `TELEMETRY_STDERR_ENABLED` controla o log no terminal e
`TELEMETRY_DB_ENABLED` a gravação no banco — independentes. O par usual em uso
normal é `false`/`true`: terminal limpo, registro no Postgres.

**Retenção:** registros com mais de `TELEMETRY_RETENTION_DAYS` (7) dias são apagados
na própria escrita, no máximo uma vez por hora por processo — sem cron, sem job. A
janela é curta de propósito: o valor deste dado é operacional (custo, latência e
documento faltando na semana), e guardar hash de pergunta de aluno indefinidamente
não se justifica.

**Falha isolada:** banco fora do ar não derruba a resposta — o INSERT é perdido, a
linha em stderr continua saindo. Sem dependência nova: é `logging`, `json` e o
Postgres que já estava lá.

## Fluxo de dados

```
data/raw/<assunto>/*.pdf|txt|md
   │  loaders/registry.py      (fonte -> Document)
   ▼
pipeline.py ── chunker.py ── embeddings locais (HF) ── pgvector
                                                          │
pergunta ── preprocess.py ── triagem.py ──► outro departamento?
                                  │              origem="encaminhado"
                            é do agente
                                  │
                            retriever.py ─────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                achou algo                   vazio
                    │                           │
                    ▼                           │
      cache? (assunto+confiança+chunks)         │
       │                    │                   │
      hit                  miss                 │
       │                    ▼                   │
       │      responder.py ── Gemini            │
       │                    │                   │
       │            contexto serve?             │
       │              não ──┴── sim             │
       │               │        │               │
       │               └────────┼──────────────►│
       │                        │               ▼
       │                        │        web_fallback.py
       │                        │        (allowlist + similaridade)
       │                        │               │
       │                        │         ┌─────┴─────┐
       │                        │      achou       nada
       │                        │         │           │
       │                        │         ▼           ▼
       │                        │      Gemini    secretaria
       ▼                        ▼    (prompt web) origem="nenhuma"
        resposta + fontes (arquivo, página)   resposta + fontes (URL)
        origem="base"                         origem="web"
```

O guardrail tem **três saídas** para "não respondo isso", e elas medem coisas
diferentes: `origem="encaminhado"` é assunto de outro departamento (nunca vai
ser indexado aqui), enquanto `origem="nenhuma"` é documento faltando na base —
o sinal que vira pauta de ingestão.

## Organização

| Caminho | Responsabilidade |
|---|---|
| `app/core/` | configuração e contratos (`Query`, `Answer`, `RetrievedChunk`) |
| `app/core/telemetry.py` | 1 linha JSON por pergunta: token, latência, guardrail, qualidade |
| `app/providers/gemini.py` | **único** ponto que conhece o provedor de IA |
| `app/ingestion/loaders/` | fonte de dados → `Document` |
| `app/ingestion/chunker.py` | divisão em chunks (função pura) |
| `app/ingestion/pipeline.py` | orquestra load → chunk → embed → indexa |
| `app/retrieval/retriever.py` | busca por similaridade + filtro por assunto |
| `app/agent/` | pré-processamento, prompt, cache e geração da resposta |
| `app/agent/prompts.py` | prompts + marcador `#TOPICO` lido pela telemetria |
| `app/agent/triagem.py` | encaminha assunto de outro departamento antes do RAG |
| `app/agent/web_fallback.py` | busca externa restrita à allowlist de domínios oficiais |
| `app/db/vector_store.py` | conexão com pgvector |
| `app/db/response_cache.py` | cache de resposta por conjunto de chunks (mesmo Postgres) |
| `app/db/telemetry_store.py` | tabela `telemetria` (JSONB), retenção de 7 dias e a consulta de lacunas |
| `app/core/pii.py` | detecção e mascaramento de RA/CPF/e-mail/telefone (LGPD) |
| `app/api/routers/demo.py` | rota `/demo` — injeta a chave da demo no HTML |
| `app/static/index.html` | o frontend de demonstração (1 arquivo, sem build) |
| `scripts/` | CLIs de ingestão, de pergunta e o relatório de lacunas |

## Pontos de extensão (features fora da v1)

Cada feature futura tem um lugar já definido — nenhuma exige reescrever a base:

| Feature | Onde entra | O que muda |
|---|---|---|
| **Web scraping** do site da instituição | `app/ingestion/loaders/registry.py` | registrar um `WebBaseLoader`; o resto do pipeline é o mesmo |
| **APIs públicas** (calendário acadêmico, API do Canvas) | mesmo registry, ou fonte de contexto extra em `responder.py` | novo loader; com 3+ fontes assim, o roteamento vira tool calling tendo `retrieve` e `buscar_na_web` como tools |
| **FAQ estruturado** (match exato, sem LLM) | antes do `retrieve` em `responder.py` | responde as perguntas de altíssima frequência com texto aprovado, latência ~0 |
| **Abertura de chamado** | onde hoje está `_encaminhar_para_secretaria` | transforma o encaminhamento em ação: abre o ticket já com a pergunta |
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
  não alucina um procedimento acadêmico — tenta as páginas públicas oficiais e,
  não achando, encaminha para a secretaria. Também serve de termômetro de quais
  documentos faltam na base (`Answer.grounded` continua `False` mesmo quando a
  web responde; quem precisa distinguir lê `Answer.origem`).
- **Fallback como ramo do guardrail, não como tool do LLM**: o gatilho ("o
  retrieval voltou vazio") é um `if`, não uma decisão ambígua. Deixar o modelo
  escolher custaria uma chamada extra para decidir o que o código já sabe, e
  trocaria uma condição testável por uma não-determinística. Só este ramo paga a
  latência da busca — as perguntas que a base responde bem seguem intactas.
- **Allowlist validada na URL, não confiada no `site:`**: o operador `site:` é
  só direcionamento de recall; a garantia vem da revalidação de cada URL
  devolvida. Assim, trocar de buscador (ou o buscador mudar de comportamento)
  não afeta a restrição.
- **Filtro de relevância da web com o embedding local**: o mesmo modelo já
  carregado para a base decide se o snippet serve, sem gastar token para
  descobrir que a busca não trouxe nada. Limiar calibrado em busca real
  (acertos em 0.52–0.65, melhor falso-positivo fora de escopo em 0.37).
- **Caminho da web sem cache**: a `_cache_key` depende de ids de chunk, que não
  existem para resultado de web, e conteúdo externo muda sem aviso enquanto a
  tabela `resposta_cache` não tem TTL. É o caminho raro; cachear depois, por
  conjunto de URLs e com expiração.
- **Passos explícitos no `responder.py`** em vez de uma chain fechada: dá para
  inspecionar o contexto recuperado (`--debug`) antes da chamada ao LLM.
- **Cache por conjunto de chunks, não pelo texto da pergunta**: duas perguntas
  parafraseadas que recuperam o mesmo topo do retrieval caem na mesma chave, e
  reingerir um arquivo alterado muda os ids recuperados e invalida a chave
  sozinho — sem tabela de invalidação nem TTL manual. Guardado no mesmo
  Postgres da ingestão, sem infra nova.

## Próximos passos sugeridos

O gargalo hoje é **conteúdo, não código** — e a demo tornou isso visível: com o
corpus atual, quase toda pergunta sai com badge amarelo (`origem="web"`), ou
seja, ~30-60s de latência num caminho que existia para ser exceção. As duas
ferramentas para medir o progresso disso passaram a existir agora
(`scripts/lacunas.py` e o badge de origem), o que muda a ordem da lista:

1. Ingerir um conjunto de PDFs reais e calibrar `CHUNK_SIZE` e
   `RELEVANCE_THRESHOLD` com `scripts/ask.py --debug`. É o item **T0.6** do
   [BACKLOG.md](BACKLOG.md), e ele decide sozinho quanto do tráfego vai para o
   fallback web — o caminho lento e caro.
2. Rodar `python -m scripts.lacunas` semanalmente e usar a lista como fila de
   ingestão. Repetir o passo 1 medindo a proporção de `origem="base"` antes e
   depois: é a métrica de que a base está melhorando.
3. Montar um conjunto de ~20 perguntas reais de alunos como teste de regressão
   do retrieval.
4. Fixar a dimensão do embedding e criar índice HNSW quando o corpus crescer.
5. Avaliar o gatilho na "zona cinzenta" (retrieval devolve chunks fracos, entre
   `RELEVANCE_THRESHOLD` e ~0.5), hoje fora do fallback de propósito.
