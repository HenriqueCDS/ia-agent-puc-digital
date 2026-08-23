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

## Sprint 2 — Segurança e correção ✅ concluída (2026-08-21)

Tudo na borda (`app/api/`), exceto duas exceções declaradas: `_cache_key` em
`app/agent/responder.py` (T2.4, que é correção de bug, não de borda) e
`get_chat_model` em `app/providers/gemini.py` (T2.5).

Validado contra o Postgres real com `lifespan` (não só com dublê): boot com
warm-up 31,8s, `/v1/ready` 200 em 10ms com o banco de pé, `/v1/assuntos` sem
chave e com chave errada barrado em 2-3ms **sem abrir conexão** (com chave
válida a mesma rota leva 407ms — a diferença é a prova de que a autenticação
roda antes do banco), CORS devolvendo o cabeçalho só para a origem declarada,
4ª chamada acima do limite bloqueada com `Retry-After: 61`, e `APIKeyHeader`
aparecendo no OpenAPI de `/v1/ask`. Suíte inteira: 174/174.

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| ✅ **T2.1** | Auth por API key (`X-API-Key`), chaves por integração no `.env` | `app/api/deps.py`, `app/api/errors.py`, `app/core/config.py` | Sem chave→401; chave inválida→401; `/health` continua público | 2h |
| ✅ **T2.2** | CORS com origens por env (nunca `*` junto com auth por header) | `app/api/app.py`, `.env.example` | Front em outra origem chama a API; origem não listada é bloqueada | 1h |
| ✅ **T2.3** | Rate limit por API key + teto global diário | `app/api/ratelimit.py`, `app/api/deps.py` | Estourou→429 com `Retry-After`. Protege cota do Gemini e o buscador do 429 | 3h |
| ✅ **T2.4** | **Corrigir `_cache_key`** — pergunta normalizada na chave | `app/agent/responder.py`, `tests/test_responder.py` | Duas perguntas distintas com o mesmo top-5 recebem respostas distintas | 1h |
| ✅ **T2.5** | Timeout na chamada ao Gemini | `app/providers/gemini.py` | Request pendurada não segura thread do pool indefinidamente | 30min |
| ✅ **T2.6** | `/v1/health` (liveness) + `/v1/ready` (checa Postgres + chave) | `app/api/routers/v1.py`, `app/api/schemas.py` | Com o banco derrubado, `/ready` responde 503 — hoje `/health` mente "ok" | 1h |

### Por que cada item

- **T2.1** — cada `/ask` que não bate cache é uma chamada paga ao Gemini +
  até 5 buscas web. O endpoint aberto é um proxy grátis para a cota da
  instituição. Sem identificar o consumidor também não há como aplicar
  quota nem atribuir custo por canal, e rate limit só por IP é inútil numa
  rede institucional atrás de NAT.
- **T2.4** — `_cache_key` era `assunto + alta_confiança + ids dos chunks`; o
  **texto da pergunta não entrava na chave**. Duas perguntas *diferentes* que
  recuperam o mesmo top-5 recebiam a mesma resposta — ex.: "como envio uma
  tarefa no Canvas?" e "onde vejo a nota da tarefa no Canvas?"
  plausivelmente recuperam os mesmos 5 chunks com `top_k=5` e limiar 0.35.
  A invalidação por id continua valendo; o que quebrava é a premissa "mesmo
  conjunto de chunks ⇒ mesma resposta", verdadeira só para paráfrases.

### Achados ao implementar

- **T2.1 — a ordem das dependências na assinatura da rota é de segurança, não
  de estilo.** O FastAPI resolve os `Depends` na ordem em que aparecem: com
  `get_assuntos_validos` antes da autenticação, uma request sem chave ainda
  abria conexão e rodava o `DISTINCT` antes de tomar 401 — o endpoint fechado
  continuava servindo de carga grátis ao banco. Medido no smoke contra o
  Postgres real: 2-3ms sem chave contra 407ms com chave válida. Há teste
  travando isso (`test_assuntos_sem_chave_da_401_sem_tocar_no_banco`).

