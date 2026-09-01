# Suíte de fidelidade — semente EN (T-1 / VET-4)

Semente mínima da suíte de fidelidade, **focada nos documentos em inglês**
(`data/raw/canvas/Canvas_Student_Guide.pdf` e `Canvas_Instructor_Guide.pdf`) —
que é o caso que o reranker cross-encoder (RET-3) foi pedido para melhorar e,
por isso, também o de maior risco de regressão (ver
`eval/future_feature/cross-encoder.md` §3.3 e §5).

## Estado

**Incompleta.** O arquivo `canvas-en.jsonc` traz as perguntas e o schema, mas
`resposta_referencia` está `null` na maioria dos itens — precisa ser preenchida
**lendo os guias**, não de memória. Sem a referência, o A/B abaixo mede só
roteamento (`origem`) e ordem de chunk, não fidelidade da resposta.

O LLM-judge (comparar `resposta` × `resposta_referencia`) é a parte que falta de
T-1 / VET-4 e não está aqui — por enquanto a conferência é manual, pelo campo
`criterio`, que o `scripts.eval_run` já destaca no resumo.

## Como rodar o A/B `RERANKER_ENABLED` false × true

Pré: reingestão feita, Postgres de pé, um provedor de LLM com cota.

```bash
# 1. baseline — reranker desligado (o padrão)
RERANKER_ENABLED=false python -m scripts.eval_run \
  eval/fidelidade/canvas-en.jsonc -m gemini:gemini-3.6-flash -c \
  -o eval/resultados/fidelidade-canvas-OFF.json

# 2. com o reranker
RERANKER_ENABLED=true python -m scripts.eval_run \
  eval/fidelidade/canvas-en.jsonc -m gemini:gemini-3.6-flash -c \
  -o eval/resultados/fidelidade-canvas-ON.json
```

No Windows/PowerShell: `$env:RERANKER_ENABLED="true"; python -m scripts.eval_run ...`

## O que comparar (item a item, OFF × ON)

| Sinal | Onde no resultado | Espera-se com o reranker |
|---|---|---|
| Roteamento | `origem_obtida`, `acertou` | igual ou melhor (nada que era `base` virar `nenhuma`) |
| Score de E5 antes do rerank | `score_top_bruto` (só no ON) | referência — comparável com o `score_top` histórico |
| Score final | `score_top` | escala nova (sigmoid); é o que `RERANKER_THRESHOLD` corta |
| Ordem do chunk que responde | `fontes_citadas`, conferência manual | o chunk certo sobe para a posição 1 |
| Fidelidade | `resposta` × `resposta_referencia` / `criterio` | igual ou melhor; **nenhuma** piora |
| Latência | `ms_total`, telemetria `ms_retrieve` | +130–260ms no retrieval, aceitável |

## Portão para virar `RERANKER_ENABLED=true` por padrão

Só se o ON mostrar os itens EN **melhores** (chunk certo ranqueado acima,
fidelidade igual ou melhor) e **nenhuma regressão** nos grupos PT de
`eval/perguntas/perguntas.jsonc` (rodar os dois datasets). Registrar a rodada em
`eval/analises/analise-reranker-<data>.md` na convenção dos `analise-telemetria-*`,
e calibrar `RERANKER_THRESHOLD` na escala nova antes de fechar.
