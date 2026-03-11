"""Construção de prompts para o synesis-coder.

Funções puras que recebem o contexto do projeto (ctx) e retornam
listas de mensagens no formato interno agnóstico ao provedor:
    [{"role": str, "content": str, "cache": bool}]

O system prompt é construído uma única vez por sessão (estático/cacheável).
Apenas bibref e texto variam por chamada (dinâmico/não cacheado).

Hierarquia de instruções por campo:
    1. spec.guidelines  (instrução metodológica do autor do template)
    2. spec.description (descrição do campo)
    3. instrução genérica baseada em spec.type
"""

from __future__ import annotations

from typing import List

from synesis.ast.nodes import FieldSpec, FieldType, Scope


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def build_item_prompt(ctx: dict, bibref: str, text: str) -> List[dict]:
    """Monta as mensagens para geração de um ITEM Synesis.

    O system prompt é cacheável (estático por sessão).
    A mensagem do usuário é dinâmica (varia por chamada).

    Args:
        ctx: Contexto do projeto retornado por load_project().
        bibref: Referência bibliográfica do item (ex: "smith2024").
        text: Texto a ser codificado.

    Returns:
        Lista de dicts no formato interno:
        [
            {"role": "system", "content": str, "cache": True},
            {"role": "user",   "content": str, "cache": False},
        ]
    """
    system_text = _build_system_prompt(ctx)
    user_text = _build_user_message(bibref, text)

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


# ---------------------------------------------------------------------------
# Construção do system prompt (estático / cacheável)
# ---------------------------------------------------------------------------


def _build_system_prompt(ctx: dict) -> str:
    """Constrói o system prompt completo para o modo item.

    Inclui (quando disponíveis):
    - Descrição do projeto (ctx["project_description"])
    - Instruções por campo derivadas do template
    - Lista de conceitos existentes (code_index)
    - Lista de tópicos existentes (topic_index)
    """
    parts: List[str] = []

    parts.append(
        "Você é um codificador de pesquisa qualitativa especializado.\n"
        "Gere blocos ITEM Synesis válidos e semanticamente precisos "
        "conforme o template do projeto abaixo.\n\n"
        "REGRAS ABSOLUTAS:\n"
        "- Produza APENAS blocos ITEM...END ITEM\n"
        "- NÃO gere blocos SOURCE, ONTOLOGY, PROJECT, TEMPLATE ou qualquer outro tipo\n"
        "- NÃO use markdown, backticks (```) ou qualquer formatação extra\n"
        "- NÃO adicione explicações, comentários ou texto fora dos blocos ITEM"
    )

    # Contexto metodológico do projeto
    if ctx.get("project_description"):
        parts.append(
            "CONTEXTO DO PROJETO:\n" + ctx["project_description"]
        )

    # Instruções por campo (derivadas do template)
    item_fields_section = _build_item_fields_section(ctx)
    if item_fields_section:
        parts.append(item_fields_section)

    # Índice de conceitos existentes
    code_section = _build_code_index_section(ctx["code_index"])
    if code_section:
        parts.append(code_section)

    # Índice de tópicos existentes
    topic_section = _build_topic_index_section(ctx["topic_index"])
    if topic_section:
        parts.append(topic_section)

    # Formato esperado do output
    parts.append(_build_output_format_section(ctx))

    return "\n\n".join(parts)


def _build_item_fields_section(ctx: dict) -> str:
    """Gera a seção de instruções por campo ITEM do template."""
    item_fields: dict = ctx["item_fields"]
    required_item: list = ctx["required_item"]
    chain_relations: dict = ctx["chain_relations"]

    if not item_fields:
        return ""

    lines = ["CAMPOS DO ITEM (gere todos os REQUIRED; OPTIONAL quando pertinente):"]

    for name, spec in item_fields.items():
        req_label = "REQUIRED" if name in required_item else "OPTIONAL"
        instruction = _field_instruction(name, spec, chain_relations)
        lines.append(f"\n  {name} ({spec.type.name}) [{req_label}]:")
        lines.append(f"    {instruction}")

    return "\n".join(lines)


