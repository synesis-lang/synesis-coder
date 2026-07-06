"""schema_builder.py - FieldSpec do template → JSON Schema (Opção 3).

Purpose:
    Converte os campos de um escopo do template (ITEM ou SOURCE) em um JSON
    Schema que o LLM preenche apenas com VALORES. A moldura estrutural do bloco
    Synesis (palavras-chave, nomes de campo, indentação, setas de chain) é
    responsabilidade do block_assembler — o schema descreve só o conteúdo.

    Restrições derivadas do template eliminam classes de erro por construção:
    - ENUMERATED/ORDERED → `enum` (elimina E027/E029)
    - CHAIN → array de hops {source, relation, target} com `relation` por `enum`
      das RELATIONS do template (elimina E008/E010/E011 e a sintaxe da seta)
    - SCALE → integer com minimum/maximum derivados de `[lo..hi]`
    - `additionalProperties: false` → elimina E022 (campo desconhecido)
    - REQUIRED → entra em `required`

Components:
    - build_item_schema(ctx): schema do objeto-ITEM (envelope `items: array`)
    - build_source_schema(ctx): schema do objeto-SOURCE
    - field_to_schema(spec): mapeia um FieldSpec para o fragmento de schema

Dependencies:
    - synesis.ast.nodes: FieldType, FieldSpec (fonte da verdade do template)

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import re
from typing import Optional

from synesis.ast.nodes import FieldSpec, FieldType

# Tipos textuais simples → string
_STRING_TYPES = frozenset(
    {
        FieldType.TEXT,
        FieldType.QUOTATION,
        FieldType.MEMO,
        FieldType.DATE,
        FieldType.TOPIC,
    }
)

# Arity >= N na forma textual do template (ex.: ">= 2")
_ARITY_MIN = re.compile(r">=\s*(\d+)")


def build_abstract_schema(ctx: dict) -> dict:
    """Monta o JSON Schema do envelope SOURCE + items para o modo abstract.

    O LLM preenche `source` (campos do SOURCE) e `items` (array de objetos-ITEM)
    em uma única chamada. O assembler reconstrói os dois blocos separadamente.
    """
    source_object = _scope_object_schema(ctx["source_fields"], ctx.get("required_source", []))
    item_object = _scope_object_schema(ctx["item_fields"], ctx["required_item"])
    return {
        "type": "object",
        "properties": {
            "source": source_object,
            "items": {
                "type": "array",
                "minItems": 1,
                "items": item_object,
            },
        },
        "required": ["source", "items"],
        "additionalProperties": False,
    }


def build_item_schema(ctx: dict) -> dict:
    """Monta o JSON Schema do envelope multi-ITEM para um chunk/documento.

    Um chunk gera N ITEMs; o topo é, portanto, `{"items": [obj, ...]}` em que
    cada `obj` segue o schema de um ITEM. O caminho de 1 ITEM (modo item) é o
    caso degenerado N=1.
    """
    item_object = _scope_object_schema(ctx["item_fields"], ctx["required_item"])
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "items": item_object,
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def build_source_schema(ctx: dict) -> dict:
    """Monta o JSON Schema de um único objeto-SOURCE."""
    return _scope_object_schema(ctx["source_fields"], ctx.get("required_source", []))


def _scope_object_schema(fields: dict, required: list) -> dict:
    """Constrói o schema de um objeto (ITEM ou SOURCE) a partir dos seus campos."""
    properties = {name: field_to_schema(spec) for name, spec in fields.items()}
    required_present = [name for name in required if name in fields]

    schema: dict = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required_present:
        schema["required"] = required_present
    return schema


def field_to_schema(spec: FieldSpec) -> dict:
    """Mapeia um FieldSpec para o fragmento de JSON Schema do seu valor.

    O mapeamento é totalmente derivado do template — nenhum nome de campo,
    relação ou valor é hardcoded.
    """
    ftype = spec.type

    if ftype == FieldType.CODE:
        # Multi-valor: o LLM devolve uma lista; o assembler junta com ", ".
        return {"type": "array", "items": {"type": "string"}}

    if ftype == FieldType.CHAIN:
        return _chain_schema(spec)

    if ftype in (FieldType.ENUMERATED, FieldType.ORDERED):
        enum_values = _enum_values(spec)
        if enum_values:
            return {"enum": enum_values}
        # Sem valores declarados: degradar para string (não inventar enum vazio).
        return {"type": "string"}

    if ftype == FieldType.SCALE:
        return _scale_schema(spec)

    if ftype in _STRING_TYPES:
        return {"type": "string"}

    # Tipo desconhecido para versões futuras do compilador: aceitar string.
    return {"type": "string"}


def _chain_schema(spec: FieldSpec) -> dict:
    """Schema de um campo CHAIN: array de hops {source, relation, target}.

    `relation` é restrita por `enum` às RELATIONS declaradas no template, o que
    elimina E010 (relação inválida) por construção. `minItems` deriva da ARITY
    quando expressa na forma `>= N`.
    """
    relation_schema: dict
    if spec.relations:
        relation_schema = {"enum": sorted(spec.relations.keys())}
    else:
        # Sem RELATIONS no template: força sentinel reservado "__untyped__" para que
        # o assembler saiba que deve omitir o rótulo e produzir chain binária A -> B.
        # O prefixo __ garante não-colisão com qualquer relação legítima de usuário.
        relation_schema = {"const": "__untyped__"}

    hop_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "relation": relation_schema,
            "target": {"type": "string"},
        },
        "required": ["source", "relation", "target"],
        "additionalProperties": False,
    }

    array_schema: dict = {
        "type": "array",
        "items": hop_schema,
    }
    min_items = _arity_min_items(spec.arity)
    if min_items is not None:
        # ARITY de CHAIN conta elementos (conceitos+relações); um hop precisa de
        # ao menos 1 tripla quando há qualquer mínimo. Mantemos >= 1 hop.
        array_schema["minItems"] = 1
    return array_schema


def _scale_schema(spec: FieldSpec) -> dict:
    """Schema de SCALE: integer com minimum/maximum derivados de `[lo..hi]`."""
    bounds = _parse_scale_format(spec.format)
    schema: dict = {"type": "integer"}
    if bounds:
        lo, hi = bounds
        schema["minimum"] = int(lo)
        schema["maximum"] = int(hi)
    return schema


def _enum_values(spec: FieldSpec) -> list:
    """Extrai os labels permitidos de um campo ENUMERATED/ORDERED."""
    if not spec.values:
        return []
    return [v.label for v in spec.values]


def _arity_min_items(arity: Optional[str]) -> Optional[int]:
    """Extrai N de uma ARITY na forma `>= N`. Retorna None se ausente/outra forma."""
    if not arity:
        return None
    m = _ARITY_MIN.search(arity)
    if m:
        return int(m.group(1))
    return None


def _parse_scale_format(fmt: Optional[str]) -> Optional[tuple[float, float]]:
    """Parseia `[lo..hi]` → (lo, hi). Espelha synesis.semantic.validator."""
    if not fmt:
        return None
    if not fmt.startswith("[") or ".." not in fmt or not fmt.endswith("]"):
        return None
    try:
        inner = fmt[1:-1]
        left, right = inner.split("..", 1)
        return float(left), float(right)
    except ValueError:
        return None
