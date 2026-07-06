"""Testes para refine_mode — re-extração com feedback (fase R do pipeline ACT).

Estratégia (espelha test_critique_mode):
- Unidades puras (normalização, reassembly, métricas, prompt) sem LLM.
- Loop (_refine_single_item) com clients FAKE determinísticos e
  validate_and_fix_async monkeypatchado (o validador tem testes próprios).
- Um teste de integração de process_refine com o projeto social_acceptance real.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synesis_coder.modes import refine_mode
from synesis_coder.modes.refine_mode import (
    IterationRecord,
    RefineResult,
    _build_metrics_header,
    _normalize_block,
    _reassemble_syn,
    _refine_single_item,
    process_refine,
)
from synesis_coder.prompt_builder import (
    _format_critique_feedback,
    build_item_refinement_prompt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CASES_DIR = Path("d:/GitHub/case-studies")
PROJECT_SOCIAL = CASES_DIR / "Sociology/Social_Acceptance/social_acceptance.synp"

_ITEM_A = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy.
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM""")

_ITEM_A_FIXED = textwrap.dedent("""\
    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy.
        chain: Trust -> INFLUENCES -> Social_Acceptance
    END ITEM""")

_SYN_TWO_ITEMS = textwrap.dedent("""\
    SOURCE @smith2024
        description: Study on community trust.
        method: survey
    END SOURCE

    ITEM @smith2024
        text: Community trust enables social acceptance of wind energy.
        chain: Trust -> ENABLES -> Social_Acceptance
    END ITEM

    ITEM @smith2024
        text: Environmental concern co-enables participation.
        chain: Environmental_Concern -> ENABLES -> Participation
    END ITEM
""")


# ctx mínimo aceito por _build_system_prompt (caminho texto-livre da re-extração).
_MIN_CTX = {
    "project_description": "test",
    "item_fields": {},
    "required_item": [],
    "chain_relations": {},
    "code_index": {"empty": True, "codes": []},
    "topic_index": {"empty": True, "topics": []},
    "bundle_pairs": [],
}


