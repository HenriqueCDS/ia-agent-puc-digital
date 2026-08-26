# Análise de telemetria — rodada de avaliação 2026-08-26

Rodada: `scripts.eval_run` sobre `eval/perguntas_teste.json` (25 perguntas), canal `eval`.
Config vigente: `EMBEDDING_MODEL=intfloat/multilingual-e5-base`, `CHUNK_SIZE=1000`,
`CHUNK_OVERLAP=150`, `TOP_K=5`, `RELEVANCE_THRESHOLD=0.35`, `EXACT_MATCH_THRESHOLD=0.90`.

Dados brutos: `eval/telemetria-eval-2026-08-26.jsonl` (50 registros = 2 rodadas
de 25, extraídos da tabela `telemetria` com `canal='eval'`). A análise abaixo
detalha a rodada das 17:51; a das 16:46 é usada na §3.3 para medir ruído.

---

## 1. Resultado bruto

| Categoria | Acertos | Taxa |
|---|---|---|
| base | 8/9 | 89% |
| web | 5/8 | 62% |
| encaminhado | 8/8 | 100% |
| **geral** | **21/25** | **84%** |

**84% é um número inflado.** Ver a seção 4: duas das cinco "acertos" de web são
recusas escritas em prosa que passaram pelo veto e foram contadas como resposta.
A taxa de respostas que de fato servem ao aluno é ~72%.

---

## 2. Tabela por pergunta

| # | Pergunta (resumo) | Esper. | Obtido | score_top | n_chunks | cache | base_insuf | Quem respondeu | ms_total |
|---|---|---|---|---|---|---|---|---|---|
| 1 | enviar atividade Canvas | base | base | 0.8677 | 5 | hit | — | cache | 40922 |
| 2 | acessar disciplinas | base | base | 0.8778 | 5 | hit | — | cache | 509 |
| 3 | fórum avaliativo | base | base | 0.8587 | 5 | hit | — | cache | 307 |
| 4 | aula ao vivo | base | base | 0.8821 | 5 | hit | — | cache | 417 |
| 5 | netiqueta | base | base | 0.8754 | 5 | hit | — | cache | 409 |
| 6 | nota final | base | base | 0.8716 | 5 | hit | — | cache | 290 |
| 7 | avisos AVA | base | base | 0.8593 | 5 | hit | — | cache | 227 |
| 8 | estudos orientados | base | **web** | **0.8838** | 5 | hit | **sim** | HF/Llama-3.3-70B | 23659 |
| 9 | fórum docente | base | base | 0.8629 | 5 | hit | — | cache | 646 |
| 10 | resetar senha Canvas | web | web | **0.8473** | 5 | hit | sim | HF/Llama-3.3-70B | 13115 |
| 11 | calendário acadêmico | web | web | 0.8629 | 5 | hit | sim | OpenRouter/deepseek-v3.1 | 48029 |
| 12 | transferência externa | web | web | 0.8569 | 5 | hit | sim | HF/Llama-3.3-70B | 28366 |
| 13 | cursos graduação | web | web | 0.8674 | 5 | hit | sim | HF/Llama-3.3-70B | 16072 |
| 14 | suporte navegador | web | **nenhuma** | 0.8594 | 5 | hit | sim | — | 3160 |
| 15 | endereço secretaria | web | web | 0.8686 | 5 | hit | sim | HF/Llama-3.3-70B | 9818 |
| 16 | bolsas de iniciação científica | web | **encaminhado** | — | — | — | — | triagem | 1.3 |
| 17 | app Canvas celular | web | **base** | 0.8693 | 5 | hit | — | cache | 290 |
| 18–25 | triagem (financeiro/diplomas/acadêmico) | encaminhado | encaminhado | — | — | — | — | triagem | 0.1–0.7 |

---

## 3. O achado central: os dois limiares de calibração estão inertes

Esta é a conclusão mais importante da rodada, e ela **invalida a premissa do
exercício de calibração** como está formulado hoje.

Faixa de `score_top` observada nas 17 perguntas que chegaram ao retrieval:

```
mínimo   0.8473   (item 10)
máximo   0.8838   (item  8)
largura  0.0365
```

Contra isso, a configuração atual:

```
RELEVANCE_THRESHOLD    = 0.35   <-- 0.50 ABAIXO do menor score já visto
EXACT_MATCH_THRESHOLD  = 0.90   <-- 0.016 ACIMA do maior score já visto
```