- **T2.1 — `auto_error=False` no `APIKeyHeader`.** O default do FastAPI devolve
  um 403 próprio, fora do envelope de `errors.py` — a mesma API passaria a ter
  dois formatos de erro. Com `auto_error=False`, a ausência do header vira
  `None` e o 401 sai pelo mesmo caminho de todo o resto. (401 e não 403 de
  propósito: 401 diz "sua credencial não vale, tente outra"; 403 diria
  "identificamos você e você não pode", que não é o caso.)

- **T2.3 — janela deslizante, não contador que zera no minuto cheio.** Com
  janela fixa, um limite de 20/min aceita 40 chamadas em 2 segundos em volta da
  virada — exatamente o pico que o limite deveria conter. Há teste de regressão
  para isso (`test_janela_fixa_seria_o_dobro_do_limite_na_virada`).

- **T2.5 — timeout sem limitar retry não resolve nada.** O default de
  `max_retries` da lib do Gemini é **6**: um timeout de 30s viraria 180s de
  thread presa no pior caso, que é o problema que T2.5 existe para evitar. Os
  dois viraram config (`GEMINI_TIMEOUT`, `GEMINI_MAX_RETRIES`).

- **T2.6 — `/ready` é público de propósito.** Exigir chave nele impediria
  justamente quem monitora de monitorar. O corpo diz *qual* dependência caiu
  (`{"banco": false}`) e nada além disso — sem host, sem porta, sem mensagem do
  driver. E a checagem da chave do LLM é só de presença: validar de verdade
  custaria uma chamada paga ao Gemini a cada probe.

**Limite conhecido do rate limit (T2.3):** o estado é em memória do processo.
Com mais de um worker do uvicorn, cada um tem o seu contador e os tetos viram
`N * limite`. Está documentado em `app/api/ratelimit.py` e amarrado a **T4.3**,
que é onde a decisão de workers vs. RAM do modelo aparece — quando ela for
tomada, só `RateLimiter` muda (`get_rate_limiter()` já é o único acesso).

---

## Sprint 3 — Demo e sinal de produto ✅ concluída (2026-08-23)

Validado contra o Postgres e o Gemini reais, com `lifespan` e também com
`uvicorn` de verdade: boot com warm-up 6,0s, `GET /demo` 6ms servindo 23 KB com
`Cache-Control: no-store` e a chave da integração `demo` injetada (placeholder
ausente na resposta), `/` → `/demo` em 307, `/v1/ready` 200 em 3ms,
`/v1/assuntos` 200 em 8ms, `POST /v1/ask` com `request_id` batendo no header e
no corpo, e a linha correspondente na tabela `telemetria` trazendo o mesmo
`request_id`, `pii: ["ra"]` e o RA **ausente da linha inteira**. O relatório de
lacunas rodou sobre esses registros reais. Suíte inteira: 221/221 (era 174).

| # | Tarefa | Arquivos | Critério de aceite | Esforço |
|---|---|---|---|---|
| ✅ **T3.1** | Frontend estático (1 arquivo HTML+JS, sem build) servido em `/demo` | `app/static/index.html`, `app/api/routers/demo.py` | Pergunta → resposta + **badge de origem** + fontes com link + latência | 4h |
| ✅ **T3.2** | Log estruturado de interação (`assunto`, `origem`, `grounded`, latência, chunks, consumidor) | `app/core/telemetry.py`, `app/api/routers/v1.py` | Toda request registrada; `request_id` correlaciona log e resposta | 3h |
| ✅ **T3.3** | Relatório de lacunas: perguntas com `grounded=False` agrupadas | `scripts/lacunas.py`, `app/db/telemetry_store.py` | Lista priorizada do que falta indexar — vira o roadmap de ingestão | 2h |
| ✅ **T3.4** | Política LGPD: retenção, hash do identificador, alerta de RA/CPF na pergunta | `app/core/pii.py`, `app/core/telemetry.py`, README | Nada de identificável em claro na tabela | 2h |

