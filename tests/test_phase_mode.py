"""Etapas B2 e B3: `synesis-coder phase`, conexão por fase, manifesto com estados
e recuperação, fase de ontologia descritiva (Guia de implementação, B2/B3).

O modelo é simulado; compilador, mesclagem, validação e gravação são reais.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synesis_coder import manifest as mf
from synesis_coder import phase_run
from synesis_coder.modes import phase_mode

TEMPLATE = """\
ITEM FIELDS
    REQUIRED trecho, codigo
END ITEM FIELDS

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD

FIELD codigo TYPE CODE
    SCOPE ITEM
END FIELD

ONTOLOGY FIELDS
    REQUIRED definicao
    OPTIONAL tipo_de, exemplos
END ONTOLOGY FIELDS

FIELD definicao TYPE TEXT
    SCOPE ONTOLOGY
END FIELD

FIELD tipo_de TYPE CODE
    SCOPE ONTOLOGY
END FIELD

FIELD exemplos TYPE TEXT
    SCOPE ONTOLOGY
END FIELD
"""

BIB = "@article{a1, title={T}, author={A}, year={2024}}\n"

ANNOTATIONS = """\
SOURCE @a1
END SOURCE

ITEM @a1
    trecho: Mensagens com link falso.
    codigo: smishing, vishing, phishing, golpe_pix
END ITEM
"""

FILHOS = """\
# Tipos de golpe — curado à mão
ONTOLOGY smishing
    definicao: Antiga.
    tipo_de: phishing
    exemplos: Mensagem com link.
END ONTOLOGY

ONTOLOGY vishing
    exemplos: Ligação falsa.
END ONTOLOGY
"""

PAIS = """\
ONTOLOGY phishing
    exemplos: E-mail falso.
END ONTOLOGY
"""

PHASES = """
PHASE ONTOLOGY definicoes
    FIELDS definicao, exemplos
    INPUT trecho
    OUTPUT "filhos.syno"