**Os dois limiares estão fora da faixa útil, um de cada lado.** Consequências
visíveis no log, e as duas se confirmam em 100% dos casos:

- `n_chunks: 5` em **todas** as 17 — o corte por `relevance_threshold` nunca
  descartou um único chunk. O retriever devolve sempre o `top_k` inteiro.
- `alta_confianca: false` em **todas** as 17 — `is_exact_match` nunca disparou,
  então `ANSWER_PROMPT_ALTA_CONFIANCA` é código morto em produção.

Ou seja: **mexer em `RELEVANCE_THRESHOLD` hoje não muda absolutamente nada**,
em nenhuma direção, até que ele entre na casa dos 0.84–0.88.

### 3.1. Pior: o score não discrimina

Separando os scores por desfecho real:

```
responderam PELA BASE   (9 itens):  média 0.8694   [0.8587 .. 0.8821]
NÃO responderam pela base (7 itens): média 0.8663   [0.8473 .. 0.8838]
```

A diferença entre as médias é **0.003** e as faixas se sobrepõem quase por
inteiro. O caso extremo prova o ponto: o **maior score de toda a rodada**
(0.8838, item 8 "estudos orientados") é de uma pergunta que a base **não**
respondeu, e o item 3, com score menor (0.8587), respondeu bem.

Isso significa que **não existe valor de `RELEVANCE_THRESHOLD` capaz de separar
as duas classes.** Não é questão de calibrar melhor — o sinal não está lá. Hoje
quem decide base-vs-web é 100% o LLM (`base_insuficiente`), e o retrieval só
entrega os 5 chunks de sempre.

Causa provável: o E5 produz similaridade de base alta entre quaisquer dois
textos em português, comprimindo tudo numa faixa estreita no topo da escala.
É comportamento esperado do modelo, não bug — mas torna o limiar absoluto
inadequado como instrumento.

### 3.2. `CHUNK_SIZE` também é quase inerte

Já está documentado em `app/core/config.py:203`: o `PyPDFLoader` entrega um
`Document` por **página** e o splitter nunca junta páginas, então o chunk real
tem no máximo ~254 tokens — bem abaixo dos 1000 **caracteres** de `CHUNK_SIZE`.
Para o corpus atual (quase todo PDF), quem define a granularidade é a página.
**Calibrar `CHUNK_SIZE` para cima não terá efeito**; para baixo, terá.

---

## 3.3. O piso de ruído: 12% das perguntas não são determinísticas

A tabela `telemetria` guardava **duas** rodadas do mesmo dataset (16:46 e 17:51,
50 registros, 25 hashes distintos), com a **mesma configuração**. Comparando as
duas, três perguntas **trocaram de desfecho sozinhas**:

| Pergunta | Rodada 1 (16:46) | Rodada 2 (17:51) | score_top |
|---|---|---|---|
| estudos orientados (8) | `nenhuma` | `web` | 0.8838 (idêntico) |
| suporte navegador (14) | `web` | `nenhuma` | 0.8594 (idêntico) |
| endereço secretaria (15) | `nenhuma` | `web` | 0.8686 (idêntico) |

O detalhe decisivo: **`score_top` é idêntico até a 4ª casa nas duas rodadas**, e
`base_insuficiente: True` nas seis. Ou seja — **o retrieval é perfeitamente
determinístico; quem oscila é a busca externa** (o `No results found`
intermitente que aparece no log em `community.instructure.com` e
`puc-campinas.edu.br`).

Consequência para o exercício de calibração:

> **3 de 25 itens (12%) mudam de resultado sem que nada na config mude.**
> Qualquer diferença menor que ~12% entre duas rodadas é **ruído, não sinal**.

E um alerta sobre a leitura ingênua do total: as duas rodadas deram **84% as
duas** — mas por coincidência, porque os itens 14 e 15 trocaram em direções
opostas e se cancelaram. O agregado estável escondeu instabilidade item a item.
**Compare sempre item a item, nunca só o total.**

Mitigações, em ordem de custo: (a) rodar N=3 e usar a mediana por item;
(b) cachear os resultados da busca web durante a bateria; (c) tratar `web` e
`nenhuma` como uma classe só ("a base não cobriu") ao medir retrieval —
distinguir as duas mede a estabilidade do buscador, não a qualidade do RAG.

