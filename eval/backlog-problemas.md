# Backlog de problemas — agente PUC Digital

Backlog único derivado das análises de telemetria de **26, 27 e 28-08-2026**
(`eval/analise-telemetria-2026-08-2{6,7,8}.md`).

**Como usar:** cada problema é um checkbox. Ao resolver um item junto com o
Claude, marcar `[x]`, preencher a data e o commit/PR na coluna `Resolvido em`, e
mover uma linha para o CHANGELOG no fim do arquivo se quiser histórico curto.

Legenda de prioridade: 🔴 bloqueia calibração / risco de dado · 🟠 corrige
comportamento · 🟡 melhoria / dívida técnica.
Legenda de status herdado das análises: ✅ já aplicado · 🔧 parcial · ⏳ pendente.

---

## 1. Infra e método de avaliação

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | INF-1 | 🔴 | Cota de tier gratuito estoura no meio da rodada (Gemini 20/dia, HF crédito esgotado, Groq TPM/413) → cada rodada mistura 2–3 modelos | chaves / `scripts.eval_run -m` | **Decisão: aceito para a fase de demo/teste.** A chave paga entra depois da fase de testes; até lá a mistura de modelos numa rodada é ruído conhecido e a mitigação é `-m` + re-rodar itens marcados `provedores_indisponivel` (ver INF-6) | ⏳ | 2026-08-29 (aceito) |
| [x] | INF-2 | 🔴 | `cache_hit` mascara o pipeline — rodada mede o cache, não retrieval/LLM | `scripts.clear_cache --yes` / `eval_run -c` | **Decisão: `-c` continua opcional.** A rodada é feita SEMPRE em dobro de propósito — a 1ª popula, a 2ª confirma que o cache está de pé. Forçar `-c` apagaria justamente o sinal que a 2ª rodada existe para ver | 🔧 | 2026-08-29 (decisão) |
| [x] | INF-3 | 🔴 | 12% dos itens trocam de desfecho sozinhos (oscilação `web`↔`nenhuma` da busca externa) | método | **Resolvido por KB-3:** `scripts/crawl.py` indexa o conteúdo da allowlist na base (`source_type="web"`), então essas perguntas passam a bater na base de forma determinística em vez de dependerem da busca web ao vivo. `web_fallback` ao vivo vira último recurso | 🔧 | 2026-08-29 (via KB-3) |
| [x] | INF-4 | 🟠 | Prompt de ~11.6k tokens (página inteira de PDF + body web) → HTTP 413 derruba a rodada | `responder._format_context`, `PROMPT_CONTEXT_ITEM_MAX_CHARS` | **Já aplicado (2026-08-27), 2 camadas independentes:** (1) `responder._conteudo_limitado` corta CADA fonte de contexto em `PROMPT_CONTEXT_ITEM_MAX_CHARS` (6000 ≈ 1500 tokens) antes de montar o prompt — o `PyPDFLoader` entrega 1 página inteira como "chunk" e uma página densa passa de 8k chars; 5 delas estouram o teto de tokens/requisição de tier gratuito; (2) se mesmo assim estourar, o 413 é classificado como indisponibilidade em `providers/base._STATUS_DE_CONTEXTO` e a cadeia cai p/ o próximo provedor (o teto é do MODELO/tier, não do pedido — o mesmo prompt cabe no Gemini). `INF-6` estendeu isso para `Cancelled`/499 | ✅ | 2026-08-27 |
| [x] | INF-5 | 🟡 | `--timeout` inteiro (30s) queimado tentando Gemini antes do fallback | `eval_run --timeout` | Feito: `scripts.eval_run --timeout` agora tem **default 20s** (não herda os 30 do `.env`) e imprime o valor em uso; `--timeout 30` volta ao valor de produção quando se quer medir com ele | ✅ | 2026-08-29 |
| [x] | INF-6 | 🟠 | 2 perguntas falham a rodada por erro de infra (`Cancelled: 499` gRPC Gemini) — Q14/Q16 sem teste | `_rodar` try/except, `providers/base.py`, `scripts/eval_run.py` | Feito: (1) `Cancelled`/HTTP 499 entram na classificação de indisponibilidade (`base._NOMES_DE_FALLBACK` + `_STATUS_DE_FALLBACK`) → a cadeia tenta o próximo provedor em vez de o erro cru subir; (2) quando TODOS os provedores caem, a linha da rodada fica com `origem_obtida="provedores_indisponivel"` (distinta de `None` de bug nosso) e o resumo lista essas perguntas para re-rodar isoladas | ✅ | 2026-08-29 |
| [x] | INF-7 | 🟡 | Colunas de `eval_run` (`n_chunks`/`score_top`) x telemetria medem coisas diferentes e confundem auditoria | `scripts/eval_run.py`, `scripts/eval_report.py` | Feito: `eval_run` já separava `chunks_recuperados`/`score_top` (retrieval) de `fontes_resposta`/`score_fonte_top` (fontes da resposta) desde 7aa2568; `eval_report` agora emite `chunks_recuperados` (era `n_chunks`) para falar a mesma língua. O campo na telemetria/JSONB segue `n_chunks` de propósito — renomear quebraria as linhas dentro da janela de retenção | ✅ | 2026-08-29 |
| [x] | INF-8 | 🟡 | Cold start de 40s no 1º request (carga do modelo de embeddings) | `app/api` boot, `scripts/eval_run.py` | Feito: `vector_store.aquecer()` — ponto ÚNICO de warm-up (carrega embeddings + `SELECT 1`). A API já chamava no `_lifespan`; agora `scripts.eval_run` também chama antes da 1ª pergunta, então o cold start não cai mais no item 1 nem infla o `ms_retrieve` dele | ✅ | 2026-08-29 |

