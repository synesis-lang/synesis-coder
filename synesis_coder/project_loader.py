"""Carregamento de contexto do projeto via compilador Synesis.

Todo acesso ao projeto passa por synesis.load(). Esta é a única função
que invoca o compilador — todos os módulos subsequentes recebem o dict
retornado como contexto (ctx).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import synesis
from synesis.ast.nodes import FieldType, Scope

try:  # synesis >= 0.13: INCLUDE com arquivo, curinga ou pasta
    from synesis.parser.paths import INCLUDE_EXTENSIONS as _INCLUDE_EXTENSIONS
    from synesis.parser.paths import expand_include as _expand_include
except ImportError:  # synesis 0.12: só arquivo e curinga
    _expand_include = None
    _INCLUDE_EXTENSIONS = {}

logger = logging.getLogger(__name__)


def _load_accepts_bibliography_contents() -> bool:
    import inspect

    return "bibliography_contents" in inspect.signature(synesis.load).parameters


# synesis >= 0.13 recebe vários .bib separados e acusa chave repetida entre eles
# (E089); a 0.12 recebe um texto só. Detectado uma vez, para o coder funcionar
# com as duas enquanto a 0.13 não está publicada.
_LOAD_TAKES_BIB_DICT = _load_accepts_bibliography_contents()

# Separador entre .bib concatenados (entradas BibTeX não atravessam arquivos).
_BIB_SEPARATOR = "\n\n"


def _bibliography_kwargs(bib_contents: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Argumento de bibliografia para synesis.load(), conforme a versão instalada."""
    if not bib_contents:
        return {"bibliography_content": None}
    if _LOAD_TAKES_BIB_DICT:
        return {"bibliography_contents": dict(bib_contents)}
    return {"bibliography_content": _BIB_SEPARATOR.join(bib_contents.values())}


def bibliography_kwargs(ctx: dict) -> Dict[str, Any]:
    """Argumento de bibliografia de um ctx para as chamadas a synesis.load().

    Use sempre este helper em vez de `bibliography_content=ctx["bib_content"]`:
    com vários .bib, só ele preserva a separação por arquivo (E089).
    """
    contents = ctx.get("bib_contents")
    if contents is None and ctx.get("bib_content"):
        contents = {"<bibliography>": ctx["bib_content"]}
    return _bibliography_kwargs(contents)


def _project_questions(linked: Any) -> List[Tuple[str, str]]:
    """Questões de competência do .synp, na ordem do arquivo.

    `getattr`: compiladores anteriores ao bloco QUESTIONS não têm o atributo.
    Linhas fora da forma `<id>: <texto>` já são erro de compilação e ficam de fora.
    """
    if not linked:
        return []
    questions = getattr(linked.project, "questions", None) or []
    return [(q.id, q.text) for q in questions if getattr(q, "id", "") and getattr(q, "text", "")]


