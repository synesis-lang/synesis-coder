"""`synesis-coder review`: avaliação por amostragem de uma fase (estudo §6.7).

    synesis-coder review <projeto.synp> <fase> --unit edge --sample 100 --stratify depth
    synesis-coder review <projeto.synp> <fase> --score <amostra julgada>.csv

- **População:** propostas APLICADAS do manifesto da fase (arestas para
  HIERARCHY, rótulos para LABEL, conceitos para pais novos).
- **Amostra estratificada** com probabilidade de inclusão conhecida
  (n_h/N_h em cada estrato); o peso analítico é o inverso dela. Pais novos que
  não servem a nenhuma questão formam um estrato próprio.
- **Precisão estimada** com esses pesos (estimador estratificado, IC de 95%);
  a **precisão ponderada por ITEMs alcançados** (importância da aresta) vem
  à parte, como métrica distinta.
- O resultado é um relatório: a anotação não muda.
"""

from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from synesis_coder import manifest as mf
from synesis_coder.modes.phase_mode import ensure_occurrences
from synesis_coder.phases import load_phase_plan, phase_output
from synesis_coder.project_loader import load_project
from synesis_coder.syno_merge import concept_key
from synesis_coder.vocabulary import build_table

UNITS = ("edge", "label", "concept")
STRATA = ("depth", "topic", "origin", "none")
YES = {"correct", "c", "yes", "y", "1", "ok", "true", "correto", "sim", "s"}
NO = {"wrong", "w", "no", "n", "0", "false", "errado", "e", "nao", "não"}

COLUMNS = ["id", "stratum", "inclusion_prob", "weight", "items_reached", "unit",
           "child_or_concept", "species_or_kind", "parent_or_label", "justification",
           "judgement", "direction_ok", "species_ok", "note"]


def _manifest(project_path: Path, phase_name: str, output: Optional[Path]) -> mf.Manifest:
    plan = load_phase_plan(project_path)
    phase = plan.find(phase_name)
    dest = Path(output).resolve() if output else phase_output(plan, phase)
    if dest is None:
        from synesis_coder.ontology_publish import resolve_destination
        dest = resolve_destination(project_path, None).path
    m = mf.Manifest.load(mf.manifest_path(dest, phase.name), phase.name, dest)
    if not m.exists:
        raise ValueError(f"No manifest of phase `{phase.name}` for {dest.name}: run the phase first.")
    return m


def _population(m: mf.Manifest, unit: str) -> List[Tuple[str, Dict[str, Any]]]:
    out = []
    for key, record in sorted(m.proposals.items()):
        if record.get("state") != mf.APPLIED:
            continue
        if unit == "edge" and record.get("child") and record.get("parent"):
            out.append((key, record))
        elif unit == "label" and record.get("term"):
            out.append((key, record))
        elif unit == "concept" and key.startswith("new:"):
            out.append((key, record))
    return out


def _reach(project_path: Path, edges: List[Dict[str, Any]]) -> Tuple[Dict[str, Set[Any]], Dict[str, str], Dict[str, int]]:
    """ITEMs da subárvore de cada conceito (união), tópico e profundidade."""
    ctx = load_project(project_path, load_annotations=True, load_ontology=False,
                       tolerate_annotation_errors=True)
    ensure_occurrences(ctx)
    occ = ctx.get("_concept_occurrences") or {}
    items: Dict[str, Set[Any]] = {k: {(b, id(i)) for b, i, _t in v} for k, v in occ.items()}
    children: Dict[str, Set[str]] = defaultdict(set)
    parents: Dict[str, Set[str]] = defaultdict(set)
    texts = {}
    for inc in _ontology_files(ctx):
        try:
            texts[inc] = inc.read_text(encoding="utf-8")
        except OSError:
            continue
    table = build_table(ctx, texts)
    for key, concept in table.items():
        for values in concept.parents.values():
            for p in values:
                children[concept_key(p)].add(key)
                parents[key].add(concept_key(p))
    for e in edges:
        c, p = concept_key(str(e["child"])), concept_key(str(e["parent"]))
        children[p].add(c)
        parents[c].add(p)

    memo: Dict[str, Set[Any]] = {}

    def subtree(k: str, seen: frozenset = frozenset()) -> Set[Any]:
        if k in memo:
            return memo[k]
        out = set(items.get(k, set()))
        for c in children.get(k, ()):
            if c not in seen:
                out |= subtree(c, seen | {k})
        memo[k] = out
        return out

    def depth(k: str, seen: frozenset = frozenset()) -> int:
        ps = [p for p in parents.get(k, ()) if p not in seen]
        return 0 if not ps else 1 + max(depth(p, seen | {k}) for p in ps)

    reach = {k: subtree(k) for k in set(items) | set(children) | set(parents)}
    topic_field = next((n for n, s in ctx.get("ontology_fields", {}).items()
                        if s.type.name == "TOPIC"), None)
    topics = {k: c.classes.get(topic_field, "") for k, c in table.items()} if topic_field else {}
    depths = {k: depth(k) for k in reach}
    return reach, topics, depths


