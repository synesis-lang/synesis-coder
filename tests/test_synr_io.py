"""Testes unitários para synr_io — reader/writer do formato .synr.

Todos os testes são unitários: sem LLM, sem I/O de rede.
Testes que usam synesis.load() requerem o compilador instalado mas não API key.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from synesis_coder.synr_io import (
    SynrDocument,
    create_synr,
    extract_revision_tags,
    parse_synr,
    serialize_revision_block,
    write_synr,
)

# ---------------------------------------------------------------------------
# Fixtures e helpers
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"

# Bloco ITEM mínimo sem revisão
_ITEM_NO_REVISION = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy projects.
        note: Trust operates as a prerequisite for acceptance
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM
""")

# Bloco ITEM com bloco REVISION completo
_ITEM_WITH_REVISION = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy projects.
        note: Trust operates as a prerequisite for acceptance
        chain: Trust -> ENABLES -> Social_Acceptance

        # REVISION
        # $suspicion_score: 0.84
        # $reason: wrong_direction
        # $chain: Trust -> INFLUENCES -> Social_Acceptance
    END ITEM
""")

# Bloco ITEM com REVISION mas score baixo (sem sugestões de campo)
_ITEM_NO_SUGGESTION = textwrap.dedent("""\
    ITEM @jones2021
        text: Environmental concern co-enables participation.
        note: Dual mechanism alongside Trust
        chain: Environmental_Concern -> ENABLES -> Participation

        # REVISION
        # $suspicion_score: 0.18
        # $reason: none
    END ITEM
""")

# Conteúdo .syn mínimo com dois ITEMs
_SYN_TWO_ITEMS = textwrap.dedent("""\
    SOURCE @smith2024
        description: Study on trust and wind energy acceptance.
        epistemic_model: Technology Acceptance Model
        method: survey
    END SOURCE

    ITEM @smith2024
        text: Community trust enables social acceptance.
        note: Trust is prerequisite
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM

    ITEM @smith2024
        text: Environmental concern co-enables participation.
        note: Dual mechanism
        chain: Environmental_Concern -> ENABLES -> Participation
    END ITEM
""")


# ---------------------------------------------------------------------------
# extract_revision_tags
# ---------------------------------------------------------------------------


class TestExtractRevisionTags:
    def test_extracts_all_tags(self):
        tags = extract_revision_tags(_ITEM_WITH_REVISION)
        assert tags["suspicion_score"] == "0.84"
        assert tags["reason"] == "wrong_direction"
        assert tags["chain"] == "Trust -> INFLUENCES -> Social_Acceptance"

    def test_returns_empty_when_no_revision(self):
        tags = extract_revision_tags(_ITEM_NO_REVISION)
        assert tags == {}

    def test_extracts_minimal_tags(self):
        tags = extract_revision_tags(_ITEM_NO_SUGGESTION)
        assert tags["suspicion_score"] == "0.18"
        assert tags["reason"] == "none"
        assert "chain" not in tags

    def test_ignores_malformed_tags(self):
        """Linhas com # mas sem $key: formato são ignoradas."""
        block = textwrap.dedent("""\
            ITEM @ref
                text: something

                # REVISION
                # comentário comum sem $
                # $valid_key: valid value
                # também isso: não é tag
            END ITEM
        """)
        tags = extract_revision_tags(block)
        assert list(tags.keys()) == ["valid_key"]
        assert tags["valid_key"] == "valid value"

    def test_stops_at_non_comment_line(self):
        """Linha não-comentário após REVISION encerra a coleta de tags."""
        block = textwrap.dedent("""\
            ITEM @ref
                # REVISION
                # $key1: val1
                text: this is a field, not a comment
                # $key2: val2
            END ITEM
        """)
        tags = extract_revision_tags(block)
        assert "key1" in tags
        assert "key2" not in tags

    def test_tag_value_preserves_spaces_and_arrows(self):
        """Valores com espaços e setas -> são preservados integralmente."""
        block = textwrap.dedent("""\
            ITEM @ref
                # REVISION
                # $chain: A -> ENABLES -> B
            END ITEM
        """)
        tags = extract_revision_tags(block)
        assert tags["chain"] == "A -> ENABLES -> B"

    def test_dotted_key_namespace(self):
        """Chaves com ponto (métricas) são aceitas: $metrics.acs"""
        block = textwrap.dedent("""\
            ITEM @ref
                # REVISION
                # $metrics.acs: 0.95
            END ITEM
        """)
        tags = extract_revision_tags(block)
        assert tags["metrics.acs"] == "0.95"


# ---------------------------------------------------------------------------
# serialize_revision_block
# ---------------------------------------------------------------------------


