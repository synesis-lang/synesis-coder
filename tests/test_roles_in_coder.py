"""Etapa A0b: papéis (ROLE) no modo ontology do coder (Spec ROLE/EXPORT v7, §11)."""

from __future__ import annotations

from types import SimpleNamespace

from synesis.ast.nodes import FieldType

from synesis_coder.concept_index import definition_field
from synesis_coder.modes.ontology_mode import _generation_ctx
from synesis_coder.prompt_builder import _build_ontology_fields_section


def _spec(field_type, role=None, description=None):
    return SimpleNamespace(type=field_type, role=role, description=description, guidelines=None,
                           values=None, format=None, relations=None, arity=None, name="x")


FIELDS = {
    "definicao": _spec(FieldType.TEXT, "NOTE DEFINITION"),
    "usado_por": _spec(FieldType.TEXT, "LABEL ALTERNATIVE"),
    "tipo_de": _spec(FieldType.CODE, "HIERARCHY KIND"),
    "nota": _spec(FieldType.TEXT, None, "Uma nota qualquer"),
}


def test_label_and_structural_fields_are_not_generated():
    gen = _generation_ctx({"ontology_fields": FIELDS, "required_ontology": ["definicao", "usado_por"]})
    assert set(gen["ontology_fields"]) == {"definicao", "nota"}
    assert gen["required_ontology"] == ["definicao"]


def test_role_is_shown_to_the_model_with_a_default_instruction():
    gen = _generation_ctx({"ontology_fields": FIELDS, "required_ontology": ["definicao"]})
    text = _build_ontology_fields_section(gen)
    assert "definicao (TEXT, NOTE DEFINITION) [REQUIRED]" in text
    assert "Define the concept by how it is used in the corpus" in text


def test_definition_field_comes_from_the_role():
    fields = {
        "a_descricao": _spec(FieldType.TEXT, None, "Definição do conceito"),
        "definicao": _spec(FieldType.TEXT, "NOTE DEFINITION"),
    }
    assert definition_field(fields) == "definicao"


def test_without_role_the_heuristic_still_applies():
    fields = {"ontology_description": _spec(FieldType.TEXT, None, "Definição do conceito")}
    assert definition_field(fields) == "ontology_description"
