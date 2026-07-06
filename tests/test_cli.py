"""CLI contract tests for synesis-coder.

Locks --help / --version output as a regression contract. Uses CliRunner so
ANSI colour is off (_tty() returns False) and output is plain and stable.
"""

from __future__ import annotations

from importlib.metadata import version

from click.testing import CliRunner

from synesis_coder.cli import main


def _run(*args: str):
    runner = CliRunner()
    return runner.invoke(main, list(args))


def test_version_reports_package_version():
    result = _run("--version")
    assert result.exit_code == 0
    assert version("synesis-coder") in result.output


def test_help_shows_title_and_usage():
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    assert "SYNESIS CODER" in out
    assert "Usage:" in out
    assert "Commands:" in out


def test_help_lists_ingestion_commands():
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    for cmd in ("item", "abstract", "document"):
        assert cmd in out


def test_help_lists_structuring_commands():
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    for cmd in ("ontology", "suggest", "finetune"):
        assert cmd in out


def test_help_lists_act_pipeline_commands():
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    for cmd in ("critique", "normalize", "incorporate"):
        assert cmd in out


def test_item_subcommand_help():
    result = _run("item", "--help")
    assert result.exit_code == 0
    out = result.output
    assert "--project" in out or "project" in out.lower()


def test_global_model_option_visible():
    result = _run("--help")
    assert result.exit_code == 0
    assert "--model" in result.output
