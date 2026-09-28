"""Fase B — proteções do coder para campanhas em lotes.

Estudo: synesis-planning/synesis/Estudo_Includes_Multiplos_Pastas_e_Lotes.md §6.2-6.4.
Origem: rodada `abstract` da Kely (face85, 2026-09-28), 65 referências:

  - o `.bib` do --input não estava no projeto; todo registro nasceu com E001;
  - o E001 traz "chaves similares", o laço de correção as repassou ao modelo, e
    47 de 47 trocas foram exatamente a 1ª sugestão — registros marcados OK,
    atribuídos a artigos de outros autores;
  - os avisos UndefinedCode (emitidos para TODO código, porque o validador não
    recebe a ontologia) foram ao modelo, que apagou as chains: 269 de 347 ITEMs.

Cobre: vários .bib no coder, checagem prévia das chaves, guarda de identidade,
laço de correção só com erros e sem E001, dataset com várias linhas.
Todos os testes são offline — compilador real, LLM simulado.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synesis_coder import project_loader, validator

# Pastas e várias linhas em INCLUDE DATASET exigem o expand_include do synesis
# >= 0.13. Com a 0.12 (a que o CI instala do PyPI até a 0.13 ser publicada), o
# coder funciona pelo caminho antigo — só esses cenários não se aplicam.
requer_synesis_013 = pytest.mark.skipif(
    project_loader._expand_include is None,
    reason="pastas em INCLUDE exigem synesis >= 0.13",
)
from synesis_coder.block_assembler import foreign_bibrefs
from synesis_coder.project_loader import (
    _collect_includes,
    assert_input_keys_in_project,
    bibliography_kwargs,
    load_project,
)

TEMPLATE = """\
TEMPLATE test

SOURCE FIELDS
    OPTIONAL summary
END SOURCE FIELDS

ITEM FIELDS
    REQUIRED citation, memo, tag
END ITEM FIELDS

FIELD summary TYPE TEXT
    SCOPE SOURCE
END FIELD

FIELD citation TYPE QUOTATION
    SCOPE ITEM
END FIELD

FIELD memo TYPE MEMO
    SCOPE ITEM
END FIELD

FIELD tag TYPE CODE
    SCOPE ITEM