## 2. Calibração de limiar / retrieval (Bloco A)

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | RET-1 | 🔴 | `RELEVANCE_THRESHOLD=0.35` inerte — pergunta 100% fora de domínio recupera 5 chunks a 0.82 (Q4 fotossíntese) | `config.RELEVANCE_THRESHOLD` | Feito: `relevance_threshold` de 0.35 → **0.85** no `.env.example` e no default de `config.py` (os dois alinhados). Rede contra lixo óbvio, não classificador. ⚠️ 0.85 raspa o menor acerto real registrado (0.8451) — se uma rodada mostrar acerto perdido entre 0.80–0.845, baixar p/ 0.80/0.78. Não exige reingestão | ✅ | 2026-08-30 |
| [x] | RET-2 | 🔴 | Score absoluto não separa "base cobre" de "base não cobre" — Q2 vs Q3 `score_top` difere 0.004; sobreposição total | `retriever`, `scripts/eval_run.py`, `scripts/eval_report.py`, `telemetry_store` | Feito: `margem_relativa` (`score_top − score_min`) virou coluna DERIVADA do arquivo de rodada (`eval_run`) e do relatório de telemetria (`eval_report` + `--detalhe`), com `score_min`/`score_mean` agora expostos por `origem_por_hash`. É FEATURE p/ acumular N rodadas e tirar mediana por item — **não entra em nenhum `if`**. Bruto continua o que a telemetria grava; a margem é sempre recalculada | ✅ | 2026-08-30 |
| [~] | RET-3 | 🟠 | Sem reranker — o `PONTO DE EXTENSÃO` de `retriever.retrieve` está vazio; é o que resolve RET-2 | `retriever.retrieve` | **Encanamento implementado, DESLIGADO** (2026-09-01): `app/retrieval/reranker.py` (cross-encoder local, offline-first, `@lru_cache`, função pura `rerank` + sigmoid), ligado em `retrieve` atrás de `RERANKER_ENABLED` (default `false`); config `RERANKER_MODEL/CANDIDATES/THRESHOLD`; telemetria `reranker_aplicado`/`score_top_bruto`; warm-up em `aquecer()` só quando ligado; testes com `FakeCrossEncoder`. **Falta p/ ligar**: suíte de fidelidade EN (T-1, semente em `eval/fidelidade/`), A/B `false`×`true`, calibrar `RERANKER_THRESHOLD`. Ver `eval/future_feature/cross-encoder.md` §5–§6 | 🔧 | encanamento 2026-09-01 |
| [x] | RET-4 | 🟡 | `alta_confianca`/`is_exact_match` só dispara por artefato do corpus (doc gigante em inglês repetitivo), não por confiança real (Q23) | `retriever.py`, `EXACT_MATCH_THRESHOLD` | **Ramo `alta_confianca` REMOVIDO** junto do encanamento do RET-3 (2026-09-01): `is_exact_match`, `EXACT_MATCH_THRESHOLD`, `SYSTEM_ALTA_CONFIANCA`/`ANSWER_PROMPT_ALTA_CONFIANCA`, o parâmetro em `_tentar_base`/`_cache_key` (invalida cache existente) e `Registro.alta_confianca`. Com ranking cross-encoder real, "2 fontes fortes no topo" deixa de ser proxy de confiança. Checklist em `cross-encoder.md` §5. (Tuning intermediário 0.90→0.87 em 2026-08-30 ficou obsoleto.) | ✅ | 2026-09-01 |
| [x] | RET-5 | 🟡 | `CHUNK_SIZE` quase inerte — `PyPDFLoader` entrega 1 Document por página; granularidade real ≤254 tokens | `config.CHUNK_SIZE`, `scripts/chunk_stats.py` | Feito: `scripts/chunk_stats.py` mede a distribuição real (5371 páginas: mediana 403 chars / 103 tokens, p75 695, p90 1073). `CHUNK_SIZE` 1000 → **700** (≈ p75 — mantém ~75% das páginas inteiras, quebra só o quartil denso; índice +18%; p99 do chunk ~185 tokens « 512 do E5). **Exige reingestão.** Voltar a 1000 se a eval piorar | ✅ | 2026-08-30 |
| [x] | RET-6 | 🟡 | `Canvas_Student_Guide.pdf` (1108 pág.) devolve 5 chunks quase idênticos → margem ~0 por repetição (Q11/Q23) | `ingestion/chunker.py`, `config.INGEST_DEDUP_SIMILARIDADE` | Feito: `chunker.deduplicar_similares` na ingestão — descarta chunk com Jaccard (shingles de 4 palavras) ≥ `INGEST_DEDUP_SIMILARIDADE` (0.9) contra outro já mantido do mesmo lote. Pega a quase-cópia que o `content_hash` (exato) não pega. 1ª ocorrência vence. Só na ingestão, fora do caminho de resposta. **Não** substitui dedup por top-k nem reranking — reduz a repetição na fonte | ✅ | 2026-08-30 |

