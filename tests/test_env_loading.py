"""D-6: settings come from the environment and the project .env, never from where
the coder is installed.

Before: `llm_client` ran `load_dotenv()` at import (searching upward from the
package folder) and the CLI always loaded the .env above the package. An
editable install pulled the repository's .env into every project; a regular
install could read any .env above site-packages.
"""

import os
import subprocess
import sys
from pathlib import Path

PROBE = (
    "import os, synesis_coder.cli, synesis_coder.llm_client;"
    "print(os.environ.get('SYNESIS_CODER_LANGUAGE', '<unset>'))"
)


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYNESIS_CODER_")}
    env.update(extra)
    return env


def _run(cwd: Path, **extra: str) -> str:
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=cwd, env=_clean_env(**extra),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_installation_env_is_not_read(tmp_path):
    # tmp_path has no .env; whatever .env sits next to the installed package
    # (the repository's, on an editable install) must not leak in.
    assert _run(tmp_path) == "<unset>"


def test_project_env_is_read(tmp_path):
    (tmp_path / ".env").write_text("SYNESIS_CODER_LANGUAGE=en\n", encoding="utf-8")
    assert _run(tmp_path) == "en"


def test_explicit_defaults_file_is_read(tmp_path):
    defaults = tmp_path / "defaults.env"
    defaults.write_text("SYNESIS_CODER_LANGUAGE=pt-BR\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    assert _run(project, SYNESIS_CODER_ENV_FILE=str(defaults)) == "pt-BR"


def test_project_env_wins_over_defaults_file(tmp_path):
    defaults = tmp_path / "defaults.env"
    defaults.write_text("SYNESIS_CODER_LANGUAGE=pt-BR\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SYNESIS_CODER_LANGUAGE=en\n", encoding="utf-8")
    assert _run(project, SYNESIS_CODER_ENV_FILE=str(defaults)) == "en"


def test_environment_variable_wins_over_everything(tmp_path):
    (tmp_path / ".env").write_text("SYNESIS_CODER_LANGUAGE=en\n", encoding="utf-8")
    assert _run(tmp_path, SYNESIS_CODER_LANGUAGE="es") == "es"
