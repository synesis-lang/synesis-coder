"""Carregamento de contexto do projeto via compilador Synesis.

Todo acesso ao projeto passa por synesis.load(). Esta é a única função
que invoca o compilador — todos os módulos subsequentes recebem o dict
retornado como contexto (ctx).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import synesis
from synesis.ast.nodes import FieldType, Scope


def load_project(
    project_path: Path,
    load_annotations: bool = True,
    load_ontology: bool = False,
) -> dict:
    """Carrega o projeto via synesis.load() e retorna contexto completo.

    Args:
        project_path: Caminho para o arquivo .synp.
        load_annotations: Se True, carrega anotações .syn existentes para
            popular code_index e topic_index.
        load_ontology: Se True, carrega também os arquivos .syno (necessário
            apenas no modo ontology). Padrão False — evita erros em projetos
            cujo .syno usa campos não definidos no template atual.

    Returns:
        dict com chaves:
            "result"              — MemoryCompilationResult completo
            "field_specs"         — Dict[str, FieldSpec] (todos os campos)
            "source_fields"       — Dict[str, FieldSpec] filtrado por SCOPE SOURCE
            "item_fields"         — Dict[str, FieldSpec] filtrado por SCOPE ITEM
            "ontology_fields"     — Dict[str, FieldSpec] filtrado por SCOPE ONTOLOGY
            "has_ontology_scope"  — bool: template define campos ONTOLOGY?
            "has_chain_field"     — bool: existe campo CHAIN no SCOPE ITEM?
            "chain_field_name"    — Optional[str]: nome do campo CHAIN (se existe)
            "chain_relations"     — Dict[str, str]: relações do campo CHAIN
            "required_item"       — List[str]: campos REQUIRED no SCOPE ITEM
            "required_source"     — List[str]: campos REQUIRED no SCOPE SOURCE
            "bundle_pairs"        — List[Tuple[str,...]]: bundles do SCOPE ITEM
            "code_index"          — dict: {"codes", "stats", "empty"}
            "topic_index"         — dict: {"topics", "topic_members", "empty"}
            "ontology_index"      — Dict[str, OntologyNode]
            "project_description" — Optional[str]: descrição do .synp
            "project_content"     — str
            "template_content"    — str
            "bib_content"         — Optional[str]
            "project_path"        — Path

    Raises:
        FileNotFoundError: Se o arquivo .synp ou o template referenciado não existir.
        ValueError: Se o compilador reportar erros fatais.
    """
    project_path = Path(project_path).resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"Projeto não encontrado: {project_path}")

    project_content = project_path.read_text(encoding="utf-8")
    base_dir = project_path.parent

    # Localizar template referenciado no .synp
    template_path = _resolve_template_path(project_content, base_dir)
    if not template_path.exists():
        raise FileNotFoundError(f"Template não encontrado: {template_path}")
    template_content = template_path.read_text(encoding="utf-8")

    # Coletar includes — .bib sempre carregado (necessário para validação)
    annotation_contents: Dict[str, str] = {}
    ontology_contents: Dict[str, str] = {}
    bib_content: Optional[str] = None

    _ann, _all_ontology, bib_content = _collect_includes(project_content, base_dir)
    if load_annotations:
        annotation_contents = _ann
    if load_ontology:
        ontology_contents = _all_ontology

    # Compilar via synesis.load() — única chamada ao compilador
    result = synesis.load(
        project_content=project_content,
        template_content=template_content,
        annotation_contents=annotation_contents or None,
        ontology_contents=ontology_contents or None,
        bibliography_content=bib_content,
        project_filename=project_path.name,
        template_filename=template_path.name,
    )

    if not result.success and result.has_errors():
        diagnostics = result.get_diagnostics()
        raise ValueError(
            f"Erro ao compilar projeto '{project_path.name}':\n{diagnostics}"
        )

    field_specs = result.template.field_specs

    # Separar campos por escopo
    source_fields = {
        name: spec
        for name, spec in field_specs.items()
        if spec.scope == Scope.SOURCE
    }
    item_fields = {
        name: spec
        for name, spec in field_specs.items()
        if spec.scope == Scope.ITEM
    }
    ontology_fields = {
        name: spec
        for name, spec in field_specs.items()
        if spec.scope == Scope.ONTOLOGY
    }

    # Detectar campo CHAIN no SCOPE ITEM
    chain_field_name: Optional[str] = None
    chain_relations: Dict[str, str] = {}
    for name, spec in item_fields.items():
        if spec.type == FieldType.CHAIN:
            chain_field_name = name
            if spec.relations:
                chain_relations = {
                    rel_name: str(rel_desc)
                    for rel_name, rel_desc in spec.relations.items()
                }
            break

    # Campos required e bundles do SCOPE ITEM/SOURCE
    required_item: List[str] = list(
        result.template.required_fields.get(Scope.ITEM, [])
    )
    required_source: List[str] = list(
        result.template.required_fields.get(Scope.SOURCE, [])
    )
    bundle_pairs: List[Tuple[str, ...]] = list(
        result.template.bundled_fields.get(Scope.ITEM, [])
    )

    # Índices derivados do linked_project
    linked = result.linked_project
    code_index = _build_code_index(linked)
    topic_index = _build_topic_index(linked)
    ontology_index = linked.ontology_index if linked else {}

    # Descrição do projeto (já processada pelo compilador)
    project_description: Optional[str] = None
    if linked and linked.project.description:
        project_description = linked.project.description

    return {
        "result": result,
        "field_specs": field_specs,
        "source_fields": source_fields,
        "item_fields": item_fields,
        "ontology_fields": ontology_fields,
        "has_ontology_scope": bool(ontology_fields),
        "has_chain_field": chain_field_name is not None,
        "chain_field_name": chain_field_name,
        "chain_relations": chain_relations,
        "required_item": required_item,
        "required_source": required_source,
        "bundle_pairs": bundle_pairs,
        "code_index": code_index,
        "topic_index": topic_index,
        "ontology_index": ontology_index,
        "project_description": project_description,
        "project_content": project_content,
        "template_content": template_content,
        "bib_content": bib_content,
        "annotation_contents": annotation_contents,  # para validação de ITEMs isolados
        "project_path": project_path,
    }


# ---------------------------------------------------------------------------
# Funções auxiliares (privadas)
# ---------------------------------------------------------------------------


def _resolve_template_path(project_content: str, base_dir: Path) -> Path:
    """Extrai o caminho do template do conteúdo do .synp."""
    import re

    match = re.search(r'TEMPLATE\s+"([^"]+)"', project_content, re.IGNORECASE)
    if not match:
        raise ValueError("Diretiva TEMPLATE não encontrada no arquivo .synp")
    return base_dir / match.group(1)


def _collect_includes(
    project_content: str, base_dir: Path
) -> Tuple[Dict[str, str], Dict[str, str], Optional[str]]:
    """Lê arquivos referenciados nas diretivas INCLUDE do .synp.

    Retorna (annotation_contents, ontology_contents, bib_content).
    Arquivos ausentes são silenciosamente ignorados.
    """
    import re

    annotation_contents: Dict[str, str] = {}
    ontology_contents: Dict[str, str] = {}
    bib_content: Optional[str] = None

    include_pattern = re.compile(
        r'INCLUDE\s+(ANNOTATIONS|ONTOLOGY|BIBLIOGRAPHY)\s+"([^"]+)"',
        re.IGNORECASE,
    )

    for match in include_pattern.finditer(project_content):
        include_type = match.group(1).upper()
        filename = match.group(2)
        file_path = base_dir / filename

        if not file_path.exists():
            continue

        content = file_path.read_text(encoding="utf-8")

        if include_type == "ANNOTATIONS":
            annotation_contents[filename] = content
        elif include_type == "ONTOLOGY":
            ontology_contents[filename] = content
        elif include_type == "BIBLIOGRAPHY":
            bib_content = content

    return annotation_contents, ontology_contents, bib_content


def _build_code_index(linked) -> dict:
    """Constrói índice de conceitos existentes no projeto.

    Combina duas fontes:
    - code_usage: campos do tipo CODE (ex: aids_corpus, nave)
    - all_triples: nós de campos CHAIN (ex: social_acceptance)

    Projetos que usam apenas CHAIN (sem campo CODE) ainda terão o code_index
    populado com os conceitos das chains existentes.

    Returns:
        dict com:
            "codes"  — List[str] ordenada de todos os conceitos
            "stats"  — Dict[str, int] frequência de cada conceito
            "empty"  — bool
    """
    if not linked:
        return {"codes": [], "stats": {}, "empty": True}

    # Fonte 1: campos CODE
    usage = linked.code_usage
    stats: dict = {code: len(items) for code, items in usage.items()}

    # Fonte 2: nós de CHAIN via all_triples (A, RELATION, B)
    # Relações são strings em MAIÚSCULAS ou com hífen — nós são os demais
    for triple in linked.all_triples:
        a, rel, b = triple
        for concept in (a, b):
            if concept not in stats:
                stats[concept] = 1
            else:
                stats[concept] += 1

    return {
        "codes": sorted(stats.keys()),
        "stats": stats,
        "empty": len(stats) == 0,
    }


def _build_topic_index(linked) -> dict:
    """Constrói índice de tópicos existentes a partir de topic_index.

    Returns:
        dict com:
            "topics"         — List[str] ordenada de tópicos
            "topic_members"  — Dict[str, List[str]] conceitos sob cada tópico
            "empty"          — bool
    """
    if not linked:
        return {"topics": [], "topic_members": {}, "empty": True}

    ti = linked.topic_index
    return {
        "topics": sorted(ti.keys()),
        "topic_members": {t: sorted(members) for t, members in ti.items()},
        "empty": len(ti) == 0,
    }