END PHASE
"""


def _project(root: Path, phases: str = PHASES) -> Path:
    for name, text in (("t.synt", TEMPLATE), ("r.bib", BIB), ("a.syn", ANNOTATIONS),
                       ("filhos.syno", FILHOS), ("pais.syno", PAIS)):
        (root / name).write_bytes(text.encode("utf-8"))
    synp = root / "p.synp"
    synp.write_bytes((
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
        'INCLUDE ANNOTATIONS "a.syn"\nINCLUDE ONTOLOGY "filhos.syno"\n'
        'INCLUDE ONTOLOGY "pais.syno"\n' + phases + "END PROJECT\n"
    ).encode("utf-8"))
    return synp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("SYNESIS_CODER_PHASE_") or key in ("SYNESIS_CODER_BACKEND", "SYNESIS_CODER_MODEL"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-global")


class Model:
    """Modelo simulado: conta chamadas e devolve um bloco por código."""

    def __init__(self, generated=None, fail=False):
        self.calls: list[str] = []
        self.client_kwargs: dict = {}
        self.generated = generated or (lambda code: (
            f"ONTOLOGY {code}\n    definicao: Gerada para {code}.\n"
            f"    exemplos: Exemplo de {code}.\nEND ONTOLOGY"))
        self.fail = fail

    def run(self, synp: Path, phase: str = "definicoes", **kwargs):
        async def fake_generate(code, ctx, semantic_ctx, llm_client):
            if self.fail:
                raise AssertionError(f"o modelo não devia ser chamado ({code})")
            self.calls.append(code)
            return self.generated(code)

        def make_client(**kw):
            self.client_kwargs = kw
            client = MagicMock()
            client.usage.summary_line.return_value = "tokens 0"
            client.fix_async = AsyncMock(side_effect=lambda text, *a, **k: text)
            return client

        with patch.object(phase_mode, "_generate_ontology_syno", fake_generate), \
             patch.object(phase_run, "LLMClient", side_effect=make_client), \
             patch.object(phase_mode, "warn_schema_fallbacks", MagicMock()):
            return phase_mode.run_phase(synp, phase, concurrent=1, **kwargs)


def _manifest(root: Path) -> dict:
    return json.loads((root / "filhos.definicoes.manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ plano / lista

class TestPlan:
    def test_list_shows_phase_fields_output_and_connection(self, tmp_path):
        text = phase_mode.list_phases(_project(tmp_path))
        assert "PHASE ONTOLOGY definicoes" in text
        assert "FIELDS  definicao, exemplos" in text
        assert "OUTPUT  filhos.syno" in text
        assert "global" in text

    def test_plan_with_compiler_error_is_refused(self, tmp_path):
        synp = _project(tmp_path, PHASES.replace("INPUT trecho", "INPUT inexistente"))
        with pytest.raises(ValueError, match="SYNESIS_E136"):
            Model(fail=True).run(synp)

    def test_annotations_phase_is_not_run_yet(self, tmp_path):
        synp = _project(tmp_path, PHASES + "\nPHASE ANNOTATIONS extracao\n    FIELDS trecho, codigo\nEND PHASE\n")
        with pytest.raises(ValueError, match="B6"):
            Model(fail=True).run(synp, "extracao")

    def test_code_field_without_role_is_not_descriptive(self, tmp_path):
        synp = _project(tmp_path, PHASES.replace("FIELDS definicao, exemplos", "FIELDS definicao, exemplos, tipo_de"))
        with pytest.raises(ValueError, match="is not a descriptive field"):
            Model(fail=True).run(synp)


# ----------------------------------------------------------------- conexão

class TestConnection:
    def test_phase_without_variables_uses_global(self, tmp_path):
        model = Model()
        model.run(_project(tmp_path))
        assert model.client_kwargs == {"model": None, "backend": None, "api_url": None, "api_key": None}

    def test_phase_variables_reach_the_client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_PHASE_DEFINICOES_MODEL", "modelo-da-fase")
        monkeypatch.setenv("SYNESIS_CODER_PHASE_DEFINICOES_API_KEY", "sk-segredo-da-fase")
        model = Model()
        model.run(_project(tmp_path))
        assert model.client_kwargs["model"] == "modelo-da-fase"
        assert model.client_kwargs["api_key"] == "sk-segredo-da-fase"
        raw = (tmp_path / "filhos.definicoes.manifest.json").read_text(encoding="utf-8")
        assert "modelo-da-fase" in raw
        assert "sk-segredo-da-fase" not in raw and "sk-test-global" not in raw

    def test_bad_connection_fails_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNESIS_CODER_PHASE_DEFINICOES_BACKEND", "inexistente")
        with pytest.raises(EnvironmentError, match="inexistente"):
            Model(fail=True).run(_project(tmp_path))

    def test_missing_key_fails_before_any_call(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        with pytest.raises(EnvironmentError, match="SYNESIS_CODER_PHASE_DEFINICOES_API_KEY"):
            Model(fail=True).run(_project(tmp_path))


# ------------------------------------------------------------- fase descritiva

class TestDescriptivePhase:
    def test_fills_only_empty_fields_and_creates_new_blocks(self, tmp_path):
        synp = _project(tmp_path)
        model = Model()
        model.run(synp)
        # smishing completo: nem chamado; phishing está em outro .syno: pendente
        assert sorted(model.calls) == ["golpe_pix", "vishing"]
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        assert "ONTOLOGY vishing\n    exemplos: Ligação falsa.\n    definicao: Gerada para vishing.\nEND ONTOLOGY" in text
        assert "ONTOLOGY golpe_pix\n" in text
        assert (tmp_path / "pais.syno").read_bytes() == PAIS.encode("utf-8")
        assert (tmp_path / "a.syn").read_bytes() == ANNOTATIONS.encode("utf-8")
        data = _manifest(tmp_path)
        assert data["status"] == "applied"
        assert data["proposals"]["phishing"]["state"] == "pending"
        assert data["proposals"]["phishing"]["file"] == "pais.syno"
        assert data["proposals"]["vishing"]["state"] == "applied"
        assert data["proposals"]["vishing"]["fields"] == ["definicao"]
        assert data["after"] == mf.sha_file(tmp_path / "filhos.syno")
        assert "pending_text" not in data

    def test_new_field_changes_only_its_lines(self, tmp_path):
        synp = _project(tmp_path)
        Model().run(synp)
        before = FILHOS.splitlines()
        after = (tmp_path / "filhos.syno").read_text(encoding="utf-8").splitlines()
        import difflib
        removed = [l for l in difflib.ndiff(before, after) if l.startswith("- ")]
        assert removed == []

    def test_repeated_run_sees_no_change(self, tmp_path):
        synp = _project(tmp_path)
        Model().run(synp)
        snapshot = (tmp_path / "filhos.syno").read_bytes()
        report = Model(fail=True).run(synp)
        assert "nothing to do" in report
        assert (tmp_path / "filhos.syno").read_bytes() == snapshot

    def test_input_change_marks_obsolete_without_overwriting(self, tmp_path):
        synp = _project(tmp_path)
        Model().run(synp)
        (tmp_path / "a.syn").write_bytes(ANNOTATIONS.replace("link falso", "link adulterado").encode("utf-8"))
        report = Model(fail=True).run(synp)
        assert "obsolete" in report
        assert _manifest(tmp_path)["proposals"]["vishing"]["state"] == "obsolete"
        # com --overwrite, os campos da fase são regenerados
        model = Model(generated=lambda c: f"ONTOLOGY {c}\n    definicao: Nova {c}.\n    exemplos: Novo.\nEND ONTOLOGY")
        model.run(synp, overwrite=True)
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        assert "definicao: Nova smishing." in text and "tipo_de: phishing" in text

    def test_dry_run_writes_nothing(self, tmp_path):
        synp = _project(tmp_path)
        report = Model().run(synp, dry_run=True)
        assert "+    definicao: Gerada para vishing." in report
        assert (tmp_path / "filhos.syno").read_bytes() == FILHOS.encode("utf-8")
        assert not (tmp_path / "filhos.definicoes.manifest.json").exists()

    def test_output_switches_destination(self, tmp_path):
        synp = _project(tmp_path)
        model = Model()
        model.run(synp, output_path=tmp_path / "pais.syno")
        assert "phishing" in model.calls
        assert "definicao: Gerada para phishing." in (tmp_path / "pais.syno").read_text(encoding="utf-8")
        assert (tmp_path / "pais.definicoes.manifest.json").exists()

    def test_rejected_is_not_retried_without_flag(self, tmp_path):
        synp = _project(tmp_path)
        bad = Model(generated=lambda c: f"ONTOLOGY {c}\n    definicao: x\n    inexistente: y\nEND ONTOLOGY")
        bad.run(synp)
        # bloco existente: o campo estranho é descartado na mesclagem (só os da fase);
        # bloco novo é validado inteiro e cai
        assert _manifest(tmp_path)["proposals"]["vishing"]["state"] == "applied"
        assert _manifest(tmp_path)["proposals"]["golpe_pix"]["state"] == "rejected"
        (tmp_path / "pais.syno").write_bytes((PAIS + "\n").encode("utf-8"))  # outra entrada muda
        Model(fail=True).run(synp)
        model = Model()
        model.run(synp, retry_rejected=True)
        assert model.calls == ["golpe_pix"]

    def test_concurrent_edit_aborts(self, tmp_path):
        synp = _project(tmp_path)
        dest = tmp_path / "filhos.syno"

        def generated(code):
            dest.write_bytes((FILHOS + "\n# editado\n").encode("utf-8"))
            return f"ONTOLOGY {code}\n    definicao: G.\nEND ONTOLOGY"

        with pytest.raises(RuntimeError, match="changed during the run"):
            Model(generated=generated).run(synp)
        assert dest.read_text(encoding="utf-8").endswith("# editado\n")


    def test_other_input_changed_during_the_run_aborts(self, tmp_path):
        """R3 (avaliação de 2026-11-17): template mudado no meio da execução."""
        synp = _project(tmp_path)
        dest = tmp_path / "filhos.syno"
        before = dest.read_bytes()
        template = tmp_path / "t.synt"

        def generated(code):
            template.write_text(template.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            return f"ONTOLOGY {code}\n    definicao: G.\n    exemplos: E.\nEND ONTOLOGY"

        with pytest.raises(RuntimeError, match=r"inputs changed during the run \(t\.synt\)"):
            Model(generated=generated).run(synp)
        assert dest.read_bytes() == before
        assert _manifest(tmp_path)["status"] == "conflict"


# ---------------------------------------------------------------- recuperação

class TestRecovery:
    def test_killed_before_swap_is_interrupted_and_resumed_without_model(self, tmp_path):
        synp = _project(tmp_path)
        with patch.object(phase_run, "safe_write_output", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                Model().run(synp)
        assert _manifest(tmp_path)["status"] == "applying"
        assert (tmp_path / "filhos.syno").read_bytes() == FILHOS.encode("utf-8")
        report = Model(fail=True).run(synp)
        assert "resumed without a new model call" in report
        assert "definicao: Gerada para vishing." in (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        data = _manifest(tmp_path)
        assert data["status"] == "applied" and data["proposals"]["vishing"]["state"] == "applied"

    def test_killed_after_swap_is_recognized_as_applied(self, tmp_path):
        synp = _project(tmp_path)
        real_save = mf.Manifest.save
        calls = {"n": 0}

        def save_then_die(self):
            calls["n"] += 1
            if calls["n"] == 2:  # a gravação final do manifesto morre
                raise KeyboardInterrupt
            real_save(self)

        with patch.object(mf.Manifest, "save", save_then_die):
            with pytest.raises(KeyboardInterrupt):
                Model().run(synp)
        assert _manifest(tmp_path)["status"] == "applying"
        report = Model(fail=True).run(synp)
        assert "nothing to do" in report
        data = _manifest(tmp_path)
        assert data["status"] == "applied" and data["proposals"]["vishing"]["state"] == "applied"

    def test_foreign_edit_after_interruption_is_a_conflict(self, tmp_path):
        synp = _project(tmp_path)
        with patch.object(phase_run, "safe_write_output", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                Model().run(synp)
        (tmp_path / "filhos.syno").write_bytes((FILHOS + "# mão\n").encode("utf-8"))
        with pytest.raises(mf.ManifestConflict):
            Model(fail=True).run(synp)
