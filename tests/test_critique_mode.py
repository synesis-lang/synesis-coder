"""Testes para critique_mode — Fase 2 do pipeline ACT.

Todos os testes de helpers são unitários (sem LLM).
Testes de process_critique usam mock do LLMClient.
Testes de integração com synesis.load() usam o projeto social_acceptance real.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synesis_coder.modes.critique_mode import (
    DEFAULT_SUSPICION_THRESHOLD,
    _extract_abstract_from_bib,
    _extract_item_text,
    AGREEMENT_WARNING_THRESHOLD,
    CONTEXT_WINDOW_CHARS,
    _build_critique_source,
    _calibration_warning,
    _get_source_text,
    _parse_critique_response,
    process_critique,
)
from synesis_coder.prompt_builder import build_critique_prompt
from synesis_coder.synr_io import create_synr, parse_synr, write_synr

# ---------------------------------------------------------------------------
# Constantes e fixtures
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"

_ITEM_BLOCK = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust and environmental concern are the most important factors.
        note: Trust operates as prerequisite for acceptance
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM
""")

_SYN_TWO_ITEMS = textwrap.dedent("""\
    SOURCE @smith2024
        description: Study on community trust.
        epistemic_model: Technology Acceptance Model
        method: survey
    END SOURCE

    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy.
        note: Trust is prerequisite
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM

    ITEM @smith2024
        text: Environmental concern co-enables participation.
        note: Dual mechanism alongside Trust
        chain: Environmental_Concern -> ENABLES -> Participation
    END ITEM
""")

_BIB_CONTENT = textwrap.dedent("""\
    @article{smith2024,
      title = {Community Trust and Social Acceptance},
      author = {Smith, J.},
      abstract = {Community trust and environmental concern are the most important factors
    influencing social acceptance of wind energy projects in rural areas. Local ownership
    models significantly reduce opposition and increase participation.},
      year = {2024}
    }

    @article{jones2020,
      title = {Environmental Factors},
      author = {Jones, A.},
      abstract = {Environmental concern independently predicts participation in energy projects.},
      year = {2020}
    }
""")


# ---------------------------------------------------------------------------
# _extract_item_text
# ---------------------------------------------------------------------------


class TestExtractItemText:
    def test_extracts_text_field(self):
        result = _extract_item_text(_ITEM_BLOCK)
        assert "Community trust" in result
        assert "environmental concern" in result

    def test_returns_empty_when_no_text(self):
        block = "ITEM @ref\n    note: some note\nEND ITEM\n"
        result = _extract_item_text(block)
        assert result == ""

    def test_does_not_include_next_field(self):
        result = _extract_item_text(_ITEM_BLOCK)
        assert "Trust operates" not in result  # note field content
        assert "Trust -> ENABLES" not in result  # chain field content


# ---------------------------------------------------------------------------
# _extract_abstract_from_bib
# ---------------------------------------------------------------------------


class TestExtractAbstractFromBib:
    def test_extracts_known_bibref(self):
        result = _extract_abstract_from_bib("smith2024", _BIB_CONTENT)
        assert result is not None
        assert "Community trust" in result
        assert "wind energy" in result

    def test_returns_none_for_unknown_bibref(self):
        result = _extract_abstract_from_bib("nonexistent2099", _BIB_CONTENT)
        assert result is None

    def test_extracts_correct_entry_among_multiple(self):
        result = _extract_abstract_from_bib("jones2020", _BIB_CONTENT)
        assert result is not None
        assert "Environmental concern" in result
        assert "independently predicts" in result

    def test_empty_bib_content(self):
        result = _extract_abstract_from_bib("smith2024", "")
        assert result is None


# ---------------------------------------------------------------------------
# _get_source_text
# ---------------------------------------------------------------------------


