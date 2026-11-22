"""D-3: fields without GUIDELINES are reported (and can be required).
D-4: coding modes accept an injected model client.
"""

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from synesis_coder import template_lint
from synesis_coder.cli import main
from synesis_coder.project_loader import load_project
from synesis_coder.token_usage import TokenUsage

TEMPLATE_WITH_GAP = (
    "ITEM FIELDS\n    REQUIRED trecho, code\nEND ITEM FIELDS\n"
    "FIELD trecho TYPE QUOTATION\n    SCOPE ITEM\n    GUIDELINES\n        Copy the words.\n"
    "    END GUIDELINES\nEND FIELD\n"
    "FIELD code TYPE CODE\n    SCOPE ITEM\n    DESCRIPTION Concepts in the passage\nEND FIELD\n"
    "ONTOLOGY FIELDS\n    REQUIRED definition\nEND ONTOLOGY FIELDS\n"
    "FIELD definition TYPE TEXT\n    SCOPE ONTOLOGY\n    GUIDELINES\n        Define it.\n"
    "    END GUIDELINES\nEND FIELD\n"
)


def _project(tmp_path: Path, template: str = TEMPLATE_WITH_GAP) -> Path:
    (tmp_path / "t.synt").write_text(template, encoding="utf-8")
    (tmp_path / "r.bib").write_text("@misc{a1,\n  title = {T}\n}\n", encoding="utf-8")
    (tmp_path / "anotacoes").mkdir()
    (tmp_path / "anotacoes" / "a1.syn").write_text(
        "SOURCE @a1\nEND SOURCE\n\nITEM @a1\n    trecho: Water arrived.\n    code: water_access\nEND ITEM\n",
        encoding="utf-8")
    (tmp_path / "ontologia").mkdir()
    synp = tmp_path / "p.synp"
    synp.write_text('PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
                    'INCLUDE ANNOTATIONS "anotacoes"\nINCLUDE ONTOLOGY "ontologia"\nEND PROJECT\n',
                    encoding="utf-8")
    return synp


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(template_lint.REQUIRE_GUIDELINES_ENV, raising=False)
    template_lint._already_warned.clear()


# --- D-3 -------------------------------------------------------------------

def test_fields_without_guidelines_are_listed(tmp_path):
    ctx = load_project(_project(tmp_path))
    missing = template_lint.fields_without_guidelines(ctx["field_specs"])
    assert missing == [("code", "CODE", "ITEM")]


def test_missing_guidelines_warn_by_default(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="synesis_coder.template_lint"):
        load_project(_project(tmp_path))
    assert "without GUIDELINES" in caplog.text and "code (CODE, ITEM)" in caplog.text


def test_missing_guidelines_fail_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv(template_lint.REQUIRE_GUIDELINES_ENV, "1")
    with pytest.raises(ValueError, match="without GUIDELINES"):
        load_project(_project(tmp_path))


def test_complete_template_passes_when_required(tmp_path, monkeypatch):
    complete = TEMPLATE_WITH_GAP.replace(
        "    DESCRIPTION Concepts in the passage\n",
        "    DESCRIPTION Concepts in the passage\n    GUIDELINES\n        One to three concepts.\n"
        "    END GUIDELINES\n")
    monkeypatch.setenv(template_lint.REQUIRE_GUIDELINES_ENV, "1")
    load_project(_project(tmp_path, complete))


def test_cli_require_guidelines_flag(tmp_path, monkeypatch):
    # The flag sets the variable in the process (one process per CLI call in real
    # use). Registering it with monkeypatch restores it after the test, so the
    # requirement does not leak into the rest of the suite.
    monkeypatch.setenv(template_lint.REQUIRE_GUIDELINES_ENV, "")
    synp = _project(tmp_path)
    r = CliRunner().invoke(main, ["--require-guidelines", "item", "--project", str(synp),
                                  "--bibref", "a1", "--text", "x", "--prompt-only"])
    assert r.exit_code != 0
    assert "without GUIDELINES" in r.output


# --- D-4 -------------------------------------------------------------------

class _StubClient:
    backend = "openai"
    model = "stub"
    recorder = None
    _api_url = ""

    def __init__(self):
        self.usage = TokenUsage()
        self.calls = 0

    def supports_json_schema(self):
        return True

    async def call_json_async(self, messages, schema, **kwargs):
        self.calls += 1
        return {"definition": "A stub definition long enough to be a valid text value."}

    async def call_async(self, *args, **kwargs):
        return ""

    async def fix_async(self, *args, **kwargs):
        return ""


def test_ontology_mode_uses_the_injected_client(tmp_path):
    from synesis_coder.modes.ontology_mode import process_ontology

    synp = _project(tmp_path, TEMPLATE_WITH_GAP)
    stub = _StubClient()
    target = tmp_path / "ontologia" / "ontologia.syno"
    pytest.importorskip("synesis.parser.lexer").is_blank_source  # needs synesis C-2
    process_ontology(synp, target, overwrite=True, client=stub)
    assert stub.calls >= 1
    assert "ONTOLOGY water_access" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("module, func", [
    ("document_mode", "process_document"), ("abstract_mode", "process_abstract"),
    ("ontology_mode", "process_ontology"), ("item_mode", "process_item"),
    ("dataset_mode", "process_dataset"),
])
def test_client_is_keyword_only_and_optional(module, func):
    import importlib
    import inspect

    param = inspect.signature(
        getattr(importlib.import_module(f"synesis_coder.modes.{module}"), func)).parameters["client"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is None
