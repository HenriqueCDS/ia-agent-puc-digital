"""Testes do parsing de saída do LLM (ver app/agent/prompts.py)."""

import pytest

from app.agent.prompts import (
    CONTEXTO_INSUFICIENTE,
    FORA_DE_ESCOPO,
    MARCADOR_TOPICO,
    _JANELA_RECUSA_PROSA,
    eh_fora_de_escopo,
    eh_insuficiente,
    eh_recusa_de_compliance,
    sem_marcador_topico,
    separar_topico,
)


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


def test_recusa_em_prosa_sem_marcador_e_insuficiente():
    """VET-1: as frases que abriram os vazamentos reais (2026-08-26/27), mesmo
    proibidas no prompt. Nenhuma emite o marcador — só a prosa."""
    reais = [
        "Infelizmente, não há informações específicas sobre dicas de estudo nos "
        "trechos fornecidos.",
        "Infelizmente, não foi possível encontrar o endereço e telefone da "
        "Secretaria Geral nos trechos fornecidos.",
        "Não é possível fornecer as datas específicas do calendário com base no "
        "contexto.",
        "Não é possível atender a esse pedido com os trechos disponíveis.",
        "O contexto fornecido não menciona como alterar o e-mail cadastrado.",
        "The provided context does not contain information about this topic.",
    ]
    for texto in reais:
        assert eh_insuficiente(texto) is True, texto


def test_recusa_em_prosa_na_abertura_de_resposta_que_compensa_e_veto():
    """O modelo recusa e depois oferece algo relacionado mas diferente — o veto
    tem que pegar a abertura, não ser enganado pelo resto do texto."""
    texto = (
        "Infelizmente, não há informações específicas sobre o endereço da "
        "secretaria nos trechos fornecidos, mas seguem outros contatos que podem "
        "ajudar: telefone da Faculdade de Direito, e-mail da ouvidoria e o "
        "portal institucional com o mapa do campus."
    )
    assert eh_insuficiente(texto) is True


def test_negacao_solta_em_resposta_real_nao_e_veto():
    """`não há prazo fixo` é resposta legítima: o veto de prosa só casa quando a
    negação vem seguida do vocabulário de meta-resposta (informação/trecho/...)."""
    assert eh_insuficiente(
        "Não há um prazo fixo para o trancamento: cada curso define o seu no "
        "calendário acadêmico. Confira em Secretaria > Calendário."
    ) is False


def test_limitacao_citada_no_meio_de_resposta_longa_nao_e_veto():
    """A recusa em prosa é curta ou vem na abertura. Uma resposta real que lá
    pelo fim menciona que a fonte não detalha um ponto não pode ser descartada."""
    texto = (
        "Para enviar a atividade no Canvas: acesse o curso, abra Tarefas, "
        "selecione a atividade e clique em Enviar tarefa. Anexe o arquivo ou "
        "cole o texto e confirme o envio. Você recebe um comprovante por e-mail. "
        "Observação: o material não detalha o tamanho máximo de anexo — confirme "
        "esse limite com o suporte. Fontes: guia-canvas.pdf, p. 8"
    )
    assert len(texto) > 320
    assert eh_insuficiente(texto) is False


@pytest.mark.parametrize(
    "resposta_real",
    [
        # Q7 (premissa falsa "a nota mínima mudou de 6,0 para 5,0?"): o modelo
        # recusou em prosa, sem emitir o marcador — foi assim que a rodada de
        # 2026-08-28 saiu como `origem="web"` contando sucesso em vez de
        # `#SEM_COBERTURA#` → encaminhado.
        "Os trechos fornecidos não mencionam qualquer alteração na nota mínima "
        "de aprovação; recomendo consultar diretamente o regulamento acadêmico.",
        # Q10 (número inventado "quais são os 7 pilares da gestão do tempo?"): o
        # material existe, então há contexto plausível para o modelo listar 7
        # itens. Ele emitiu o marcador em vez de fabricar a lista.
        CONTEXTO_INSUFICIENTE,
        "#SEM_COBERTURA# — o material sobre gestão do tempo não organiza o "
        "conteúdo em 7 pilares.",
    ],
)
def test_regressao_veto_de_contexto_q7_q10(resposta_real):
    """VET-5: Q7 (premissa falsa) e Q10 (número inventado) da rodada de
    2026-08-28 — os dois casos-armadilha de alucinação por complacência do
    dataset. O veto de contexto segurou os dois; este teste trava esse
    comportamento no nível da detecção, para que uma recalibração de limiar,
    prompt ou `eh_insuficiente` não o desfaça em silêncio."""
    assert eh_insuficiente(resposta_real) is True


