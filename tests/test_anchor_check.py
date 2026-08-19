"""Testes para anchor_check — Fase 4 (verificação determinística de ancoragem).

Sem LLM: toda a verificação é comparação de strings normalizadas.
"""

from __future__ import annotations

import textwrap

from synesis_coder.anchor_check import (
    AnchorIssue,
    check_anchoring,
    format_report,
)

_BIB = textwrap.dedent("""\
    @article{smith2024,
      title = {Trust and acceptance},
      abstract = {Community trust enables social acceptance of wind energy.
                  Local ownership further reduces opposition in rural areas.}
    }

    @article{jones2020,
      title = {No abstract here},
      year = {2020}
    }
    """)


def _syn(*items: str) -> str:
    return "\n".join(items)


class TestCheckAnchoring:
    def test_anchored_item_produces_no_issue(self):
        content = textwrap.dedent("""\
            ITEM @smith2024
                text: Community trust enables social acceptance of wind energy.
                zone: Result
            END ITEM
            """)
        assert check_anchoring(content, _BIB) == []

    def test_unanchored_item_reported(self):
        content = textwrap.dedent("""\
            ITEM @smith2024
                text: This sentence appears nowhere in the source.
                zone: Result
            END ITEM
            """)
        issues = check_anchoring(content, _BIB)
        assert len(issues) == 1
        assert issues[0].bibref == "smith2024"
        assert "appears nowhere" in issues[0].excerpt

    def test_reports_correct_line_number(self):
        content = "# header\n\n" + textwrap.dedent("""\
            ITEM @smith2024
                text: Absent sentence.
            END ITEM
            """)
        issues = check_anchoring(content, _BIB)
        assert issues[0].line == 3

    def test_tolerates_typographic_apostrophes(self):
        bib = "@article{t2024,\n  abstract = {The firm's role is central here.}\n}\n"
        content = "ITEM @t2024\n    text: The firm’s role is central here.\nEND ITEM\n"
        assert check_anchoring(content, bib) == []

    def test_tolerates_latex_escapes_in_bib(self):
        """`BM\\&FBOVESPA` no .bib vs `BM&FBOVESPA` na anotação.

        Encontrado no corpus real (face85/@correa2012): metade dos falsos
        positivos da 1ª execução vinham de escapes LaTeX não normalizados.
        """
        bib = "@article{l2024,\n  abstract = {Listed on the BM\\&FBOVESPA exchange.}\n}\n"
        content = "ITEM @l2024\n    text: Listed on the BM&FBOVESPA exchange.\nEND ITEM\n"
        assert check_anchoring(content, bib) == []

    def test_tolerates_whitespace_differences(self):
        bib = "@article{w2024,\n  abstract = {Trust   enables\n  acceptance now.}\n}\n"
        content = "ITEM @w2024\n    text: Trust enables acceptance now.\nEND ITEM\n"
        assert check_anchoring(content, bib) == []

    def test_case_insensitive_match(self):
        bib = "@article{c2024,\n  abstract = {The Wind Sector Expanded.}\n}\n"
        content = "ITEM @c2024\n    text: the wind sector expanded.\nEND ITEM\n"
        assert check_anchoring(content, bib) == []

    def test_skips_bibref_without_abstract(self):
        """Sem fonte não há o que ancorar — reportar seria ruído."""
        content = "ITEM @jones2020\n    text: Anything at all.\nEND ITEM\n"
        assert check_anchoring(content, _BIB) == []

    def test_skips_unknown_bibref(self):
        content = "ITEM @nosuchref\n    text: Anything.\nEND ITEM\n"
        assert check_anchoring(content, _BIB) == []

    def test_skips_item_without_text_field(self):
        content = "ITEM @smith2024\n    zone: Result\nEND ITEM\n"
        assert check_anchoring(content, _BIB) == []

    def test_multiple_items_mixed(self):
        content = _syn(
            "ITEM @smith2024",
            "    text: Community trust enables social acceptance of wind energy.",
            "END ITEM",
            "",
            "ITEM @smith2024",
            "    text: Fabricated claim not present.",
            "END ITEM",
            "",
            "ITEM @smith2024",
            "    text: Local ownership further reduces opposition in rural areas.",
            "END ITEM",
        )
        issues = check_anchoring(content, _BIB)
        assert len(issues) == 1
        assert "Fabricated" in issues[0].excerpt

    def test_empty_inputs(self):
        assert check_anchoring("", _BIB) == []
        assert check_anchoring("ITEM @x\n    text: y\nEND ITEM\n", "") == []

    def test_custom_field_name(self):
        bib = "@article{q2024,\n  abstract = {A quoted passage lives here.}\n}\n"
        content = "ITEM @q2024\n    quote: A quoted passage lives here.\nEND ITEM\n"
        assert check_anchoring(content, bib, field="quote") == []
        assert len(check_anchoring(content, bib, field="text")) == 0  # campo ausente


class TestFormatReport:
    def test_clean_report(self):
        out = format_report([], 10)
        assert "nenhum problema" in out
        assert "10" in out

    def test_report_lists_issues(self):
        issues = [AnchorIssue("smith2024", 3, "some excerpt")]
        out = format_report(issues, 10)
        assert "1 de 10" in out
        assert "10.0%" in out
        assert "smith2024" in out
        assert "linha 3" in out

    def test_long_excerpt_truncated(self):
        issues = [AnchorIssue("x", 1, "y" * 200)]
        out = format_report(issues, 1)
        assert "..." in out
        assert len(out) < 200
