"""Testes da cadeia de fallback entre provedores de LLM.

Nenhum destes testes toca em rede, banco ou chave de API real: os providers são
dublês que respondem ou levantam o que o teste mandar. É de propósito — o valor
da cadeia está na REGRA (quando cair, quando propagar, quantas vezes tentar), e
regra se testa sem SDK no meio.
"""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core import telemetry
from app.core.config import Settings
from app.providers import chain as chain_mod
from app.providers.base import (
    LLMProvider,
    TodosProvidersFalharam,
    motivo_de_fallback,
)
from app.providers.chain import ProviderChain, construir_providers, sem_segredo

MENSAGENS = [HumanMessage(content="como envio a atividade?")]


class ErroHTTP(Exception):
    """Exceção com `status_code`, no formato do `openai.APIStatusError`."""

    def __init__(self, status: int, mensagem: str = ""):
        self.status_code = status
        super().__init__(mensagem or f"Error code: {status}")


class FakeProvider(LLMProvider):
    """Responde `resposta`, ou levanta `erro`. Conta as chamadas recebidas.

    A contagem é o que prova o requisito "1 tentativa por provider": sem ela,
    um retry interno acidental passaria em todos os outros testes.
    """

    def __init__(self, nome, resposta="ok", erro=None, modelo=None):
        self.nome = nome
        self.modelo = modelo or f"modelo-{nome}"
        self.resposta = resposta
        self.erro = erro
        self.chamadas = 0

    def generate(self, mensagens):
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        return AIMessage(
            content=self.resposta,
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


def _cadeia(*providers) -> ProviderChain:
    return ProviderChain(providers)


# --- O caminho feliz e a cadeia caindo --------------------------------------


def test_primeiro_provider_responde_e_os_outros_nao_sao_tocados():
    gemini = FakeProvider("gemini", resposta="Resposta do Gemini.")
    groq = FakeProvider("groq")
    openrouter = FakeProvider("openrouter")

    resposta = _cadeia(gemini, groq, openrouter).invoke(MENSAGENS)

    assert resposta.content == "Resposta do Gemini."
    assert (gemini.chamadas, groq.chamadas, openrouter.chamadas) == (1, 0, 0)


def test_quota_do_primeiro_cai_para_o_segundo():
    gemini = FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded"))
    groq = FakeProvider("groq", resposta="Resposta do Groq.")
    openrouter = FakeProvider("openrouter")

    resposta = _cadeia(gemini, groq, openrouter).invoke(MENSAGENS)

    assert resposta.content == "Resposta do Groq."
    assert (gemini.chamadas, groq.chamadas, openrouter.chamadas) == (1, 1, 0)


def test_falha_dos_dois_primeiros_cai_ate_o_terceiro():
    """O requisito central: provider 1 e 2 fora, a pergunta é respondida pelo 3.

    Dois motivos DIFERENTES de propósito (cota no primeiro, credencial no
    segundo) — a cadeia não pode depender de os gatilhos serem do mesmo tipo.
    """
    gemini = FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded"))
    groq = FakeProvider("groq", erro=ErroHTTP(401, "Invalid API Key"))
    openrouter = FakeProvider("openrouter", resposta="Resposta do OpenRouter.")

    resposta = _cadeia(gemini, groq, openrouter).invoke(MENSAGENS)

    assert resposta.content == "Resposta do OpenRouter."
    assert (gemini.chamadas, groq.chamadas, openrouter.chamadas) == (1, 1, 1)


def test_cada_provider_e_tentado_uma_unica_vez():
    """Fallback não é retry: nenhum provider pode ser chamado duas vezes.

    Sem isto, um `retry` acidental dentro da cadeia multiplicaria o pior caso de
    latência pelo número de tentativas — e o aluno esperaria minutos por uma
    resposta que a cadeia já sabia que não viria.
    """
    providers = [
        FakeProvider("gemini", erro=ErroHTTP(429)),
        FakeProvider("groq", erro=ErroHTTP(503)),
        FakeProvider("openrouter", erro=ErroHTTP(500)),
    ]

    with pytest.raises(TodosProvidersFalharam):
        _cadeia(*providers).invoke(MENSAGENS)

    assert [p.chamadas for p in providers] == [1, 1, 1]


def test_todos_falharam_vira_erro_de_servico_indisponivel():
    """`TodosProvidersFalharam` é `RuntimeError` para cair no handler 503 que já
    existe em `app/api/errors.py` — o contrato de `/ask` não ganha código novo."""
    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429)),
        FakeProvider("groq", erro=ErroHTTP(500)),
    )

    with pytest.raises(TodosProvidersFalharam) as exc:
        cadeia.invoke(MENSAGENS)

    assert isinstance(exc.value, RuntimeError)
    # A mensagem tem que dizer quem falhou e por quê: é o que o operador lê no
    # 503 antes de ir ao log.
    assert "gemini" in str(exc.value) and "groq" in str(exc.value)
    assert "429" in str(exc.value) and "500" in str(exc.value)


# --- A regra: o que cai para o próximo e o que propaga ----------------------


