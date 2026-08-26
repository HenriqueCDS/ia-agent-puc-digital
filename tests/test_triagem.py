"""Testes da triagem por assunto (ver app/agent/triagem.py).

O valor desta etapa é ser determinística: dá para provar qual pergunta vai para
qual e-mail, e provar que a pergunta em escopo continua passando.
"""

import pytest

from app.agent import responder
from app.agent.triagem import classificar
from app.core.models import Query


# --- classificação -----------------------------------------------------------


@pytest.mark.parametrize(
    "pergunta,assunto,email",
    [
        ("Não recebi meu boleto deste mês", "financeiro", "dcr@"),
        ("Como faço para renovar minha bolsa?", "financeiro", "dcr@"),
        ("Dúvida sobre o FIES", "financeiro", "dcr@"),
        ("Quando fica pronto meu diploma?", "diplomas", "diplomas@"),
        ("Preciso do certificado do curso", "diplomas", "diplomas@"),
        ("Como faço a rematrícula?", "academico", "puc.digital@"),
        ("Quero meu histórico escolar", "academico", "puc.digital@"),
        ("Como peço trancamento?", "academico", "puc.digital@"),
    ],
)
def test_assunto_fora_de_escopo_vai_para_o_departamento_certo(pergunta, assunto, email):
    categoria = classificar(pergunta)

    assert categoria is not None
    assert categoria.assunto == assunto
    assert email in categoria.resposta


@pytest.mark.parametrize(
    "pergunta",
    [
        "Como envio uma atividade no Canvas?",
        "Onde vejo o calendário da disciplina?",
        "Qual horario de funcionamento da Praças de Alimentação?",
    ],
)
def test_pergunta_em_escopo_segue_para_o_rag(pergunta):
    assert classificar(pergunta) is None


def test_acento_e_caixa_nao_atrapalham():
    """O casamento roda sobre o texto normalizado, sem acento e em caixa baixa."""
    assert classificar("REMATRÍCULA") is not None
    assert classificar("rematricula") is not None


# --- "matrícula": só rematrícula é encaminhada -------------------------------


@pytest.mark.parametrize(
    "pergunta",
    [
        "Como faço minha matrícula?",
        "Como faço matrícula em disciplina no Canvas?",
        "Onde faço matrícula pela plataforma?",
    ],
)
def test_matricula_sozinha_segue_para_o_rag(pergunta):
    """"matricula" não é termo de nenhuma categoria: encaminhá-lo tiraria da
    base perguntas que ela responde bem (matrícula em disciplina no Canvas).
    Quem é da secretaria é "rematrícula", que está listado."""
    assert classificar(pergunta) is None


def test_rematricula_e_encaminhada():
    """"matricula" é substring de "rematricula" — a categoria casa pelo termo
    completo, então a rematrícula continua indo para a secretaria."""
    categoria = classificar("Como faço a rematrícula na disciplina?")

    assert categoria is not None and categoria.assunto == "academico"


def test_trancamento_de_disciplina_e_encaminhado():
    assert classificar("Quero trancamento de disciplina") is not None


# --- "bolsa": o benefício é da cobrança, a de pesquisa/monitoria não é -------


@pytest.mark.parametrize(
    "pergunta",
    [
        "Como faço para participar do processo seletivo de bolsas de iniciação científica?",
        "Como consigo uma bolsa de monitoria?",
        "Existe bolsa de pesquisa para a pós-graduação?",
        "Como funciona a bolsa de extensão?",
    ],
)
def test_bolsa_academica_ou_de_pesquisa_nao_vai_para_a_cobranca(pergunta):
    """Regressão de 2026-08-26: "processo seletivo de bolsas de iniciação
    científica" era encaminhada para a cobrança em 1.3ms, sem tocar no RAG (ver
    eval/analise-telemetria-2026-08-26.md §5). IC, monitoria, pesquisa e
    extensão são acadêmico/pesquisa — nada a ver com o setor financeiro.

    Vale para os dois lados: `web_fallback` consulta a mesma `classificar`,
    então estas também voltam a poder ser pesquisadas nos domínios oficiais."""
    assert classificar(pergunta) is None


@pytest.mark.parametrize(
    "pergunta",
    [
        "Como faço para renovar minha bolsa?",
        "Ainda tenho direito à minha bolsa?",
        "Perdi minha bolsa, o que faço?",
    ],
)
def test_bolsa_como_beneficio_do_aluno_continua_encaminhada(pergunta):
    """O outro lado da exceção acima: pedir sobre o próprio benefício depende do
    cadastro do aluno e continua sendo da cobrança."""
    categoria = classificar(pergunta)

    assert categoria is not None and categoria.assunto == "financeiro"