---

## 4. Vazamento do veto: recusa em prosa contada como resposta

Três respostas de `origem=web` são, no conteúdo, "não encontrei" — mas saíram
como resposta válida porque o modelo escreveu a recusa em prosa em vez de emitir
o marcador `CONTEXTO_INSUFICIENTE` que `prompts.eh_insuficiente` procura:

- **item 8**: *"Infelizmente, não há informações específicas sobre dicas para
  organizar os estudos orientados nos trechos fornecidos."*
- **item 15**: *"Infelizmente, não foi possível encontrar o endereço e telefone
  da Secretaria Geral (...) nos trechos fornecidos."*
- **item 11** (parcial): *"não é possível fornecer as datas específicas do
  calendário acadêmico"*.

Em todos, `web_insuficiente: null` e `veto_escapou: null` — ou seja, **nenhum
dos dois mecanismos de detecção viu nada.** A telemetria registrou sucesso.

Impactos, do mais visível ao mais insidioso:

**1. O aluno recebe um não-resposta em vez do encaminhamento.** É o pior dos
dois mundos. Se o veto tivesse disparado, `_responder_pela_web` devolveria
`_encaminhar_para_secretaria()` — o texto `SEM_CONTEXTO`, curto, com o e-mail de
quem resolve. Em vez disso o item 8 entregou um parágrafo que admite não ter a
informação e emenda em conteúdo tangencial (cursos EAD, guia de normalização),
sem dizer ao aluno o que fazer a seguir.

**2. A acurácia de `web` está superestimada** — 5/8 reportado contra ~3/8 útil.

**3. `scripts.lacunas` rebaixa a prioridade do tema.** *(Correção de uma versão
anterior desta análise, que afirmava que a lacuna sumiria do relatório — está
errado: `grounded=false` e `origem<>'encaminhado'` fazem o item **entrar** na
consulta.)* O efeito real é mais sutil e por isso mais perigoso: como
`sem_resposta` conta só `origem='nenhuma'`, o item entra rotulado
**"coberta pela web" (amarelo)** em vez de **"sem resposta" (vermelho)** — e o
relatório ordena por essa coluna. Quem lê vai tratá-lo como a lacuna barata,
aquela cuja fonte "já está achada", e ao abrir as URLs citadas descobre que elas
também não respondem. O rótulo mente sobre o trabalho que existe.

### Por que não é o mesmo bug que o `#SEM_COBERTURA#` já resolveu

O comentário em `prompts.py:47` descreve um caso parecido — o modelo devolveu
`INSUFFICIENT` traduzido, o veto não casou e o marcador cru foi para a tela.
São falhas **diferentes**, e a distinção define o conserto:

| | Marcador deformado (resolvido) | Recusa em prosa (aberto) |
|---|---|---|
| O modelo emitiu marcador? | sim, numa forma inesperada | **não, nenhuma** |
| Como falha | alto: aluno vê `#SEM_COBERTURA#` | **silencioso: parece resposta boa** |
| Dá para detectar por token? | sim — foi o que os `#` e o regex fizeram | **não há token a procurar** |
| Quem percebe | o aluno, e reclama | ninguém |

A camada 2 de `eh_insuficiente` (palavra solta em resposta curta) também não
alcança: as três respostas têm 900–1400 caracteres, muito acima do
`_LIMITE_VETO_PALAVRA_SOLTA = 200` — e o corte por tamanho está certo, é ele que
impede vetar uma resposta legítima que diga "documentação insuficiente".

Ou seja: **não existe token a procurar.** Endurecer o regex não resolve, e
tentar casar frases de recusa ("infelizmente", "não foi possível encontrar")
traz falso positivo em resposta legítima — bloquearia uma resposta correta que
apenas começa com uma ressalva.

---

## 5. Falso positivo da triagem (item 16)

> "Como faço para participar do processo seletivo de **bolsas** de iniciação
> científica na PUC Campinas?"

Encaminhada para **financeiro** em 1.3ms, sem tocar no RAG, pelo termo `"bolsa"`
em `ENCAMINHAMENTOS` (`app/core/config.py:105`). Iniciação científica é
pesquisa/acadêmico — o aluno é mandado para o setor de cobrança.