class TestGetSourceText:
    def test_prefers_bib_abstract(self):
        ctx = {"bib_content": _BIB_CONTENT}
        result = _get_source_text(_ITEM_BLOCK, "smith2024", ctx)
        assert "wind energy" in result  # from bib, not from item text field

    def test_falls_back_to_item_text_when_no_bib(self):
        ctx = {"bib_content": None}
        result = _get_source_text(_ITEM_BLOCK, "smith2024", ctx)
        assert "Community trust" in result

    def test_falls_back_when_bibref_not_in_bib(self):
        ctx = {"bib_content": _BIB_CONTENT}
        result = _get_source_text(_ITEM_BLOCK, "unknown2099", ctx)
        # Should fall back to text field
        assert "Community trust" in result

    def test_fallback_sentinel_when_no_text(self):
        ctx = {"bib_content": None}
        block = "ITEM @ref\n    note: something\nEND ITEM\n"
        result = _get_source_text(block, "ref", ctx)
        assert "not available" in result


# ---------------------------------------------------------------------------
# _build_critique_source — Fase 2 (escopo do trecho)
# ---------------------------------------------------------------------------


_ABSTRACT_LONG = (
    "A" * 500
    + " Community trust enables social acceptance of wind energy projects. "
    + "B" * 500
)
_BIB_LONG = f"@article{{long2024,\n  title = {{T}},\n  abstract = {{{_ABSTRACT_LONG}}}\n}}\n"


class TestBuildCritiqueSource:
    def test_wraps_excerpt_in_target(self):
        block = (
            "ITEM @long2024\n"
            "    text: Community trust enables social acceptance of wind energy projects.\n"
            "END ITEM\n"
        )
        source, anchored = _build_critique_source(block, "long2024", {"bib_content": _BIB_LONG})
        assert anchored is True
        assert "<target>" in source and "</target>" in source
        assert "Community trust enables social acceptance" in source

    def test_window_truncates_abstract(self):
        block = (
            "ITEM @long2024\n"
            "    text: Community trust enables social acceptance of wind energy projects.\n"
            "END ITEM\n"
        )
        source, _ = _build_critique_source(block, "long2024", {"bib_content": _BIB_LONG})
        # 1000 chars de preenchimento reduzidos a ~300 de cada lado
        assert len(source) < len(_ABSTRACT_LONG)
        assert source.count("A") <= CONTEXT_WINDOW_CHARS + 5
        assert source.count("B") <= CONTEXT_WINDOW_CHARS + 5

    def test_unanchored_falls_back_to_full_abstract(self):
        block = "ITEM @long2024\n    text: totally absent sentence.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "long2024", {"bib_content": _BIB_LONG})
        assert anchored is False
        assert "<target>" not in source
        assert source == _ABSTRACT_LONG

    def test_no_abstract_uses_excerpt_as_target(self):
        block = "ITEM @ref\n    text: Only this sentence.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "ref", {"bib_content": None})
        assert anchored is True
        assert source == "<target>Only this sentence.</target>"

    def test_no_excerpt_returns_abstract_unanchored(self):
        block = "ITEM @long2024\n    note: no text field\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "long2024", {"bib_content": _BIB_LONG})
        assert anchored is False
        assert "<target>" not in source

    def test_matches_despite_typographic_apostrophe(self):
        abstract = "The study of Si’s role in the network is central."
        bib = f"@article{{typo2024,\n  abstract = {{{abstract}}}\n}}\n"
        block = "ITEM @typo2024\n    text: The study of Si's role in the network is central.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "typo2024", {"bib_content": bib})
        assert anchored is True
        assert "<target>" in source

    def test_matches_despite_whitespace_differences(self):
        abstract = "Trust   enables\n  acceptance of the technology."
        bib = f"@article{{ws2024,\n  abstract = {{{abstract}}}\n}}\n"
        block = "ITEM @ws2024\n    text: Trust enables acceptance of the technology.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "ws2024", {"bib_content": bib})
        assert anchored is True
        assert "<target>" in source

    def test_excerpt_at_start_no_underflow(self):
        abstract = "Opening sentence here. " + "C" * 400
        bib = f"@article{{start2024,\n  abstract = {{{abstract}}}\n}}\n"
        block = "ITEM @start2024\n    text: Opening sentence here.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "start2024", {"bib_content": bib})
        assert anchored is True
        assert source.startswith("<target>Opening sentence here.</target>")

    def test_excerpt_at_end_no_overflow(self):
        abstract = "D" * 400 + " Closing sentence here."
        bib = f"@article{{end2024,\n  abstract = {{{abstract}}}\n}}\n"
        block = "ITEM @end2024\n    text: Closing sentence here.\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "end2024", {"bib_content": bib})
        assert anchored is True
        assert source.endswith("<target>Closing sentence here.</target>")

    def test_target_preserves_original_casing(self):
        abstract = "The Wind Energy Sector expanded rapidly last year."
        bib = f"@article{{case2024,\n  abstract = {{{abstract}}}\n}}\n"
        block = "ITEM @case2024\n    text: the wind energy sector expanded rapidly\nEND ITEM\n"
        source, anchored = _build_critique_source(block, "case2024", {"bib_content": bib})
        assert anchored is True
        assert "<target>The Wind Energy Sector expanded rapidly</target>" in source


