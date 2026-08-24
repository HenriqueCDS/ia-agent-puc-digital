"""A cadeia de fallback: tenta os providers em ordem, o primeiro que responder vence.

Fallback NÃO é retry, e a diferença define o desenho deste módulo: retry é
insistir com quem já falhou (e aí backoff exponencial faz sentido); fallback é
desistir dele e perguntar para outro. Por isso são no máximo `n` chamadas para
`n` providers, sem espera entre elas — o pior caso de latência é a soma dos
timeouts, e é o único orçamento que precisa ser explicado a quem opera.

A cadeia é montada a partir do `.env` (`LLM_PROVIDERS=gemini,groq,openrouter`):
reordenar, desligar um provedor ou rodar com um só é edição de configuração,
sem deploy de código.

CONTRATO COM O RESTO DO PROJETO — `ProviderChain.invoke(mensagens) -> AIMessage`
é a mesma assinatura de um `BaseChatModel` do LangChain, que é o que
`app/agent/responder.py` já chamava e o que os dublês de teste já implementam.
A cadeia entra no lugar do `ChatGoogleGenerativeAI` sem que responder, guardrail
ou o endpoint `/ask` saibam da existência de mais de um provedor.
"""

import logging
import re

from functools import lru_cache
from typing import Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from app.core.config import settings
from app.providers.base import (
    LLMProvider,
    ProviderIndisponivel,
    TodosProvidersFalharam,
    motivo_de_fallback,
)
from app.providers.gemini import GeminiProvider
from app.providers.openai_compat import (
    GROQ_BASE_URL,
    OPENROUTER_BASE_URL,
    GroqProvider,
    OpenRouterProvider,
)

logger = logging.getLogger(__name__)

_REDIGIDO = "***"

# Chave que aparece no texto de uma exceção do SDK. A defesa principal é a
# outra (`_segredos_configurados`, que redige o valor exato das chaves em uso);
# estes prefixos cobrem o caso em que a mensagem traz uma chave que NÃO é a
# nossa — a de outro ambiente num erro proxeado, um exemplo colado pelo provedor
# na mensagem de erro. Vazamento de credencial em log é irreversível: o log já
# foi para stderr, para o agregador e para o backup antes de alguém notar.
_PADROES_DE_CHAVE = re.compile(
    # `sk-or-` antes de `sk-`: alternância em regex é o PRIMEIRO que casa, não o
    # mais longo. Na prática as duas redigem a chave inteira aqui (o `[\w\-]+` é
    # guloso), mas a ordem certa evita a pegadinha se um dos padrões ganhar uma
    # âncora depois.
    r"\b(?:AIza[\w\-]{10,}|sk-or-[\w\-]{10,}|sk-[\w\-]{10,}|gsk_[\w\-]{10,})\b"
)


def _segredos_configurados() -> list[str]:
    """Valores de chave que este processo conhece, para redigir do log."""
    candidatos = (
        settings.chave_gemini,
        settings.groq_api_key,
        settings.openrouter_api_key,
    )
    # `len > 8` evita que uma chave vazia ou de placeholder ("test") transforme
    # o log inteiro em asteriscos nos testes.
    return [chave for chave in candidatos if chave and len(chave) > 8]


def sem_segredo(texto: str) -> str:
    """Texto de exceção pronto para o log, sem nenhuma chave de API.

    Público (sem `_`) porque é testável isoladamente e porque qualquer código
    futuro que registre uma falha de provider deve passar por aqui.
    """
    for chave in _segredos_configurados():
        texto = texto.replace(chave, _REDIGIDO)
    return _PADROES_DE_CHAVE.sub(_REDIGIDO, texto)