### Desvios do plano original

- **T3.2/T3.4 não criaram `app/db/interacoes.py`.** Quando o backlog foi
  escrito, esse arquivo era o lugar onde o log de interação passaria a existir.
  Ele já existe: `app/core/telemetry.py` + `app/db/telemetry_store.py`
  registram `assunto`, `origem`, `grounded`, `n_chunks`, `score_top`,
  `cache_hit`, tokens e latência por etapa numa coluna JSONB, com retenção de 7
  dias. Uma segunda tabela com os mesmos campos seria duplicação com dois
  pontos de escrita para manter em sincronia. Faltavam só duas coisas, e foram
  elas o trabalho de T3.2/T3.4: `request_id` e a contenção de PII.

- **Exceção declarada fora da borda:** `Answer.cached` (`app/core/models.py` e
  `app/agent/responder.py`). O cache hit existia só na telemetria, que é
  destino de observabilidade e não é lido por quem chama a API. Sem expor isso,
  a demo mostra "1.476 ms" numa resposta cacheada e "29.884 ms" numa nova sem
  conseguir dizer por quê — e o cache, que é o item de custo mais visível do
  sistema, fica indemonstrável.

- **T2.3 ganhou teto diário por consumidor** (`RATE_LIMIT_DIARIO_POR_CONSUMIDOR`).
  Não estava no backlog, mas é o que torna a decisão 3(a) defensável: sem teto
  próprio, a chave pública da demo consome o orçamento do dia inteiro,
  incluindo o do AVA.

### Achados ao implementar

- **T3.4 — não gravar a pergunta não basta.** A premissa da telemetria era "o
  texto da pergunta nunca entra no registro, só `assunto` e hash". Só que
  `topico` é escrito **pelo LLM a partir da pergunta**, e nada impede o modelo
  de repetir o RA que o aluno digitou ("acesso ao Canvas do RA 12345678"). Esse
  campo é persistido e é justamente o que o relatório de lacunas lê. O mesmo
  vale para `erro`, que carrega `str(exc)`. Os dois passam por `pii.mascarar`
  num ponto único, em `telemetry.registrar`, pela mesma razão da rede de
  segurança em `responder.answer`: cada ponto de extensão futuro é mais um
  lugar onde dá para esquecer.

- **T3.4 — o alerta de PII vale pela precisão, não pelo recall.** As duas
  primeiras versões dos padrões falharam contra casos reais, e as duas falhas
  eram do tipo que mata um alerta de LGPD por desuso:
  (a) `\W` entre "RA" e o número não casa "meu RA **é** 12345678", porque `é` é
  caractere de palavra — o vão virou `[^\d\n]{0,15}`;
  (b) `matrícula 987654321` era contada como RA **e** telefone, porque 9 dígitos
  crus casam o padrão de celular. Hoje o telefone exige DDD ou separador
  interno. Pela mesma lógica, CPF sem pontuação é validado pelos dígitos
  verificadores (senão todo protocolo de 11 dígitos vira alerta) e telefone
  fixo ficou **fora** de propósito: `\d{4}[\s-]?\d{4}` casa "2024 2025".

- **T3.3 — agrupar por `(tema, assunto, origem)` destrói o relatório.** A
  primeira versão da consulta fazia isso, e no banco real a mesma lacuna
  apareceu em três linhas de peso 1: "Envio de atividade no Canvas" rotulado
  ora `canvas`, ora `puc-digital`, mais uma linha por ter caído na web numa das
  vezes. Como a ordenação por frequência é o único motivo de o relatório
  existir, o agrupamento passou a ser só pelo tema (em caixa baixa), com
  assunto e origem virando agregados da linha. Consolidado, o tema sobe para o
  topo com 2 ocorrências, que é o que se queria ver.