@pytest.mark.parametrize(
    "texto",
    [
        # o caso real (Q17, 28-08): jailbreak que furou o guardrail, modelo recusou em inglês
        "I'm sorry, but I can't comply with that.",
        "I cannot assist with that request.",
        "I cannot help you with that.",
        "As an AI assistant, I cannot help you with that.",
        "I'm not able to help with that.",
        "Sorry, I can't do that.",
        "I must decline to answer this.",
        "That request goes against my guidelines.",
        "It would not be appropriate for me to do that.",
        # mesma recusa em português
        "Desculpe, mas não posso ajudar com isso.",
        "Não posso atender a esse pedido.",
        "Não posso cumprir com essa solicitação.",
        "Isso vai contra minhas diretrizes.",
    ],
)
def test_recusa_de_compliance_e_detectada_em_pt_e_en(texto):
    """VET-2: o modelo se nega a OBEDECER (não a responder por falta de contexto).
    Casa por estrutura — modal de negação + verbo de ação — nos dois idiomas."""
    assert eh_recusa_de_compliance(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "Para enviar a atividade, acesse Tarefas e clique em Enviar.",
        # falta de contexto NÃO é recusa de compliance: é o caminho do VET-1
        "Não posso fornecer essa informação porque não está nos trechos fornecidos.",
        "I can't provide that information as it is not in the provided context.",
        "Não posso garantir que o prazo será prorrogado; confirme na secretaria.",
        "Você pode continuar com o envio normalmente.",
        "O sistema não permite anexos maiores que 500 MB.",
    ],
)
def test_resposta_legitima_nao_e_recusa_de_compliance(texto):
    assert eh_recusa_de_compliance(texto) is False


def test_recusa_de_compliance_no_meio_de_resposta_longa_nao_casa():
    """A recusa de compliance é front-loaded. Uma resposta real que lá pra
    frente discuta, como conteúdo, limites de um assistente não pode disparar."""
    texto = (
        "O Canvas permite que o professor configure a política de submissão da "
        "tarefa, incluindo tentativas e prazo. O aluno envia pelo botão Enviar "
        "tarefa e acompanha o status na própria página da atividade. "
        "Vale lembrar que o suporte não pode alterar notas lançadas pelo docente."
    )
    assert len(texto) > 240
    assert eh_recusa_de_compliance(texto) is False


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


# --- VET-7: recusa em prosa depois de um preâmbulo verboso ------------------


def test_recusa_com_preambulo_verboso_ainda_e_veto():
    """VET-7: um preâmbulo longo ('Olá! Obrigado... Sobre a sua dúvida...')
    empurra a recusa para além da janela de 160 chars, e ela vazava como
    `origem="base"/"web"` com `grounded=True`. As frases que o prompt proíbe
    verbatim ('não há informações', 'os trechos não mencionam') passam a ser
    vetadas numa janela maior."""
    preambulo = (
        "Olá! Obrigado por entrar em contato com o suporte acadêmico da "
        "instituição de ensino. Fico feliz em poder te ajudar com essa dúvida. "
        "Vou verificar agora o que consta no material que temos disponível a "
        "respeito exatamente do que você perguntou, um momento por favor. "
    )
    assert len(preambulo) > _JANELA_RECUSA_PROSA  # a recusa começa só depois da janela curta
    reais = [
        preambulo + "Infelizmente não há informações específicas sobre isso.",
        preambulo + "Os trechos fornecidos não mencionam essa data.",
    ]
    for texto in reais:
        assert eh_insuficiente(texto[:_JANELA_RECUSA_PROSA]) is False
        assert eh_insuficiente(texto) is True, texto


