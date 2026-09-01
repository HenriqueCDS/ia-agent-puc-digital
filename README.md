# Agente de IA — Suporte Acadêmico EAD (v1)

Agente de RAG que responde dúvidas de alunos e funcionários sobre o Canvas e
sobre procedimentos acadêmicos, usando como base de conhecimento os arquivos
colocados em `data/raw/<assunto>/` (PDF, texto, DOCX e os modelos de e-mail de
atendimento em `data/raw/email_modelos/`) e as páginas oficiais pré-indexadas
pelo crawler da allowlist (`scripts/crawl.py`).

Escopo da v1: base local + páginas da allowlist, sem dados sigilosos do aluno.
Ver [arquitetura-agente-ia-suporte-ead-v0.md](arquitetura-agente-ia-suporte-ead-v0.md).

## Estado atual do projeto

Esqueleto da v1 implementado e executável localmente. O que existe hoje:

**Ingestão** — leitura de `.pdf` (via `pypdf`), `.txt`, `.md`, `.docx` e a
planilha de modelos de e-mail de atendimento (`.xlsx` pré-chunkado por
`ler_dados_pst.ipynb`, em `data/raw/email_modelos/`) a partir de
`data/raw/<assunto>/`, divisão em chunks com overlap
(`RecursiveCharacterTextSplitter`), geração de embeddings com um modelo local
(HuggingFace/sentence-transformers, multilíngue — sem depender de cota de API)
e indexação no pgvector. Todo loader novo entra num ponto só:
`app/ingestion/loaders/registry.py`. A metadata de origem (`assunto`,
`source_type`, `source_uri`, `source_path`, `source_name`, `page`,
`chunk_index`; `categoria="web"` no conteúdo crawlado) é gravada em todo chunk.
A ingestão é idempotente: cada chunk tem id determinístico e os chunks antigos
da fonte são removidos antes da reindexação, então rodar o ingest de novo não
duplica nem deixa conteúdo órfão. `tests/test_ingestao.py` trava que todo
arquivo em `data/raw/` tenha um loader — nada fica de fora em silêncio.

**Retrieval** — busca por similaridade de cosseno no pgvector, com filtro
opcional por assunto e corte por limiar de relevância (`RELEVANCE_THRESHOLD`).

**Agente** — monta o prompt com os trechos recuperados e suas citações, chama o
LLM e devolve a resposta com as fontes (`arquivo, página`). Quando nada passa
do limiar, não chama o LLM com contexto vazio: recorre às páginas oficiais da
allowlist (ver abaixo) antes de encaminhar para a secretaria.

**Cadeia de provedores** — a chamada ao LLM passa por uma cadeia de fallback
(`LLM_PROVIDERS=gemini,huggingface,groq,openrouter`): o primeiro que responder vence, e um
provedor fora do ar (cota estourada, credencial inválida, timeout, 5xx) faz a
pergunta cair para o próximo **sem** o aluno perceber. Erro do *pedido* (prompt
inválido, modelo inexistente) propaga em vez de cair — nenhum outro provedor
aceitaria o mesmo pedido. Uma tentativa por provedor, sem backoff: fallback é
perguntar a outro, retry é insistir com o mesmo. Ver `app/providers/`.

**Escolha do modelo** — cada provedor tem o seu no `.env` (`CHAT_MODEL`,
`HF_MODEL`, `GROQ_MODEL`, `OPENROUTER_MODEL`). Modelo fora do catálogo da chave dá 404 com
uma mensagem que não distingue "não existe" de "você não tem acesso" — as duas
se resolvem olhando o catálogo da própria chave:

```bash
python -m scripts.modelos            # o que cada chave acessa; marca o do .env
python -m scripts.ask "..." --modelo groq:llama-3.1-8b-instant   # testa sem editar o .env
```

