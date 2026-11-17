"""Modo phase: executa uma PHASE ONTOLOGY do .synp (etapas B2–B5).

O algoritmo vem dos papéis dos FIELDS da fase (estudo §6.4; `phase_run.phase_kind`):

- **descritiva** (B3): campos sem papel estrutural (TEXT, TOPIC, NOTE...). O
  algoritmo do modo `ontology`, restrito aos campos da fase, gravado pela
  mesclagem por bloco (A0a):
  - bloco que já existe no destino: só os campos da fase que estão VAZIOS
    (todos os da fase com --overwrite); o resto do bloco fica como estava;
  - conceito do corpus sem bloco em nenhum .syno: bloco novo com os FIELDS da
    fase e os REQUIRED geráveis (a única exceção à regra de ouro, §6.2);
  - conceito com bloco em outro .syno do projeto: PENDENTE no manifesto.
  O contexto de cada conceito é filtrado pelo INPUT; o hash desse contexto
  identifica as entradas da proposta (mudou: obsoleta).
- **taxonomia** (B4): campos HIERARCHY; `modes/taxonomy_mode.py`.
- **sinônimos** (B5): campos LABEL ALTERNATIVE/HIDDEN; `modes/synonym_mode.py`.

A moldura comum (plano, conexão, destino, manifesto, recuperação, gravação)
está em `synesis_coder.phase_run`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from synesis.ast.nodes import FieldType, PhaseNode

from synesis_coder import manifest as mf
from synesis_coder.concept_index import concept_occurrences
from synesis_coder.llm_client import LLMClient
from synesis_coder.modes.ontology_mode import (
    _build_semantic_ctx,
    _generate_ontology_syno,
    _generation_ctx,
)
from synesis_coder.ontology_publish import validate_changes
from synesis_coder.phase_run import (
    DESCRIPTIVE,
    SYNONYMS,
    TAXONOMY,
    PhaseRun,
    prepare,
)
from synesis_coder.phases import (
    effective_connection,
    load_phase_plan,
    phase_connection,
    phase_output,
)
from synesis_coder.runtime_info import warn_schema_fallbacks
from synesis_coder.syno_merge import (
    Block,
    block_entries,
    concept_key,
    entries_by_field,
    merge_block,
    merge_into_syno,
)
from synesis_coder.validator import validate_ontology_entry_async
from synesis_coder.vocabulary import is_alias_block

logger = logging.getLogger(__name__)

# Versão do algoritmo de cada tipo de fase: entra na configuração do manifesto,
# para que uma mudança de algoritmo não passe por "mesma execução".
ALGORITHMS = {DESCRIPTIVE: "descriptive-1", TAXONOMY: "taxonomy-1", SYNONYMS: "synonyms-1"}
ALGORITHM = ALGORITHMS[DESCRIPTIVE]


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------


def list_phases(project_path: Path) -> str:
    """Tabela das fases do projeto: escopo, campos, destino, conexão, estado."""
    plan = load_phase_plan(project_path)
    if not plan.phases:
        return f"{plan.project_path.name}: no PHASE block."
    lines = [f"Phases of {plan.project_path.name} (in file order):", ""]
    for phase in plan.phases:
        conn = effective_connection(phase, phase_connection(phase))
        lines.append(f"PHASE {phase.scope} {phase.name}")
        lines.append(f"    FIELDS  {', '.join(phase.fields('FIELDS')) or '-'}")
        if phase.fields("INPUT"):
            lines.append(f"    INPUT   {', '.join(phase.fields('INPUT'))}")
        if phase.fields("RULES"):
            lines.append(f"    RULES   {', '.join(phase.fields('RULES'))}")
        if phase.scope == "ONTOLOGY":
            out = phase_output(plan, phase)
            lines.append(f"    OUTPUT  {out.name if out else '(single eligible .syno)'}")
            if out is not None:
                m = mf.Manifest.load(mf.manifest_path(out, phase.name), phase.name, out)
                if m.exists:
                    lines.append(f"    last    {m.status} at {m.finished_at or m.started_at}")
        lines.append(f"    model   {conn['model']} ({conn['backend']}, {conn['source']} config; "
                     f"variables SYNESIS_CODER_PHASE_{phase.env_key}_*)")
        if phase.scope == "ANNOTATIONS":
            lines.append("    (PHASE ANNOTATIONS is not run by the coder yet: stage B6)")
        lines.append("")
    for diag in plan.errors + plan.warnings:
        kind = "error" if diag in plan.errors else "warning"
        lines.append(f"{kind} {diag.CODE}: {diag.to_cli_line()}")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------


def run_phase(
    project_path: Path,
    phase_name: str,
    *,
    output_path: Optional[Path] = None,
    dry_run: bool = False,
    overwrite: bool = False,
    retry_rejected: bool = False,
    concurrent: int = 5,
    model: Optional[str] = None,
    backup: bool = False,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    """Executa uma PHASE ONTOLOGY e devolve o relatório.

    `options`: políticas e parâmetros do algoritmo (taxonomia: group_by,
    group_size, new_generations, within_topic, min_children, max_new,
    max_calls). Entram no manifesto.
    """
    return asyncio.run(_run_phase_async(
        Path(project_path), phase_name, output_path=output_path, dry_run=dry_run,
        overwrite=overwrite, retry_rejected=retry_rejected, concurrent=concurrent,
        model=model, backup=backup, options=options,
    ))


async def _run_phase_async(
    project_path: Path,
    phase_name: str,
    *,
    output_path: Optional[Path],
    dry_run: bool,
    overwrite: bool,
    retry_rejected: bool,
    concurrent: int,
    model: Optional[str],
    backup: bool,
    options: Optional[Dict[str, Any]],
) -> str:
    from synesis_coder.modes import synonym_mode, taxonomy_mode

    run = prepare(
        project_path, phase_name, algorithms=ALGORITHMS, output_path=output_path,
        dry_run=dry_run, overwrite=overwrite, retry_rejected=retry_rejected,
        concurrent=concurrent, model=model, backup=backup,
        options=_options_for(project_path, phase_name, options),
    )
    if isinstance(run, str):
        return run
    if run.kind == TAXONOMY:
        return await taxonomy_mode.run_taxonomy(run)
    if run.kind == SYNONYMS:
        return await synonym_mode.run_synonyms(run)
    return await _run_descriptive(run)


def _options_for(project_path: Path, phase_name: str, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Só as opções que valem (não None): elas entram na configuração do manifesto."""
    # `is`, não `in`: 0 == False em Python, e --new-generations 0 é uma escolha.
    return {k: v for k, v in (options or {}).items() if v is not None and v is not False}