def test_preambulo_seguido_de_resposta_real_nao_e_veto():
    """A janela mais larga do VET-7 é estreita no PADRÃO: uma resposta real que
    abre com saudação e depois responde de fato (com uma negação qualquer no
    meio) não pode disparar."""
    texto = (
        "Olá! Obrigado por entrar em contato. Sobre a sua dúvida a respeito do "
        "trancamento de disciplinas: o pedido é feito pela secretaria, dentro "
        "do prazo do calendário acadêmico, e não há cobrança de multa se for "
        "feito no período regular. Consulte o calendário para as datas exatas."
    )
    assert eh_insuficiente(texto) is False


# --- VET-6: o marcador de tópico não pode vazar para o aluno ----------------


@pytest.mark.parametrize(
    ("bruto", "texto_esperado", "topico_esperado"),
    [
        # caso normal: linha própria no fim
        (f"Acesse Tarefas e clique em Enviar.\n{MARCADOR_TOPICO} envio de atividade",
         "Acesse Tarefas e clique em Enviar.", "envio de atividade"),
        # VET-6: marcador inline, na mesma linha do texto — o `^...$` original
        # não casava e o marcador ia para a tela
        ("Acesse Tarefas e clique em Enviar. #TOPICO: envio de atividade",
         "Acesse Tarefas e clique em Enviar.", "envio de atividade"),
        # embrulhado em markdown (negrito)
        (f"Resposta aqui.\n**{MARCADOR_TOPICO} acesso ao canvas**",
         "Resposta aqui.", "acesso ao canvas"),
        # acento em TÓPICO
        ("Resposta.\n#TÓPICO: prazo de matrícula",
         "Resposta.", "prazo de matrícula"),
        # sem marcador: texto intacto, tópico None
        ("Resposta normal, sem marcador nenhum.",
         "Resposta normal, sem marcador nenhum.", None),
    ],
)
def test_separar_topico_extrai_e_nunca_deixa_o_marcador_no_texto(
    bruto, texto_esperado, topico_esperado
):
    texto, topico = separar_topico(bruto)
    assert texto == texto_esperado
    assert MARCADOR_TOPICO not in texto and "TÓPICO" not in texto
    assert topico == topico_esperado


def test_sem_marcador_topico_devolve_texto_intacto_quando_nao_ha_marcador():
    """A rede de `answer()` (VET-6) usa o `!=` para distinguir 'removi um
    marcador que escapou' de 'não havia nada' — então sem marcador o texto
    volta idêntico, sem nem `.strip()`."""
    assert sem_marcador_topico("Resposta com espaço no fim.  ") == "Resposta com espaço no fim.  "
    assert sem_marcador_topico("x\n#TOPICO: y") == "x"


def test_sentinela_de_cobertura_sobrevive_ao_marcador_de_topico_na_linha_seguinte():
    """Regressão do 1º draft do VET-6: o prefixo do `_RE_TOPICO` casava o `#`
    final de `#SEM_COBERTURA#` na linha anterior e o comia — o sentinela ficava
    `#SEM_COBERTURA` e `eh_insuficiente` deixava de reconhecê-lo."""
    texto, _ = separar_topico(f"{CONTEXTO_INSUFICIENTE}\n{MARCADOR_TOPICO} receita de bolo")
    assert texto == CONTEXTO_INSUFICIENTE
    assert eh_insuficiente(texto) is True


# --- TRI-4: o marcador de pedido fora de escopo -----------------------------


@pytest.mark.parametrize(
    "texto",
    [
        FORA_DE_ESCOPO,
        f"Desculpe, não posso ajudar com isso. {FORA_DE_ESCOPO}",
        f"{FORA_DE_ESCOPO}\n#TOPICO: pedido fora de escopo",
    ],
)
def test_eh_fora_de_escopo_reconhece_o_marcador_em_qualquer_posicao(texto):
    assert eh_fora_de_escopo(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "Para enviar a atividade, acesse Tarefas e clique em Enviar.",
        "O escopo do suporte é o Canvas e procedimentos acadêmicos.",
        CONTEXTO_INSUFICIENTE,
    ],
)
def test_resposta_legitima_nao_e_fora_de_escopo(texto):
    assert eh_fora_de_escopo(texto) is False
