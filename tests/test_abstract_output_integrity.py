"""Integridade da saída do modo abstract — Etapas 0 e 1.

Cobrem dois defeitos reproduzidos em
synesis-planning/synesis-coder/Estudo_Saida_Particionada_e_Incremental.md:

  §2   — o modo arquivo-único TRUNCAVA a saída a cada batch; numa campanha de
         2.800 registros sobravam 25.
  §8.1 — o bloco de falha continha apenas comentários, e um `.syn` só de
         comentários NÃO é parseável: o `load_project()` do batch seguinte
         levantava exceção e a campanha inteira abortava.

Nenhum teste aqui chama LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import synesis

from synesis_coder.modes import abstract_mode as am
from synesis_coder.modes.abstract_mode import (
    FAILURE_MARKER,
    _build_failure_block,
    _placeholder_value,
    is_failed_output,
)
from synesis_coder.project_loader import load_project

CASES_DIR = Path("d:/GitHub/case-studies")
FACE85 = CASES_DIR / "ufmg/face85"
PROJECT_FACE85 = FACE85 / "face85.synp"


def _face85_ctx():
    if not PROJECT_FACE85.exists():
        pytest.skip("corpus face85 indisponível")
    return load_project(PROJECT_FACE85, load_annotations=False)


def _compile(annotations: dict):
    return synesis.load(
        project_content=PROJECT_FACE85.read_text(encoding="utf-8"),
        template_content=(FACE85 / "face85.synt").read_text(encoding="utf-8"),
        annotation_contents=annotations,
        bibliography_content=(
            FACE85 / "face85_selecionados.bib"
        ).read_text(encoding="utf-8"),
    )


# ---------------------------------------------------------------------------
# Etapa 1 — acumulação entre batches
# ---------------------------------------------------------------------------


class TestBatchAccumulation:
    """O arquivo único deve acumular, não truncar, a cada batch."""

    @staticmethod
    def _run_batches(tmp_path, n_batches, per_batch, monkeypatch):
        async def fake(bibref, abstract, ctx, client, sem,
                       entry_index=0, total_entries=0):
            return bibref, "SOURCE @%s\nEND SOURCE" % bibref, True

        monkeypatch.setattr(am, "_process_one_abstract", fake)
        accumulated = []
        for b in range(n_batches):
            entries = [
                {"bibref": "r%d_%d" % (b, i), "abstract": "x"}
                for i in range(per_batch)
            ]
            asyncio.run(am._process_batch(
                entries, {}, None, 2, tmp_path, False, None,
                accumulated=accumulated,
            ))
        return (tmp_path / "annotations.syn").read_text(encoding="utf-8")

    def test_three_batches_all_preserved(self, tmp_path, monkeypatch):
        """Antes desta correção sobrariam apenas os 2 do último batch."""
        content = self._run_batches(tmp_path, 3, 2, monkeypatch)
        sources = [ln for ln in content.splitlines() if ln.startswith("SOURCE")]
        assert len(sources) == 6

    def test_single_batch_unchanged(self, tmp_path, monkeypatch):
        content = self._run_batches(tmp_path, 1, 3, monkeypatch)
        assert content.count("SOURCE @") == 3

    def test_output_order_is_deterministic(self, tmp_path, monkeypatch):
        """as_completed devolvia fora de ordem; a saída precisa ser estável."""
        async def fake(bibref, abstract, ctx, client, sem,
                       entry_index=0, total_entries=0):
            # inverte a ordem de conclusão em relação à de entrada
            await asyncio.sleep(0.01 if bibref.endswith("0") else 0.0)
            return bibref, "SOURCE @%s\nEND SOURCE" % bibref, True

        monkeypatch.setattr(am, "_process_one_abstract", fake)
        entries = [{"bibref": "r%d" % i, "abstract": "x"} for i in range(4)]
        acc = []
        asyncio.run(am._process_batch(
            entries, {}, None, 4, tmp_path, False, None, accumulated=acc,
        ))
        content = (tmp_path / "annotations.syn").read_text(encoding="utf-8")
        sources = [ln for ln in content.splitlines() if ln.startswith("SOURCE")]
        assert sources == ["SOURCE @r0", "SOURCE @r1", "SOURCE @r2", "SOURCE @r3"]

    def test_per_reference_writes_one_file_each(self, tmp_path, monkeypatch):
        async def fake(bibref, abstract, ctx, client, sem,
                       entry_index=0, total_entries=0):
            return bibref, "SOURCE @%s\nEND SOURCE" % bibref, True

        monkeypatch.setattr(am, "_process_one_abstract", fake)
        entries = [{"bibref": "a1", "abstract": "x"}, {"bibref": "a2", "abstract": "y"}]
        asyncio.run(am._process_batch(
            entries, {}, None, 2, tmp_path, True, None, accumulated=[],
        ))
        assert (tmp_path / "a1.syn").exists()
        assert (tmp_path / "a2.syn").exists()
        assert not (tmp_path / "annotations.syn").exists()


# ---------------------------------------------------------------------------
# Etapa 0 — bloco de falha parseável
# ---------------------------------------------------------------------------


class TestFailureBlock:
    def test_failure_block_compiles_alone(self):
        ctx = _face85_ctx()
        blk = _build_failure_block(ctx, "torga2017", "timeout")
        result = _compile({"bad.syn": blk})
        assert len(result.validation_result.errors) == 0

    def test_failure_block_compiles_beside_valid_files(self):
        """O caso que abortava a campanha: erro + arquivos bons no mesmo glob."""
        ctx = _face85_ctx()
        blk = _build_failure_block(ctx, "torga2017", "timeout")
        good = "SOURCE @caliari2017\n    description: d\n" \
               "    knowledge_area: Administração\n    method: m\nEND SOURCE\n"
        result = _compile({"ok.syn": good, "bad.syn": blk})
        assert len(result.validation_result.errors) == 0

    def test_comment_only_block_would_break(self):
        """Regressão: documenta POR QUE o formato antigo era inaceitável."""
        with pytest.raises(Exception):
            _compile({"bad.syn": "# ERRO: falhou\n# timeout\n"})

    def test_failure_block_is_marked(self):
        ctx = _face85_ctx()
        blk = _build_failure_block(ctx, "x2024", "timeout")
        assert is_failed_output(blk)
        assert FAILURE_MARKER in blk

    def test_failure_block_carries_reason(self):
        ctx = _face85_ctx()
        blk = _build_failure_block(ctx, "x2024", "rate limit excedido")
        assert "rate limit excedido" in blk

    def test_successful_output_is_not_marked(self):
        assert not is_failed_output("SOURCE @a\nEND SOURCE\n")

    def test_template_without_required_source_fields(self):
        blk = _build_failure_block({}, "x2024", "erro")
        assert "SOURCE @x2024" in blk
        assert "END SOURCE" in blk
        assert is_failed_output(blk)


class TestResumeDetection:
    """Etapa 3 — o estado vem do disco; falhas precisam ser reprocessadas."""

    OK = "SOURCE @a\n    description: d\nEND SOURCE\n\nITEM @a\n    text: t\nEND ITEM\n"

    def test_successful_record_is_complete(self):
        assert am.is_complete_output(self.OK)

    def test_failed_marker_is_not_complete(self):
        assert not am.is_complete_output(FAILURE_MARKER + "\n" + self.OK)

    def test_source_without_item_is_not_complete(self):
        """O caso do Estudo §8.2: zero ITEMs grava SOURCE completo."""
        assert not am.is_complete_output("SOURCE @a\n    description: d\nEND SOURCE\n")

    def test_legacy_source_only_file_is_not_complete(self):
        """.syn antigo, sem marca, mas sem ITEM — reprocessa."""
        assert not am.is_complete_output("SOURCE @old\nEND SOURCE\n")

    def test_empty_is_not_complete(self):
        assert not am.is_complete_output("")

    def test_per_reference_lists_only_successful(self, tmp_path):
        (tmp_path / "ok1.syn").write_text(self.OK, encoding="utf-8")
        (tmp_path / "ok2.syn").write_text(self.OK, encoding="utf-8")
        (tmp_path / "bad.syn").write_text(
            FAILURE_MARKER + "\nSOURCE @bad\nEND SOURCE\n", encoding="utf-8"
        )
        (tmp_path / "empty.syn").write_text("", encoding="utf-8")
        assert am.completed_bibrefs(tmp_path, per_reference=True) == {"ok1", "ok2"}

    def test_combined_file_lists_only_successful(self, tmp_path):
        content = "\n\n".join([
            "SOURCE @a\n    description: d\nEND SOURCE\n\nITEM @a\n    text: t\nEND ITEM",
            FAILURE_MARKER + "\n# ERRO: timeout\nSOURCE @b\n    description: d\nEND SOURCE",
            "SOURCE @c\n    description: d\nEND SOURCE\n\nITEM @c\n    text: t\nEND ITEM",
        ]) + "\n"
        (tmp_path / "annotations.syn").write_text(content, encoding="utf-8")
        assert am.completed_bibrefs(tmp_path, per_reference=False) == {"a", "c"}

    def test_failure_marker_does_not_leak_to_previous_record(self, tmp_path):
        """A marca precede o SOURCE: o fatiamento deve recuar sobre comentários."""
        content = (
            "SOURCE @a\n    description: d\nEND SOURCE\n\nITEM @a\n    text: t\nEND ITEM\n\n"
            + FAILURE_MARKER + "\nSOURCE @b\n    description: d\nEND SOURCE\n"
        )
        (tmp_path / "annotations.syn").write_text(content, encoding="utf-8")
        done = am.completed_bibrefs(tmp_path, per_reference=False)
        assert "a" in done and "b" not in done

    def test_missing_output_dir_returns_empty(self, tmp_path):
        assert am.completed_bibrefs(tmp_path / "nope", per_reference=True) == set()
        assert am.completed_bibrefs(tmp_path, per_reference=False) == set()


class TestResumeEndToEnd:
    """Interromper e retomar não pode duplicar nem perder registros."""

    @staticmethod
    def _fake_ok(monkeypatch):
        async def fake(bibref, abstract, ctx, client, sem,
                       entry_index=0, total_entries=0):
            block = (
                "SOURCE @%s\n    description: d\nEND SOURCE\n\n"
                "ITEM @%s\n    text: t\nEND ITEM" % (bibref, bibref)
            )
            return bibref, block, True
        monkeypatch.setattr(am, "_process_one_abstract", fake)

    def test_per_reference_resume_skips_done(self, tmp_path, monkeypatch):
        self._fake_ok(monkeypatch)
        acc = []
        first = [{"bibref": "r%d" % i, "abstract": "x"} for i in range(3)]
        asyncio.run(am._process_batch(
            first, {}, None, 3, tmp_path, True, None, accumulated=acc,
        ))
        done = am.completed_bibrefs(tmp_path, per_reference=True)
        assert done == {"r0", "r1", "r2"}

        # "Retomada": todos os 5 do corpus, menos os já feitos
        corpus = [{"bibref": "r%d" % i, "abstract": "x"} for i in range(5)]
        remaining = [e for e in corpus if e["bibref"] not in done]
        assert [e["bibref"] for e in remaining] == ["r3", "r4"]

    def test_combined_resume_preserves_previous_records(self, tmp_path, monkeypatch):
        """Sem semear o acumulador, a retomada apagaria o trabalho anterior."""
        self._fake_ok(monkeypatch)
        acc = []
        asyncio.run(am._process_batch(
            [{"bibref": "r0", "abstract": "x"}, {"bibref": "r1", "abstract": "x"}],
            {}, None, 2, tmp_path, False, None, accumulated=acc,
        ))
        previous = (tmp_path / "annotations.syn").read_text(encoding="utf-8")

        # Retomada: semear como _process_abstract_async faz
        acc2 = [c.rstrip("\n") for _, c in am._iter_records(previous)]
        asyncio.run(am._process_batch(
            [{"bibref": "r2", "abstract": "x"}],
            {}, None, 1, tmp_path, False, None, accumulated=acc2,
        ))
        final = (tmp_path / "annotations.syn").read_text(encoding="utf-8")
        assert am.completed_bibrefs(tmp_path, per_reference=False) == {"r0", "r1", "r2"}
        assert final.count("SOURCE @") == 3


class TestSplitEvery:
    """Etapa 4 — particionamento por contagem de registros."""

    @staticmethod
    def _fake_ok(monkeypatch):
        async def fake(bibref, abstract, ctx, client, sem,
                       entry_index=0, total_entries=0):
            return bibref, (
                "SOURCE @%s\n    description: d\nEND SOURCE\n\n"
                "ITEM @%s\n    text: t\nEND ITEM" % (bibref, bibref)
            ), True
        monkeypatch.setattr(am, "_process_one_abstract", fake)

    @staticmethod
    def _refs(path):
        return [
            ln.split("@")[1]
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("SOURCE")
        ]

    def test_filename_derives_from_global_position(self):
        assert am.split_filename(0, 100) == "annotations_0001.syn"
        assert am.split_filename(99, 100) == "annotations_0001.syn"
        assert am.split_filename(100, 100) == "annotations_0002.syn"
        assert am.split_filename(250, 100) == "annotations_0003.syn"

    def test_is_split_file(self):
        assert am.is_split_file("annotations_0001.syn")
        assert not am.is_split_file("annotations.syn")
        assert not am.is_split_file("smith2024.syn")

    def test_mode_resolution(self):
        assert am.resolve_output_mode(False, None) == "single"
        assert am.resolve_output_mode(True, None) == "per_reference"
        assert am.resolve_output_mode(False, 100) == "split"

    def test_split_and_per_reference_are_exclusive(self):
        with pytest.raises(ValueError, match="mutuamente exclusivos"):
            am.resolve_output_mode(True, 100)

    def test_split_every_must_be_positive(self):
        with pytest.raises(ValueError, match=">= 1"):
            am.resolve_output_mode(False, 0)

    def test_partitions_across_batches(self, tmp_path, monkeypatch):
        """Batches (4) que cruzam partições (3) não devem desalinhar nada."""
        self._fake_ok(monkeypatch)
        entries = [{"bibref": "r%02d" % i, "abstract": "x"} for i in range(7)]
        acc_file = {}
        for start in (0, 4):
            batch = entries[start:start + 4]
            asyncio.run(am._process_batch(
                batch, {}, None, 4, tmp_path, False, None,
                index_base=start, split_every=3,
                accumulated_by_file=acc_file,
                positions=list(range(start, start + len(batch))),
            ))
        assert self._refs(tmp_path / "annotations_0001.syn") == ["r00", "r01", "r02"]
        assert self._refs(tmp_path / "annotations_0002.syn") == ["r03", "r04", "r05"]
        assert self._refs(tmp_path / "annotations_0003.syn") == ["r06"]

    def test_no_record_is_split_across_files(self, tmp_path, monkeypatch):
        self._fake_ok(monkeypatch)
        entries = [{"bibref": "r%d" % i, "abstract": "x"} for i in range(5)]
        asyncio.run(am._process_batch(
            entries, {}, None, 5, tmp_path, False, None,
            split_every=2, accumulated_by_file={},
            positions=list(range(5)),
        ))
        for path in tmp_path.glob("annotations_*.syn"):
            content = path.read_text(encoding="utf-8")
            # Cada SOURCE do arquivo tem o seu END SOURCE
            assert content.count("SOURCE @") == content.count("END SOURCE")

    def test_resume_preserves_partition_mapping(self, tmp_path, monkeypatch):
        """Retomar não pode deslocar registros para outra partição."""
        self._fake_ok(monkeypatch)
        corpus = [{"bibref": "r%02d" % i, "abstract": "x"} for i in range(7)]

        asyncio.run(am._process_batch(
            corpus[:4], {}, None, 4, tmp_path, False, None,
            split_every=3, accumulated_by_file={}, positions=[0, 1, 2, 3],
        ))
        done = am.completed_bibrefs(tmp_path, per_reference=False, split_every=3)
        assert done == {"r00", "r01", "r02", "r03"}

        kept = [(p, e) for p, e in zip(range(7), corpus) if e["bibref"] not in done]
        seeded = {
            p.name: [c.rstrip("\n") for _, c in am._iter_records(
                p.read_text(encoding="utf-8"))]
            for p in sorted(tmp_path.glob("annotations_*.syn"))
        }
        asyncio.run(am._process_batch(
            [e for _, e in kept], {}, None, 3, tmp_path, False, None,
            split_every=3, accumulated_by_file=seeded,
            positions=[p for p, _ in kept],
        ))

        assert self._refs(tmp_path / "annotations_0001.syn") == ["r00", "r01", "r02"]
        assert self._refs(tmp_path / "annotations_0002.syn") == ["r03", "r04", "r05"]
        assert self._refs(tmp_path / "annotations_0003.syn") == ["r06"]

    def test_completed_bibrefs_ignores_non_split_files(self, tmp_path):
        (tmp_path / "annotations_0001.syn").write_text(
            "SOURCE @a\nEND SOURCE\n\nITEM @a\n    text: t\nEND ITEM\n",
            encoding="utf-8",
        )
        (tmp_path / "annotations.syn").write_text(
            "SOURCE @z\nEND SOURCE\n\nITEM @z\n    text: t\nEND ITEM\n",
            encoding="utf-8",
        )
        done = am.completed_bibrefs(tmp_path, per_reference=False, split_every=3)
        assert done == {"a"}


class TestCooldown:
    """Etapa 2 — a pausa deve seguir a duração do BATCH, não o tempo total.

    A fórmula anterior (`elapsed_so_far * 0.1`) usava o acumulado desde o
    início, saturando o teto de 30s por volta do 5º batch: ~55 min de sleep
    numa campanha de 112 batches, contra ~12 min desta.
    """

    def test_proportional_to_batch_duration(self):
        assert am.compute_cooldown(100.0) == 10.0

    def test_respects_floor(self):
        assert am.compute_cooldown(1.0) == am.COOLDOWN_MIN
        assert am.compute_cooldown(0.0) == am.COOLDOWN_MIN

    def test_respects_ceiling(self):
        assert am.compute_cooldown(10_000.0) == am.COOLDOWN_MAX

    def test_does_not_grow_with_elapsed_campaign_time(self):
        """Regressão do bug: batches de mesma duração → mesma pausa."""
        same = [am.compute_cooldown(60.0) for _ in range(112)]
        assert len(set(same)) == 1
        assert same[0] == 6.0

    def test_accumulated_cost_is_far_below_old_formula(self):
        """Compara as duas fórmulas em 112 batches de 60s."""
        batch_seconds = 60.0
        new_total = sum(am.compute_cooldown(batch_seconds) for _ in range(111))
        old_total = sum(
            min(30.0, max(5.0, (batch_seconds * n) * 0.1)) for n in range(1, 112)
        )
        assert new_total < old_total / 3

    @pytest.mark.parametrize(
        "raw,expected",
        [("auto", None), ("", None), (None, None), ("0", 0.0),
         ("12.5", 12.5), (7, 7.0), (0, 0.0)],
    )
    def test_resolve_setting(self, raw, expected):
        assert am.resolve_cooldown_setting(raw) == expected

    def test_resolve_setting_rejects_garbage(self):
        with pytest.raises(ValueError, match="Valor inválido"):
            am.resolve_cooldown_setting("devagar")

    def test_resolve_setting_rejects_negative(self):
        with pytest.raises(ValueError, match="negativo"):
            am.resolve_cooldown_setting(-1)


class TestPlaceholderValue:
    """Sentinela precisa respeitar o TIPO: texto livre em ENUMERATED quebraria."""

    class _Spec:
        def __init__(self, type_name, values=None):
            self.type = type("T", (), {"name": type_name})()
            self.values = values

    def test_enumerated_uses_first_valid_value(self):
        val = type("V", (), {"label": "Administração"})()
        assert _placeholder_value(self._Spec("ENUMERATED", [val])) == "Administração"

    def test_ordered_uses_first_valid_value(self):
        val = type("V", (), {"label": "Low"})()
        assert _placeholder_value(self._Spec("ORDERED", [val])) == "Low"

    def test_scale_uses_zero(self):
        assert _placeholder_value(self._Spec("SCALE")) == "0"

    def test_date_uses_iso_sentinel(self):
        assert _placeholder_value(self._Spec("DATE")) == "1900-01-01"

    def test_text_uses_textual_sentinel(self):
        assert "falha" in _placeholder_value(self._Spec("TEXT"))

    def test_enumerated_without_values_falls_back(self):
        assert "falha" in _placeholder_value(self._Spec("ENUMERATED", None))
