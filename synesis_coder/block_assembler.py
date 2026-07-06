"""block_assembler.py - dict de valores → texto Synesis determinístico (Opção 3).

Purpose:
    Monta a moldura estrutural inteira de blocos SOURCE/ITEM a partir do template
    e de um bibref já validado. O LLM contribui APENAS valores (via JSON); o Python
    emite palavras-chave de bloco, nomes de campo, indentação, `@{bibref}` e as
    setas `->` de chains. Isso elimina por construção fence markdown, campo
    desconhecido (E022), separador errado de CODE (E033/E015) e sintaxe de chain.

Components:
    - assemble_items(ctx, bibref, data): envelope {"items": [...]} → N blocos ITEM
    - assemble_source(ctx, bibref, data): objeto → um bloco SOURCE
    - _render_field(spec, value): valor tipado → linha(s) Synesis

Dependencies:
    - synesis.ast.nodes: FieldType (despacho por tipo de campo)

Generated conforming to: Synesis Specification v1.1
"""

from __future__ import annotations

import re
from typing import List

from synesis.ast.nodes import FieldType

_INDENT = "    "  # 4 espaços — indentação canônica de campo Synesis


def assemble_items(ctx: dict, bibref: str, data: dict) -> str:
    """Monta N blocos ITEM a partir do envelope JSON `{"items": [...]}`.

    Args:
        ctx: Contexto do projeto (define item_fields como fonte da verdade).
        bibref: Referência já validada (sem `@`); o assembler prefixa `@`.
        data: Dict do LLM. Aceita `{"items": [obj, ...]}` ou um único objeto
            (tratado como N=1) por robustez.

    Returns:
        String com os blocos `ITEM @bibref ... END ITEM` concatenados.
    """
    items = _extract_item_list(data)
    fields = ctx["item_fields"]
    required = set(ctx.get("required_item", []))
    blocks = [_assemble_block("ITEM", bibref, fields, obj, required) for obj in items]
    return "\n\n".join(blocks)


def assemble_source(ctx: dict, bibref: str, data: dict) -> str:
    """Monta um único bloco SOURCE a partir de um objeto de valores."""
    fields = ctx["source_fields"]
    required = set(ctx.get("required_source", []))
    return _assemble_block("SOURCE", bibref, fields, data, required)


def _extract_item_list(data: dict) -> List[dict]:
    """Normaliza a entrada para uma lista de objetos-ITEM."""
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        return [obj for obj in data["items"] if isinstance(obj, dict)]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [obj for obj in data if isinstance(obj, dict)]
    return []


_NA = "NA"  # sentinel para campos REQUIRED que o LLM não preencheu


def _assemble_block(
    keyword: str, bibref: str, fields: dict, obj: dict, required: set
) -> str:
    """Emite um bloco `KEYWORD @bibref ... END KEYWORD` para um objeto de valores.

    Campos são emitidos na ordem do template. Chaves extras no JSON (não
    presentes em `fields`) são ignoradas — a moldura nunca produz campo
    desconhecido. Campos OPTIONAL ausentes no JSON são simplesmente omitidos.
    Campos REQUIRED ausentes ou vazios recebem o valor "NA" (Not Available),
    garantindo conformidade estrutural mesmo quando o LLM não extrai o dado.
    """
    bibref = bibref.lstrip("@").strip()
    lines: List[str] = [f"{keyword} @{bibref}"]

    for name, spec in fields.items():
        value = obj.get(name)
        if not _has_value(value):
            if name in required:
                lines.append(_INDENT + f"{name}: {_NA}")
            continue
        for line in _render_field(name, spec, value):
            lines.append(_INDENT + line)

    lines.append(f"END {keyword}")
    return "\n".join(lines)


def _render_field(name: str, spec, value) -> List[str]:
    """Converte um valor tipado em uma ou mais linhas `campo: valor` Synesis."""
    ftype = spec.type

    if ftype == FieldType.CODE:
        joined = _render_code(value)
        return [f"{name}: {joined}"] if joined else []

    if ftype == FieldType.CHAIN:
        return [f"{name}: {chain_str}" for chain_str in _render_chains(value)]

    # Demais tipos: valor escalar textualizado, newlines normalizadas.
    rendered = _scalar_to_text(value)
    if not rendered:
        return []
    return [f"{name}: {rendered}"]


def _render_code(value) -> str:
    """Junta valores CODE com `, ` (separador exigido pelo compilador).

    Aceita lista (caminho JSON) ou string (caminho de fallback/texto livre).
    Normaliza para lowercase para coincidir com normalize_code() do compilador,
    evitando que variantes de case (ex: Graduacao_Curso / graduacao_curso)
    apareçam como códigos distintos no code_index.
    """
    if isinstance(value, list):
        parts = [str(v).strip().lower() for v in value if str(v).strip()]
        return ", ".join(parts)
    return str(value).strip().lower()


def _render_chains(value) -> List[str]:
    """Converte hops {source, relation, target} em strings `A -> rel -> B`.

    Hops contíguos (`target[i] == source[i+1]`) são interleavados numa só linha
    multi-hop (`A -> r1 -> B -> r2 -> C`). O Python insere todas as setas — o
    LLM nunca digita `->`. Conceitos são normalizados (snake_case, sem espaços)
    como defesa extra contra E015, já que source/target são texto livre.
    """
    if not isinstance(value, list):
        # Fallback: string de chain já formada pelo LLM (caminho texto livre).
        return [str(value).strip()] if str(value).strip() else []

    hops = [h for h in value if isinstance(h, dict)]
    if not hops:
        return []

    lines: List[str] = []
    current: List[str] = []  # elementos interleavados do encadeamento atual

    for hop in hops:
        src = _normalize_concept(hop.get("source", ""))
        rel = str(hop.get("relation", "")).strip()
        tgt = _normalize_concept(hop.get("target", ""))
        if not (src and tgt):
            continue

        # Omite o rótulo quando vazio ou é o sentinel "__untyped__" (injetado pelo
        # schema_builder em templates sem RELATIONS). Qualquer outro valor —
        # incluindo "linked_to" — é tratado como relação legítima e preservado.
        if rel and rel != "__untyped__":
            elements = [rel, tgt]
        else:
            elements = [tgt]

        if current and current[-1] == src:
            current.extend(elements)
        else:
            if current:
                lines.append(" -> ".join(current))
            current = [src] + elements

    if current:
        lines.append(" -> ".join(current))

    return lines


def _scalar_to_text(value) -> str:
    """Textualiza um valor escalar, normalizando newlines que quebrariam o parser."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        # Lista para campo não-CODE/não-CHAIN: juntar com vírgula como degradação.
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    text = str(value).strip()
    # O parser LALR é orientado a linha; colapsar quebras internas em espaço.
    text = re.sub(r"\s*\n\s*", " ", text)
    return text


def _normalize_concept(s: str) -> str:
    """Normaliza um conceito de chain: trim, lowercase, sem espaços (snake_case)."""
    s = str(s).strip().lower()
    if not s:
        return ""
    return re.sub(r"\s+", "_", s)


def _has_value(value) -> bool:
    """True se o valor é preenchido (espelha synesis.semantic.validator._has_value)."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, list):
        return len(value) > 0
    return True