class ProviderChain:
    """Providers em ordem de prioridade; UMA tentativa em cada.

    Não herda de `BaseChatModel` de propósito: herdar traria o contrato inteiro
    do LangChain (`_generate`, `_llm_type`, batch, streaming, callbacks) para ser
    honrado por algo que só precisa de `invoke`. Aqui, o que a cadeia entrega é
    exatamente o que `responder.py` consome, e o que ela não entrega falha no
    import de quem tentar usar — em vez de falhar em produção num caminho que
    ninguém testou.
    """

    def __init__(self, providers: Sequence[LLMProvider]):
        if not providers:
            # `RuntimeError` -> 503 no `/ask` (ver app/api/errors.py), mesma
            # resposta que a ausência de chave já dava antes desta mudança.
            raise RuntimeError(
                "Nenhum provider de LLM configurado. Preencha ao menos uma das chaves "
                "(GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY) no .env e confira "
                "LLM_PROVIDERS."
            )
        self._providers = tuple(providers)

    @property
    def providers(self) -> tuple[LLMProvider, ...]:
        return self._providers

    def generate(self, mensagens: list[BaseMessage]) -> AIMessage:
        """Percorre a cadeia e devolve a primeira resposta.

        Levanta `TodosProvidersFalharam` se todos ficarem indisponíveis, e
        RE-LEVANTA na hora a exceção que não for de indisponibilidade (prompt
        inválido, modelo inexistente): esse erro é do pedido, e nenhum outro
        provider o aceitaria — ver `base.motivo_de_fallback`.
        """
        falhas: list[ProviderIndisponivel] = []

        for provider in self._providers:
            try:
                resposta = provider.generate(mensagens)
            except Exception as exc:
                motivo = motivo_de_fallback(exc)
                if motivo is None:
                    # Erro de conteúdo/validação: propaga sem tocar nos outros.
                    # Sem log de WARNING aqui — não houve fallback para
                    # explicar, e quem chamou vai receber a exceção inteira.
                    raise
                detalhe = sem_segredo(f"{type(exc).__name__}: {exc}")
                # WARNING quando é indisponibilidade (a pergunta ainda vai ser
                # respondida pelo próximo da fila, e esperar resolve). ERROR
                # quando é configuração — modelo que não existe para a chave:
                # cair para o próximo atende o aluno, mas nenhuma requisição
                # futura vai funcionar melhor, e em WARNING isso ficaria meses
                # invisível porque a resposta sempre chega. Ver `base.Motivo`.
                logger.log(
                    logging.ERROR if motivo.configuracao else logging.WARNING,
                    "provider %s (%s) fora da cadeia [%s]: %s — tentando o próximo",
                    provider.nome,
                    provider.modelo,
                    motivo.rotulo,
                    detalhe,
                )
                falhas.append(ProviderIndisponivel(provider.nome, motivo.rotulo, detalhe))
                continue

            if falhas:
                # Só quando houve troca: em operação normal o INFO abaixo já
                # basta, e uma linha extra por pergunta afogaria o sinal.
                logger.info(
                    "cadeia caiu para %s depois de %d provider(s) indisponível(is): %s",
                    provider.nome,
                    len(falhas),
                    ", ".join(f"{f.provider} [{f.motivo}]" for f in falhas),
                )
            logger.info("resposta gerada por %s (%s)", provider.nome, provider.modelo)
            return _carimbar(resposta, provider)

        resumo = "; ".join(str(falha) for falha in falhas)
        logger.error("todos os %d providers falharam: %s", len(falhas), resumo)
        # `from None` de propósito: encadear a última exceção do SDK anexaria a
        # mensagem CRUA dela ao traceback, e é justamente ela que pode trazer a
        # API key. O que se perde em contexto já está no `resumo` (redigido) e
        # nos WARNINGs por provider logo acima — que é onde se depura isto.
        raise TodosProvidersFalharam(
            f"Nenhum provedor de IA respondeu ({len(falhas)} tentativa(s)): {resumo}"
        ) from None

    def invoke(self, mensagens: list[BaseMessage]) -> AIMessage:
        """Nome que `responder.py` chama (contrato `BaseChatModel`). Ver `generate`."""
        return self.generate(mensagens)


def _carimbar(resposta: AIMessage, provider: LLMProvider) -> AIMessage:
    """Registra na resposta quem a gerou.

    É por aqui que a telemetria descobre o provider sem que `responder.py`
    precise de um parâmetro novo: `Registro.somar_tokens` já lê a resposta do
    LLM nos dois pontos de chamada (base e web) e passou a ler estes dois campos
    junto. Sem isto, uma pergunta respondida pelo Groq apareceria na tabela
    `telemetria` como `chat_model: "gemini-3.6-flash"` — e o custo do fallback
    ficaria invisível justamente no dia em que ele mais importa.

    `response_metadata` é um dict que o LangChain já usa para metadado do
    provedor, então não há chave nova a inventar nem campo a serializar.
    """
    if isinstance(resposta, AIMessage):
        resposta.response_metadata = {
            **(resposta.response_metadata or {}),
            "provider": provider.nome,
            "provider_model": provider.modelo,
        }
    return resposta


# --- Montagem a partir da configuração --------------------------------------

# Uma entrada por provedor conhecido. O valor é a fábrica, e ela devolve `None`
# quando falta a chave — "sem chave" é ausência esperada (a instalação que só
# tem Gemini é a instalação normal), não erro de configuração. Cadeia sem
# NENHUM provider é que é erro, e quem levanta é o `ProviderChain.__init__`.
#
# Cada fábrica recebe um `modelo` opcional que SUBSTITUI o do `.env`. É o que
# sustenta o override por requisição (`cadeia_para_modelo`) sem duplicar a
# construção do cliente em dois lugares.
_FABRICAS: dict[str, Callable[[str | None], LLMProvider | None]] = {}


