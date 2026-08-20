"""Testes de duas garantias de prompt/observabilidade do coder.

1. Campos SOURCE recebem, no texto do prompt, os valores/relações/faixa
   derivados do tipo — como já ocorria em ITEM e ONTOLOGY. Sem isso, um
   ENUMERATED em SOURCE ficava sem lista de valores no caminho de TEXTO LIVRE
   (o `enum` do schema só protege o caminho JSON).

2. O abandono do caminho JSON é reportado ao pesquisador. O contador existia,
   mas só era exibido com `--format verbose`; no formato padrão a degradação
   era invisível e o registro contava como OK.

Nenhum destes testes chama a API.
"""

from __future__ import annotations

import logging

from synesis.parser.template_loader import load_template_from_string

from synesis_coder.prompt_builder import (
    _build_item_fields_section,
    _build_source_fields_section,
)
from synesis_coder.runtime_info import warn_schema_fallbacks
from synesis_coder.token_usage import TokenUsage

# ---------------------------------------------------------------------------
# Template sintético: exercita ENUMERATED, ORDERED e SCALE no escopo SOURCE
# ---------------------------------------------------------------------------

TEMPLATE = """TEMPLATE t

SOURCE FIELDS
    REQUIRED area
    OPTIONAL maturity, score, plain_text, no_guidelines
END SOURCE FIELDS

FIELD area TYPE ENUMERATED
    SCOPE SOURCE
    DESCRIPTION Área disciplinar.
    VALUES
        Economics: Estudos econômicos
        Accounting: Estudos contábeis
    END VALUES
    GUIDELINES
        Escolha exatamente uma das opções acima.
    END GUIDELINES
END FIELD

FIELD maturity TYPE ORDERED
    SCOPE SOURCE
    DESCRIPTION Maturidade.
    VALUES
        [0] Low: Baixa
        [1] High: Alta
    END VALUES
END FIELD

FIELD score TYPE SCALE
    SCOPE SOURCE
    FORMAT [0..10]
    DESCRIPTION Nota.
END FIELD

FIELD plain_text TYPE TEXT
    SCOPE SOURCE
    DESCRIPTION Texto livre.
    GUIDELINES
        Escreva uma frase.
    END GUIDELINES
END FIELD

FIELD no_guidelines TYPE TEXT
    SCOPE SOURCE
END FIELD

ITEM FIELDS
    REQUIRED quote
END ITEM FIELDS

FIELD quote TYPE QUOTATION
    SCOPE ITEM
    DESCRIPTION Trecho.
END FIELD
"""


def _ctx() -> dict:
    """Monta o ctx mínimo que as funções de seção consomem."""
    from synesis.ast.nodes import Scope

    template = load_template_from_string(TEMPLATE, "t.synt")
    specs = template.field_specs
    return {
        "source_fields": {
            n: s for n, s in specs.items() if s.scope == Scope.SOURCE
        },
        "item_fields": {
            n: s for n, s in specs.items() if s.scope == Scope.ITEM
        },
        "required_source": list(template.required_fields.get(Scope.SOURCE, [])),
        "required_item": list(template.required_fields.get(Scope.ITEM, [])),
        "chain_relations": {},
    }


class TestSourceFieldValuesInPrompt:
    """A seção SOURCE deve entregar os valores permitidos, como a seção ITEM."""

    def test_enumerated_source_field_lists_allowed_values(self):
        section = _build_source_fields_section(_ctx())

        assert "Allowed values" in section
        assert "Economics" in section
        assert "Accounting" in section

    def test_value_descriptions_are_included(self):
        """As descrições desambiguam a escolha e não existem no enum do schema."""
        section = _build_source_fields_section(_ctx())

        assert "Estudos econômicos" in section
        assert "Estudos contábeis" in section

    def test_ordered_source_field_lists_indexed_values(self):
        """O índice tem saliência; o rótulo entra como glosa entre parênteses.

        A forma antiga (`0: Low`) sugeria os dois lados com o mesmo peso e
        produzia anotações com o rótulo — hoje erro E088 no compilador.
        """
        section = _build_source_fields_section(_ctx())

        assert "0  (Low)" in section
        assert "1  (High)" in section
        assert "write the NUMBER, never the label" in section

    def test_scale_source_field_shows_range(self):
        section = _build_source_fields_section(_ctx())

        assert "Range: [0..10]" in section

    def test_guidelines_still_take_precedence(self):
        """A GUIDELINE do autor continua sendo a instrução principal."""
        section = _build_source_fields_section(_ctx())

        assert "Escolha exatamente uma das opções acima." in section
        assert "Escreva uma frase." in section

    def test_source_specific_fallback_is_preserved(self):
        """Campo SOURCE sem guidelines/description mantém o genérico por NOME.

        Regressão: usar o genérico por TIPO degradaria campos de metadado
        documental (description/method) para instruções mais fracas.
        """
        from synesis_coder.prompt_builder import _generic_source_instruction

        ctx = _ctx()
        section = _build_source_fields_section(ctx)

        expected = _generic_source_instruction("no_guidelines")
        assert expected in section

    def test_text_field_gets_no_spurious_values_block(self):
        """Campos sem VALUES não devem ganhar seção de valores."""
        section = _build_source_fields_section(_ctx())
        block = section.split("plain_text (")[1].split("\n  ")[0]

        assert "Allowed values" not in block

    def test_item_section_unchanged(self):
        """A seção ITEM continua produzindo o que já produzia."""
        section = _build_item_fields_section(_ctx())

        assert "quote (QUOTATION) [REQUIRED]" in section


class TestSchemaFallbackWarning:
    """O abandono do caminho JSON precisa chegar ao pesquisador."""

    class _Client:
        def __init__(self, n: int) -> None:
            self.usage = TokenUsage()
            for _ in range(n):
                self.usage.record_schema_fallback()

    def test_warns_when_fallback_occurred(self, caplog):
        with caplog.at_level(logging.WARNING, logger="synesis_coder.runtime_info"):
            warn_schema_fallbacks(self._Client(2))

        assert "TEXTO LIVRE" in caplog.text
        assert "2" in caplog.text

    def test_silent_when_no_fallback(self, caplog):
        with caplog.at_level(logging.WARNING, logger="synesis_coder.runtime_info"):
            warn_schema_fallbacks(self._Client(0))

        assert caplog.text == ""

    def test_warning_level_is_warning(self, caplog):
        """WARNING, não INFO: `-q` deve preservar o aviso."""
        with caplog.at_level(logging.WARNING, logger="synesis_coder.runtime_info"):
            warn_schema_fallbacks(self._Client(1))

        assert any(r.levelno == logging.WARNING for r in caplog.records)
