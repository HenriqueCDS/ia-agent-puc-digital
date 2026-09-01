"""Invariante de escopo do `app/db/vector_store.py`.

Sem banco de propósito — o resto da suíte já dubla o Postgres, e o que precisa
ser travado aqui não é o resultado de uma query, é uma REGRA sobre o SQL do
módulo: toda declaração que toca `langchain_pg_embedding` é escopada à coleção
ativa (`collection_id = (SELECT ... WHERE name = :collection_name)`).

O teste existe por causa de um bug real. `existing_content_hashes` nasceu sem
esse filtro e enxergava a tabela inteira. Enquanto houve uma coleção só, nada
apareceu. Ao trocar o modelo de embedding (o que obriga a criar uma coleção
nova, porque a dimensão do vetor muda), o `content_hash` — que é do TEXTO
normalizado, não do vetor, e portanto idêntico entre as duas — passou a casar
contra a coleção ANTIGA, e todo chunk foi descartado como "duplicado":

    [canvas] 2 arquivo(s), 0 chunk(s) indexado(s).
      5998 chunk(s) descartado(s) por já existir com o mesmo conteúdo em outra fonte

Zero chunks, zero erros, exit 0 — a coleção nova ficava permanentemente vazia e
o agente respondia "não encontrei na base" para tudo. É o modo de falha que
custa mais caro para achar, e é barato de impedir.

Vale para qualquer isolamento por coleção (A/B de chunking, staging), não só
para a troca de modelo.
"""

import re
from pathlib import Path

import app.db.vector_store as vector_store

FONTE = Path(vector_store.__file__).read_text(encoding="utf-8")

# Blocos de SQL do módulo: strings com aspas triplas que consultam a tabela de
# chunks. O `FROM` (maiúsculo, com espaço) é o que separa SQL de menção em
# docstring — `delete_by_source` cita `langchain_pg_embedding` em prosa ao
# explicar o acoplamento com o schema do langchain-postgres, e essa citação não
# é uma query.
_BLOCOS_SQL = [
    bloco
    for bloco in re.findall(r'"""(.*?)"""', FONTE, re.DOTALL)
    if "FROM langchain_pg_embedding" in bloco
]


def test_o_modulo_tem_sql_para_inspecionar():
    """Guarda do próprio teste: se a extração parar de achar as queries (SQL
    movido para outro arquivo, aspas trocadas), os testes abaixo passariam
    vazios e a invariante ficaria sem cobertura em silêncio."""
    assert len(_BLOCOS_SQL) >= 5


def test_toda_query_de_chunks_e_escopada_a_colecao_ativa():
    """A invariante. Uma query nova sem o escopo falha aqui, e não numa
    reingestão silenciosa meses depois.

    Exige a constante compartilhada `_COLLECTION_ID`, e não um `collection_id`
    qualquer no texto: um filtro escrito à mão (id literal, parâmetro próprio)
    passaria numa checagem por substring e reintroduziria o bug com o filtro
    aparentemente no lugar. Como o fonte é lido cru, o que se vê aqui é o
    placeholder do f-string, antes da interpolação.
    """
    sem_escopo = [b for b in _BLOCOS_SQL if "{_COLLECTION_ID}" not in b]

    assert sem_escopo == [], (
        "SQL toca langchain_pg_embedding sem escopar por {_COLLECTION_ID} — "
        "vaza para coleções abandonadas de outro modelo de embedding:\n"
        + "\n---\n".join(sem_escopo)
    )


def test_delete_by_assunto_nao_toca_no_conteudo_crawlado():
    """`--assunto` limpa ARQUIVOS de uma pasta. As páginas da WEB_ALLOWLIST são
    gravadas com o mesmo `assunto` da FonteWeb (não `"web"`), então sem excluir
    `source_type='web'` no DELETE um `--assunto puc-digital` para tirar 3 PDFs
    levava junto todo o crawl daquele assunto, em silêncio."""
    bloco = next(
        b for b in _BLOCOS_SQL
        if "DELETE FROM langchain_pg_embedding" in b and ":assunto" in b
    )
    assert "source_type" in bloco and "IS DISTINCT FROM 'web'" in bloco


def test_list_web_sources_filtra_so_o_conteudo_crawlado():
    """A contraparte de `delete_by_assunto`: o comando que LISTA o crawl para
    apagar (prune do re-crawl, `remove_ingested --web`) tem que enxergar só
    `source_type='web'` — senão levaria PDF junto."""
    bloco = next(
        b for b in _BLOCOS_SQL
        if "GROUP BY" in b and "cmetadata->>'source_type' = 'web'" in b
    )
    assert "SELECT" in bloco and "source_path" in bloco


def test_o_escopo_vem_do_nome_da_colecao_ativa_e_nao_de_um_id_solto():
    """O outro lado da invariante: a constante que as queries usam tem que
    resolver a coleção pelo NOME configurado, ligado como parâmetro. Se ela
    virasse um id fixo, todas as queries continuariam "escopadas" e apontariam
    para a coleção errada."""
    assert ":collection_name" in vector_store._COLLECTION_ID
    assert "langchain_pg_collection" in vector_store._COLLECTION_ID
