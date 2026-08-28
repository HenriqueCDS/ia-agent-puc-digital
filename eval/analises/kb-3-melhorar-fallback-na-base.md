# KB-3 — como melhorar o fallback trazendo a resposta para a base

**Data:** 2026-08-28
**Contexto:** backlog KB-3 — o fallback web custa ~50x o caminho da base
(~15s vs ~300ms). A ação registrada era "priorizar indexação das lacunas
amarelas". Esta análise detalha o *como*.

---

## 1. O que o fallback realmente custa hoje

Por request que cai no fallback (`_responder_pela_web`):

| etapa | custo |
|---|---|
| `_coletar` — 1..3 queries `site:` em paralelo no `ddgs` | orçamento `WEB_SEARCH_TIMEOUT=8s`, 2ª rodada até 16s |
| `_relevantes` — `embed_query` + `embed_documents` dos snippets | CPU local, sem API |
| `llm.invoke` com `ANSWER_PROMPT_WEB` | 1 chamada paga ao Gemini |
| 2ª rodada quando há assunto e a 1ª volta vazia | dobra o tempo de busca |

Ou seja: latência dominada pela raspagem do DuckDuckGo (sem API oficial, sujeita
a rate limit e a `No results found` após ~25s), e uma chamada de LLM que o
caminho da base também paga — **o delta de custo é quase todo a busca**.

E o resultado é pior: `grounded=False`, citação é uma URL que pode mudar sem
aviso, sem cache (a `_cache_key` depende de ids de chunk).

## 2. Três frentes de melhoria

### 2.1. Fechar a lacuna na fonte — indexar o que a web respondeu (maior valor)

O relatório `scripts.lacunas` já classifica cada tema:

- **`coberta pela web` (amarelo)** — a resposta existe numa página oficial, a URL
  já está identificada na telemetria (`origem_por_hash`). É a lacuna mais barata:
  não precisa descobrir a fonte, só baixá-la para `data/raw/<assunto>/` e rodar
  `python -m scripts.ingest <assunto> --apenas-novos`.
- **`sem resposta` (vermelho)** — nem a web cobriu. Exige conteúdo novo (a
  secretaria precisa escrever), então não é candidato a automação.

**Proposta concreta:** um passo semanal no procedimento de calibração —

```
python -m scripts.lacunas --json > eval/analises/lacunas-<data>.json
```

e, para cada item `coberta pela web` com `perguntas_distintas >= 2`:
1. abrir a URL citada (agora restrita a páginas curadas — ver KB-2);
2. se for conteúdo estável (calendário, regulamento, procedimento), salvar
   como PDF/MD em `data/raw/puc-digital/` e reingerir;
3. se for volátil (notícia, prazo do semestre), **não** indexar — deixar no
   fallback é o comportamento certo.

Isso converte cada tema recorrente de ~15s + chamada web para ~300ms de RAG
cacheável, uma vez só.

### 2.2. Cachear o resultado da busca web

Hoje `_responder_pela_web` não cacheia "de propósito" (ids de chunk não existem
para web, conteúdo externo muda). Mas a maior parte do custo é a **busca**, não
a síntese. Um cache só da camada de coleta —

- chave: `sha256(pergunta_normalizada + assunto)`;
- valor: a lista de `{url, titulo, snippet}` que passou pela allowlist;
- TTL curto (6–24h), numa tabela própria ou reusando `resposta_cache` com
  coluna de expiração.

Ganho: a 2ª vez que o mesmo tema amarelo é perguntado (antes de alguém indexar)
pula os 8–16s de raspagem e paga só o LLM. Baixo risco: o conteúdo continua
sendo revalidado contra a allowlist na leitura, e o TTL limita o stale.

Também resolve parte de INF-3 (oscilação `web`↔`nenhuma` da busca externa entre
rodadas de avaliação): com a busca cacheada dentro da bateria, o item para de
trocar de desfecho sozinho.

### 2.3. Reduzir a latência da busca quando ela acontece

- **Cortar a 2ª rodada por padrão.** `buscar_na_web` faz uma 2ª rodada com a
  allowlist inteira quando a 1ª (restrita ao assunto) volta vazia. Com a
  allowlist enxugada em KB-2 (2 hosts de `puc-digital`, 1 de `canvas`), a 1ª
  rodada já é quase a allowlist toda — a 2ª quase nunca acrescenta e dobra o
  pior caso. Medir quantas respostas de fato vêm só da 2ª rodada (telemetria) e,
  se for <5%, removê-la ou colocá-la atrás de uma flag.
- **`WEB_SEARCH_TIMEOUT` de 8s → 5s.** O p50 de uma busca bem-sucedida no `ddgs`
  fica abaixo de 3s; 8s é quase todo espera de backend que já falhou. Um teto
  menor troca alguns recalls de cauda por uma degradação mais rápida para a
  secretaria — que neste caminho já é o desfecho aceitável.

## 3. Ordem sugerida

1. **2.1** (indexar lacunas amarelas) — sem código, maior retorno, começa já.
2. **2.2** (cache da coleta web) — ~1 tabela + 1 função; corta o custo do que
   ainda cair no fallback e estabiliza a avaliação.
3. **2.3** (timeouts / 2ª rodada) — só depois de medir, para não cortar recall
   às cegas.

## 4. O que NÃO fazer

- Indexar página volátil (notícia, prazo do semestre) só para ganhar latência:
  vira resposta desatualizada com `grounded=True`, que é pior que 15s de busca.
- Aumentar `top_k` / baixar `relevance_threshold` para "forçar" a base a
  responder e evitar o fallback: reintroduz o lixo que RET-1 quer cortar.