class TestSerializeRevisionBlock:
    def test_empty_tags_returns_empty(self):
        assert serialize_revision_block({}) == ""

    def test_basic_serialization(self):
        tags = {"suspicion_score": "0.84", "reason": "wrong_direction"}
        result = serialize_revision_block(tags)
        assert "    # REVISION" in result
        assert "    # $suspicion_score: 0.84" in result
        assert "    # $reason: wrong_direction" in result

    def test_custom_indent(self):
        tags = {"key": "value"}
        result = serialize_revision_block(tags, indent="\t")
        assert result.startswith("\t# REVISION")

    def test_ends_with_newline(self):
        tags = {"key": "value"}
        result = serialize_revision_block(tags)
        assert result.endswith("\n")

    def test_round_trip_tags(self):
        """Bloco serializado é parseável por extract_revision_tags."""
        original_tags = {"suspicion_score": "0.72", "reason": "off_topic", "note": "rephrase"}
        block = f"ITEM @ref\n{serialize_revision_block(original_tags)}END ITEM\n"
        parsed = extract_revision_tags(block)
        assert parsed == original_tags


# ---------------------------------------------------------------------------
# create_synr
# ---------------------------------------------------------------------------


class TestCreateSynr:
    def test_header_prepended(self):
        header = {"phase": "critique", "model": "claude-sonnet-4-6", "timestamp": "2026-04-24T00:00:00Z"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        assert doc.content.startswith("# $phase: critique")
        assert "# $model: claude-sonnet-4-6" in doc.content
        assert "# $timestamp: 2026-04-24T00:00:00Z" in doc.content

    def test_header_canonical_order(self):
        """phase, model, timestamp escritos nessa ordem."""
        header = {"timestamp": "T", "model": "M", "phase": "P"}
        doc = create_synr("", header, [])
        lines = [l for l in doc.content.splitlines() if l.startswith("# $")]
        assert lines[0] == "# $phase: P"
        assert lines[1] == "# $model: M"
        assert lines[2] == "# $timestamp: T"

    def test_no_revisions_content_preserved(self):
        """Sem revisões, o corpo do .syn é preservado integralmente."""
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        # body deve conter todo o conteúdo original
        assert "SOURCE @smith2024" in doc.content
        assert "END SOURCE" in doc.content
        assert "ITEM @smith2024" in doc.content
        assert "END ITEM" in doc.content

    def test_revision_injected_before_end_item(self):
        """Revisão é injetada antes de END ITEM."""
        revisions = [
            {"suspicion_score": "0.9", "reason": "wrong_direction"},
            None,
        ]
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        # Bloco 1 deve ter REVISION
        assert "# REVISION" in doc.content
        assert "# $suspicion_score: 0.9" in doc.content
        # END ITEM deve vir depois de REVISION
        idx_rev = doc.content.index("# REVISION")
        idx_end = doc.content.index("END ITEM")
        assert idx_rev < idx_end

    def test_none_revision_not_injected(self):
        """None na lista não injeta nada no ITEM correspondente."""
        revisions = [None, None]
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        assert "# REVISION" not in doc.content

    def test_empty_revision_dict_not_injected(self):
        """Dict vazio não injeta bloco REVISION."""
        revisions = [{}, None]
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        assert "# REVISION" not in doc.content

    def test_item_revisions_populated(self):
        """item_revisions do documento tem uma entrada por ITEM."""
        revisions = [
            {"suspicion_score": "0.9", "reason": "r"},
            {"suspicion_score": "0.1", "reason": "none"},
        ]
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        assert len(doc.item_revisions) == 2
        assert doc.item_revisions[0][1]["suspicion_score"] == "0.9"
        assert doc.item_revisions[1][1]["suspicion_score"] == "0.1"

    def test_fewer_revisions_than_items(self):
        """Lista mais curta que ITEMs não injeta nos ITEMs restantes."""
        revisions = [{"suspicion_score": "0.5", "reason": "r"}]
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, revisions)
        assert len(doc.item_revisions) == 2
        # Segundo ITEM não deve ter tags
        assert doc.item_revisions[1][1] == {}

    def test_header_in_document(self):
        header = {"phase": "critique", "model": "test-model", "timestamp": "2026-01-01T00:00:00Z"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        assert doc.header["phase"] == "critique"
        assert doc.header["model"] == "test-model"


# ---------------------------------------------------------------------------
# parse_synr
# ---------------------------------------------------------------------------


class TestParseSynr:
    def test_parse_header(self, tmp_path):
        synr_content = textwrap.dedent("""\
            # $phase: critique
            # $model: claude-sonnet-4-6
            # $timestamp: 2026-04-24T00:00:00Z

            SOURCE @ref
                description: test
            END SOURCE
        """)
        synr_file = tmp_path / "test.synr"
        synr_file.write_text(synr_content, encoding="utf-8")

        doc = parse_synr(synr_file)
        assert doc.header["phase"] == "critique"
        assert doc.header["model"] == "claude-sonnet-4-6"
        assert doc.header["timestamp"] == "2026-04-24T00:00:00Z"

    def test_parse_item_revisions_in_order(self, tmp_path):
        synr_content = textwrap.dedent("""\
            # $phase: critique

        """) + _ITEM_WITH_REVISION + "\n" + _ITEM_NO_SUGGESTION
        synr_file = tmp_path / "test.synr"
        synr_file.write_text(synr_content, encoding="utf-8")

        doc = parse_synr(synr_file)
        assert len(doc.item_revisions) == 2
        bibref1, tags1 = doc.item_revisions[0]
        bibref2, tags2 = doc.item_revisions[1]
        assert bibref1 == "smith2024"
        assert tags1["suspicion_score"] == "0.84"
        assert bibref2 == "jones2021"
        assert tags2["reason"] == "none"

    def test_parse_item_no_revision(self, tmp_path):
        synr_file = tmp_path / "test.syn"
        synr_file.write_text(_ITEM_NO_REVISION, encoding="utf-8")

        doc = parse_synr(synr_file)
        assert len(doc.item_revisions) == 1
        assert doc.item_revisions[0][1] == {}

    def test_content_preserved(self, tmp_path):
        synr_file = tmp_path / "test.synr"
        synr_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        doc = parse_synr(synr_file)
        assert doc.content == _SYN_TWO_ITEMS

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            parse_synr(Path("d:/nao_existe/arquivo.synr"))


# ---------------------------------------------------------------------------
# write_synr
# ---------------------------------------------------------------------------


class TestWriteSynr:
    def test_write_and_read_back(self, tmp_path):
        header = {"phase": "critique", "model": "m", "timestamp": "t"}
        doc = create_synr(_SYN_TWO_ITEMS, header, [])
        out = tmp_path / "output.synr"
        write_synr(out, doc)

        doc2 = parse_synr(out)
        assert doc2.content == doc.content
        assert doc2.header == doc.header

    def test_creates_file(self, tmp_path):
        doc = SynrDocument(content="# test\n")
        out = tmp_path / "new.synr"
        write_synr(out, doc)
        assert out.exists()
        assert out.read_text(encoding="utf-8") == "# test\n"


# ---------------------------------------------------------------------------
# Round-trip com synesis.load()
# ---------------------------------------------------------------------------


class TestSynrCompilerCompatibility:
    """Verifica que .synr gerado é aceito pelo compilador Synesis."""

    def test_round_trip_no_revision_compiles(self, tmp_path):
        """Criar .synr sem revisões a partir de conteúdo .syn mínimo e compilar."""
        try:
            import synesis
            from synesis_coder.project_loader import load_project
        except ImportError:
            pytest.skip("synesis ou project_loader não disponível")

        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        # Carregar conteúdo de anotação real do projeto
        social_dir = PROJECT_SOCIAL.parent
        syn_files = list(social_dir.glob("*.syn"))
        if not syn_files:
            pytest.skip("Nenhum .syn encontrado no projeto social_acceptance")

        syn_content = syn_files[0].read_text(encoding="utf-8")
        ctx = load_project(PROJECT_SOCIAL)

        header = {"phase": "critique", "model": "claude-sonnet-4-6", "timestamp": "2026-04-24T00:00:00Z"}
        doc = create_synr(syn_content, header, [])

        # Compilar o .synr via synesis.load()
        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"test.synr": doc.content},
            bibliography_content=ctx.get("bib_content"),
        )

        from synesis_coder.validator import _has_structural_errors
        assert not _has_structural_errors(result), (
            f"Arquivo .synr não compilou:\n{result.get_diagnostics()}"
        )

    def test_synr_with_revisions_compiles(self, tmp_path):
        """Criar .synr com blocos REVISION e verificar que compila."""
        try:
            import synesis
            from synesis_coder.project_loader import load_project
        except ImportError:
            pytest.skip("synesis ou project_loader não disponível")

        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        social_dir = PROJECT_SOCIAL.parent
        syn_files = list(social_dir.glob("*.syn"))
        if not syn_files:
            pytest.skip("Nenhum .syn encontrado no projeto social_acceptance")

        syn_content = syn_files[0].read_text(encoding="utf-8")
        ctx = load_project(PROJECT_SOCIAL)

        # Contar ITEMs no conteúdo e injetar revisão no primeiro
        item_count = syn_content.count("\nITEM @") + (1 if syn_content.startswith("ITEM @") else 0)
        revisions = [{"suspicion_score": "0.5", "reason": "test"}] + [None] * (item_count - 1)

        header = {"phase": "critique", "model": "test-model", "timestamp": "2026-04-24T00:00:00Z"}
        doc = create_synr(syn_content, header, revisions)

        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"test.synr": doc.content},
            bibliography_content=ctx.get("bib_content"),
        )

        from synesis_coder.validator import _has_structural_errors
        assert not _has_structural_errors(result), (
            f"Arquivo .synr com REVISION não compilou:\n{result.get_diagnostics()}"
        )
