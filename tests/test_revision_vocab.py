"""Testes do vocabulário de revisão e da taxonomia — Fase 5.

Cobrem o requisito não-negociável da fase: `.synr` do formato 1 (vocabulário
antigo) continuam legíveis por refine/incorporate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from synesis_coder.critique_taxonomy import (
    REASON_CATEGORIES,
    VALID_REASONS,
    applicable_categories,
    build_taxonomy_section,
    validate_reason,
)
from synesis_coder.revision_vocab import (
    META_TAGS,
    SYNR_FORMAT_VERSION,
    canonical_tag,
    is_meta_tag,
    resolve_sensitivity,
)


def _field(type_name: str):
    return SimpleNamespace(type=SimpleNamespace(name=type_name))


class TestCanonicalTag:
    @pytest.mark.parametrize(
        "old,new",
        [
            ("suspicion_score", "divergence"),
            ("reason_detail", "comment"),
            ("items_flagged", "items_to_review"),
            ("threshold", "sensitivity"),
        ],
    )
    def test_translates_format_1_names(self, old, new):
        assert canonical_tag(old) == new

    def test_canonical_names_pass_through(self):
        for name in ("divergence", "comment", "reason", "unknown_field"):
            assert canonical_tag(name) == name

    def test_idempotent(self):
        assert canonical_tag(canonical_tag("suspicion_score")) == "divergence"


class TestIsMetaTag:
    @pytest.mark.parametrize(
        "tag",
        ["divergence", "reason", "comment", "note", "phase", "format",
         "model", "timestamp", "threshold", "sensitivity",
         "suspicion_score", "reason_detail"],
    )
    def test_meta_tags_recognized(self, tag):
        assert is_meta_tag(tag)

    def test_metrics_namespace_is_meta(self):
        assert is_meta_tag("metrics.agreement")
        assert is_meta_tag("metrics.items_total")

    def test_numbered_variant_is_meta(self):
        assert is_meta_tag("divergence.1")

    def test_field_names_are_not_meta(self):
        for tag in ("chain", "text", "zone", "confidence"):
            assert not is_meta_tag(tag)

    def test_format_1_names_kept_for_backcompat(self):
        """Sem isto, um .synr antigo aplicaria suspicion_score como campo."""
        assert "suspicion_score" in META_TAGS
        assert "reason_detail" in META_TAGS


class TestResolveSensitivity:
    def test_named_levels(self):
        assert resolve_sensitivity("strict") == (0.10, "strict")
        assert resolve_sensitivity("standard") == (0.20, "standard")
        assert resolve_sensitivity("lenient") == (0.35, "lenient")

    def test_case_insensitive(self):
        assert resolve_sensitivity("STRICT")[0] == 0.10

    def test_default_when_none(self):
        threshold, label = resolve_sensitivity(None)
        assert threshold == 0.20 and label == "standard"

    def test_numeric_string_maps_back_to_label(self):
        assert resolve_sensitivity("0.10") == (0.10, "strict")

    def test_numeric_float_accepted(self):
        assert resolve_sensitivity(0.20) == (0.20, "standard")

    def test_arbitrary_number_kept_as_label(self):
        threshold, label = resolve_sensitivity(0.42)
        assert threshold == 0.42 and label == "0.42"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Sensibilidade inválida"):
            resolve_sensitivity("very-strict")


class TestValidateReason:
    @pytest.mark.parametrize("reason", sorted(VALID_REASONS))
    def test_valid_categories_accepted(self, reason):
        assert validate_reason(reason) == (reason, True)

    def test_case_insensitive(self):
        assert validate_reason("Unsupported") == ("unsupported", True)

    @pytest.mark.parametrize(
        "old", ["mechanism_unsupported", "wrong_direction", "anchor_missing",
                "optional_field_unfounded", "granularity_violation"],
    )
    def test_old_categories_now_rejected(self, old):
        """As 5 antigas não são mais válidas — passavam em silêncio antes."""
        value, ok = validate_reason(old)
        assert (value, ok) == ("none", False)

    def test_garbage_rejected(self):
        assert validate_reason("whatever") == ("none", False)
        assert validate_reason("") == ("none", False)


class TestApplicableCategories:
    def test_chain_template_gets_chain_categories(self):
        fields = {"chain": _field("CHAIN"), "text": _field("QUOTATION")}
        applicable = applicable_categories(fields)
        assert "inverted" in applicable
        assert "granularity" in applicable
        assert applicable["inverted"] == ["chain"]

    def test_template_without_chain_omits_chain_categories(self):
        """O ponto central da §7.2.1: 30% dos templates não têm CHAIN."""
        fields = {"score": _field("SCALE"), "criterio": _field("ENUMERATED")}
        applicable = applicable_categories(fields)
        assert "granularity" not in applicable
        assert "infidelity" not in applicable
        assert "overstated" in applicable

    def test_quotation_enables_infidelity(self):
        applicable = applicable_categories({"text": _field("QUOTATION")})
        assert applicable["infidelity"] == ["text"]

    def test_required_enables_incomplete(self):
        fields = {"text": _field("TEXT"), "zone": _field("ENUMERATED")}
        applicable = applicable_categories(fields, required=["zone"])
        assert applicable["incomplete"] == ["zone"]

    def test_unsupported_always_present(self):
        applicable = applicable_categories({"x": _field("TEXT")})
        assert applicable["unsupported"] == ["any field"]

    def test_empty_template(self):
        assert applicable_categories({}) == {}


class TestBuildTaxonomySection:
    def test_emits_definitions_not_bare_names(self):
        """A causa raiz do §3: categorias chegavam como nomes crus."""
        section = build_taxonomy_section({"chain": _field("CHAIN")})
        assert "inverted" in section
        assert REASON_CATEGORIES["inverted"][:40] in section

    def test_omits_inapplicable_categories(self):
        section = build_taxonomy_section({"score": _field("SCALE")})
        assert "granularity" not in section
        assert "infidelity" not in section

    def test_none_always_offered(self):
        section = build_taxonomy_section({"score": _field("SCALE")})
        assert "none" in section

    def test_lists_applicable_fields(self):
        section = build_taxonomy_section({"chain": _field("CHAIN")})
        assert "applies to: chain" in section

    def test_empty_template_yields_empty(self):
        assert build_taxonomy_section({}) == ""

    def test_format_version_is_two(self):
        assert SYNR_FORMAT_VERSION == 2
