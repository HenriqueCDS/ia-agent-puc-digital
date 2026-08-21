"""Testes do parsing de saída do LLM (ver app/agent/prompts.py)."""

from app.agent.prompts import CONTEXTO_INSUFICIENTE, eh_insuficiente


def test_marcador_puro_e_insuficiente():
    assert eh_insuficiente(CONTEXTO_INSUFICIENTE) is True


def test_marcador_em_caixa_baixa_e_insuficiente():
    """O prompt pede caixa alta, mas o veto não pode depender do modelo obedecer."""
    assert eh_insuficiente("insuficiente") is True


def test_marcador_embrulhado_em_frase_e_insuficiente():
    """O caso que o `startswith` original perdia: o modelo não devolveu só a
    palavra, embrulhou num pedido de desculpas — o veto ainda tem que disparar."""
    texto = "Peço desculpas, mas o contexto fornecido é INSUFICIENTE para responder."
    assert eh_insuficiente(texto) is True


def test_marcador_traduzido_para_ingles_e_insuficiente():
    """Regressão do bug real: o modelo devolveu INSUFFICIENT (inglês) em vez do
    marcador combinado. O veto não casou, o texto cru foi para o aluno e a busca
    externa nem chegou a ser tentada."""
    assert eh_insuficiente("INSUFFICIENT") is True


def test_sentinela_em_resposta_longa_e_insuficiente():
    """O sentinela delimitado não aparece em texto natural: vale em qualquer
    posição e a qualquer comprimento."""
    texto = "Bla bla. " * 40 + CONTEXTO_INSUFICIENTE
    assert eh_insuficiente(texto) is True


def test_resposta_normal_nao_e_insuficiente():
    assert eh_insuficiente("Para enviar a atividade, acesse Tarefas.") is False


def test_palavra_relacionada_nao_casa_por_acidente():
    """Word boundary: 'insuficiência' não é o marcador 'insuficiente'."""
    assert eh_insuficiente("Há uma insuficiência de dados no sistema.") is False


def test_resposta_longa_que_usa_a_palavra_nao_e_veto():
    """A palavra solta só conta como veto em resposta curta. Uma resposta de
    verdade que fale em 'documentação insuficiente' não pode ser descartada."""
    texto = (
        "Para solicitar o aproveitamento de disciplinas, entregue o histórico e o "
        "plano de ensino na secretaria. Atenção: se a documentação entregue for "
        "insuficiente, o pedido é devolvido para complementação e você precisa "
        "reabrir o protocolo dentro do prazo do calendário acadêmico. "
        "Fontes: manual-do-aluno.pdf, p. 12"
    )
    assert len(texto) > 200  # a guarda que separa recusa curta de resposta real
    assert eh_insuficiente(texto) is False