Um 404 desses **não derruba** o `/ask`: o provedor mal configurado sai de cena e
o próximo responde — mas a linha sai em `ERROR`, porque esperar não conserta.
Para escolher o modelo por requisição no `POST /ask` (campo opcional `modelo`,
sem fallback), ligue `ASK_MODELO_OVERRIDE_ENABLED` — desligado por padrão, já
que a chave é da instituição e o teto diário conta requisições, não custo.

Por padrão o override também não usa cache (repetir a mesma pergunta com o
mesmo modelo paga o LLM de novo a cada vez); `MODELO_OVERRIDE_CACHE_ENABLED`
liga o cache também para ele. É seguro ligar: o modelo entra na chave do
cache, então um override nunca serve a resposta cacheada de outro modelo nem
da cadeia normal.

**Páginas oficiais (allowlist)** — o que a base indexada não cobre pode estar
numa página oficial. O conjunto é FECHADO (`WEB_ALLOWLIST` em
`app/core/config.py`): caminhos curados do portal da PUC-Campinas
(`/calendario/`, `/secretaria-geral/`, `/biblioteca/`), a base de conhecimento
oficial do Canvas (`community.instructure.com/en/kb/`) e a doc do Teams
(`support.microsoft.com/pt-br/teams/`). Cada entrada é `(host, path_prefixes)` —
o portal **não** entra inteiro nem por subdomínio: uma página nova só passa a ser
citável depois de alguém conferir e adicionar o prefixo (KB-2 — antes disso o
agente citava vestibular e avaliação institucional em pergunta de nota).

Há dois caminhos até essas páginas:

- **Pré-crawl (comum)** — `python -m scripts.crawl` lê o sitemap de cada fonte,
  filtra pelos `path_prefixes` (reusando a mesma revalidação da busca ao vivo),
  extrai o conteúdo e indexa no pgvector com `source_type="web"` /
  `categoria="web"` e o `assunto` da fonte. Roda semanalmente. A pergunta cujo
  conteúdo já foi crawlado é respondida pelo RAG normal (~300ms, `grounded=true`).
- **Busca ao vivo (rede)** — quando nem a base nem o conteúdo crawlado cobrem, o
  `web_fallback` raspa o DuckDuckGo (uma query `site:<host>` por fonte, cada URL
  revalidada contra a allowlist), corta por similaridade com o embedding local e
  passa por um veto final do LLM (`#SEM_COBERTURA#`). É o caminho raro e caro
  (~15s); o pré-crawl existe para mantê-lo raro. Liga/desliga com
  `WEB_FALLBACK_ENABLED`.

Nada relevante em nenhum dos dois → resposta com o contato da secretaria. Ver
`app/agent/web_fallback.py` e `eval/analises/kb-3-melhorar-fallback-na-base.md`.

**Triagem por assunto** — antes de qualquer coisa, a pergunta que é de outro
departamento (cobrança, diploma, rematrícula…) é encaminhada com o contato certo,
sem tocar no retrieval nem no LLM. É guardrail, não economia: sem ela, uma
pergunta sobre boleto pode recuperar um chunk fraco e o LLM responderia sobre
dinheiro a partir de um documento que não é sobre isso. `matrícula` tem exceções
explícitas (Canvas, disciplina, plataforma) para não perder o que a base
responde bem. Liga/desliga com `TRIAGEM_ENABLED`; as categorias e os e-mails
estão em `ENCAMINHAMENTOS` (`app/core/config.py`); ver `app/agent/triagem.py`.