END FIELD
"""

BIB_A = "@article{alpha2020,\n  title = {Primeiro},\n  year = {2020}\n}\n"
BIB_B = "@article{beta2021,\n  title = {Segundo},\n  year = {2021}\n}\n"


def _annotation(bibref: str, tag: str = "conceito_existente") -> str:
    return (
        f"SOURCE @{bibref}\n    summary: estudo.\nEND SOURCE\n\n"
        f"ITEM @{bibref}\n    citation: trecho.\n    memo: nota.\n    tag: {tag}\nEND ITEM\n"
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _project(tmp_path: Path, bib_includes: str) -> Path:
    _write(tmp_path / "t.synt", TEMPLATE)
    synp = tmp_path / "p.synp"
    _write(synp, f'PROJECT p\nTEMPLATE "t.synt"\n{bib_includes}END PROJECT\n')
    return synp


# ---------------------------------------------------------------------------
# §6.2 — vários .bib no coder
# ---------------------------------------------------------------------------

def test_collect_includes_guarda_todos_os_bib(tmp_path):
    """Antes cada .bib sobrescrevia o anterior e só o último sobrevivia."""
    _write(tmp_path / "fontes/a.bib", BIB_A)
    _write(tmp_path / "fontes/b.bib", BIB_B)
    project = 'INCLUDE BIBLIOGRAPHY "fontes/a.bib"\nINCLUDE BIBLIOGRAPHY "fontes/b.bib"\n'

    _ann, _onto, bib = _collect_includes(project, tmp_path)

    assert list(bib) == ["fontes/a.bib", "fontes/b.bib"]


@requer_synesis_013
def test_load_project_com_pasta_de_bib(tmp_path):
    _write(tmp_path / "Sources/a.bib", BIB_A)
    _write(tmp_path / "Sources/docente/b.bib", BIB_B)
    synp = _project(tmp_path, 'INCLUDE BIBLIOGRAPHY "Sources"\n')

    ctx = load_project(synp)

    assert set(ctx["bib_keys"]) == {"alpha2020", "beta2021"}
    assert len(ctx["bib_contents"]) == 2
    # texto único preservado para os consumidores por regex (critique, anchor)
    assert "alpha2020" in ctx["bib_content"] and "beta2021" in ctx["bib_content"]


def test_bibliography_kwargs_usa_dict_quando_o_compilador_aceita(monkeypatch):
    ctx = {"bib_contents": {"a.bib": BIB_A, "b.bib": BIB_B}}

    monkeypatch.setattr(project_loader, "_LOAD_TAKES_BIB_DICT", True)
    assert bibliography_kwargs(ctx) == {"bibliography_contents": {"a.bib": BIB_A, "b.bib": BIB_B}}

    # synesis 0.12: um texto só, com os dois arquivos
    monkeypatch.setattr(project_loader, "_LOAD_TAKES_BIB_DICT", False)
    kwargs = bibliography_kwargs(ctx)
    assert set(kwargs) == {"bibliography_content"}
    assert "alpha2020" in kwargs["bibliography_content"] and "beta2021" in kwargs["bibliography_content"]


def test_bibliography_kwargs_sem_bib():
    assert bibliography_kwargs({}) == {"bibliography_content": None}


# ---------------------------------------------------------------------------
# §6.3.1 — checagem prévia das chaves do --input
# ---------------------------------------------------------------------------

def test_checagem_previa_aborta_com_chaves_fora_do_projeto(tmp_path):
    _write(tmp_path / "refs.bib", BIB_A)
    ctx = load_project(_project(tmp_path, 'INCLUDE BIBLIOGRAPHY "refs.bib"\n'))

    with pytest.raises(ValueError) as exc:
        assert_input_keys_in_project(ctx, ["alpha2020", "kely2024", "kely2023"], "kely.bib")

    msg = str(exc.value)
    assert "2 de 3" in msg and "kely2024" in msg and "kely.bib" in msg
    assert "Nenhuma chamada ao modelo" in msg


def test_checagem_previa_passa_e_ignora_caixa(tmp_path):
    _write(tmp_path / "refs.bib", BIB_A)
    ctx = load_project(_project(tmp_path, 'INCLUDE BIBLIOGRAPHY "refs.bib"\n'))

    assert_input_keys_in_project(ctx, ["Alpha2020", "@alpha2020"], "x.bib")


def test_checagem_previa_sem_include_bibliography_nao_valida(tmp_path):
    ctx = load_project(_project(tmp_path, ""))

    assert_input_keys_in_project(ctx, ["qualquer"], "x.bib")


def test_abstract_aborta_antes_de_criar_o_cliente(tmp_path, monkeypatch):
    """A campanha da Kely teria parado aqui, sem nenhuma chamada paga."""
    from synesis_coder.modes import abstract_mode

    _write(tmp_path / "refs.bib", BIB_A)
    synp = _project(tmp_path, 'INCLUDE BIBLIOGRAPHY "refs.bib"\n')
    entrada = tmp_path / "Sources" / "kely.bib"
    _write(entrada, "@article{kely2024,\n  title = {K},\n  abstract = {Um resumo.}\n}\n")

    def _proibido(*args, **kwargs):
        raise AssertionError("o cliente LLM não deveria ser criado")

    monkeypatch.setattr(abstract_mode, "LLMClient", _proibido)
    with pytest.raises(ValueError, match="kely2024"):
        abstract_mode.process_abstract(
            project_path=synp, bib_path=entrada, output_dir=tmp_path / "out",
        )
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# §6.3.2 — guarda de identidade
# ---------------------------------------------------------------------------

def test_foreign_bibrefs_detecta_troca():
    saida = "SOURCE @paula2011\nEND SOURCE\nITEM @paula2011\nEND ITEM\n"

    assert foreign_bibrefs(saida, "paiva2022") == {"paula2011"}


def test_foreign_bibrefs_ignora_caixa_e_comentarios():
    saida = (
        "# SOURCE @outra (linha de diagnóstico)\n"
        "SOURCE @Paiva2022\nEND SOURCE\nITEM @paiva2022\nEND ITEM\n"
    )

    assert foreign_bibrefs(saida, "@paiva2022") == set()


# ---------------------------------------------------------------------------
# §6.4 — laço de correção: só erros, e E001 nunca vai ao modelo
# ---------------------------------------------------------------------------

def _ctx_para_validacao(tmp_path: Path) -> dict:
    _write(tmp_path / "refs.bib", BIB_A)
    return load_project(_project(tmp_path, 'INCLUDE BIBLIOGRAPHY "refs.bib"\n'))


def test_laco_sincrono_valida_so_itens_e_nao_ve_e001(tmp_path):
    """O laço síncrono (item/document) descarta o SOURCE: ITEM com chave
    desconhecida é OrphanItem, ignorado de propósito — não há E001 a filtrar.
    Nesses modos a chave vem do --bibref, validada antes (assert_bibref_known)."""
    ctx = _ctx_para_validacao(tmp_path)
    client = MagicMock()

    _final, ok = validator.validate_and_fix(
        _annotation("alpha2021"), ctx, client, scope="item", max_tries=1,
    )

    assert ok
    assert not client.fix.called


def test_e001_nao_vai_ao_modelo_no_laco_async(tmp_path):
    ctx = _ctx_para_validacao(tmp_path)
    client = MagicMock()

    final, ok = asyncio.run(validator.validate_and_fix_async(
        _annotation("alpha2021"), ctx, client, max_tries=3,
    ))

    assert not ok
    assert not client.fix_async.called
    assert "não corrigível pelo modelo" in final


def test_avisos_nao_vao_ao_modelo(tmp_path):
    """Erro real + UndefinedCode: o modelo recebe o erro, não os avisos."""
    ctx = _ctx_para_validacao(tmp_path)
    # sem `memo` (REQUIRED) → erro real; `tag` fora da ontologia → aviso
    ruim = (
        "SOURCE @alpha2020\n    summary: estudo.\nEND SOURCE\n\n"
        "ITEM @alpha2020\n    citation: trecho.\n    tag: conceito_novo\nEND ITEM\n"
    )
    client = MagicMock()
    client.fix.return_value = ruim

    validator.validate_and_fix(ruim, ctx, client, scope="abstract", max_tries=1)

    enviado = client.fix.call_args.args[1]
    assert "=== ERROS ===" in enviado
    assert "AVISOS" not in enviado
    assert "conceito_novo" not in enviado


def test_fix_diagnostics_so_tem_erros(tmp_path):
    import synesis

    ctx = _ctx_para_validacao(tmp_path)
    result = synesis.load(
        project_content=ctx["project_content"],
        template_content=ctx["template_content"],
        annotation_contents={"x.syn": _annotation("alpha2020", tag="conceito_novo")},
        **bibliography_kwargs(ctx),
    )
    assert result.validation_result.warnings  # UndefinedCode existe...

    texto = validator._fix_diagnostics(result)

    assert "conceito_novo" not in texto  # ...mas não chega ao modelo


# ---------------------------------------------------------------------------
# §6.2.2 — dataset: todas as linhas, e chave repetida aborta
# ---------------------------------------------------------------------------

_DATASET_TEMPLATE = """\
TEMPLATE demo

