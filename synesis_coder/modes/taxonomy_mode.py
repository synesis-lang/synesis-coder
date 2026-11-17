"""Fase de taxonomia (etapa B4; estudo §6.5): campos com papel HIERARCHY.

Quatro operações separadas:

1. **reconhecer pais existentes** entre os conceitos com bloco;
2. **criar um conceito mais amplo**, quando a execução permite e há
   justificativa; o critério de validade é o teste da espécie (é-um para
   KIND, parte-de para PART, caso-de para INSTANCE); as QUESTIONS dão o
   critério de utilidade (o manifesto registra a que questões o pai serve);
3. **completar o conceito criado** com os campos obrigatórios do template,
   a partir de trechos dos descendentes (evidência derivada, registrada);
4. **decidir se o conceito terá pai**: pode ficar no topo; procurar pai novo
   para um pai novo é outra geração (`new_generations`, padrão 1).

O trabalho é dividido em grupos (por um campo de classificação ou por
tamanho), só para caber no contexto; uma segunda passada compara os
conceitos intermediários de grupos diferentes.

Políticas do gerador, desligadas por padrão: `within_topic`, `min_children`,
`max_new`. O orçamento de chamadas (`max_calls`) é proteção, sempre ativa.
Sem herança: os valores dos filhos são insumo, não resposta.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from synesis.ast.nodes import FieldType

from synesis_coder import manifest as mf
from synesis_coder.modes.ontology_mode import _build_semantic_ctx, _generate_ontology_syno
from synesis_coder.modes.phase_mode import _input_hash, _phase_ctx, ensure_occurrences
from synesis_coder.ontology_publish import validate_changes
from synesis_coder.phase_run import PhaseRun
from synesis_coder.runtime_info import warn_schema_fallbacks
from synesis_coder.syno_merge import concept_key, merge_into_syno
from synesis_coder.validator import validate_ontology_entry_async
from synesis_coder.vocabulary import (
    Concept,
    ask_json,
    build_table,
    chains_between,
    project_context_text,
    roles_by_field,
)

logger = logging.getLogger(__name__)

DEFAULTS = {"group_size": 60, "new_generations": 1, "max_calls": 60}

SPECIES_TESTS = {
    "KIND": ("KIND (is a kind of): every A is a B — the 'all-and-some' test. Parent and child "
             "are the same sort of concept (a thing under a thing, an action under an action). "
             "A part is never a kind: the liver is part of the body, not a kind of body."),
    "PART": ("PART (is part of): A belongs to the whole B in any context. Do not use PART when "
             "the part belongs to several different wholes. A part is not a type of the whole."),
    "INSTANCE": "INSTANCE (is an instance of): A is an individual case of the class B.",
}

# Classes da checagem prévia dos campos obrigatórios (estudo §6.5, item 5).
PRODUCIBLE_TYPES = (FieldType.TEXT, FieldType.TOPIC, FieldType.ENUMERATED,
                    FieldType.ORDERED, FieldType.SCALE, FieldType.DATE)

_NAME_RE = re.compile(r"\s+")


@dataclass
class Link:
    child: str  # identidade
    parent: str  # identidade (de conceito existente ou de pai novo)
    species: str
    test: str = ""
    group: str = ""
    passe: int = 1

    @property
    def pkey(self) -> str:
        return f"{self.child} {self.species} {self.parent}"


@dataclass
class NewParent:
    name: str
    key: str
    species: str
    children: List[str] = field(default_factory=list)  # identidades
    justification: str = ""
    questions: List[str] = field(default_factory=list)
    generation: int = 1
    group: str = ""
    evidence: List[str] = field(default_factory=list)  # filhos cujos trechos foram usados

    @property
    def pkey(self) -> str:
        return f"new:{self.key}"


# ---------------------------------------------------------------------------
# Checagem prévia
# ---------------------------------------------------------------------------


def precheck(ctx: dict, new_generations: int) -> Tuple[bool, Dict[str, str], str]:
    """(criação de pais permitida?, classe de cada REQUIRED, motivo do bloqueio)."""
    specs = ctx.get("ontology_fields", {})
    roles = roles_by_field(ctx)
    classes: Dict[str, str] = {}
    for name in ctx.get("required_ontology", []):
        spec, role = specs.get(name), roles.get(name, "")
        if role == "LABEL PREFERRED":
            classes[name] = "known"
        elif role in ("LABEL ALTERNATIVE", "LABEL HIDDEN"):
            classes[name] = "blocking (a synonym cannot be fabricated)"
        elif spec is not None and spec.type in PRODUCIBLE_TYPES and (role == "" or role.startswith("NOTE")):
            classes[name] = "producible"
        else:
            kind = spec.type.name if spec is not None else "?"
            classes[name] = f"blocking ({kind}{' ' + role if role else ''} is not generated)"
    blocking = [n for n, c in classes.items() if c.startswith("blocking")]
    if new_generations <= 0:
        return False, classes, "--new-generations 0: parent creation off"
    if blocking:
        return False, classes, (
            "parent creation off: required field " + ", ".join(
                f"`{n}` ({classes[n]})" for n in blocking)
            + "; reconhecer pais existentes continua")
    return True, classes, ""


# ---------------------------------------------------------------------------
# Grupos
# ---------------------------------------------------------------------------


def make_groups(table: Dict[str, Concept], group_by: Optional[str], size: int) -> List[Tuple[str, List[str]]]:
    """Divide a tabela para caber no contexto; não restringe quem é pai de quem."""
    buckets: Dict[str, List[str]] = {}
    for key, concept in table.items():
        value = concept.classes.get(group_by, "") if group_by else ""
        buckets.setdefault(value or "(no value)", []).append(key)
    groups: List[Tuple[str, List[str]]] = []
    current: List[str] = []
    labels: List[str] = []
    for value in sorted(buckets):
        members = sorted(buckets[value])
        while len(members) > size:  # grupo grande: em pedaços
            groups.append((f"{value} ({len(groups) + 1})", members[:size]))
            members = members[size:]
        if current and len(current) + len(members) > size:
            groups.append((" + ".join(labels), current))
            current, labels = [], []
        current = current + members
        labels.append(value)
    if current:
        groups.append((" + ".join(labels), current))
    return groups


# ---------------------------------------------------------------------------
# Prompt e esquema
# ---------------------------------------------------------------------------


def _row(c: Concept, phase_fields: Set[str], with_parents: bool = True) -> str:
    parts = [c.name]
    if c.definition:
        parts.append("def: " + c.definition[:300])
    for name, value in sorted(c.classes.items()):
        parts.append(f"{name}: {value}")
    if with_parents:
        for name, parents in sorted(c.parents.items()):
            parts.append(f"{name}: {', '.join(parents)}")
    labels = c.all_labels()
    if labels:
        parts.append("labels: " + ", ".join(labels))
    parts.append(f"ITEMs: {c.items}")
    return "- " + " | ".join(parts)


def _schema(species: List[str], allow_new: bool) -> dict:
    link = {"type": "object", "additionalProperties": False,
            "required": ["child", "parent", "species", "test"],
            "properties": {"child": {"type": "string"}, "parent": {"type": "string"},
                           "species": {"type": "string", "enum": species},
                           "test": {"type": "string"}}}
    new = {"type": "object", "additionalProperties": False,
           "required": ["name", "species", "children", "justification", "questions"],
           "properties": {"name": {"type": "string"},
                          "species": {"type": "string", "enum": species},
                          "children": {"type": "array", "items": {"type": "string"}},
                          "justification": {"type": "string"},
                          "questions": {"type": "array", "items": {"type": "string"}}}}
    if not allow_new:
        new_arr: dict = {"type": "array", "items": new, "maxItems": 0}
    else:
        new_arr = {"type": "array", "items": new}
    top = {"type": "object", "additionalProperties": False, "required": ["concept", "reason"],
           "properties": {"concept": {"type": "string"}, "reason": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["links", "new_parents", "top"],
            "properties": {"links": {"type": "array", "items": link},
                           "new_parents": new_arr, "top": {"type": "array", "items": top}}}


def _messages(run: PhaseRun, species: List[str], rows: List[str], chains: List[Tuple[str, str, str]],
              allow_new: bool, second_pass: bool, policies: List[str]) -> List[dict]:
    ctx = run.ctx
    tests = "\n".join(f"- {SPECIES_TESTS[s]}" for s in species)
    questions = "\n".join(f"- {qid}: {text}" for qid, text in ctx.get("project_questions") or [])
    system = [
        "You organize the concepts of a qualitative research project into a hierarchy "
        "(a lightweight thesaurus, ANSI/NISO Z39.19). You only answer with the requested JSON.",
        "VALIDITY — a link is valid only if it passes the test of its species:\n" + tests,
        "Operations, kept separate:\n"
        "1. LINKS: for a concept of the table, name broader concepts that ALREADY exist in the "
        "table (use the exact names). Concluding that no parent is appropriate is a valid answer.\n"
        + ("2. NEW PARENTS: propose a broader concept that does not exist yet only when two or more "
           "listed concepts (or one, if the test holds clearly) need it and the test of the species "
           "holds for EACH child. Name it in the language and style of the existing names "
           "(lowercase, words joined by '_'). Do not create a parent that is just a synonym of a "
           "child or of an existing concept. Say which project questions (ids) the parent helps "
           "answer, or none.\n" if allow_new else
           "2. NEW PARENTS: not allowed in this run; return an empty list.\n")
        + "3. TOP: list concepts you judge to be top concepts (no broader concept fits), with the reason.",
        "Do not infer a parent from a shared topic or classification value alone: those columns "
        "group the work and are evidence, not answers. Never invert direction (the child is the "
        "narrower concept). No cycles.",
    ]
    if second_pass:
        system.append("This is the SECOND PASS: the concepts come from different groups (the "
                      "'group' marker); look for links ACROSS groups, between intermediate concepts.")
    if policies:
        system.append("Run policies: " + "; ".join(policies) + ".")
    context = project_context_text(ctx)
    if context:
        system.append("PROJECT:\n" + context)
    if questions and "QUESTIONS" not in context.upper():
        system.append("PROJECT QUESTIONS:\n" + questions)
    user = ["CONCEPTS:\n" + "\n".join(rows)]
    if chains:
        user.append("RELATIONS STATED BY SOURCES (evidence, not answers):\n"
                    + "\n".join(f"  {a} -> {r} -> {b}" for a, r, b in chains[:200]))
    user.append(f"Allowed species: {', '.join(species)}.")
    return [{"role": "system", "content": "\n\n".join(system), "cache": True},
            {"role": "user", "content": "\n\n".join(user), "cache": False}]


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------


async def run_taxonomy(run: PhaseRun) -> str:
    ctx, phase, m = run.ctx, run.phase, run.manifest
    opts = {**DEFAULTS, **run.options}
    roles = roles_by_field(ctx)
    fields = phase.fields("FIELDS")
    species_field = {roles[f].split(" ", 1)[1]: f for f in fields if roles.get(f, "").startswith("HIERARCHY")}
    justification_field = next((f for f in fields if f not in species_field.values()), None)
    species = sorted(species_field)
    ensure_occurrences(ctx)

    texts = {run.dest: run.original, **run.other_texts()}
    table = build_table(ctx, texts)
    label_owner = {concept_key(lab): k for k, c in table.items() for lab in c.all_labels()}

    allow_new, required_classes, blocked_reason = precheck(ctx, int(opts["new_generations"]))
    group_by = opts.get("group_by")
    if group_by is None:
        group_by = next((n for n, s in ctx.get("ontology_fields", {}).items() if s.type == FieldType.TOPIC), None)
    elif group_by not in ctx.get("ontology_fields", {}):
        raise ValueError(f"--group-by {group_by}: field outside the template's ONTOLOGY.")
    topic_field = next((n for n, s in ctx.get("ontology_fields", {}).items() if s.type == FieldType.TOPIC), None)
    if opts.get("within_topic") and topic_field is None:
        raise ValueError("--within-topic needs a TOPIC field in the template's ONTOLOGY.")
    use_chains = any(getattr(ctx.get("item_fields", {}).get(f), "type", None) == FieldType.CHAIN
                     for f in phase.fields("INPUT"))
    phase_fields = set(species_field.values())
    # Relações da CHAIN fora do prompt: num piloto avaliado, os pares do gabarito
    # (ex.: TIPO_DE, PARTE_DE) não podem ser material do prompt (guia, B4).
    hidden = {r.strip().upper() for r in str(opts.get("hide_relations") or "").split(",") if r.strip()}
    policies = []
    if opts.get("within_topic"):
        policies.append("a new parent only groups children with the same topic")
    if opts.get("min_children"):
        policies.append(f"a new parent needs at least {opts['min_children']} children")

    notes: Dict[str, Any] = {
        "precheck": required_classes,
        "parent_creation": "allowed" if allow_new else blocked_reason,
        "grouping": group_by or f"size ({opts['group_size']})",
        "top": dict(m.notes.get("top", {})),
        "groups": {},
    }
    previous_groups = m.notes.get("groups", {}) if m.status == mf.APPLIED else {}
    client = None
    calls = 0
    stop_reason = "completed"
    links: List[Link] = []
    new_parents: Dict[str, NewParent] = {}
    t0 = time.monotonic()

    # Ligações propostas pelo modelo e descartadas antes do manifesto, com o
    # motivo (R4 da avaliação de 2026-11-17: antes sumiam sem registro).
    discarded: List[str] = []

    def resolve(name: str) -> Optional[str]:
        key = concept_key(_NAME_RE.sub("_", name.strip()))
        if key in table:
            return key
        return label_owner.get(key)

    def resolve_any(name: str) -> Optional[str]:
        """Conceito existente (nome ou rótulo) ou pai novo já proposto nesta execução.

        Vale para o PAI e para o FILHO de uma ligação: na segunda passada, um
        pai novo da primeira pode receber o seu próprio pai.
        """
        key = resolve(name)
        if key:
            return key
        candidate = concept_key(_NAME_RE.sub("_", name.strip()))
        return candidate if candidate in new_parents else None

    def absorb(data: Optional[dict], group: str, passe: int, allowed_new: bool) -> None:
        if not data:
            return
        for item in data.get("links", []) or []:
            child_name, parent_name = str(item.get("child", "")), str(item.get("parent", ""))
            child, pkey = resolve_any(child_name), resolve_any(parent_name)
            sp = str(item.get("species", "")).upper()
            reason = ("unknown child" if not child else "unknown parent" if not pkey
                      else f"species outside the phase ({sp or '?'})" if sp not in species_field
                      else "self-link" if child == pkey else "")
            if reason or child is None or pkey is None:
                discarded.append(f"{child_name} {sp or '?'} {parent_name} ({group}): {reason}")
                continue
            links.append(Link(child, pkey, sp, str(item.get("test", ""))[:300], group, passe))
        for item in data.get("new_parents", []) or []:
            name = _NAME_RE.sub("_", str(item.get("name", "")).strip())
            sp = str(item.get("species", "")).upper()
            if not name or sp not in species_field:
                continue
            children = []
            for c in item.get("children", []) or []:
                ckey = resolve_any(str(c))
                if ckey:
                    children.append(ckey)
                else:
                    discarded.append(f"{c} -> new parent {name} ({group}): unknown child")
            existing = resolve(name)
            if existing:  # não duplica: o "pai novo" já existe (nome ou rótulo)
                links.extend(Link(c, existing, sp, "pai existente reaproveitado", group, passe)
                             for c in children if c != existing)
                continue
            if not allowed_new:
                continue
            key = concept_key(name)
            target = new_parents.setdefault(key, NewParent(
                name=name, key=key, species=sp, justification=str(item.get("justification", ""))[:500],
                questions=[str(q) for q in item.get("questions", []) or []],
                generation=passe, group=group))
            for c in children:
                if c not in target.children:
                    target.children.append(c)
        for item in data.get("top", []) or []:
            top_key = resolve(str(item.get("concept", "")))
            if top_key:
                notes["top"][table[top_key].name] = str(item.get("reason", ""))[:300]

    # 1ª passada: por grupo
    groups = make_groups(table, group_by, int(opts["group_size"]))
    ran_any = False
    for gid, keys in groups:
        rows = [_row(table[k], phase_fields) for k in keys]
        ghash = _input_hash([_row(table[k], phase_fields, with_parents=False) for k in keys])
        notes["groups"][gid] = ghash
        if not run.overwrite and previous_groups.get(gid) == ghash:
            continue  # grupo sem mudança desde a última execução aplicada
        if calls >= int(opts["max_calls"]):
            stop_reason = "call budget"
            notes["groups"].pop(gid)
            continue
        chains = [t for t in chains_between(ctx, set(keys)) if t[1].upper() not in hidden] if use_chains else []
        client = client or run.client()
        calls += 1
        ran_any = True
        absorb(await ask_json(client, _messages(run, species, rows, chains, allow_new, False, policies),
                              _schema(species, allow_new)), gid, 1, allow_new)

    # 2ª passada: intermediários de grupos diferentes
    group_of = {k: gid for gid, keys in groups for k in keys}
    middle: Set[str] = {link.parent for link in links if link.parent in table}
    for concept in table.values():
        for values in concept.parents.values():
            middle.update(k for k in (resolve(v) for v in values) if k)
    middle.update(k for k in (resolve(n) for n in notes["top"]) if k)
    intermediates = sorted(middle)
    # Pais novos também acionam a segunda passada: é nela que eles podem
    # receber o próprio pai (R4).
    crosses_groups = len({group_of.get(k) for k in intermediates}) > 1
    if (crosses_groups or new_parents) and calls < int(opts["max_calls"]):
        rows = [_row(table[k], phase_fields) + f" | group: {group_of.get(k, '?')}" for k in intermediates]
        rows += [f"- {p.name} | NEW (proposed in this run) | children: "
                 + ", ".join(_display(c, table, new_parents) for c in p.children) for p in new_parents.values()]
        p2hash = _input_hash(rows)
        notes["groups"]["(second pass)"] = p2hash
        if ran_any or run.overwrite or previous_groups.get("(second pass)") != p2hash:
            client = client or run.client()
            calls += 1
            allow_gen2 = allow_new and int(opts["new_generations"]) >= 2
            absorb(await ask_json(client, _messages(run, species, rows, [], allow_gen2, True, policies),
                                  _schema(species, allow_gen2)), "(second pass)", 2, allow_gen2)

    proposals: Dict[str, Dict[str, Any]] = {k: v for k, v in m.proposals.items()}
    dropped: Dict[str, str] = {}

    # Políticas do gerador sobre os pais novos
    for key, parent in list(new_parents.items()):
        reason = ""
        if opts.get("min_children") and len(parent.children) < int(opts["min_children"]):
            reason = f"--min-children {opts['min_children']}: {len(parent.children)} filho(s)"
        elif opts.get("within_topic") and len({table[c].classes.get(topic_field or "", "")
                                                for c in parent.children}) > 1:
            reason = "--within-topic: children from different topics"
        elif not parent.children:
            reason = "no child from the table"
        if reason:
            dropped[parent.pkey] = reason
            del new_parents[key]
    if opts.get("max_new") and len(new_parents) > int(opts["max_new"]):
        ranked = sorted(new_parents.values(), key=lambda p: (-len(p.children), p.key))
        for parent in ranked[int(opts["max_new"]):]:
            dropped[parent.pkey] = f"--max-new {opts['max_new']}"
            del new_parents[parent.key]
    for parent in new_parents.values():
        links.extend(Link(c, parent.key, parent.species, parent.justification[:300], parent.group,
                          parent.generation) for c in parent.children)

    # Filtros de ligação: existente, não repetida, sem inversão óbvia, campo vazio
    accepted_links: Dict[str, Link] = {}
    for link in links:
        field_name = species_field[link.species]
        child = table.get(link.child)
        known_parent = link.parent in table or link.parent in new_parents
        if child is None and link.child not in new_parents:
            continue
        if not known_parent or link.pkey in accepted_links:
            continue
        reverse = f"{link.parent} {link.species} {link.child}"
        if reverse in accepted_links:
            dropped[link.pkey] = "inverse of another proposed link"
            continue
        if child is not None:
            declared = {concept_key(p) for p in child.parents.get(field_name, [])}
            if link.parent in declared:
                continue  # já declarada
            if declared and not run.overwrite:
                dropped[link.pkey] = f"`{field_name}` already filled (use --overwrite)"
                continue
        accepted_links[link.pkey] = link

    # Pendências: filho com bloco em outro .syno
    dest_keys = set(run.dest_blocks())
    for pkey, link in list(accepted_links.items()):
        child = table.get(link.child)
        if child is not None and link.child not in dest_keys:
            proposals[pkey] = {"state": mf.PENDING, "child": child.name, "species": link.species,
                               "parent": _display(link.parent, table, new_parents),
                               "file": child.file.name,
                               "reason": "block in another .syno; run the phase with --output on that file"}
            del accepted_links[pkey]

    # Completar os pais novos (campos obrigatórios a partir dos descendentes)
    completed: Dict[str, str] = {}
    if new_parents:
        client = client or run.client()
        producible = [n for n, c in required_classes.items() if c == "producible"]
        gen_ctx = _phase_ctx(ctx, producible)
        known = [n for n, c in required_classes.items() if c == "known"]
        semaphore = asyncio.Semaphore(run.concurrent)

        async def complete(parent: NewParent) -> Tuple[str, Optional[str], str]:
            if calls_left() <= 0:
                return parent.key, None, "call budget"
            async with semaphore:
                semantic = _derived_evidence(parent, table, ctx)
                block = f"ONTOLOGY {parent.name}\nEND ONTOLOGY"
                if producible:
                    block = await _generate_ontology_syno(parent.name, gen_ctx, semantic, client)
                block = _add_lines(block, [f"    {n}: {parent.name.replace('_', ' ')}" for n in known])
                own = sorted({l.parent for l in accepted_links.values() if l.child == parent.key})
                for sp, fname in species_field.items():
                    parents = [_display(p, table, new_parents) for p in own
                               if accepted_links.get(f"{parent.key} {sp} {p}")]
                    if parents:
                        block = _add_lines(block, [f"    {fname}: {', '.join(parents)}"])
                if justification_field and parent.justification:
                    block = _add_lines(block, [f"    {justification_field}: {parent.justification}"])
                final, ok = await validate_ontology_entry_async(
                    block, gen_ctx, client, ontology_key=f"ontology_{parent.key}.syno")
                return parent.key, final if ok else None, "" if ok else "the new parent's block is invalid in the template"

        budget = {"left": int(opts["max_calls"]) - calls}

        def calls_left() -> int:
            budget["left"] -= 1
            return budget["left"] + 1

        for key, block, reason in await asyncio.gather(*(complete(p) for p in new_parents.values())):
            if block is None:
                dropped[new_parents[key].pkey] = reason
                if reason == "call budget":
                    stop_reason = reason
            else:
                completed[key] = block
        calls += sum(1 for _ in new_parents) if producible else 0

    # Registro das propostas
    for pkey, link in accepted_links.items():
        proposals[pkey] = {"state": mf.PROPOSED, "child": _display(link.child, table, new_parents),
                           "species": link.species, "field": species_field[link.species],
                           "parent": _display(link.parent, table, new_parents),
                           "test": link.test, "group": link.group, "pass": link.passe}
    for parent in new_parents.values():
        proposals[parent.pkey] = {
            "state": mf.PROPOSED, "concept": parent.name, "species": parent.species,
            "children": [_display(c, table, new_parents) for c in parent.children],
            "justification": parent.justification, "questions": parent.questions,
            "generation": parent.generation, "derived_evidence": parent.evidence,
            "group": parent.group,
        }
    for pkey, reason in dropped.items():
        record = proposals.setdefault(pkey, {})
        record.update(state=mf.REJECTED, reason=reason)
    notes["discarded"] = discarded

    # Montagem e validação do conjunto, com cascata para os pais novos que caem
    excluded: Set[str] = {k for k in new_parents if k not in completed}
    final = run.original
    rejected: Dict[str, List[str]] = {}
    changes: Dict[str, Dict[str, List[str]]] = {}
    new_blocks: Dict[str, str] = {}
    for _ in range(len(new_parents) + 2):
        changes, new_blocks = _assemble(accepted_links, table, new_parents, completed, excluded,
                                        species_field, dest_keys)
        orphan = {k for k in new_parents if k not in excluded
                  and not any(l.parent == k and l.child not in excluded for l in accepted_links.values()
                              if l.child in changes or l.child in new_blocks)}
        if orphan:  # pai novo sem nenhum filho ligado: não se fundamenta
            excluded |= orphan
            for k in orphan:
                proposals[new_parents[k].pkey].update(state=mf.REJECTED, reason="no linked child (not grounded)")
            continue

        def build(accepted: Set[str], _c=changes, _n=new_blocks) -> str:
            return merge_into_syno(run.original, {k: v for k, v in _c.items() if k in accepted},
                                   {k: v for k, v in _n.items() if k in accepted})

        changed_keys = set(changes) | set(new_blocks)
        final, rejected = validate_changes(ctx, run.destination, run.original, build, changed_keys)
        fallen = {k for k in rejected if k in new_blocks}
        if not fallen:
            break
        excluded |= fallen
    if client is not None:
        warn_schema_fallbacks(client)

    def links_of(identity: str) -> List[str]:
        own = [p for p, l in accepted_links.items() if l.child == identity and l.parent not in excluded]
        return own + ([new_parents[identity].pkey] if identity in new_parents else [])

    for k in excluded:
        if k in new_parents:
            for pkey, link in accepted_links.items():
                if link.parent == k:
                    proposals[pkey].update(state=mf.REJECTED, reason=f"the new parent `{new_parents[k].name}` was dropped")

    accepted = sorted(k for k in set(changes) | set(new_blocks) if k not in rejected)
    report = lambda props: _report(run, props, table, notes, calls, time.monotonic() - t0)  # noqa: E731
    if not changes and not new_blocks:
        run.finish_without_change(proposals, stop_reason if stop_reason != "completed" else "no new link",
                                  notes)
        return report(proposals)

    def revalidate(keys: Set[str]) -> Tuple[str, Dict[str, List[str]]]:
        def build(accepted_keys: Set[str]) -> str:
            return merge_into_syno(run.original, {k: v for k, v in changes.items() if k in accepted_keys},
                                   {k: v for k, v in new_blocks.items() if k in accepted_keys})
        return validate_changes(ctx, run.destination, run.original, build, keys)

    return run.commit(final, accepted, rejected, proposals, revalidate, report,
                      stop_reason=stop_reason, notes=notes, proposal_keys=links_of)


def _display(key: str, table: Dict[str, Concept], new_parents: Dict[str, NewParent]) -> str:
    if key in table:
        return table[key].name
    if key in new_parents:
        return new_parents[key].name
    return key


def _add_lines(block: str, lines: List[str]) -> str:
    if not lines:
        return block
    head, sep, tail = block.rpartition("END ONTOLOGY")
    if not sep:
        return block
    if not head.endswith("\n"):
        head += "\n"
    return head + "\n".join(lines) + "\n" + sep + tail


def _derived_evidence(parent: NewParent, table: Dict[str, Concept], ctx: dict) -> dict:
    """Trechos dos descendentes (não só as definições deles), marcados como derivados."""
    children = sorted((table[c] for c in parent.children if c in table), key=lambda c: (-c.items, c.key))
    examples: List[dict] = []
    for child in children:
        sem = _build_semantic_ctx(child.name, ctx)
        for ex in sem.get("examples", [])[:1]:
            examples.append({"narrower concept (derived evidence)": child.name, **ex})
            parent.evidence.append(child.name)
        if len(examples) == 3:
            break
    if not examples:
        examples = [{"narrower concepts (derived evidence)": ", ".join(c.name for c in children)}]
    return {"frequency": sum(c.items for c in children), "sources": sum(c.sources for c in children),
            "relations": [], "co_codes": [], "examples": examples}


def _assemble(links: Dict[str, Link], table: Dict[str, Concept], new_parents: Dict[str, NewParent],
              completed: Dict[str, str], excluded: Set[str], species_field: Dict[str, str],
              dest_keys: Set[str]) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, str]]:
    """Campos HIERARCHY por filho do destino; blocos dos pais novos aceitos."""
    by_child: Dict[str, Dict[str, List[str]]] = {}
    for link in links.values():
        if link.parent in excluded or link.child in new_parents or link.child not in dest_keys:
            continue
        name = _display(link.parent, table, new_parents)
        values = by_child.setdefault(link.child, {}).setdefault(species_field[link.species], [])
        if name not in values:
            values.append(name)
    changes: Dict[str, Dict[str, List[str]]] = {}
    for child, fields in by_child.items():
        changes[child] = {f: [f"    {f}: {', '.join(sorted(v))}\n"] for f, v in fields.items()}
    new_blocks = {k: b for k, b in completed.items() if k not in excluded}
    return changes, new_blocks


def _report(run: PhaseRun, proposals: Dict[str, Dict[str, Any]], table: Dict[str, Concept],
            notes: Dict[str, Any], calls: int, elapsed: float) -> str:
    phase = run.phase
    head = "dry run (--dry-run), nothing written" if run.dry_run else f"destination {run.dest.name}"
    lines = [f"{phase.label} (taxonomy): {head}"]
    lines.append(f"  concepts in the table: {len(table)}; grouping: {notes['grouping']}; "
                 f"groups: {len(notes['groups'])}; calls: {calls}")
    lines.append(f"  parent creation: {notes['parent_creation']}")
    by_state: Dict[str, int] = {}
    by_species: Dict[str, int] = {}
    for key, record in proposals.items():
        by_state[record.get("state", "?")] = by_state.get(record.get("state", "?"), 0) + 1
        if not key.startswith("new:") and record.get("state") in (mf.APPLIED, mf.PROPOSED, mf.APPLYING):
            by_species[record.get("species", "?")] = by_species.get(record.get("species", "?"), 0) + 1
    lines.append("  links by species: " + (", ".join(f"{k} {v}" for k, v in sorted(by_species.items())) or "none"))
    new = [r for k, r in proposals.items() if k.startswith("new:") and r.get("state") != mf.REJECTED]
    if new:
        lines.append(f"  new parents ({len(new)}): " + ", ".join(
            f"{r['concept']} <- {', '.join(r.get('children', []))}"
            + ("" if r.get("questions") else " [no question]") for r in new[:10]))
    rejected = [(k, r) for k, r in proposals.items() if r.get("state") == mf.REJECTED]
    if rejected:
        lines.append(f"  rejected ({len(rejected)}):")
        lines.extend(f"    {k}: {r.get('reason', '')}" for k, r in sorted(rejected)[:15])
    discarded = notes.get("discarded") or []
    if discarded:
        lines.append(f"  model links discarded before validation ({len(discarded)}; see notes.discarded):")
        lines.extend(f"    {d}" for d in discarded[:10])
    with_parent = {k for k, c in table.items() if c.parents}
    with_parent |= {concept_key(str(r.get("child", ""))) for r in proposals.values()
                    if r.get("state") in (mf.APPLIED, mf.PROPOSED) and r.get("child")}
    top = {concept_key(n) for n in notes.get("top", {})}
    unplaced = sorted(table[k].name for k in table if k not in with_parent and k not in top)
    lines.append(f"  top concepts decided: {len(top)}; unplaced (curation): {len(unplaced)}"
                 + (f" — {', '.join(unplaced[:12])}{' ...' if len(unplaced) > 12 else ''}" if unplaced else ""))
    lines.append("  states: " + ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())))
    client = run.client_used
    if client is not None:
        lines.append(f"  {client.usage.summary_line()} | {elapsed:.1f}s")
    return "\n".join(lines) + "\n"
