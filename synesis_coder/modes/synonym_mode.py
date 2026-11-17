"""Fase de sinônimos (etapa B5; estudo §6.4): campos LABEL ALTERNATIVE/HIDDEN.

- **Candidatos** por semelhança de nome (`vocabulary.similarity_key`:
  separadores, caixa, acentos, plural simples) entre os conceitos com bloco e
  os termos do corpus sem bloco. Embeddings ficam para quando o projeto tiver
  o sidecar (não usados nesta versão).
- **Termo sem bloco** num grupo com um conceito: o modelo decide se nomeiam o
  mesmo conceito e de que tipo é o rótulo (ALTERNATIVE ou HIDDEN, só os que a
  fase declara). Aceito, o termo vira rótulo do conceito.
- **Dois conceitos com bloco** parecidos: candidatos a duplicata, para
  decisão do pesquisador (relatório e manifesto). A consolidação é da etapa U;
  esta fase nunca funde blocos.

Rótulos são acrescentados ao campo (nada é apagado). Um rótulo igual ao nome
de outro conceito, ou já usado por outro conceito, não é gravado.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Set, Tuple

from synesis_coder import manifest as mf
from synesis_coder.modes.phase_mode import ensure_occurrences
from synesis_coder.ontology_publish import validate_changes
from synesis_coder.phase_run import PhaseRun
from synesis_coder.runtime_info import warn_schema_fallbacks
from synesis_coder.syno_merge import concept_key, existing_blocks, merge_into_syno
from synesis_coder.vocabulary import (
    Concept,
    ask_json,
    build_table,
    is_alias_block,
    project_context_text,
    roles_by_field,
    similarity_key,
)

logger = logging.getLogger(__name__)

GROUPS_PER_CALL = 40


def candidate_groups(table: Dict[str, Concept], terms: List[str]) -> List[Dict[str, Any]]:
    """Grupos de nomes com a mesma chave de semelhança (dois ou mais membros)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for key, concept in table.items():
        bucket = buckets.setdefault(similarity_key(concept.name), {"concepts": [], "terms": []})
        bucket["concepts"].append(key)
    known_labels = {concept_key(lab) for c in table.values() for lab in c.all_labels()}
    for term in terms:
        key = concept_key(term)
        if key in table or key in known_labels:
            continue
        bucket = buckets.setdefault(similarity_key(term), {"concepts": [], "terms": []})
        if term not in bucket["terms"]:
            bucket["terms"].append(term)
    groups = []
    for sim, bucket in sorted(buckets.items()):
        if len(bucket["concepts"]) + len(bucket["terms"]) >= 2:
            groups.append({"key": sim, "concepts": sorted(bucket["concepts"]), "terms": sorted(bucket["terms"])})
    return groups