# ---------------------------------------------------------------------------
# _calibration_warning — Fase 3 (calibração anti-falso-positivo)
# ---------------------------------------------------------------------------


class TestCalibrationWarning:
    def test_warns_below_threshold(self):
        # face85 real: 75/108 sinalizados → concordância 0.306
        w = _calibration_warning(0.306, 108)
        assert w is not None
        assert "0.306" in w
        assert "descalibrado" in w

    def test_silent_above_threshold(self):
        assert _calibration_warning(0.85, 108) is None

    def test_silent_exactly_at_threshold(self):
        assert _calibration_warning(AGREEMENT_WARNING_THRESHOLD, 108) is None

    def test_warns_just_below_threshold(self):
        assert _calibration_warning(AGREEMENT_WARNING_THRESHOLD - 0.01, 108) is not None

    def test_silent_on_small_sample(self):
        """Poucos ITEMs → taxa instável, não sustenta o aviso."""
        assert _calibration_warning(0.0, 5) is None

    def test_warns_on_boundary_sample_size(self):
        assert _calibration_warning(0.1, 10) is not None


# ---------------------------------------------------------------------------
# _parse_critique_response
# ---------------------------------------------------------------------------


class TestParseCritiqueResponse:
    def test_parses_hash_dollar_format(self):
        raw = "# $divergence: 0.84\n# $reason: inverted\n# $chain: A -> B -> C"
        tags = _parse_critique_response(raw)
        assert tags["divergence"] == "0.84"
        assert tags["reason"] == "inverted"
        assert tags["chain"] == "A -> B -> C"

    def test_parses_plain_format_fallback(self):
        raw = "suspicion_score: 0.72\nreason: missing_evidence\nnote: rephrase needed"
        tags = _parse_critique_response(raw)
        assert tags["divergence"] == "0.72"
        # `missing_evidence` nunca foi categoria válida: antes passava em
        # silêncio; agora é rejeitada e normalizada (Estudo §7.2.5).
        assert tags["reason"] == "none"

    def test_defaults_when_no_output(self):
        tags = _parse_critique_response("")
        assert tags["divergence"] == "0.0"
        assert tags["reason"] == "none"

    def test_defaults_when_only_partial_output(self):
        raw = "# $divergence: 0.5"
        tags = _parse_critique_response(raw)
        assert tags["divergence"] == "0.5"
        assert tags["reason"] == "none"  # default injected

    def test_ignores_synesis_block_keywords(self):
        """Não deve capturar linhas ITEM/END como tags."""
        raw = "ITEM @ref\n# $divergence: 0.1\n# $reason: none\nEND ITEM"
        tags = _parse_critique_response(raw)
        assert "ITEM" not in tags
        assert "END" not in tags
        assert tags["divergence"] == "0.1"

    def test_parses_mixed_formats(self):
        """Aceita # $ e plain na mesma resposta (prioriza # $)."""
        raw = "# $divergence: 0.6\nreason: off_topic"
        tags = _parse_critique_response(raw)
        assert tags["divergence"] == "0.6"
        assert tags["reason"] == "none"  # fora do enum → rejeitado

    def test_preserves_arrow_values(self):
        raw = "# $chain: Trust -> INFLUENCES -> Social_Acceptance"
        tags = _parse_critique_response(raw)
        assert tags["chain"] == "Trust -> INFLUENCES -> Social_Acceptance"