- **T3.3 — `grounded=false` inclui o caminho web, e isso é intencional.** Uma
  resposta que veio de página pública oficial **também** é lacuna: a informação
  existia, só não na nossa base. É inclusive a lacuna mais barata de fechar,
  porque a página que respondeu já diz qual documento indexar. Ficam de fora
  `origem='encaminhado'` (outro departamento, nunca vai ser indexado aqui) e as
  linhas com `erro` (falha de infra, não ausência de conteúdo) — sem esses dois
  cortes, uma queda do Postgres viraria "documento faltando".

- **T3.1 — o front tem 4 badges, não 3.** O escopo escrito no backlog previa
  `base`/`web`/`nenhuma`; `encaminhado` (triagem) entrou no agente depois. É a
  mesma divergência que já tinha quebrado `app/api/schemas.py` em runtime e que
  levou `Origem` a morar em `app/core/models.py`.

- **T3.1 — o corpus atual manda quase tudo para o fallback web.** Rodando a
  demo contra o banco real, "como acesso o Canvas?" saiu com `origem="web"` em
  ~60s na primeira vez. Não é bug do front (as fontes vieram com URL clicável,
  que é T1.3 funcionando) — é **T0.6 ainda não feita**. A demo torna isso
  visível de uma forma que nenhum log tornava: o badge amarelo em quase toda
  pergunta é o argumento pronto para priorizar a calibração do limiar e a
  ingestão.

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

#### O frontend de demonstração, como ficou

Um arquivo (`app/static/index.html`), sem framework, sem build e **sem nenhum
recurso externo** — nada de CDN, nada de fonte remota. Não é minimalismo por
estética: é o que permite abrir a demo numa máquina sem internet (a banca, a
sala de aula) e o que garante que a página não manda a pergunta do aluno para um
terceiro.

- interface de conversa, com a marca do agente (SVG radial de 12 raios,
  definida uma vez e reaproveitada por `<use>` no avatar de cada resposta);
- campo de pergunta + `<select>` de assunto populado por `GET /v1/assuntos`;
- indicador de estado no cabeçalho, vindo de `GET /v1/ready`;
- **badge de origem** com cor: `base` (verde, material interno) / `web`
  (amarelo, página pública) / `encaminhado` (azul, outro departamento) /
  `nenhuma` (cinza, sem resposta em nenhuma fonte). O `title` de cada badge
  explica o que aquele caminho significa — é o guardrail ficando visível;
- lista de fontes com link quando houver URL, e o score de cada uma;
- latência (medida no cliente, incluindo rede: é a que o aluno sente) e o chip
  de cache hit;
- `<details>` com o JSON cru da resposta — para apresentação a banca/gestor;
- erros traduzidos a partir do envelope de T1.5 pela chave `erro`, que é
  estável (429 mostra o `Retry-After`, 503 diz que é o serviço), nunca por
  parsing do texto de `detalhe`;
- renderização mínima do markdown que o modelo de fato produz (listas
  numeradas de procedimento, negrito, `código`, URL solta), com **escape
  aplicado antes** de qualquer regra: o texto vem do LLM, que por sua vez leu
  páginas externas.

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
| ✅ S1 — Borda HTTP | ~13h | Integração multi-plataforma |
| ✅ S2 — Segurança | ~8h | Exposição fora da máquina de desenvolvimento |
| ✅ S3 — Demo + sinal | ~11h | Demonstração e roadmap de conteúdo |

**O que ficou aberto:** S0 inteira, e ela agora é o gargalo real. A demo tornou
isso visível — com o corpus atual, quase toda pergunta sai com badge amarelo
(`origem="web"`), o que significa ~30-60s de latência num caminho que existia
para ser exceção. **T0.6 (calibrar `RELEVANCE_THRESHOLD`) e T0.2 (ingerir um
assunto real de verdade) são o próximo trabalho**, e agora há duas ferramentas
para medir o resultado que antes não existiam: `python -m scripts.lacunas`
(o que falta indexar, priorizado) e o badge de origem na demo (proporção do
tráfego que a base cobre).

---

## Decisões pendentes