@pytest.mark.parametrize(
    "erro",
    [
        ErroHTTP(429, "rate limit"),  # cota
        ErroHTTP(401, "unauthorized"),  # credencial
        ErroHTTP(403, "forbidden"),  # token expirado / sem permissão
        ErroHTTP(402, "insufficient credits"),  # o `:free` do OpenRouter
        ErroHTTP(408, "request timeout"),
        ErroHTTP(500, "internal error"),
        ErroHTTP(502, "bad gateway"),
        ErroHTTP(503, "service unavailable"),
        TimeoutError("deadline"),
        ConnectionError("connection reset"),
    ],
    ids=lambda e: f"{type(e).__name__}-{getattr(e, 'status_code', '')}",
)
def test_gatilhos_de_indisponibilidade_caem_para_o_proximo(erro):
    primeiro = FakeProvider("gemini", erro=erro)
    segundo = FakeProvider("groq", resposta="Resposta do Groq.")

    assert _cadeia(primeiro, segundo).invoke(MENSAGENS).content == "Resposta do Groq."
    assert segundo.chamadas == 1


@pytest.mark.parametrize(
    "erro",
    [
        ErroHTTP(400, "Invalid prompt"),  # conteúdo/validação
        ErroHTTP(422, "unprocessable"),
        ValueError("pergunta vazia"),
    ],
    ids=lambda e: f"{type(e).__name__}-{getattr(e, 'status_code', '')}",
)
def test_erro_do_pedido_propaga_sem_tentar_o_proximo(erro):
    """Nenhum outro provider aceitaria o mesmo pedido inválido — tentar seria
    gastar três chamadas e três timeouts para chegar ao mesmo lugar, e ainda
    apagar o erro que o desenvolvedor precisa ver, trocando-o por um 503."""
    primeiro = FakeProvider("gemini", erro=erro)
    segundo = FakeProvider("groq")

    with pytest.raises(type(erro)):
        _cadeia(primeiro, segundo).invoke(MENSAGENS)

    assert segundo.chamadas == 0


MENSAGEM_404_GROQ = (
    "Error code: 404 - {'error': {'message': 'The model `llama-3.3-70b-versatile` does "
    "not exist or you do not have access to it', 'type': 'invalid_request_error', "
    "'code': 'model_not_found'}}"
)


def test_modelo_inexistente_cai_para_o_proximo_em_vez_de_derrubar_o_ask():
    """Bug real: `GROQ_MODEL` fora do catálogo derrubava `/ask` inteiro.

    404 é erro de configuração DAQUELE provider, não do pedido — o prompt está
    perfeito e o próximo provedor o aceita. Propagar (como um 400) fazia um nome
    de modelo errado tirar o serviço do ar com dois providers saudáveis, que é
    exatamente o que a cadeia existe para evitar.
    """
    groq = FakeProvider("groq", erro=ErroHTTP(404, MENSAGEM_404_GROQ))
    openrouter = FakeProvider("openrouter", resposta="Resposta do OpenRouter.")

    resultado = _cadeia(groq, openrouter).invoke(MENSAGENS)

    assert resultado.content == "Resposta do OpenRouter."
    assert openrouter.chamadas == 1


def test_modelo_inexistente_e_logado_como_erro_e_nao_como_aviso(caplog):
    """ERROR, não WARNING: cair para o próximo atende o aluno, mas esperar não
    conserta — nenhuma requisição futura vai funcionar melhor. Em WARNING isso
    ficaria meses invisível, porque a resposta sempre chega."""
    cadeia = _cadeia(
        FakeProvider("groq", erro=ErroHTTP(404, MENSAGEM_404_GROQ)),
        FakeProvider("openrouter", resposta="ok"),
    )

    with caplog.at_level(logging.INFO, logger="app.providers.chain"):
        cadeia.invoke(MENSAGENS)

    erros = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(erros) == 1
    assert "groq" in erros[0].getMessage()
    # A mensagem tem que dizer o que FAZER: a do provedor é ambígua por natureza
    # ("does not exist OR you do not have access"), e as duas causas se resolvem
    # olhando o catálogo da própria chave.
    assert "scripts.modelos" in erros[0].getMessage()


def test_indisponibilidade_passageira_continua_em_warning(caplog):
    """A contrapartida do teste acima: 429 não pode virar ERROR, senão o sinal
    de configuração quebrada se perde no meio do ruído de cota."""
    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded")),
        FakeProvider("groq", resposta="ok"),
    )

    with caplog.at_level(logging.INFO, logger="app.providers.chain"):
        cadeia.invoke(MENSAGENS)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_excecao_desconhecida_propaga_em_vez_de_mascarar():
    """A regra padrão é propagar: um bug nosso não pode virar 503 silencioso
    depois de três chamadas pagas."""

    class BugInterno(Exception):
        pass

    segundo = FakeProvider("groq")
    with pytest.raises(BugInterno):
        _cadeia(FakeProvider("gemini", erro=BugInterno("None não tem .content")), segundo).invoke(
            MENSAGENS
        )
    assert segundo.chamadas == 0


