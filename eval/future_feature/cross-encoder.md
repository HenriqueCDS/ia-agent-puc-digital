# Future feature — Reranker cross-encoder no retrieval

Status: **implementado, DESLIGADO** (`RERANKER_ENABLED=false`, o padrão) desde
2026-09-01. O encanamento existe (`app/retrieval/reranker.py`, ligado em
`retriever.retrieve`, config e telemetria); virar `true` por padrão continua
travado nos pré-requisitos da §6 — falta a suíte de fidelidade (T-1) para saber
se o rerank melhora ou piora, sobretudo nos documentos em inglês (ver §5). Este
documento agora descreve o que foi construído e o que falta medir.

**2026-09-02** — `RET-7` (piso de E5 no 1º estágio) e `RET-8` (`rerank` que
falha não derruba o `/ask`) resolvidos em `retriever.retrieve`. Ver §4.1.

Relacionado: `RET-1`, `RET-2`, `RET-3`, `RET-4`, `RET-7`, `RET-8` em
`eval/backlog-problemas.md`; análise em
`eval/analises/analise-telemetria-2026-08-28.md` §2.

---

## 1. O problema

A busca atual é **bi-encoder** (o modelo `intfloat/multilingual-e5-base`):
pergunta e chunk viram vetores **separadamente** e a relevância é o cosseno
entre eles. O vetor de cada chunk é pré-calculado na ingestão; na hora da
pergunta só se compara vetor com vetor.

O efeito medido (análise de 26 e 28-08-2026):

- **`RELEVANCE_THRESHOLD` é inerte.** O E5 pontua **~0.82 para qualquer par de
  textos em português**. Uma pergunta 100% fora de domínio (Q4, fotossíntese)
  recupera 5 chunks a 0.82 — o mesmo patamar de um acerto real. Não existe
  valor de limiar absoluto sobre `score_top` que separe "a base cobre" de "a
  base não cobre". (`RET-1` subiu o limiar para 0.85 como **rede contra lixo
  óbvio**, não como classificador — é paliativo.)

- **O score absoluto não separa as classes.** Q2 e Q3 (uma coberta, outra não)
  têm `score_top` a 0.004 de distância. Sobreposição total entre os dois grupos.

- **A margem relativa (`score_top − score_min`) TEM sinal, mas não é
  classificador sozinha.** Quando a base cobre o tema, o chunk do topo se
  destaca dos outros quatro; quando não cobre, os cinco chegam quase empatados.
  Mas margem baixa **não** prova "não cobre" (Q15, Q25 são respostas legítimas
  com margem ~0.01), e o caso `Canvas_Student_Guide.pdf` tem margem quase zero
  por repetição de conteúdo, não por falta de cobertura (`RET-6`).
  `RET-2` já expõe essa margem como coluna derivada (`margem_relativa`) em
  `scripts.eval_run` e `scripts.eval_report` — como **feature**, nunca como `if`.

**Raiz de tudo:** o bi-encoder mede "os dois textos falam do mesmo assunto
amplo", não "este trecho responde a esta pergunta". É o teto de qualidade da
abordagem, não um problema de calibração.

---

## 2. A solução possível

### 2.1. Em termos simples

O bi-encoder compara os textos pela **etiqueta** (o resumo numérico calculado à
parte). Rápido, mas nunca "abre o livro".

O **cross-encoder** cola a pergunta e o chunk num único input
(`[pergunta] [SEP] [chunk]`) e roda o transformer inteiro sobre os dois ao mesmo
tempo, devolvendo **um** número: "quão bem este trecho responde esta pergunta".
A atenção cruza as duas metades token a token — "prazo" na pergunta consegue
olhar para "até 23h59 do dia da entrega" no chunk.

Exemplo — pergunta *"até quando posso entregar a atividade atrasada?"*:

| | Bi-encoder (hoje) | Cross-encoder |
|---|---|---|
| trecho sobre prazo de entrega | 0.82 | **alto** |
| trecho "como enviar uma atividade" | 0.82 | **baixo** (não fala de prazo) |

### 2.2. Por que não trocar o bi-encoder por ele

O cross-encoder **não pode ser pré-calculado**: precisa da pergunta, que só
existe em runtime. São *N* forward passes por pergunta (um por candidato).
Rodar isso sobre a base inteira a cada pergunta é inviável.

### 2.3. Reranking em dois estágios (o desenho)

1. **Recall** — o bi-encoder (E5, já existe) busca um conjunto AMPLO de
   candidatos (~20–50), barato.
