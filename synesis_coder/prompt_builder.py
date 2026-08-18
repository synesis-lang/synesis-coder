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

from typing import List, Optional

from synesis.ast.nodes import FieldSpec, FieldType

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
        "You are a specialized qualitative research coder.\n"
        "Generate valid, semantically precise Synesis ITEM blocks "
        "according to the project template below.\n\n"
        "ABSOLUTE RULES:\n"
        "- Output ONLY ITEM...END ITEM blocks\n"
        "- Do NOT generate SOURCE, ONTOLOGY, PROJECT, TEMPLATE or any other block type\n"
        "- Do NOT use markdown, backticks (```) or any extra formatting\n"
        "- Do NOT add explanations, comments or any text outside the ITEM blocks"
    )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text field values (MEMO, TEXT descriptions) "
            f"must be written in {lang}.\n"
            "Exceptions: QUOTATION blocks preserve the original language of the source "
            "text. Concept names in CHAIN fields remain in the language used in "
            "EXISTING PROJECT CONCEPTS below."
        )

    # Contexto metodológico do projeto
    if ctx.get("project_description"):
        parts.append(
            "PROJECT CONTEXT:\n" + ctx["project_description"]
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


# ---------------------------------------------------------------------------
# Caminho JSON (Opção 3): prompts que pedem apenas VALORES
# ---------------------------------------------------------------------------


def build_item_values_prompt(ctx: dict, bibref: str, text: str) -> List[dict]:
    """Monta o prompt do caminho JSON para o modo item (devolve só valores).

    Reusa as seções de conteúdo (project description, GUIDELINES por campo,
    índices de conceitos/tópicos), mas NÃO inclui a seção de formato de bloco:
    no caminho JSON a moldura é montada pelo block_assembler, não pelo LLM. O
    LLM devolve apenas um JSON de valores conforme o schema fornecido à API.
    """
    system_text = _build_values_system_prompt(ctx, scope="item")
    user_text = _build_values_user_message(bibref, text)
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def build_document_values_prompt(
    ctx: dict,
    bibref: str,
    chunk: str,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> List[dict]:
    """Monta o prompt JSON para um chunk de documento (devolve só valores)."""
    system_text = _build_values_system_prompt(ctx, scope="item")
    user_text = _build_values_document_message(bibref, chunk, chunk_index, total_chunks)
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def build_document_source_values_prompt(
    ctx: dict, bibref: str, excerpt: str
) -> List[dict]:
    """Monta o prompt JSON para o bloco SOURCE de um documento (devolve só valores).

    Espelha build_document_values_prompt, mas para o escopo SOURCE: o LLM devolve
    um objeto plano de valores (não envelope `items`), montado por assemble_source.
    """
    system_text = _build_values_system_prompt(ctx, scope="source")
    user_text = (
        f"BIBREF: @{bibref}\n"
        f"<excerpt>{excerpt[:500].strip()}</excerpt>\n\n"
        "Return the JSON object of SOURCE field values for this document."
    )
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_values_system_prompt(ctx: dict, scope: str) -> str:
    """System prompt do caminho JSON — reusa conteúdo, omite a moldura do bloco.

    scope="item": contrato com array `items`; campos ITEM.
    scope="source": contrato com objeto plano de valores; campos SOURCE.
    """
    parts: List[str] = []

    if scope == "source":
        parts.append(
            "You are a specialized qualitative research coder.\n"
            "Extract structured VALUES for the SOURCE (document-level metadata) "
            "from the text below, following the project template.\n\n"
            "OUTPUT CONTRACT:\n"
            "- Return ONLY a JSON object that conforms to the provided schema\n"
            "- The JSON is a flat object holding the SOURCE field VALUES\n"
            "- Provide values only — do NOT write Synesis block keywords, field names "
            "with colons, or indentation. The system assembles the block.\n"
            "- Omit OPTIONAL fields you have no content for; include all REQUIRED fields"
        )
    else:
        parts.append(
            "You are a specialized qualitative research coder.\n"
            "Extract structured VALUES for qualitative annotations from the text below, "
            "following the project template.\n\n"
            "OUTPUT CONTRACT:\n"
            "- Return ONLY a JSON object that conforms to the provided schema\n"
            "- The JSON has an `items` array; each element holds the field VALUES of one "
            "annotation (one finding, causal relation or argument)\n"
            "- Provide values only — do NOT write Synesis block keywords, field names with "
            "colons, indentation, or chain arrows. The system assembles the block.\n"
            "- For CHAIN fields, return a list of hops; each hop is "
            "{\"source\": concept, \"relation\": one allowed relation, \"target\": concept}\n"
            "- Omit OPTIONAL fields you have no content for; include all REQUIRED fields"
        )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text values (MEMO, TEXT) must be written in {lang}. "
            "QUOTATION values preserve the source language. Chain concepts stay in the "
            "language of EXISTING PROJECT CONCEPTS below."
        )

    if ctx.get("project_description"):
        parts.append("PROJECT CONTEXT:\n" + ctx["project_description"])

    if scope == "source":
        # Escopo SOURCE: apenas a seção de campos SOURCE. Índices de
        # conceitos/tópicos e bundles são específicos de ITEM.
        source_section = _build_source_fields_section(ctx)
        if source_section:
            parts.append(source_section)
        return "\n\n".join(parts)

    fields_section = _build_item_fields_section(ctx)
    if fields_section:
        parts.append(fields_section)

    code_section = _build_code_index_section(ctx["code_index"])
    if code_section:
        parts.append(code_section)

    topic_section = _build_topic_index_section(ctx["topic_index"])
    if topic_section:
        parts.append(topic_section)

    bundle_pairs = ctx.get("bundle_pairs", [])
    if bundle_pairs:
        bundle_strs = [" + ".join(b) for b in bundle_pairs]
        parts.append(
            "BUNDLED FIELDS (BUNDLE): " + ", ".join(bundle_strs)
            + " — within one annotation these must all be present together or all absent."
        )

    return "\n\n".join(parts)


def _build_values_user_message(bibref: str, text: str) -> str:
    """Mensagem dinâmica do caminho JSON (modo item)."""
    return (
        f"BIBREF: @{bibref}\n"
        f"<text>{text}</text>\n\n"
        "Return the JSON object of values for the corresponding annotation(s)."
    )


def _build_values_document_message(
    bibref: str, chunk: str, chunk_index: int, total_chunks: int
) -> str:
    """Mensagem dinâmica do caminho JSON (chunk de documento)."""
    position_note = ""
    if total_chunks > 1:
        position_note = (
            f"[Excerpt {chunk_index + 1} of {total_chunks} — "
            "extract only annotations with complete evidence in this excerpt]\n\n"
        )
    return (
        f"BIBREF: @{bibref}\n"
        f"{position_note}"
        f"<text>{chunk}</text>\n\n"
        "Return the JSON object of values for the corresponding annotation(s)."
    )


def _build_item_fields_section(ctx: dict) -> str:
    """Gera a seção de instruções por campo ITEM do template."""
    item_fields: dict = ctx["item_fields"]
    required_item: list = ctx["required_item"]
    chain_relations: dict = ctx["chain_relations"]

    if not item_fields:
        return ""

    lines = ["ITEM FIELDS (generate all REQUIRED fields; OPTIONAL only when relevant):"]

    for name, spec in item_fields.items():
        req_label = "REQUIRED" if name in required_item else "OPTIONAL"
        instruction = _field_instruction(name, spec, chain_relations)
        lines.append(f"\n  {name} ({spec.type.name}) [{req_label}]:")
        lines.append(f"    {instruction}")

    return "\n".join(lines)


def _field_instruction(
    name: str, spec: FieldSpec, chain_relations: dict,
    fallback: Optional[str] = None,
) -> str:
    """Gera instrução para um campo com base em guidelines, description e tipo.

    Args:
        name: Nome do campo no template.
        spec: FieldSpec do campo.
        chain_relations: Relações do campo CHAIN do template.
        fallback: Instrução a usar quando o campo não declara guidelines nem
            description. None usa a genérica por TIPO. O escopo SOURCE passa
            aqui a sua genérica por NOME (`_generic_source_instruction`), que
            é mais específica para campos de metadado documental.

    Returns:
        A instrução, acrescida dos valores/relações/faixa derivados do tipo —
        é este acréscimo que entrega ao modelo a lista fechada de um
        ENUMERATED/ORDERED, as RELATIONS de um CHAIN e a faixa de um SCALE.
    """
    # Instrução principal: guidelines > description > genérica (por nome ou tipo)
    base = (
        spec.guidelines
        or spec.description
        or fallback
        or _generic_instruction(spec.type)
    )

    extras: List[str] = []

    if spec.type == FieldType.CHAIN:
        if chain_relations:
            rel_lines = [
                f"      {rel}: {desc}" for rel, desc in chain_relations.items()
            ]
            extras.append(
                "    Available relations (use only these):\n" + "\n".join(rel_lines)
            )
        extras.append(
            "    Syntax: Concept_A -> RELATION -> Concept_B -> RELATION -> Concept_C\n"
            "    Odd number of elements. Concepts in snake_case. "
            "No spaces in names."
        )

    elif spec.type == FieldType.TOPIC:
        pass  # topic_index é injetado separadamente

    elif spec.type in (FieldType.ORDERED, FieldType.ENUMERATED):
        if spec.values:
            val_lines = _format_values(spec)
            extras.append("    Allowed values:\n" + "\n".join(val_lines))

    elif spec.type == FieldType.SCALE:
        if spec.format:
            extras.append(f"    Range: {spec.format}")

    result = base
    if extras:
        result = base + "\n" + "\n".join(extras)
    return result


def _generic_instruction(field_type: FieldType) -> str:
    """Instrução genérica de fallback quando guidelines e description ausentes."""
    _GENERIC: dict = {
        FieldType.QUOTATION: "Extract a relevant direct quotation from the text.",
        FieldType.MEMO: "Write an analytical note about the content.",
        FieldType.CODE: (
            "Assign one or more concise analytical codes (snake_case). "
            "Separate multiple codes with a comma: code_a, code_b, code_c."
        ),
        FieldType.CHAIN: "Build a causal chain between concepts.",
        FieldType.TEXT: "Provide relevant descriptive text.",
        FieldType.DATE: "Provide the date in YYYY-MM-DD format.",
        FieldType.SCALE: "Assign a numeric value within the indicated scale.",
        FieldType.ENUMERATED: "Choose one of the allowed values.",
        FieldType.ORDERED: "Choose one of the ordered allowed values.",
        FieldType.TOPIC: "Assign a relevant thematic topic.",
    }
    return _GENERIC.get(field_type, "Fill this field according to its type.")


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
        "EXISTING PROJECT CONCEPTS (prefer these; "
        "create new ones only when none of the existing ones apply):\n"
        + code_lines
    )