def _registrar(nome: str) -> Callable[[Callable[[str | None], LLMProvider | None]], Callable]:
    def decorador(fabrica):
        _FABRICAS[nome] = fabrica
        return fabrica

    return decorador


@_registrar("gemini")
def _gemini(modelo: str | None = None) -> LLMProvider | None:
    if not settings.chave_gemini:
        return None
    return GeminiProvider(
        api_key=settings.chave_gemini,
        modelo=modelo or settings.chat_model,
        timeout=settings.llm_timeout,
        tentativas=settings.llm_tentativas_por_provider,
    )


@_registrar("groq")
def _groq(modelo: str | None = None) -> LLMProvider | None:
    if not settings.groq_api_key:
        return None
    return GroqProvider(
        api_key=settings.groq_api_key,
        modelo=modelo or settings.groq_model,
        timeout=settings.llm_timeout,
        tentativas=settings.llm_tentativas_por_provider,
        base_url=settings.groq_base_url or GROQ_BASE_URL,
    )


@_registrar("openrouter")
def _openrouter(modelo: str | None = None) -> LLMProvider | None:
    if not settings.openrouter_api_key:
        return None
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key,
        modelo=modelo or settings.openrouter_model,
        timeout=settings.llm_timeout,
        tentativas=settings.llm_tentativas_por_provider,
        base_url=settings.openrouter_base_url or OPENROUTER_BASE_URL,
    )


def construir_providers() -> list[LLMProvider]:
    """Providers habilitados, na ordem de `LLM_PROVIDERS`.

    Habilitado = está na lista E tem chave. Nome desconhecido na lista vira
    WARNING e é ignorado, em vez de derrubar o boot: um typo no `.env` não
    deve tirar o serviço do ar quando os outros dois provedores estão de pé.
    """
    providers: list[LLMProvider] = []
    for nome in settings.llm_providers_lista:
        fabrica = _FABRICAS.get(nome)
        if fabrica is None:
            logger.warning(
                "LLM_PROVIDERS: provider desconhecido %r ignorado (conhecidos: %s)",
                nome,
                ", ".join(sorted(_FABRICAS)),
            )
            continue
        provider = fabrica(None)
        if provider is None:
            logger.info("provider %s fora da cadeia: chave de API não configurada", nome)
            continue
        providers.append(provider)
    return providers


def providers_conhecidos() -> list[str]:
    """Nomes que `LLM_PROVIDERS` aceita — o que existe, não o que está de pé."""
    return sorted(_FABRICAS)


def provider_avulso(nome: str, modelo: str | None = None) -> LLMProvider | None:
    """UM provider, fora de qualquer cadeia. `None` se não tem chave configurada.

    Para quem precisa falar com um provider específico sem o comportamento de
    fallback — hoje a CLI de catálogo (`scripts/modelos.py`). Público para que
    ela não precise alcançar `_FABRICAS`: o registro é detalhe interno, e um
    consumidor externo amarrado a ele congelaria a forma dele.
    """
    fabrica = _FABRICAS.get(nome.strip().casefold())
    return fabrica(modelo) if fabrica else None


class ModeloInvalido(ValueError):
    """Override de modelo malformado ou de provider que não está de pé.

    `ValueError` para cair no handler 422 que já existe (`app/api/errors.py`):
    é o PEDIDO que está errado, e a mensagem diz como corrigir. Distinto do 503
    de "serviço indisponível" de propósito — quem mandou `modelo=xpto:foo` não
    deve concluir que o agente caiu.
    """


def _separar_provider(spec: str) -> tuple[str, str]:
    """`[provider:]modelo` -> `(provider ou "", modelo)`.

    O prefixo só conta como provider se for um nome CONHECIDO — e essa regra não
    é preciosismo. Nome de modelo com `:` é comum justamente no provider onde o
    default tem um: `deepseek/deepseek-chat-v3.1:free`. Cortar no primeiro (ou
    no último) `:` sem checar transformaria esse nome em
    `provider="deepseek/deepseek-chat-v3.1"`, `modelo="free"` — e o operador
    receberia "provider desconhecido" para um modelo perfeitamente válido.

    O preço é que um provider digitado errado (`grok:` em vez de `groq:`) vira
    nome de modelo e cai como 404 em vez de 422. Aceitável: o 404 já sai em
    ERROR apontando `scripts.modelos`, enquanto o outro erro seria em um caminho
    que funciona todo dia.
    """
    prefixo, separador, resto = spec.strip().partition(":")
    if separador and prefixo.strip().casefold() in _FABRICAS:
        return prefixo.strip().casefold(), resto.strip()
    return "", spec.strip()