2. **Precisão** — o cross-encoder reordena **só esses candidatos** e devolve os
   `top_k` melhores, caro mas sobre poucos itens.

O barato faz a peneira grossa; o caro faz o acabamento onde já sobrou pouca
coisa.

### 2.4. O que isso destrava

- Score com **faixa dinâmica real** → um `RELEVANCE_THRESHOLD` absoluto volta a
  significar algo, e o lixo óbvio (Q4) cai para um score de fato baixo.
- Ordenação correta dentro do top-k → o chunk que responde sobe para a posição
  1 (melhora a ordem do prompt em `_format_context` e desarma o artefato de
  `is_exact_match` — `RET-4`).
- Permite aposentar hacks: `EXACT_MATCH_THRESHOLD`, a margem-como-feature, talvez
  o ramo `alta_confianca` inteiro.
- Cross-encoder multilíngue lida melhor com pergunta PT × documento EN (o Canvas
  guide).

---

## 3. Trade-offs

### 3.1. Latência (o custo mais concreto)

Sem GPU, tudo em CPU. Ponto de partida: retrieval ~300ms; resposta total de um
`/ask` sem cache ~2–5s (a chamada ao LLM domina).

Custo adicional **por pergunta**, em CPU, em função do modelo e de quantos
candidatos são reordenados (é ~linear no nº de pares):

| Modelo | Rerank 10 | Rerank 20 | Rerank 50 |
|---|---|---|---|
| MiniLM-L6 (~80MB) | ~30–60ms | ~60–120ms | ~150–300ms |
| **mMiniLMv2-L12 multilíngue (~120MB)** | ~60–130ms | ~130–260ms | ~300–650ms |
| bge-reranker-base (~1.1GB) | ~200–400ms | ~400–800ms | ~1–2s |
| bge-reranker-v2-m3 (~2.3GB) | ~500ms–1s | ~1–2s | ~2.5–5s |

Também pesa o **tamanho dos trechos**: a maioria dos chunks tem ~100 tokens
(rápido), mas página densa de PDF é bem maior (cross-encoder trunca em ~512
tokens e fica mais lento).

Onde isso cai:

- **Retrieval isolado:** ~300ms → ~450–600ms (opção realista: mMiniLMv2-L12,
  rerank ~20). ~2x.
- **Resposta total percebida:** ~2–5s → ~2.2–5.3s. **+3–8%** relativo — o LLM
  continua dominando.
- **Caminho web** (~15s): +150–300ms é ruído.
- **Cache hit:** zero impacto (nem toca no retrieval).

### 3.2. Memória e boot

- Mais um modelo local: ~80–130MB (MiniLM) ou 1–2.3GB (bge). O E5-base já ocupa
  ~1GB — numa VM modesta isso aperta.
- Carrega uma vez, no warm-up (`vector_store.aquecer`, ver INF-8): boot de
  ~40–65s → +5–15s com um MiniLM, mais com um bge. Custo de startup, não
  por-request.

### 3.3. Risco de qualidade — pode ficar PIOR

- Reranker mal calibrado **rebaixa o chunk certo**. Troca-se um sinal medíocre
  *conhecido* (E5 ~0.82) por um *desconhecido*.
- Cross-encoders são treinados em MS MARCO / mmarco — domínio diferente de
  suporte acadêmico PT-BR com documentos em inglês. Transferência não é
  garantida; precisa de calibração e eval própria.
- Força **re-tunar o `RELEVANCE_THRESHOLD`** de novo, na escala nova.

### 3.4. Complexidade

- `sentence-transformers` já está no projeto (via `HuggingFaceEmbeddings`) —
  não é pacote novo, mas é novo download, novo cache offline (padrão de
  `providers/embeddings._modelo_local`), novo dublê nos testes, novo ponto de
  falha no boot.
- Arquiteturalmente é esperado: o `PONTO DE EXTENSÃO` de `retriever.retrieve` já
  reserva o lugar. Baixa surpresa.

### 3.5. O que o cross-encoder NÃO resolve

- **`RET-6`** (Canvas guide → 5 chunks quase idênticos): pontua os 5 parecidos
  igualmente alto → margem ~0 continua. Dedup é fix separado — já feito na
  ingestão (`chunker.deduplicar_similares`); o cross-encoder não substitui isso.
- **Teto de recall:** se o E5 não trouxe o chunk certo nos primeiros ~50,
  reordenar não inventa. Isso é **busca híbrida** (BM25 + vetor), eixo
  diferente — e é o que resolveria "trancamento" não casar "trancar" (`TRI-1`).
- Custo de embedding na ingestão: inalterado.