def _compose_project_context(
    description: Optional[str], questions: List[Tuple[str, str]]
) -> Optional[str]:
    """DESCRIPTION seguido das questões de competência, quando houver."""
    if not questions:
        return description or None
    lines = [
        "Competency questions (what this project's structure must allow to answer):",
        *[f"- {qid}: {text}" for qid, text in questions],
    ]
    block = "\n".join(lines)
    return f"{description.strip()}\n\n{block}" if description else block


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
                                    (BUNDLE + OPTIONAL BUNDLE)
            "required_bundles"    — List[Tuple[str,...]]: só BUNDLE (SCOPE ITEM)
            "optional_bundles"    — List[Tuple[str,...]]: só OPTIONAL BUNDLE (SCOPE ITEM)
            "code_index"          — dict: {"codes", "stats", "empty"}
            "topic_index"         — dict: {"topics", "topic_members", "empty"}
            "ontology_index"      — Dict[str, OntologyNode]
            "bib_keys"            — List[str]: chaves do .bib (ordenadas)
            "bib_entries"         — Dict[str, dict]: entradas parseadas (chave e campos em minúsculas)
            "project_description" — Optional[str]: descrição do .synp
            "project_content"     — str
            "template_content"    — str
            "bib_content"         — Optional[str]: todos os .bib concatenados
            "bib_contents"        — Dict[str, str]: cada .bib separado
            "project_path"        — Path

    Raises:
        FileNotFoundError: Se o arquivo .synp ou o template referenciado não existir.
        ValueError: Se o compilador reportar erros fatais.
    """
    project_path = Path(project_path).resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"Project not found: {project_path}")

    project_content = project_path.read_text(encoding="utf-8")
    base_dir = project_path.parent

    # Localizar template referenciado no .synp
    template_path = _resolve_template_path(project_content, base_dir)
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    template_content = template_path.read_text(encoding="utf-8")

    # Coletar includes — .bib sempre carregado (necessário para validação)
    annotation_contents: Dict[str, str] = {}
    ontology_contents: Dict[str, str] = {}

    _ann, _all_ontology, bib_contents = _collect_includes(project_content, base_dir)
    # Texto único, na ordem do .synp, para quem procura uma entrada por regex
    # (critique_mode, anchor_check). O compilador recebe os arquivos separados
    # (bibliography_kwargs), para acusar chave repetida entre eles (E089).
    bib_content: Optional[str] = (
        _BIB_SEPARATOR.join(bib_contents.values()) if bib_contents else None
    )
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
        dataset_index=dataset_index,
        **_bibliography_kwargs(bib_contents),
        project_filename=project_path.name,
        template_filename=template_path.name,
    )

    if not result.success and result.has_errors():
        if tolerate_annotation_errors:
            _split_and_tolerate_errors(result, annotation_contents, project_path)
        else:
            diagnostics = result.get_diagnostics(verbose=False)
            raise ValueError(
                f"Error compiling project '{project_path.name}':\n{diagnostics}"
            )

    field_specs = result.template.field_specs

    # Avisos sobre o template (não alteram nada; uma vez por template).
    from synesis_coder.template_lint import warn_template_issues

    warn_template_issues(template_path.name, template_content, field_specs)

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
    # BUNDLE e OPTIONAL BUNDLE vivem em estruturas distintas do TemplateNode
    # (`bundled_fields` e `optional_bundles`). Ler só a primeira deixava
    # `bundle_pairs` vazio para templates com OPTIONAL BUNDLE, e o prompt nunca
    # dizia que aqueles campos andam juntos (Estudo_Paridade_Bundle_Schema §3).
    required_bundles: List[Tuple[str, ...]] = [
        tuple(b) for b in result.template.bundled_fields.get(Scope.ITEM, [])
    ]
    optional_bundles: List[Tuple[str, ...]] = [
        tuple(b)
        for b in getattr(result.template, "optional_bundles", {}).get(Scope.ITEM, [])
    ]
    bundle_pairs: List[Tuple[str, ...]] = required_bundles + optional_bundles

    # Campos de um bundle passam a sair juntos e na ordem declarada no BUNDLE —
    # no prompt, no schema e no .syn —, em vez da ordem das definições FIELD.
    source_fields = _order_by_bundles(source_fields, _scope_bundles(result.template, Scope.SOURCE))
    item_fields = _order_by_bundles(item_fields, bundle_pairs)
    ontology_fields = _order_by_bundles(
        ontology_fields, _scope_bundles(result.template, Scope.ONTOLOGY)
    )

    # Índices derivados do linked_project
    linked = result.linked_project
    code_index = _build_code_index(linked)
    topic_index = _build_topic_index(linked)
    ontology_index = linked.ontology_index if linked else {}

    # Chaves do .bib já parseado pelo compilador (sem reparse)
    bib_keys: List[str] = sorted(result.bibliography.keys()) if result.bibliography else []
    bib_entries: Dict[str, dict] = dict(result.bibliography) if result.bibliography else {}

    # Descrição do projeto (já processada pelo compilador) e questões de
    # competência (bloco QUESTIONS, synesis >= 0.15). As questões entram no mesmo
    # contexto do DESCRIPTION, que todo prompt já leva como "PROJECT CONTEXT"; um
    # projeto sem QUESTIONS (ou um compilador sem o bloco) gera o texto de sempre.
    project_questions = _project_questions(linked)
    project_description = _compose_project_context(
        linked.project.description if linked else None, project_questions
    )

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
        "required_bundles": required_bundles,
        "optional_bundles": optional_bundles,
        "code_index": code_index,
        "topic_index": topic_index,
        "ontology_index": ontology_index,
        "bib_keys": bib_keys,
        "bib_entries": bib_entries,  # campos de cada entrada (ex.: abstract p/ o SOURCE)
        "project_description": project_description,
        "project_questions": project_questions,  # [(id, texto)] do bloco QUESTIONS
        "project_content": project_content,
        "template_content": template_content,
        "bib_content": bib_content,
        "bib_contents": bib_contents,  # {rótulo: conteúdo} de cada .bib
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
            f"Bibref '@{key}' could not be validated: the project declares "
            "INCLUDE BIBLIOGRAPHY in the .synp file but the bibliography (.bib) "
            "loaded no key. Check that the .bib file exists "
            "and is not empty."
        )

    # Amostra das chaves disponíveis (todas se forem poucas)
    sample = bib_keys if len(bib_keys) <= 20 else bib_keys[:20]
    sample_str = ", ".join(sample)
    if len(bib_keys) > 20:
        sample_str += f", … (+{len(bib_keys) - 20} outras)"

    lines = [
        f"Bibref '@{key}' does not exist in the project bibliography "
        f"({len(bib_keys)} key(s) available).",
        f"Available keys: {sample_str}",
    ]

    description = ctx.get("project_description")
    if description:
        lines.append(
            "Project convention (DESCRIPTION of the .synp):\n" + description.strip()
        )

    lines.append(
        "Give a --bibref that matches a .bib key exactly."
    )

    raise ValueError("\n".join(lines))


def assert_input_keys_in_project(ctx: dict, keys: List[str], input_name: str) -> None:
    """Checagem prévia de campanha: toda chave do --input precisa existir no projeto.

    O validador confere cada saída contra a bibliografia DO PROJETO (INCLUDE
    BIBLIOGRAPHY), não contra o --input. Se as chaves do lote não estão lá, todo
    registro começa com E001 — e, na rodada da Kely (face85, 2026-09-28), o
    laço de correção trocou 47 das 65 chaves pela sugestão do E001 e as marcou
    OK. Aqui a campanha aborta antes da primeira chamada, sem custo.

    Projeto sem INCLUDE BIBLIOGRAPHY não valida bibref: nada a checar.

    Raises:
        ValueError: listando as chaves ausentes e como incluir o arquivo.
    """
    import re

    project_content = ctx.get("project_content", "")
    if not re.search(r"INCLUDE\s+BIBLIOGRAPHY\s+\"", project_content, re.IGNORECASE):
        return
    known = {k.lower() for k in ctx.get("bib_keys", [])}
    missing = [k for k in keys if k.lstrip("@").strip().lower() not in known]
    if not missing:
        return
    sample = ", ".join(missing[:10])
    if len(missing) > 10:
        sample += f", … (+{len(missing) - 10})"
    raise ValueError(
        f"{len(missing)} of {len(keys)} reference(s) of '{input_name}' are not in the "
        f"project bibliography: {sample}.\n"
        "The coder validates each output against the .synp's INCLUDE BIBLIOGRAPHY, not against "
        "the --input. Include the file in the project — for example "
        'INCLUDE BIBLIOGRAPHY "Sources" (the whole folder, synesis >= 0.13) — and run again.\n'
        "No model call was made."
    )


# ---------------------------------------------------------------------------
# Funções auxiliares (privadas)
# ---------------------------------------------------------------------------


def _resolve_template_path(project_content: str, base_dir: Path) -> Path:
    """Extrai o caminho do template do conteúdo do .synp."""
    import re

    match = re.search(r'TEMPLATE\s+"([^"]+)"', project_content, re.IGNORECASE)
    if not match:
        raise ValueError("TEMPLATE directive not found in the .synp file")
    return base_dir / match.group(1)


def project_include_files(project_path: Path) -> List[Dict[str, Any]]:
    """Arquivos incluídos pelo .synp, com o que importa para gravar neles.

    Cada item: {"type", "path" (resolvido), "key" (como em _collect_includes),
    "shared" (INCLUDE SHARED), "single" (o INCLUDE cita um arquivo, não pasta nem
    padrão)}. A expansão é a do compilador; INCLUDE comentado não conta.
    """
    project_path = Path(project_path)
    content = project_path.read_text(encoding="utf-8")
    base_dir = project_path.parent
    pattern = re.compile(
        r'^[ \t]*INCLUDE\s+(SHARED\s+)?(ANNOTATIONS|ONTOLOGY|BIBLIOGRAPHY)\s+"([^"]+)"',
        re.IGNORECASE | re.MULTILINE,
    )
    from synesis.parser.paths import has_glob, resolve_include

    out: List[Dict[str, Any]] = []
    for match in pattern.finditer(content):
        shared = match.group(1) is not None
        include_type = match.group(2).upper()
        raw = match.group(3)
        if _expand_include is not None:
            expansion = _expand_include(
                base_dir, raw, _INCLUDE_EXTENSIONS.get(include_type), shared=shared,
            )
            paths = list(expansion.files)
            single = getattr(expansion, "kind", "file") == "file"
        else:
            resolution = resolve_include(base_dir, raw, shared=shared)
            paths = [resolution.path] if resolution.error is None else []
            single = not has_glob(raw)
        for path in paths:
            path = Path(path).resolve()
            try:
                key = path.relative_to(base_dir.resolve()).as_posix()
            except ValueError:
                key = path.name
            out.append({
                "type": include_type, "path": path, "key": key,
                "shared": shared, "single": single,
            })
    return out


def _collect_includes(
    project_content: str, base_dir: Path
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Lê arquivos referenciados nas diretivas INCLUDE do .synp.

    Retorna (annotation_contents, ontology_contents, bib_contents). Os três são
    dicts `{rótulo: conteúdo}` na ordem do .synp: várias linhas INCLUDE, curingas
    e pastas se somam — inclusive a bibliografia, que antes guardava só o ÚLTIMO
    arquivo (cada .bib sobrescrevia o anterior).

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
    bib_contents: Dict[str, str] = {}

    # `SHARED` é opcional e não-capturante: só ONTOLOGY o aceita na gramática,
    # mas tolerá-lo aqui para os três tipos mantém a regex simples sem risco
    # (um `INCLUDE SHARED ANNOTATIONS` inválido seria barrado pelo compilador).
    # Ancorada no início da linha: um INCLUDE comentado (`# INCLUDE SHARED
    # ONTOLOGY "../ontologia.syno"`) não é include. Sem a âncora, o coder lia o
    # arquivo que o compilador ignora — e um .syno vazio derrubava o modo
    # ontology com "Token inesperado <EOF>" (Phishing/UFG, 2026-10-14).
    include_pattern = re.compile(
        r'^[ \t]*INCLUDE\s+(SHARED\s+)?(ANNOTATIONS|ONTOLOGY|BIBLIOGRAPHY)\s+"([^"]+)"',
        re.IGNORECASE | re.MULTILINE,
    )

    for match in include_pattern.finditer(project_content):
        is_shared = match.group(1) is not None
        include_type = match.group(2).upper()
        raw = match.group(3)

        if _expand_include is not None:
            # synesis >= 0.13: arquivo, curinga OU pasta, pela regra do compilador
            # (mesma ordem, mesma contenção ao projeto).
            paths = list(_expand_include(
                base_dir, raw, _INCLUDE_EXTENSIONS.get(include_type), shared=is_shared,
            ).files)
        elif has_glob(raw):
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
                bib_contents.setdefault(key, content)

    return annotation_contents, ontology_contents, bib_contents


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

    from synesis.parser.dataset_loader import SOURCE_FILE_KEY, load_dataset

    declared = re.findall(
        r'INCLUDE\s+DATASET\s+"([^"]+)"', project_content, re.IGNORECASE
    )
    if not declared:
        return None
    key_path = _dataset_key_path(template)
    if key_path is None:
        return None
    if glob_override is not None or _expand_include is None:
        # Override pontual (--dataset) ou synesis 0.12: o comportamento anterior.
        glob = glob_override if glob_override is not None else declared[0]
        return load_dataset(glob, key_path=key_path, base_dir=base_dir)

    # Todas as linhas INCLUDE DATASET, pela regra do compilador: arquivo,
    # curinga ou pasta, restritos ao projeto — assim o coder aceita exatamente o
    # dataset que `synesis compile` aceita. Chave repetida entre arquivos aborta
    # (o compilador reporta E089): decidir o registro certo cabe ao pesquisador.
    index: Dict[str, Any] = {}
    for raw in declared:
        for path in _expand_include(base_dir, raw, _INCLUDE_EXTENSIONS.get("DATASET")).files:
            for key, record in load_dataset(path, key_path=key_path, base_dir=base_dir).items():
                if key in index:
                    raise ValueError(
                        f"Dataset key '{key}' repeated in "
                        f"'{index[key].get(SOURCE_FILE_KEY)}' and '{path}'. "
                        "Each record needs a unique key in the project."
                    )
                index[key] = record
    return index


def _scope_bundles(template, scope) -> List[Tuple[str, ...]]:
    """Bundles (BUNDLE + OPTIONAL BUNDLE) declarados para um escopo."""
    bundles = list(template.bundled_fields.get(scope, []))
    bundles += list(getattr(template, "optional_bundles", {}).get(scope, []))
    return [tuple(b) for b in bundles]


def _order_by_bundles(fields: dict, bundles: List[Tuple[str, ...]]) -> dict:
    """Reordena os campos para que os de um bundle fiquem juntos, na ordem do BUNDLE.

    Os demais campos mantêm a ordem das definições FIELD. O bundle entra na
    posição do primeiro dos seus campos a ser definido. Sem isso, um template
    que define `note` antes de `chain` recebia `note` antes de `chain` no
    prompt e no .syn, contra a ordem declarada em `BUNDLE chain, status, note`.

    A reordenação não altera o dado: o compilador associa os campos de um
    bundle por índice (`ItemNode.note_chain_pairs` usa `zip`) e o validador
    confere só presença e contagem.
    """
    if not bundles:
        return fields
    bundle_of = {name: b for b in bundles for name in b}
    ordered: dict = {}
    for name, spec in fields.items():
        if name in ordered:
            continue
        bundle = bundle_of.get(name)
        if bundle is None:
            ordered[name] = spec
            continue
        for member in bundle:
            if member in fields and member not in ordered:
                ordered[member] = fields[member]
    return ordered


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
            "Ignoring previous annotations (%d errors under the current template):\n%s",
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
            f"Error compiling project '{project_path.name}':\n{diagnostics}"
        )
