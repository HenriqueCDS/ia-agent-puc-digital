"""Triagem por assunto: encaminha o que não é do agente ANTES de gastar RAG.

Roda como primeira etapa de `responder._responder`, antes do retrieval. A ordem
não é detalhe de performance — é o ponto principal:

- é guardrail, não otimização. Uma pergunta sobre boleto que chega ao retrieval
  pode recuperar um chunk fraco (qualquer coisa acima de `relevance_threshold`)
  e o LLM responderá sobre dinheiro a partir de um documento que não é sobre
  isso. Resposta errada sobre cobrança é pior que resposta nenhuma;
- a pergunta fora de escopo não paga embedding, nem query no pgvector, nem
  token de LLM;
- a pergunta em escopo — o caminho comum — paga só uma comparação de substring
  em memória.

POR QUE LÉXICO, e não embedding ou uma chamada leve ao LLM: as categorias aqui
são nomeadas por substantivos de altíssima distintividade (boleto, diploma,
rematrícula, FIES, ProUni). Não é intenção difusa, é vocabulário fechado — o
caso em que casar termo ganha de classificar. Além disso um guardrail sobre
dinheiro e registro acadêmico precisa ser determinístico e auditável: dá para
provar em teste qual pergunta vai para qual e-mail. Uma chamada ao LLM na
entrada do pipeline seria contraditória (gastar LLM para evitar gastar LLM).

O QUE ESTA ABORDAGEM NÃO PEGA: paráfrase sem o termo ("não consigo pagar a
parcela", "quero meu canudo"). Essas seguem para o RAG, que é o comportamento
de hoje — a triagem só melhora, nunca piora. O caminho para fechar essa lacuna
é medir antes de codar: `origem="encaminhado"` e `assunto` na telemetria dizem
quais categorias disparam e com que volume, e as perguntas que deveriam ter
disparado aparecem agrupadas por `pergunta_hash` em `origem="nenhuma"`. Com esse
dado dá para adicionar uma segunda camada por embedding (o modelo local já está
carregado, custo de API zero — ver `web_fallback._relevantes`) com limiar
calibrado em caso real, e não no chute.
"""

import logging
import unicodedata

from app.core.config import ENCAMINHAMENTOS, CategoriaEncaminhada

logger = logging.getLogger(__name__)


def _sem_acento(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def classificar(pergunta: str) -> CategoriaEncaminhada | None:
    """A categoria fora de escopo que a pergunta casa, ou None se é do agente.

    Fonte única da verdade sobre "este assunto é nosso?": além da triagem na
    entrada, é o que `web_fallback` consulta para não levar assunto sensível
    para a busca externa. Ter os dois lados na mesma função é o que garante que
    uma exceção valha nos dois — "matrícula em disciplina no Canvas" não é
    encaminhada E pode ser pesquisada na web.

    Percorre `ENCAMINHAMENTOS` na ordem declarada e para no primeiro match; ver
    lá por que a ordem importa.
    """
    texto = _sem_acento(pergunta)
    for categoria in ENCAMINHAMENTOS:
        if any(_sem_acento(e) in texto for e in categoria.excecoes):
            continue
        if any(_sem_acento(t) in texto for t in categoria.termos):
            return categoria
    return None