def test_status_no_texto_da_excecao_tambem_e_reconhecido():
    """Nem todo SDK entrega `status_code` estruturado; alguns só têm a mensagem."""

    class ErroCru(Exception):
        pass

    assert motivo_de_fallback(ErroCru("Error code: 429 - Quota exceeded")) is not None
    assert motivo_de_fallback(ErroCru("Error code: 400 - Invalid prompt")) is None
    # Um número de 3 dígitos solto NÃO é status: sem isso, "prompt tem 400
    # tokens a mais" acionaria a cadeia inteira por um erro de validação.
    assert motivo_de_fallback(ErroCru("token 429 do documento é inválido")) is None


def test_400_de_contexto_excedido_cai_para_o_proximo():
    """Estouro de janela de contexto é por MODELO, não por formato do pedido.

    Diferente de "Invalid prompt" (que os três provedores rejeitariam igual),
    o mesmo `mensagens` que estoura um modelo pequeno pode caber num com janela
    maior — então este 400 específico autoriza a cadeia a tentar o próximo.
    """
    mensagem = "Error code: 400 - Please reduce the length of the messages or completion."
    assert motivo_de_fallback(ErroHTTP(400, mensagem)) is not None
    # Também reconhecido sem `status_code` estruturado (SDK só com a mensagem).
    assert motivo_de_fallback(Exception(mensagem)) is not None

    segundo = FakeProvider("groq")
    resposta = _cadeia(FakeProvider("gemini", erro=ErroHTTP(400, mensagem)), segundo).invoke(
        MENSAGENS
    )
    assert resposta.response_metadata["provider"] == "groq"
    assert segundo.chamadas == 1


def test_413_request_too_large_cai_para_o_proximo():
    """`request_too_large` (HTTP 413) é teto de tokens por requisição do MODELO/
    tier, não erro de formato do pedido — mesma lógica do 400 de contexto
    excedido. Bug real: a Groq free-tier devolvia isso e o `APIStatusError:
    Error code: 413` cru subia para `/ask` em vez de a cadeia tentar o Gemini.
    """
    mensagem = (
        "Error code: 413 - {'error': {'message': 'Request Entity Too Large', "
        "'type': 'invalid_request_error', 'code': 'request_too_large'}}"
    )
    assert motivo_de_fallback(ErroHTTP(413, mensagem)) is not None
    # Também reconhecido sem `status_code` estruturado (SDK só com a mensagem).
    assert motivo_de_fallback(Exception(mensagem)) is not None

    segundo = FakeProvider("gemini", resposta="Resposta do Gemini.")
    resposta = _cadeia(FakeProvider("groq", erro=ErroHTTP(413, mensagem)), segundo).invoke(
        MENSAGENS
    )
    assert resposta.response_metadata["provider"] == "gemini"
    assert segundo.chamadas == 1


def test_cancelled_499_do_gemini_cai_para_o_proximo():
    """gRPC `Cancelled` do Gemini: chega como nome de exceção com "499" no texto,
    sem `status_code` estruturado e sem o prefixo "error code". É transporte
    cortado, não pedido inválido — INF-6: Q14/Q16 de 2026-08-28 morriam porque
    o `Cancelled: 499` cru subia em vez de a cadeia tentar o próximo provedor.
    """

    class Cancelled(Exception):
        pass

    assert motivo_de_fallback(Cancelled("Cancelled: 499")) is not None
    assert motivo_de_fallback(ErroHTTP(499, "client closed request")) is not None

    segundo = FakeProvider("groq", resposta="Resposta do Groq.")
    resposta = _cadeia(
        FakeProvider("gemini", erro=Cancelled("Cancelled: 499")), segundo
    ).invoke(MENSAGENS)
    assert resposta.response_metadata["provider"] == "groq"
    assert segundo.chamadas == 1


def test_nome_da_excecao_vale_quando_nao_ha_status():
    """Timeout e falha de conexão nunca chegam com resposta HTTP para ler."""

    class DeadlineExceeded(Exception):
        pass

    class InvalidArgument(Exception):
        pass

    assert motivo_de_fallback(DeadlineExceeded("504.5s")) is not None
    assert motivo_de_fallback(InvalidArgument("prompt inválido")) is None


# --- Log: quem respondeu, quem falhou, e nunca a chave ----------------------


def test_log_registra_quem_respondeu_e_quem_falhou(caplog):
    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded")),
        FakeProvider("groq", resposta="Resposta do Groq.", modelo="llama-3.3-70b-versatile"),
    )

    with caplog.at_level(logging.INFO, logger="app.providers.chain"):
        cadeia.invoke(MENSAGENS)

    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]

    # WARNING: quem falhou e por quê.
    assert len(avisos) == 1
    assert "gemini" in avisos[0].getMessage()
    assert "429" in avisos[0].getMessage()

    # INFO: quem respondeu, com o modelo — é o que responde "de onde veio esta
    # resposta?" sem precisar de traceback.
    assert any(
        "groq" in r.getMessage() and "llama-3.3-70b-versatile" in r.getMessage() for r in infos
    )