class _FakeClient:
    """Client fake: critique retorna scores scriptados; re-extração, blocos scriptados.

    O loop chama call_async tanto para critique (via _critique_tags) quanto,
    no fallback texto-livre, para re-extração. Distinguimos pelo conteúdo do
    prompt: mensagens de critique contêm "structured critique"; as de refino,
    "corrected ITEM".
    """

    def __init__(self, critique_scores, reextractions, model="fake-model"):
        self._scores = list(critique_scores)
        self._reextractions = list(reextractions)
        self.model = model
        self.backend = "anthropic"
        self.usage = MagicMock()
        self.usage.api_calls = 0
        self.usage.input_tokens = 0
        self.usage.output_tokens = 0

    def supports_json_schema(self):
        return False  # força caminho texto-livre (determinístico p/ teste)

    async def call_async(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "structured critique" in user.lower() or "evaluate whether" in user.lower():
            score = self._scores.pop(0) if self._scores else 0.0
            return f"# $suspicion_score: {score}\n# $reason: wrong_direction"
        # re-extração
        return self._reextractions.pop(0) if self._reextractions else _ITEM_A


async def _passthrough_validate(candidate, ctx, client, **kwargs):
    """Substitui validate_and_fix_async: aceita o candidato como válido."""
    return candidate, True


async def _reject_validate(candidate, ctx, client, **kwargs):
    """Substitui validate_and_fix_async: rejeita todo candidato."""
    return candidate, False


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _normalize_block
# ---------------------------------------------------------------------------


class TestNormalizeBlock:
    def test_whitespace_insensitive(self):
        a = "ITEM @x\n    chain: A -> B\nEND ITEM"
        b = "ITEM @x\n        chain:   A -> B\n\nEND ITEM"
        assert _normalize_block(a) == _normalize_block(b)

    def test_case_insensitive(self):
        assert _normalize_block("Chain: Trust") == _normalize_block("chain: trust")

    def test_distinct_content_differs(self):
        assert _normalize_block(_ITEM_A) != _normalize_block(_ITEM_A_FIXED)


# ---------------------------------------------------------------------------
# _format_critique_feedback (prompt builder helper)
# ---------------------------------------------------------------------------


class TestFormatFeedback:
    def test_includes_reason_and_field_hints(self):
        tags = {
            "suspicion_score": "0.8",
            "reason": "wrong_direction",
            "reason_detail": "arrow points the wrong way",
            "chain": "Trust -> INFLUENCES -> Social_Acceptance",
        }
        out = _format_critique_feedback(tags)
        assert "wrong_direction" in out
        assert "arrow points the wrong way" in out
        assert "chain: Trust -> INFLUENCES -> Social_Acceptance" in out

    def test_excludes_meta_tags_from_hints(self):
        tags = {"suspicion_score": "0.8", "reason": "none", "note": "internal"}
        out = _format_critique_feedback(tags)
        # note/suspicion_score não devem aparecer como field hints
        assert "note:" not in out
        assert "suspicion_score" not in out

    def test_numbered_keys_normalized(self):
        tags = {"chain": "A -> R -> B", "chain.1": "C -> R -> D"}
        out = _format_critique_feedback(tags)
        assert "chain: A -> R -> B" in out
        assert "chain: C -> R -> D" in out


# ---------------------------------------------------------------------------
# build_item_refinement_prompt (estrutura)
# ---------------------------------------------------------------------------


class TestRefinementPromptStructure:
    def _ctx(self):
        # ctx mínimo suficiente para _build_system_prompt.
        return {
            "project_description": "test project",
            "item_fields": {},
            "required_item": [],
            "chain_relations": {},
            "code_index": {"empty": True, "codes": []},
            "topic_index": {"empty": True, "topics": []},
            "bundle_pairs": [],
        }

    def test_system_user_roles_and_cache(self):
        msgs = build_item_refinement_prompt(
            self._ctx(), "smith2024", "the source", _ITEM_A,
            {"reason": "wrong_direction", "chain": "Trust -> INFLUENCES -> X"},
        )
        assert msgs[0]["role"] == "system" and msgs[0]["cache"] is True
        assert msgs[1]["role"] == "user" and msgs[1]["cache"] is False

    def test_user_message_contains_source_prev_and_feedback(self):
        msgs = build_item_refinement_prompt(
            self._ctx(), "smith2024", "THE SOURCE TEXT", _ITEM_A,
            {"reason": "wrong_direction"},
        )
        user = msgs[1]["content"]
        assert "THE SOURCE TEXT" in user
        assert "PREVIOUS ANNOTATION" in user
        assert "REVIEWER DIAGNOSIS" in user
        assert "wrong_direction" in user
        assert "@smith2024" in user


# ---------------------------------------------------------------------------
# _reassemble_syn
# ---------------------------------------------------------------------------


class TestReassemble:
    def test_replaces_item_blocks_preserves_source(self):
        results = [
            RefineResult("smith2024", _ITEM_A_FIXED, 0.1, 0.8),
            RefineResult(
                "smith2024",
                "ITEM @smith2024\n    text: unchanged.\nEND ITEM",
                0.05, 0.05,
            ),
        ]
        out = _reassemble_syn(_SYN_TWO_ITEMS, results)
        assert "SOURCE @smith2024" in out          # SOURCE preservado
        assert "INFLUENCES" in out                  # 1º ITEM substituído
        assert "text: unchanged." in out            # 2º ITEM substituído
        assert "ENABLES -> Social_Acceptance" not in out
        # Ordem: SOURCE antes dos ITEMs
        assert out.index("SOURCE") < out.index("INFLUENCES")


# ---------------------------------------------------------------------------
# _build_metrics_header
# ---------------------------------------------------------------------------


class TestMetricsHeader:
    def test_contains_aggregate_and_per_item_trace(self):
        results = [
            RefineResult(
                "smith2024", _ITEM_A_FIXED, 0.18, 0.62,
                trace=[IterationRecord(0, 0.62), IterationRecord(1, 0.18)],
                improved=True,
            ),
        ]
        header = _build_metrics_header(
            results, Path("corpus.syn"), "critic-m", "gen-m", 2, 0.20,
        )
        assert "# --- Fase R: Refine" in header
        assert "metrics.refine.items_total: 1" in header
        assert "metrics.refine.items_improved: 1" in header
        assert "metrics.refine.critique_model: critic-m" in header
        assert "metrics.refine.refine_model: gen-m" in header
        assert "$refine.@smith2024.trace: 0.62 -> 0.18" in header


# ---------------------------------------------------------------------------
# _refine_single_item — lógica do loop
# ---------------------------------------------------------------------------


class TestRefineLoop:
    def _sem(self):
        return asyncio.Semaphore(1)

    def test_converges_below_threshold_no_iteration(self):
        """Score inicial < threshold → não entra no loop, retorna original."""
        critic = _FakeClient(critique_scores=[0.05], reextractions=[])
        gen = _FakeClient(critique_scores=[], reextractions=[])
        with patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=2, thinking_budget=0,
            ))
        assert res.improved is False
        assert res.final_block == _ITEM_A
        assert len(res.trace) == 1  # só o baseline

    def test_improves_when_score_drops(self):
        """Score inicial alto; re-extração baixa o score → aceita nova versão."""
        # baseline 0.62; critique dentro do loop 0.62; candidato pontua 0.10
        critic = _FakeClient(critique_scores=[0.62, 0.62, 0.10], reextractions=[])
        gen = _FakeClient(critique_scores=[], reextractions=[_ITEM_A_FIXED])
        with patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=2, thinking_budget=0,
            ))
        assert res.improved is True
        assert "INFLUENCES" in res.final_block
        assert res.final_score == pytest.approx(0.10)

    def test_non_regression_rejects_worse_candidate(self):
        """Candidato com score >= atual é rejeitado; original preservado."""
        # baseline 0.62; critique loop 0.62; candidato pontua 0.70 (pior)
        critic = _FakeClient(critique_scores=[0.62, 0.62, 0.70], reextractions=[])
        gen = _FakeClient(critique_scores=[], reextractions=[_ITEM_A_FIXED])
        with patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=2, thinking_budget=0,
            ))
        assert res.improved is False
        assert res.final_block == _ITEM_A
        assert res.final_score == pytest.approx(0.62)

    def test_invalid_candidate_rejected(self):
        """validate_and_fix_async falha → mantém a melhor versão anterior."""
        critic = _FakeClient(critique_scores=[0.62, 0.62], reextractions=[])
        gen = _FakeClient(critique_scores=[], reextractions=[_ITEM_A_FIXED])
        with patch.object(refine_mode, "validate_and_fix_async", _reject_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=2, thinking_budget=0,
            ))
        assert res.improved is False
        assert res.final_block == _ITEM_A

    def test_fixed_point_stops(self):
        """Re-extração devolve bloco idêntico ao anterior → para (ponto-fixo)."""
        critic = _FakeClient(critique_scores=[0.62, 0.62], reextractions=[])
        # gerador devolve o MESMO bloco de entrada
        gen = _FakeClient(critique_scores=[], reextractions=[_ITEM_A])
        with patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=3, thinking_budget=0,
            ))
        assert res.improved is False  # nenhum progresso

    def test_max_iter_respected(self):
        """max_iter=1 executa no máximo uma re-extração."""
        # Se não parasse, consumiria mais scores. Fornecemos scores decrescentes
        # p/ garantir que só 1 iteração roda apesar de o score continuar alto.
        critic = _FakeClient(critique_scores=[0.80, 0.80, 0.60], reextractions=[])
        gen = _FakeClient(critique_scores=[], reextractions=[_ITEM_A_FIXED, _ITEM_A])
        with patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            res = _run(_refine_single_item(
                _ITEM_A, "smith2024", _MIN_CTX, critic, gen, self._sem(),
                suspicion_threshold=0.20, max_iter=1, thinking_budget=0,
            ))
        # trace: baseline + no máximo 1 iteração
        assert len(res.trace) <= 2