def _field_instruction(
    name: str, spec: FieldSpec, chain_relations: dict
) -> str:
    """Gera instrução para um campo com base em guidelines, description e tipo."""
    # Instrução principal: guidelines > description > genérica por tipo
    base = spec.guidelines or spec.description or _generic_instruction(spec.type)

    extras: List[str] = []

    if spec.type == FieldType.CHAIN:
        if chain_relations:
            rel_lines = [
                f"      {rel}: {desc}" for rel, desc in chain_relations.items()
            ]
            extras.append(
                "    Relações disponíveis (use apenas estas):\n" + "\n".join(rel_lines)
            )
        extras.append(
            "    Sintaxe: Conceito_A -> RELACAO -> Conceito_B -> RELACAO -> Conceito_C\n"
            "    Número ímpar de elementos. Conceitos em snake_case. "
            "Sem espaços nos nomes."
        )

    elif spec.type == FieldType.TOPIC:
        pass  # topic_index é injetado separadamente

    elif spec.type in (FieldType.ORDERED, FieldType.ENUMERATED):
        if spec.values:
            val_lines = _format_values(spec)
            extras.append("    Valores permitidos:\n" + "\n".join(val_lines))

    elif spec.type == FieldType.SCALE:
        if spec.format:
            extras.append(f"    Intervalo: {spec.format}")

    result = base
    if extras:
        result = base + "\n" + "\n".join(extras)
    return result


def _generic_instruction(field_type: FieldType) -> str:
    """Instrução genérica de fallback quando guidelines e description ausentes."""
    _GENERIC: dict = {
        FieldType.QUOTATION: "Extraia uma citação direta relevante do texto.",
        FieldType.MEMO: "Escreva uma nota analítica sobre o conteúdo.",
        FieldType.CODE: "Atribua um código analítico conciso (snake_case).",
        FieldType.CHAIN: "Construa uma cadeia causal entre conceitos.",
        FieldType.TEXT: "Forneça texto descritivo relevante.",
        FieldType.DATE: "Forneça a data no formato YYYY-MM-DD.",
        FieldType.SCALE: "Atribua um valor numérico na escala indicada.",
        FieldType.ENUMERATED: "Escolha um dos valores permitidos.",
        FieldType.ORDERED: "Escolha um dos valores ordenados permitidos.",
        FieldType.TOPIC: "Atribua um tópico temático relevante.",
    }
    return _GENERIC.get(field_type, "Preencha este campo conforme o tipo.")


def _format_values(spec: FieldSpec) -> List[str]:
    """Formata lista de valores ORDERED/ENUMERATED para o prompt."""
    lines = []
    for val in spec.values:
        if val.index >= 0:
            label = f"      {val.index}: {val.label}"
        else:
            label = f"      {val.label}"
        if val.description:
            label += f" — {val.description}"
        lines.append(label)
    return lines


def _build_code_index_section(code_index: dict) -> str:
    """Gera seção de conceitos existentes para o prompt."""
    if code_index["empty"]:
        return ""

    codes = code_index["codes"]
    # Agrupar em linhas de 10 para legibilidade
    groups = [codes[i : i + 10] for i in range(0, len(codes), 10)]
    code_lines = "\n".join("  " + ", ".join(g) for g in groups)

    return (
        "CONCEITOS EXISTENTES NO PROJETO (use preferencialmente; "
        "crie novos apenas quando nenhum existente se aplica):\n"
        + code_lines
    )


def _build_topic_index_section(topic_index: dict) -> str:
    """Gera seção de tópicos existentes para o prompt."""
    if topic_index["empty"]:
        return ""

    topics = topic_index["topics"]
    return (
        "TÓPICOS EXISTENTES (para campos TOPIC — use preferencialmente):\n"
        "  " + ", ".join(topics)
    )


def _build_output_format_section(ctx: dict) -> str:
    """Gera instrução de formato de output."""
    bundle_pairs = ctx.get("bundle_pairs", [])

    lines = [
        "FORMATO DO OUTPUT:",
        "  ITEM @{bibref}",
        "    {campo}: {valor}",
        "    ...",
        "  END ITEM",
    ]

    if bundle_pairs:
        bundle_strs = [" + ".join(b) for b in bundle_pairs]
        lines.append(
            f"  Campos agrupados (BUNDLE): {', '.join(bundle_strs)} "
            "— devem aparecer juntos ou nenhum."
        )

    lines.append(
        "  Substitua {bibref} pela referência fornecida. "
        "Omita campos OPTIONAL sem conteúdo relevante."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mensagem dinâmica do usuário (por chamada)
# ---------------------------------------------------------------------------


def _build_user_message(bibref: str, text: str) -> str:
    """Constrói a mensagem dinâmica do usuário."""
    return (
        f"BIBREF: @{bibref}\n"
        f"<text>{text}</text>\n\n"
        "Gere o(s) bloco(s) ITEM Synesis correspondentes."
    )