def cadeia_para_modelo(spec: str) -> ProviderChain:
    """Cadeia de UM provider só, com o modelo pedido. Formato: `[provider:]modelo`.

        groq:llama-3.1-8b-instant     -> Groq com esse modelo
        gemini-3.6-flash              -> primeiro provider da cadeia, com esse modelo

    Um provider só, e SEM fallback, de propósito. O override existe para
    responder "este modelo funciona / responde bem?", e uma cadeia que caísse
    para o próximo devolveria a resposta de OUTRO modelo com cara de sucesso —
    o pior resultado possível para a única pergunta que o override faz. Quem
    quer resiliência não manda `modelo`.

    Sem provider explícito, vale o primeiro de `LLM_PROVIDERS`: é o que a
    pergunta usaria de qualquer forma, então `modelo=X` sozinho significa "o de
    sempre, com outro modelo".
    """
    provider_pedido, modelo = _separar_provider(spec)

    if not modelo:
        raise ModeloInvalido(f"Modelo vazio em {spec!r}. Formato: `[provider:]modelo`.")

    if not provider_pedido:
        habilitados = settings.llm_providers_lista
        if not habilitados:
            raise ModeloInvalido("LLM_PROVIDERS está vazio; não há provider para usar.")
        provider_pedido = habilitados[0]
        if provider_pedido not in _FABRICAS:
            raise ModeloInvalido(
                f"O primeiro provider de LLM_PROVIDERS ({provider_pedido!r}) é desconhecido. "
                f"Válidos: {', '.join(sorted(_FABRICAS))}."
            )

    provider = _FABRICAS[provider_pedido](modelo)
    if provider is None:
        raise ModeloInvalido(
            f"Provider {provider_pedido!r} não tem chave de API configurada."
        )

    logger.info("override de modelo: %s (%s), sem fallback", provider.nome, provider.modelo)
    return ProviderChain([provider])


def modelo_padrao(nome: str) -> str:
    """Modelo que o `.env` manda usar neste provider — sem chamada nenhuma.

    Único lugar com este mapeamento: `scripts/modelos.py` e o endpoint
    `GET /v1/modelos` (borda HTTP da demo) usam esta função em vez de repetir a
    lista `chat_model`/`groq_model`/`openrouter_model` cada um por conta
    própria — duas cópias dessa lista é duas chances de esquecer de atualizar
    uma quando um quarto provider entrar.
    """
    return {
        "gemini": settings.chat_model,
        "groq": settings.groq_model,
        "openrouter": settings.openrouter_model,
    }.get(nome, "")


@lru_cache(maxsize=8)
def listar_modelos_cache(nome: str) -> tuple[str, ...]:
    """Catálogo de um provider, com cache pela vida do processo.

    `listar_modelos()` é uma chamada de rede ao provedor — cara demais para
    rodar a cada carga da demo (ver `GET /v1/modelos/{provider}` em
    `app/api/routers/v1.py`) ou a cada clique em "ver mais modelos". Mesma
    lógica de `get_chat_model`/`get_embeddings`: o catálogo não muda dentro da
    vida do processo, então cachear é seguro — reiniciar o processo é o jeito
    de ver um modelo novo que o provedor lançou.

    Levanta a exceção do provider sem tratar: quem chama decide o que virar
    (ver `v1.catalogo_modelos`, que usa `sem_segredo` antes de expor ao
    cliente). Não cachear a exceção é o comportamento padrão do `lru_cache`
    quando a função levanta — uma falha de rede passageira não fica presa.
    """
    provider = provider_avulso(nome)
    if provider is None:
        raise ModeloInvalido(f"Provider {nome!r} não tem chave de API configurada.")
    return tuple(provider.listar_modelos())


def chaves_por_provider() -> dict[str, bool]:
    """`{nome: tem chave}` para os providers habilitados. Usado pelo `/v1/ready`.

    Só a PRESENÇA da chave: validar de verdade custaria uma chamada paga a cada
    probe. Chave inválida aparece como fallback no log e, se todos falharem,
    como 503 no `/ask`.
    """
    chaves = {
        "gemini": settings.chave_gemini,
        "groq": settings.groq_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    return {
        nome: bool(chaves.get(nome))
        for nome in settings.llm_providers_lista
        if nome in chaves
    }


@lru_cache(maxsize=1)
def get_chat_model() -> ProviderChain:
    """A cadeia do processo. Mesmo nome de antes: `responder.py` não mudou.

    `lru_cache` porque construir os clientes é caro (sessão HTTP, validação de
    parâmetro) e a cadeia não tem estado por pergunta. Exceção não é cacheada
    pelo `lru_cache`, então um processo que sobe sem chave e recebe a chave
    depois de um restart não fica preso no erro.
    """
    cadeia = ProviderChain(construir_providers())
    logger.info(
        "cadeia de LLM: %s",
        " -> ".join(f"{p.nome}({p.modelo})" for p in cadeia.providers),
    )
    return cadeia