# ---------------------------------------------------------------------------
# build_critique_prompt
# ---------------------------------------------------------------------------


class TestBuildCritiquePrompt:
    def test_prompt_structure(self):
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")
        from synesis_coder.project_loader import load_project
        ctx = load_project(PROJECT_SOCIAL)

        messages = build_critique_prompt(ctx, _ITEM_BLOCK, "source text here")

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["cache"] is True
        assert messages[1]["role"] == "user"
        assert messages[1]["cache"] is False

    def test_system_prompt_contains_guidelines(self):
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")
        from synesis_coder.project_loader import load_project
        ctx = load_project(PROJECT_SOCIAL)

        messages = build_critique_prompt(ctx, _ITEM_BLOCK, "source text")
        system_content = messages[0]["content"]

        # Deve conter pelo menos um dos nomes de campo
        assert any(f in system_content for f in ("chain", "note", "text"))
        # Deve conter instruções de output format
        assert "divergence" in system_content

    def test_user_message_contains_item_and_source(self):
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")
        from synesis_coder.project_loader import load_project
        ctx = load_project(PROJECT_SOCIAL)

        source = "This is the source text about trust."
        messages = build_critique_prompt(ctx, _ITEM_BLOCK, source)
        user_content = messages[1]["content"]

        assert "Community trust" in user_content  # from item block
        assert "This is the source text" in user_content

    def test_user_message_has_output_instruction(self):
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")
        from synesis_coder.project_loader import load_project
        ctx = load_project(PROJECT_SOCIAL)

        messages = build_critique_prompt(ctx, _ITEM_BLOCK, "source text")
        assert "critique" in messages[1]["content"].lower()


# ---------------------------------------------------------------------------
# process_critique com LLM mockado
# ---------------------------------------------------------------------------