### 3.6. Alternativas mais baratas a pesar antes

| Alternativa | Ataca | Custo |
|---|---|---|
| Só o bump de `RELEVANCE_THRESHOLD` (`RET-1`, feito) | lixo óbvio | zero |
| Busca híbrida (BM25 + vetor) | recall / termo exato | médio, sem modelo |
| Dedup na ingestão (`RET-6`) | margem inflada por repetição | baixo |
| LLM como reranker | — | rejeitado: filosofia do projeto (não usar LLM pro que um modelo barato faz), latência e custo piores |

---

## 4. Arquitetura

### 4.1. Onde encaixa

No `PONTO DE EXTENSÃO` de `app/retrieval/retriever.retrieve`, **entre a busca
vetorial e o corte por limiar** — o agente (`responder.py`), o guardrail e a
borda HTTP não mudam. `retrieve` continua devolvendo `list[RetrievedChunk]`
ordenada, só que agora os scores são do cross-encoder quando ele está ligado.

```
retrieve(query):
    if not settings.reranker_enabled:                       # caminho de hoje
        candidatos = store.similarity_search(k=top_k)
        return [c for c in candidatos if c.score >= RELEVANCE_THRESHOLD][:top_k]

    candidatos = store.similarity_search(k=RERANKER_CANDIDATES)   # recall amplo
    candidatos = [c for c in candidatos if c.score >= RELEVANCE_THRESHOLD]  # RET-7: piso de E5
    try:
        rerankeados = reranker.rerank(query.text, candidatos)     # reescreve .score
    except Exception:                                             # RET-8
        return candidatos[:top_k]                                 # cai p/ ordem bi-encoder
    return [c for c in rerankeados if c.score >= RERANKER_THRESHOLD][:top_k]
```

Quando o reranker está **desligado**, o caminho é exatamente o de hoje
(`k = top_k`, limiar sobre o score do E5). Kill switch idêntico ao de
`triagem_enabled` / `guardrail_enabled`.

**RET-7 — o piso de E5 no 1º estágio (feito 2026-09-02).** O corte final com o
reranker é `RERANKER_THRESHOLD` (default `0.0`, a calibrar). Sem um piso no 1º
estágio, "reranker ligado + threshold não calibrado" deixaria passar lixo fora
de domínio que o bi-encoder cortava (Q4 fotossíntese, 0.82 no E5 < 0.85). Então
`RELEVANCE_THRESHOLD` é aplicado aos candidatos **antes** do rerank — um chunk
abaixo dele nunca chega ao cross-encoder, e o resultado respeita o piso reranker
ou não. `RERANKER_THRESHOLD` continua sendo um corte ADICIONAL na escala nova,
calibrado na T-1.

**RET-8 — o rerank não derruba o `/ask` (feito 2026-09-02).** O cross-encoder
pode estourar memória na VM (§4.3 admite que "aperta junto do E5"). `rerank`
dentro de `try/except` em `retrieve`: qualquer exceção (inclusive `ModuleNotFoundError`
se `sentence_transformers` faltar, ou `MemoryError`) → WARNING + cai para a ordem
bi-encoder já filtrada pelo piso de E5, sem marcar `reranker_aplicado`. Mesmo
espírito da `ProviderChain`. Timeout duro NÃO foi adicionado: não dá para
cancelar uma chamada síncrona de torch, e um timeout por thread deixaria a
inferência rodando (pior sob carga) — a mitigação para lentidão é o modelo
pequeno (§4.3) + a telemetria `ms_rerank` (INF-9) no A/B.

### 4.2. Módulo novo — `app/retrieval/reranker.py`

Espelha `app/providers/embeddings.py`:

- `get_reranker()` — `@lru_cache(maxsize=1)`, devolve o `CrossEncoder` do
  processo. Carregamento **offline-first** (tenta `local_files_only=True`,
  cai para download só se não houver cache), igual `embeddings._modelo_local`.
- `rerank(pergunta: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]`
  — função pura: monta os pares `(pergunta, chunk.page_content)`, chama
  `CrossEncoder.predict` (batched), devolve nova lista ordenada por score
  decrescente com `RetrievedChunk.score` reescrito. Não conhece o pgvector nem
  o agente.

Normalização do score: `CrossEncoder` pode devolver logit cru (faixa aberta) —
aplicar `sigmoid` para trazer a 0..1 e manter o contrato de `RetrievedChunk`
("relevância 0..1"). Documentar que a ESCALA é outra (não comparável com score
de E5 histórico na telemetria).

### 4.3. Configuração (`app/core/config.py` + `.env.example`)