def _schema(kinds: List[str]) -> dict:
    label = {"type": "object", "additionalProperties": False,
             "required": ["term", "kind"],
             "properties": {"term": {"type": "string"},
                            "kind": {"type": "string", "enum": kinds}}}
    group = {"type": "object", "additionalProperties": False,
             "required": ["group", "same_concept", "concept", "labels", "reason"],
             "properties": {"group": {"type": "integer"}, "same_concept": {"type": "boolean"},
                            "concept": {"type": "string"},
                            "labels": {"type": "array", "items": label},
                            "reason": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["groups"],
            "properties": {"groups": {"type": "array", "items": group}}}


_KIND_TEXT = {
    "ALTERNATIVE": "ALTERNATIVE: another name in use for the same concept (synonym, other wording)",
    "HIDDEN": "HIDDEN: a variant spelling, typo, old code or separator variant, kept for search only",
}


def _messages(run: PhaseRun, kinds: List[str], groups: List[Tuple[int, Dict[str, Any]]],
              table: Dict[str, Concept]) -> List[dict]:
    system = [
        "You decide whether terms used in the annotations of a qualitative research project name "
        "the same concept as an existing concept of its vocabulary. You only answer with the "
        "requested JSON.",
        "For each group: same_concept is true only if the TERM(S) and the CONCEPT mean the same "
        "thing in this project (not a narrower or broader idea, not a related idea). If true, "
        "give the concept name exactly as listed and classify each term:\n"
        + "\n".join(f"- {_KIND_TEXT[k]}" for k in kinds),
        "If false, return an empty labels list. A term with a different meaning is never a label.",
    ]
    context = project_context_text(run.ctx)
    if context:
        system.append("PROJECT:\n" + context)
    rows = []
    for gid, group in groups:
        concepts = "; ".join(
            f"{table[k].name}" + (f" (def: {table[k].definition[:200]})" if table[k].definition else "")
            for k in group["concepts"])
        rows.append(f"group {gid}: CONCEPT(S): {concepts} || TERM(S) without entry: {', '.join(group['terms'])}")
    return [{"role": "system", "content": "\n\n".join(system), "cache": True},
            {"role": "user", "content": "\n".join(rows), "cache": False}]


async def run_synonyms(run: PhaseRun) -> str:
    ctx, phase, m = run.ctx, run.phase, run.manifest
    roles = roles_by_field(ctx)
    kind_field = {roles[f].split(" ", 1)[1]: f for f in phase.fields("FIELDS")}
    kinds = sorted(kind_field)
    ensure_occurrences(ctx)
    texts = {run.dest: run.original, **run.other_texts()}
    table = build_table(ctx, texts)
    # Os preteridos (USE) já estão consolidados: nem conceito nem termo candidato.
    aliases = {concept_key(b.concept) for t in texts.values()
               for b in existing_blocks(t).values() if is_alias_block(b, ctx)}
    terms = sorted(c for c in ctx.get("code_index", {}).get("codes", []) if concept_key(c) not in aliases)
    groups = candidate_groups(table, terms)

    proposals: Dict[str, Dict[str, Any]] = {k: v for k, v in m.proposals.items()}
    notes: Dict[str, Any] = {"duplicates": [], "embeddings": "not used (no sidecar in this version)"}

    # Duplicatas entre conceitos com bloco: só relatório (decisão do pesquisador; etapa U)
    for group in groups:
        if len(group["concepts"]) >= 2:
            notes["duplicates"].append([table[k].name for k in group["concepts"]])

    # Grupos com termo sem bloco e um conceito: o modelo decide
    judged = [(i, g) for i, g in enumerate(groups, 1) if g["terms"] and g["concepts"]]
    orphans = [g for g in groups if g["terms"] and not g["concepts"]]
    notes["terms_without_concept"] = [", ".join(g["terms"]) for g in orphans]
    t0 = time.monotonic()
    calls = 0
    decisions: List[dict] = []
    if judged:
        client = run.client()
        for start in range(0, len(judged), GROUPS_PER_CALL):
            batch = judged[start:start + GROUPS_PER_CALL]
            calls += 1
            data = await ask_json(client, _messages(run, kinds, batch, table), _schema(kinds))
            decisions.extend((data or {}).get("groups", []) or [])
        warn_schema_fallbacks(client)

    by_id = dict(judged)
    names = {k for k in table} | {concept_key(lab) for c in table.values() for lab in c.all_labels()}
    new_labels: Dict[str, Dict[str, List[str]]] = {}  # conceito -> campo -> rótulos
    used: Set[str] = set()
    for decision in decisions:
        cand = by_id.get(int(decision.get("group", -1) or -1))
        if cand is None:
            continue
        target = concept_key(str(decision.get("concept", "")))
        for label in decision.get("labels", []) or []:
            term, kind = str(label.get("term", "")).strip(), str(label.get("kind", "")).upper()
            pkey = f"{target} {kind} {term}"
            if not decision.get("same_concept") or term not in cand["terms"]:
                continue
            if target not in cand["concepts"] or kind not in kind_field:
                proposals[pkey] = {"state": mf.REJECTED, "term": term, "reason": "concept or kind outside the group"}
                continue
            tkey = concept_key(term)
            if tkey in names or tkey in used:
                proposals[pkey] = {"state": mf.REJECTED, "term": term,
                                   "reason": "label equal to the name or label of another concept"}
                continue
            used.add(tkey)
            new_labels.setdefault(target, {}).setdefault(kind_field[kind], []).append(term)
            proposals[pkey] = {"state": mf.PROPOSED, "concept": table[target].name, "term": term,
                               "kind": kind, "field": kind_field[kind],
                               "reason": str(decision.get("reason", ""))[:300]}

    dest_keys = set(run.dest_blocks())
    changes: Dict[str, Dict[str, List[str]]] = {}
    for target, fields in new_labels.items():
        concept = table[target]
        if target not in dest_keys:
            for f, values in fields.items():
                for term in values:
                    proposals[f"{target} {_kind_of(f, kind_field)} {term}"].update(
                        state=mf.PENDING, file=concept.file.name,
                        reason="block in another .syno; run the phase with --output on that file")
            continue
        changes[target] = {
            f: [f"    {f}: {', '.join(concept.labels.get(f, []) + values)}\n"]
            for f, values in fields.items()
        }

    def report(props: Dict[str, Dict[str, Any]]) -> str:
        return _report(run, props, notes, len(groups), calls, time.monotonic() - t0)

    if not changes:
        run.finish_without_change(proposals, "no new label", notes)
        return report(proposals)

    def build(accepted: Set[str]) -> str:
        return merge_into_syno(run.original, {k: v for k, v in changes.items() if k in accepted}, {})

    def labels_of(identity: str) -> List[str]:
        return [p for p, r in proposals.items()
                if r.get("state") == mf.PROPOSED and concept_key(str(r.get("concept", ""))) == identity]

    final, rejected = validate_changes(ctx, run.destination, run.original, build, set(changes))
    accepted = sorted(k for k in changes if k not in rejected)
    return run.commit(
        final, accepted, rejected, proposals,
        revalidate=lambda keys: validate_changes(ctx, run.destination, run.original, build, keys),
        report=report, notes=notes, proposal_keys=labels_of,
    )


def _kind_of(field_name: str, kind_field: Dict[str, str]) -> str:
    return next(k for k, f in kind_field.items() if f == field_name)


def _report(run: PhaseRun, proposals: Dict[str, Dict[str, Any]], notes: Dict[str, Any],
            groups: int, calls: int, elapsed: float) -> str:
    head = "dry run (--dry-run), nothing written" if run.dry_run else f"destination {run.dest.name}"
    lines = [f"{run.phase.label} (synonyms): {head}"]
    lines.append(f"  candidate groups: {groups}; calls: {calls}; embeddings: {notes['embeddings']}")
    labels = [r for r in proposals.values() if r.get("term") and r.get("state") in (mf.APPLIED, mf.PROPOSED)]
    if labels:
        lines.append(f"  labels ({len(labels)}): " + "; ".join(
            f"{r['concept']} <- {r['term']} ({r['kind']})" for r in labels[:12]))
    dups: List[List[str]] = notes.get("duplicates", [])
    lines.append(f"  duplicate candidates between concepts ({len(dups)}; the researcher decides, "
                 "consolidation with USE):")
    lines.extend(f"    {' × '.join(d)}" for d in dups[:20])
    if notes.get("terms_without_concept"):
        lines.append(f"  similar terms without a concept block ({len(notes['terms_without_concept'])}): "
                     + "; ".join(notes["terms_without_concept"][:8]))
    rejected = [(k, r) for k, r in proposals.items() if r.get("state") == mf.REJECTED]
    if rejected:
        lines.append(f"  rejected ({len(rejected)}): " + "; ".join(
            f"{r.get('term', k)}: {r.get('reason', '')}" for k, r in rejected[:8]))
    client = run.client_used
    if client is not None:
        lines.append(f"  {client.usage.summary_line()} | {elapsed:.1f}s")
    return "\n".join(lines) + "\n"