SOURCE FIELDS
    REQUIRED researcher_id ON DATASET "meta.id"
END SOURCE FIELDS

ITEM FIELDS
    REQUIRED quote
END ITEM FIELDS

FIELD researcher_id TYPE TEXT
    SCOPE SOURCE
    IDENTIFIES researcher
END FIELD

FIELD quote TYPE QUOTATION
    SCOPE ITEM
END FIELD
"""


def _dataset_project(tmp_path: Path, includes: str) -> Path:
    _write(tmp_path / "demo.synt", _DATASET_TEMPLATE)
    synp = tmp_path / "demo.synp"
    _write(synp, f'PROJECT demo\nTEMPLATE "demo.synt"\n{includes}END PROJECT\n')
    return synp


@requer_synesis_013
def test_dataset_soma_todas_as_linhas(tmp_path):
    _write(tmp_path / "lote1/r1.toml", '[meta]\nid = "rec-1"\n')
    _write(tmp_path / "lote2/r2.toml", '[meta]\nid = "rec-2"\n')
    synp = _dataset_project(tmp_path, 'INCLUDE DATASET "lote1"\nINCLUDE DATASET "lote2/*.toml"\n')

    ctx = load_project(synp, load_annotations=False)

    assert set(ctx["dataset_index"]) == {"rec-1", "rec-2"}


@requer_synesis_013
def test_dataset_chave_repetida_aborta(tmp_path):
    _write(tmp_path / "d/a.toml", '[meta]\nid = "rec-1"\n')
    _write(tmp_path / "d/b.toml", '[meta]\nid = "rec-1"\n')
    synp = _dataset_project(tmp_path, 'INCLUDE DATASET "d"\n')

    with pytest.raises(ValueError, match="rec-1"):
        load_project(synp, load_annotations=False)
