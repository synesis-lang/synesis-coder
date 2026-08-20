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


def build_ontology_schema(ctx: dict, topics: Optional[list] = None) -> dict:
    """Monta o JSON Schema de um único objeto-ONTOLOGY (modo ontology).

    O LLM preenche apenas os VALORES dos campos declarados em ONTOLOGY FIELDS
    (ex.: ontology_description, topic); a moldura `ONTOLOGY <code> ... END
    ONTOLOGY` é responsabilidade do block_assembler. `additionalProperties:
    false` impede que chaves alucinadas (ex.: um "item"/"type" espúrio) cheguem
    ao assembler, eliminando por construção a classe de erro de sintaxe que o
    caminho de texto livre permitia.

    Quando `topics` é fornecido e o template tem um campo TOPIC, esse campo é
    restringido por `enum` aos tópicos já existentes no projeto — reforço extra
    contra tópico inválido (o LLM não pode inventar categoria fora do conjunto).
    """
    fields = ctx.get("ontology_fields", {})
    required = ctx.get("required_ontology", [])
    schema = _scope_object_schema(fields, required)

    if topics:
        for name, spec in fields.items():
            if spec.type == FieldType.TOPIC:
                schema["properties"][name] = {"enum": sorted(set(topics))}
                break

    return schema


def _nullable(fragment: dict) -> dict:
    """Torna um fragmento de schema aceitável como `null` além do seu tipo.

    Necessário porque o modo `strict` das structured outputs (OpenAI/Azure)
    exige que TODA chave de `properties` conste em `required`; a opcionalidade
    de um campo do template passa a ser expressa pelo tipo nullable, não pela
    ausência em `required` (ver `_scope_object_schema`).

    As três formas produzidas por `field_to_schema` são tratadas:
    - `{"type": T}`        -> `{"type": [T, "null"]}`
    - `{"enum": [...]}`    -> acrescenta `None` ao enum
    - `{"const": V}`       -> vira `{"enum": [V, None]}` (const não aceita união)
    """
    out = dict(fragment)

    if "type" in out:
        t = out["type"]
        if isinstance(t, list):
            if "null" not in t:
                out["type"] = [*t, "null"]
        elif t != "null":
            out["type"] = [t, "null"]
        return out

    if "enum" in out:
        values = list(out["enum"])
        if None not in values:
            values.append(None)
        out["enum"] = values
        return out

    if "const" in out:
        return {"enum": [out["const"], None]}

    return out


def _scope_object_schema(fields: dict, required: list) -> dict:
    """Constrói o schema de um objeto (ITEM ou SOURCE) a partir dos seus campos.

    Campos com origem-de-valor externa (ON BIBLIOGRAPHY / ON DATASET) são
    EXCLUÍDOS do schema: o compilador os resolve do .bib/TOML, então o LLM não
    deve gerá-los (evita valores fabricados p/ campos vazios na fonte).

    CONFORMIDADE COM `strict` (structured outputs): provedores OpenAI/Azure
    recusam o schema com HTTP 400 quando `required` não inclui todas as chaves
    de `properties` ("'required' is required to be supplied and to be an array
    including every key in properties"). Como `llm_client` envia `strict: True`,
    TODOS os campos entram em `required`; os que o template declara OPTIONAL
    são marcados como nullable (`_nullable`), preservando a semântica de
    "pode não vir" sem violar a spec. O assembler já descarta valores None,
    então um campo opcional omitido pelo modelo continua ausente no bloco .syn.

    Sem isso o caminho JSON falhava e caía silenciosamente para texto livre,
    descartando as restrições estruturais derivadas do template (enum de
    ENUMERATED/ORDERED, minimum/maximum de SCALE, enum de relações de CHAIN,
    additionalProperties=false) — justamente as garantias que este módulo
    existe para dar.
    """
    gen_fields = {
        name: spec
        for name, spec in fields.items()
        if getattr(spec, "value_origin", "document") not in ("bibliography", "dataset")
    }
    required_set = {name for name in required if name in gen_fields}

    properties: dict = {}
    for name, spec in gen_fields.items():
        fragment = field_to_schema(spec)
        properties[name] = fragment if name in required_set else _nullable(fragment)

    schema: dict = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if properties:
        # Ordem estável e determinística: segue a ordem dos campos do template.
        schema["required"] = list(properties.keys())
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

    if ftype == FieldType.ORDERED:
        # O dado de ORDERED é o ÍNDICE (o rótulo é só exibição — ver E088 no
        # compilador). Oferecer rótulos aqui produziria .syn/.syno inválidos.
        return _ordered_schema(spec)

    if ftype == FieldType.ENUMERATED:
        # ENUMERATED não tem ordem nem índice: o dado é o próprio rótulo.
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
    """Extrai os labels permitidos de um campo ENUMERATED."""
    if not spec.values:
        return []
    return [v.label for v in spec.values]


def _ordered_schema(spec: FieldSpec) -> dict:
    """Schema de ORDERED: um inteiro restrito aos índices declarados.

    COMPATIBILIDADE DE PROVEDOR: o Gemini (Google AI Studio) recusa com HTTP 400
    qualquer `enum` de valores NUMÉRICOS — com ou sem `type: integer` —, sob a
    mensagem enganosa "schema at top-level requires unspecified property '<x>'".
    Enum de strings passa; enum de inteiros não. Medido em produção contra
    `google/gemini-3.7-flash` (2026-08-20), onde cada recusa derrubava a chamada
    para texto livre, descartando justamente as garantias do schema.

    Como os índices de ORDERED são contíguos na prática (`[0]`, `[1]`, `[2]`…),
    `minimum`/`maximum` expressam a MESMA restrição e são universalmente
    aceitos. Quando houver lacuna na sequência (ex.: `[0..4]` e `[6..11]`, sem o
    5), a faixa admitiria um índice inexistente — aí o `enum` é obrigatório e
    preferimos a correção do dado à compatibilidade com um provedor.
    """
    indices = _ordered_indices(spec)
    if not indices:
        return {"type": "integer"}

    lo, hi = min(indices), max(indices)
    if sorted(indices) == list(range(lo, hi + 1)):
        return {"type": "integer", "minimum": lo, "maximum": hi}
    return {"enum": indices}


def _ordered_indices(spec: FieldSpec) -> list:
    """Extrai os índices permitidos de um campo ORDERED.

    Em ORDERED o índice é o dado gravado: ele define a ordem e não admite
    variantes de grafia. O rótulo pertence à declaração do template e é
    reconstituído na apresentação (inlay hint do LSP).

    Índices negativos são a sentinela de "sem prefixo `[N]`" do transformer e
    nunca são valores válidos.
    """
    if not spec.values:
        return []
    return [v.index for v in spec.values if v.index >= 0]


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