## 3. Veto / fidelidade / prompt (Bloco B)

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | VET-1 | 🔴 | Recusa em prosa vaza como `origem=web` — `web_insuficiente`/`veto_escapou` `null`, telemetria conta sucesso e `lacunas` rotula "coberto (amarelo)" (Q7; 26: 8/11/15; 27: Q15/Q24/Q9r2) | `prompts.eh_insuficiente`, `SYSTEM_WEB` | Feito: 3ª camada em `eh_insuficiente` — `_RE_RECUSA_PROSA` casa a recusa em prosa (PT+EN) **presa ao vocabulário de meta-resposta** (informação/trecho/contexto/base), nunca a negação solta ("não há prazo fixo" não casa), e só na janela inicial (`_JANELA_RECUSA_PROSA=160` — a recusa real é front-loaded). Reusa os 3 pontos de veto que já compartilham a função: base→web, web→secretaria, rede de `answer()`; `web_insuficiente`/`veto_escapou` deixam de vir `null`. **Léxico primeiro** (decisão): o classificador não-léxico fica como escalada se a telemetria mostrar forma nova furando | ✅ | 2026-08-31 |
| [x] | VET-2 | 🟠 | Modelo recusa **em inglês** e é classificado `base`/`grounded` em vez de recusa (Q17) | `responder`, `prompts.SYSTEM` | Feito: `prompts.eh_recusa_de_compliance` (`_RE_RECUSA_COMPLIANCE`) casa a recusa de **compliance** (o modelo se nega a OBEDECER, ≠ falta de contexto do VET-1) **por estrutura, PT+EN** — modal de negação + verbo de ação recusada ("não posso cumprir/atender esse pedido", "I can't comply/assist with that"), + apelo a diretriz. Preso a verbo de AÇÃO → não colide com "não posso fornecer essa informação". Rede de segurança de `answer()` converte no **mesmo desfecho do guardrail** (`origem="encaminhado"`, `CONTATO_PADRAO` PT-BR, assunto "fora de escopo"/`guardrail`), sem tentar web. Nova coluna `telemetry.recusa_modelo`. Fix real do Q17 é o guardrail pegar a paráfrase (**TRI-4**) — isto é a defesa em profundidade | ✅ | 2026-08-31 |
| [x] | VET-3 | 🟠 | Sem `max_tokens` em nenhum provider — Q10 gerou 1450 tokens, Q25 1729, antes do veto | `app/providers/` | Feito: `settings.llm_max_tokens` (**1400** — folga sobre os ~800–1000 da análise p/ não cortar procedimento longo) traduzido em cada família de SDK: `max_output_tokens` no Gemini, `max_tokens` por chamada no OpenAI-compat. Aplicado nas 4 fábricas de `chain.py`. Teste garante que o teto chega em TODA a cadeia, não só no 1º elo | ✅ | 2026-08-31 |
| [ ] | VET-4 | 🟠 | Sem suíte de fidelidade automatizada — só 3 citações conferidas à mão | `tests/` | 15–20 perguntas com resposta-referência do PDF, LLM-judge ou similaridade | ⏳ | |
| [x] | VET-5 | 🟡 | Fixar como regressão: alucinação por complacência **não** ocorreu (Q7 premissa falsa, Q10 número inventado) | `tests/` | Feito (= T-6): regressão em 2 níveis — `test_prompts.py` trava a detecção (`eh_insuficiente` pega as respostas reais de Q7/Q10 de 28-08); `test_responder.py` cobre o caminho completo (base com chunks plausíveis + modelo recusa → não vaza como `grounded`, cai p/ secretaria) | ✅ | 2026-08-31 |

