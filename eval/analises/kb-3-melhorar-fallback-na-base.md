# KB-3 — como melhorar o fallback trazendo a resposta para a base

**Data:** 2026-08-28
**Contexto:** backlog KB-3 — o fallback web custa ~50x o caminho da base
(~15s vs ~300ms). A ação registrada era "priorizar indexação das lacunas
amarelas". Esta análise detalha o *como*.

**Decisão (2026-08-28):** meio-termo — **pré-crawl** dos domínios da allowlist
para o pgvector + **manter a busca ao vivo** só como último recurso quando o
índice (agora incluindo o conteúdo crawlado) não cobre.

**Status:** crawler implementado em `scripts/crawl.py` (2026-08-28). Falta: 1ª
execução contra o pgvector de produção e agendar o re-crawl semanal (`/schedule`
ou cron). O `web_fallback` ao vivo segue ligado.

---

## 1. O que o fallback realmente custa hoje

Por request que cai em `_responder_pela_web`:

| etapa | custo |
|---|---|
| `_coletar` — 1..3 queries `site:` no `ddgs` (raspa HTML, sem API oficial) | `WEB_SEARCH_TIMEOUT=8s`, 2ª rodada até 16s; rate limit / `No results found` após ~25s |
| `_relevantes` — embeddings locais dos snippets | CPU local, sem API |
| `llm.invoke` com `ANSWER_PROMPT_WEB` | 1 chamada paga (a base também paga) |

O delta de custo vs. a base é **quase todo a raspagem do DuckDuckGo**. E o
resultado é pior: `grounded=False`, citação é uma URL que pode mudar, sem cache.

## 2. Solução: pré-crawl + fallback ao vivo como rede

### 2.1. Pré-crawl dos domínios da allowlist para o pgvector (principal)

Em vez de baixar PDF à mão (a ação original do backlog), um crawler sobe o
conteúdo das páginas curadas para o índice. O `web_fallback` ao vivo passa a
ser exceção rara — o que mantém o `ddgs` abaixo do rate limit.

**Por que agora é viável:** o KB-2 enxugou a allowlist para um conjunto pequeno
e estável (`/calendario/`, `/secretaria-geral/`, `/biblioteca/` do portal
`puc-campinas.edu.br`). Sem vestibular / notícia / LP de campanha, o que
sobra é conteúdo que muda devagar — bom para cachear no índice.

**Infra já preparada:**
- [`pipeline._enrich`](../../app/ingestion/pipeline.py) grava `source_type` /
  `source_uri` "para conviver com scraping mais tarde";
- o [registry de loaders](../../app/ingestion/loaders/registry.py) tem o exemplo
  `register("http", WebBaseLoader)` no docstring.

**Esboço:**
1. `scripts/crawl.py` — por `FonteWeb` da allowlist:
   - descobre URLs pelo `sitemap.xml` (o portal é WordPress, tem; não sair
     seguindo link a esmo);
   - filtra pelos `path_prefixes` da entrada;
   - fetch + extração do conteúdo principal (o `WebBaseLoader` do LangChain
     serve; site é HTML estático, sem JS);
   - `Document(metadata={source_type: "web", source_uri: <url>,
     source_path: <url>, assunto: fonte.assunto})`.
2. Passa pelo **mesmo** `pipeline` (chunk → embed → store).
   `delete_by_source(<url>)` antes, para re-crawl substituir sem duplicar.
3. Cron semanal de re-crawl (`/schedule`) — cobre página nova e correção de
   calendário dentro de uma semana.

**Efeitos:**
- conteúdo crawlado é recuperado por `retrieve` junto com os PDFs → resposta
  `~300ms`, `grounded=True`, cacheável pela `_cache_key` de sempre;
- `source_type="web"` separa de PDF oficial para ops (re-crawl, e citar a URL em
  vez de um nome de arquivo);
- perde o sinal de lacuna (`scripts.lacunas`) para esse conteúdo — aceitável,
  foi decisão deliberada de que aquilo pertence à base.

**Escopo do crawler:** só o domínio da PUC. O Canvas
(`community.instructure.com/en/kb/`) fica de fora — os guias **já estão
indexados como PDF** (`Canvas_Student_Guide.pdf`), e há ToS/robots a conferir.

### 2.2. Busca ao vivo (`web_fallback`) — mantida como último recurso

Sem mudança no fluxo do [`_responder`](../../app/agent/responder.py): se
`retrieve` (agora com o conteúdo crawlado) não achar nada **ou** o LLM vetar os
chunks, ainda tenta `_responder_pela_web`; se essa também falhar, encaminha para
a secretaria — que já é o comportamento atual.

A diferença é a frequência: com o crawl absorvendo o caso comum, o `ddgs` é
chamado poucas vezes por dia (página nova ainda não crawlada, pergunta cuja
resposta está fora dos `path_prefixes`), longe do rate limit.

`WEB_FALLBACK_ENABLED` continua como kill switch — não desligar por padrão
enquanto o crawl não estiver rodando estável.

### 2.3. Ajustes menores (só se o fallback ao vivo continuar relevante)

- **Cache da camada de coleta web** — chave `sha256(pergunta_normalizada +
  assunto)`, valor = lista `{url, titulo, snippet}` pós-allowlist, TTL 6–24h.
  Corta a raspagem na repetição antes de alguém crawlar aquela página.
- **`WEB_SEARCH_TIMEOUT` 8s → 5s** e **cortar a 2ª rodada** (com a allowlist do
  KB-2 a 1ª já é quase tudo) — medir na telemetria antes de cortar recall.

## 3. Ordem sugerida

1. **2.1** — `scripts/crawl.py` + cron. É o que resolve o KB-3.
2. **2.3 (cache)** — se ainda houver volume no fallback ao vivo depois do crawl.
3. **2.3 (timeouts)** — só depois de medir.

## 4. O que NÃO fazer

- Crawlar conteúdo volátil (notícia, prazo do semestre): vira resposta
  desatualizada com `grounded=True`, pior que 15s de busca. O KB-2 já os
  excluiu da allowlist — manter assim.
- Aumentar `top_k` / baixar `relevance_threshold` para forçar a base a
  responder: reintroduz o lixo que o RET-1 quer cortar.
- Desligar `WEB_FALLBACK_ENABLED` antes do crawl estar estável.
