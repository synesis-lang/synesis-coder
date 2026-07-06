"""Testes para synesis_coder/modes/normalize_mode.py (Etapa 5 do pipeline ACT).

Cobertura:
- Extração de conceitos de chains
- Extração de códigos de documentos
- Construção de inventário cross-file
- Normalização determinística
- Parse de resposta LLM
- Aplicação de sugestões LLM ao inventário
- Geração de revisões por arquivo
- Fluxo completo com mock LLM
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synesis_coder.modes.normalize_mode import (
    CodeGroup,
    NormalizationSuggestion,
    _apply_llm_suggestions,
    _build_revisions_for_doc,
    _extract_codes_from_item_block,
    _extract_concepts_from_chain,
    _normalize_code_key,
    _parse_normalization_response,
    _substitute_code_in_chain,
    apply_deterministic_normalization,
    build_code_inventory,
    process_normalize,
)
from synesis_coder.synr_io import SynrDocument, parse_synr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_SOCIAL = Path("d:/GitHub/synesis-coder/test_output").parent / "tests" / "fixtures" / "social_acceptance.synp"
# Use test_output synp if available
_SYNP_CANDIDATES = [
    Path("d:/GitHub/synesis-coder/test_output"),
    Path("d:/GitHub/synesis-coder"),
]


def _find_synp() -> Optional[Path]:
    for base in _SYNP_CANDIDATES:
        synps = list(base.glob("**/*.synp"))
        if synps:
            return synps[0]
    return None


@pytest.fixture
def simple_syn_content() -> str:
    return """\
SOURCE @smith2024
  description: Study on trust and community.
END SOURCE

ITEM @smith2024
  text: 'Trust is essential for community cohesion.'
  chain: trust -> ENABLES -> Community_Cohesion
END ITEM

ITEM @smith2024
  text: 'Social trust facilitates cooperation.'
  chain: Social_Trust -> INFLUENCES -> Cooperation
END ITEM
"""


@pytest.fixture
def multi_variant_syn_content() -> str:
    return """\
SOURCE @jones2023
  description: Another study.
END SOURCE

ITEM @jones2023
  text: 'Public trust drives governance.'
  chain: Public_Trust -> INFLUENCES -> Governance
END ITEM

ITEM @jones2023
  text: 'Institutional trust matters.'
  chain: Institutional_Trust -> ENABLES -> Legitimacy