def test_log_nao_vaza_a_api_key(caplog, monkeypatch):
    """SDK que ecoa a credencial na mensagem de erro é comum. O log vai para
    stderr, para o agregador e para o backup antes de alguém notar — vazamento
    aqui é irreversível."""
    chave = "AIzaSyD-EXEMPLO-DE-CHAVE-QUE-NAO-PODE-VAZAR"
    monkeypatch.setattr(chain_mod.settings, "gemini_api_key", chave)

    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(401, f"API key not valid: key={chave}")),
        FakeProvider("groq", resposta="ok"),
    )

    with caplog.at_level(logging.INFO, logger="app.providers.chain"):
        cadeia.invoke(MENSAGENS)

    assert chave not in caplog.text
    assert "***" in caplog.text


def test_sem_segredo_redige_chave_de_qualquer_provedor():
    """Mesmo a que este processo não conhece: um erro proxeado pode trazer a
    credencial de outro ambiente."""
    texto = "falha com sk-or-v1-abcdef0123456789 e gsk_ABCDEF0123456789xyz"

    limpo = sem_segredo(texto)

    assert "sk-or-v1-abcdef0123456789" not in limpo
    assert "gsk_ABCDEF0123456789xyz" not in limpo
    assert limpo.count("***") == 2


# --- Carimbo na resposta (telemetria) ---------------------------------------


def test_resposta_diz_qual_provider_a_gerou():
    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429)),
        FakeProvider("groq", resposta="ok", modelo="llama-3.3-70b-versatile"),
    )

    resposta = cadeia.invoke(MENSAGENS)

    assert resposta.response_metadata["provider"] == "groq"
    assert resposta.response_metadata["provider_model"] == "llama-3.3-70b-versatile"


def test_telemetria_grava_o_provider_que_de_fato_respondeu():
    """Sem isto, uma pergunta respondida pelo Groq apareceria na tabela como
    `chat_model: gemini-3.6-flash` — e o custo do fallback ficaria invisível
    justamente no dia em que ele mais importa."""
    registro = telemetry.Registro(
        canal="teste", assunto=None, pergunta_hash="abc", chat_model="gemini-3.6-flash"
    )
    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429)),
        FakeProvider("groq", resposta="ok", modelo="llama-3.3-70b-versatile"),
    )

    registro.somar_tokens(cadeia.invoke(MENSAGENS))

    assert registro.provider == "groq"
    assert registro.chat_model == "llama-3.3-70b-versatile"
    assert (registro.input_tokens, registro.output_tokens) == (10, 5)


def test_llm_sem_carimbo_nao_apaga_o_modelo_configurado():
    """Dublê de teste e provedor que não reporta metadado continuam válidos."""
    registro = telemetry.Registro(
        canal="teste", assunto=None, pergunta_hash="abc", chat_model="gemini-3.6-flash"
    )

    registro.somar_tokens(AIMessage(content="resposta sem metadado"))

    assert registro.provider is None
    assert registro.chat_model == "gemini-3.6-flash"


# --- Montagem da cadeia a partir do .env ------------------------------------


@pytest.fixture
def config(monkeypatch):
    """Settings isolada do `.env` da máquina: estes testes decidem cada chave.

    Sem isto, o resultado dependeria de quais provedores o desenvolvedor tem
    configurado localmente — e o teste passaria na máquina dele e falharia no CI.
    """
    cfg = Settings(
        _env_file=None,
        gemini_api_key="chave-gemini",
        groq_api_key="chave-groq",
        openrouter_api_key="chave-openrouter",
    )
    monkeypatch.setattr(chain_mod, "settings", cfg)
    return cfg


def _nomes(providers):
    return [p.nome for p in providers]


def test_ordem_padrao_da_cadeia(config):
    """Sem HF_TOKEN configurado (a fixture não seta), `huggingface` sai da
    cadeia sozinho — igual a Groq/OpenRouter sem chave."""
    assert _nomes(construir_providers()) == ["gemini", "groq", "openrouter"]


def test_teto_de_tokens_de_saida_chega_a_todo_provider(config, monkeypatch):
    """VET-3: `LLM_MAX_TOKENS` tem que ser aplicado em TODA a cadeia, não só no
    primeiro elo — senão o fallback para Groq/OpenRouter continua sem teto de
    saída. Os dois SDKs usam nomes diferentes: `max_output_tokens` no
    langchain-google-genai, `max_tokens` (por chamada) no da OpenAI."""
    monkeypatch.setattr(config, "hf_token", "chave-hf")
    monkeypatch.setattr(config, "llm_max_tokens", 1400)

    por_nome = {p.nome: p for p in construir_providers()}

    assert por_nome["gemini"]._llm.max_output_tokens == 1400
    for nome in ("huggingface", "groq", "openrouter"):
        assert por_nome[nome].max_tokens == 1400


def test_huggingface_entra_como_segundo_elo_quando_tem_token(config, monkeypatch):
    """Com HF_TOKEN preenchido, `huggingface` ocupa o 2º lugar da ordem padrão —
    entre Gemini e Groq, reaproveitando a mesma chave dos embeddings locais."""
    monkeypatch.setattr(config, "hf_token", "chave-hf")

    assert _nomes(construir_providers()) == ["gemini", "huggingface", "groq", "openrouter"]