**Guardrail de entrada** — antes até da triagem, o pedido de ataque/abuso
(injeção de prompt, exfiltração de segredo, `DROP TABLE`, código de exploit,
`altere a nota`…) é encaminhado para o suporte pelo mesmo caminho da triagem —
`origem="encaminhado"`, sem RAG, web nem LLM. Casamento léxico por substring
(mesma filosofia da triagem), calibrado contra o dataset OWASP em `eval/`. Não
substitui as defesas do pipeline (RAG fechado no CONTEXTO, `SYSTEM_WEB`,
allowlist), é a 1ª linha que evita mandar a entrada hostil para um LLM ou para a
busca externa. Liga/desliga com `GUARDRAIL_ENABLED`; ver `app/agent/guardrail.py`
e `eval/analise-telemetria-2026-08-27.md`.

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
que não bate cache é uma chamada paga (e, no caminho raro do fallback ao vivo,
mais um punhado de buscas web), então o endpoint aberto seria um proxy grátis
para a cota da instituição. Sobre isso vêm rate
limit por consumidor (janela deslizante de 60s) e um teto diário do processo,
CORS restrito às origens do `.env`, e timeout na chamada ao LLM. Ver
`app/api/deps.py`, `app/api/ratelimit.py` e `app/api/app.py`.

**Infra** — `docker-compose.yml` com `pgvector/pgvector:pg16`; configuração via
`.env` (`pydantic-settings`).

**Testes** — 388 testes cobrindo chunking, ids determinísticos, corte por
limiar, filtro por assunto, formatação de citação, o guardrail do agente, o
hit/miss do cache de resposta, a cobertura de loader de toda fonte em
`data/raw/` (`test_ingestao`), o crawler da allowlist (parsing de sitemap,
filtro por `path_prefixes`, extração de conteúdo — `test_crawl`), o fallback de
busca externa (allowlist, domínio sósia, path fora dos curados, redirect do
buscador, corte por similaridade, blocklist de assunto sensível e degradação em
caso de rate limit), a cadeia de fallback entre os quatro provedores de LLM e a
borda HTTP inteira — contrato de resposta, cada código de erro, autenticação,
CORS, a janela deslizante do rate limit, liveness vs. readiness e o seletor de
modelo da demo. Rodam sem banco, sem chave de API e **sem rede**, usando dublês
de vector store, de LLM, de cache, de busca e de relógio.

### Validado ponta a ponta; calibração em aberto

O caminho completo `ingest → embeddings locais (e5-base) → pgvector →
retrieve → LLM` já rodou contra um banco real e um corpus de 3 PDFs (1289
chunks) — ver a rodada de avaliação em
[`eval/analise-telemetria-2026-08-26.md`](eval/analise-telemetria-2026-08-26.md).
O que essa rodada deixou em aberto, em ordem de impacto:

1. **`RELEVANCE_THRESHOLD` está fora da faixa útil** do embedding atual (§3 da
   análise): não descarta chunk nenhum hoje, porque o `score_top` de qualquer
   pergunta cai numa faixa estreita (0.84–0.88) esteja a base cobrindo o tema ou
   não. A resposta escolhida é o **reranker cross-encoder** (RET-3) —
   encanamento no código, `RERANKER_ENABLED=false` até calibrar. O antigo
   `EXACT_MATCH_THRESHOLD` / ramo `alta_confianca` foi **removido** junto: com
   ranking real ele deixou de fazer sentido (ver `cross-encoder.md` §5).
2. **Recusa em prosa do LLM escapa dos dois vetos de contexto insuficiente**
   (`prompts.eh_insuficiente`) quando o modelo não emite o marcador esperado —
   infla a acurácia aparente de `origem="web"` (§4 da análise).
3. **Pré-aquecer o modelo de embeddings no boot**: o primeiro request do
   processo paga ~40s de carregamento de peso que as demais requisições não
   pagam (§7 da análise).

As correções já aplicadas nessa rodada (termo ambíguo "bolsa" isolado na
triagem, gabarito do dataset corrigido, prefixo duplicado de `GROQ_MODEL`)
já estão em `app/core/config.py` e `eval/perguntas/perguntas.jsonc`; os itens 1–3
acima continuam pendentes.

### Fora do escopo desta fase