| Setting | Default | Para quê |
|---|---|---|
| `RERANKER_ENABLED` | `false` | Kill switch. Entra ligado só depois da calibração. |
| `RERANKER_MODEL` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | Multilíngue, ~120MB, ~130–260ms p/ rerank 20 em CPU. |
| `RERANKER_CANDIDATES` | `30` | `k` do 1º estágio (bi-encoder). Recall amplo p/ o reranker ter o que reordenar. |
| `RERANKER_THRESHOLD` | `0.0` (a calibrar) | Corte ADICIONAL sobre o score do cross-encoder. **Separado** de `RELEVANCE_THRESHOLD` porque a escala é outra. RET-7: `RELEVANCE_THRESHOLD` continua sendo o PISO do 1º estágio mesmo com o reranker ligado. |

`top_k` (nº final de chunks no prompt) não muda.

### 4.4. Warm-up (INF-8)

`app/db/vector_store.aquecer()` passa a chamar `get_reranker()` junto de
`get_vector_store()` — o custo de carregar o modelo cai no boot (API) e antes da
1ª pergunta (`scripts.eval_run`), nunca na 1ª request do aluno. `aquecer()`
continua sendo o ponto único.

### 4.5. Telemetria (`app/core/telemetry.py`)

Campos novos no `Registro` (JSONB, sem migração):

- `reranker_aplicado: bool | None` — se o 2º estágio rodou nesta pergunta.
- `score_top_bruto: float | None` — o `score_top` do **bi-encoder**, antes do
  rerank. Mantém a série histórica comparável e permite medir "o rerank mudou a
  ordem?" depois.

`score_top` / `score_min` / `score_mean` passam a refletir o score **final**
(pós-rerank quando aplicável). `margem_relativa` (RET-2) continua derivada dos
mesmos campos — passa a medir a dispersão na escala do cross-encoder.

### 4.6. Testes (sem infra, como o resto da suíte)

- Dublê `FakeCrossEncoder` (à la `FakeEmbeddings` em `test_web_fallback.py`):
  `predict(pares)` devolve scores fixos/derivados de uma regra do teste.
- `test_reranker.py` — a função pura: `rerank` reordena por score, reescreve
  `.score`, aplica sigmoid, tolera lista vazia.
- `test_retrieval.py` — com `RERANKER_ENABLED=true` + dublê: `retrieve` busca
  `RERANKER_CANDIDATES`, filtra pelo piso de E5 (RET-7), reordena, corta por
  `RERANKER_THRESHOLD`, devolve `top_k`. `rerank` que levanta exceção → cai p/ a
  ordem bi-encoder sem propagar (RET-8). Com `false`: caminho de hoje intacto.
- `test_vector_store.py` / `test_api.py` — `aquecer()` chama `get_reranker()`.

### 4.7. Caminho web (`app/agent/web_fallback.py`)

Fica **de fora nesta primeira versão.** O fallback web já custa ~15s e tem
filtro próprio (`web_relevance_threshold` sobre embeddings de snippet). Rerankear
lá é integração a mais para ganho marginal — reavaliar depois que o caminho da
base estiver calibrado.

---

## 5. O que muda para o resto do backlog

Análise de sequenciamento (2026-09-01). O reranker não é um item isolado: ele
**colapsa** parte do bloco de calibração de retrieval. Isto é mapa de
dependência, **não** move a recomendação da §6 — só diz o que parar de tunar.

| Item | Efeito do reranker (quando ligado) | O que fazer agora |
|---|---|---|
| **`RET-4`** — ramo `alta_confianca` | **torna obsoleto.** Com ranking real, "2 fontes fortes no topo" deixa de ser proxy de confiança — vira artefato de corpus repetitivo (era exatamente o caso da Q23, `Canvas_Student_Guide.pdf`). | **removido junto deste PR** (decisão de 2026-09-01): ver o checklist abaixo. |
| **`RET-2`** — `margem_relativa` | **absorvido.** A dispersão do top-k era o candidato a sinal de cobertura; o score do cross-encoder mede cobertura direto. Vira, no máximo, uma *feature* de entrada — nunca um `if`. | mantém a coluna instrumentada (`eval_run`/`eval_report`, custo zero); **não** construir roteamento sobre ela. |
| **`RET-1`** — `RELEVANCE_THRESHOLD=0.85` | **superado no caminho ativo.** `RERANKER_THRESHOLD` numa escala real (sigmoid do cross-encoder) faz o corte anti-lixo que o 0.85 fazia por aproximação. | 0.85 continua valendo com `RERANKER_ENABLED=false`; **não subir mais**. |
| README §Próximos passos #1 (recalibrar limiar p/ faixa 0.84–0.88) | **moot.** A faixa comprimida do E5 é o problema que o reranker existe para resolver. | tirar da lista de ações abertas. |
| **`RET-6`** — dedup na ingestão | **NÃO subsume.** O cross-encoder pontua 5 quase-cópias igualmente alto; a margem continua ~0 por repetição. Dedup é ingestion-side (`chunker.deduplicar_similares`). | segue como está. |
| Busca híbrida / BM25 (`TRI-1`, `"trancamento"`×`"trancar"`) | **NÃO subsume.** Eixo de *recall*: reordenar não inventa o chunk que o E5 não trouxe nos primeiros `RERANKER_CANDIDATES`. | investimento de retrieval **independente**, pode andar em paralelo — e o reranker *depende* de o recall estar bom. |

