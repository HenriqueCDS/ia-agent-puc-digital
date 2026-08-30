"""Contrato comum dos provedores de geração e a classificação de falha.

Duas coisas moram aqui, e as duas existem para que `chain.py` fique curto:

1. `LLMProvider` — a interface que Gemini, Groq e OpenRouter implementam.
2. `motivo_de_fallback` — a regra que decide se uma exceção autoriza a cadeia a
   tentar o próximo provider ou se ela deve subir para quem chamou.

O ponto 2 é o coração da tarefa, e é o que separa um fallback de um `except
Exception` disfarçado: cair para o próximo provider é a resposta certa para
"este provedor está indisponível para mim agora" (cota, credencial, timeout,
5xx) e a resposta ERRADA para "este pedido está errado" (prompt inválido,
parâmetro que o modelo não aceita, JSON malformado). No segundo caso, os três
providers falhariam do mesmo jeito — mascarar isso custa três chamadas, três
timeouts de latência, e some com o erro que o desenvolvedor precisa ver.

Por que a classificação é por status HTTP + nome da exceção, e não por
`except GoogleAPIError` / `except openai.RateLimitError`: são três SDKs com três
hierarquias próprias (e o LangChain às vezes reembrulha, às vezes repassa a
exceção crua do SDK). Casar tipo a tipo significaria uma lista que quebra em
silêncio a cada upgrade de biblioteca — e o modo de falha é o pior possível:
o fallback simplesmente para de acontecer, e ninguém percebe até a cota do
Gemini acabar em produção. Status e nome são estáveis porque são o protocolo,
não a implementação.
"""

import logging
import re

from abc import ABC, abstractmethod
from typing import NamedTuple

from langchain_core.messages import AIMessage, BaseMessage

logger = logging.getLogger(__name__)


class Motivo(NamedTuple):
    """Por que a cadeia caiu para o próximo, e de quem é a culpa.

    `configuracao=True` marca a falha que NÃO se conserta esperando: o modelo
    configurado não existe para aquela chave. A cadeia continua caindo para o
    próximo provider (o aluno é atendido), mas a linha sai como ERROR em vez de
    WARNING — senão um `GROQ_MODEL` errado fica meses invisível, porque a
    resposta sempre chega e ninguém olha WARNING de rotina.
    """

    rotulo: str
    configuracao: bool = False

    def __str__(self) -> str:
        return self.rotulo


class ProviderIndisponivel(Exception):
    """Um provider falhou por um motivo que autoriza tentar o próximo.

    Não é levantada pelos providers — é construída pela cadeia ao classificar a
    exceção original, e guardada para compor a mensagem de
    `TodosProvidersFalharam` se nenhum responder.
    """

    def __init__(self, provider: str, motivo: str, detalhe: str):
        self.provider = provider
        self.motivo = motivo
        self.detalhe = detalhe
        super().__init__(f"{provider}: {motivo} ({detalhe})")


class TodosProvidersFalharam(RuntimeError):
    """Nenhum provider da cadeia conseguiu responder.

    `RuntimeError` de propósito: `app/api/errors.py` já mapeia essa família para
    503 ("serviço indisponível", e não "seu pedido está errado"), que é
    exatamente a semântica aqui — e é o mesmo código que a ausência de chave já
    devolvia. Quem consome `/ask` não vê contrato novo.
    """


# Status que significam "este provedor não vai me atender agora" — os quatro
# gatilhos pedidos, traduzidos para HTTP:
#
#   401/403  credencial inválida ou token expirado (a chave do vizinho serve)
#   402      crédito acabado (o `:free` do OpenRouter devolve isso)
#   408      timeout reportado pelo servidor
#   429      cota/rate limit
#   499      requisição cancelada / deadline estourado do lado do cliente-proxy
#            (o gRPC do Gemini reporta `Cancelled` como 499 — bug real: Q14/Q16
#            de 2026-08-28 morriam sem teste porque o `Cancelled: 499` cru
#            subia em vez de a cadeia tentar o próximo provedor)
#   5xx      falha do lado do provedor
#
# Tudo o que NÃO está aqui (nem em `_STATUS_DE_CONFIGURACAO` nem tratado à
# parte abaixo) e tem status conhecido propaga: 400 (prompt/parâmetro
# inválido), 422.
_STATUS_DE_FALLBACK = frozenset({401, 402, 403, 408, 429, 499})

# 413 = `request_too_large`. Cai para o próximo provider, e a razão é a mesma do
# 400 de contexto excedido tratado em `_MENSAGENS_DE_CONTEXTO_EXCEDIDO`: o teto
# de tokens por requisição é do MODELO/tier, não do formato do pedido. O SDK da
# OpenAI reporta assim quando o mesmo `mensagens` que estoura um free-tier da
# Groq (ex.: "Request Entity Too Large" em `qwen/qwen3.6-27b`) cabe folgado no
# Gemini. Propagar aqui derrubava `/ask` (e a eval com `--modelo`) com o
# `APIStatusError: Error code: 413` cru, em vez de tentar quem aceitaria.
_STATUS_DE_CONTEXTO = frozenset({413})

