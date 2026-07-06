"""Testes de block_assembler — dict de valores → texto Synesis (sem LLM).

Cobre os ganhos por construção da Opção 3: CODE com vírgula (E033/E015),
CHAIN hops → setas, OPTIONAL omitido, moldura determinística mesmo com chaves
extras/ausentes, envelope items → N blocos. Inclui uma checagem de compilação
real via synesis.load().
"""

from __future__ import annotations

from pathlib import Path

import synesis
from synesis.ast.nodes import FieldSpec, FieldType, Scope

from synesis_coder.block_assembler import (
    _render_chains,
    _render_code,
    assemble_items,
    assemble_source,
)
from synesis_coder.project_loader import load_project
from synesis_coder.validator import _has_structural_errors

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"


def _ctx_with_item_fields(fields: dict, required: list | None = None) -> dict:
    return {
        "item_fields": fields,
        "required_item": required or [],
        "source_fields": {},
        "required_source": [],
    }


class TestRenderCode:
    def test_list_joined_with_comma_space(self):
        assert _render_code(["a", "b", "c"]) == "a, b, c"

    def test_list_strips_and_drops_empties(self):
        assert _render_code([" a ", "", "b"]) == "a, b"

    def test_string_passthrough(self):
        assert _render_code("single") == "single"

    def test_list_normalizes_case(self):
        assert _render_code(["Graduacao_Curso", "graduacao_curso"]) == "graduacao_curso, graduacao_curso"

    def test_string_normalizes_case(self):
        assert _render_code("Titulo") == "titulo"


class TestRenderChains:
    def test_single_hop(self):
        hops = [{"source": "a", "relation": "ENABLES", "target": "b"}]
        assert _render_chains(hops) == ["a -> ENABLES -> b"]

    def test_contiguous_hops_interleaved(self):
        hops = [
            {"source": "a", "relation": "R1", "target": "b"},
            {"source": "b", "relation": "R2", "target": "c"},
        ]
        assert _render_chains(hops) == ["a -> R1 -> b -> R2 -> c"]

    def test_concepts_normalized_to_snake_case(self):
        hops = [{"source": "community trust", "relation": "ENABLES", "target": "social acceptance"}]
        assert _render_chains(hops) == ["community_trust -> ENABLES -> social_acceptance"]

    def test_concepts_normalized_to_lowercase(self):
        hops = [{"source": "Graduacao_Curso", "relation": "RELATES", "target": "Titulo"}]
        assert _render_chains(hops) == ["graduacao_curso -> RELATES -> titulo"]

    def test_non_contiguous_hops_separate_lines(self):
        hops = [
            {"source": "a", "relation": "R", "target": "b"},
            {"source": "x", "relation": "R", "target": "y"},
        ]
        assert _render_chains(hops) == ["a -> R -> b", "x -> R -> y"]

    def test_empty_relation_produces_binary_chain(self):
        # Relação vazia → chain binária A -> B (sem rótulo de relação)
        hops = [{"source": "a", "relation": "", "target": "b"}]
        assert _render_chains(hops) == ["a -> b"]

    def test_linked_to_is_preserved_as_legitimate_relation(self):
        # "linked_to" é relação legítima de usuário — NÃO deve ser omitida
        hops = [{"source": "a", "relation": "linked_to", "target": "b"}]
        assert _render_chains(hops) == ["a -> linked_to -> b"]

    def test_untyped_sentinel_omitted(self):
        # "__untyped__" é o sentinel reservado do schema_builder — deve ser omitido
        hops = [{"source": "a", "relation": "__untyped__", "target": "b"}]
        assert _render_chains(hops) == ["a -> b"]

    def test_missing_source_or_target_skipped(self):
        hops = [{"source": "", "relation": "R", "target": "b"}]
        assert _render_chains(hops) == []