def _build_topic_index_section(topic_index: dict) -> str:
    """Gera seção de tópicos existentes para o prompt."""
    if topic_index["empty"]:
        return ""

    topics = topic_index["topics"]
    return (
        "EXISTING TOPICS (for TOPIC fields — prefer these):\n"
        "  " + ", ".join(topics)
    )


def _build_output_format_section(ctx: dict) -> str:
    """Gera instrução de formato de output."""
    bundle_pairs = ctx.get("bundle_pairs", [])

    lines = [
        "OUTPUT FORMAT:",
        "  ITEM @{bibref}",
        "    {field}: {value}",
        "    ...",
        "  END ITEM",
    ]

    if bundle_pairs:
        bundle_strs = [" + ".join(b) for b in bundle_pairs]
        lines.append(
            f"  Bundled fields (BUNDLE): {', '.join(bundle_strs)} "
            "— must appear together or not at all."
        )

    lines.append(
        "  Replace {bibref} with the provided reference. "
        "Omit OPTIONAL fields with no relevant content."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ponto de entrada: modo abstract (Fase 2)
# ---------------------------------------------------------------------------


def build_abstract_prompt(
    ctx: dict, bibref: str, abstract: str
) -> List[dict]:
    """Monta as mensagens para processamento de um abstract (SOURCE + ITEMs).

    Similar a build_item_prompt(), mas instrui o LLM a gerar um bloco SOURCE
    seguido de um ou mais blocos ITEM — conforme o formato de anotação .syn.

    O system prompt é cacheável (estático por sessão) e inclui instruções
    para geração tanto de SOURCE quanto de ITEMs.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        bibref: Referência bibliográfica (chave BibTeX, ex: "smith2024").
        abstract: Texto do abstract a ser codificado.

    Returns:
        Lista de dicts no formato interno:
        [
            {"role": "system", "content": str, "cache": True},
            {"role": "user",   "content": str, "cache": False},
        ]
    """
    system_text = _build_abstract_system_prompt(ctx)
    user_text = _build_abstract_user_message(bibref, abstract)

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_abstract_system_prompt(ctx: dict) -> str:
    """Constrói o system prompt para o modo abstract.

    Diferenças em relação ao modo item:
    - Instrui geração de bloco SOURCE + blocos ITEM
    - Inclui instruções para campos SOURCE do template
    - Bloco SOURCE contém campos descritivos do artigo
    """
    parts: List[str] = []

    parts.append(
        "You are a specialized qualitative research coder.\n"
        "Analyze a scientific article abstract and generate valid Synesis annotations "
        "according to the project template.\n\n"
        "ABSOLUTE RULES:\n"
        "- Generate EXACTLY ONE SOURCE @{bibref} block at the beginning\n"
        "- Generate ONE or MORE ITEM @{bibref} blocks for each causal chain, "
        "finding or relevant argument extracted from the abstract\n"
        "- Do NOT generate ONTOLOGY, PROJECT, TEMPLATE or any other block type\n"
        "- Do NOT use markdown, backticks (```) or any extra formatting\n"
        "- Do NOT add explanations, comments or any text outside the blocks\n"
        "- Each ITEM must capture ONE specific finding, causal relation or argument\n"
        "- If the abstract contains no codeable content, generate only the SOURCE block "
        "with a note indicating this"
    )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text field values (MEMO, TEXT descriptions) "
            f"must be written in {lang}.\n"
            "Exceptions: QUOTATION blocks preserve the original language of the source "
            "text. Concept names in CHAIN fields remain in the language used in "
            "EXISTING PROJECT CONCEPTS below."
        )

    # Contexto metodológico do projeto
    if ctx.get("project_description"):
        parts.append(
            "PROJECT CONTEXT:\n" + ctx["project_description"]
        )

    # Campos SOURCE do template
    source_section = _build_source_fields_section(ctx)
    if source_section:
        parts.append(source_section)

    # Campos ITEM do template
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

    # Formato esperado do output (SOURCE + ITEM)
    parts.append(_build_abstract_output_format_section(ctx))

    return "\n\n".join(parts)


