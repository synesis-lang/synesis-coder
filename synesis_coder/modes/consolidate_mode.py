"""`synesis-coder consolidate`: consolidação de conceitos com ROLE USE (etapa U).

    synesis-coder consolidate <projeto.synp> <preterido> --into <alvo>
    synesis-coder consolidate <projeto.synp> <preterido> --revert

A operação (Spec_USE_Etapa_U.md, §1 e §4):

1. leva ao alvo o que o preterido tem: rótulos e pais por união (um pai que
   seria o próprio alvo, ou um descendente dele, fica de fora: formaria
   ciclo); campo vazio no alvo recebe o valor do preterido; campo preenchido
   nos dois fica com o do alvo, e o do preterido é ARQUIVADO no manifesto;
2. redireciona para o alvo os aliases que apontavam para o preterido (no
   mesmo arquivo; nos outros, o compilador acusa E146 e o relatório avisa);
3. grava o preterido como um bloco só com o campo USE.

Tudo num único .syno local (preterido e alvo no mesmo arquivo), com validação
do conjunto, conferência das entradas e manifesto
(`<destino>.consolidation.manifest.json`) com o texto original dos blocos. A
reversão restaura o preterido; o alvo volta ao texto anterior se não foi
editado desde a consolidação (hash conferido). Os .syn nunca mudam.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from synesis_coder import manifest as mf
from synesis_coder.ontology_publish import (
    DestinationError,
    InputSnapshot,
    resolve_destination,
    validate_changes,
)
from synesis_coder.phase_run import publish
from synesis_coder.project_loader import load_project, project_include_files
from synesis_coder.syno_merge import (
    Block,
    block_entries,
    concept_key,
    existing_blocks,
    parse_syno,
    replace_blocks,
)
from synesis_coder.vocabulary import field_list, field_value, roles_by_field

logger = logging.getLogger(__name__)

PHASE_NAME = "consolidation"
_LEGACY_PHASE_NAME = "consolidacao"  # manifestos de versões de teste anteriores


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_text(block: Block) -> str:
    return "".join(block.lines).replace("\r\n", "\n").strip("\n")


def _locate(project_path: Path, names: List[str]) -> Dict[str, Path]:
    """Arquivo .syno local de cada conceito pedido."""
    found: Dict[str, Path] = {}
    for inc in project_include_files(project_path):
        if inc["type"] != "ONTOLOGY" or inc["shared"]:
            continue
        try:
            text = Path(inc["path"]).read_text(encoding="utf-8")
        except OSError:
            continue
        keys = existing_blocks(text)
        for name in names:
            if concept_key(name) in keys:
                found.setdefault(concept_key(name), Path(inc["path"]).resolve())
    return found


def _render(header: str, fields: List[Tuple[str, str]], indent: str = "    ") -> str:
    lines = [header.rstrip("\r\n")]
    lines += [f"{indent}{name}: {value}" for name, value in fields]
    lines.append("END ONTOLOGY")
    return "\n".join(lines)


def _entries(block: Block) -> List[Tuple[str, List[str]]]:
    return [(e.name, e.lines) for e in block_entries(block)[1] if e.name is not None]


def _descendants(blocks: Dict[str, Block], hierarchy_fields: List[str], root: str) -> Set[str]:
    children: Dict[str, Set[str]] = {}
    for key, block in blocks.items():
        for f in hierarchy_fields:
            for parent in field_list(block, f):
                children.setdefault(concept_key(parent), set()).add(key)
    out: Set[str] = set()
    stack = list(children.get(root, ()))
    while stack:
        k = stack.pop()
        if k not in out:
            out.add(k)
            stack.extend(children.get(k, ()))
    return out


def consolidate(
    project_path: Path,
    alias: str,
    target: Optional[str] = None,
    *,
    revert: bool = False,
    dry_run: bool = False,
    backup: bool = False,
) -> str:
    project_path = Path(project_path)
    ctx = load_project(project_path, load_annotations=True, load_ontology=False,
                       tolerate_annotation_errors=True)
    roles = roles_by_field(ctx)
    use = next((f for f, r in roles.items() if r == "USE"), None)
    if use is None:
        raise ValueError("The template does not declare a field with ROLE USE (stage U); "
                         "add, for example, `FIELD usar TYPE CODE / SCOPE ONTOLOGY / ROLE USE`.")
    names = [alias] + ([target] if target else [])
    where = _locate(project_path, names)
    akey = concept_key(alias)
    if akey not in where:
        raise ValueError(f"`{alias}` has no block in a local .syno of the project.")
    dest_path = where[akey]
    try:
        destination = resolve_destination(project_path, dest_path)
    except DestinationError as exc:
        raise ValueError(str(exc)) from exc
    m_path = mf.manifest_path(dest_path, PHASE_NAME)
    legacy = mf.manifest_path(dest_path, _LEGACY_PHASE_NAME)
    if not m_path.is_file() and legacy.is_file():
        legacy.replace(m_path)  # o registro antigo continua valendo (--revert)
    m = mf.Manifest.load(m_path, PHASE_NAME, dest_path)
    if mf.recover(m, dest_path) == mf.INTERRUPTED and m.pending_text is not None:
        logger.warning("Previous consolidation interrupted before the swap; it was not applied.")
    snapshot = InputSnapshot.take(project_path, destination)
    with open(dest_path, encoding="utf-8", newline="") as fh:
        original = fh.read()
    blocks = existing_blocks(original)

    if revert:
        return _revert(project_path, ctx, destination, snapshot, m, original, blocks, alias, use,
                       dry_run=dry_run, backup=backup)

    if not target:
        raise ValueError("Give the target with --into <concept>.")
    tkey = concept_key(target)
    if tkey == akey:
        raise ValueError("Alias and target are the same concept.")
    if tkey not in where:
        raise ValueError(f"`{target}` has no block in a local .syno of the project.")
    if where[tkey] != dest_path:
        raise ValueError(
            f"`{alias}` is in {dest_path.name} and `{target}` in {where[tkey].name}: a consolidation "
            "writes a single file. Move one of the blocks into the other's file and run again."
        )
    a_block, t_block = blocks[akey], blocks[tkey]
    if field_list(a_block, use):
        raise ValueError(f"`{alias}` is already an alias (uses {field_value(a_block, use)}).")
    if field_list(t_block, use):
        final = field_value(t_block, use)
        raise ValueError(f"`{target}` is an alias (uses {final}): consolidate into `{final}`.")

    hierarchy_fields = [f for f, r in roles.items() if r.startswith("HIERARCHY")]
    label_fields = [f for f, r in roles.items() if r in ("LABEL ALTERNATIVE", "LABEL HIDDEN")]
    below_target = _descendants(blocks, hierarchy_fields, tkey)
    t_entries = dict(_entries(t_block))
    new_t_fields: Dict[str, str] = {}
    transferred: Dict[str, str] = {}
    archived: Dict[str, str] = {}
    dropped: List[str] = []
    for name, lines in _entries(a_block):
        value = field_value(a_block, name)
        if not value:
            continue
        if roles.get(name) == "LABEL PREFERRED":
            # O rótulo preferido é a forma de exibir o PRETERIDO: passá-lo ao alvo
            # trocaria o nome com que o alvo aparece. Fica arquivado; o nome do
            # preterido já vira rótulo do alvo no SKOS (ALIAS_LABEL).
            archived[name] = "".join(lines).strip()
            continue
        if name in hierarchy_fields or name in label_fields:
            current = field_list(t_block, name)
            extra = []
            for v in field_list(a_block, name):
                vkey = concept_key(v)
                if name in hierarchy_fields and (vkey == tkey or vkey in below_target):
                    dropped.append(f"{name}: {v} (would create a cycle or a self-reference in the target)")
                    continue
                if v not in current and v not in extra:
                    extra.append(v)
            if extra:
                new_t_fields[name] = ", ".join(current + extra)
                transferred[name] = ", ".join(extra)
        elif name in t_entries and field_value(t_block, name):
            archived[name] = "".join(lines).strip()
        else:
            new_t_fields[name] = value
            transferred[name] = value

    # Bloco do alvo: campos existentes no lugar, novos no fim.
    header, t_body, _footer = block_entries(t_block)
    rendered_t = [header.rstrip("\r\n")]
    placed = set()
    for entry in t_body:
        if entry.name is not None and entry.name in new_t_fields:
            if entry.name not in placed:
                indent = entry.lines[0][: len(entry.lines[0]) - len(entry.lines[0].lstrip())]
                rendered_t.append(f"{indent}{entry.name}: {new_t_fields[entry.name]}")
                placed.add(entry.name)
            continue
        rendered_t.extend(line.rstrip("\r\n") for line in entry.lines)
    indent = next((e.lines[0][: len(e.lines[0]) - len(e.lines[0].lstrip())]
                   for e in t_body if e.name is not None), "    ")
    for name, value in new_t_fields.items():
        if name not in placed:
            rendered_t.append(f"{indent}{name}: {value}")
    rendered_t.append("END ONTOLOGY")
    new_target = "\n".join(rendered_t)

    new_alias = _render(a_block.lines[0], [(use, t_block.concept)], indent)

    # Aliases que apontavam para o preterido: redirecionados no mesmo arquivo.
    redirected: Dict[str, str] = {}
    for key, block in blocks.items():
        if key in (akey, tkey):
            continue
        if any(concept_key(v) == akey for v in field_list(block, use)):
            redirected[key] = _render(block.lines[0], [(use, t_block.concept)], indent)
    # Aliases do preterido em OUTROS .syno (R2 da avaliação de 2026-11-17):
    # depois da consolidação eles apontariam para um alias (E146). A consolidação
    # escreve um arquivo só, então recusa ANTES de gravar e diz o que mover —
    # um aviso depois da gravação não cumpriria a promessa de validar o conjunto.
    external = [
        f"`{b.concept}` in {p.name}"
        for p in destination.ontology_files.values()
        if Path(p).resolve() != Path(dest_path).resolve()
        for b in existing_blocks(p.read_text(encoding="utf-8")).values()
        if any(concept_key(v) == akey for v in field_list(b, use))
    ]
    if external:
        raise ValueError(
            f"`{a_block.concept}` is the target of aliases in other .syno files: "
            + ", ".join(external)
            + f". After the consolidation they would point to an alias (E146). Nothing was written.\n"
            f"  Redirect them first ({use}: {t_block.concept}), or move them into {dest_path.name}, "
            "and run again."
        )

    replacements = {akey: new_alias, tkey: new_target, **redirected}

    def build(accepted: Set[str]) -> str:
        return replace_blocks(original, {k: v for k, v in replacements.items() if k in accepted})

    final, rejected = validate_changes(ctx, destination, original, build, set(replacements), atomic=True)
    op = f"{a_block.concept} -> {t_block.concept}"
    report = [f"consolidation {op} in {dest_path.name}" + (" (dry run, nothing written)" if dry_run else "")]
    if transferred:
        report.append("  transferred to the target: " + "; ".join(f"{k} = {v}" for k, v in transferred.items()))
    if archived:
        report.append("  archived in the manifest (the target already had it, or the alias's preferred label): " + ", ".join(archived))
    if dropped:
        report.append("  left out: " + "; ".join(dropped))
    if redirected:
        report.append("  redirected aliases: " + ", ".join(blocks[k].concept for k in redirected))
    if rejected:
        detail = "; ".join(f"{k}: {'; '.join(v)}" for k, v in rejected.items())
        raise ValueError("The consolidation introduces an error in the project; nothing was written.\n  " + detail)
    if dry_run:
        return "\n".join(report) + "\n"
    dest_changed, others = snapshot.changed()
    if dest_changed or others:
        raise RuntimeError("Project inputs changed during the operation; nothing was written. Run again.")
    if final and not final.endswith(("\n", "\r\n")):
        final += "\r\n" if "\r\n" in original else "\n"

    m.started_at, m.finished_at, m.stop_reason = mf.now(), "", "consolidation"
    m.inputs = {str(p): h for p, h in snapshot.hashes.items()}
    m.before = snapshot.hashes.get(dest_path)
    m.config = {"operation": "consolidate"}
    m.proposals[akey] = {
        "state": mf.APPLYING, "alias": a_block.concept, "target": t_block.concept,
        "original_alias": _block_text(a_block), "original_target": _block_text(t_block),
        "target_after": _sha(new_target), "transferred": transferred, "archived": archived,
        "dropped": dropped,
        "redirected": {blocks[k].concept: {"original": _block_text(blocks[k]), "after": _sha(v)}
                       for k, v in redirected.items()},
    }
    publish(m, dest_path, final, backup=backup)
    return "\n".join(report) + "\n"


def _revert(project_path: Path, ctx: dict, destination, snapshot, m: mf.Manifest, original: str,
            blocks: Dict[str, Block], alias: str, use: str, *, dry_run: bool, backup: bool) -> str:
    akey = concept_key(alias)
    record = m.proposals.get(akey)
    if not record or record.get("state") != mf.APPLIED:
        raise ValueError(f"There is no applied consolidation of `{alias}` in the manifest of {destination.path.name}.")
    a_block = blocks.get(akey)
    if a_block is None or not field_list(a_block, use):
        raise ValueError(f"`{alias}` is no longer in USE form; revert by hand from the manifest.")
    replacements: Dict[str, str] = {akey: record["original_alias"]}
    notes: List[str] = []
    tkey = concept_key(record["target"])
    t_block = blocks.get(tkey)
    if t_block is not None and _sha(_block_text(t_block)) == record["target_after"]:
        replacements[tkey] = record["original_target"]
    else:
        # O alvo foi editado: ele fica como está, e o que foi transferido para
        # ele não volta ao preterido (um rótulo em dois conceitos seria erro).
        transferred = set(record.get("transferred", {}))
        orig_block = parse_syno(record["original_alias"])[0]
        assert isinstance(orig_block, Block)
        header, entries, footer = block_entries(orig_block)
        kept = [header.rstrip("\r\n")]
        for entry in entries:
            if entry.name is None or entry.name not in transferred:
                kept.extend(line.rstrip("\r\n") for line in entry.lines)
        kept.append(footer.rstrip("\r\n"))
        replacements[akey] = "\n".join(kept)
        notes.append(f"`{record['target']}` was edited after the consolidation: it stays as it is, and "
                     f"what was transferred to it ({', '.join(sorted(transferred)) or 'nothing'}) does not go back "
                     f"to the alias; review by hand if needed.")
    for name, info in (record.get("redirected") or {}).items():
        key = concept_key(name)
        block = blocks.get(key)
        if block is not None and _sha(_block_text(block)) == info["after"]:
            replacements[key] = info["original"]

    def build(accepted: Set[str]) -> str:
        return replace_blocks(original, {k: v for k, v in replacements.items() if k in accepted})

    final, rejected = validate_changes(ctx, destination, original, build, set(replacements), atomic=True)
    if rejected:
        detail = "; ".join(f"{k}: {'; '.join(v)}" for k, v in rejected.items())
        raise ValueError("The reversal introduces an error in the project; nothing was written.\n  " + detail)
    head = f"reversal of {record['alias']} -> {record['target']} in {destination.path.name}"
    if dry_run:
        return "\n".join([head + " (dry run, nothing written)"] + notes) + "\n"
    dest_changed, others = snapshot.changed()
    if dest_changed or others:
        raise RuntimeError("Project inputs changed during the operation; nothing was written. Run again.")
    if final and not final.endswith(("\n", "\r\n")):
        final += "\r\n" if "\r\n" in original else "\n"
    m.before = snapshot.hashes.get(destination.path)
    m.inputs = {str(p): h for p, h in snapshot.hashes.items()}
    record["state"] = mf.APPLYING
    record["reverted_at"] = mf.now()
    publish(m, destination.path, final, backup=backup)
    m.proposals[akey]["state"] = "revertida"
    m.save()
    return "\n".join([head] + notes) + "\n"