def test_ordem_e_reconfiguravel_sem_mexer_em_codigo(config, monkeypatch):
    monkeypatch.setattr(config, "llm_providers", "groq,gemini,openrouter")

    assert _nomes(construir_providers()) == ["groq", "gemini", "openrouter"]


def test_provider_fora_de_llm_providers_nao_entra(config, monkeypatch):
    monkeypatch.setattr(config, "llm_providers", "gemini,openrouter")

    assert _nomes(construir_providers()) == ["gemini", "openrouter"]


def test_provider_sem_chave_sai_da_cadeia_sozinho(config, monkeypatch):
    """A instalação que só tem Gemini é a instalação normal — não configurar
    Groq/OpenRouter é ausência esperada, não erro de configuração."""
    monkeypatch.setattr(config, "groq_api_key", "")
    monkeypatch.setattr(config, "openrouter_api_key", "")

    assert _nomes(construir_providers()) == ["gemini"]


def test_nome_desconhecido_e_ignorado_sem_derrubar_o_boot(config, monkeypatch, caplog):
    """Um typo no .env não pode tirar o serviço do ar enquanto há provedor de pé."""
    monkeypatch.setattr(config, "llm_providers", "gemini,gemin,groq")

    with caplog.at_level(logging.WARNING, logger="app.providers.chain"):
        providers = construir_providers()

    assert _nomes(providers) == ["gemini", "groq"]
    assert "gemin" in caplog.text


def test_provider_repetido_nao_e_tentado_duas_vezes(config, monkeypatch):
    """`gemini,groq,gemini` faria a mesma chave 429 ser tentada de novo antes do
    Groq — exatamente o retry que a cadeia existe para não fazer."""
    monkeypatch.setattr(config, "llm_providers", "gemini,groq,gemini")

    assert _nomes(construir_providers()) == ["gemini", "groq"]


def test_gemini_api_key_tem_precedencia_sobre_o_nome_legado():
    cfg = Settings(_env_file=None, gemini_api_key="nova", google_api_key="antiga")
    assert cfg.chave_gemini == "nova"


def test_google_api_key_continua_valendo(config, monkeypatch):
    """`.env` existente não pode quebrar por causa do nome novo."""
    monkeypatch.setattr(config, "gemini_api_key", "")
    monkeypatch.setattr(config, "google_api_key", "chave-legada")

    assert "gemini" in _nomes(construir_providers())


def test_cadeia_sem_nenhum_provider_e_erro_de_servico_indisponivel(config, monkeypatch):
    """Mesma resposta (503) que a ausência de `GOOGLE_API_KEY` já dava antes."""
    for chave in ("gemini_api_key", "google_api_key", "groq_api_key", "openrouter_api_key"):
        monkeypatch.setattr(config, chave, "")

    with pytest.raises(RuntimeError, match="Nenhum provider de LLM configurado"):
        ProviderChain(construir_providers())


def test_ready_reporta_a_chave_por_provider_habilitado(config, monkeypatch):
    monkeypatch.setattr(config, "openrouter_api_key", "")

    assert chain_mod.chaves_por_provider() == {
        "gemini": True,
        "huggingface": False,
        "groq": True,
        "openrouter": False,
    }


# --- Escolha do modelo por requisição ---------------------------------------


def test_override_usa_o_provider_e_o_modelo_pedidos(config):
    cadeia = chain_mod.cadeia_para_modelo("groq:llama-3.1-8b-instant")

    assert [(p.nome, p.modelo) for p in cadeia.providers] == [
        ("groq", "llama-3.1-8b-instant")
    ]


def test_override_sem_provider_usa_o_primeiro_da_cadeia(config, monkeypatch):
    """`modelo=X` sozinho = "o provider de sempre, com outro modelo"."""
    monkeypatch.setattr(config, "llm_providers", "groq,gemini")

    cadeia = chain_mod.cadeia_para_modelo("llama-3.1-8b-instant")

    assert [(p.nome, p.modelo) for p in cadeia.providers] == [
        ("groq", "llama-3.1-8b-instant")
    ]


def test_override_nao_tem_fallback(config):
    """Um provider só, de propósito: o override existe para responder "este
    modelo funciona?", e uma cadeia que caísse para o próximo devolveria a
    resposta de OUTRO modelo com cara de sucesso — o pior resultado possível
    para a única pergunta que o override faz."""
    assert len(chain_mod.cadeia_para_modelo("gemini:gemini-3.6-flash").providers) == 1


