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


class TestPromptOnlyContract:
    """`--prompt-only` grava um .md e dispensa as opções de saída de produção."""

    def test_flag_documented_in_main_help(self):
        result = _run("--help")
        assert result.exit_code == 0
        assert "--prompt-only" in result.output

    def test_offered_by_the_four_generator_modes(self):
        for cmd in ("item", "abstract", "document", "ontology"):
            result = _run(cmd, "--help")
            assert result.exit_code == 0, cmd
            assert "--prompt-only" in result.output, cmd

    def test_output_required_without_prompt_only(self):
        """A exigência de destino permanece fora do modo de inspeção."""
        for cmd, opt in (("document", "--output"), ("ontology", "--output"),
                         ("abstract", "--output-dir")):
            result = _run(cmd, "--help")
            assert opt in result.output, cmd

    def test_emit_prompt_default_name_derives_from_project_and_mode(self, tmp_path):
        from synesis_coder.cli import _emit_prompt

        project = tmp_path / "face85.synp"
        project.write_text("x", encoding="utf-8")

        _emit_prompt("# dump", project, "abstract", None)

        assert (tmp_path / "face85_abstract_prompt.md").read_text(
            encoding="utf-8"
        ) == "# dump"

    def test_emit_prompt_honours_explicit_output(self, tmp_path):
        from synesis_coder.cli import _emit_prompt

        project = tmp_path / "p.synp"
        project.write_text("x", encoding="utf-8")
        dest = tmp_path / "nested" / "custom.md"

        _emit_prompt("# dump", project, "item", dest)

        assert dest.read_text(encoding="utf-8") == "# dump"

    def test_emit_prompt_reports_destination_on_stderr(self, tmp_path, capsys):
        """stdout fica livre para redirecionamento; o caminho vai a stderr.

        Regressão: usar o logger em nível DEST (22) fazia `-q` engolir a linha
        — e `-q` é o que se usa para calar o banner neste modo.
        """
        from synesis_coder.cli import _emit_prompt

        project = tmp_path / "p.synp"
        project.write_text("x", encoding="utf-8")

        _emit_prompt("# dump", project, "ontology", None)

        captured = capsys.readouterr()
        assert "p_ontology_prompt.md" in captured.err
        assert captured.out == ""