## 4. LGPD / PII na entrada (Bloco C)

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | PII-1 | 🔴 | CPF/RA/e-mail vão crus ao provider LLM (EUA) — `pii.mascarar` só age nos campos persistidos, `query.text` vai cru p/ `_format_context`/`invoke`/`buscar_na_web` (Q11, Q13; 27: Q5) | `responder._responder`, `app/core/pii.py` | Feito: `responder._sem_pii` mascara `query.text` no topo de `_responder` (antes de guardrail/triagem/retrieval, funil único; `dataclasses.replace`, objeto original preservado p/ a telemetria). Detecção (`registro.pii` + WARNING) segue sobre o texto original em `telemetry.registrar`. Mascarar, não recusar | ✅ | 2026-08-31 |
| [x] | PII-2 | 🔴 | "senha"/"password" não é categoria de `pii.py` — credencial `'Aluno@2026'` seguiu p/ o Gemini (Q12; 27: Q5) | `app/core/pii.py` | Feito: categoria `senha` — `_SENHA` (palavra + conector `:`/`=`/`é` ou aspas + valor); só conta com valor de cara de credencial (dígito/símbolo/aspas), nunca "esqueci minha senha". Mascarada 1º na ordem (antes do e-mail). Entra no mesmo caminho do PII-1 | ✅ | 2026-08-31 |

## 5. Triagem / guardrail (Blocos E e F)

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | TRI-1 | 🟠 | `"trancar"` não casa `"trancamento"` de `ENCAMINHAMENTOS` — léxico preso à forma nominal (Q13; 27: Q14) | `config.ENCAMINHAMENTOS` | Feito: `"trancar"` (verbo) somado a `"trancamento"` (nominal) nos `termos` de `academico`. As duas formas explícitas em vez de normalizar por radical `tranc` — sem risco de casar palavra não relacionada; não são substring uma da outra. Testes em `test_triagem.py` | ✅ | 2026-08-31 |
| [x] | TRI-2 | 🟠 | `"bolsa"` em entrada com termos inequívocos manda aluno de iniciação científica p/ financeiro/cobrança (26 item 16) | `config.ENCAMINHAMENTOS` | Entrada própria com `excecoes` | ✅ | 2026-08-26 |
| [ ] | TRI-3 | 🟡 | Guardrail estruturalmente cego a injeção indireta — só lê a pergunta, nunca o CONTEXTO recuperado (Q18) | `guardrail.py`, sanitização de contexto | Defesa no prompt + sanitização do contexto; teste da parte-2 (LLM01 Indirect) | ⏳ | |
| [ ] | TRI-4 | 🟡 | Guardrail léxico frágil a paráfrase / outro idioma — 4/15 ataques da parte-2 passam (DoS por repetição, footprinting `pypdf2`/`.bin`, PII por ID de aluno) | `guardrail._PADROES` | Calibrar léxico após 1ª rodada da parte-2; 2ª camada por embedding | ⏳ | |
| [ ] | TRI-5 | 🟡 | Guardrail alimenta `scripts.lacunas` — "DROP TABLE"/"chave de API" viram pauta de indexação | `telemetry_store.origem_por_hash` | Filtrar `assunto_origem="guardrail"` | ⏳ | |
| [x] | TRI-6 | 🟡 | `web_fallback.buscar_na_web` não chama o guardrail — payload de ataque sai p/ DuckDuckGo quando `GUARDRAIL_ENABLED=false` (Q15/Q24/Q9r2) | `app/agent/web_fallback.py` | Feito: `web_fallback.abuso_bloqueado` reusa `guardrail.deve_encaminhar` e roda no topo de `buscar_na_web`, ao lado de `assunto_bloqueado`. **Não** respeita `settings.guardrail_enabled` de propósito — é a última barreira quando o guardrail de entrada está desligado. Mesmo desenho do `assunto_bloqueado` (que duplica a triagem). Teste em `test_web_fallback.py` | ✅ | 2026-08-31 |