def test_modelo_com_dois_pontos_no_nome_nao_e_confundido_com_provider(config):
    """`deepseek/deepseek-chat-v3.1:free` — o `:` faz parte do NOME do modelo.

    Cortar no primeiro (ou no último) `:` sem checar se o prefixo é um provider
    conhecido faria o default do OpenRouter virar
    `provider="deepseek/deepseek-chat-v3.1"`, e o operador receberia "provider
    desconhecido" para um modelo perfeitamente válido.
    """
    livre = "deepseek/deepseek-chat-v3.1:free"

    com_prefixo = chain_mod.cadeia_para_modelo(f"openrouter:{livre}").providers[0]
    sem_prefixo = chain_mod.cadeia_para_modelo(livre).providers[0]

    assert (com_prefixo.nome, com_prefixo.modelo) == ("openrouter", livre)
    # Sem prefixo cai no primeiro da cadeia (gemini), mas o NOME do modelo tem
    # que chegar inteiro — é isso que o corte ingênuo quebrava.
    assert sem_prefixo.modelo == livre


def test_override_sem_cadeia_configurada_e_erro_do_pedido(config, monkeypatch):
    monkeypatch.setattr(config, "llm_providers", "")

    with pytest.raises(chain_mod.ModeloInvalido, match="LLM_PROVIDERS"):
        chain_mod.cadeia_para_modelo("llama-3.1-8b-instant")


@pytest.mark.parametrize("spec", ["", "   ", "groq:", "groq:   "])
def test_override_sem_nome_de_modelo_e_erro_do_pedido(config, spec):
    with pytest.raises(chain_mod.ModeloInvalido, match="[Mm]odelo vazio"):
        chain_mod.cadeia_para_modelo(spec)


def test_override_de_provider_sem_chave_e_erro_do_pedido(config, monkeypatch):
    monkeypatch.setattr(config, "groq_api_key", "")

    with pytest.raises(chain_mod.ModeloInvalido, match="chave de API"):
        chain_mod.cadeia_para_modelo("groq:llama-3.1-8b-instant")


def test_modelo_invalido_e_erro_do_pedido_e_nao_do_servico():
    """`ValueError` para virar 422, e não o 503 de `TodosProvidersFalharam`:
    quem mandou um modelo errado não deve concluir que o agente caiu."""
    assert issubclass(chain_mod.ModeloInvalido, ValueError)


def test_override_nao_le_nem_grava_no_cache(monkeypatch):
    """A resposta de um modelo experimental não pode ser servida ao próximo
    aluno que fizer a mesma pergunta — mesma classe de bug que a T2.4 fechou."""
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.config import settings
    from app.core.models import Query, RetrievedChunk

    # Explícito, e não confiado no default: `settings` é lido do `.env` real do
    # projeto, e este teste verifica o comportamento com o switch DESLIGADO —
    # precisa valer isso, independente do que estiver configurado na máquina.
    monkeypatch.setattr(settings, "modelo_override_cache_enabled", False)
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])

    gravado: dict[str, str] = {}
    monkeypatch.setattr(responder, "get_cached_answer", gravado.get)
    monkeypatch.setattr(
        responder, "set_cached_answer", lambda k, a, r, m=None: gravado.__setitem__(k, r)
    )

    pergunta = "como envio a atividade?"
    experimental = _cadeia(FakeProvider("groq", resposta="Resposta experimental."))
    responder.answer(Query(text=pergunta, modelo="groq:x"), llm=experimental)

    assert gravado == {}  # nada foi gravado sob a chave da pergunta

    # E a pergunta seguinte, SEM override, chama o LLM normal em vez de receber
    # a resposta do experimental.
    normal = FakeProvider("gemini", resposta="Resposta homologada.")
    resultado = responder.answer(Query(text=pergunta), llm=_cadeia(normal))

    assert resultado.text == "Resposta homologada."
    assert normal.chamadas == 1


def _cache_em_memoria(monkeypatch):
    from app.agent import responder

    armazenado: dict[str, str] = {}
    monkeypatch.setattr(responder, "get_cached_answer", armazenado.get)
    monkeypatch.setattr(
        responder, "set_cached_answer", lambda k, a, r, m=None: armazenado.__setitem__(k, r)
    )
    return armazenado


def test_modelo_override_cache_enabled_serve_do_cache_no_override_repetido(monkeypatch):
    """Com o switch ligado, repetir a MESMA pergunta com o MESMO override serve
    do cache em vez de pagar o LLM de novo — o ganho que o usuário pediu."""
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.config import settings
    from app.core.models import Query, RetrievedChunk

    monkeypatch.setattr(settings, "modelo_override_cache_enabled", True)
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    _cache_em_memoria(monkeypatch)

    pergunta = "como envio a atividade?"
    primeiro = FakeProvider("groq", resposta="Resposta do Qwen.")
    responder.answer(Query(text=pergunta, modelo="groq:qwen"), llm=_cadeia(primeiro))

    segundo = FakeProvider("groq", resposta="NUNCA deveria aparecer.")
    resultado = responder.answer(Query(text=pergunta, modelo="groq:qwen"), llm=_cadeia(segundo))

    assert resultado.text == "Resposta do Qwen."
    assert resultado.cached is True
    assert segundo.chamadas == 0  # serviu do cache, não chamou o LLM de novo


