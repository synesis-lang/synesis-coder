"""D-1: `ontology` creates a first .syno when the ontology folder is empty.

Needs synesis with correction C-2 (empty .syno compiles to no blocks). With an
older synesis, `validate_changes` failed loading the empty previous state.
"""

from pathlib import Path

import pytest

lexer = pytest.importorskip("synesis.parser.lexer")
if not hasattr(lexer, "is_blank_source"):
    pytest.skip("synesis without correction C-2", allow_module_level=True)

from synesis_coder.modes import ontology_mode  # noqa: E402
from synesis_coder.token_usage import TokenUsage  # noqa: E402


class _StubClient:
    backend = "openai"
    model = "stub"
    recorder = None
    _api_url = ""

    def __init__(self, *args, **kwargs):
        self.usage = TokenUsage()

    def supports_json_schema(self):
        return True

    async def call_json_async(self, messages, schema, **kwargs):
        return {"definition": "A stub definition long enough to be a valid text value."}

    async def call_async(self, *args, **kwargs):
        return ""

    async def fix_async(self, *args, **kwargs):
        return ""


def test_first_ontology_is_created(tmp_path: Path, monkeypatch):
    (tmp_path / "t.synt").write_text(
        "ITEM FIELDS\n    REQUIRED code\nEND ITEM FIELDS\n"
        "FIELD code TYPE CODE\n    SCOPE ITEM\n    GUIDELINES\n        One code.\n    END GUIDELINES\nEND FIELD\n"
        "ONTOLOGY FIELDS\n    REQUIRED definition\nEND ONTOLOGY FIELDS\n"
        "FIELD definition TYPE TEXT\n    SCOPE ONTOLOGY\n    GUIDELINES\n        Define it.\n"
        "    END GUIDELINES\nEND FIELD\n", encoding="utf-8")
    (tmp_path / "r.bib").write_text("@misc{a1,\n  title = {T}\n}\n", encoding="utf-8")
    (tmp_path / "anotacoes").mkdir()
    (tmp_path / "anotacoes" / "a1.syn").write_text(
        "ITEM @a1\n    code: water_access\nEND ITEM\n", encoding="utf-8")
    (tmp_path / "ontologia").mkdir()
    (tmp_path / "p.synp").write_text(
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
        'INCLUDE ANNOTATIONS "anotacoes"\nINCLUDE ONTOLOGY "ontologia"\nEND PROJECT\n',
        encoding="utf-8")
    monkeypatch.setattr(ontology_mode, "LLMClient", _StubClient)
    target = tmp_path / "ontologia" / "ontologia.syno"
    ontology_mode.process_ontology(tmp_path / "p.synp", target, overwrite=True)
    assert target.exists()
    assert "ONTOLOGY water_access" in target.read_text(encoding="utf-8")