# ---------------------------------------------------------------------------
# process_refine — integração com projeto real + LLM mockado
# ---------------------------------------------------------------------------


class TestProcessRefine:
    def test_distinct_critique_and_refine_clients(self, tmp_path):
        """Dois LLMClient são instanciados (crítico != gerador) — §3.3."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "corpus.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        instantiated = []

        def _fake_ctor(model=None, **kwargs):
            c = _FakeClient(critique_scores=[0.05] * 10, reextractions=[_ITEM_A] * 10,
                            model=model or "default")
            instantiated.append(c)
            return c

        with patch("synesis_coder.modes.refine_mode.LLMClient", side_effect=_fake_ctor), \
             patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            out = process_refine(
                syn_path=syn_file, project_path=PROJECT_SOCIAL,
                critique_model="critic-m", refine_model="gen-m",
                suspicion_threshold=0.20, max_iter=2,
            )

        assert len(instantiated) == 2  # crítico + gerador
        assert (tmp_path / "corpus_refined.syn").exists()
        assert "Refine concluído" in out

    def test_output_contains_metrics_header(self, tmp_path):
        """O .syn de saída traz o cabeçalho metrics.refine.*."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "corpus.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        def _fake_ctor(model=None, **kwargs):
            return _FakeClient(critique_scores=[0.05] * 10, reextractions=[_ITEM_A] * 10,
                               model=model or "default")

        with patch("synesis_coder.modes.refine_mode.LLMClient", side_effect=_fake_ctor), \
             patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            process_refine(
                syn_path=syn_file, project_path=PROJECT_SOCIAL,
                suspicion_threshold=0.20, max_iter=2,
            )

        content = (tmp_path / "corpus_refined.syn").read_text(encoding="utf-8")
        assert "metrics.refine.items_total: 2" in content
        assert "$refine.@smith2024.trace:" in content

    def test_default_output_suffix(self, tmp_path):
        """Sem --output, gera <stem>_refined.syn (nunca sobrescreve a entrada)."""
        if not PROJECT_SOCIAL.exists():
            pytest.skip("Projeto social_acceptance não encontrado")

        syn_file = tmp_path / "corpus.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        def _fake_ctor(model=None, **kwargs):
            return _FakeClient(critique_scores=[0.05] * 10, reextractions=[_ITEM_A] * 10)

        with patch("synesis_coder.modes.refine_mode.LLMClient", side_effect=_fake_ctor), \
             patch.object(refine_mode, "validate_and_fix_async", _passthrough_validate):
            process_refine(syn_path=syn_file, project_path=PROJECT_SOCIAL)

        assert (tmp_path / "corpus_refined.syn").exists()
        assert syn_file.read_text(encoding="utf-8") == _SYN_TWO_ITEMS  # entrada intacta


# ---------------------------------------------------------------------------
# CLI — subcomando refine registrado
# ---------------------------------------------------------------------------


class TestCliRefine:
    def test_refine_command_registered(self):
        from synesis_coder.cli import main
        assert "refine" in main.commands

    def test_refine_help_renders(self):
        from click.testing import CliRunner

        from synesis_coder.cli import main
        result = CliRunner().invoke(main, ["refine", "--help"])
        assert result.exit_code == 0
        assert "--max-iter" in result.output
        assert "--critique-model" in result.output
        assert "--refine-model" in result.output

    def test_output_equals_input_guard(self, tmp_path):
        """--output == entrada sem --overwrite → erro claro."""
        from click.testing import CliRunner

        from synesis_coder.cli import main

        syn_file = tmp_path / "corpus.syn"
        syn_file.write_text(_SYN_TWO_ITEMS, encoding="utf-8")

        result = CliRunner().invoke(main, [
            "refine", str(syn_file),
            "--project", str(PROJECT_SOCIAL),
            "--output", str(syn_file),
        ])
        assert result.exit_code == 1
        assert "arquivo de entrada" in result.output
