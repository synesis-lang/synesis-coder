"""
phase_run.py - Preparação e publicação comuns às fases de ontologia (B2–B5)

Propósito:
    Cada fase de ontologia (descritiva, taxonomia, sinônimos) tem um algoritmo
    próprio, mas a mesma moldura: plano validado, conexão da fase, projeto
    carregado, destino único, manifesto com recuperação, conferência das
    entradas e publicação atômica. Esta moldura fica aqui, uma vez.

Componentes principais:
    - phase_kind: qual algoritmo os papéis dos FIELDS pedem
    - PhaseRun: o estado de uma execução
    - prepare: do .synp ao PhaseRun, ou a resposta antecipada (retomada,
      "nada a fazer")
    - PhaseRun.commit: validação já feita -> conferência, manifesto, gravação
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from synesis.ast.nodes import FieldType, PhaseNode

from synesis_coder import manifest as mf
from synesis_coder.llm_client import LLMClient
from synesis_coder.ontology_publish import (
    Destination,
    DestinationError,
    InputSnapshot,
    resolve_destination,
)
from synesis_coder.phases import (
    PhasePlan,
    effective_connection,
    load_phase_plan,
    phase_connection,
    phase_output,
    validate_connection,
)
from synesis_coder.project_loader import load_project
from synesis_coder.syno_merge import Block, existing_blocks
from synesis_coder.synr_io import safe_write_output

logger = logging.getLogger(__name__)

DESCRIPTIVE = "descriptive"
TAXONOMY = "taxonomy"
SYNONYMS = "synonyms"

LABEL_WRITABLE = ("LABEL ALTERNATIVE", "LABEL HIDDEN")


def coder_version() -> str:
    try:
        return version("synesis-coder")
    except PackageNotFoundError:
        return "dev"


def role_of(ctx: dict, name: str) -> str:
    spec = ctx.get("ontology_fields", {}).get(name)
    return str(getattr(spec, "role", None) or "") if spec is not None else ""


def phase_kind(phase: PhaseNode, ctx: dict) -> str:
    """O algoritmo pelos papéis dos FIELDS (estudo §6.4).

    - algum campo `HIERARCHY *` -> taxonomia (pode levar um TEXT sem papel,
      para a justificativa, §6.6);
    - algum `LABEL ALTERNATIVE|HIDDEN` -> sinônimos;
    - nenhum dos dois -> descritiva.
    Misturar hierarquia e rótulos numa fase é erro: são unidades diferentes.
    """
    roles = {f: role_of(ctx, f) for f in phase.fields("FIELDS")}
    hierarchy = [f for f, r in roles.items() if r.startswith("HIERARCHY")]
    labels = [f for f, r in roles.items() if r.startswith("LABEL")]
    if hierarchy and labels:
        raise ValueError(
            f"`{phase.label}` mixes hierarchy ({', '.join(hierarchy)}) and labels "
            f"({', '.join(labels)}). Taxonomy and synonyms are separate phases."
        )
    if hierarchy:
        specs = ctx.get("ontology_fields", {})
        extra = [f for f in roles if f not in hierarchy
                 and not (roles[f] == "" and getattr(specs.get(f), "type", None) == FieldType.TEXT)]
        justification = [f for f in roles if f not in hierarchy and f not in extra]
        if extra or len(justification) > 1:
            extra = extra or justification
            raise ValueError(
                f"`{phase.label}`: a taxonomy phase produces HIERARCHY fields and, at most, "
                f"one TEXT field without a role for the justification; {', '.join(extra)} does not fit."
            )
        return TAXONOMY
    if labels:
        wrong = [f for f in labels if roles[f] not in LABEL_WRITABLE]
        if wrong or len(labels) != len(roles):
            raise ValueError(
                f"`{phase.label}`: a synonyms phase produces only LABEL ALTERNATIVE "
                "or LABEL HIDDEN fields (LABEL PREFERRED is the block name or a manual decision)."
            )
        return SYNONYMS
    return DESCRIPTIVE


@dataclass
class PhaseRun:
    """Estado de uma execução de fase, do preparo à publicação."""

    project_path: Path
    plan: PhasePlan
    phase: PhaseNode
    kind: str
    conn: Dict[str, Optional[str]]
    ctx: dict
    destination: Destination
    manifest: mf.Manifest
    snapshot: InputSnapshot
    inputs: Dict[str, Optional[str]]
    config: Dict[str, Any]
    original: str
    started: str
    dry_run: bool = False
    backup: bool = False
    overwrite: bool = False
    retry_rejected: bool = False
    concurrent: int = 5
    options: Dict[str, Any] = field(default_factory=dict)
    _client: Optional[LLMClient] = None

    @property
    def dest(self) -> Path:
        return self.destination.path

    def client(self) -> LLMClient:
        if self._client is None:
            self._client = LLMClient(model=self.conn["model"], backend=self.conn["backend"],
                                     api_url=self.conn["api_url"], api_key=self.conn["api_key"])
        return self._client

    @property
    def client_used(self) -> Optional[LLMClient]:
        return self._client

    def dest_blocks(self) -> Dict[str, Block]:
        return existing_blocks(self.original)

    def other_texts(self) -> Dict[Path, str]:
        out: Dict[Path, str] = {}
        for path in self.destination.ontology_files.values():
            try:
                out[path] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return out

    def other_blocks(self) -> Dict[str, Tuple[Path, Block]]:
        """Conceito -> (arquivo, bloco), nos .syno do projeto fora do destino."""
        where: Dict[str, Tuple[Path, Block]] = {}
        for path, text in self.other_texts().items():
            for key, block in existing_blocks(text).items():
                where.setdefault(key, (path, block))
        return where

    # ------------------------------------------------------------ publicação

    def finish_without_change(self, proposals: Dict[str, Dict[str, Any]], reason: str,
                              notes: Optional[Dict[str, Any]] = None) -> None:
        """Registra uma execução que não altera o destino (salvo em --dry-run)."""
        if self.dry_run:
            return
        m = self.manifest
        m.status, m.stop_reason = mf.APPLIED, reason
        m.started_at, m.finished_at = self.started, mf.now()
        m.config, m.inputs = mf.without_secrets(self.config), self.inputs
        m.before = m.after = mf.sha_file(self.dest)
        m.proposals, m.pending_text = proposals, None
        if notes is not None:
            m.notes = notes
        m.save()

    def commit(
        self,
        final: str,
        accepted: List[str],
        rejected: Dict[str, List[str]],
        proposals: Dict[str, Dict[str, Any]],
        revalidate: Callable[[Set[str]], Tuple[str, Dict[str, List[str]]]],
        report: Callable[[Dict[str, Dict[str, Any]]], str],
        *,
        stop_reason: str = "completed",
        notes: Optional[Dict[str, Any]] = None,
        proposal_keys: Optional[Callable[[str], List[str]]] = None,
    ) -> str:
        """Confere as entradas, grava (ou mostra o diff) e registra no manifesto.

        `accepted`/`rejected` são as identidades da validação do conjunto;
        `proposal_keys(identidade)` diz quais propostas cada uma carrega (padrão:
        a própria identidade).

        Qualquer entrada que mudou durante a execução — o destino ou outra —
        aborta sem gravar (R3 da avaliação de 2026-11-17). A geração e a
        validação usaram o contexto lido no início (template, anotações,
        bibliografia); revalidar com esse contexto afirmaria uma garantia que não
        existe, e recompilar com o novo não atualiza a fundamentação do que foi
        gerado. `revalidate` fica na assinatura por compatibilidade e não é usado.
        """
        keys_of = proposal_keys or (lambda k: [k])
        dest_changed, others_changed = self.snapshot.changed()
        m = self.manifest
        if dest_changed:
            if not self.dry_run:
                m.status, m.stop_reason = mf.CONFLICT, "destination changed during the run"
                m.started_at, m.finished_at = self.started, mf.now()
                m.save()
            raise RuntimeError(f"{self.dest} was changed during the run; nothing was written. Run again.")
        if others_changed:
            names = ", ".join(sorted(p.name for p in others_changed))
            if not self.dry_run:
                m.status, m.stop_reason = mf.CONFLICT, f"inputs changed during the run: {names}"
                m.started_at, m.finished_at = self.started, mf.now()
                m.save()
            raise RuntimeError(
                f"Project inputs changed during the run ({names}); nothing was written. "
                "The proposals were generated and validated against the previous version. Run again."
            )
        for key, messages in rejected.items():
            for pk in keys_of(key):
                if pk in proposals:
                    proposals[pk].update(state=mf.REJECTED, reason="; ".join(messages)[:500])
        if final and not final.endswith(("\n", "\r\n")):
            final += "\r\n" if "\r\n" in self.original else "\n"

        if self.dry_run:
            diff = "".join(difflib.unified_diff(
                self.original.splitlines(keepends=True), final.splitlines(keepends=True),
                fromfile=self.dest.name, tofile=f"{self.dest.name} (proposto)",
            ))
            return report(proposals) + ("\n" + diff if diff else "")

        applied = [pk for k in accepted for pk in keys_of(k) if pk in proposals]
        m.started_at, m.finished_at, m.stop_reason = self.started, "", stop_reason
        m.config, m.inputs = mf.without_secrets(self.config), self.inputs
        m.before = self.snapshot.hashes.get(self.dest)
        m.proposals = proposals
        if notes is not None:
            m.notes = notes
        m.set_proposals(mf.APPLYING, applied)
        for pk in applied:
            m.proposals[pk]["run"] = self.started
        if final == self.original:
            m.set_proposals(mf.APPLIED, applied)
            m.status, m.after, m.finished_at = mf.APPLIED, m.before, mf.now()
            m.save()
        else:
            publish(m, self.dest, final, backup=self.backup)
        return report(m.proposals)


def publish(m: mf.Manifest, dest: Path, text: str, *, backup: bool) -> None:
    """Registro "aplicando" (atômico) -> troca do .syno -> registro "aplicada"."""
    m.status = mf.APPLYING
    m.after = mf.sha_text(text)
    m.pending_text = text
    m.save()
    dest.parent.mkdir(parents=True, exist_ok=True)
    safe_write_output(dest, text, overwrite=True, backup=backup, newline="")
    applying = [c for c, r in m.proposals.items() if r.get("state") == mf.APPLYING]
    m.set_proposals(mf.APPLIED, applying)
    m.status = mf.APPLIED
    m.pending_text = None
    m.finished_at = mf.now()
    m.save()


def _check_plan(plan: PhasePlan, phase: PhaseNode) -> None:
    if plan.errors:
        detail = "\n".join(f"  {d.CODE}: {d.to_cli_line()}" for d in plan.errors)
        raise ValueError(f"The phase plan has errors; fix the .synp before running:\n{detail}")
    if phase.scope != "ONTOLOGY":
        raise ValueError(
            f"`{phase.label}`: PHASE ANNOTATIONS is not run by the coder yet "
            "(stage B6). Use the abstract/document modes."
        )


def prepare(
    project_path: Path,
    phase_name: str,
    *,
    algorithms: Dict[str, str],
    output_path: Optional[Path] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    retry_rejected: bool = False,
    concurrent: int = 5,
    model: Optional[str] = None,
    backup: bool = False,
    options: Optional[Dict[str, Any]] = None,
    extra_config: Optional[Callable[[str, PhaseNode, dict], Dict[str, Any]]] = None,
) -> Union[PhaseRun, str]:
    """Do .synp à execução preparada; ou a resposta, quando não há o que gerar.

    `algorithms` mapeia o tipo de fase à versão do algoritmo (entra na
    configuração: mudar de algoritmo não passa por "mesma execução").
    """
    project_path = Path(project_path)
    plan = load_phase_plan(project_path)
    phase = plan.find(phase_name)
    _check_plan(plan, phase)

    conn = phase_connection(phase, model)
    validate_connection(phase, conn)

    ctx = load_project(project_path, load_annotations=True, load_ontology=False,
                       tolerate_annotation_errors=True)
    if not ctx["has_ontology_scope"]:
        raise ValueError("The template defines no ONTOLOGY fields.")
    kind = phase_kind(phase, ctx)

    target = Path(output_path).resolve() if output_path else phase_output(plan, phase)
    try:
        destination = resolve_destination(project_path, target)
    except DestinationError as exc:
        raise ValueError(str(exc)) from exc
    dest = destination.path

    m = mf.Manifest.load(mf.manifest_path(dest, phase.name), phase.name, dest)
    recovered = mf.recover(m, dest)

    snapshot = InputSnapshot.take(project_path, destination)
    inputs = {str(p): h for p, h in snapshot.hashes.items()}
    opts = dict(options or {})
    config: Dict[str, Any] = {
        "phase": phase.label,
        "kind": kind,
        "fields": phase.fields("FIELDS"),
        "input": phase.fields("INPUT"),
        "rules": phase.fields("RULES"),
        "overwrite": overwrite,
        "algorithm": algorithms.get(kind, kind),
        "coder": coder_version(),
        **effective_connection(phase, conn),
    }
    if opts:
        config["policies"] = {k: v for k, v in sorted(opts.items())}
    if extra_config is not None:
        config.update(extra_config(kind, phase, ctx))

    others_now = {k: v for k, v in inputs.items() if k != str(dest)}
    others_then = {k: v for k, v in m.inputs.items() if k != str(dest)}

    # Interrompida antes da troca, com tudo igual: publica o texto guardado.
    if (m.status == mf.INTERRUPTED and m.pending_text is not None and not dry_run
            and mf.sha_file(dest) == m.before and others_now == others_then
            and mf.without_secrets(config) == m.config):
        interrupted = [c for c, r in m.proposals.items() if r.get("state") == mf.INTERRUPTED]
        m.set_proposals(mf.APPLYING, interrupted)
        publish(m, dest, m.pending_text, backup=backup)
        return (f"{phase.label}: interrupted run resumed without a new model call; "
                f"{len(interrupted)} proposal(s) applied in {dest.name}.\n")

    # Mesmas entradas, mesma configuração, destino = resultado anterior.
    retry = retry_rejected and any(r.get("state") == mf.REJECTED for r in m.proposals.values())
    if not retry and m.same_run(inputs, config, mf.sha_file(dest)):
        note = " (record completed on recovery)" if recovered == mf.APPLIED else ""
        return f"{phase.label}: nothing to do{note}; inputs and configuration equal to the last run.\n"

    original = ""
    if dest.is_file():
        with open(dest, encoding="utf-8", newline="") as fh:
            original = fh.read()
    return PhaseRun(
        project_path=project_path, plan=plan, phase=phase, kind=kind, conn=conn, ctx=ctx,
        destination=destination, manifest=m, snapshot=snapshot, inputs=inputs, config=config,
        original=original, started=mf.now(), dry_run=dry_run, backup=backup,
        overwrite=overwrite, retry_rejected=retry_rejected, concurrent=concurrent, options=opts,
    )
