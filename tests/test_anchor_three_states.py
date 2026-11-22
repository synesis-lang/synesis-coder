"""D-2: anchoring reports every quotation in three states and never passes silently.

Before (0.15.0): a reference without abstract was skipped and the report said
"no problems"; only the first filled QUOTATION field was compared; only the
first line of a value was read.
"""

import json
from pathlib import Path

from click.testing import CliRunner

from synesis_coder.anchor_check import (
    FOUND,
    NOT_FOUND,
    UNVERIFIABLE,
    check_anchoring,
    check_quotations,
    summarize,
)
from synesis_coder.cli import main

BIB = (
    "@article{a1,\n  title = {T},\n  abstract = {Water arrived. The group grew again.}\n}\n"
    "@misc{e1,\n  title = {Interview without abstract}\n}\n"
)


def _item(ref: str, body: str) -> str:
    return f"ITEM @{ref}\n{body}END ITEM\n"


def test_second_quotation_is_checked():
    syn = _item("a1", "    trecho: Water arrived.\n    trecho2: Invented sentence.\n")
    results = check_quotations(syn, BIB, ["trecho", "trecho2"])
    assert [(r.field, r.status) for r in results] == [("trecho", FOUND), ("trecho2", NOT_FOUND)]
    assert check_anchoring(syn, BIB, fields=["trecho", "trecho2"])  # old API now sees it too


def test_reference_without_source_is_unverifiable_not_ok():
    syn = _item("e1", "    trecho: Anything.\n")
    results = check_quotations(syn, BIB, ["trecho"])
    assert results[0].status == UNVERIFIABLE
    s = summarize(results)
    assert s[UNVERIFIABLE] == 1 and s["verified"] is False


def test_source_dir_text_takes_precedence():
    syn = _item("e1", "    trecho: We carried buckets.\n")
    results = check_quotations(syn, BIB, ["trecho"], sources={"e1": "Back then we carried buckets."})
    assert results[0].status == FOUND and results[0].origin == "source-dir"


def test_repeated_field_and_continuation_lines_are_read():
    syn = _item("a1", "    trecho: Water\n        arrived.\n    trecho: Not in the source.\n")
    results = check_quotations(syn, BIB, ["trecho"])
    assert [(r.occurrence, r.excerpt, r.status) for r in results] == [
        (0, "Water arrived.", FOUND), (1, "Not in the source.", NOT_FOUND)]


def test_literal_mode_is_strict():
    syn = _item("a1", "    trecho: water arrived.\n")
    assert check_quotations(syn, BIB, ["trecho"])[0].status == FOUND  # normalized: case-insensitive
    assert check_quotations(syn, BIB, ["trecho"], literal=True)[0].status == NOT_FOUND


def test_no_quotation_is_not_verified():
    assert summarize([])["verified"] is False


def _project(tmp_path: Path) -> Path:
    (tmp_path / "t.synt").write_text(
        "SOURCE FIELDS\n    OPTIONAL description\nEND SOURCE FIELDS\n"
        "FIELD description TYPE TEXT\n    SCOPE SOURCE\nEND FIELD\n"
        "ITEM FIELDS\n    REQUIRED trecho\nEND ITEM FIELDS\n"
        "FIELD trecho TYPE QUOTATION\n    SCOPE ITEM\nEND FIELD\n", encoding="utf-8")
    (tmp_path / "r.bib").write_text(BIB, encoding="utf-8")
    (tmp_path / "p.synp").write_text(
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
        'INCLUDE ANNOTATIONS "a.syn"\nEND PROJECT\n', encoding="utf-8")
    syn = tmp_path / "a.syn"
    syn.write_text(_item("a1", "    trecho: Water arrived.\n")
                   + _item("e1", "    trecho: We carried buckets.\n"), encoding="utf-8")
    return syn


def test_cli_strict_fails_on_unverifiable_and_writes_json(tmp_path: Path):
    syn = _project(tmp_path)
    out = tmp_path / "anchoring.json"
    r = CliRunner().invoke(main, ["anchor", str(syn), "--project", str(tmp_path / "p.synp"),
                                  "--strict", "--json", str(out)])
    assert r.exit_code == 1, r.output
    assert "unverifiable" in r.output.lower() and "NOT verified" in r.output
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["summary"][UNVERIFIABLE] == 1 and report["summary"]["verified"] is False
    assert report["normalization"]


def test_cli_source_dir_makes_it_verified(tmp_path: Path):
    syn = _project(tmp_path)
    src = tmp_path / "fontes"
    src.mkdir()
    (src / "e1.txt").write_text("Back then we carried buckets.", encoding="utf-8")
    r = CliRunner().invoke(main, ["anchor", str(syn), "--project", str(tmp_path / "p.synp"),
                                  "--source-dir", str(src), "--strict"])
    assert r.exit_code == 0, r.output
    assert "Verified." in r.output
