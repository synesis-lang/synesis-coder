"""
vocabulary.py - Tabela de conceitos para as fases de vocabulário (B4, B5)

Propósito:
    As fases cuja unidade é o vocabulário inteiro (taxonomia e sinônimos;
    estudo §6.4) trabalham sobre uma tabela: cada conceito com bloco em algum
    .syno do projeto, com a definição (pelo papel), o tópico, os demais campos
    de classificação, os pais e os rótulos já declarados e o uso no corpus.

Componentes principais:
    - Concept / build_table: a tabela, lida do TEXTO dos .syno (o destino e os
      demais), como a mesclagem a lê; nada é compilado de novo
    - field_value / field_list: valor de um campo no texto de um bloco
    - similarity_key: chave de semelhança de nome (separadores, caixa,
      acentos, plural simples), para candidatos a sinônimo
    - ask_json: chamada com JSON Schema, com volta ao texto livre

Notas:
    A identidade de conceito é a do compilador (`concept_key`); a chave de
    semelhança só propõe candidatos e nunca decide identidade.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from synesis.ast.nodes import FieldType

from synesis_coder.syno_merge import Block, block_entries, concept_key, existing_blocks

CLASSIFICATION_TYPES = (FieldType.TOPIC, FieldType.ENUMERATED, FieldType.ORDERED)


def field_value(block: Block, name: str) -> str:
    """Texto do campo no bloco (linhas de continuação unidas por espaço)."""
    for entry in block_entries(block)[1]:
        if entry.name != name:
            continue
        first = entry.lines[0].split(":", 1)[1] if ":" in entry.lines[0] else ""
        parts = [first.strip()] + [line.strip() for line in entry.lines[1:]]
        return " ".join(p for p in parts if p)
    return ""


def field_list(block: Block, name: str) -> List[str]:
    """Valores de um campo de lista (CODE, rótulos), separados por vírgula."""
    value = field_value(block, name)
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Concept:
    """Uma linha da tabela."""

    name: str
    key: str
    file: Path
    block: Block
    definition: str = ""
    classes: Dict[str, str] = field(default_factory=dict)  # TOPIC/ENUMERATED/ORDERED
    parents: Dict[str, List[str]] = field(default_factory=dict)  # campo HIERARCHY -> pais
    labels: Dict[str, List[str]] = field(default_factory=dict)  # campo LABEL -> rótulos
    items: int = 0  # ITEMs que citam o conceito (CODE ou CHAIN)
    sources: int = 0

    def all_labels(self) -> List[str]:
        return [v for values in self.labels.values() for v in values]


def roles_by_field(ctx: dict) -> Dict[str, str]:
    return {n: str(getattr(s, "role", None) or "") for n, s in ctx.get("ontology_fields", {}).items()}


def use_field(ctx: dict) -> Optional[str]:
    """Campo com ROLE USE (etapa U), se o template declara."""
    return next((n for n, r in roles_by_field(ctx).items() if r == "USE"), None)


def is_alias_block(block: Block, ctx: dict) -> bool:
    """Bloco preterido: tem valor no campo USE. Não é conceito para as fases."""
    name = use_field(ctx)
    return bool(name and field_value(block, name))


def build_table(ctx: dict, texts: Dict[Path, str]) -> Dict[str, Concept]:
    """Conceitos com bloco em algum dos textos, na ordem dos arquivos dados.

    Um conceito em dois arquivos fica com o primeiro (o destino vem primeiro).
    """
    specs = ctx.get("ontology_fields", {})
    roles = roles_by_field(ctx)
    definition = next((n for n, r in roles.items() if r == "NOTE DEFINITION"), None)
    if definition is None:
        definition = next((n for n, s in specs.items()
                           if s.type == FieldType.TEXT and not roles.get(n)), None)
    classes = [n for n, s in specs.items() if s.type in CLASSIFICATION_TYPES]
    hierarchy = [n for n, r in roles.items() if r.startswith("HIERARCHY")]
    labels = [n for n, r in roles.items() if r.startswith("LABEL")]
    occ = ctx.get("_concept_occurrences") or {}

    table: Dict[str, Concept] = {}
    for path, text in texts.items():
        for key, block in existing_blocks(text).items():
            if key in table or is_alias_block(block, ctx):
                continue  # o preterido (USE) não é conceito da tabela
            uses = occ.get(key, [])
            table[key] = Concept(
                name=block.concept, key=key, file=path, block=block,
                definition=field_value(block, definition) if definition else "",
                classes={c: field_value(block, c) for c in classes if field_value(block, c)},
                parents={h: field_list(block, h) for h in hierarchy if field_list(block, h)},
                labels={lab: field_list(block, lab) for lab in labels if field_list(block, lab)},
                items=len(uses), sources=len({b for b, _i, _t in uses}),
            )
    return table


def chains_between(ctx: dict, keys: set) -> List[Tuple[str, str, str]]:
    """Triplas das CHAINs cujos dois extremos estão na tabela (sem repetição)."""
    result = ctx.get("result")
    linked = getattr(result, "linked_project", None) if result is not None else None
    triples = getattr(linked, "all_triples", None) or []
    seen = set()
    out: List[Tuple[str, str, str]] = []
    for a, r, b in triples:
        t = (str(a), str(r), str(b))
        if t in seen or concept_key(t[0]) not in keys or concept_key(t[2]) not in keys:
            continue
        seen.add(t)
        out.append(t)
    return sorted(out)


# ---------------------------------------------------------------------------
# Semelhança de nome
# ---------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[\s_\-./]+")


def similarity_key(name: str) -> str:
    """Chave para candidatos: sem acento, caixa, separadores e plural simples.

    `pig-butchering`, `pig_butchering` e `Pig Butchering` dão a mesma chave;
    `phishings` e `phishing` também. Só PROPÕE: a decisão é do modelo ou do
    pesquisador.
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    words = [w for w in _SEPARATORS.split(text) if w]
    return "".join(_singular(w) for w in words)


# Plurais regulares do português e do inglês (sem acento: "ões" já é "oes").
_PLURALS = (("ais", "al"), ("eis", "el"), ("ois", "ol"), ("oes", "ao"), ("aes", "ao"),
            ("ns", "m"), ("ies", "y"))


def _singular(word: str) -> str:
    if len(word) <= 3:
        return word
    for suffix, repl in _PLURALS:
        if word.endswith(suffix):
            return word[: -len(suffix)] + repl
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


# ---------------------------------------------------------------------------
# Chamada estruturada
# ---------------------------------------------------------------------------


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


async def ask_json(client: Any, messages: List[dict], schema: dict) -> Optional[dict]:
    """JSON Schema quando o backend aceita; senão texto livre com o JSON extraído."""
    if client.supports_json_schema():
        data = await client.call_json_async(messages, schema, temperature=0.0)
        if isinstance(data, dict):
            return data
    text = await client.call_async(messages, temperature=0.0)
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def project_context_text(ctx: dict) -> str:
    """DESCRIPTION e QUESTIONS do projeto, como toda fase recebe (§6.2)."""
    return str(ctx.get("project_description") or "").strip()
