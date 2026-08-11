"""Carregamento de contexto do projeto via compilador Synesis.

Todo acesso ao projeto passa por synesis.load(). Esta é a única função
que invoca o compilador — todos os módulos subsequentes recebem o dict
retornado como contexto (ctx).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import synesis
from synesis.ast.nodes import FieldType, Scope

logger = logging.getLogger(__name__)


def load_project(
    project_path: Path,
    load_annotations: bool = True,
    load_ontology: bool = False,
    tolerate_annotation_errors: bool = False,
    dataset_glob_override: Optional[str] = None,
) -> dict:
    """Carrega o projeto via synesis.load() e retorna contexto completo.

    Args:
        project_path: Caminho para o arquivo .synp.
        load_annotations: Se True, carrega anotações .syn existentes para
            popular code_index e topic_index.
        load_ontology: Se True, carrega também os arquivos .syno (necessário
            apenas no modo ontology). Padrão False — evita erros em projetos
            cujo .syno usa campos não definidos no template atual.
        tolerate_annotation_errors: Se True, erros nas anotações .syn existentes
            são emitidos como warnings em vez de abortar. Erros de template,
            bibref e sintaxe do .synp continuam abortando. Usar em modos
            geradores (document) onde o output substituirá as anotações atuais.
        dataset_glob_override: Se informado, substitui o glob de
            `INCLUDE DATASET "<glob>"` declarado no .synp (ex.: para restringir
            a um único arquivo TOML em teste pontual) sem editar o projeto no
            disco. Resolvido relativo ao mesmo `base_dir` do .synp. Ignorado
            quando o projeto não declara `INCLUDE DATASET`.

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
            "bib_keys"            — List[str]: chaves do .bib (ordenadas)
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

    # Dataset TOML (ON DATASET): a chave de indexação vem do template, então o
    # template é parseado aqui, antes de synesis.load(), para descobri-la. O
    # dataset_index é passado ao compilador para resolver os valores ON DATASET.
    from synesis.parser.template_loader import load_template_from_string

    _template_for_dataset = load_template_from_string(
        template_content, template_path.name
    )
    dataset_index = _load_dataset(
        project_content, base_dir, _template_for_dataset, dataset_glob_override
    )

    # Compilar via synesis.load() — única chamada ao compilador
    result = synesis.load(
        project_content=project_content,
        template_content=template_content,
        annotation_contents=annotation_contents or None,
        ontology_contents=ontology_contents or None,
        bibliography_content=bib_content,
        dataset_index=dataset_index,
        project_filename=project_path.name,
        template_filename=template_path.name,
    )

    if not result.success and result.has_errors():
        if tolerate_annotation_errors:
            _split_and_tolerate_errors(result, annotation_contents, project_path)
        else:
            diagnostics = result.get_diagnostics(verbose=False)
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

    # Campos required e bundles do SCOPE ITEM/SOURCE/ONTOLOGY
    required_item: List[str] = list(
        result.template.required_fields.get(Scope.ITEM, [])
    )
    required_source: List[str] = list(
        result.template.required_fields.get(Scope.SOURCE, [])
    )
    required_ontology: List[str] = list(
        result.template.required_fields.get(Scope.ONTOLOGY, [])
    )
    bundle_pairs: List[Tuple[str, ...]] = list(
        result.template.bundled_fields.get(Scope.ITEM, [])
    )

    # Índices derivados do linked_project
    linked = result.linked_project
    code_index = _build_code_index(linked)
    topic_index = _build_topic_index(linked)
    ontology_index = linked.ontology_index if linked else {}

    # Chaves do .bib já parseado pelo compilador (sem reparse)
    bib_keys: List[str] = sorted(result.bibliography.keys()) if result.bibliography else []

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
        "required_ontology": required_ontology,
        "bundle_pairs": bundle_pairs,
        "code_index": code_index,
        "topic_index": topic_index,
        "ontology_index": ontology_index,
        "bib_keys": bib_keys,
        "project_description": project_description,
        "project_content": project_content,
        "template_content": template_content,
        "bib_content": bib_content,
        "dataset_index": dataset_index,  # registros TOML (ON DATASET); None se ausente
        "annotation_contents": annotation_contents,  # para validação de ITEMs isolados
        "project_path": project_path,
        "output_language": os.environ.get("SYNESIS_CODER_LANGUAGE", "").strip() or None,
    }