Interpretação de print/imagem, consumo de APIs públicas em tempo real (calendário
acadêmico, API do Canvas), FAQ estruturado sem LLM, classificação de
intenção/escalonamento e canais de atendimento. Nenhum deles está implementado —
cada um tem o lugar de encaixe definido na tabela de
[pontos de extensão](#pontos-de-extensão-features-fora-da-v1).

Web scraping **saiu** do "fora de escopo": `scripts/crawl.py` já indexa as
páginas da allowlist (via sitemap). O que segue fora é o scraping de site
arbitrário — a allowlist continua sendo um conjunto fechado por decisão de
segurança.

## Como rodar

```bash
# 1. dependências
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt

# 2. configuração
copy .env.example .env          # preencha ao menos GEMINI_API_KEY

# 3. banco (Postgres + pgvector)
docker compose up -d

# 4. coloque os arquivos em data/raw/canvas/, data/raw/puc-digital/,
#    data/raw/email_modelos/ (PDF, txt, md, docx, xlsx de modelos)

# 5. ingestão dos arquivos locais
python -m scripts.ingest canvas puc-digital email_modelos -v

# 6. (opcional) indexa as páginas oficiais da allowlist
python -m scripts.crawl --dry-run     # confere as URLs; depois rode sem --dry-run

# 7. pergunte
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
 "pergunta_hash":"8efa09547286","chat_model":"gemini-3.6-flash","provider":"gemini",
 "origem":"base","grounded":true,"n_chunks":2,"score_top":0.95,"score_min":0.87,
 "score_mean":0.91,"reranker_aplicado":null,"score_top_bruto":null,
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
python -m scripts.crawl --dry-run            # URLs da allowlist que entrariam no índice
python -m scripts.crawl                      # crawla e indexa (rodar semanal)
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

### Avaliação de qualidade (eval)

Dataset único de perguntas com origem esperada (`eval/perguntas/perguntas.jsonc`,
125 itens agrupados por origem em blocos comentados) rodado contra o agente de
verdade, para calibrar `CHUNK_SIZE`/`RELEVANCE_THRESHOLD` e comparar modelo:

```bash
# -c limpa a resposta_cache; -m fixa um provider (sem fallback); --timeout encurta o tempo morto
# --intervalo roda só um trecho (1-based, inclusivo) — divide a rodada p/ não estourar a cota
python -m scripts.eval_run --intervalo 26-50 -m huggingface:meta-llama/Llama-3.3-70B-Instruct -c --timeout 15
python -m scripts.eval_report --dias 1 --detalhe   # audita o que ficou gravado
```

`eval_run` roda o dataset e salva o resultado num arquivo local
(`eval/resultados/<timestamp>.json`); `eval_report` lê a **mesma** telemetria
que `scripts.lacunas` usa (canal `eval`), o que permite comparar rodadas
passadas sem reexecutar nada.

> **`acertou` mede só o ROTEAMENTO.** Ele compara `resultado.origem` com
> `origem_esperada` — uma resposta que inventa um prazo ou cita a página errada
> sai como acerto desde que tenha ido pelo caminho certo. Por isso cada linha do
> resultado traz a `resposta` (mascarada por `pii.mascarar`), as `fontes_citadas`,
> os três scores do retrieval (`score_top`/`score_min`/`score_mean`), os flags de
> veto (`base_insuficiente`, `web_insuficiente`, `veto_escapou`) e o `criterio` de
> conferência manual do dataset. O arquivo é a base da revisão, não o veredito —
> ver [`eval/plano-testes-2026-08-28.md`](eval/plano-testes-2026-08-28.md) §1.
>
> Duas colunas com nomes parecidos, de propósito: `fontes_resposta`/`score_fonte_top`
> são as fontes **da resposta** (vazias quando a origem é `nenhuma`/`encaminhado`);
> `chunks_recuperados`/`score_top` são o que o **retrieval** trouxe (quase sempre 5).

As flags existem por causa de armadilhas achadas
nas rodadas reais ([`analise-telemetria-2026-08-26.md`](eval/analise-telemetria-2026-08-26.md)
§6, [`-2026-08-27.md`](eval/analise-telemetria-2026-08-27.md) §10):

- **Cache mascara o pipeline.** Uma pergunta já respondida antes serve do
  `resposta_cache` sem tocar retrieval nem LLM — uma rodada assim não mede
  ajuste nenhum em `CHUNK_SIZE`/`RELEVANCE_THRESHOLD`, e a única pergunta
  não-cacheada (se for a que gera um prompt grande) pode derrubar a rodada num
  413. Use `-c`.
- **A cadeia de fallback troca o gerador no meio da rodada.** Sem `--modelo`,
  a cota do tier gratuito (Gemini: 20 req/dia) estoura no meio e as perguntas
  seguintes saem de outro provider — a rodada compara modelos, não configs. Sem
  `-m` o script avisa. `--timeout 15` corta pela metade os ~30s que cada
  pergunta perde tentando o provider sem cota antes do fallback.
- **~12% das perguntas oscilam sozinhas**, mesma config, mesmo score de
  retrieval: quem varia é o resultado do buscador externo (`ddgs`), não o
  agente — o retrieval em si é determinístico (`score_top` bate na 4ª casa
  decimal entre rodadas). Rode N=3 e compare **item a item**, nunca só o
  total: duas trocas em direção oposta se cancelam no agregado e escondem
  instabilidade real.

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
| `metadata` | o retrieval achou chunks | `assunto` gravado na ingestão (`pipeline._enrich` para arquivo, `crawl._documento` para página crawlada) |
| `allowlist` | respondeu pela web | `FonteWeb.assunto` do domínio (`WEB_ALLOWLIST`) |
| `triagem` | assunto de outro departamento | categoria casada em `ENCAMINHAMENTOS` |
| `guardrail` | pedido de ataque/abuso barrado na entrada | padrão casado em `guardrail._PADROES` (`assunto="fora de escopo"`) |
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
| A qualidade caiu? | `n_chunks`, `score_top`/`score_min`/`score_mean` (dispersão do top-k), `score_top_bruto` (score de E5 antes do rerank, quando `reranker_aplicado`) ao longo do tempo |

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

**A pergunta também é mascarada ANTES de sair da máquina** (PII-1/PII-2): o
provedor de LLM roda nos EUA e a busca de fallback vai para o DuckDuckGo, e até
a correção o `query.text` seguia cru para os dois — CPF, RA, e-mail, telefone e
a **senha** que o aluno cola no texto ("minha senha é `Aluno@2026`, não entra").
`responder._sem_pii` roda `pii.mascarar` no topo de `_responder`, antes de
guardrail/triagem/retrieval, então todo caminho abaixo (base, web e cada fonte
de contexto nova) opera sobre a versão limpa. **A decisão é mascarar, não
recusar:** "não consigo acessar, meu RA é `[ra]`" continua respondível, e barrar
toda pergunta com RA deixaria o agente inútil. A **detecção** (`registro.pii`, o
WARNING de auditoria) continua sobre o texto original — roda em
`telemetry.registrar`, antes do mascaramento.

| Mecanismo | O que faz | Onde |
|---|---|---|
| Hash da pergunta | Agrupa repetição sem guardar o texto | `telemetry.hash_pergunta` |
| Alerta de PII | `pii: ["cpf","ra","senha"]` — a **categoria**, nunca o valor | `app/core/pii.py` |
| Mascaramento de saída | `query.text` → `... meu RA é [ra]` antes do LLM e da busca web | `responder._sem_pii` |
| Mascaramento de persistência | `topico` e `erro` viram `... do RA [ra]` antes de gravar | `telemetry.registrar` |
| Retenção | 7 dias, apagados na própria escrita | `app/db/telemetry_store.py` |

O detector cobre CPF, RA/matrícula, e-mail, celular e senha colada no texto. Ele
é calibrado para **precisão, não recall**: um alerta que dispara em número de
protocolo, em ano ("2024 2025") ou em "esqueci minha **senha**" vira ruído e é
ignorado em duas semanas — aí o vazamento de verdade passa junto. Por isso CPF
sem pontuação é validado pelos dígitos verificadores, RA exige a palavra que o
nomeia por perto, telefone fixo ficou de fora, e a senha só conta quando vem
seguida de um valor com cara de credencial (`senha: X`, `senha é Aluno@2026`),
nunca só a palavra.

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
data/raw/<assunto>/*.pdf|txt|md|docx|xlsx      scripts/crawl.py
   │  loaders/registry.py  (fonte -> Document)   │  sitemap + path_prefixes
   ▼                                             ▼  (source_type="web")
pipeline.py ── chunker.py ── embeddings locais (HF) ── pgvector
                                                          │
pergunta ── preprocess.py ── guardrail.py ──► ataque/abuso?
                                  │              origem="encaminhado"
                                  │
                             triagem.py ──────► outro departamento?
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
       │      responder.py ── LLM (cadeia)      │
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
       │                        │       LLM      secretaria
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
| `app/providers/` | **único** ponto que conhece um SDK de LLM |
| `app/providers/chain.py` | cadeia de fallback entre Gemini, HuggingFace, Groq e OpenRouter |
| `app/providers/base.py` | interface `LLMProvider` + regra de quando cair p/ o próximo |
| `app/ingestion/loaders/` | fonte de dados → `Document` (PDF, txt, md, docx, xlsx de modelos de e-mail) |
| `app/ingestion/chunker.py` | divisão em chunks (função pura) |
| `app/ingestion/pipeline.py` | orquestra load → chunk → embed → indexa; `ingest_file` (arquivo) e `ingest_documents` (páginas crawladas) |
| `app/retrieval/retriever.py` | busca por similaridade + filtro por assunto |
| `app/agent/` | pré-processamento, prompt, cache e geração da resposta |
| `app/agent/prompts.py` | prompts + marcador `#TOPICO` lido pela telemetria |
| `app/agent/triagem.py` | encaminha assunto de outro departamento antes do RAG |
| `app/agent/guardrail.py` | encaminha pedido de ataque/abuso antes do RAG (léxico, OWASP LLM Top 10) |
| `app/agent/web_fallback.py` | busca externa restrita à allowlist de domínios oficiais |
| `app/db/vector_store.py` | conexão com pgvector |
| `app/db/response_cache.py` | cache de resposta por conjunto de chunks (mesmo Postgres) |
| `app/db/telemetry_store.py` | tabela `telemetria` (JSONB), retenção de 7 dias e a consulta de lacunas |
| `app/core/pii.py` | detecção e mascaramento de RA/CPF/e-mail/telefone/senha (LGPD) — persistência e saída p/ LLM |
| `app/api/routers/demo.py` | rota `/demo` — injeta a chave da demo no HTML |
| `app/static/index.html` | o frontend de demonstração (1 arquivo, sem build) |
| `scripts/` | CLIs de ingestão, de pergunta, relatório de lacunas e avaliação |
| `scripts/crawl.py` | indexa as páginas da `WEB_ALLOWLIST` no pgvector (KB-3); rodar semanal |
| `scripts/eval_run.py` | roda um dataset de eval contra o agente de verdade, salva local |
| `scripts/eval_report.py` | audita, na telemetria já gravada, o mesmo dataset (sem reexecutar) |
| `scripts/clear_cache.py` | apaga `resposta_cache` — necessário antes de toda rodada de calibração |
| `scripts/clear_logs.py` | apaga a tabela `telemetria` |
| `eval/` | datasets de avaliação, análises de telemetria e `backlog-problemas.md` (fila priorizada de correções) |

## Pontos de extensão (features fora da v1)

Cada feature futura tem um lugar já definido — nenhuma exige reescrever a base:

| Feature | Onde entra | O que muda |
|---|---|---|
| ~~**Web scraping** da allowlist~~ | `scripts/crawl.py` | **feito** (KB-3): sitemap → `path_prefixes` → `pipeline.ingest_documents`. Falta agendar o re-crawl semanal |
| **APIs públicas** (calendário acadêmico, API do Canvas) | mesmo registry, ou fonte de contexto extra em `responder.py` | novo loader; com 3+ fontes assim, o roteamento vira tool calling tendo `retrieve` e `buscar_na_web` como tools |
| **FAQ estruturado** (match exato, sem LLM) | antes do `retrieve` em `responder.py` | responde as perguntas de altíssima frequência com texto aprovado, latência ~0 |
| **Abertura de chamado** | onde hoje está `_encaminhar_para_secretaria` | transforma o encaminhamento em ação: abre o ticket já com a pergunta |
| **Interpretação de print/imagem** | `app/agent/preprocess.py` | anexo → descrição textual via Gemini multimodal → mesma `Query` |
| **Reranking cross-encoder** (RET-3) | `app/retrieval/reranker.py`, ligado em `retriever.retrieve` | **encanamento pronto, `RERANKER_ENABLED=false`** — supera `RET-1`/`RET-2`/`RET-4` no caminho ativo; virar `true` está travado na suíte de fidelidade (ver [`eval/future_feature/cross-encoder.md`](eval/future_feature/cross-encoder.md) §6) |
| **Busca híbrida** (BM25 + vetor) | `app/retrieval/retriever.py` | eixo de recall, ortogonal ao reranker — entra entre a busca e o corte |
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
  web responde; quem precisa distinguir lê `Answer.origem`). **Ressalva medida
  em produção**: com o embedding atual, `score_top` cai numa faixa estreita
  (0.84–0.88) esteja a base cobrindo o tema ou não, então o limiar de 0.35 hoje
  nunca descarta um chunk — quem decide base-vs-web é o veto do LLM, não este
  número (ver
  [`eval/analise-telemetria-2026-08-26.md`](eval/analise-telemetria-2026-08-26.md) §3).
  Essa faixa comprimida é o que o **reranker cross-encoder** (RET-3) existe para
  quebrar: o encanamento já está no código, desligado (`RERANKER_ENABLED=false`),
  e quando ligar o corte passa a ser `RERANKER_THRESHOLD` numa escala real. Até
  lá, o `0.85` é rede contra lixo óbvio, não classificador — ver
  [`eval/future_feature/cross-encoder.md`](eval/future_feature/cross-encoder.md).
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
- **Pré-crawl da allowlist em vez de só busca ao vivo** (KB-3): raspar o
  DuckDuckGo a cada pergunta custa ~15s e estoura rate limit. `scripts/crawl.py`
  indexa as páginas oficiais no pgvector (com `assunto` da fonte, não `"web"`,
  para o filtro do retrieval continuar valendo), e a busca ao vivo fica só como
  rede para o que ainda não foi crawlado. O conteúdo crawlado responde como
  `origem="base"` — perde-se o sinal de lacuna daquela página, o que é a decisão
  deliberada de que ela pertence à base.