def _ontology_files(ctx: dict) -> List[Path]:
    from synesis_coder.project_loader import project_include_files
    return [i["path"] for i in project_include_files(Path(ctx["project_path"])) if i["type"] == "ONTOLOGY"]


def sample(project_path: Path, phase_name: str, *, unit: str, size: int, stratify: str,
           seed: int = 1, output: Optional[Path] = None, csv_path: Optional[Path] = None) -> str:
    if unit not in UNITS:
        raise ValueError(f"--unit {unit}: use {', '.join(UNITS)}.")
    if stratify not in STRATA:
        raise ValueError(f"--stratify {stratify}: use {', '.join(STRATA)}.")
    m = _manifest(project_path, phase_name, output)
    population = _population(m, unit)
    if not population:
        return f"No applied proposal of unit `{unit}` in the manifest of `{phase_name}`.\n"
    edges = [r for _k, r in _population(m, "edge")]
    reach, topics, depths = _reach(project_path, edges)

    def subject(record: Dict[str, Any]) -> str:
        return concept_key(str(record.get("child") or record.get("concept") or ""))

    def stratum(key: str, record: Dict[str, Any]) -> str:
        if key.startswith("new:") and not record.get("questions"):
            return "new parent without a question"
        if stratify == "depth":
            return f"depth {depths.get(subject(record), 0)}"
        if stratify == "topic":
            return topics.get(subject(record), "") or "(no topic)"
        if stratify == "origin":
            if key.startswith("new:"):
                return "new parent"
            parent = concept_key(str(record.get("parent", "")))
            return "new parent" if f"new:{parent}" in m.proposals else f"pass {record.get('pass', 1)}"
        return "single"

    strata: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for key, record in population:
        strata[stratum(key, record)].append((key, record))
    total = len(population)
    size = min(size, total)
    # Alocação proporcional, ao menos 1 por estrato enquanto houver amostra.
    alloc = {h: max(1, round(size * len(units) / total)) for h, units in strata.items()}
    while sum(alloc.values()) > size and any(v > 1 for v in alloc.values()):
        h = max(alloc, key=lambda x: alloc[x])
        alloc[h] -= 1
    rng = random.Random(seed)
    rows = []
    for h in sorted(strata):
        units = strata[h]
        n_h = min(alloc[h], len(units))
        prob = n_h / len(units)
        for key, record in sorted(rng.sample(units, n_h)):
            reached = len(reach.get(subject(record), set()))
            rows.append({
                "id": key, "stratum": h, "inclusion_prob": f"{prob:.6f}", "weight": f"{1 / prob:.6f}",
                "items_reached": reached, "unit": unit,
                "child_or_concept": record.get("child") or record.get("concept") or "",
                "species_or_kind": record.get("species") or record.get("kind") or "",
                "parent_or_label": record.get("parent") or record.get("term")
                or ", ".join(record.get("children", [])),
                "justification": record.get("test") or record.get("justification") or record.get("reason") or "",
                "judgement": "", "direction_ok": "", "species_ok": "", "note": "",
            })
    target = csv_path or m.path.with_name(m.path.name.replace(".manifest.json", f".review_{unit}.csv"))
    with open(target, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    lines = [f"Sample of {len(rows)} out of {total} ({unit}), strata by {stratify}, seed {seed}:"]
    lines += [f"  {h}: {min(alloc[h], len(strata[h]))} of {len(strata[h])}" for h in sorted(strata)]
    lines.append(f"File for judgement: {target}")
    lines.append("Fill in `judgement` (correct/wrong) and, for edges, `direction_ok` and `species_ok`; "
                 "then: synesis-coder review ... --score <file>.")
    return "\n".join(lines) + "\n"


def _judgement(value: str) -> Optional[int]:
    v = (value or "").strip().casefold()
    if v in YES:
        return 1
    if v in NO:
        return 0
    return None


def _stratified(rows: List[Dict[str, str]], column: str) -> Optional[Dict[str, Any]]:
    """Estimador estratificado da proporção, com os limites declarados.

    R6 da avaliação de 2026-11-17: um julgamento só, de um estrato de 100,
    saía como "100.0% (IC ±0.0%)". A largura zero vinha da FALTA de informação,
    não de certeza. Agora:

    - N_h é a soma dos pesos das linhas sorteadas do estrato (= tamanho do
      estrato, porque todas pesam 1/p_h). Censo: p_h = 1 e todos julgados —
      aí não há erro amostral, e variância zero é correta.
    - Estrato amostral com menos de 2 julgados: sem informação para variância.
      O IC NÃO é publicado; a saída diz qual estrato falta.
    - Proporção extrema (0% ou 100%) num estrato amostral: a variância usa o
      ajuste de Agresti–Coull, p~ = (y + 2) / (n + 4), para que a ausência de
      discordância na amostra não vire certeza.
    - Não resposta: linhas sorteadas e não julgadas são tratadas como ausentes ao
      acaso dentro do estrato; estratos sem nenhum julgamento ficam FORA da
      estimativa, e a fração da população que eles representam é declarada.
    """
    size: Dict[str, float] = defaultdict(float)
    sampled: Dict[str, int] = defaultdict(int)
    census: Dict[str, bool] = {}
    judged: Dict[str, List[int]] = defaultdict(list)
    for row in rows:
        h = row["stratum"]
        size[h] += float(row["weight"])
        sampled[h] += 1
        census[h] = census.get(h, True) and float(row.get("inclusion_prob") or 0) >= 0.999999
        y = _judgement(row.get(column, ""))
        if y is not None:
            judged[h].append(y)
    if not judged:
        return None

    population = sum(size.values())
    covered = sum(size[h] for h in judged)
    estimate, variance = 0.0, 0.0
    insufficient: List[str] = []
    adjusted: List[str] = []
    census_strata: List[str] = []
    for h, values in sorted(judged.items()):
        n_h, big_n = len(values), size[h]
        share = big_n / covered
        p_h = sum(values) / n_h
        estimate += share * p_h
        if census[h] and n_h == sampled[h]:
            census_strata.append(h)  # todo o estrato julgado: sem erro amostral
            continue
        if n_h < 2:
            insufficient.append(h)
            continue
        p_var = p_h
        if p_h in (0.0, 1.0):
            p_var = (sum(values) + 2) / (n_h + 4)
            adjusted.append(h)
        fpc = max(0.0, 1 - n_h / big_n)
        variance += share ** 2 * fpc * p_var * (1 - p_var) / (n_h - 1)
    return {
        "estimate": estimate,
        "half_width": None if insufficient else 1.96 * math.sqrt(variance),
        "judged": sum(len(v) for v in judged.values()),
        "sampled": sum(sampled[h] for h in judged),
        "insufficient": insufficient,
        "adjusted": adjusted,
        "census": census_strata,
        "unjudged_strata": sorted(h for h in size if h not in judged),
        "uncovered_share": 1 - covered / population if population else 0.0,
    }


def score(csv_path: Path) -> str:
    with open(csv_path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return "Empty sample file.\n"
    lines = [f"Evaluation of {csv_path.name} ({rows[0].get('unit', '?')}):"]
    notes: List[str] = []
    for column, label in (("judgement", "precision"), ("direction_ok", "direction precision"),
                          ("species_ok", "species precision")):
        r = _stratified(rows, column)
        if r is None:
            continue
        counts = f"{r['judged']} judged of {r['sampled']} sampled; inclusion weights"
        if r["half_width"] is None:
            lines.append(
                f"  {label}: {r['estimate']:.1%} (95% CI not reported — stratum with fewer than 2 "
                f"judgements: {', '.join(r['insufficient'])}; {counts})"
            )
        else:
            lines.append(f"  {label}: {r['estimate']:.1%} (95% CI ±{r['half_width']:.1%}; {counts})")
        if column != "judgement":
            continue
        if r["unjudged_strata"]:
            notes.append(
                f"  strata with no judgement, left out of the estimate: {', '.join(r['unjudged_strata'])} "
                f"({r['uncovered_share']:.0%} of the population) — the estimate covers only the judged strata"
            )
        if r["adjusted"]:
            notes.append(
                "  extreme proportion (0% or 100%) in a sampled stratum: the CI uses the Agresti–Coull "
                f"adjustment there ({', '.join(r['adjusted'])})"
            )
        if r["census"]:
            notes.append(f"  census strata (every unit judged, no sampling error): {', '.join(r['census'])}")
    judged_rows: List[Tuple[Dict[str, str], int]] = []
    for row in rows:
        y = _judgement(row.get("judgement", ""))
        if y is not None:
            judged_rows.append((row, y))
    num = sum(float(row["weight"]) * float(row["items_reached"] or 0) * y for row, y in judged_rows)
    den = sum(float(row["weight"]) * float(row["items_reached"] or 0) for row, _y in judged_rows)
    if den:
        lines.append(f"  precision weighted by ITEMs reached (separate metric): {num / den:.1%}")
    missing = sum(1 for row in rows if _judgement(row.get("judgement", "")) is None)
    if missing:
        notes.append(f"  not judged: {missing} row(s), treated as missing at random within their stratum")
    return "\n".join(lines + notes) + "\n"
