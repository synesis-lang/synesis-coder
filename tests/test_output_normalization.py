"""Testes das garantias determinísticas sobre a FORMA do output.

Princípio: a moldura do bloco (indentação, contagem, unicidade) é
responsabilidade do Python; ao modelo cabe apenas o CONTEÚDO. O caminho JSON
já garantia isso via `block_assembler`; estes testes cobrem as garantias
levadas ao caminho de texto livre.

Casos reais medidos com `inclusionai/ling-2.6-flash` em 2026-07-30:
- SOURCE emitido sem indentação → erro de parse → registro perdido
- 121 ITEMs para 22 únicos (loop degenerativo)
- registro com 0 ITEMs reportado como OK

Todos offline — sem LLM, sem IO de rede.
"""

from __future__ import annotations

from synesis_coder.block_assembler import (
    count_item_blocks,
    dedupe_item_blocks,
    normalize_indentation,
)
from synesis_coder.validator import _strip_markdown_fences


class TestNormalizeIndentation:
    def test_fixes_unindented_source_real_case(self):
        """Caso real que quebrou o parser e custou o registro."""
        bad = (
            "SOURCE @3355559305779367\n"
            'cargo_institucional: "Professor Associado"\n'
            "END SOURCE"
        )
        out = normalize_indentation(bad)

        assert out.splitlines()[1].startswith("    "), "campo deve ser indentado"
        assert out.splitlines()[0] == "SOURCE @3355559305779367"
        assert out.splitlines()[2] == "END SOURCE"

    def test_normalizes_two_space_indent_to_four(self):
        """Outro registro real usava 2 espaços; canônico é 4."""
        out = normalize_indentation("ITEM @a\n  trecho: \"x\"\nEND ITEM")

        assert out.splitlines()[1] == '    trecho: "x"'

    def test_idempotent(self):
        """Aplicar duas vezes não muda nada."""
        text = "ITEM @a\n    trecho: \"x\"\nEND ITEM"
        assert normalize_indentation(normalize_indentation(text)) == text

    def test_preserves_blank_lines_between_blocks(self):
        out = normalize_indentation("ITEM @a\nx: 1\nEND ITEM\n\nITEM @b\ny: 2\nEND ITEM")

        assert "\n\n" in out
        assert out.count("END ITEM") == 2

    def test_leaves_text_outside_blocks_alone(self):
        """Cabeçalhos de erro não são conteúdo de bloco."""
        text = "# ERRO: validação falhou\n# diagnóstico\n\nITEM @a\nx: 1\nEND ITEM"
        out = normalize_indentation(text)

        assert out.startswith("# ERRO: validação falhou\n# diagnóstico")
        assert "    x: 1" in out

    def test_handles_ontology_blocks(self):
        out = normalize_indentation("ONTOLOGY code_x\ndescription: \"d\"\nEND ONTOLOGY")

        assert out.splitlines()[1] == '    description: "d"'

    def test_does_not_alter_values(self):
        """Normalizar forma não pode mexer em conteúdo."""
        out = normalize_indentation('ITEM @a\ntrecho: "  espaços  internos  "\nEND ITEM')

        assert 'espaços  internos' in out

    def test_wired_into_strip_markdown_fences(self):
        """O gargalo único do validator aplica a normalização."""
        out = _strip_markdown_fences("```synesis\nITEM @a\ntrecho: \"x\"\nEND ITEM\n```")

        assert "```" not in out
        assert '    trecho: "x"' in out


class TestDedupeItemBlocks:
    def test_removes_exact_duplicates(self):
        block = 'ITEM @a\n    criterio: X\n    score: 3\nEND ITEM'
        text = "\n\n".join([block] * 5)
        out, removed = dedupe_item_blocks(text)

        assert removed == 4
        assert count_item_blocks(out) == 1

    def test_preserves_order_and_first_occurrence(self):
        a = 'ITEM @a\n    criterio: A\nEND ITEM'
        b = 'ITEM @a\n    criterio: B\nEND ITEM'
        out, removed = dedupe_item_blocks("\n\n".join([a, b, a]))

        assert removed == 1
        assert out.index("criterio: A") < out.index("criterio: B")

    def test_keeps_distinct_items_with_same_criterio(self):
        """Mesmo critério em trechos diferentes é legítimo — não deduplicar."""
        a = 'ITEM @a\n    trecho: "t1"\n    criterio: X\nEND ITEM'
        b = 'ITEM @a\n    trecho: "t2"\n    criterio: X\nEND ITEM'
        out, removed = dedupe_item_blocks(f"{a}\n\n{b}")

        assert removed == 0
        assert count_item_blocks(out) == 2

    def test_ignores_whitespace_differences(self):
        a = 'ITEM @a\n    criterio: X\nEND ITEM'
        b = 'ITEM @a\n        criterio:  X\nEND ITEM'
        _, removed = dedupe_item_blocks(f"{a}\n\n{b}")

        assert removed == 1, "espaçamento não deve tornar o bloco 'diferente'"

    def test_preserves_source_block(self):
        text = 'SOURCE @a\n    x: 1\nEND SOURCE\n\nITEM @a\n    y: 2\nEND ITEM'
        out, removed = dedupe_item_blocks(text)

        assert removed == 0
        assert "SOURCE @a" in out

    def test_no_duplicates_returns_unchanged(self):
        text = 'ITEM @a\n    criterio: A\nEND ITEM'
        out, removed = dedupe_item_blocks(text)

        assert removed == 0
        assert out == text

    def test_degenerate_loop_real_shape(self):
        """Forma do caso real: 22 únicos, 121 totais."""
        blocks = [f'ITEM @a\n    criterio: C{i}\nEND ITEM' for i in range(22)]
        text = "\n\n".join(blocks + blocks[:22] * 4 + blocks[:11])
        out, removed = dedupe_item_blocks(text)

        assert count_item_blocks(out) == 22
        assert removed == 121 - 22


class TestCountItemBlocks:
    def test_counts_complete_blocks(self):
        assert count_item_blocks("ITEM @a\nx: 1\nEND ITEM\n\nITEM @b\ny: 2\nEND ITEM") == 2

    def test_source_only_is_zero(self):
        """O caso que era reportado OK: nenhuma anotação gerada."""
        assert count_item_blocks("SOURCE @a\n    x: 1\nEND SOURCE") == 0

    def test_empty_text(self):
        assert count_item_blocks("") == 0
