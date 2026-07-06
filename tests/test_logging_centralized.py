"""Teste-guarda: logging é configurado só pela CLI, nunca pelos modos.

Os modos não devem chamar logging.basicConfig — isso derrubaria a configuração
central da CLI (_configure_logging), ignorando -v/-q e reativando o ruído de
loggers de terceiros. Este teste falha se qualquer modo reintroduzir basicConfig.
"""

from __future__ import annotations

from pathlib import Path

MODES_DIR = Path(__file__).parent.parent / "synesis_coder" / "modes"


def test_no_mode_calls_basicconfig():
    offenders = []
    for py in sorted(MODES_DIR.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        if "basicConfig" in text:
            offenders.append(py.name)
    assert not offenders, (
        "Modos não devem chamar logging.basicConfig (logging é centralizado na "
        f"CLI via _configure_logging). Ofensores: {offenders}"
    )