## 6. Base de conhecimento / busca web (transversais)

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | KB-1 | 🔴 | `modelos_resposta_chunks.xlsx` indexado — vira `fonte_citada`, "contamina" o tom p/ "e-mail de atendimento" | `data/raw/email_modelos/` | **Comportamento aceito e mantido**: são modelos REAIS de atendimento extraídos do e-mail, fonte curada e pré-chunkada (loader dedicado `xlsx_modelos_resposta.py`, pasta `email_modelos/`). O tom de e-mail de atendimento é desejado. Follow-up opcional: citação amigável em vez do nome do arquivo | ✅ | 2026-08-28 |
| [x] | KB-2 | 🟠 | `WEB_ALLOWLIST` amplo demais — portal `puc-campinas.edu.br` inteiro + todos os subdomínios (vestibular, avaliação institucional, LPs, PDFs soltos) alucinava sobre assunto fora do agente (Q7 nota mínima citou vestibular) | `config.WEB_ALLOWLIST`, `web_fallback.fonte_permitida` | Feito: `learn.microsoft.com` já não estava; portal reduzido a uma lista de `path_prefixes` curados em `puc-campinas.edu.br` (`/calendario/`, `/secretaria-geral/`, `/biblioteca/`), sem `subdominios`. `FonteWeb.path_prefix` virou tupla `path_prefixes` | ✅ | 2026-08-28 |
| [~] | KB-3 | 🟡 | Fallback web custa ~50x o caminho da base (~15s vs ~300ms); raspagem do `ddgs` estoura rate limit | `scripts/crawl.py`, `pipeline.ingest_documents`, `web_fallback` | **Implementado**: `scripts/crawl.py` lê o sitemap de cada `FonteWeb`, filtra pelos `path_prefixes`, extrai o conteúdo (bs4, sem menu/rodapé) e indexa via `pipeline.ingest_documents` (`source_type="web"`, `categoria="web"`, `assunto` da fonte). `web_fallback` ao vivo mantido como último recurso. **Falta**: 1ª execução em produção + agendar re-crawl semanal. Plano/detalhe: `eval/analises/kb-3-melhorar-fallback-na-base.md` | 🔧 | crawler 2026-08-28 |
| [x] | KB-4 | 🟠 | Ingestão ignora em silêncio arquivo sem loader (`report.ignorados`, sem erro) | `tests/test_ingestao.py` | Invertido conforme KB-1: o teste **não reprova** fonte — garante que todo arquivo em `data/raw/` tem loader registrado (senão ficaria de fora sem aviso). `.gitkeep` etc. na lista de ignorados | ✅ | 2026-08-28 |

## 7. Dataset / gabarito

