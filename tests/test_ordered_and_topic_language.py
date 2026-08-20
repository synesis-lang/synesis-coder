"""Contrato de ORDERED (índice) e idioma de TOPIC nos prompts.

ORDERED: o dado gravado é o ÍNDICE — escrever o rótulo é erro E088 no
compilador. O schema restringe o caminho JSON aos índices e o assembler resolve
rótulo→índice como defesa independente do backend.

TOPIC: é tipo distinto de TEXT e precisa ser citado explicitamente na instrução
de idioma, senão o modelo aplica seu default (inglês) mesmo com
SYNESIS_CODER_LANGUAGE definido.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from synesis.ast.nodes import (
    FieldSpec,
    FieldType,
    OrderedValue,
    Scope,
    SourceLocation,
)

from synesis_coder.block_assembler import _render_field

LOC = SourceLocation(file=Path("t.syno"), line=1, column=1)


def _aspect_spec() -> FieldSpec:
    return FieldSpec(
        name="aspect",
        type=FieldType.ORDERED,
        scope=Scope.ONTOLOGY,
        values=[
            OrderedValue(index=0, label="Indefinido", description="", location=LOC),
            OrderedValue(index=11, label="Econômico", description="", location=LOC),
        ],
        description="",
        location=LOC,
    )


class TestAssemblerEmitsIndex:
    """O assembler emite sempre o índice, resolvendo o rótulo se preciso."""

    def test_int_index_passes_through(self):
        assert _render_field("aspect", _aspect_spec(), 11) == ["aspect: 11"]

    def test_index_zero_is_emitted(self):
        # 0 é falsy: não pode ser confundido com ausência de valor.
        assert _render_field("aspect", _aspect_spec(), 0) == ["aspect: 0"]

    def test_numeric_string_passes_through(self):
        assert _render_field("aspect", _aspect_spec(), "11") == ["aspect: 11"]

    @pytest.mark.parametrize("label", ["Econômico", "ECONOMICO", "economico", " Econômico "])
    def test_label_variants_resolve_to_index(self, label):
        """Defesa contra backend que ignore o `enum` e devolva o rótulo."""
        assert _render_field("aspect", _aspect_spec(), label) == ["aspect: 11"]

    def test_unknown_label_is_emitted_as_is(self):
        """Silenciar aqui esconderia o problema — quem reporta é o compilador."""
        assert _render_field("aspect", _aspect_spec(), "Gastronomico") == [
            "aspect: Gastronomico"
        ]

    def test_bool_is_not_treated_as_index(self):
        # bool é subclasse de int em Python.
        assert _render_field("aspect", _aspect_spec(), True) == ["aspect: true"]

    def test_empty_value_emits_nothing(self):
        assert _render_field("aspect", _aspect_spec(), "") == []


class TestOrderedPromptFormatting:
    """O prompt de texto livre precisa dizer QUAL lado escrever."""

    def test_generic_instruction_demands_the_number(self):
        from synesis_coder.prompt_builder import _generic_instruction

        text = _generic_instruction(FieldType.ORDERED)
        assert "NUMBER" in text
        assert "not its label" in text

    def test_values_list_gives_index_the_salience(self):
        from synesis_coder.prompt_builder import _format_values

        lines = _format_values(_aspect_spec())
        assert any(line.strip().startswith("11  (Econômico)") for line in lines)

    def test_enumerated_values_keep_label_form(self):
        from synesis_coder.prompt_builder import _format_values

        spec = FieldSpec(
            name="zone",
            type=FieldType.ENUMERATED,
            scope=Scope.ITEM,
            values=[OrderedValue(index=-1, label="Aim", description="", location=LOC)],
            description="",
            location=LOC,
        )
        assert any("Aim" in line for line in _format_values(spec))


class TestTopicLanguageInstruction:
    """TOPIC é tipo distinto de TEXT e precisa constar da instrução de idioma."""

    def _sources(self) -> list[str]:
        import inspect

        from synesis_coder import prompt_builder

        src = inspect.getsource(prompt_builder)
        return [
            block
            for block in src.split("OUTPUT LANGUAGE")[1:]
        ]

    def test_every_language_instruction_mentions_topic(self):
        blocks = self._sources()
        assert blocks, "nenhuma instrução OUTPUT LANGUAGE encontrada"
        for block in blocks:
            head = block[:220]
            assert "TOPIC" in head, f"instrução sem TOPIC: {head[:80]!r}"

    def test_all_six_sites_are_covered(self):
        """Os 6 sites cobrem ontology, abstract, document e dataset."""
        assert len(self._sources()) == 6


class TestOrderedSchemaProviderCompatibility:
    """O Gemini recusa `enum` numérico; índices contíguos viram faixa.

    Medido em produção (2026-08-20, `google/gemini-3.7-flash`): `{"enum": [0,1]}`
    devolvia HTTP 400 com "schema at top-level requires unspecified property",
    mensagem que não menciona o enum. Cada recusa derrubava a chamada para texto
    livre, descartando as garantias do schema.
    """

    def _spec(self, indices):
        return FieldSpec(
            name="aspect",
            type=FieldType.ORDERED,
            scope=Scope.ONTOLOGY,
            values=[
                OrderedValue(index=i, label=f"L{i}", description="", location=LOC)
                for i in indices
            ],
            description="",
            location=LOC,
        )

    def test_contiguous_indices_become_a_range(self):
        from synesis_coder.schema_builder import field_to_schema

        assert field_to_schema(self._spec(range(16))) == {
            "type": "integer",
            "minimum": 0,
            "maximum": 15,
        }

    def test_range_carries_no_numeric_enum(self):
        from synesis_coder.schema_builder import field_to_schema

        assert "enum" not in field_to_schema(self._spec(range(16)))

    def test_gap_keeps_enum_to_avoid_admitting_a_missing_index(self):
        from synesis_coder.schema_builder import field_to_schema

        # Sem o 5: a faixa 0..7 aceitaria um índice que o template não declara.
        schema = field_to_schema(self._spec([0, 1, 2, 3, 4, 6, 7]))
        assert schema == {"enum": [0, 1, 2, 3, 4, 6, 7]}

    def test_single_value_is_a_degenerate_range(self):
        from synesis_coder.schema_builder import field_to_schema

        assert field_to_schema(self._spec([3])) == {
            "type": "integer",
            "minimum": 3,
            "maximum": 3,
        }

    def test_nullable_preserves_the_range_bounds(self):
        from synesis_coder.schema_builder import _nullable, field_to_schema

        nullable = _nullable(field_to_schema(self._spec(range(3))))
        assert nullable["type"] == ["integer", "null"]
        assert nullable["minimum"] == 0 and nullable["maximum"] == 2