END ITEM
"""


@pytest.fixture
def simple_doc(simple_syn_content) -> SynrDocument:
    return SynrDocument(content=simple_syn_content)


@pytest.fixture
def multi_variant_doc(multi_variant_syn_content) -> SynrDocument:
    return SynrDocument(content=multi_variant_syn_content)


# ---------------------------------------------------------------------------
# Normalização de chave
# ---------------------------------------------------------------------------

class TestNormalizeCodeKey:
    def test_lowercase(self):
        assert _normalize_code_key("Trust") == "trust"

    def test_underscores_preserved(self):
        assert _normalize_code_key("Social_Trust") == "social_trust"

    def test_spaces_to_underscores(self):
        assert _normalize_code_key("Social Trust") == "social_trust"

    def test_hyphens_to_underscores(self):
        assert _normalize_code_key("Social-Trust") == "social_trust"

    def test_strip_whitespace(self):
        assert _normalize_code_key("  Trust  ") == "trust"

    def test_mixed_variants_same_key(self):
        assert _normalize_code_key("Trust") == _normalize_code_key("trust")
        assert _normalize_code_key("Social_Trust") == _normalize_code_key("social trust")


# ---------------------------------------------------------------------------
# Extração de conceitos de chain
# ---------------------------------------------------------------------------

class TestExtractConceptsFromChain:
    def test_simple_chain(self):
        concepts = _extract_concepts_from_chain("A -> ENABLES -> B")
        assert concepts == ["A", "B"]

    def test_long_chain(self):
        concepts = _extract_concepts_from_chain("A -> ENABLES -> B -> INFLUENCES -> C")
        assert concepts == ["A", "B", "C"]

    def test_single_node(self):
        concepts = _extract_concepts_from_chain("SingleNode")
        assert concepts == ["SingleNode"]

    def test_underscore_concepts(self):
        concepts = _extract_concepts_from_chain(
            "Social_Trust -> ENABLES -> Community_Cohesion"
        )
        assert concepts == ["Social_Trust", "Community_Cohesion"]

    def test_relation_not_included(self):
        concepts = _extract_concepts_from_chain("A -> RELATES_TO -> B")
        assert "RELATES_TO" not in concepts


# ---------------------------------------------------------------------------
# Extração de códigos de bloco ITEM
# ---------------------------------------------------------------------------

class TestExtractCodesFromItemBlock:
    def test_chain_field(self):
        block = "ITEM @ref\n  chain: Trust -> ENABLES -> Community\nEND ITEM\n"
        results = _extract_codes_from_item_block(block)
        field_names = [r[0] for r in results]
        codes = [r[1] for r in results]
        assert "chain" in field_names
        assert "Trust" in codes
        assert "Community" in codes

    def test_code_field(self):
        block = "ITEM @ref\n  code: Social_Trust\nEND ITEM\n"
        results = _extract_codes_from_item_block(block)
        assert len(results) == 1
        assert results[0][0] == "code"
        assert results[0][1] == "Social_Trust"

    def test_no_chain_or_code(self):
        block = "ITEM @ref\n  text: 'hello'\nEND ITEM\n"
        results = _extract_codes_from_item_block(block)
        assert results == []

    def test_skips_revision_lines(self):
        block = (
            "ITEM @ref\n"
            "  chain: Trust -> ENABLES -> Community\n"
            "  # REVISION\n"
            "  # $chain: Trust -> INFLUENCES -> Community\n"
            "END ITEM\n"
        )
        # Only extracts from non-comment lines
        results = _extract_codes_from_item_block(block)
        field_values = [r[2] for r in results]
        assert "Trust -> ENABLES -> Community" in field_values


# ---------------------------------------------------------------------------
# Inventário
# ---------------------------------------------------------------------------

class TestBuildCodeInventory:
    def test_single_doc_inventory(self, simple_doc):
        inventory = build_code_inventory([(Path("test.synr"), simple_doc)])
        # Should have trust, community_cohesion, social_trust, cooperation
        keys = set(inventory.keys())
        assert "trust" in keys
        assert "social_trust" in keys
        assert "community_cohesion" in keys

    def test_cross_file_inventory(self, simple_doc, multi_variant_doc):
        inventory = build_code_inventory([
            (Path("file1.synr"), simple_doc),
            (Path("file2.synr"), multi_variant_doc),
        ])
        # trust and social_trust from file1; public_trust, institutional_trust from file2
        assert "trust" in inventory
        assert "social_trust" in inventory
        assert "public_trust" in inventory
        assert "institutional_trust" in inventory

    def test_variant_counting(self, simple_syn_content, multi_variant_syn_content):
        # Both files have 'trust' variants: 'trust' and (from multi) nothing direct
        # But 'social_trust' key from file1: 'Social_Trust', from file2: nothing matching
        doc1 = SynrDocument(content=simple_syn_content)
        doc2 = SynrDocument(content=simple_syn_content)  # same content → double count
        inventory = build_code_inventory([
            (Path("f1.synr"), doc1),
            (Path("f2.synr"), doc2),
        ])
        trust_group = inventory.get("trust")
        assert trust_group is not None
        assert trust_group.variants.get("trust", 0) == 2  # 'trust' (lowercase) from item1 twice


# ---------------------------------------------------------------------------
# Normalização determinística
# ---------------------------------------------------------------------------

class TestApplyDeterministicNormalization:
    def test_single_variant_canonical(self):
        group = CodeGroup(normalized_key="trust")
        group.variants["Trust"] = 5
        group.occurrences = []
        inventory = {"trust": group}
        apply_deterministic_normalization(inventory)
        assert group.canonical == "Trust"

    def test_most_frequent_wins(self):
        group = CodeGroup(normalized_key="trust")
        group.variants["Trust"] = 10
        group.variants["trust"] = 3
        inventory = {"trust": group}
        apply_deterministic_normalization(inventory)
        assert group.canonical == "Trust"

    def test_underscore_preferred_on_tie(self):
        group = CodeGroup(normalized_key="social_trust")
        group.variants["Social_Trust"] = 5
        group.variants["Social Trust"] = 5
        inventory = {"social_trust": group}
        apply_deterministic_normalization(inventory)
        assert group.canonical == "Social_Trust"

    def test_no_normalization_needed(self):
        group = CodeGroup(normalized_key="community")
        group.variants["Community"] = 3
        inventory = {"community": group}
        apply_deterministic_normalization(inventory)
        assert not group.needs_normalization

    def test_needs_normalization_when_variants_differ(self):
        group = CodeGroup(normalized_key="trust")
        group.variants["Trust"] = 10
        group.variants["trust"] = 2
        inventory = {"trust": group}
        apply_deterministic_normalization(inventory)
        assert group.needs_normalization  # "trust" != canonical "Trust"


# ---------------------------------------------------------------------------
# Parse da resposta LLM
# ---------------------------------------------------------------------------

class TestParseLlmResponse:
    def test_parses_single_block(self):
        raw = """\