def assert_bibref_known(ctx: dict, bibref: str) -> None:
    """Valida que o bibref informado existe no .bib do projeto; aborta se não.

    Pré-validação com abort precoce (Parte B do plano JSON Assembler): o erro
    dominante em runs reais é E001 (bibref inexistente), que nenhuma melhoria
    de formato de saída resolve. Validar aqui evita gastar toda uma execução
    LLM com um bibref que falharia em todo chunk.

    Não adivinha nem auto-deriva o bibref — apenas valida e aborta com uma
    mensagem que lista as chaves disponíveis e, quando o .synp traz uma
    DESCRIPTION, cita-a (a convenção do projeto costuma estar ali).

    Args:
        ctx: Contexto retornado por load_project() (precisa conter "bib_keys").
        bibref: Referência informada pelo usuário (com ou sem "@" à frente).

    Raises:
        ValueError: Se bibref normalizado não estiver entre ctx["bib_keys"].
    """
    key = bibref.lstrip("@").strip()
    bib_keys: List[str] = ctx.get("bib_keys", [])

    if key in bib_keys:
        return

    if not bib_keys:
        import re

        project_content = ctx.get("project_content", "")
        has_bibliography_directive = bool(
            re.search(r"INCLUDE\s+BIBLIOGRAPHY\s+\"", project_content, re.IGNORECASE)
        )
        if not has_bibliography_directive:
            # Projeto sem INCLUDE BIBLIOGRAPHY: SOURCE é definido exclusivamente
            # pelo template (synesis core >= 0.6.0), sem bibref para validar
            # contra um .bib. Nada a validar aqui — o compilador já teria
            # abortado o load_project() se o .synp fosse inválido para esse modo.
            return

        raise ValueError(
            f"Bibref '@{key}' não pôde ser validado: o projeto declara "
            "INCLUDE BIBLIOGRAPHY no arquivo .synp mas a bibliografia (.bib) "
            "não carregou nenhuma chave. Verifique se o arquivo .bib existe "
            "e não está vazio."
        )

    # Amostra das chaves disponíveis (todas se forem poucas)
    sample = bib_keys if len(bib_keys) <= 20 else bib_keys[:20]
    sample_str = ", ".join(sample)
    if len(bib_keys) > 20:
        sample_str += f", … (+{len(bib_keys) - 20} outras)"

    lines = [
        f"Bibref '@{key}' não existe na bibliografia do projeto "
        f"({len(bib_keys)} chave(s) disponível(is)).",
        f"Chaves disponíveis: {sample_str}",
    ]

    description = ctx.get("project_description")
    if description:
        lines.append(
            "Convenção do projeto (DESCRIPTION do .synp):\n" + description.strip()
        )

    lines.append(
        "Informe um --bibref que corresponda exatamente a uma chave do .bib."
    )

    raise ValueError("\n".join(lines))


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

    Delega a resolução de caminhos aos utilitários do compilador
    (`synesis.parser.paths`) em vez de reimplementá-la, de modo que as três
    formas aceitas pelo .synp funcionem aqui exatamente como no `synesis
    compile`:

    - GLOB (`"annotations/*.syn"`): expandido por `resolve_glob`, que confina
      o resultado ao diretório do projeto (`../*.syn` não escapa).
    - SHARED (`INCLUDE SHARED ONTOLOGY "../ontologia.syno"`): a palavra
      `SHARED` autoriza alvo fora do projeto, e é repassada a
      `resolve_include(shared=True)` — sem ela a ontologia compartilhada
      seria recusada por ESCAPES_PROJECT.
    - Caminho literal: resolvido por `resolve_include`.

    Arquivos ausentes ou ilegíveis continuam sendo ignorados silenciosamente
    (o compilador é quem reporta o diagnóstico ao usuário); aqui só interessa
    popular os índices de contexto.
    """
    import re

    from synesis.parser.paths import has_glob, resolve_glob, resolve_include

    annotation_contents: Dict[str, str] = {}
    ontology_contents: Dict[str, str] = {}
    bib_content: Optional[str] = None

    # `SHARED` é opcional e não-capturante: só ONTOLOGY o aceita na gramática,
    # mas tolerá-lo aqui para os três tipos mantém a regex simples sem risco
    # (um `INCLUDE SHARED ANNOTATIONS` inválido seria barrado pelo compilador).
    include_pattern = re.compile(
        r'INCLUDE\s+(SHARED\s+)?(ANNOTATIONS|ONTOLOGY|BIBLIOGRAPHY)\s+"([^"]+)"',
        re.IGNORECASE,
    )

    for match in include_pattern.finditer(project_content):
        is_shared = match.group(1) is not None
        include_type = match.group(2).upper()
        raw = match.group(3)

        if has_glob(raw):
            paths, _outside = resolve_glob(base_dir, raw)
        else:
            resolution = resolve_include(base_dir, raw, shared=is_shared)
            paths = [resolution.path] if resolution.error is None else []

        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            # Chave: caminho relativo ao projeto quando possível (identifica o
            # arquivo real expandido do glob, não o padrão), com fallback para
            # o nome — o compilador usa isso só como rótulo de diagnóstico.
            try:
                key = path.relative_to(base_dir.resolve()).as_posix()
            except ValueError:
                key = path.name

            if include_type == "ANNOTATIONS":
                annotation_contents[key] = content
            elif include_type == "ONTOLOGY":
                ontology_contents[key] = content
            elif include_type == "BIBLIOGRAPHY":
                bib_content = content

    return annotation_contents, ontology_contents, bib_content


def _dataset_key_path(template) -> Optional[str]:
    """Descobre o caminho da chave de indexação do dataset a partir do template.

    A chave é o `dataset_path` do campo SCOPE SOURCE que também é `IDENTIFIES`
    (D3/D8): é a identidade do registro. Se nenhum campo IDENTIFIES tiver
    ON DATASET, usa o primeiro campo SOURCE com ON DATASET como fallback.
    O loader é agnóstico — quem sabe a chave é o template, não o loader.
    """
    from synesis.ast.nodes import Scope

    fallback: Optional[str] = None
    for spec in template.field_specs.values():
        if getattr(spec, "value_origin", "document") != "dataset":
            continue
        if spec.scope != Scope.SOURCE:
            continue
        path = getattr(spec, "dataset_path", None)
        if path is None:
            continue
        if getattr(spec, "identifies", None):
            return path
        if fallback is None:
            fallback = path
    return fallback


def _load_dataset(
    project_content: str,
    base_dir: Path,
    template,
    glob_override: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Carrega o dataset TOML declarado por INCLUDE DATASET no .synp.

    Retorna None quando o projeto não declara INCLUDE DATASET ou o template não
    tem campo ON DATASET (chave indescobrível) — mantendo no-op para projetos
    sem dataset. Erros de chave/parse do loader propagam (falha explícita).

    `glob_override`, quando informado, substitui o glob extraído do .synp —
    permite restringir a um único arquivo TOML sem editar o projeto no disco.
    Só tem efeito se o projeto já declarar INCLUDE DATASET (não injeta um
    dataset em projeto que não tem a diretiva).
    """
    import re

    from synesis.parser.dataset_loader import load_dataset

    match = re.search(
        r'INCLUDE\s+DATASET\s+"([^"]+)"', project_content, re.IGNORECASE
    )
    if not match:
        return None
    key_path = _dataset_key_path(template)
    if key_path is None:
        return None
    glob = glob_override if glob_override is not None else match.group(1)
    return load_dataset(glob, key_path=key_path, base_dir=base_dir)


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


