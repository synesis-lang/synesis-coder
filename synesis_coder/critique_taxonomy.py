"""Taxonomia universal de `reason` e sua aplicabilidade derivada do template.

Problema que resolve (Estudo_Critique_Escopo_e_Taxonomia §3 e §7.2): as cinco
categorias anteriores chegavam ao modelo como **nomes crus, sem definição**, e
duas delas concentravam 73% das ocorrências — não porque o corpus tivesse esses
defeitos, mas porque eram os rótulos mais genéricos de uma lista sem semântica.
`anchor_missing` cobria seis defeitos não relacionados.

Duas correções, em camadas:

1. **Camada universal (fixa).** Categorias definidas por RELAÇÃO entre anotação
   e fonte, sem citar nome de campo. Medição sobre 31 templates reais mostrou
   que nenhum NOME de campo é universal e nenhum TIPO é universal exceto TEXT —
   `zone` não existe fora do face85; 30% dos templates não têm CHAIN. Categorias
   ancoradas em nomes de campo seriam letra morta na maioria dos projetos.

2. **Camada do projeto (gerada).** Onde cada categoria se aplica *naquele*
   template, derivado mecanicamente de `FieldType` e da lista de obrigatórios.
   Num template sem CHAIN, `inverted` e `granularity` simplesmente não são
   emitidas — a categoria some do prompt em vez de confundir o modelo.

Nada aqui é escrito à mão por projeto: campo novo no template → aplicabilidade
nova, de graça.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from synesis.semantic.template import FieldSpec

# ---------------------------------------------------------------------------
# Camada universal
# ---------------------------------------------------------------------------

# Ordem importa: é a ordem em que chegam ao modelo, do defeito mais grave ao
# mais brando, terminando em `none` para que a saída padrão seja a última lida.
REASON_CATEGORIES: dict[str, str] = {
    "unsupported": (
        "The annotation asserts content the passage under review does not "
        "sustain at all. No textual basis in the evaluated excerpt."
    ),
    "overstated": (
        "The passage sustains something WEAKER than the annotation asserts: "
        "a stronger relation, a higher degree, or a more specific category "
        "than the text warrants."
    ),
    "inverted": (
        "The annotation reverses a direction, polarity or ordering that the "
        "passage states in the opposite sense."
    ),
    "granularity": (
        "The annotation fuses distinct concepts into one unit, or splits a "
        "single concept across units, in a way that harms reuse across the "
        "corpus. A correction MUST be more reusable than the original."
    ),
    "infidelity": (
        "A field required to reproduce the source verbatim does not do so: "
        "altered punctuation, typographic variants, truncation, silent "
        "ellipsis."
    ),
    "incomplete": (
        "A field the template REQUIRES is absent, or a required element of a "
        "structured value is missing."
    ),
    "none": (
        "No defect. Use whenever the template ADMITS the annotation, even if "
        "another reading would also be defensible."
    ),
}

VALID_REASONS = frozenset(REASON_CATEGORIES)

# Tipo de campo → categorias que fazem sentido para aquele tipo.
# Derivação puramente mecânica; `unsupported` e `none` valem sempre.
_TYPE_APPLICABILITY: dict[str, tuple[str, ...]] = {
    "QUOTATION": ("infidelity",),
    "CHAIN": ("overstated", "inverted", "granularity"),
    "SCALE": ("overstated", "inverted"),
    "ORDERED": ("overstated", "inverted"),
    "ENUMERATED": ("overstated",),
    "CODE": ("granularity",),
    "TOPIC": ("granularity",),
}

_UNIVERSAL = ("unsupported", "none")


def applicable_categories(item_fields: dict, required: list | None = None) -> dict[str, list[str]]:
    """Mapeia categoria → campos deste template em que ela pode ocorrer.

    Args:
        item_fields: {nome: FieldSpec} do escopo ITEM.
        required: nomes de campos obrigatórios (para `incomplete`).

    Returns:
        {categoria: [campos]}. Categorias sem nenhum campo aplicável são
        OMITIDAS — é o que impede rótulo inaplicável de chegar ao modelo.
    """
    mapping: dict[str, list[str]] = {}

    for name, spec in item_fields.items():
        type_name = getattr(spec.type, "name", str(spec.type))
        for category in _TYPE_APPLICABILITY.get(type_name, ()):
            mapping.setdefault(category, []).append(name)

    for name in required or []:
        if name in item_fields:
            mapping.setdefault("incomplete", []).append(name)

    if item_fields:
        mapping["unsupported"] = ["any field"]

    return mapping


def build_taxonomy_section(item_fields: dict, required: list | None = None) -> str:
    """Gera a seção de categorias + aplicabilidade para o system prompt.

    Emite apenas categorias aplicáveis a ESTE template, com suas definições.
    """
    applicable = applicable_categories(item_fields, required)
    if not applicable:
        return ""

    lines = [
        "REASON CATEGORIES — choose the ONE that best fits the primary defect.",
        "Each is defined by the relation between the annotation and the source:",
        "",
    ]

    for category, definition in REASON_CATEGORIES.items():
        if category != "none" and category not in applicable:
            continue  # inaplicável neste template — não emitir
        lines.append(f"  {category}")
        lines.append(f"    {definition}")
        fields = applicable.get(category)
        if fields and category != "none":
            lines.append(f"    → applies to: {', '.join(sorted(set(fields)))}")
        lines.append("")

    lines.append(
        "Use EXACTLY one of these names. Any other value is rejected and the "
        "review discarded."
    )
    return "\n".join(lines)


def validate_reason(value: str) -> tuple[str, bool]:
    """Valida `reason` contra o enum.

    Antes, `_parse_critique_response` só garantia que a chave existisse, com
    default "none" — qualquer string passava, e a descalibração ficava
    silenciosa (§7.2.5).

    Returns:
        (valor_normalizado, era_valido). Inválido vira "none", sinalizado.
    """
    normalized = (value or "").strip().lower()
    if normalized in VALID_REASONS:
        return normalized, True
    return "none", False