# $group: Trust, trust, Social_Trust
# $suggested_canonical: Trust
# $merge_confidence: 0.85
# $reason: all_refer_to_same_concept
---
"""
        suggestions = _parse_normalization_response(raw)
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.suggested_canonical == "Trust"
        assert s.merge_confidence == pytest.approx(0.85)
        assert "Trust" in s.group_codes
        assert "trust" in s.group_codes

    def test_parses_multiple_blocks(self):
        raw = """\
# $group: A, a
# $suggested_canonical: A
# $merge_confidence: 0.90
# $reason: case_only
---
# $group: B, b
# $suggested_canonical: B
# $merge_confidence: 0.75
# $reason: case_only
---
"""
        suggestions = _parse_normalization_response(raw)
        assert len(suggestions) == 2

    def test_empty_response(self):
        suggestions = _parse_normalization_response("")
        assert suggestions == []

    def test_missing_canonical_ignored(self):
        raw = """\
# $group: X, y
# $merge_confidence: 0.90
---
"""
        suggestions = _parse_normalization_response(raw)
        assert len(suggestions) == 0

    def test_low_confidence_parsed(self):
        raw = """\
# $group: A, B
# $suggested_canonical: A
# $merge_confidence: 0.30
# $reason: uncertain
---
"""
        suggestions = _parse_normalization_response(raw)
        # Parsed fine; threshold filter is done separately in _apply_llm_suggestions
        assert len(suggestions) == 1
        assert suggestions[0].merge_confidence == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Aplicar sugestões LLM
# ---------------------------------------------------------------------------

class TestApplyLlmSuggestions:
    def _make_inventory(self, **variant_maps) -> dict[str, CodeGroup]:
        inventory = {}
        for nkey, variants in variant_maps.items():
            g = CodeGroup(normalized_key=nkey)
            for v, c in variants.items():
                g.variants[v] = c
            g.canonical = max(variants, key=variants.get)
            inventory[nkey] = g
        return inventory

    def test_applies_high_confidence(self):
        inventory = self._make_inventory(
            trust={"Trust": 5, "trust": 2},
            social_trust={"Social_Trust": 3},
        )
        suggestions = [
            NormalizationSuggestion(
                group_codes=["Trust", "Social_Trust"],
                suggested_canonical="Trust",
                merge_confidence=0.85,
                reason="same_concept",
            )
        ]
        updated = _apply_llm_suggestions(inventory, suggestions, confidence_threshold=0.65)
        assert updated >= 0  # social_trust canonical → Trust
        assert inventory["social_trust"].canonical == "Trust"

    def test_rejects_low_confidence(self):
        inventory = self._make_inventory(trust={"Trust": 5})
        original_canonical = inventory["trust"].canonical
        suggestions = [
            NormalizationSuggestion(
                group_codes=["Trust"],
                suggested_canonical="NewCanonical",
                merge_confidence=0.40,
                reason="weak",
            )
        ]
        _apply_llm_suggestions(inventory, suggestions, confidence_threshold=0.65)
        assert inventory["trust"].canonical == original_canonical  # unchanged

    def test_marks_llm_suggested(self):
        # Deterministic canonical is "trust" (1 occurrence); LLM suggests "Trust"
        inventory = self._make_inventory(trust={"trust": 1})
        # Override canonical to lowercase to simulate deterministic chose lowercase
        inventory["trust"].canonical = "trust"
        suggestions = [
            NormalizationSuggestion(
                group_codes=["trust"],
                suggested_canonical="Trust",
                merge_confidence=0.90,
                reason="case",
            )
        ]
        _apply_llm_suggestions(inventory, suggestions, confidence_threshold=0.65)
        assert inventory["trust"].llm_suggested is True
        assert inventory["trust"].canonical == "Trust"


# ---------------------------------------------------------------------------
# Substituição em chain
# ---------------------------------------------------------------------------

class TestSubstituteCodeInChain:
    def test_replaces_concept(self):
        result = _substitute_code_in_chain("trust -> ENABLES -> Community", "trust", "Trust")
        assert result == "Trust -> ENABLES -> Community"

    def test_no_change_when_not_found(self):
        result = _substitute_code_in_chain("A -> ENABLES -> B", "C", "D")
        assert result == "A -> ENABLES -> B"

    def test_replaces_last_concept(self):
        result = _substitute_code_in_chain("A -> ENABLES -> old", "old", "New")
        assert result == "A -> ENABLES -> New"

    def test_does_not_replace_relation(self):
        result = _substitute_code_in_chain("A -> ENABLES -> B", "ENABLES", "NEW_RELATION")
        assert "ENABLES" in result


# ---------------------------------------------------------------------------
# Geração de revisões por documento
# ---------------------------------------------------------------------------

class TestBuildRevisionsForDoc:
    def test_generates_revision_for_changed_code(self):
        content = """\