- **Allowlist do portal por caminho, não por site/subdomínio** (KB-2): o portal
  institucional inteiro trazia vestibular, avaliação institucional e landing
  pages de campanha respondendo com confiança aparente sobre assunto que não é
  do agente. Cada caminho em `path_prefixes` é uma seção conferida à mão.
- **Caminho da web ao vivo sem cache**: a `_cache_key` depende de ids de chunk,
  que não existem para resultado de web, e conteúdo externo muda sem aviso
  enquanto a tabela `resposta_cache` não tem TTL. É o caminho raro (o pré-crawl
  cobre o comum); cachear a camada de coleta depois, por conjunto de URLs e com
  expiração.
- **Passos explícitos no `responder.py`** em vez de uma chain fechada: dá para
  inspecionar o contexto recuperado (`--debug`) antes da chamada ao LLM.
- **Cache por conjunto de chunks, não pelo texto da pergunta**: duas perguntas
  parafraseadas que recuperam o mesmo topo do retrieval caem na mesma chave, e
  reingerir um arquivo alterado muda os ids recuperados e invalida a chave
  sozinho — sem tabela de invalidação nem TTL manual. Guardado no mesmo
  Postgres da ingestão, sem infra nova.

## Próximos passos sugeridos

Com o pipeline já validado ponta a ponta (ver acima) e datasets de eval
rodando de verdade contra o agente, o gargalo deixou de ser "falta corpus" e
passou a ser **calibração, vazamento de guardrail e cota de API** — é o que as
rodadas de 2026-08-26 e 2026-08-27
([`-26`](eval/analise-telemetria-2026-08-26.md),
[`-27`](eval/analise-telemetria-2026-08-27.md)) mapearam, em ordem de impacto:

| # | Ação | Onde | Status |
|---|---|---|---|
| 1 | Reranker cross-encoder (RET-3) é a direção escolhida para o limiar comprimido do E5 — encanamento pronto (`app/retrieval/reranker.py`), `RERANKER_ENABLED=false`; virar `true` travado na suíte de fidelidade (T-1) + A/B | `RERANKER_*`, [`cross-encoder.md`](eval/future_feature/cross-encoder.md) | ⏳ desligado — `RET-4` (`alta_confianca`) já removido; `score_top_bruto` instrumentado |
| 2 | Fechar o vazamento do veto: recusa em prosa do LLM sem o marcador esperado passa como resposta válida | `prompts.eh_insuficiente` | ⏳ pendente — prompt endurecido em 2026-08-27, mas ainda probabilístico |
| 3 | Pré-aquecer o modelo de embeddings no boot (evita ~40s no primeiro request do processo) | `app/api/app.py` | ⏳ pendente |
| 4 | Guardrail de entrada (injeção / segredo / exploit → `encaminhado`) | `app/agent/guardrail.py` | ✅ aplicado 2026-08-27 |
| 5 | Corte do 413: cada fonte de contexto limitada a `PROMPT_CONTEXT_ITEM_MAX_CHARS`; 413 do provider cai para o próximo | `responder._format_context`, `providers/base.py` | ✅ aplicado 2026-08-27 |
| 6 | Rodada de calibração com `-c` (cache limpo), `-m` (modelo fixo) e `--timeout`, comparando item a item em N=3 | `scripts/eval_run.py` | ✅ flags aplicadas — ver [Avaliação de qualidade (eval)](#avaliação-de-qualidade-eval) |
| 7 | Corrigir termo ambíguo "bolsa" na triagem | `ENCAMINHAMENTOS` | ✅ aplicado |
| 8 | Enxugar a `WEB_ALLOWLIST` para caminhos curados; `FonteWeb.path_prefix` → tupla `path_prefixes` | `app/core/config.py` | ✅ aplicado 2026-08-28 (KB-2) |
| 9 | Crawler da allowlist para o pgvector (`scripts/crawl.py`) — busca ao vivo vira rede | `scripts/crawl.py`, `pipeline.ingest_documents` | 🔧 crawler pronto; falta 1ª execução em prod + cron semanal (KB-3) |

O backlog completo, priorizado e com histórico, está em
[`eval/backlog-problemas.md`](eval/backlog-problemas.md).

Depois dos pendentes acima, os próximos são os de sempre: rodar
`python -m scripts.lacunas` e `python -m scripts.crawl` semanalmente (lacuna
amarela recorrente → conferir e adicionar o `path_prefix`), ampliar o dataset de
eval (hoje 25 perguntas) com perguntas reais de aluno, e fixar a dimensão do
embedding + criar índice HNSW quando o corpus crescer além de ~50k chunks.