# ---------------------------------------------------------------------------
# Fase descritiva (B3)
# ---------------------------------------------------------------------------


def _phase_fields(phase: PhaseNode, ctx: dict) -> Tuple[List[str], List[str]]:
    """(FIELDS que a fase descritiva produz, REQUIRED geráveis para blocos novos)."""
    generable = _generation_ctx(ctx)["ontology_fields"]
    fields = phase.fields("FIELDS")
    structural = [f for f in fields if f not in generable]
    if structural:
        raise ValueError(
            f"`{phase.label}`: {', '.join(structural)} is not a descriptive field (CODE or CHAIN "
            "without a role). A descriptive phase produces TEXT, TOPIC, NOTE and the like; hierarchy and "
            "labels have their own phases (HIERARCHY and LABEL roles)."
        )
    required = [r for r in ctx.get("required_ontology", []) if r in generable and r not in fields]
    return fields, required


def _phase_ctx(ctx: dict, names: List[str]) -> dict:
    """Contexto de geração só com os campos `names` (todos pedidos ao modelo)."""
    gen = dict(ctx)
    specs = ctx.get("ontology_fields", {})
    gen["ontology_fields"] = {n: specs[n] for n in names if n in specs}
    gen["required_ontology"] = list(gen["ontology_fields"])
    return gen


def _input_view(semantic: dict, phase: PhaseNode, item_fields: dict) -> dict:
    """Contexto do conceito restrito ao INPUT da fase (sem INPUT: o do modo ontology)."""
    inputs = set(phase.fields("INPUT"))
    view = dict(semantic)
    view["relations"] = sorted(tuple(r) for r in semantic.get("relations", []))
    if not inputs:
        return view
    chains = {n for n, s in item_fields.items() if s.type == FieldType.CHAIN}
    if not inputs & chains:
        view["relations"] = []
    examples = [{k: v for k, v in ex.items() if k in inputs} for ex in semantic.get("examples", [])]
    view["examples"] = [ex for ex in examples if ex]
    return view


