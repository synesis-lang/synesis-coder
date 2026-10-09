"""concept_index.py - A lista de conceitos existentes durante uma campanha.

Purpose:
    A seção "EXISTING PROJECT CONCEPTS" é o mecanismo de reuso do coder: com o
    Sonnet, a lista plana produziu menos conceitos novos por fonte que uma
    vizinhança por embedding (Estudo_Memoria_Processamento_Coder). Mas ela não
    estava em todo modo nem acompanhava a campanha:
      - o `dataset` carregava o projeto SEM anotações — lista vazia;
      - o `document` carregava uma vez, e os trechos em paralelo não viam os
        conceitos uns dos outros.

    Este módulo dá as peças, lendo os campos pelo TIPO declarado no template
    (CODE e CHAIN abertos no ITEM), nunca pelo nome:
      - `has_open_term_fields`: o template tem vocabulário aberto a reusar?
      - `concepts_from_item_blocks`: conceitos dos blocos ITEM já VALIDADOS;
      - `with_concepts`: nova VERSÃO da lista, sem alterar a anterior;
      - `refresh_every`: tamanho do lote que usa a mesma versão.

    Regra de versionamento (para o contexto não depender da ordem em que as
    chamadas paralelas terminam): todas as chamadas de um lote usam a MESMA
    versão; só resultados concluídos e válidos entram na versão seguinte.

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from typing import Iterable

from synesis.ast.nodes import FieldType

_log = logging.getLogger(__name__)

_FIELD_LINE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.+?)\s*$")
_ARROW = re.compile(r"\s*->\s*")


def open_term_fields(item_fields: dict) -> dict:
    """{nome: spec} dos campos CODE/CHAIN do ITEM sem lista fechada (`values`)."""
    return {
        name: spec
        for name, spec in (item_fields or {}).items()
        if spec.type in (FieldType.CODE, FieldType.CHAIN) and not getattr(spec, "values", None)
    }


def has_open_term_fields(item_fields: dict) -> bool:
    return bool(open_term_fields(item_fields))


def _chain_concepts(value: str, qualified: bool) -> list[str]:
    parts = [p.strip() for p in _ARROW.split(value) if p.strip()]
    # Cadeia qualificada (campo com RELATIONS): conceito, relação, conceito...
    return parts[0::2] if qualified else parts


def concepts_from_item_blocks(blocks: Iterable[str], item_fields: dict) -> Counter:
    """Conceitos usados nos blocos ITEM, contados por ITEM DISTINTO.

    Lê só os campos de termo abertos, identificados pelo tipo no template. Um
    conceito repetido em várias relações do mesmo ITEM conta uma vez.
    """
    fields = {n.lower(): s for n, s in open_term_fields(item_fields).items()}
    total: Counter = Counter()
    for block in blocks:
        seen: set[str] = set()
        for line in str(block).splitlines():
            m = _FIELD_LINE.match(line)
            if not m:
                continue
            spec = fields.get(m.group(1).lower())
            if spec is None:
                continue
            value = m.group(2)
            if spec.type == FieldType.CHAIN:
                seen.update(_chain_concepts(value, bool(getattr(spec, "relations", None))))
            else:
                seen.update(c.strip() for c in value.split(",") if c.strip())
        total.update(seen)
    return total


def with_concepts(code_index: dict, new: Counter) -> dict:
    """Nova versão do code_index com os conceitos acrescentados (a anterior fica intacta)."""
    stats = dict((code_index or {}).get("stats") or {})
    for concept, n in new.items():
        stats[concept] = stats.get(concept, 0) + n
    return {"codes": sorted(stats), "stats": stats, "empty": not stats}


def refresh_every(concurrent: int) -> int:
    """Trechos por versão da lista. `SYNESIS_CODER_CONCEPT_REFRESH`: 0 desliga.

    Padrão: max(10, concurrent). Cada versão nova muda o system prompt e quebra
    o cache de prompt (como já acontece no `abstract` a cada lote); lotes de 10
    limitam esse custo.
    """
    raw = os.environ.get("SYNESIS_CODER_CONCEPT_REFRESH", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            _log.warning("SYNESIS_CODER_CONCEPT_REFRESH=%r ignorado (use um inteiro)", raw)
    return max(10, concurrent)


# ---------------------------------------------------------------------------
# Ocorrências dos conceitos nos ITEMs já compilados (para o modo ontology)
# ---------------------------------------------------------------------------

# Atributos do ItemNode que o parser preenche só para os nomes tradicionais;
# qualquer outro nome fica em `extra_fields`. Espelha
# synesis.semantic.linker._get_item_field_value.
_ITEM_ALIASES = {
    "quote": "quote", "quotation": "quote",
    "code": "codes", "codes": "codes",
    "note": "notes", "notes": "notes", "memo": "notes", "memos": "notes",
    "chain": "chains", "chains": "chains",
}


def item_field_value(item, name: str):
    """Valor de um campo do ItemNode pelo NOME DECLARADO no template."""
    extra = getattr(item, "extra_fields", None) or {}
    if extra.get(name) is not None:
        return extra[name]
    attr = _ITEM_ALIASES.get(name.lower())
    return getattr(item, attr, None) if attr else None


def _as_list(value) -> list:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _concept_key(concept: str) -> str:
    """Igualdade do contrato da linguagem (`normalize_code`): caixa e espaços."""
    return " ".join(str(concept).strip().split()).lower()


def item_concepts(item, item_fields: dict) -> dict[str, list[tuple[str, str]]]:
    """{campo: [(conceito, texto do valor)]} dos campos de termo do ITEM, pelo TIPO.

    CHAIN: os conceitos de cada cadeia (posições pares se o campo declara
    RELATIONS; todos os elementos se é cadeia simples). CODE: os valores,
    separados por vírgula quando chegam como texto único.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for name, spec in (item_fields or {}).items():
        if spec.type not in (FieldType.CODE, FieldType.CHAIN):
            continue
        pares: list[tuple[str, str]] = []
        for v in _as_list(item_field_value(item, name)):
            if spec.type == FieldType.CHAIN:
                qualified = bool(getattr(spec, "relations", None))
                nodes = [str(n) for n in (getattr(v, "nodes", None) or [])]
                if not nodes:
                    nodes = [p.strip() for p in _ARROW.split(str(v)) if p.strip()]
                texto = " -> ".join(nodes)
                for c in (nodes[0::2] if qualified else nodes):
                    pares.append((c, texto))
            else:
                for c in str(v).split(","):
                    if c.strip():
                        pares.append((c.strip(), c.strip()))
        if pares:
            out[name] = pares
    return out