| Feito | ID | Prio | Problema | Onde | Ação | Herdado | Resolvido em |
|---|---|---|---|---|---|---|---|
| [x] | DS-1 | 🔴 | Categoria `bloqueado` do dataset nunca casa — 10 ataques `acertou:false` por construção | dataset + guardrail | `bloqueado`→`encaminhado`; guardrail roteia abuso p/ `encaminhado` | ✅ | 2026-08-27 |
| [ ] | DS-2 | 🟡 | Gabarito Q8 (28) errado — `AulasAoVivo_v2-2.pdf` é só tutorial; agente recusou e acertou | `perguntas.jsonc` grupo `teste3` | Q8 → `encaminhado` | ⏳ | |
| [x] | DS-3 | 🟡 | Gabarito item 17 (26) — app Canvas celular: base respondeu bem; `web`→`base` | `perguntas.jsonc` grupo `teste` | Corrigido | ✅ | 2026-08-26 |
| [ ] | DS-4 | 🟡 | Gabarito Q12 (27) — "altero meu e-mail": base respondeu certo; `encaminhado`→`base` | `perguntas.jsonc` grupo `owasp-1` | Revisar | ⏳ | |
| [ ] | DS-5 | 🟡 | Gabaritos Q8/Q16/Q25 (27) responderam `web` de fonte oficial em vez de `encaminhado` | dataset | Reavaliar intenção do gabarito | ⏳ | |
| [x] | DS-6 | 🟠 | `GROQ_MODEL` com prefixo `groq:` duplicado → 404 na cadeia | `.env` | Corrigido | ✅ | 2026-08-26 |

## 8. Testes que faltam antes de "pronto"

| Feito | ID | Teste | Cobre | Resolvido em |
|---|---|---|---|---|
| [ ] | T-1 | Suíte de fidelidade automatizada (15–20 perguntas, resposta-referência do PDF, LLM-judge) | VET-4 | |
| [x] | T-2 | `test_ingestao` — todo arquivo em `data/raw/` tem loader (não reprova fonte; ver KB-1/KB-4) | KB-1, KB-4 | 2026-08-28 |
| [ ] | T-3 | Teste ponta a ponta com pgvector + E5 reais no caminho feliz | RET-* | |
| [x] | T-4 | `test_web_fallback` — allowlist rejeita paths fora dos curados do portal (vestibular, avaliação institucional, LP) | KB-2 | 2026-08-28 |
| [x] | T-5 | `test_responder` — recusa do modelo (qualquer idioma) → `origem` de recusa + texto PT | VET-1, VET-2 | 2026-08-31 |
| [x] | T-6 | Regressão do veto de contexto — Q7 (premissa falsa) e Q10 (número inventado) continuam recusando | VET-5 | 2026-08-31 |
| [ ] | T-7 | Re-rodar Q14/Q16 (falharam por infra em 28) | INF-6 | |
| [ ] | T-8 | Parte-2 OWASP (injeção indireta, DoS por repetição, footprinting, PII por ID) | TRI-3, TRI-4, VET-3 | |

---

## Progresso

| Grupo | Feitos / Total |
|---|---|
| 1. Infra e método | 8 / 8 |
| 2. Retrieval / limiar | 5 / 6 |
| 3. Veto / fidelidade | 4 / 5 |
| 4. LGPD / PII | 2 / 2 |
| 5. Triagem / guardrail | 3 / 6 |
| 6. Base de conhecimento / web | 3 / 4 (KB-3 parcial) |
| 7. Dataset / gabarito | 3 / 6 |
| 8. Testes | 4 / 8 |
| **Total** | **32 / 45** |

## Changelog

