"""Provider do Gemini — o primeiro da cadeia (ver `app/providers/chain.py`).

Continua sendo o provedor preferido: é o modelo com que os prompts em
`app/agent/prompts.py` foram calibrados, e os outros dois existem para o dia em
que ele não atender, não para dividir tráfego com ele.

`get_embeddings` saiu daqui para `app/providers/embeddings.py` — os embeddings
nunca foram do Gemini (rodam locais, via HuggingFace) e o nome do módulo já era
enganoso antes desta mudança.
"""

import logging

from langchain_core.messages import AIMessage, BaseMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """Gemini via `langchain-google-genai`.

    `timeout` é obrigatório e não decorativo: as rotas da API são síncronas
    (`def`), então o FastAPI as roda no threadpool — uma chamada pendurada
    segura uma thread até o socket morrer sozinho. Poucas dessas e o servidor
    inteiro para de responder, inclusive as perguntas que sairiam do cache sem
    tocar no LLM.
    """

    nome = "gemini"

    def __init__(self, api_key: str, modelo: str, timeout: float, tentativas: int):
        self.modelo = modelo
        # Guardada só para `listar_modelos` (a CLI de catálogo): o
        # `langchain-google-genai` não expõe o `ListModels` da API, então essa
        # chamada é feita pelo SDK do Google direto. Nunca vai para log — ver
        # `providers/chain.sem_segredo`.
        self._api_key = api_key
        # ATENÇÃO à unidade: em `langchain-google-genai`, `max_retries` alimenta
        # um `stop_after_attempt` do tenacity — é o número TOTAL de tentativas,
        # não de repetições (o default 6 significa 6 chamadas). Já no SDK da
        # OpenAI o mesmo nome significa "repetições depois da primeira". A
        # tradução das duas semânticas para o mesmo `tentativas` é feita aqui e
        # em `openai_compat.py`, e é por isso que a configuração fala em
        # TENTATIVAS, não em retries — ver `settings.llm_tentativas_por_provider`.
        self._llm = ChatGoogleGenerativeAI(
            model=modelo,
            google_api_key=api_key,
            temperature=0.1,  # suporte acadêmico: previsibilidade > criatividade
            timeout=timeout,
            max_retries=max(tentativas, 1),
        )

    def generate(self, mensagens: list[BaseMessage]) -> AIMessage:
        return self._llm.invoke(mensagens)

    def listar_modelos(self) -> list[str]:
        """`ListModels` da API do Google, via REST direto — de propósito, SEM o SDK.

        `google.generativeai.list_models()` desserializa a resposta num
        dataclass `Model` fixo da versão instalada do SDK. Bug real visto em
        produção: a API já devolve um campo (`thinking`) que o SDK instalado
        não conhece, e a desserialização quebra com
        `Model.__init__() got an unexpected keyword argument 'thinking'` —
        pane que atinge TODOS os modelos, porque acontece antes de qualquer
        filtro rodar. `ListModels` é um contrato JSON estável; ler os dois
        campos que interessam (`name`, `supportedGenerationMethods`) direto da
        resposta evita ficar hostage da versão do SDK instalada, que pode
        atrasar meses atrás do que a API já aceita.

        `httpx` e não `requests`: já é dependência do projeto (transitiva do
        FastAPI/TestClient) — sem biblioteca nova para uma chamada.

        O filtro por `generateContent` não é cosmético: metade do catálogo é de
        embedding e de `aqa`, e oferecê-los como opção de chat só produziria o
        próximo 404. O prefixo `models/` sai porque é assim que o nome entra no
        `.env`.
        """
        import httpx

        modelos: list[str] = []
        params: dict[str, str | int] = {"key": self._api_key, "pageSize": 200}
        with httpx.Client(timeout=30.0) as cliente:
            while True:
                resposta = cliente.get(
                    "https://generativelanguage.googleapis.com/v1beta/models", params=params
                )
                resposta.raise_for_status()
                corpo = resposta.json()
                modelos.extend(
                    modelo["name"].removeprefix("models/")
                    for modelo in corpo.get("models", [])
                    if "generateContent" in modelo.get("supportedGenerationMethods", [])
                )
                token = corpo.get("nextPageToken")
                if not token:
                    break
                params["pageToken"] = token
        return sorted(modelos)