def _build_source_fields_section(ctx: dict) -> str:
    """Gera a seção de instruções por campo SOURCE do template.

    Usa `_field_instruction` — a mesma função dos escopos ITEM e ONTOLOGY — para
    que um campo SOURCE receba, além da sua GUIDELINE, os valores/relações/faixa
    derivados do tipo. Sem isso um ENUMERATED em SOURCE chegava ao modelo sem a
    lista de valores permitidos: no caminho JSON o `enum` do schema ainda
    restringia a saída, mas no caminho de TEXTO LIVRE (fallback) não havia
    defesa alguma, e uma GUIDELINE como "escolha exatamente uma das opções
    acima" referenciava uma lista que nunca fora enviada.

    O fallback por NOME do escopo SOURCE (`_generic_source_instruction`) é
    preservado e passado explicitamente: ele é mais específico que o genérico
    por tipo para campos de metadado documental (description, method).
    """
    source_fields: dict = ctx["source_fields"]
    required_source: list = ctx.get("required_source", [])
    chain_relations: dict = ctx.get("chain_relations", {})

    if not source_fields:
        return ""

    lines = ["SOURCE FIELDS (generate all REQUIRED fields; OPTIONAL only when relevant):"]

    for name, spec in source_fields.items():
        # Campos com origem-de-valor externa (ON BIBLIOGRAPHY / ON DATASET) NÃO
        # são gerados pelo LLM — o compilador os resolve do .bib/TOML. Pedi-los
        # ao modelo desperdiça tokens e induz valores fabricados (ex.: `false`
        # para um campo TOML vazio). Omitidos do prompt.
        if getattr(spec, "value_origin", "document") in ("bibliography", "dataset"):
            continue
        req_label = "REQUIRED" if name in required_source else "OPTIONAL"
        instruction = _field_instruction(
            name, spec, chain_relations,
            fallback=_generic_source_instruction(name),
        )
        lines.append(f"\n  {name} ({spec.type.name}) [{req_label}]:")
        lines.append(f"    {instruction}")

    return "\n".join(lines)


def _generic_source_instruction(field_name: str) -> str:
    """Instrução genérica para campos SOURCE sem guidelines/description."""
    _GENERIC: dict = {
        "description": "Describe the study objective and scope in 1-2 sentences.",
        "method": "Briefly describe the methodology used.",
        "epistemic_model": "Indicate the theoretical model or framework used.",
    }
    return _GENERIC.get(
        field_name.lower(),
        "Fill this field with relevant information from the abstract.",
    )


def build_abstract_values_prompt(ctx: dict, bibref: str, abstract: str) -> List[dict]:
    """Prompt para o caminho JSON do modo abstract (SOURCE + ITEMs como valores).

    Análogo a build_item_values_prompt, mas solicita um envelope combinado:
    {"source": {...}, "items": [{...}, ...]}.
    O system prompt reutiliza as seções de GUIDELINES e índices; omite a seção
    de formato de bloco (a moldura é responsabilidade do assembler).
    """
    system_text = _build_abstract_values_system_prompt(ctx)
    user_text = (
        f"BIBREF: @{bibref}\n"
        f"<abstract>{abstract}</abstract>\n\n"
        "Extract structured values for the SOURCE block and all ITEM blocks."
    )
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_abstract_values_system_prompt(ctx: dict) -> str:
    """System prompt para o caminho JSON do modo abstract."""
    parts: List[str] = []

    parts.append(
        "You are a specialized qualitative research coder.\n"
        "Extract structured VALUES for qualitative annotations from the abstract below, "
        "following the project template.\n\n"
        "OUTPUT CONTRACT:\n"
        "- Return ONLY a JSON object with two keys: \"source\" and \"items\"\n"
        "- \"source\": object with the SOURCE field values\n"
        "- \"items\": array of objects, each holding the field VALUES of one ITEM "
        "(one finding, causal relation or argument)\n"
        "- Provide values only — do NOT write Synesis block keywords, field names "
        "with colons, indentation, or chain arrows. The system assembles the blocks.\n"
        "- For CHAIN fields, return a list of hops; each hop is "
        "{\"source\": concept, \"relation\": one allowed relation, \"target\": concept}\n"
        "- Omit OPTIONAL fields you have no content for; include all REQUIRED fields"
    )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text values (MEMO, TEXT) must be written in {lang}. "
            "QUOTATION values preserve the source language. Chain concepts stay in the "
            "language of EXISTING PROJECT CONCEPTS below."
        )

    if ctx.get("project_description"):
        parts.append("PROJECT CONTEXT:\n" + ctx["project_description"])

    source_section = _build_source_fields_section(ctx)
    if source_section:
        parts.append(source_section)

    item_fields_section = _build_item_fields_section(ctx)
    if item_fields_section:
        parts.append(item_fields_section)

    code_section = _build_code_index_section(ctx["code_index"])
    if code_section:
        parts.append(code_section)

    topic_section = _build_topic_index_section(ctx["topic_index"])
    if topic_section:
        parts.append(topic_section)

    bundle_pairs = ctx.get("bundle_pairs", [])
    if bundle_pairs:
        bundle_strs = [" + ".join(b) for b in bundle_pairs]
        parts.append(
            f"BUNDLE CONSTRAINT: {', '.join(bundle_strs)} "
            "— these fields must appear together or not at all within each ITEM."
        )

    return "\n\n".join(parts)