- **2026-09-01** — RET-3 (encanamento) + RET-4 (remoção): `app/retrieval/reranker.py` — cross-encoder local em 2 estágios, offline-first, `@lru_cache`, função pura `rerank` (monta pares, `CrossEncoder.predict`, sigmoid, reordena, preserva o score de E5 em `RetrievedChunk.score_bruto`). Ligado em `retriever.retrieve` atrás de `RERANKER_ENABLED` (default `false` — nada carrega, import local de `sentence_transformers`). Config `RERANKER_MODEL` (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`), `RERANKER_CANDIDATES=30`, `RERANKER_THRESHOLD=0.0`. Telemetria: `reranker_aplicado`, `score_top_bruto` (remove `alta_confianca`). `aquecer()` carrega o reranker só quando ligado. **RET-4**: ramo `alta_confianca` inteiro removido (`is_exact_match`, `EXACT_MATCH_THRESHOLD`, `SYSTEM_ALTA_CONFIANCA`/`ANSWER_PROMPT_ALTA_CONFIANCA`, param de `_tentar_base`/`_cache_key` — invalida cache — e `Registro.alta_confianca`). Testes: `test_reranker.py` (`FakeCrossEncoder`), `test_retrieval.py` (on/off), warm-up. Docs: `cross-encoder.md` §5–§6, README, `arquitetura-*.md`. **Segue DESLIGADO** até T-1 + A/B + calibração de `RERANKER_THRESHOLD`.
- **2026-08-31** — infra eval: os 5 datasets (`perguntas_teste{,2,3}.json`, `perguntas-owasp-2026-parte-{1,2}.json`) fundidos em `eval/perguntas/perguntas.jsonc` (125 itens, JSONC — blocos comentados com `//`, removidos na carga). Cada item ganhou `grupo` (`teste`/`teste2`/`teste3`/`owasp-1`/`owasp-2`); campo `nota` → `criterio`. `eval_run` e `eval_report` toleram os comentários; `eval_run --intervalo/-i` roda um trecho (`1-6`, `27-50`, `27 a 50`, `26-`, `7`) e o resumo quebra o acerto por grupo. `test_guardrail.py` filtra `owasp-1` pelo `grupo`.
- **2026-08-31** — TRI-6: `web_fallback.abuso_bloqueado` (reusa `guardrail.deve_encaminhar`) roda no topo de `buscar_na_web`, ao lado de `assunto_bloqueado`; não respeita `GUARDRAIL_ENABLED` de propósito (última barreira antes da rede quando o guardrail de entrada está off). Teste em `test_web_fallback.py`.
- **2026-08-31** — TRI-1: `"trancar"` somado a `"trancamento"` nos `termos` de `academico` em `config.ENCAMINHAMENTOS`. Testes em `test_triagem.py`.
- **2026-08-31** — PII-1 / PII-2: `responder._sem_pii` mascara `query.text` (CPF/RA/e-mail/telefone/senha) no topo de `_responder`, antes de qualquer saída p/ o provedor de LLM (EUA) ou a busca web. Nova categoria `senha` em `pii.py` (`_SENHA` — palavra + conector/aspas + valor de cara de credencial). Detecção segue sobre o texto original. Testes em `test_pii.py` e `test_responder.py`. README §Privacidade e LGPD atualizado.
- **2026-08-31** — VET-3 / T-6 (via VET-5): `settings.llm_max_tokens=1400` — teto de saída aplicado em toda a cadeia (`max_output_tokens` no Gemini, `max_tokens` por chamada no OpenAI-compat; 4 fábricas de `chain.py`). `LLM_MAX_TOKENS` no `.env.example`. Regressão do veto de contexto Q7/Q10 em `test_prompts.py` (detecção) e `test_responder.py` (caminho completo). Teste de cadeia em `test_providers.py`.
- **2026-08-31** — T-5: `test_responder.py`/`test_telemetry.py`/`test_prompts.py` cobrem recusa do modelo em qualquer idioma → encaminhamento + texto PT (via VET-1 e VET-2).
- **2026-08-31** — VET-2: `prompts.eh_recusa_de_compliance` (`_RE_RECUSA_COMPLIANCE`) — recusa de compliance PT+EN por estrutura; rede de `answer()` converte em `origem="encaminhado"` (desfecho do guardrail), sem web. Nova coluna `telemetry.recusa_modelo`. Testes em `test_prompts.py`, `test_responder.py`, `test_telemetry.py`.
- **2026-08-31** — VET-1: `eh_insuficiente` ganha 3ª camada — `_RE_RECUSA_PROSA` (recusa em prosa PT+EN, presa ao vocabulário de meta-resposta, casada só na janela inicial `_JANELA_RECUSA_PROSA=160`). Fecha o vazamento de recusa em prosa como `origem="base"/"web"`; roteamento base→web→secretaria inalterado. Testes em `test_prompts.py` (frases reais de 26/27-08 + guardas de falso-positivo), `test_responder.py` e `test_telemetry.py`.
- **2026-08-30** — RET-4: `EXACT_MATCH_THRESHOLD` 0.90 → 0.87.
- **2026-08-30** — RET-5: `scripts/chunk_stats.py` (mede a distribuição real de chunk/página); `CHUNK_SIZE` 1000 → 700 (≈ p75 das páginas). Exige reingestão.
- **2026-08-30** — RET-6: `chunker.deduplicar_similares` — dedup de chunks quase idênticos na ingestão (Jaccard de shingles ≥ `INGEST_DEDUP_SIMILARIDADE`).
- **2026-08-30** — RET-1: `RELEVANCE_THRESHOLD` 0.35 → 0.85 (`.env.example` + default de `config.py`).
- **2026-08-30** — RET-2: `margem_relativa` (`score_top − score_min`) como coluna derivada em `eval_run` e `eval_report`; `score_min`/`score_mean` expostos por `telemetry_store.origem_por_hash`. Feature, não `if`.
- **2026-08-30** — RET-3: desenho da feature documentado em `eval/future_feature/cross-encoder.md` (não implementado).
- **2026-08-29** — INF-8: `vector_store.aquecer()` vira o ponto único de warm-up (embeddings + `SELECT 1`); `scripts.eval_run` passa a chamá-lo antes da 1ª pergunta.
- **2026-08-29** — INF-7: `scripts.eval_report` emite `chunks_recuperados` (era `n_chunks`), alinhado a `eval_run`.
- **2026-08-29** — INF-5: `scripts.eval_run --timeout` com default 20s.
- **2026-08-29** — INF-4: confirmado já aplicado (corte por fonte em `_conteudo_limitado` + 413 → próximo provedor).
- **2026-08-29** — INF-6: `Cancelled`/HTTP 499 do Gemini classificados como indisponibilidade (`providers/base.py`) → cadeia cai para o próximo provedor; `scripts/eval_run.py` marca `origem_obtida="provedores_indisponivel"` quando todos caem e lista essas perguntas no resumo. Testes em `test_providers.py` e `test_eval_run.py`.
- **2026-08-29** — INF-3: fechado por KB-3 (pré-crawl da allowlist na base torna o desfecho determinístico).
- **2026-08-29** — INF-1 / INF-2: decisões registradas — chave paga fica para depois da demo; `-c` segue opcional (rodada em dobro proposital verifica o cache).
- **2026-08-28** — KB-1: xlsx de modelos de e-mail confirmado como fonte curada intencional (`data/raw/email_modelos/`); comportamento mantido.
- **2026-08-28** — KB-2: `WEB_ALLOWLIST` do portal PUC reduzida a `path_prefixes` curados (`/calendario/`, `/secretaria-geral/`, `/biblioteca/`), sem `subdominios`; `FonteWeb.path_prefix` → tupla `path_prefixes`.
- **2026-08-28** — KB-3: `scripts/crawl.py` — pré-crawl da allowlist (sitemap → `path_prefixes` → bs4 → `pipeline.ingest_documents`, `source_type="web"`); `web_fallback` ao vivo vira último recurso. Falta rodar em produção + cron semanal. `eval/analises/kb-3-melhorar-fallback-na-base.md`.
- **2026-08-28** — KB-4 / T-2 / T-4: `tests/test_ingestao.py` (todo arquivo em `data/raw/` tem loader) e cobertura de allowlist restrita em `test_web_fallback.py`.
- **2026-08-27** — DS-1: categoria `bloqueado` consolidada em `encaminhado`; guardrail de entrada roteia abuso.
- **2026-08-27** — INF-4: corte por fonte (`PROMPT_CONTEXT_ITEM_MAX_CHARS`) + 413 cai p/ próximo provider.
- **2026-08-26** — TRI-2: `"bolsa"` movido para entrada própria com `excecoes`.
- **2026-08-26** — DS-3: gabarito item 17 corrigido (`web`→`base`).
- **2026-08-26** — DS-6: `GROQ_MODEL` sem prefixo duplicado.

---

## Notas de features futuras

- **RET-3 — reranker cross-encoder:** encanamento **implementado, DESLIGADO**
  (`RERANKER_ENABLED=false`) desde 2026-09-01. Desenho, o que foi construído,
  relação com o backlog (§5) e o que falta para ligar (§6) em
  [`eval/future_feature/cross-encoder.md`](future_feature/cross-encoder.md).
  Resumo: a busca bi-encoder (E5) mede "mesmo assunto amplo", não "responde a
  pergunta" — por isso o score fica ~0.82 pra tudo. O cross-encoder lê pergunta
  e chunk juntos e dá um score de relevância real, mas custa *N* forward passes
  por pergunta (CPU) e mais um modelo no boot. **Ligar** depende da suíte de
  fidelidade EN (T-1, semente em `eval/fidelidade/`), do A/B `false`×`true` e da
  calibração de `RERANKER_THRESHOLD`. Absorve RET-1/RET-2/RET-4 no caminho ativo.