def concept_occurrences(linked, item_fields: dict) -> dict[str, list[tuple[str, object, dict]]]:
    """{chave do conceito: [(bibref, ItemNode, campos de termo do ITEM)]}.

    Lê TODOS os campos CODE/CHAIN do ITEM pelo tipo — o `code_usage` do
    compilador só cobre CODE, e um projeto só-CHAIN (face85, Social
    Acceptance) ficava sem ocorrência nenhuma.
    """
    occ: dict[str, list[tuple[str, object, dict]]] = {}
    for bibref, src in (getattr(linked, "sources", None) or {}).items():
        for item in getattr(src, "items", None) or []:
            termos = item_concepts(item, item_fields)
            vistos: set[str] = set()
            for pares in termos.values():
                for conceito, _texto in pares:
                    k = _concept_key(conceito)
                    if k in vistos:
                        continue
                    vistos.add(k)
                    occ.setdefault(k, []).append((str(bibref), item, termos))
    return occ


def definition_field(ontology_fields: dict) -> str | None:
    """Campo TEXT do ONTOLOGY que DEFINE o conceito, escolhido pela DESCRIÇÃO.

    O nome varia por projeto (`ontology_description` no face85 e no Social
    Acceptance, mas não é contrato da linguagem). Vale o campo cuja descrição
    declara definição ("defin…"); sem isso, o único TEXT do ONTOLOGY; senão, None.
    """
    textos = [
        (name, spec) for name, spec in sorted((ontology_fields or {}).items())
        if spec.type == FieldType.TEXT
    ]
    for name, spec in textos:
        if re.search(r"defin", getattr(spec, "description", None) or "", re.I):
            return str(name)
    return str(textos[0][0]) if len(textos) == 1 else None