def test_modelo_override_cache_enabled_nao_mistura_modelos_diferentes(monkeypatch):
    """O motivo de ser seguro ligar o switch: o modelo entra na chave, então
    `groq:x` e `gemini:y` nunca compartilham entrada — mesmo com pergunta e
    chunks idênticos, que foi exatamente o caso real que gerou a dúvida."""
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.config import settings
    from app.core.models import Query, RetrievedChunk

    monkeypatch.setattr(settings, "modelo_override_cache_enabled", True)
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    _cache_em_memoria(monkeypatch)

    pergunta = "como envio a atividade?"
    gemini = FakeProvider("gemini", resposta="Resposta do Gemini.")
    responder.answer(Query(text=pergunta, modelo="gemini:g"), llm=_cadeia(gemini))

    groq = FakeProvider("groq", resposta="Resposta do Groq.")
    resultado = responder.answer(Query(text=pergunta, modelo="groq:q"), llm=_cadeia(groq))

    assert resultado.text == "Resposta do Groq."
    assert groq.chamadas == 1  # NÃO serviu do cache do Gemini


def test_modelo_override_cache_enabled_nao_mistura_com_a_cadeia_normal(monkeypatch):
    """A mesma pergunta, uma vez com override e uma vez sem, não pode
    compartilhar cache — senão a cadeia normal (que atende o aluno de verdade)
    ficaria sujeita ao resultado de um teste de modelo experimental."""
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.config import settings
    from app.core.models import Query, RetrievedChunk

    monkeypatch.setattr(settings, "modelo_override_cache_enabled", True)
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    _cache_em_memoria(monkeypatch)

    pergunta = "como envio a atividade?"
    experimental = FakeProvider("groq", resposta="Resposta experimental.")
    responder.answer(Query(text=pergunta, modelo="groq:x"), llm=_cadeia(experimental))

    normal = FakeProvider("gemini", resposta="Resposta homologada.")
    resultado = responder.answer(Query(text=pergunta), llm=_cadeia(normal))

    assert resultado.text == "Resposta homologada."
    assert normal.chamadas == 1


# --- A cadeia no lugar do LLM único -----------------------------------------


def test_agente_responde_pelo_fallback_sem_saber_que_houve_fallback(monkeypatch):
    """O requisito de transparência, provado no ponto onde ele importa.

    `responder.answer` continua chamando `llm.invoke(mensagens)` e recebendo uma
    `AIMessage` — é exatamente o que fazia com um provedor só. Quem consome
    `/ask` recebe a mesma `Answer`, com as mesmas fontes e o mesmo `grounded`,
    tenha respondido o Gemini ou o terceiro da fila.
    """
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.models import Query, RetrievedChunk

    chunk = RetrievedChunk(
        document=Document(
            id="c1",
            page_content="Para enviar a atividade, acesse Tarefas.",
            metadata={"source_name": "guia.pdf", "page": 0},
        ),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    monkeypatch.setattr(responder, "get_cached_answer", lambda chave: None)
    monkeypatch.setattr(responder, "set_cached_answer", lambda *args: None)

    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded")),
        FakeProvider("groq", erro=ErroHTTP(503, "Service unavailable")),
        FakeProvider("openrouter", resposta="Acesse Tarefas e clique em Enviar."),
    )

    resultado = responder.answer(Query(text="como envio atividade?"), llm=cadeia)

    assert resultado.text == "Acesse Tarefas e clique em Enviar."
    assert resultado.origem == "base"
    assert resultado.grounded is True
    assert [c.citation for c in resultado.sources] == ["guia.pdf, p. 1"]


