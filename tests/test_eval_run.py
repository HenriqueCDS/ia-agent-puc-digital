"""Testes do harness de avaliação (ver scripts/eval_run.py).

O harness é a régua do projeto — se ele mede errado, toda análise de rodada
herda o erro. Estes testes travam três garantias que vieram de falhas reais:

1. a linha do resultado carrega o registro da TELEMETRIA (scores do retrieval),
   não só o que sobrou em `Answer.sources`;
2. uma pergunta que levanta não derruba a rodada (o 413 de 2026-08-27 matou uma
   rodada no item 14 de 25);
3. a captura de telemetria ENCADEIA sobre o sink já instalado, em vez de
   substituí-lo — senão ligar o arquivo local desligaria o Postgres.
"""

import json

import pytest

from app.core import telemetry
from app.core.models import Answer, RetrievedChunk
from langchain_core.documents import Document

from scripts import eval_run


@pytest.fixture(autouse=True)
def sink_limpo():
    """Nenhum teste daqui pode deixar sink instalado para o próximo."""
    anterior = telemetry.persistencia_atual()
    yield
    telemetry.configurar_persistencia(anterior)


def _item(pergunta="pergunta?", esperada="base", **extra):
    return {"pergunta": pergunta, "assunto": None, "origem_esperada": esperada, **extra}


def _answer(texto="Resposta.", origem="base"):
    chunk = RetrievedChunk(
        document=Document(id="c1", page_content="x", metadata={"source_name": "guia.pdf", "page": 3}),
        score=0.87,
    )
    return Answer(text=texto, sources=[chunk], grounded=True, origem=origem)


# --- captura encadeada -------------------------------------------------------


def test_captura_encadeia_sobre_o_sink_ja_instalado():
    """O Postgres continua recebendo: o arquivo local é um destino A MAIS."""
    do_banco = []
    telemetry.configurar_persistencia(do_banco.append)

    capturados = eval_run._capturar_telemetria()
    telemetry.persistencia_atual()({"canal": "eval", "score_top": 0.9})

    assert capturados == [{"canal": "eval", "score_top": 0.9}]
    assert do_banco == [{"canal": "eval", "score_top": 0.9}]


def test_captura_funciona_sem_sink_anterior():
    telemetry.configurar_persistencia(None)

    capturados = eval_run._capturar_telemetria()
    telemetry.persistencia_atual()({"canal": "eval"})

    assert capturados == [{"canal": "eval"}]


# --- a linha do resultado ----------------------------------------------------


def test_linha_traz_os_scores_do_retrieval_e_nao_so_as_fontes_da_resposta():
    """A confusão que a §7 de analise-telemetria-2026-08-27 documentou: o
    arquivo mostrava `n_chunks: 0` onde a telemetria mostrava 5, porque um
    contava as fontes da RESPOSTA e o outro os chunks RECUPERADOS. Agora os dois
    aparecem, com nomes diferentes."""
    registro = {"n_chunks": 5, "score_top": 0.88, "score_min": 0.84, "score_mean": 0.86,
                "provider": "huggingface", "cache_hit": False}

    linha = eval_run._linha(_item(), _answer(), registro, erro=None)

    assert linha["chunks_recuperados"] == 5      # o retrieval trouxe 5
    assert linha["fontes_resposta"] == 1         # 1 sustentou a resposta
    assert (linha["score_top"], linha["score_min"], linha["score_mean"]) == (0.88, 0.84, 0.86)
    assert linha["score_fonte_top"] == 0.87
    assert linha["provider"] == "huggingface"


def test_linha_grava_a_resposta_e_as_fontes_citadas():
    linha = eval_run._linha(_item(), _answer(texto="Acesse Tarefas."), {}, erro=None)

    assert linha["resposta"] == "Acesse Tarefas."
    assert linha["fontes_citadas"] == ["guia.pdf, p. 4"]


def test_resposta_gravada_e_mascarada():
    """O arquivo fica no repositório e o dataset de teste traz PII de propósito
    (perguntas_teste3, bloco C) — a resposta pode ecoar o identificador."""
    linha = eval_run._linha(
        _item(), _answer(texto="O CPF 390.533.447-05 está ativo."), {}, erro=None
    )

    assert "390.533.447-05" not in linha["resposta"]
    assert "[cpf]" in linha["resposta"]