def test_termo_inequivoco_vence_a_excecao_de_bolsa():
    """A ordem de `ENCAMINHAMENTOS` importa: "boleto" está numa entrada ANTES da
    de "bolsa", então ele casa primeiro e as exceções da outra não o alcançam —
    que é justamente por que "bolsa" ganhou entrada própria em vez de virar
    exceção da entrada com boleto/FIES/mensalidade."""
    categoria = classificar("O boleto da minha bolsa de pesquisa veio errado")

    assert categoria is not None and categoria.assunto == "financeiro"


# --- "minha nota": o valor é da secretaria, ver a nota é do agente -----------


@pytest.mark.parametrize(
    "pergunta",
    [
        "Qual é a minha nota final?",
        "Minha nota de Cálculo está errada",
        "Não concordo com minhas notas deste semestre",
    ],
)
def test_pedido_do_valor_da_nota_e_encaminhado(pergunta):
    """O valor depende do registro do aluno — uma página pública responderia
    com confiança aparente e conteúdo errado."""
    categoria = classificar(pergunta)

    assert categoria is not None and categoria.assunto == "academico"


@pytest.mark.parametrize(
    "pergunta",
    [
        "Onde vejo minhas notas no Canvas?",
        "Como acesso minha nota na área do aluno?",
        "Como consulto minhas notas pela plataforma?",
        "Onde encontro minhas notas?",
    ],
)
def test_como_ver_a_nota_segue_para_o_rag(pergunta):
    """Procedimento, não valor: os guias oficiais do Canvas documentam isso."""
    assert classificar(pergunta) is None


def test_excecoes_da_nota_nao_desarmam_os_outros_termos_academicos():
    """As exceções valem para a categoria inteira, por isso "minha nota" mora
    em entrada própria: rematrícula é da secretaria mesmo citando a plataforma."""
    categoria = classificar("Como faço a rematrícula no portal do aluno?")

    assert categoria is not None and categoria.assunto == "academico"


# --- precedência entre categorias --------------------------------------------


def test_pergunta_que_casa_duas_categorias_usa_a_primeira():
    """"boleto da rematrícula" casa financeiro e acadêmico, que têm e-mails
    diferentes. Quem cobra responde por cobrança."""
    categoria = classificar("Preciso do boleto da rematrícula")

    assert categoria.assunto == "financeiro"
    assert "dcr@" in categoria.resposta


# --- integração com o agente -------------------------------------------------


def test_encaminhamento_nao_toca_no_rag_nem_no_llm(monkeypatch):
    """O ponto da triagem: sem embedding, sem pgvector, sem token."""

    def nao_deveria_ser_chamado(_query):
        raise AssertionError("retrieval não pode rodar para assunto encaminhado")

    monkeypatch.setattr(responder, "retrieve", nao_deveria_ser_chamado)

    class LLMProibido:
        def invoke(self, mensagens):
            raise AssertionError("LLM não pode ser chamado para assunto encaminhado")

    resultado = responder.answer(Query(text="Não recebi meu boleto"), llm=LLMProibido())

    assert resultado.origem == "encaminhado"
    assert resultado.grounded is False
    assert resultado.sources == []
    assert "dcr@puc-campinas.edu.br" in resultado.text


def test_kill_switch_devolve_a_pergunta_para_o_rag(monkeypatch):
    """TRIAGEM_ENABLED=false: rollback para o comportamento anterior, em que o
    assunto sensível ia ao RAG e só era rotulado no caminho `origem="nenhuma"`."""
    monkeypatch.setattr(responder.settings, "triagem_enabled", False)
    monkeypatch.setattr(responder, "retrieve", lambda q: [])
    monkeypatch.setattr(responder.settings, "web_fallback_enabled", False)

    resultado = responder.answer(Query(text="Não recebi meu boleto"), llm=None)

    assert resultado.origem == "nenhuma"  # seguiu o fluxo antigo, não a triagem


def test_assunto_encaminhado_nao_vai_para_a_busca_externa():
    """Defesa em profundidade: mesmo chamando `buscar_na_web` direto (sem passar
    pela triagem), assunto sensível não é pesquisado na web."""
    from app.agent import web_fallback

    assert web_fallback.assunto_bloqueado("Não recebi meu boleto") is True
    assert web_fallback.termo_bloqueado("Não recebi meu boleto") == "financeiro"
    # E a exceção de "matrícula" vale nos dois lados, pela mesma `classificar`.
    assert web_fallback.assunto_bloqueado("matrícula em disciplina no Canvas") is False