1. ~~**T2.4 — como corrigir a `_cache_key`?**~~ **Resolvido em (a)**: a
   pergunta normalizada (caixa, espaço repetido e pontuação final descartados)
   entra na chave. (b) — cachear só no ramo `alta_confianca` — foi descartada
   porque preservaria mais hits deixando o erro de pé justamente no ramo comum,
   que é onde ele acontece. A normalização recupera parte do hit rate perdido
   sem misturar perguntas de fato diferentes.

2. **Ordem entre S2 e S3.** ~~Como está, S2 vem antes.~~ Resolvido pelos fatos:
   S2 foi feita antes. **T2.2 (CORS) já está pronto para o front de T3.1**, que
   de todo modo será servido da mesma origem (`StaticFiles`).

3. ~~**A demo (T3.1) precisa de uma chave de API no navegador.**~~ **Resolvido
   em (a)**: `/demo` injeta a chave no HTML no servidor. A opção (b) (cookie de
   sessão) foi descartada — cria um segundo mecanismo de autenticação para
   manter e testar, e resolve um problema que os tetos abaixo já contêm.

   A chave continua pública para quem abrir o DevTools; isso não tem solução
   dentro de um arquivo estático. O que foi controlado é o **estrago**:
   - integração própria (`DEMO_CONSUMIDOR`, default `demo`), então revogá-la não
     derruba o AVA;
   - teto diário próprio (`RATE_LIMIT_DIARIO_POR_CONSUMIDOR=demo:200`), então o
     pior caso de abuso é a demo parar — e o teto **global** é checado antes do
     próprio, para que nenhum consumidor passe do limite de custo do serviço;
   - injetada em runtime, nunca commitada: o arquivo em disco tem só o
     placeholder, e há teste travando isso
     (`test_o_arquivo_em_disco_nao_contem_chave_nenhuma`);
   - `Cache-Control: no-store` na resposta — a página tem uma chave dentro, e um
     proxy guardando isso é como ela sobrevive a uma rotação.

---

## Diagnóstico que originou este backlog

Avaliação da API atual ([app/main.py](app/main.py), 49 linhas) para uso
multi-plataforma. Cada item virou uma tarefa acima.

| Achado | Tarefa |
|---|---|
| Contrato de resposta perde a URL da fonte web e o score | T1.3 |
| `origem: str` sem `Literal` — OpenAPI não documenta os valores | T1.4 |
| Erro tem formato diferente do sucesso; falhas de infra viram 500 | T1.5 |
| Sem autenticação — endpoint aberto é proxy grátis para o Gemini | ✅ T2.1 |
| Sem versionamento de rota — a primeira melhoria de contrato quebra quem integrou | T1.1 |
| CORS ausente — o front de demo não consegue chamar a API | ✅ T2.2 |
| Sem limite de tamanho na pergunta; `assunto` é texto livre | T1.6, T1.7 |
| `/health` retorna "ok" incondicionalmente | ✅ T2.6 |
| Embeddings carregam lazy no primeiro request (modelo de ~1 GB) | T1.2 |
| `llm.invoke()` sem timeout | ✅ T2.5 |
| Rate limiting ausente | ✅ T2.3 |
| Interações não são registradas — sinal de lacuna é descartado | T3.2, T3.3 |
| Cache pode servir a resposta de outra pergunta | ✅ T2.4 |
| Sem memória — follow-up quebra o retrieval | T4.1 |

Achados de S3 que entraram na tabela depois:

| Achado | Tarefa |
|---|---|
| Interações não são correlacionáveis com a resposta que o aluno recebeu | ✅ T3.2 |
| `topico` (escrito pelo LLM) é persistido e pode repetir RA/CPF da pergunta | ✅ T3.4 |
| Sinal de `grounded=false` gravado mas nunca lido | ✅ T3.3 |
| Cache hit invisível para quem chama a API | ✅ T3.1 (`Answer.cached`) |
| Teto de custo só global — chave pública da demo gastaria o do AVA | ✅ T3.1 |

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
