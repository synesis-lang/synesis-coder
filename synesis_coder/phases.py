"""
phases.py - Plano de fases do .synp e conexão de cada fase (etapa B2)

Propósito:
    Ler os blocos PHASE pelo parser do synesis (não por expressão regular) e
    resolver, por fase, modelo e conexão a partir do ambiente:

        SYNESIS_CODER_PHASE_<NOME>_MODEL
        SYNESIS_CODER_PHASE_<NOME>_BACKEND
        SYNESIS_CODER_PHASE_<NOME>_API_URL
        SYNESIS_CODER_PHASE_<NOME>_API_KEY

    <NOME> é o nome da fase em maiúsculas, com o que não for letra ou dígito
    trocado por `_` (PhaseNode.env_key). Uma fase sem variáveis usa a
    configuração global. A conexão é validada antes da primeira chamada.

Notas:
    Os diagnósticos de PHASE (E133-E144) são os do compilador, não uma cópia:
    o coder recusa rodar um plano que o compilador acusa como erro.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from synesis.ast.nodes import PhaseNode
from synesis.ast.results import ErrorSeverity
from synesis.compiler import SynesisCompiler
from synesis.semantic.project_blocks import phase_errors

_BACKENDS = ("anthropic", "openai")


@dataclass
class PhasePlan:
    """Fases do projeto, na ordem do arquivo, e os diagnósticos do plano."""

    project_path: Path
    phases: List[PhaseNode]
    errors: List[Any]  # severidade de erro: impedem a execução
    warnings: List[Any]

    def find(self, name: str) -> PhaseNode:
        for phase in self.phases:
            if phase.name == name:
                return phase
        folded = [p for p in self.phases if p.name.lower() == name.lower()]
        if len(folded) == 1:
            return folded[0]
        names = ", ".join(p.name for p in self.phases) or "none"
        raise ValueError(f"Phase `{name}` not found in {self.project_path.name}. Phases: {names}.")


def load_phase_plan(project_path: Path) -> PhasePlan:
    """Lê os blocos PHASE e os valida contra o template, como o compilador."""
    project_path = Path(project_path).resolve()
    compiler = SynesisCompiler(project_path)
    project, _result = compiler.parse_project()
    try:
        template = compiler.load_template(project)
    except Exception:  # noqa: BLE001 — sem template, a validação de campos fica de fora
        template = None
    diagnostics = phase_errors(project, template)
    errors = [d for d in diagnostics if getattr(d, "severity", None) == ErrorSeverity.ERROR]
    warnings = [d for d in diagnostics if d not in errors]
    return PhasePlan(project_path=project_path, phases=list(project.phases),
                     errors=errors, warnings=warnings)


def phase_output(plan: PhasePlan, phase: PhaseNode) -> Optional[Path]:
    """`OUTPUT` da fase, relativo ao .synp; None quando a fase não o declara."""
    output = phase.prop("OUTPUT")
    if not output:
        return None
    return Path(plan.project_path.parent / str(output).strip().strip('"')).resolve()


# ---------------------------------------------------------------------------
# Conexão por fase
# ---------------------------------------------------------------------------


def _env(phase: PhaseNode, axis: str) -> Optional[str]:
    return os.environ.get(f"SYNESIS_CODER_PHASE_{phase.env_key}_{axis}") or None


def phase_connection(phase: PhaseNode, model: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Modelo e conexão da fase: `--model` > variável da fase > configuração global.

    Valores None acionam, no LLMClient, o fallback global do ambiente.
    """
    return {
        "model": model or _env(phase, "MODEL"),
        "backend": _env(phase, "BACKEND"),
        "api_url": _env(phase, "API_URL"),
        "api_key": _env(phase, "API_KEY"),
    }


def effective_connection(phase: PhaseNode, conn: Dict[str, Optional[str]]) -> Dict[str, str]:
    """A conexão que será usada, sem a chave; para o relatório e o manifesto."""
    backend = (conn.get("backend") or os.environ.get("SYNESIS_CODER_BACKEND") or "anthropic").lower()
    out = {
        "model": conn.get("model") or os.environ.get("SYNESIS_CODER_MODEL") or "claude-opus-4-6",
        "backend": backend,
        "source": "phase" if any(_env(phase, a) for a in ("MODEL", "BACKEND", "API_URL", "API_KEY"))
        else "global",
    }
    if backend == "openai":
        out["api_url"] = conn.get("api_url") or os.environ.get("SYNESIS_CODER_API_URL") or "http://localhost:11434"
    return out


def validate_connection(phase: PhaseNode, conn: Dict[str, Optional[str]]) -> None:
    """Falha antes da primeira chamada se a conexão da fase não pode funcionar."""
    prefix = f"SYNESIS_CODER_PHASE_{phase.env_key}_"
    backend = (conn.get("backend") or os.environ.get("SYNESIS_CODER_BACKEND") or "anthropic").lower()
    if backend not in _BACKENDS:
        raise EnvironmentError(
            f"Phase `{phase.name}`: unknown backend `{backend}` (use anthropic or openai)."
        )
    if backend == "anthropic" and not (conn.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")):
        raise EnvironmentError(
            f"Phase `{phase.name}`: no key for the anthropic backend. Set in the .env "
            f"`ANTHROPIC_API_KEY` (global) or `{prefix}API_KEY` (this phase only)."
        )