### Checklist `RET-4` — o que sai com o ramo `alta_confianca`

Feito neste PR (o ramo não fica atrás do kill switch — vale já no merge, com o
reranker ainda desligado):

- [x] `retriever.is_exact_match` — função removida.
- [x] `settings.exact_match_threshold` / `EXACT_MATCH_THRESHOLD` no `.env.example`.
- [x] `prompts.SYSTEM_ALTA_CONFIANCA` / `prompts.ANSWER_PROMPT_ALTA_CONFIANCA`.
- [x] o bloco `alta_confianca = is_exact_match(chunks)` e o parâmetro
  `alta_confianca` de `_tentar_base` em `responder.py`.
- [x] o termo `alta_confianca` na `_cache_key` — **invalida as chaves de cache
  existentes** (a `resposta_cache` não tem TTL; a próxima pergunta re-gera, sem
  ação manual).
- [x] `Registro.alta_confianca` na telemetria e a coluna em `scripts.eval_run`.

### Interação com o conteúdo crawlado (KB-3)

As páginas da allowlist pré-indexadas por `scripts/crawl.py` entram na coleção
com `source_type="web"` mas respondem como `origem="base"` — então, com o
reranker ligado, elas são rerankeadas junto dos PDFs. É o comportamento
desejado (uma página oficial e um chunk de PDF competem pelo mesmo topo pela
relevância real à pergunta), só vale registrar que o 2º estágio não distingue
as duas origens.

---

## 6. Pré-requisitos antes de LIGAR (`RERANKER_ENABLED=true`)

O encanamento está no código (§4), desligado.

**Segurança já resolvida (2026-09-02):** os dois furos que faziam "reranker
ligado" ser *pior* que hoje foram fechados — `RET-7` (piso de E5 no 1º estágio,
o corte anti-lixo não depende de `RERANKER_THRESHOLD` estar calibrado) e `RET-8`
(`rerank` que estoura não derruba o `/ask`, cai para o bi-encoder). Então ligar
agora, ainda sem calibrar, não regride o comportamento fora de domínio — só não
traz o ganho do rerank. A calibração abaixo é para o GANHO, não mais uma
pré-condição de segurança.

Para virar `true` por padrão:

1. **Suíte de fidelidade** (`T-1` / `VET-4`) — semente mínima focada nos
   documentos EN em `eval/fidelidade/canvas-en.jsonc` (ver
   `eval/fidelidade/README.md`): 8–12 perguntas PT contra os guias Canvas, com
   resposta-referência. Sem ela não há como medir se o rerank melhorou ou
   piorou justamente onde ele foi pedido — os arquivos em inglês.
2. **A/B `RERANKER_ENABLED` false × true** sobre a semente + os grupos de
   `eval/perguntas/perguntas.jsonc`, comparando item a item: `origem`,
   `score_top_bruto` vs `score_top` pós-rerank, se o chunk que responde subiu
   para a posição 1, fidelidade da resposta, `ms_retrieve`.
3. **Calibrar `RERANKER_THRESHOLD`** na escala nova (sigmoid, não comparável ao
   ~0.82 do E5): o valor que derruba pergunta fora de domínio (Q4) para um score
   de fato baixo sem cortar resposta PT legítima de margem baixa (Q15/Q25).
4. Confirmar o orçamento de latência com quem opera: +130–260ms no caminho da
   base (~2× no retrieval isolado, ~+3–8% no total percebido).

**Portão:** virar `true` só se o A/B mostrar os arquivos EN melhores (chunk
certo ranqueado acima, fidelidade igual ou melhor) e nenhuma regressão no PT.
Registrar a rodada em `eval/analises/` na convenção dos `analise-telemetria-*`.