def _build_abstract_output_format_section(ctx: dict) -> str:
    """Gera instrução de formato de output para o modo abstract."""
    bundle_pairs = ctx.get("bundle_pairs", [])

    lines = [
        "OUTPUT FORMAT:",
        "  SOURCE @{bibref}",
        "    {source_field}: {value}",
        "    ...",
        "  END SOURCE",
        "",
        "  ITEM @{bibref}",
        "    {item_field}: {value}",
        "    ...",
        "  END ITEM",
        "",
        "  (repeat ITEM blocks for each finding/relation)",
    ]

    if bundle_pairs:
        bundle_strs = [" + ".join(b) for b in bundle_pairs]
        lines.append(
            f"  Bundled fields (BUNDLE): {', '.join(bundle_strs)} "
            "— must appear together or not at all."
        )

    lines.append(
        "  Replace {bibref} with the provided reference. "
        "Omit OPTIONAL fields with no relevant content."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mensagem dinâmica do usuário (por chamada)
# ---------------------------------------------------------------------------


def _build_user_message(bibref: str, text: str) -> str:
    """Constrói a mensagem dinâmica do usuário para o modo item."""
    return (
        f"BIBREF: @{bibref}\n"
        f"<text>{text}</text>\n\n"
        "Generate the corresponding Synesis ITEM block(s)."
    )


def _build_abstract_user_message(bibref: str, abstract: str) -> str:
    """Constrói a mensagem dinâmica do usuário para o modo abstract."""
    return (
        f"BIBREF: @{bibref}\n"
        f"<abstract>{abstract}</abstract>\n\n"
        "Analyze the abstract and generate the SOURCE block followed by all "
        "corresponding ITEM blocks."
    )


# ---------------------------------------------------------------------------
# Ponto de entrada: modo document (Fase 3)
# ---------------------------------------------------------------------------


def build_document_prompt(
    ctx: dict,
    bibref: str,
    chunk: str,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> List[dict]:
    """Monta as mensagens para codificação de um chunk de documento longo.

    O modo document processa documentos longos (entrevistas, artigos, capítulos)
    divididos em chunks com overlap. Cada chunk produz apenas blocos ITEM —
    sem SOURCE (gerado uma única vez separadamente).

    O system prompt é idêntico ao do modo item (cacheável por sessão), mas
    a mensagem do usuário inclui metadados de posição do chunk para que o LLM
    saiba que está vendo um trecho de um documento maior.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        bibref: Referência bibliográfica (ex: "entrevista_01").
        chunk: Trecho do documento a ser codificado.
        chunk_index: Índice do chunk atual (0-based).
        total_chunks: Total de chunks do documento.

    Returns:
        Lista de dicts no formato interno:
        [
            {"role": "system", "content": str, "cache": True},
            {"role": "user",   "content": str, "cache": False},
        ]
    """
    system_text = _build_system_prompt(ctx)
    user_text = _build_document_user_message(
        bibref, chunk, chunk_index, total_chunks
    )

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_document_user_message(
    bibref: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
) -> str:
    """Constrói a mensagem dinâmica do usuário para um chunk de documento."""
    position_note = ""
    if total_chunks > 1:
        position_note = (
            f"[Excerpt {chunk_index + 1} of {total_chunks} — "
            "extract only ITEMs with complete evidence in this excerpt]\n\n"
        )

    return (
        f"BIBREF: @{bibref}\n"
        f"{position_note}"
        f"<text>{chunk}</text>\n\n"
        "Generate the corresponding Synesis ITEM block(s). "
        "Do NOT generate a SOURCE block."
    )


# ---------------------------------------------------------------------------
# Ponto de entrada: modo ontology (Fase 4)
# ---------------------------------------------------------------------------


def build_ontology_prompt(
    ctx: dict,
    code: str,
    semantic_ctx: dict,
) -> List[dict]:
    """Monta as mensagens para geração de uma entrada ONTOLOGY.

    O modo ontology gera definições semânticas para códigos que já foram
    usados nas anotações (.syn) do projeto. O LLM recebe contexto rico
    derivado do corpus anotado: frequência, fontes, relações, co-ocorrências
    e exemplos concretos de uso do código.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        code: Nome do código a ser definido (ex: "Social_Acceptance").
        semantic_ctx: Contexto semântico derivado do corpus:
            {
                "frequency": int,           — total de ITEMs usando este código
                "sources": int,             — fontes bibliográficas distintas
                "relations": List[tuple],   — triples (A, REL, B) envolvendo o código
                "co_codes": List[str],      — outros códigos no mesmo ITEM
                "examples": List[dict],     — campos dos primeiros 3 ITEMs
            }

    Returns:
        Lista de dicts no formato interno:
        [
            {"role": "system", "content": str, "cache": True},
            {"role": "user",   "content": str, "cache": False},
        ]
    """
    system_text = _build_ontology_system_prompt(ctx)
    user_text = _build_ontology_user_message(code, semantic_ctx, ctx)

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def build_ontology_values_prompt(
    ctx: dict,
    code: str,
    semantic_ctx: dict,
) -> List[dict]:
    """Prompt para o caminho JSON do modo ontology (valores de um ONTOLOGY).

    Análogo a build_ontology_prompt, mas solicita apenas um objeto JSON com os
    VALORES dos campos ONTOLOGY — o assembler monta a moldura `ONTOLOGY <code>
    ... END ONTOLOGY`. Reutiliza a mesma mensagem de usuário rica em
    semantic_ctx; só o system prompt (contrato de saída) difere.
    """
    system_text = _build_ontology_values_system_prompt(ctx)
    user_text = _build_ontology_user_message(code, semantic_ctx, ctx)

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_ontology_values_system_prompt(ctx: dict) -> str:
    """System prompt para o caminho JSON do modo ontology."""
    parts: List[str] = []

    parts.append(
        "You are an expert in qualitative analysis and domain ontology.\n"
        "Generate the structured VALUES of a Synesis ONTOLOGY entry for an "
        "analytical code, based on the semantic context derived from the "
        "already annotated corpus.\n\n"
        "OUTPUT CONTRACT:\n"
        "- Return ONLY a JSON object with the ONTOLOGY field values\n"
        "- Provide values only — do NOT write Synesis block keywords "
        "(ONTOLOGY, END ONTOLOGY, ITEM, TYPE), field names with colons, or "
        "indentation. The system assembles the block.\n"
        "- Omit OPTIONAL fields you have no content for; include all REQUIRED fields"
    )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text values (TEXT) must be written in {lang}."
        )

    if ctx.get("project_description"):
        parts.append("PROJECT CONTEXT:\n" + ctx["project_description"])

    ontology_fields_section = _build_ontology_fields_section(ctx)
    if ontology_fields_section:
        parts.append(ontology_fields_section)

    topic_section = _build_topic_index_section(ctx["topic_index"])
    if topic_section:
        parts.append(topic_section)

    return "\n\n".join(parts)


def _build_ontology_system_prompt(ctx: dict) -> str:
    """Constrói o system prompt para o modo ontology."""
    parts: List[str] = []

    parts.append(
        "You are an expert in qualitative analysis and domain ontology.\n"
        "Generate a valid Synesis ONTOLOGY entry for an analytical code, "
        "based on the semantic context derived from the already annotated corpus.\n\n"
        "ABSOLUTE RULES:\n"
        "- Output ONLY ONE ONTOLOGY...END ONTOLOGY block\n"
        "- Do NOT generate ITEM, SOURCE, PROJECT, TEMPLATE or any other block type\n"
        "- Do NOT use markdown, backticks (```) or any extra formatting\n"
        "- Do NOT add explanations, comments or any text outside the ONTOLOGY block\n"
        "- The definition must be based EXCLUSIVELY on the observed usage in the corpus"
    )

    lang = ctx.get("output_language")
    if lang:
        parts.append(
            f"OUTPUT LANGUAGE: All free-text field values (TEXT descriptions) "
            f"must be written in {lang}.\n"
            "Exception: Concept names remain in the language used in the corpus."
        )

    # Contexto metodológico do projeto
    if ctx.get("project_description"):
        parts.append(
            "PROJECT CONTEXT:\n" + ctx["project_description"]
        )

    # Campos ONTOLOGY do template
    ontology_fields_section = _build_ontology_fields_section(ctx)
    if ontology_fields_section:
        parts.append(ontology_fields_section)

    # Tópicos existentes (para campo TOPIC)
    topic_section = _build_topic_index_section(ctx["topic_index"])
    if topic_section:
        parts.append(topic_section)

    # Formato esperado do output
    parts.append(_build_ontology_output_format_section(ctx))

    return "\n\n".join(parts)


def _build_ontology_fields_section(ctx: dict) -> str:
    """Gera a seção de instruções por campo ONTOLOGY do template."""
    ontology_fields: dict = ctx.get("ontology_fields", {})
    required_ontology: list = ctx.get("required_ontology", [])

    if not ontology_fields:
        return ""

    lines = ["ONTOLOGY FIELDS (generate all REQUIRED fields; OPTIONAL only when relevant):"]

    for name, spec in ontology_fields.items():
        req_label = "REQUIRED" if name in required_ontology else "OPTIONAL"
        instruction = _field_instruction(name, spec, ctx.get("chain_relations", {}))
        lines.append(f"\n  {name} ({spec.type.name}) [{req_label}]:")
        lines.append(f"    {instruction}")

    return "\n".join(lines)


def _build_ontology_output_format_section(ctx: dict) -> str:
    """Gera instrução de formato de output para o modo ontology."""
    ontology_fields: dict = ctx.get("ontology_fields", {})

    lines = [
        "OUTPUT FORMAT:",
        "  ONTOLOGY {code}",
    ]

    for name in ontology_fields:
        lines.append(f"    {name}: {{value}}")

    lines.append("  END ONTOLOGY")
    lines.append(
        "  Replace {code} with the provided code name. "
        "Omit OPTIONAL fields with no relevant content."
    )

    return "\n".join(lines)


def _build_ontology_user_message(
    code: str,
    semantic_ctx: dict,
    ctx: dict,
) -> str:
    """Constrói a mensagem do usuário com o contexto semântico do código."""
    parts: List[str] = [f"CODE: {code}"]

    # Estatísticas de uso
    freq = semantic_ctx.get("frequency", 0)
    sources = semantic_ctx.get("sources", 0)
    parts.append(f"CORPUS USAGE: {freq} occurrence(s) across {sources} distinct source(s)")

    # Relações no grafo de chains
    relations = semantic_ctx.get("relations", [])
    if relations:
        rel_lines = [f"  {a} -> {r} -> {b}" for a, r, b in relations[:15]]
        parts.append("OBSERVED RELATIONS:\n" + "\n".join(rel_lines))

    # Co-ocorrências
    co_codes = semantic_ctx.get("co_codes", [])
    if co_codes:
        parts.append("CO-OCCURRENCES (codes in the same ITEM): " + ", ".join(co_codes[:20]))

    # Tópicos existentes disponíveis (se há campo TOPIC nos campos ONTOLOGY)
    topic_index = ctx.get("topic_index", {})
    if not topic_index.get("empty") and ctx.get("ontology_fields"):
        for name, spec in ctx["ontology_fields"].items():
            from synesis.ast.nodes import FieldType
            if spec.type == FieldType.TOPIC:
                topics = topic_index.get("topics", [])
                if topics:
                    parts.append(
                        f"AVAILABLE TOPICS (for field '{name}'): "
                        + ", ".join(topics)
                    )
                break

    # Exemplos concretos do corpus
    examples = semantic_ctx.get("examples", [])
    if examples:
        example_parts = []
        for i, ex in enumerate(examples[:3], 1):
            ex_lines = [f"  Example {i}:"]
            for field_name, field_val in ex.items():
                ex_lines.append(f"    {field_name}: {field_val}")
            example_parts.append("\n".join(ex_lines))
        parts.append("USAGE EXAMPLES:\n" + "\n".join(example_parts))

    parts.append(f"Generate the ONTOLOGY entry for code '{code}'.")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Ponto de entrada: modo suggest (Fase 5)
# ---------------------------------------------------------------------------


def build_critique_prompt(
    ctx: dict,
    item_block: str,
    source_text: str,
    item_position: Optional[tuple] = None,
) -> List[dict]:
    """Monta as mensagens para revisão crítica de um bloco ITEM.

    O crítico avalia se os campos do ITEM representam fielmente o texto-fonte,
    usando as GUIDELINES do template como critério. Produz um score de suspeição
    e, se necessário, sugestões de correção em formato # $key: value.

    O system prompt é cacheável (estático por sessão para um dado template).
    A mensagem do usuário é dinâmica (varia por ITEM).

    Args:
        ctx: Contexto do projeto retornado por load_project().
        item_block: Texto completo do bloco ITEM a revisar.
        source_text: Texto-fonte. Quando contém `<target>`, a avaliação é
            restrita ao trecho delimitado (ver critique_mode._build_critique_source).
        item_position: (n, total) do ITEM dentro do bibref, quando conhecido.

    Returns:
        Lista de dicts no formato interno:
        [
            {"role": "system", "content": str, "cache": True},
            {"role": "user",   "content": str, "cache": False},
        ]
    """
    system_text = _build_critique_system_prompt(ctx)
    user_text = _build_critique_user_message(item_block, source_text, item_position)

    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _build_critique_system_prompt(ctx: dict) -> str:
    """Constrói o system prompt para o modo critique."""
    parts: List[str] = []

    parts.append(
        "You are a critical reviewer of qualitative research annotations.\n"
        "Your role is to evaluate whether a Synesis ITEM block accurately represents "
        "the source text, using the template's GUIDELINES as evaluation criteria.\n\n"
        "ABSOLUTE RULES:\n"
        "- Output ONLY the structured critique lines shown below — nothing else\n"
        "- Do NOT generate Synesis annotation blocks (ITEM, SOURCE, ONTOLOGY)\n"
        "- Do NOT add explanations, headers or any text outside the output format\n"
        "- If you find no issues, still output the divergence and reason lines"
    )

    if ctx.get("project_description"):
        parts.append("PROJECT CONTEXT:\n" + ctx["project_description"])

    criteria = _build_critique_item_criteria(ctx)
    if criteria:
        parts.append(criteria)

    parts.append(_build_critique_output_format(ctx))

    return "\n\n".join(parts)


def _build_critique_item_criteria(ctx: dict) -> str:
    """Gera os critérios de avaliação por campo ITEM para o prompt de critique."""
    item_fields: dict = ctx.get("item_fields", {})
    chain_relations: dict = ctx.get("chain_relations", {})

    if not item_fields:
        return ""

    lines = [
        "EVALUATION CRITERIA — check each field against these guidelines:"
    ]

    for name, spec in item_fields.items():
        guideline = spec.guidelines or spec.description
        if not guideline:
            continue
        lines.append(f"\n  {name} ({spec.type.name}):")
        lines.append(f"    {guideline}")

        if spec.type == FieldType.CHAIN and chain_relations:
            rel_lines = [
                f"      {rel}: {desc}" for rel, desc in chain_relations.items()
            ]
            lines.append(
                "    Valid relations:\n" + "\n".join(rel_lines)
            )

    return "\n".join(lines)


def _build_critique_output_format(ctx: dict) -> str:
    """Gera a instrução de formato de saída para o critique."""
    item_fields: dict = ctx.get("item_fields", {})
    correctable_fields = [
        name for name, spec in item_fields.items()
        if spec.type.name not in ("QUOTATION",)
    ]

    from synesis_coder.critique_taxonomy import build_taxonomy_section

    lines = [
        "OUTPUT FORMAT — respond with ONLY these lines, nothing else:",
        "",
        "  # $divergence: [0.00-1.00]",
        "  # $reason: [one of the categories below]",
        "  # $comment: [optional free-text explanation — use this, NOT # $note:]",
    ]

    taxonomy = build_taxonomy_section(item_fields, ctx.get("required_item"))
    if taxonomy:
        lines.append("")
        lines.append(taxonomy)

    if correctable_fields:
        lines.append("")
        lines.append(
            "  ← For each field that needs correction, add ONE line with the full corrected value:"
        )
        examples = correctable_fields[:3]
        for field in examples:
            lines.append(
                f"  # ${field}: [complete corrected value]   "
                "← ONLY when you have a concrete replacement"
            )
        lines.append("")
        lines.append("IMPORTANT RULES FOR FIELD CORRECTIONS:")
        lines.append(
            "  - NEVER output # $note: — use # $comment: for explanations instead."
        )
        lines.append(
            "  - When a field appears multiple times (e.g. multiple `chain:` lines in one ITEM),\n"
            "    the corrected value must start with the SAME source node as the original chain\n"
            "    you are correcting, so the system can identify which occurrence to replace.\n"
            "    Example: if correcting `Subsidy -> ENABLES -> X`, output\n"
            "    `# $chain: Subsidy -> INFLUENCES -> X` (same source node 'Subsidy')."
        )
        lines.append(
            "  - If multiple chain lines all need the same correction (e.g. renaming a terminal\n"
            "    node), output one # $chain: line per affected chain, each starting with its\n"
            "    own source node."
        )

    lines.append("")
    lines.append("SCORING GUIDE:")
    lines.append(
        "  0.00-0.15  → no issues found (reason: none)\n"
        "  0.16-0.40  → minor deviation — annotation is plausible but imprecise\n"
        "  0.41-0.70  → significant issue — field value diverges from source text\n"
        "  0.71-1.00  → serious issue — annotation misrepresents or contradicts source"
    )

    lines.append("")
    lines.append("DEFERENCE RULE — read before assigning any score above 0.15:")
    lines.append(
        "  Your task is to detect annotations that VIOLATE the template's stated\n"
        "  rules — NOT to apply a stricter standard than the template declares.\n"
        "\n"
        "  - If the template ADMITS the existing annotation under a reasonable\n"
        "    reading, assign 0.00-0.15 with reason `none` — even when a different\n"
        "    reading would also be defensible, and even when you would personally\n"
        "    have annotated it differently.\n"
        "  - Preferring an alternative is NOT a defect. Only flag what the\n"
        "    template's own guidelines rule out.\n"
        "  - Do not require precision the guidelines do not ask for: if a rule\n"
        "    does not state a threshold, a magnitude or a direction, its absence\n"
        "    in the annotation is not a violation.\n"
        "  - Most annotations in a well-built corpus are correct. Finding no\n"
        "    issue is the expected outcome, not a failure to review carefully."
    )

    return "\n".join(lines)


def _build_critique_user_message(
    item_block: str,
    source_text: str,
    item_position: Optional[tuple] = None,
) -> str:
    """Constrói a mensagem dinâmica do usuário para o critique de um ITEM.

    Quando `source_text` traz um `<target>`, a instrução restringe a avaliação
    ao trecho delimitado: o entorno serve só para desambiguar referências, não
    para cobrar cobertura do que está fora dele.
    """
    position_note = ""
    if item_position:
        n, total = item_position
        if total > 1:
            position_note = (
                f"\nThis is ITEM {n} of {total} annotating this same source. "
                "Each ITEM covers a DIFFERENT excerpt — do not fault this one "
                "for content that other ITEMs cover.\n"
            )

    if "<target>" in source_text:
        scope_note = (
            "Evaluate ONLY the passage inside <target>...</target>. The "
            "surrounding text is context to disambiguate references — never "
            "fault the ITEM for failing to cover content outside <target>."
        )
    else:
        scope_note = (
            "Evaluate whether the ITEM fields accurately represent the SOURCE "
            "TEXT according to the guidelines."
        )

    return (
        f"SOURCE TEXT:\n<source>{source_text}</source>\n"
        f"{position_note}\n"
        f"ITEM TO REVIEW:\n{item_block.strip()}\n\n"
        f"{scope_note} Output the structured critique."
    )


# ---------------------------------------------------------------------------
# Ponto de entrada: modo refine (re-extração com feedback — pipeline ACT)
# ---------------------------------------------------------------------------

# Meta-tags do critique que descrevem o diagnóstico, não sugestões de campo.
# Fonte única compartilhada com incorporate_mode — antes eram duas constantes
# literais duplicadas que nada mantinha sincronizadas (Estudo §9.3).
from synesis_coder.revision_vocab import META_TAGS as _CRITIQUE_META_TAGS  # noqa: E402


def build_item_refinement_prompt(
    ctx: dict,
    bibref: str,
    source_text: str,
    prev_item_block: str,
    critique_tags: dict,
) -> List[dict]:
    """Monta as mensagens para re-extração informada por feedback (texto livre).

    O gerador raciocina de novo sobre o texto-fonte, ciente do erro apontado pelo
    crítico, e reescreve a anotação corrigindo APENAS os campos sinalizados. É o
    caminho de texto-livre (backend Anthropic nativo); o caminho JSON usa
    build_item_refinement_values_prompt.

    O system prompt reusa o do modo item (GUIDELINES, índices, formato de bloco),
    preservando `cache=True` sobre a maior parte dos tokens. Todo o feedback
    (dinâmico) vai apenas na mensagem do usuário.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        bibref: Referência bibliográfica do ITEM.
        source_text: Texto-fonte original (abstract ou campo text do ITEM).
        prev_item_block: Bloco ITEM gerado anteriormente (a versão a corrigir).
        critique_tags: Dict de tags do critique (reason, reason_detail e
            sugestões de campo).

    Returns:
        Lista de dicts no formato interno [{"role","content","cache"}].
    """
    system_text = _build_system_prompt(ctx)
    user_text = _build_refinement_user_message(
        bibref, source_text, prev_item_block, critique_tags
    )
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def build_item_refinement_values_prompt(
    ctx: dict,
    bibref: str,
    source_text: str,
    prev_item_block: str,
    critique_tags: dict,
) -> List[dict]:
    """Prompt JSON para re-extração informada por feedback (devolve só valores).

    Análogo a build_item_values_prompt, mas a mensagem do usuário inclui a
    anotação anterior e o diagnóstico do crítico. A moldura do bloco é montada
    por assemble_items — o LLM devolve o envelope `items` de valores.
    """
    system_text = _build_values_system_prompt(ctx, scope="item")
    user_text = _build_refinement_user_message(
        bibref, source_text, prev_item_block, critique_tags, json_mode=True
    )
    return [
        {"role": "system", "content": system_text, "cache": True},
        {"role": "user", "content": user_text, "cache": False},
    ]


def _format_critique_feedback(critique_tags: dict) -> str:
    """Serializa as tags do critique em texto legível para o gerador.

    Distingue o diagnóstico (reason/reason_detail) das sugestões de campo, que
    são apresentadas como HIPÓTESES a serem verificadas contra o texto-fonte —
    o gerador decide, não copia cegamente (padrão Self-Refine). Chaves numeradas
    (ex: "chain.1") são normalizadas ao campo-base para exibição.
    """
    lines: List[str] = []

    reason = critique_tags.get("reason")
    if reason and reason != "none":
        lines.append(f"  reason: {reason}")
    detail = critique_tags.get("reason_detail")
    if detail:
        lines.append(f"  detail: {detail}")

    field_hints: List[str] = []
    for key, value in critique_tags.items():
        base_key = key.split(".")[0]
        if base_key in _CRITIQUE_META_TAGS or key.startswith("metrics."):
            continue
        field_hints.append(f"    {base_key}: {value}")

    if field_hints:
        lines.append(
            "  field hints (hypotheses — verify against the SOURCE, do not copy blindly):"
        )
        lines.extend(field_hints)

    if not lines:
        # Crítico sinalizou sem detalhar: instrução genérica de reavaliação.
        lines.append("  (the reviewer flagged this annotation; re-examine all fields)")

    return "\n".join(lines)


def _build_refinement_user_message(
    bibref: str,
    source_text: str,
    prev_item_block: str,
    critique_tags: dict,
    json_mode: bool = False,
) -> str:
    """Constrói a mensagem dinâmica do usuário para a re-extração."""
    feedback = _format_critique_feedback(critique_tags)
    closing = (
        "Re-extract this annotation from the SOURCE, correcting ONLY the flagged "
        "field(s). Keep all correct fields unchanged. "
        + (
            "Return the JSON object of values."
            if json_mode
            else "Output only the corrected ITEM block(s)."
        )
    )
    return (
        f"BIBREF: @{bibref}\n"
        f"<source>{source_text}</source>\n\n"
        "PREVIOUS ANNOTATION (contains an issue flagged by the reviewer):\n"
        f"{prev_item_block.strip()}\n\n"
        "REVIEWER DIAGNOSIS:\n"
        f"{feedback}\n\n"
        f"{closing}"
    )


# ---------------------------------------------------------------------------
# Ponto de entrada: modo normalize (Fase 3)
# ---------------------------------------------------------------------------


def build_normalization_prompt(ctx: dict, code_groups: list) -> List[dict]:
    """Monta mensagens para normalização semântica de grupos de códigos.

    Recebe um lote de grupos de códigos com múltiplas variantes e solicita ao
    LLM que sugira formas canônicas para grupos semanticamente equivalentes.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        code_groups: Lista de CodeGroup com múltiplas variantes (residuais após
            normalização determinística).

    Returns:
        Lista de mensagens no formato interno [{"role", "content", "cache"}].
    """
    system_content = _build_normalization_system_prompt(ctx)
    user_content = _build_normalization_user_message(code_groups)

    return [
        {"role": "system", "content": system_content, "cache": True},
        {"role": "user", "content": user_content, "cache": False},
    ]


def _build_normalization_system_prompt(ctx: dict) -> str:
    """Constrói o system prompt para normalização de códigos."""
    project_description = ctx.get("project_description") or ""
    guidelines = ctx.get("guidelines") or ""

    parts = [
        "You are a terminology normalization expert for a systematic review annotation project.",
        "Your task is to identify groups of code variants that refer to the same concept and",
        "suggest a single canonical form for each group.",
        "",
        "RULES:",
        "- Only suggest merging codes that are truly synonymous or near-identical variants.",
        "- Do NOT merge codes with meaningfully different scopes (e.g., 'Trust' and 'Social_Trust'",
        "  should only merge if they are used interchangeably in this corpus).",
        "- Prefer forms that use Title_Case_With_Underscores.",
        "- Preserve specificity: do not collapse meaningful distinctions.",
        "- If a group has no clear canonical form, set merge_confidence below 0.65.",
    ]

    if project_description:
        parts += ["", "PROJECT CONTEXT:", project_description]

    if guidelines:
        parts += ["", "ANNOTATION GUIDELINES:", guidelines]

    parts += [
        "",
        "OUTPUT FORMAT:",
        "For each group that should be canonicalized, output one block:",
        "",
        "  # $group: Variant1, Variant2, Variant3",
        "  # $suggested_canonical: CanonicalForm",
        "  # $merge_confidence: 0.85",
        "  # $reason: brief_reason",
        "  ---",
        "",
        "- merge_confidence: float 0.0–1.0 (how certain you are these should merge)",
        "- Only output blocks for groups you recommend merging (confidence ≥ 0.65).",
        "- Separate blocks with '---' on its own line.",
        "- Do not add explanatory prose outside the structured blocks.",
    ]

    return "\n".join(parts)


def _build_normalization_user_message(code_groups: list) -> str:
    """Constrói a mensagem do usuário com os grupos de código a normalizar."""
    lines = [
        "Below are groups of code variants found in the corpus.",
        "Each group has the same normalized key but different raw forms.",
        "For each group that should be merged under one canonical form, output a structured block.",
        "",
        "CODE GROUPS:",
        "",
    ]

    for group in code_groups:
        # group is a CodeGroup instance
        variants_summary = ", ".join(
            f"{form!r} (n={count})" for form, count in sorted(
                group.variants.items(), key=lambda x: -x[1]
            )
        )
        lines.append(f"  Group [{group.normalized_key}]: {variants_summary}")

    lines += [
        "",
        "Output structured normalization suggestions for groups that should be merged.",
        "If no groups need merging, output nothing.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ponto de entrada: modo suggest (Fase 5)
# ---------------------------------------------------------------------------


def build_topic_filter_prompt(available_topics: list, text: str) -> List[dict]:
    """Monta mensagens para o passo 1 do suggest: identificação de tópicos.

    Prompt mínimo — 32 tópicos cabe em ~50 tokens.
    Temperatura 0.0 (determinístico — lista fechada).

    Args:
        available_topics: Lista de nomes de tópicos do projeto.
        text: Trecho de texto a analisar.

    Returns:
        Lista de dicts no formato interno.
    """
    topics_line = ", ".join(available_topics)
    system_text = (
        "You are a research assistant. Given a text excerpt and a list of research "
        "topics, identify the 2-4 most relevant topics.\n"
        "Reply with ONLY the topic names, one per line. No explanations.\n\n"
        f"Topics:\n{topics_line}"
    )
    user_text = f'Text: "{text}"'

    return [
        {"role": "system", "content": system_text, "cache": False},
        {"role": "user", "content": user_text, "cache": False},
    ]


def build_suggest_prompt(ctx: dict, text: str, enriched_codes: str) -> List[dict]:
    """Monta mensagens para sugestão de códigos relevantes (passo 2 ou único).

    O system prompt inclui a lista de códigos enriquecida com frequências e
    descrições ontológicas. O user prompt contém apenas o texto a analisar.

    Args:
        ctx: Contexto do projeto retornado por load_project().
        text: Trecho de texto a analisar.
        enriched_codes: String formatada com códigos, frequências e descrições
            (gerada por suggest_mode._build_enriched_code_list).

    Returns:
        Lista de dicts no formato interno.
    """
    project_ctx = ""
    if ctx.get("project_description"):
        desc = ctx["project_description"]
        if len(desc) > 200:
            desc = desc[:200] + "..."
        project_ctx = f"\nProject context: {desc}\n"

    system_text = (
        "You are a qualitative research assistant.\n"
        "Given a text excerpt and a list of analytical codes, suggest 3-5 existing "
        "codes that best match the text. For each, explain briefly why (max 15 words).\n"
        "If no existing code fits well, suggest ONE new code marked [NEW].\n"
        "Prefer existing codes over new ones.\n\n"
        "Reply format (one per line):\n"
        "• Code_Name - brief reason\n"
        f"{project_ctx}\n"
        f"Analytical codes:\n{enriched_codes}"
    )
    user_text = (
        f'Text: "{text}"\n\n'
        "Suggest the most relevant codes."
    )

    return [
        {"role": "system", "content": system_text, "cache": False},
        {"role": "user", "content": user_text, "cache": False},
    ]