O termo `"bolsa"` é ambíguo do mesmo jeito que `"minha nota"` já é, e o próprio
código documenta o padrão para isso: entrada **própria** com `excecoes`. Hoje
`"bolsa"` está numa entrada com termos inequívocos (`boleto`, `fies`,
`mensalidade`), onde não dá para adicionar exceção sem afrouxar os outros.

---

## 6. O que limitou ESTA rodada (validade do experimento)

Três fatores tornam esta rodada inutilizável como linha de base de calibração:

### 6.1. `cache_hit: true` em 16 das 17 — a rodada não testou o pipeline

Todas as respostas de base vieram da `resposta_cache` (`input_tokens: null`,
`ms_llm: null`). **Esta rodada mediu o cache, não o retrieval nem o LLM.** Um
ajuste em `CHUNK_SIZE` ou `RELEVANCE_THRESHOLD` seria invisível aqui.

> Correção: `python -m scripts.clear_cache --yes` antes de **toda** rodada de
> calibração. (E `clear_logs` se quiser a janela de telemetria limpa.)

### 6.2. Quota do Gemini estourou na pergunta 8 — o modelo variou no meio

```
limit: 20, model: gemini-3.6-flash  (free tier, por DIA)
```

A partir daí a cadeia de fallback assumiu, e a rodada acabou comparando
**três modelos diferentes** entre si:

| Modelo | Itens |
|---|---|
| (cache, sem LLM) | 1–7, 9, 17 |
| huggingface / Llama-3.3-70B | 8, 10, 12, 13, 15 |
| openrouter / deepseek-v3.1 | 11 |
| nenhum (web sem resultado) | 14 |

A cadeia fez seu trabalho — **nenhuma pergunta ficou sem resposta por falta de
provider**, o que é uma validação positiva do design. Mas para calibrar
retrieval é preciso manter o gerador fixo: use `--modelo` para fixar um
provider que aguente as 25 chamadas (Groq ou HF), não o Gemini free.

> Nota: `groq:groq/compound` deu **404** (item 11). O prefixo está duplicado —
> o valor de `GROQ_MODEL` já recebe o prefixo `groq:` na cadeia. Confira com
> `python -m scripts.modelos --provider groq`.

### 6.3. Dois itens do dataset estavam errados, não o agente

- **item 17** (app Canvas celular): marquei `web`, mas a base respondeu, e
  respondeu **bem** (Guia-do-estudante p.13). O agente acertou; o gabarito
  errou. → corrigir para `base`.
- **item 8** (estudos orientados): marquei `base` supondo cobertura de
  `EstudosOrientados_v2-3.pdf`/`Gestao_Do_Tempo.pdf`; o LLM vetou os chunks.
  Precisa de inspeção manual (`scripts.ask "..." --debug`) para decidir se é
  gabarito errado ou lacuna real de conteúdo.

---

## 7. Latência

| Caminho | ms_total |
|---|---|
| triagem | 0.1 – 1.3 |
| base via cache | 227 – 646 |
| web (HF) | 9.8k – 28.4k |
| web (OpenRouter/deepseek) | **48.0k** |

- **Cold start**: item 1 com `ms_retrieve: 40.320ms` (40s) — é o download/carga
  do modelo local de embeddings ("Loading weights" no log). Paga-se uma vez por
  processo; some das demais (227–646ms). Vale pré-aquecer no boot da API para
  o primeiro aluno do dia não pagar isso.
- O fallback web custa **~50x** o caminho da base. Reforça o argumento de
  `scripts.lacunas`: indexar o conteúdo que hoje só a web responde troca ~15s
  por ~300ms.

---

## 8. Recomendações, em ordem de impacto