def _split_and_tolerate_errors(result, annotation_contents: dict, project_path: Path) -> None:
    """Separa erros por origem: aborta em erros de template/projeto; tolera erros de anotação.

    Usado por load_project(tolerate_annotation_errors=True) em modos geradores (document)
    onde as anotações pré-existentes podem estar desatualizadas em relação ao template.

    Erros cujo location.file aponta para um arquivo de anotação (.syn / .syno) são
    emitidos como warnings. Qualquer outro erro (template, bibref, .synp) aborta.
    """
    annotation_filenames = set(annotation_contents.keys())

    fatal_errors = []
    tolerated_errors = []

    for err in result.validation_result.errors:
        loc = getattr(err, "location", None)
        file_str = str(getattr(loc, "file", "")).replace("\\", "/")
        file_name = file_str.rsplit("/", 1)[-1] if "/" in file_str else file_str
        if file_name in annotation_filenames or file_str.endswith(".syn"):
            tolerated_errors.append(err)
        else:
            fatal_errors.append(err)

    if tolerated_errors:
        # Aggregate errors by message template (strip location prefix) for compact display
        from collections import Counter
        counts: Counter = Counter()
        for e in tolerated_errors:
            raw = getattr(e, "to_cli_line", lambda: str(e))()
            # Strip leading location ("arquivo.syn:12: ") to group by message
            msg = raw.split(": ", 2)[-1] if ": " in raw else raw
            counts[msg] += 1

        bullet_lines = "\n".join(
            f"       - {n}x {msg}" for msg, n in counts.most_common()
        )
        logger.warning(
            "Ignorando anotações anteriores (%d erros sob o template atual):\n%s",
            len(tolerated_errors), bullet_lines,
        )

    if fatal_errors:
        from synesis.ast.results import ValidationResult
        fatal_result = ValidationResult(
            errors=fatal_errors,
            warnings=result.validation_result.warnings,
            info=result.validation_result.info,
        )
        diagnostics = fatal_result.to_diagnostics(verbose=False)
        raise ValueError(
            f"Erro ao compilar projeto '{project_path.name}':\n{diagnostics}"
        )
