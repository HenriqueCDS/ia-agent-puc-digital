"""Providers 2, 3 e 4 da cadeia: HuggingFace, Groq e OpenRouter.

Os três expõem a API de chat da OpenAI, então a diferença entre eles cabe em
dois campos — `base_url` e `modelo`. Uma base parametrizada em vez de três
classes copiadas: a lógica de chamada, timeout e tentativa existe UMA vez, e
acrescentar um provedor compatível novo (Together, Fireworks, um vLLM interno)
passa a ser uma entrada de configuração, não um arquivo novo.

Por que o SDK `openai` cru e não `langchain-openai`: o `langchain-openai`
publicado hoje exige um `langchain-core` da série 0.4, e este projeto está
preso na 0.3 por causa do `langchain-postgres` e do `langchain-google-genai`
(instalá-lo quebra o import com `ImportError: cannot import name
ContextOverflowError`). Trocar a linha inteira do LangChain para acomodar dois
providers de fallback seria o rabo abanando o cachorro — e a superfície que
usamos aqui é pequena: uma chamada de chat e a leitura do uso de token. O que o
LangChain nos dá de fato (`AIMessage` com `usage_metadata`) é construído em
`_para_ai_message`, e é o mesmo objeto que o Gemini devolve — que é a única
coisa que a cadeia e a telemetria exigem.
"""

import logging

from langchain_core.messages import AIMessage, BaseMessage, convert_to_openai_messages
from openai import OpenAI

from app.providers.base import LLMProvider

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"


def _para_ai_message(resposta) -> AIMessage:
    """Resposta da API da OpenAI -> `AIMessage`, no formato que o Gemini devolve.

    `usage_metadata` com estes três nomes exatos não é detalhe: é o que
    `telemetry.Registro.somar_tokens` lê para contabilizar custo por pergunta.
    Um provider que devolvesse o dict cru da OpenAI (`prompt_tokens`/
    `completion_tokens`) apareceria na telemetria com custo zero — falha
    silenciosa, do tipo que só aparece na fatura.
    """
    escolha = resposta.choices[0]
    uso = getattr(resposta, "usage", None)
    metadata_uso = None
    if uso is not None:
        metadata_uso = {
            "input_tokens": uso.prompt_tokens or 0,
            "output_tokens": uso.completion_tokens or 0,
            "total_tokens": uso.total_tokens or 0,
        }

    return AIMessage(
        content=escolha.message.content or "",
        usage_metadata=metadata_uso,
        # `model` da RESPOSTA, não o pedido: o OpenRouter roteia para variantes
        # e é o valor devolvido que diz o que de fato gerou o texto.
        response_metadata={
            "model_name": getattr(resposta, "model", None),
            "finish_reason": escolha.finish_reason,
        },
    )


class OpenAICompatibleProvider(LLMProvider):
    """Qualquer endpoint que fale a API de chat da OpenAI."""

    def __init__(
        self,
        nome: str,
        api_key: str,
        modelo: str,
        base_url: str,
        timeout: float,
        tentativas: int,
        max_tokens: int,
        headers: dict[str, str] | None = None,
        temperatura: float = 0.1,  # suporte acadêmico: previsibilidade > criatividade
    ):
        self.nome = nome
        self.modelo = modelo
        self.temperatura = temperatura
        # Teto de saída, aplicado por chamada em `generate` (`max_tokens` é o
        # nome do parâmetro no SDK da OpenAI; `max_output_tokens` no
        # langchain-google-genai — ver `gemini.py`). Ver `settings.llm_max_tokens`
        # (VET-3).
        self.max_tokens = max_tokens
        # `max_retries` aqui é o do SDK da OpenAI: repetições DEPOIS da primeira
        # chamada. `tentativas=1` (o padrão) vira 0 — uma chamada e ponto. É o
        # oposto da semântica do mesmo nome no Gemini; ver o comentário em
        # `gemini.GeminiProvider.__init__`.
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max(tentativas - 1, 0),
            default_headers=headers or None,
        )

    def generate(self, mensagens: list[BaseMessage]) -> AIMessage:
        resposta = self._client.chat.completions.create(
            model=self.modelo,
            messages=convert_to_openai_messages(mensagens),
            temperature=self.temperatura,
            max_tokens=self.max_tokens,
        )
        return _para_ai_message(resposta)

    def listar_modelos(self) -> list[str]:
        """`GET /models` — o catálogo que ESTA chave enxerga. Ver a base."""
        return sorted(modelo.id for modelo in self._client.models.list().data)


class GroqProvider(OpenAICompatibleProvider):
    """Groq — segundo da cadeia.

    Vem antes do OpenRouter por latência: a inferência da Groq é a mais rápida
    dos três, e este é o provider que atende quando o Gemini está fora. O
    fallback só vale a pena se o aluno não perceber que ele aconteceu.
    """

    def __init__(
        self,
        api_key: str,
        modelo: str,
        timeout: float,
        tentativas: int,
        max_tokens: int,
        base_url: str = GROQ_BASE_URL,
    ):
        super().__init__(
            nome="groq",
            api_key=api_key,
            modelo=modelo,
            base_url=base_url,
            timeout=timeout,
            tentativas=tentativas,
            max_tokens=max_tokens,
        )


class HuggingFaceProvider(OpenAICompatibleProvider):
    """HuggingFace Inference Providers — segundo da cadeia.

    Vem logo depois do Gemini: usa o mesmo `HF_TOKEN` que os embeddings locais já
    exigem (ver `app/providers/embeddings.py`), então é fallback "de graça" — sem
    chave nova a gerenciar — antes de cair para Groq/OpenRouter. `router.
    huggingface.co/v1` é o endpoint único e compatível com a API de chat da
    OpenAI que a HF expõe na frente de vários backends (Together, Novita,
    Fireworks...); qual backend atende um modelo dado é decidido pela HF, não
    por este código.
    """

    def __init__(
        self,
        api_key: str,
        modelo: str,
        timeout: float,
        tentativas: int,
        max_tokens: int,
        base_url: str = HUGGINGFACE_BASE_URL,
    ):
        super().__init__(
            nome="huggingface",
            api_key=api_key,
            modelo=modelo,
            base_url=base_url,
            timeout=timeout,
            tentativas=tentativas,
            max_tokens=max_tokens,
        )


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter — último da cadeia, com modelo `:free`.

    Último de propósito: o modelo gratuito tem a cota mais apertada e a fila
    mais imprevisível dos três. Ele é a rede de segurança para o dia em que
    Gemini E Groq estiverem fora ao mesmo tempo, não um provedor de regime.

    O header é o que o OpenRouter usa para atribuir o tráfego a uma aplicação
    (e o que dá acesso ao rate limit de aplicação identificada, em vez do de
    anônimo). Nenhum dado de aluno vai nele — é só o nome do projeto.
    """

    def __init__(
        self,
        api_key: str,
        modelo: str,
        timeout: float,
        tentativas: int,
        max_tokens: int,
        base_url: str = OPENROUTER_BASE_URL,
        titulo: str = "ia-agent-puc-digital",
    ):
        super().__init__(
            nome="openrouter",
            api_key=api_key,
            modelo=modelo,
            base_url=base_url,
            timeout=timeout,
            tentativas=tentativas,
            max_tokens=max_tokens,
            headers={"X-Title": titulo},
        )