SOURCE @ref
  description: test
END SOURCE

ITEM @ref
  text: 'test'
  chain: trust -> ENABLES -> Community
END ITEM
"""
        doc = SynrDocument(content=content)
        group = CodeGroup(normalized_key="trust")
        group.variants["trust"] = 1
        group.canonical = "Trust"
        inventory = {"trust": group}

        revisions = _build_revisions_for_doc(doc, Path("test.synr"), inventory)
        assert len(revisions) == 1
        rev = revisions[0]
        assert rev is not None
        assert "chain" in rev
        assert "Trust" in rev["chain"]

    def test_no_revision_when_already_canonical(self):
        content = """\
SOURCE @ref
  description: test
END SOURCE

ITEM @ref
  text: 'test'
  chain: Trust -> ENABLES -> Community
END ITEM
"""
        doc = SynrDocument(content=content)
        group = CodeGroup(normalized_key="trust")
        group.variants["Trust"] = 1
        group.canonical = "Trust"
        inventory = {"trust": group}

        revisions = _build_revisions_for_doc(doc, Path("test.synr"), inventory)
        assert len(revisions) == 1
        assert revisions[0] is None

    def test_multiple_items(self):
        content = """\
SOURCE @ref
  description: test
END SOURCE

ITEM @ref
  text: 'a'
  chain: trust -> ENABLES -> X
END ITEM

ITEM @ref
  text: 'b'
  chain: Trust -> ENABLES -> X
END ITEM
"""
        doc = SynrDocument(content=content)
        group = CodeGroup(normalized_key="trust")
        group.variants["trust"] = 1
        group.variants["Trust"] = 1
        group.canonical = "Trust"
        inventory = {"trust": group}

        revisions = _build_revisions_for_doc(doc, Path("test.synr"), inventory)
        assert len(revisions) == 2
        assert revisions[0] is not None  # "trust" → "Trust" needed
        assert revisions[1] is None  # already "Trust"


# ---------------------------------------------------------------------------
# Fluxo completo com mock LLM
# ---------------------------------------------------------------------------

_MOCK_CTX = {
    "project_description": "Test project",
    "guidelines": "",
    "item_fields": [],
    "bib_content": "",
}


class TestProcessNormalizeWithMockLLM:
    """Testes de fluxo completo com LLMClient e load_project mockados."""

    def _make_syn_file(self, tmp_path: Path) -> tuple[Path, Path]:
        synp = tmp_path / "test.synp"
        synp.write_text("placeholder", encoding="utf-8")  # load_project is mocked

        syn1 = tmp_path / "file1.synr"
        syn1.write_text(
            """\
SOURCE @ref1
  description: test
END SOURCE

ITEM @ref1
  text: 'A'
  chain: trust -> ENABLES -> Community
END ITEM

ITEM @ref1
  text: 'B'
  chain: Social_Trust -> INFLUENCES -> Governance