# 404 = `model_not_found`. Cai para o próximo, mas em categoria PRÓPRIA, e a
# distinção custou um bug real ("The model `llama-3.3-70b-versatile` does not
# exist or you do not have access to it"):
#
# * É erro de CONFIGURAÇÃO do provider, não do pedido. O prompt está perfeito e
#   os outros dois provedores o aceitariam — tratá-lo como 400 e propagar fazia
#   um nome de modelo errado no Groq derrubar `/ask` inteiro mesmo com Gemini e
#   OpenRouter de pé. Era exatamente o que a cadeia existe para evitar.
# * Mas também não é indisponibilidade passageira: esperar não conserta, e
#   nenhuma requisição futura vai funcionar melhor. Por isso vai para o log em
#   ERROR (ver `Motivo.configuracao`), com a dica de como descobrir o nome
#   certo — a mensagem do provedor não distingue "não existe" de "sua chave não
#   tem acesso", e as duas se resolvem olhando o catálogo da própria chave
#   (`python -m scripts.modelos`).
_STATUS_DE_CONFIGURACAO = frozenset({404})

# Marcas no NOME da exceção que valem por si, sem status: timeout e falha de
# conexão nunca chegam com resposta HTTP, e é justamente o caso em que a cadeia
# mais precisa funcionar (o socket morreu, não houve resposta para ler).
#
# Inclui os nomes gRPC do Gemini (`ResourceExhausted`, `DeadlineExceeded`,
# `PermissionDenied`, `Unauthenticated`, `ServiceUnavailable`, `Cancelled`), os
# do SDK da OpenAI (`APITimeoutError`, `APIConnectionError`, `RateLimitError`) e
# os builtins (`TimeoutError`, `ConnectionError`).
#
# `cancelled` cobre o `google.api_core.exceptions.Cancelled` do Gemini, que
# chega como nome de exceção (status 499 no texto, sem `status_code`
# estruturado, e sem o prefixo "error code" que `_STATUS_NO_TEXTO` exige). É
# transporte, não pedido: o socket foi cortado antes da resposta, e o próximo
# provedor aceitaria o mesmo `mensagens`.
_NOMES_DE_FALLBACK = (
    "timeout",
    "deadline",
    "connection",
    "cancelled",
    "unavailable",
    "resourceexhausted",
    "toomanyrequests",
    "ratelimit",
    "unauthenticated",
    "unauthorized",
    "permissiondenied",
    "internalserver",
    "overloaded",
)

# Só formatos em que o número REALMENTE é um status, para o caso de o SDK
# entregar a exceção sem nenhum atributo estruturado. Um `\d{3}` solto casaria
# com qualquer id, contagem de token ou timestamp no meio da mensagem e faria a
# cadeia cair para o próximo provider por um erro de validação.
_STATUS_NO_TEXTO = re.compile(
    r"(?:error code|status[ _-]?code|http[ _-]?(?:error|status)|response code)\D{0,4}(\d{3})",
    re.IGNORECASE,
)

# Exceção à regra "400 é erro do pedido, propaga": estouro de janela de
# contexto TAMBÉM chega como 400 (é assim que o SDK da OpenAI reporta —
# `openai.BadRequestError`, "Please reduce the length of the messages or
# completion"), mas a premissa de "os três provedores rejeitariam igual" não
# vale aqui. Contexto máximo é por MODELO, não por formato de pedido: o mesmo
# `mensagens` que estoura um `:free` do OpenRouter cabe folgado no Gemini. Cair
# para o próximo provider é, portanto, a resposta certa — e é o próprio
# `TodosProvidersFalharam` (com a causa de cada tentativa) que volta para quem
# chamou se nenhum aceitar.
_MENSAGENS_DE_CONTEXTO_EXCEDIDO = re.compile(
    r"reduce the length|context[_ ]length[_ ]exceeded|maximum context length|"
    r"context window|too many tokens|prompt is too long",
    re.IGNORECASE,
)


def _status_http(exc: BaseException) -> int | None:
    """Status HTTP da exceção, procurado nos formatos dos três SDKs.

    `openai.APIStatusError` traz `status_code`; `google.api_core.exceptions`
    traz `code` como int HTTP; `httpx.HTTPStatusError` traz `response.status_code`.
    O filtro `100 <= valor < 600` existe por causa do `code`: em exceções gRPC
    ele é um enum de código gRPC, não um status HTTP, e sem o filtro um
    `INVALID_ARGUMENT` (3) passaria por aqui como se fosse alguma coisa.
    """
    for atributo in ("status_code", "http_status", "code"):
        valor = getattr(exc, atributo, None)
        if isinstance(valor, int) and 100 <= valor < 600:
            return valor

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 100 <= status < 600:
        return status

    achado = _STATUS_NO_TEXTO.search(str(exc))
    return int(achado.group(1)) if achado else None


