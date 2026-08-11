"""
test_dataset_mode.py - Fase 4: parsing/serialização determinística do modo dataset.

Cobre APENAS a parte SEM chamada de LLM/API:
  - parse_dataset_records extrai (bibref, text) do ctx["dataset_index"];
  - _serialize_record prioriza seções CONTEXT FROM DATASET (com pré-filtro);
  - fallback serializa o registro sem chaves internas;
  - erro claro quando não há dataset carregado.

Nenhum teste aqui instancia LLMClient nem faz chamada de rede.
"""

from __future__ import annotations

import pytest
from synesis.ast.nodes import FieldSpec, FieldType, Scope

from synesis_coder.modes.dataset_mode import (
    _serialize_record,
    parse_dataset_records,
)


def _spec_with_context(sections):
    return FieldSpec(
        name="chain", type=FieldType.CHAIN, scope=Scope.ITEM,
        context_from_dataset=sections,
    )


_RECORD = {
    "meta": {"id": "rec-1"},
    "linhas": [{"nome": "Otimização"}, {"nome": "ML"}],
    "projetos": [
        {"nome": "P1", "ano_fim": "Atual"},
        {"nome": "P2", "ano_fim": "2019"},
    ],
    "_source_file": "rec-1.toml",
}


def test_parse_records_requires_dataset():
    with pytest.raises(ValueError):
        parse_dataset_records({"dataset_index": None, "field_specs": {}})


def test_parse_records_yields_bibref_and_text():
    ctx = {"dataset_index": {"rec-1": _RECORD}, "field_specs": {}}
    entries = parse_dataset_records(ctx)
    assert len(entries) == 1
    assert entries[0]["bibref"] == "rec-1"
    assert "linhas" in entries[0]["text"] or "projetos" in entries[0]["text"]


def test_serialize_uses_declared_context_sections_with_prefilter():
    ctx = {
        "field_specs": {"chain": _spec_with_context(["linhas", "projetos[ano_fim=Atual]"])},
    }
    text = _serialize_record(ctx, _RECORD)
    # a seção declarada aparece
    assert "[linhas]" in text
    assert "[projetos[ano_fim=Atual]]" in text
    # o pré-filtro reduziu projetos a P1 (Atual), excluindo P2 (2019)
    assert "P1" in text
    assert "P2" not in text


def test_serialize_fallback_excludes_internal_keys():
    ctx = {"field_specs": {}}  # nenhum CONTEXT declarado -> fallback
    text = _serialize_record(ctx, _RECORD)
    assert "_source_file" not in text
    assert "meta" in text  # conteúdo de domínio presente


def test_strip_external_fields_removes_on_dataset_lines():
    """Rede de segurança §11.3: linha de campo ON DATASET é removida do bloco."""
    from synesis_coder.modes.dataset_mode import (
        _external_origin_fields,
        _strip_external_fields,
    )

    ctx = {
        "field_specs": {
            "lattes_id": FieldSpec(
                name="lattes_id", type=FieldType.TEXT, scope=Scope.SOURCE,
                value_origin="dataset", dataset_path="m.id",
            ),
            "trecho": FieldSpec(name="trecho", type=FieldType.QUOTATION, scope=Scope.ITEM),
        }
    }
    external = _external_origin_fields(ctx)
    assert external == {"lattes_id"}

    text = (
        "SOURCE @rec-1\n    lattes_id: false\nEND SOURCE\n\n"
        "ITEM @rec-1\n    trecho: um trecho\nEND ITEM"
    )
    out = _strip_external_fields(text, external)
    assert "lattes_id" not in out          # campo externo removido
    assert "trecho: um trecho" in out       # campo de documento preservado
    assert "SOURCE @rec-1" in out           # moldura intacta


# ---------------------------------------------------------------------------
# --dataset (dataset_glob_override): restringe o corpus a 1 arquivo, sem tocar
# no .synp em disco. Fixture real em tmp_path (load_project lê do filesystem);
# sem chamada de LLM/API.
# ---------------------------------------------------------------------------

_MIN_TEMPLATE = """
TEMPLATE t

SOURCE FIELDS
    REQUIRED rid ON DATASET "id"
END SOURCE FIELDS

FIELD rid TYPE TEXT
    SCOPE SOURCE
    IDENTIFIES researcher
END FIELD

ITEM FIELDS
    OPTIONAL trecho
END ITEM FIELDS

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD
"""

_MIN_PROJECT = """
PROJECT t

TEMPLATE "t.synt"
INCLUDE DATASET "curriculos/*.toml"

END PROJECT
"""


def _write_min_project(tmp_path):
    (tmp_path / "t.synt").write_text(_MIN_TEMPLATE, encoding="utf-8")
    (tmp_path / "t.synp").write_text(_MIN_PROJECT, encoding="utf-8")
    curriculos = tmp_path / "curriculos"
    curriculos.mkdir()
    (curriculos / "a.toml").write_text('id = "AAA"\n', encoding="utf-8")
    (curriculos / "b.toml").write_text('id = "BBB"\n', encoding="utf-8")
    return tmp_path / "t.synp"


def test_dataset_glob_override_restringe_a_um_arquivo(tmp_path):
    """dataset_loader normaliza a chave para minúsculas (ver dataset_loader.py)."""
    from synesis_coder.project_loader import load_project

    synp = _write_min_project(tmp_path)

    ctx_full = load_project(synp)
    assert set(ctx_full["dataset_index"].keys()) == {"aaa", "bbb"}

    ctx_one = load_project(
        synp, dataset_glob_override=str(tmp_path / "curriculos" / "a.toml")
    )
    assert set(ctx_one["dataset_index"].keys()) == {"aaa"}


def test_dataset_glob_override_none_preserva_comportamento_padrao(tmp_path):
    """Sem override (default None), o glob do .synp continua sendo usado."""
    from synesis_coder.project_loader import load_project

    synp = _write_min_project(tmp_path)
    ctx = load_project(synp, dataset_glob_override=None)
    assert set(ctx["dataset_index"].keys()) == {"aaa", "bbb"}