| # | Ação | Onde | Por quê |
|---|---|---|---|
| 1 | `clear_cache` antes de toda rodada | `scripts.clear_cache --yes` | sem isso a calibração não mede nada (§6.1) |
| 2 | Recalibrar a **escala** dos limiares para 0.84–0.88, ou trocar o critério | `RELEVANCE_THRESHOLD`, `EXACT_MATCH_THRESHOLD` | hoje ambos estão inertes (§3) |
| 3 | Fechar o vazamento do veto de recusa em prosa | `prompts.eh_insuficiente` | infla acurácia e cega o `lacunas` (§4) |
| 4 | Tirar `"bolsa"` da entrada `financeiro`, pôr em entrada própria com `excecoes` | `config.ENCAMINHAMENTOS` | manda aluno de IC para cobrança (§5) |
| 5 | Fixar o modelo com `--modelo` nas rodadas | `scripts.eval_run -m` | quota do Gemini quebra a comparação (§6.2) |
| 6 | Corrigir gabarito do item 17 (`web` → `base`) | `eval/perguntas_teste.json` | o agente acertou, o dataset errou (§6.3) |
| 7 | Corrigir `GROQ_MODEL` (prefixo duplicado) | `.env` | 404 na cadeia (§6.2) |
| 8 | Pré-aquecer embeddings no boot | `app/api` | 40s no primeiro request (§7) |
| 9 | Rodar N=3 e comparar item a item, não o total | método | 12% dos itens oscilam sozinhos (§3.3) |

### Status das correções (aplicadas em 2026-08-26)

| # | Ação | Status |
|---|---|---|
| 4 | `"bolsa"` em entrada própria com `excecoes` | ✅ `config.ENCAMINHAMENTOS` |
| 6 | Gabarito do item 17 (`web` → `base`) | ✅ `eval/perguntas_teste.json` |
| 7 | `GROQ_MODEL` sem o prefixo duplicado | ✅ `.env` |
| — | `score_min`/`score_mean` do top-k na telemetria | ✅ instrumentação p/ §3.1 |
| 1, 5, 9 | processo de rodada (cache, modelo fixo, N=3) | 📋 §9 |
| 2, 3, 8 | limiares, veto em prosa, pré-aquecimento | ⏳ pendentes |

Os campos novos de telemetria são **só instrumentação**: nada no roteamento os
lê. A ordem é deliberada — acumular dado real primeiro, decidir o critério
depois, que é o mesmo caminho que `triagem.py` documenta para a segunda camada
dela. As rodadas a partir de agora já gravam o dado; a §3.1 poderá ser refeita
com margem relativa em vez de hipótese.

### Sobre o item 2, a decisão de fundo

Como `score_top` não separa as classes (§3.1), subir o `RELEVANCE_THRESHOLD`
para ~0.86 cortaria acertos e erros na mesma proporção — não adianta. As saídas
reais, em ordem de custo:

- **Margem relativa** em vez de limiar absoluto: comparar `score[0]` com
  `score[k]` (dispersão do top-k). Quando a base cobre o tema, o topo tende a
  destacar; quando não cobre, os 5 chegam empatados. É o sinal que falta hoje —
  e o log **já tem** `score_top`, mas não guarda os demais scores. **Passo zero:
  registrar `score_min`/`score_mean` do top-k na telemetria** para testar essa
  hipótese com dados, sem adivinhar.
- **Reranker** (cross-encoder) no `PONTO DE EXTENSÃO` que `retriever.retrieve`
  já reserva: dá um score de fato discriminativo, ao custo de mais um modelo.
- **Aceitar o desenho atual**: assumir que o LLM é o roteador (é o que já
  acontece de fato) e tratar `relevance_threshold` como uma rede de segurança
  contra lixo, não como instrumento de calibração.

---

## 9. Como refazer a rodada corretamente

```bash
# 1. confirme que o modelo fixo existe no catálogo DA SUA CHAVE
python -m scripts.modelos --provider groq

# 2. cache limpo — sem isso a rodada mede o cache, não o pipeline (§6.1)
python -m scripts.clear_cache --yes

# 3. modelo FIXO, para o gerador não variar no meio (§6.2)
python -m scripts.eval_run eval/perguntas_teste.json -m groq:<modelo-do-passo-1>

# 4. leitura item a item, não só o total (§3.3)
python -m scripts.eval_report eval/perguntas_teste.json --dias 1 --detalhe
```

Repita os passos 2–4 três vezes e use a **mediana por item**: com 12% dos itens
oscilando sozinhos (§3.3), uma rodada única não distingue efeito de ruído.

`clear_logs` **não** entra aqui de propósito: manter a telemetria acumulada é o
que permite comparar a rodada de hoje com a de antes do ajuste. Use-o só para
zerar a janela quando quiser recomeçar o histórico.

Só a partir dessa linha de base — cache limpo, modelo fixo, gabarito corrigido —
os números de acurácia passam a significar alguma coisa sobre `CHUNK_SIZE` e
`RELEVANCE_THRESHOLD`.