class TestProcessCritiqueWithMockLLM:
    """Testa process_critique sem chamar LLM real."""

    def _make_mock_client(self, response: str):
        """Cria um mock de LLMClient que retorna `response` em call_async."""
        mock = MagicMock()
        mock.model = "mock-model"
        mock.usage = MagicMock()
        mock.usage.summary_line.return_value = "tokens: in 0 | out 0 | total 0 | calls 0"
        mock.call_async = AsyncMock(return_value=response)
        return mock

    def test_high_score_generates_revision_block(self, tmp_path):
        """Score >= threshold gera bloco # REVISION no .synr."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        # Criar .syn mínimo
        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        high_score_response = "# $divergence: 0.85\n# $reason: inverted\n# $chain: Trust -> INFLUENCES -> Social_Acceptance"
        low_score_response = "# $divergence: 0.05\n# $reason: none"

        call_count = 0
        async def mock_call_async(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return high_score_response if call_count == 1 else low_score_response

        mock_client = self._make_mock_client(high_score_response)
        mock_client.call_async = mock_call_async

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                suspicion_threshold=0.20,
                format="plain",
            )

        synr_file = tmp_path / "test.synr"
        assert synr_file.exists()
        content = synr_file.read_text(encoding="utf-8")
        # Primeiro ITEM (score 0.85) deve ter REVISION
        assert "# REVISION" in content
        assert "# $divergence: 0.85" in content

    def test_low_score_no_revision_block(self, tmp_path):
        """Score < threshold não gera bloco # REVISION."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        low_score = "# $divergence: 0.05\n# $reason: none"
        mock_client = self._make_mock_client(low_score)
        mock_client.call_async = AsyncMock(return_value=low_score)

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                suspicion_threshold=0.20,
                format="plain",
            )

        synr_file = tmp_path / "test.synr"
        assert synr_file.exists()
        content = synr_file.read_text(encoding="utf-8")
        assert "# REVISION" not in content

    def test_synr_has_phase_header(self, tmp_path):
        """Arquivo .synr gerado tem cabeçalho # $phase: critique."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        mock_client = self._make_mock_client("# $divergence: 0.0\n# $reason: none")
        mock_client.call_async = AsyncMock(return_value="# $divergence: 0.0\n# $reason: none")

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                format="plain",
            )

        content = (tmp_path / "test.synr").read_text(encoding="utf-8")
        assert "# $phase: review" in content
        assert "# $model:" in content
        assert "# $timestamp:" in content

    def test_default_output_path(self, tmp_path):
        """Sem --output, gera arquivo com extensão .synr."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "review.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        mock_client = self._make_mock_client("# $divergence: 0.0\n# $reason: none")
        mock_client.call_async = AsyncMock(return_value="# $divergence: 0.0\n# $reason: none")

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                format="plain",
            )

        assert (tmp_path / "review.synr").exists()

    def test_custom_output_path(self, tmp_path):
        """--output personalizado é respeitado."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        mock_client = self._make_mock_client("# $divergence: 0.0\n# $reason: none")
        mock_client.call_async = AsyncMock(return_value="# $divergence: 0.0\n# $reason: none")

        custom_out = tmp_path / "subdir" / "output.synr"

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                output_path=custom_out,
                format="plain",
            )

        assert custom_out.exists()

    def test_verbose_format(self, tmp_path):
        """Formato verbose inclui cabeçalho com metadados."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        mock_client = self._make_mock_client("# $divergence: 0.0\n# $reason: none")
        mock_client.call_async = AsyncMock(return_value="# $divergence: 0.0\n# $reason: none")

        with patch("synesis_coder.modes.critique_mode.LLMClient", return_value=mock_client):
            result = process_critique(
                syn_path=syn_file,
                project_path=PROJECT_SOCIAL,
                format="verbose",
            )

        assert "# synesis-coder critique" in result

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            process_critique(syn_path=tmp_path / "nao_existe.syn")

    def test_project_not_found_raises(self, tmp_path):
        """Sem projeto detectável levanta FileNotFoundError."""
        syn_file = tmp_path / "test.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        with pytest.raises(FileNotFoundError, match="synp"):
            process_critique(
                syn_path=syn_file,
                project_path=None,
                # tmp_path não tem .synp → deve falhar
            )


# ---------------------------------------------------------------------------
# Integração: .synr gerado por critique compila via synesis.load()
# ---------------------------------------------------------------------------


class TestCritiqueOutputCompiles:
    def test_synr_output_compiles(self, tmp_path):
        """O .synr gerado pelo critique compila via synesis.load() sem erros."""
        try:
            import synesis

            from synesis_coder.project_loader import load_project
            from synesis_coder.validator import _has_structural_errors
        except ImportError:
            pytest.skip("synesis não disponível")

        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        social_dir = PROJECT_SOCIAL.parent
        syn_files = list(social_dir.glob("*.syn"))
        if not syn_files:
            pytest.skip("Nenhum .syn encontrado no projeto social_acceptance")

        syn_content = syn_files[0].read_text(encoding="utf-8")
        ctx = load_project(PROJECT_SOCIAL)

        # Simular output do critique: revisar o primeiro ITEM com score alto
        from synesis_coder.modes.critique_mode import _extract_item_blocks_with_bibrefs as _eib
        items = _eib(syn_content)
        n_items = len(items)

        revisions = [{"suspicion_score": "0.85", "reason": "inverted"}] + \
                    [None] * max(0, n_items - 1)

        header = {"phase": "critique", "model": "test-model", "timestamp": "T"}
        synr_doc = create_synr(syn_content, header, revisions)

        synr_file = tmp_path / "output.synr"
        write_synr(synr_file, synr_doc)

        final_content = synr_file.read_text(encoding="utf-8")
        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"output.synr": final_content},
            bibliography_content=ctx.get("bib_content"),
        )

        assert not _has_structural_errors(result), (
            f".synr gerado pelo critique não compilou:\n{result.get_diagnostics()}"
        )