def test_repeticao_apos_fallback_serve_do_cache_sem_chamar_nenhum_provider(monkeypatch):
    """A dúvida real que motivou este teste: "se o modelo X que respondeu antes
    ficar indisponível, a mesma pergunta repetida vai reprocessar e consumir API
    de novo?" — não, e a razão está em ONDE `_cache_key` é checada.

    Em `_tentar_base` (app/agent/responder.py), o cache é lido ANTES de
    `_resolver_llm`/`ProviderChain.generate` serem sequer chamados. A chave, por
    sua vez, não guarda QUEM respondeu (só pergunta + assunto + chunks — ver o
    docstring de `_cache_key`), então uma pergunta repetida bate na mesma chave
    independente de qual provider da cadeia a atendeu da primeira vez.

    Resultado: 1ª chamada cai no fallback (Gemini falha, Groq responde) e grava
    no cache; na 2ª chamada, IDÊNTICA, a cadeia inteira nem é tocada — nenhum
    provider (nem o que respondeu, nem o que falhou) recebe uma nova chamada.
    """
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.models import Query, RetrievedChunk

    chunk = RetrievedChunk(
        document=Document(
            id="c1", page_content="Para enviar a atividade, acesse Tarefas.",
            metadata={"source_name": "guia.pdf", "page": 0},
        ),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    armazenado = _cache_em_memoria(monkeypatch)

    pergunta = "como envio atividade?"

    # 1ª chamada: Gemini indisponível, Groq responde e a resposta é cacheada.
    gemini_1 = FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded"))
    groq_1 = FakeProvider("groq", resposta="Acesse Tarefas e clique em Enviar.")
    primeiro = responder.answer(Query(text=pergunta), llm=_cadeia(gemini_1, groq_1))

    assert primeiro.text == "Acesse Tarefas e clique em Enviar."
    assert primeiro.cached is False
    assert armazenado  # a chave foi gravada

    # 2ª chamada, MESMA pergunta: monta uma cadeia nova em que os DOIS
    # providers -- inclusive o Groq que respondeu antes -- levantariam exceção
    # se fossem chamados. Se o cache não interceptar antes da cadeia, o teste
    # falha aqui (a exceção subiria de `ProviderChain.generate`).
    def nao_deveria_ser_chamado(mensagens):
        raise AssertionError("cache deveria ter respondido sem tocar em nenhum provider")

    gemini_2 = FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded"))
    groq_2 = FakeProvider("groq")
    groq_2.generate = nao_deveria_ser_chamado

    segundo = responder.answer(Query(text=pergunta), llm=_cadeia(gemini_2, groq_2))

    assert segundo.text == "Acesse Tarefas e clique em Enviar."
    assert segundo.cached is True
    assert gemini_2.chamadas == 0
    assert groq_2.chamadas == 0


def test_cache_da_cadeia_normal_nao_registra_qual_provider_respondeu(monkeypatch):
    """O reverso do teste acima, e o trade-off que vale o usuário saber: a
    chave do cache (caminho SEM `--modelo`) não inclui qual provider respondeu
    — só a pergunta e os chunks. Então, se o Groq responder hoje (por exemplo,
    com o Gemini fora), essa resposta fica "congelada" no cache e será servida
    de novo amanhã mesmo que o Gemini volte — não é regenerada por provider,
    é a mesma resposta para a mesma pergunta+contexto, seja qual for o modelo
    que a gerou. Não é bug: é a escolha de design documentada em `_cache_key`
    (T2.4) — mas é o oposto exato do medo de "recomputar à toa"."""
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.models import Query, RetrievedChunk

    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    _cache_em_memoria(monkeypatch)

    pergunta = "como envio atividade?"
    responder.answer(
        Query(text=pergunta),
        llm=_cadeia(FakeProvider("gemini", erro=ErroHTTP(500)), FakeProvider("groq", resposta="Resposta do Groq.")),
    )

    # Gemini "volta" e responderia diferente, mas nunca é nem tentado.
    gemini_de_volta = FakeProvider("gemini", resposta="Resposta nova do Gemini.")
    resultado = responder.answer(Query(text=pergunta), llm=_cadeia(gemini_de_volta))

    assert resultado.text == "Resposta do Groq."  # a resposta antiga, do fallback
    assert gemini_de_volta.chamadas == 0


# --- A tabela `resposta_cache` grava QUEM respondeu (coluna `modelo`) -------
#
# Não afeta a BUSCA (a chave continua sem o provider, ver o teste acima) — é
# metadado de auditoria, para responder "essa resposta cacheada veio de qual
# modelo?" sem precisar adivinhar pela data. Ver `app/db/response_cache.py`.


def test_cache_grava_o_provider_e_o_modelo_que_responderam(monkeypatch):
    from langchain_core.documents import Document

    from app.agent import responder
    from app.core.models import Query, RetrievedChunk

    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    monkeypatch.setattr(responder, "get_cached_answer", lambda k: None)

    gravado = {}
    monkeypatch.setattr(
        responder, "set_cached_answer", lambda k, a, r, m=None: gravado.update(modelo=m)
    )

    cadeia = _cadeia(
        FakeProvider("gemini", erro=ErroHTTP(429, "Quota exceeded")),
        FakeProvider("groq", resposta="Resposta do Groq.", modelo="qwen/qwen3.6-27b"),
    )
    responder.answer(Query(text="como envio atividade?"), llm=cadeia)

    assert gravado["modelo"] == "groq:qwen/qwen3.6-27b"


def test_cache_sem_carimbo_de_provider_grava_o_modelo_configurado(monkeypatch):
    """Um LLM injetado direto (dublê de teste, ou uso do agente como
    biblioteca sem a `ProviderChain`) não carimba `response_metadata` — sobra
    o modelo CONFIGURADO (`settings.chat_model`), a mesma informação que a
    telemetria já usa nesse caso (`Registro.chat_model`)."""
    from langchain_core.documents import Document
    from langchain_core.messages import AIMessage

    from app.agent import responder
    from app.core.config import settings
    from app.core.models import Query, RetrievedChunk

    monkeypatch.setattr(settings, "chat_model", "gemini-3.6-flash")
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="texto", metadata={"source_name": "g.pdf"}),
        score=0.9,
    )
    monkeypatch.setattr(responder, "retrieve", lambda q: [chunk])
    monkeypatch.setattr(responder, "get_cached_answer", lambda k: None)

    gravado = {}
    monkeypatch.setattr(
        responder, "set_cached_answer", lambda k, a, r, m=None: gravado.update(modelo=m)
    )

    class LLMDireto:
        def invoke(self, mensagens):
            return AIMessage(content="Resposta direta.")

    responder.answer(Query(text="como envio atividade?"), llm=LLMDireto())

    assert gravado["modelo"] == "gemini-3.6-flash"