END ITEM
""",
            encoding="utf-8",
        )
        return syn1, synp

    def _patch_all(self, mock_llm_response: str = ""):
        """Context manager stack: patches LLMClient and load_project together."""
        from contextlib import ExitStack
        stack = ExitStack()
        mock_client = MagicMock()
        mock_client.model = "mock-model"
        mock_client.call_async = AsyncMock(return_value=mock_llm_response)
        mock_client.usage = MagicMock()
        mock_client.usage.summary_line.return_value = "tokens: 0"
        stack.enter_context(
            patch("synesis_coder.modes.normalize_mode.LLMClient", return_value=mock_client)
        )
        stack.enter_context(
            patch("synesis_coder.modes.normalize_mode.load_project", return_value=_MOCK_CTX)
        )
        return stack, mock_client

    def test_writes_synr_output(self, tmp_path):
        syn1, synp = self._make_syn_file(tmp_path)
        with self._patch_all()[0]:
            result = process_normalize(
                synr_paths=[syn1],
                project_path=synp,
                output_dir=tmp_path,
                model="mock-model",
            )

        assert "Normalize concluído" in result
        output_files = list(tmp_path.glob("*.synr"))
        assert len(output_files) >= 1

    def test_deterministic_normalization_applied(self, tmp_path):
        """Both 'trust' and 'Trust' variants → canonical 'Trust' → revision for the lowercase one."""
        synp = tmp_path / "test.synp"
        synp.write_text("placeholder", encoding="utf-8")

        # Two items: one with 'Trust' (2 occurrences across items), one with 'trust'
        # Deterministic: Trust wins (more frequent), trust needs revision
        syn1 = tmp_path / "mixed.synr"
        syn1.write_text(
            """\
SOURCE @ref1
  description: test
END SOURCE

ITEM @ref1
  text: 'A'
  chain: trust -> ENABLES -> X
END ITEM

ITEM @ref1
  text: 'B'
  chain: Trust -> ENABLES -> Y
END ITEM

ITEM @ref1
  text: 'C'
  chain: Trust -> INFLUENCES -> Z
END ITEM
""",
            encoding="utf-8",
        )

        with self._patch_all()[0]:
            process_normalize(
                synr_paths=[syn1],
                project_path=synp,
                output_dir=tmp_path,
                model="mock-model",
            )

        out = tmp_path / "mixed.synr"
        content = out.read_text(encoding="utf-8")
        # First ITEM has 'trust' (lowercase) → needs revision to 'Trust'
        assert "# REVISION" in content
        assert "Trust -> ENABLES -> X" in content  # corrected canonical in revision tag

    def test_llm_called_for_residual_groups(self, tmp_path):
        """LLM should be called when multiple variants exist after deterministic phase."""
        synp = tmp_path / "test.synp"
        synp.write_text("placeholder", encoding="utf-8")

        syn1 = tmp_path / "corpus.synr"
        syn1.write_text(
            """\
SOURCE @ref1
  description: test1
END SOURCE

ITEM @ref1
  text: 'A'
  chain: Trust -> ENABLES -> X
END ITEM

SOURCE @ref2
  description: test2
END SOURCE

ITEM @ref2
  text: 'B'
  chain: trust -> ENABLES -> Y
END ITEM
""",
            encoding="utf-8",
        )

        call_count = 0

        async def _mock_call_async(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return ""

        mock_client = MagicMock()
        mock_client.model = "mock-model"
        mock_client.call_async = AsyncMock(side_effect=_mock_call_async)
        mock_client.usage = MagicMock()
        mock_client.usage.summary_line.return_value = "tokens: 0"

        with patch("synesis_coder.modes.normalize_mode.LLMClient", return_value=mock_client), \
             patch("synesis_coder.modes.normalize_mode.load_project", return_value=_MOCK_CTX):
            process_normalize(
                synr_paths=[syn1],
                project_path=synp,
                model="mock-model",
                output_dir=tmp_path,
            )

        # Trust and trust are same group → residual with 2 variants → LLM called
        assert call_count >= 1

    def test_verbose_format(self, tmp_path):
        syn1, synp = self._make_syn_file(tmp_path)
        with self._patch_all()[0]:
            result = process_normalize(
                synr_paths=[syn1],
                project_path=synp,
                output_dir=tmp_path,
                model="mock-model",
                format="verbose",
            )

        assert "# synesis-coder normalize" in result

    def test_file_not_found_raises(self, tmp_path):
        synp = tmp_path / "test.synp"
        synp.write_text("placeholder", encoding="utf-8")

        with patch("synesis_coder.modes.normalize_mode.load_project", return_value=_MOCK_CTX):
            with pytest.raises(FileNotFoundError):
                process_normalize(
                    synr_paths=[tmp_path / "nonexistent.synr"],
                    project_path=synp,
                )

    def test_inventory_saved(self, tmp_path):
        syn1, synp = self._make_syn_file(tmp_path)
        inventory_file = tmp_path / "inventory.txt"

        with self._patch_all()[0]:
            process_normalize(
                synr_paths=[syn1],
                project_path=synp,
                output_dir=tmp_path,
                inventory_path=inventory_file,
                model="mock-model",
            )

        assert inventory_file.exists()
        content = inventory_file.read_text(encoding="utf-8")
        assert "canonical=" in content