class TestAssembleItems:
    def test_frame_is_deterministic(self):
        fields = {
            "text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM),
            "code": FieldSpec(name="code", type=FieldType.CODE, scope=Scope.ITEM),
        }
        ctx = _ctx_with_item_fields(fields)
        data = {"items": [{"text": "hello", "code": ["x", "y"]}]}
        out = assemble_items(ctx, "@smith2024", data)
        assert out.startswith("ITEM @smith2024")
        assert out.rstrip().endswith("END ITEM")
        assert "    text: hello" in out
        assert "    code: x, y" in out  # CODE com vírgula (E033/E015 eliminado)

    def test_bibref_at_prefix_stripped(self):
        fields = {"text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM)}
        ctx = _ctx_with_item_fields(fields)
        out = assemble_items(ctx, "@smith2024", {"items": [{"text": "x"}]})
        assert "ITEM @smith2024" in out
        assert "@@" not in out

    def test_extra_keys_ignored(self):
        fields = {"text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM)}
        ctx = _ctx_with_item_fields(fields)
        data = {"items": [{"text": "x", "bogus_field": "should be dropped"}]}
        out = assemble_items(ctx, "smith2024", data)
        assert "bogus_field" not in out

    def test_optional_absent_field_omitted(self):
        fields = {
            "text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM),
            "note": FieldSpec(name="note", type=FieldType.MEMO, scope=Scope.ITEM),
        }
        ctx = _ctx_with_item_fields(fields, required=["text"])
        out = assemble_items(ctx, "smith2024", {"items": [{"text": "x"}]})
        assert "note:" not in out

    def test_required_absent_field_gets_na(self):
        # Campos REQUIRED que o LLM omitiu recebem "NA" em vez de serem omitidos.
        fields = {
            "text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM),
            "note": FieldSpec(name="note", type=FieldType.MEMO, scope=Scope.ITEM),
        }
        ctx = _ctx_with_item_fields(fields, required=["text", "note"])
        out = assemble_items(ctx, "smith2024", {"items": [{}]})
        assert "text: NA" in out
        assert "note: NA" in out

    def test_required_empty_string_gets_na(self):
        fields = {"text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM)}
        ctx = _ctx_with_item_fields(fields, required=["text"])
        out = assemble_items(ctx, "smith2024", {"items": [{"text": ""}]})
        assert "text: NA" in out

    def test_optional_absent_never_gets_na(self):
        fields = {
            "text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM),
            "note": FieldSpec(name="note", type=FieldType.MEMO, scope=Scope.ITEM),
        }
        ctx = _ctx_with_item_fields(fields, required=["text"])
        out = assemble_items(ctx, "smith2024", {"items": [{"text": "x"}]})
        # "note" é OPTIONAL e ausente → deve ficar fora do output completamente
        assert "note" not in out

    def test_envelope_produces_n_blocks(self):
        fields = {"text": FieldSpec(name="text", type=FieldType.QUOTATION, scope=Scope.ITEM)}
        ctx = _ctx_with_item_fields(fields)
        data = {"items": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
        out = assemble_items(ctx, "smith2024", data)
        assert out.count("ITEM @smith2024") == 3
        assert out.count("END ITEM") == 3

    def test_multiline_value_normalized(self):
        fields = {"note": FieldSpec(name="note", type=FieldType.MEMO, scope=Scope.ITEM)}
        ctx = _ctx_with_item_fields(fields)
        out = assemble_items(ctx, "smith2024", {"items": [{"note": "line1\nline2"}]})
        assert "line1 line2" in out
        assert "note: line1\nline2" not in out


class TestAssembleSource:
    def test_source_block(self):
        ctx = {
            "source_fields": {
                "description": FieldSpec(name="description", type=FieldType.TEXT, scope=Scope.SOURCE),
            },
            "required_source": ["description"],
        }
        out = assemble_source(ctx, "smith2024", {"description": "a study"})
        assert out.startswith("SOURCE @smith2024")
        assert "    description: a study" in out
        assert out.rstrip().endswith("END SOURCE")


class TestAssemblerCompiles:
    def test_real_project_output_compiles(self):
        """Output do assembler (SOURCE + ITEM com bundle) compila sem erros."""
        ctx = load_project(PROJECT_SOCIAL)
        src = assemble_source(
            ctx, "abdin2024",
            {"description": "x", "epistemic_model": "y", "method": "z"},
        )
        items = assemble_items(
            ctx, "abdin2024",
            {"items": [{
                "text": "Community trust is key.",
                "note": "author argues trust precedes acceptance",
                "chain": [
                    {"source": "community trust", "relation": "ENABLES", "target": "social acceptance"},
                ],
            }]},
        )
        out = src + "\n\n" + items
        validation = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"t.syn": out},
            bibliography_content=ctx.get("bib_content"),
        )
        assert not _has_structural_errors(validation), validation.get_diagnostics()