def test_criterio_do_dataset_vai_para_o_resultado():
    """Blocos B e C de perguntas_teste3 não são avaliáveis por `origem` — o
    critério tem que chegar em quem revisa à mão."""
    linha = eval_run._linha(_item(criterio="conferir a página no PDF"), _answer(), {}, erro=None)

    assert linha["criterio"] == "conferir a página no PDF"


def test_acertou_compara_a_origem():
    assert eval_run._linha(_item(esperada="base"), _answer(origem="base"), {}, None)["acertou"]
    assert not eval_run._linha(_item(esperada="base"), _answer(origem="web"), {}, None)["acertou"]


def test_origem_tambem_ok_conta_como_acerto():
    """`nenhuma` e `encaminhado` dão a mesma mensagem ao aluno: numa pergunta que
    o agente legitimamente não sabe, as duas são acerto (ver `_origens_aceitas`)."""
    item = _item(esperada="encaminhado", origem_tambem_ok=["nenhuma"])

    assert eval_run._linha(item, _answer(origem="nenhuma"), {}, None)["acertou"]
    assert eval_run._linha(item, _answer(origem="encaminhado"), {}, None)["acertou"]
    # e não afrouxa o resto
    assert not eval_run._linha(item, _answer(origem="web"), {}, None)["acertou"]


def test_origem_tambem_ok_vai_para_a_linha():
    linha = eval_run._linha(
        _item(esperada="web", origem_tambem_ok=["nenhuma"]), _answer(origem="web"), {}, None
    )
    assert linha["origem_tambem_ok"] == ["nenhuma"]
    # ausente no dataset -> None, não [] (distingue "não declarado" de "vazio")
    assert eval_run._linha(_item(), _answer(), {}, None)["origem_tambem_ok"] is None


# --- resiliência da rodada ---------------------------------------------------


def test_pergunta_que_levanta_nao_derruba_a_rodada(monkeypatch):
    """Regressão do 413 de 2026-08-27: a rodada morreu no item 14 de 25."""
    registros = []

    def answer_falso(query, *a, **k):
        registros.append({"n_chunks": 5, "erro": None})
        if "estoura" in query.text:
            raise RuntimeError("Error code: 413 - request_too_large")
        return _answer()

    monkeypatch.setattr(eval_run, "answer", answer_falso)

    linhas = eval_run._rodar(
        [_item("normal 1"), _item("estoura aqui"), _item("normal 2")], None, registros
    )

    assert len(linhas) == 3                        # a rodada foi até o fim
    assert linhas[0]["erro"] is None
    assert "413" in linhas[1]["erro"]
    assert linhas[1]["origem_obtida"] is None
    assert linhas[1]["acertou"] is False
    assert linhas[2]["erro"] is None               # seguiu depois da falha


def test_cada_linha_recebe_o_registro_da_sua_pergunta(monkeypatch):
    """1 registro por chamada de `answer()`, na ordem — o pareamento é por
    posição, e uma troca aqui atribuiria o score de uma pergunta a outra."""
    registros = []
    scores = iter([0.81, 0.82, 0.83])

    def answer_falso(query, *a, **k):
        registros.append({"n_chunks": 5, "score_top": next(scores)})
        return _answer()

    monkeypatch.setattr(eval_run, "answer", answer_falso)

    linhas = eval_run._rodar([_item("a"), _item("b"), _item("c")], None, registros)

    assert [l["score_top"] for l in linhas] == [0.81, 0.82, 0.83]


# --- serialização ------------------------------------------------------------


def test_csv_tem_todas_as_colunas_da_linha():
    """`_CAMPOS_SAIDA` alimenta o DictWriter: um campo novo em `_linha` que não
    entre na lista quebraria a saída CSV com ValueError."""
    linha = eval_run._linha(_item(criterio="x"), _answer(), {"n_chunks": 5}, None)

    assert set(linha) == set(eval_run._CAMPOS_SAIDA)


def test_salvar_json_e_csv(tmp_path):
    linhas = [eval_run._linha(_item(), _answer(), {"n_chunks": 5}, None)]

    destino_json = tmp_path / "r.json"
    eval_run._salvar(linhas, destino_json, "json")
    assert json.loads(destino_json.read_text(encoding="utf-8"))[0]["chunks_recuperados"] == 5

    destino_csv = tmp_path / "r.csv"
    eval_run._salvar(linhas, destino_csv, "csv")
    assert "chunks_recuperados" in destino_csv.read_text(encoding="utf-8")
