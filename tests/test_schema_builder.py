"""Testes de schema_builder — FieldSpec → JSON Schema (sem LLM).

Usa o projeto real social_acceptance (que exercita CODE-via-CHAIN, ORDERED,
ENUMERATED e SCALE no escopo ONTOLOGY) e FieldSpecs sintéticos para os tipos
não cobertos pelos fixtures.
"""

from __future__ import annotations

from pathlib import Path

from synesis.ast.nodes import FieldSpec, FieldType, OrderedValue, Scope

from synesis_coder.project_loader import load_project
from synesis_coder.schema_builder import (
    build_item_schema,
    build_source_schema,
    field_to_schema,
)

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"


def _ov(index: int, label: str) -> OrderedValue:
    return OrderedValue(index=index, label=label, description="", location=None)


class TestFieldToSchema:
    def test_text_is_string(self):
        spec = FieldSpec(name="t", type=FieldType.TEXT, scope=Scope.ITEM)
        assert field_to_schema(spec) == {"type": "string"}

    def test_quotation_memo_date_topic_are_string(self):
        for ft in (FieldType.QUOTATION, FieldType.MEMO, FieldType.DATE, FieldType.TOPIC):
            spec = FieldSpec(name="f", type=ft, scope=Scope.ITEM)
            assert field_to_schema(spec) == {"type": "string"}

    def test_code_is_array_of_strings(self):
        spec = FieldSpec(name="c", type=FieldType.CODE, scope=Scope.ITEM)
        assert field_to_schema(spec) == {
            "type": "array",
            "items": {"type": "string"},
        }

    def test_enumerated_becomes_enum(self):
        spec = FieldSpec(
            name="confidence",
            type=FieldType.ENUMERATED,
            scope=Scope.ONTOLOGY,
            values=[_ov(-1, "LOW"), _ov(-1, "MEDIUM"), _ov(-1, "HIGH")],
        )
        assert field_to_schema(spec) == {"enum": ["LOW", "MEDIUM", "HIGH"]}

    def test_ordered_becomes_enum(self):
        spec = FieldSpec(
            name="dimension",
            type=FieldType.ORDERED,
            scope=Scope.ONTOLOGY,
            values=[_ov(0, "Undefined"), _ov(1, "Community_Acceptance")],
        )
        assert field_to_schema(spec) == {"enum": ["Undefined", "Community_Acceptance"]}

    def test_enum_without_values_degrades_to_string(self):
        spec = FieldSpec(name="e", type=FieldType.ENUMERATED, scope=Scope.ITEM)
        assert field_to_schema(spec) == {"type": "string"}

    def test_scale_min_max_from_format(self):
        spec = FieldSpec(
            name="sig", type=FieldType.SCALE, scope=Scope.ONTOLOGY, format="[0..5]"
        )
        assert field_to_schema(spec) == {
            "type": "integer",
            "minimum": 0,
            "maximum": 5,
        }

    def test_scale_without_format_is_plain_integer(self):
        spec = FieldSpec(name="s", type=FieldType.SCALE, scope=Scope.ITEM)
        assert field_to_schema(spec) == {"type": "integer"}

    def test_chain_hops_with_relation_enum(self):
        spec = FieldSpec(
            name="chain",
            type=FieldType.CHAIN,
            scope=Scope.ITEM,
            arity=">= 2",
            relations={"ENABLES": "", "INFLUENCES": ""},
        )
        schema = field_to_schema(spec)
        assert schema["type"] == "array"
        hop = schema["items"]
        assert hop["properties"]["relation"] == {"enum": ["ENABLES", "INFLUENCES"]}
        assert hop["required"] == ["source", "relation", "target"]
        assert hop["additionalProperties"] is False

    def test_chain_without_relations_uses_untyped_sentinel(self):
        spec = FieldSpec(name="chain", type=FieldType.CHAIN, scope=Scope.ITEM)
        schema = field_to_schema(spec)
        assert schema["items"]["properties"]["relation"] == {"const": "__untyped__"}


class TestBuildSchemasFromRealProject:
    def test_item_schema_envelope(self):
        ctx = load_project(PROJECT_SOCIAL)
        schema = build_item_schema(ctx)
        assert schema["properties"]["items"]["type"] == "array"
        item_obj = schema["properties"]["items"]["items"]
        # Todos os campos ITEM presentes; additionalProperties=false elimina E022
        assert set(item_obj["properties"]) == set(ctx["item_fields"])
        assert item_obj["additionalProperties"] is False
        # REQUIRED do template propagado
        assert "text" in item_obj["required"]

    def test_source_schema(self):
        ctx = load_project(PROJECT_SOCIAL)
        schema = build_source_schema(ctx)
        assert set(schema["properties"]) == set(ctx["source_fields"])
        assert schema["additionalProperties"] is False
        for req in ctx["required_source"]:
            assert req in schema["required"]