def _input_hash(view: Any) -> str:
    payload = json.dumps(view, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _filled(block: Block) -> Set[str]:
    """Campos do bloco com valor (um `campo:` sem nada depois conta como vazio)."""
    out: Set[str] = set()
    for entry in block_entries(block)[1]:
        if entry.name is None:
            continue
        first = entry.lines[0].split(":", 1)[1] if ":" in entry.lines[0] else ""
        if first.strip() or any(line.strip() for line in entry.lines[1:]):
            out.add(entry.name)
    return out


def ensure_occurrences(ctx: dict) -> None:
    """Índice de ocorrências por conceito, uma vez por execução."""
    if "_concept_occurrences" in ctx:
        return
    result = ctx.get("result")
    if result is not None and result.linked_project is not None:
        ctx["_concept_occurrences"] = concept_occurrences(result.linked_project, ctx.get("item_fields") or {})


async def _generate_one(
    code: str,
    gen_ctx: dict,
    semantic: dict,
    existing: Optional[Block],
    wanted: Optional[Set[str]],
    client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> Tuple[str, str, bool, str]:
    """(código, bloco, válido?, motivo). Valida o bloco já mesclado, como o modo ontology."""
    async with semaphore:
        try:
            raw = await _generate_ontology_syno(code, gen_ctx, semantic, client)
        except Exception as exc:  # noqa: BLE001 — falha de um conceito não para a fase
            logger.error("LLM call failed for '%s': %s", code, exc)
            return code, "", False, f"model call failed: {exc}"
        candidate = raw
        generated = entries_by_field(raw)
        if existing is not None and generated:
            keep = {n: v for n, v in generated.items() if wanted is None or n in wanted}
            candidate = "".join(merge_block(existing, keep).lines)
        final, ok = await validate_ontology_entry_async(
            candidate, gen_ctx, client, ontology_key=f"ontology_{code}.syno",
        )
        return code, final, ok, "" if ok else "the block did not pass template validation"


async def _run_descriptive(run: PhaseRun) -> str:
    ctx, phase, m = run.ctx, run.phase, run.manifest
    fields, required_new = _phase_fields(phase, ctx)
    structural_required = [
        r for r in ctx.get("required_ontology", [])
        if r not in _generation_ctx(ctx)["ontology_fields"]
    ]
    dest_blocks = run.dest_blocks()
    other_files = {k: path for k, (path, _b) in run.other_blocks().items()}
    item_fields = ctx.get("item_fields") or {}
    ensure_occurrences(ctx)

    previous = m.proposals
    proposals: Dict[str, Dict[str, Any]] = {}
    work: Dict[str, Tuple[Optional[Set[str]], dict]] = {}  # código -> (campos ou None=novo, contexto)
    obsolete: List[str] = []
    for code in sorted(ctx["code_index"]["codes"]):
        key = concept_key(code)
        view = _input_view(_build_semantic_ctx(code, ctx), phase, item_fields)
        ih = _input_hash(view)
        prev = previous.get(key, {})
        changed_input = bool(prev) and prev.get("input_hash") != ih
        if prev.get("state") == mf.APPLIED and changed_input:
            obsolete.append(key)
        if key not in dest_blocks and key in other_files:
            proposals[key] = {"state": mf.PENDING, "input_hash": ih,
                              "file": other_files[key].name,
                              "reason": "block in another .syno; run the phase with --output on that file"}
            continue
        if prev.get("state") == mf.REJECTED and not changed_input and not run.retry_rejected:
            proposals[key] = dict(prev)
            continue
        if key in dest_blocks and is_alias_block(dest_blocks[key], ctx):
            continue  # preterido (USE): o conceito é o alvo; nada a gerar aqui
        if key in dest_blocks:
            filled = _filled(dest_blocks[key])
            wanted = set(fields) if run.overwrite else set(fields) - filled
            if not wanted:
                if key in obsolete:
                    proposals[key] = {**prev, "state": mf.OBSOLETE, "input_hash_new": ih}
                elif prev:
                    proposals[key] = dict(prev)
                continue
            work[code] = (wanted, view)
        else:
            if structural_required:
                proposals[key] = {"state": mf.REJECTED, "input_hash": ih, "reason": (
                    f"a new block requires {', '.join(structural_required)}, which this phase "
                    "does not produce (pre-check; no model call)")}
                continue
            work[code] = (None, view)
        proposals[key] = {"state": mf.PROPOSED, "input_hash": ih}
    # Propostas antigas de conceitos que saíram do corpus ficam como estavam.
    for key, record in previous.items():
        proposals.setdefault(key, record)

    if not work:
        run.finish_without_change(proposals, "nada a gerar")
        return _report(phase, run.dest, {}, {}, obsolete, proposals, run.dry_run, None)

    ctx_existing = _phase_ctx(ctx, fields)
    ctx_new = _phase_ctx(ctx, fields + required_new)
    client = run.client()
    semaphore = asyncio.Semaphore(run.concurrent)
    t0 = time.monotonic()
    tasks = [
        _generate_one(code, ctx_new if wanted is None else ctx_existing, view,
                      dest_blocks.get(concept_key(code)), wanted, client, semaphore)
        for code, (wanted, view) in sorted(work.items())
    ]
    changes: Dict[str, Dict[str, List[str]]] = {}
    new_blocks: Dict[str, str] = {}
    for coro in asyncio.as_completed(tasks):
        code, block, ok, reason = await coro
        key = concept_key(code)
        if not ok:
            proposals[key].update(state=mf.REJECTED, reason=reason)
            continue
        fields_wanted = work[code][0]
        if fields_wanted is None:
            new_blocks[key] = block
            proposals[key]["fields"] = sorted(entries_by_field(block))
        else:
            entries = {n: v for n, v in entries_by_field(block).items() if n in fields_wanted}
            if entries:
                changes[key] = entries
                proposals[key]["fields"] = sorted(entries)
    warn_schema_fallbacks(client)

    def build(accepted: Set[str]) -> str:
        return merge_into_syno(
            run.original,
            {k: v for k, v in changes.items() if k in accepted},
            {k: v for k, v in new_blocks.items() if k in accepted},
        )

    changed_keys = set(changes) | set(new_blocks)
    final, rejected = validate_changes(ctx, run.destination, run.original, build, changed_keys)
    accepted = sorted(k for k in changed_keys if k not in rejected)
    return run.commit(
        final, accepted, rejected, proposals,
        revalidate=lambda keys: validate_changes(ctx, run.destination, run.original, build, keys),
        report=lambda props: _report(phase, run.dest, changes, new_blocks, obsolete, props,
                                     run.dry_run, client, elapsed=time.monotonic() - t0),
    )


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------


def _report(phase: PhaseNode, dest: Path, changes: dict, new_blocks: dict, obsolete: List[str],
            proposals: Dict[str, Dict[str, Any]], dry_run: bool,
            client: Optional[LLMClient], elapsed: float = 0.0) -> str:
    states: Dict[str, int] = {}
    for record in proposals.values():
        states[record.get("state", "?")] = states.get(record.get("state", "?"), 0) + 1
    head = "dry run (--dry-run), nothing written" if dry_run else f"destination {dest.name}"
    lines = [f"{phase.label}: {head}"]
    lines.append(f"  blocks with new fields: {len(changes)}; new blocks: {len(new_blocks)}")
    rejected = sorted(k for k, r in proposals.items() if r.get("state") == mf.REJECTED)
    pending = sorted(k for k, r in proposals.items() if r.get("state") == mf.PENDING)
    if rejected:
        lines.append(f"  rejected ({len(rejected)}): {', '.join(rejected[:10])}"
                     + (" ..." if len(rejected) > 10 else ""))
    if pending:
        files = sorted({proposals[k].get('file', '?') for k in pending})
        lines.append(f"  pending in another .syno ({len(pending)}): {', '.join(files)}")
    if obsolete:
        lines.append(f"  obsolete ({len(obsolete)}; inputs changed, filled fields "
                     f"kept without --overwrite): {', '.join(sorted(obsolete)[:10])}")
    lines.append("  states: " + ", ".join(f"{k} {v}" for k, v in sorted(states.items())))
    if client is not None:
        lines.append(f"  {client.usage.summary_line()} | {elapsed:.1f}s")
    return "\n".join(lines) + "\n"