def motivo_de_fallback(exc: BaseException) -> Motivo | None:
    """Gatilho que autoriza o próximo provider, ou `None` para propagar.

    O rótulo (`"HTTP 429"`, `"APITimeoutError"`...) vai direto para o log: é ele
    que responde "por que caiu para o Groq hoje?" sem precisar de traceback. O
    `configuracao` do `Motivo` escolhe o nível — ver a nota lá.

    A regra padrão é PROPAGAR. Exceção desconhecida, sem status e sem nome
    reconhecido, sobe — porque a alternativa (cair para o próximo por via das
    dúvidas) transforma qualquer bug nosso em três chamadas silenciosas e um
    503 genérico no fim, apagando o rastro do bug de verdade.
    """
    status = _status_http(exc)
    if status is not None:
        if status in _STATUS_DE_CONFIGURACAO:
            return Motivo(
                f"HTTP {status} — modelo não existe ou a chave não tem acesso a ele; "
                "confira o catálogo com `python -m scripts.modelos`",
                configuracao=True,
            )
        if status in _STATUS_DE_FALLBACK:
            return Motivo(f"HTTP {status}")
        if status in _STATUS_DE_CONTEXTO:
            return Motivo(f"HTTP {status} (requisição excede o limite do modelo)")
        if 500 <= status < 600:
            return Motivo(f"HTTP {status} (falha do provedor)")
        if status == 400 and _MENSAGENS_DE_CONTEXTO_EXCEDIDO.search(str(exc)):
            return Motivo("HTTP 400 (contexto excede o limite do modelo)")
        # Status conhecido e fora das listas = o PEDIDO é que está errado.
        # Nenhum outro provider vai aceitá-lo, então não adianta tentar.
        return None

    nome = type(exc).__name__.casefold()
    if any(marca in nome for marca in _NOMES_DE_FALLBACK):
        return Motivo(type(exc).__name__)

    return None


class LLMProvider(ABC):
    """Um provedor de geração, com uma única operação.

    NOTA DE CONTRATO — o método é `generate(mensagens) -> AIMessage`, e não
    `generate(prompt, contexto) -> str`, porque este projeto já tem dois
    contratos estabelecidos que uma assinatura de string quebraria:

    * `app/agent/responder.py` escolhe entre TRÊS templates de prompt
      (`ANSWER_PROMPT`, `ANSWER_PROMPT_ALTA_CONFIANCA`, `ANSWER_PROMPT_WEB`) e
      já monta o contexto com `_format_context`. Receber `(prompt, contexto)`
      cru significaria mover essa escolha para cá — que é justamente a "lógica
      de guardrail existente" que não deve mudar.
    * `AIMessage.usage_metadata` é de onde a telemetria tira input/output tokens
      (`telemetry.Registro.somar_tokens`). Devolver `str` apagaria a métrica de
      custo por pergunta, que é metade do porquê da telemetria existir.

    O que a interface garante é o que importa para a cadeia: uma operação só,
    mesma entrada e mesma saída para os três provedores, sem nada do SDK
    vazando. Quem quiser a forma de string tem `str(resposta.content)`.
    """

    #: Rótulo curto, usado no log e na telemetria ("gemini", "groq"...).
    nome: str
    #: Modelo concreto, para o log responder "quem respondeu e com o quê".
    modelo: str

    @abstractmethod
    def generate(self, mensagens: list[BaseMessage]) -> AIMessage:
        """UMA tentativa. Falha vira exceção — quem decide o que fazer é a cadeia.

        Nenhum provider faz backoff por conta própria: com três provedores em
        fila, o retry interno multiplica a latência do pior caso pelo número de
        tentativas antes de a cadeia sequer começar a trabalhar. Ver
        `settings.llm_tentativas_por_provider`.
        """

    def listar_modelos(self) -> list[str]:
        """Modelos que ESTA chave consegue usar, para a CLI `scripts.modelos`.

        Não é `abstractmethod` de propósito: é uma conveniência de operação, não
        parte do caminho de resposta. Um provider futuro sem endpoint de
        catálogo continua sendo um provider válido — a CLI mostra "não
        suportado" e segue para os outros, em vez de o import quebrar.

        Existe porque a mensagem de 404 dos provedores é ambígua por natureza
        ("does not exist OR you do not have access to it") e as duas causas se
        resolvem com o mesmo dado: o que a chave de fato acessa.
        """
        raise NotImplementedError(
            f"{type(self).__name__} não sabe listar modelos"
        )

    def __repr__(self) -> str:  # pragma: no cover - conveniência de debug
        return f"{type(self).__name__}(nome={self.nome!r}, modelo={self.modelo!r})"
