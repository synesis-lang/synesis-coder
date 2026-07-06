"""Modo incorporate: aplica revisões do .synr e emite .syn final.

Fase 4 do pipeline ACT — determinístico, sem LLM.

Fluxo:
    1. parse_synr(synr_path) → SynrDocument
    2. Auto-detectar ou carregar ctx do projeto (para validação por campo)
    3. Para cada bloco ITEM com tags # $<field>: (geradas pelo modo critique):
        a. Tentar substituir o valor do campo
        b. Validar o ITEM modificado via synesis.load()
        c. Se válido → aceitar; se inválido → preservar original + log warning
    4. _strip_revision_metadata() → remove cabeçalho .synr e blocos # REVISION
    5. Prepend cabeçalho # $metrics.* com estatísticas da incorporação
    6. Escrever .syn final
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from typing import Optional

import synesis

from synesis_coder.synr_io import (
    _END_ITEM,
    _ITEM_START,
    _REVISION_MARKER,
    parse_synr,
    safe_write_output,
)
from synesis_coder.validator import _has_structural_errors

_log = logging.getLogger(__name__)

# Tags que não são sugestões de campo — ignoradas durante a incorporação.
# `note` é incluída porque o LLM de critique usa `# $note:` como raciocínio
# (reason_detail), não como substituição do campo `note:` do ITEM.
# `reason_detail` é o tag explícito para explicações livres do LLM.
_META_TAGS = frozenset({"suspicion_score", "reason", "reason_detail", "note", "phase"})

# Regex que captura uma linha de campo Synesis: indentação + nome + : + valor
_FIELD_LINE_RE = re.compile(r"^(\s*)([\w]+)(\s*:\s*)(.*)$")

# Regex para detectar qualquer linha de metadata .synr (# $key: value)
_ANY_TAG_LINE = re.compile(r"^\s*#\s*\$[\w.]+\s*:.*$")


# ---------------------------------------------------------------------------
# Resolução do contexto do projeto
# ---------------------------------------------------------------------------


def _resolve_project_context(
    synr_path: Path,
    project_path: Optional[Path] = None,
) -> Optional[dict]:
    """Carrega ctx do projeto para validação. Retorna None se não encontrado.

    Se project_path não for fornecido, sobe até 4 níveis de diretório
    procurando por um arquivo .synp.
    """
    if project_path is not None:
        from synesis_coder.project_loader import load_project
        return load_project(project_path)

    search_dir = synr_path.resolve().parent
    for _ in range(5):
        synp_files = list(search_dir.glob("*.synp"))
        if synp_files:
            from synesis_coder.project_loader import load_project
            try:
                return load_project(synp_files[0])
            except Exception as exc:
                _log.warning("Falha ao carregar projeto %s: %s", synp_files[0], exc)
                return None
        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent

    _log.warning(
        "Projeto .synp não encontrado próximo a %s — incorporação sem validação por campo",
        synr_path,
    )
    return None


# ---------------------------------------------------------------------------
# Substituição de campo e validação
# ---------------------------------------------------------------------------


def _replace_field_value(
    item_block: str,
    field_name: str,
    new_value: str,
) -> Optional[str]:
    """Substitui o valor de um campo dentro de um bloco ITEM.

    Faz match case-insensitive no nome do campo. Quando há múltiplas ocorrências
    do mesmo campo (ex: vários `chain:` num ITEM complexo), tenta encontrar a
    ocorrência cujo valor atual é o prefixo-raiz da sugestão — ou seja, a
    ocorrência que o LLM estava endereçando.

    Estratégia de match quando há múltiplas ocorrências:
    1. Prefere a linha cujo valor atual aparece no início de new_value (match semântico).
    2. Fallback: primeira ocorrência do campo.

    Args:
        item_block: Texto completo do bloco ITEM.
        field_name: Nome do campo a substituir (ex: "chain", "code").
        new_value: Novo valor sugerido pelo LLM.

    Returns:
        Bloco modificado, ou None se o campo não foi encontrado.
    """
    field_lower = field_name.lower()
    lines = item_block.splitlines(keepends=True)

    # Coletar todas as posições de ocorrência do campo
    candidate_indices: list[int] = []
    candidate_values: list[str] = []
    for idx, line in enumerate(lines):
        m = _FIELD_LINE_RE.match(line.rstrip("\r\n"))
        if m:
            _, fname, _, old_val = m.groups()
            if fname.lower() == field_lower:
                candidate_indices.append(idx)
                candidate_values.append(old_val.strip())

    if not candidate_indices:
        return None

    # Selecionar qual ocorrência substituir
    target_idx = candidate_indices[0]  # fallback: primeira

    if len(candidate_indices) > 1:
        new_val_lower = new_value.strip().lower()
        for i, (idx, old_val) in enumerate(zip(candidate_indices, candidate_values)):
            # Extrai o nó-fonte da sugestão (primeiro token antes de ->)
            new_root = new_val_lower.split("->")[0].strip()
            old_root = old_val.lower().split("->")[0].strip()
            if new_root and old_root and old_root == new_root:
                target_idx = idx
                break

    # Substituir na linha escolhida
    result = list(lines)
    m = _FIELD_LINE_RE.match(lines[target_idx].rstrip("\r\n"))
    if m:
        indent, fname, sep, _ = m.groups()
        eol = "\n" if lines[target_idx].endswith("\n") else ""
        result[target_idx] = f"{indent}{fname}{sep}{new_value}{eol}"

    return "".join(result)


def _validate_item_block(item_block: str, ctx: dict) -> bool:
    """Verifica se um bloco ITEM compila sem erros estruturais via synesis.load().

    Ignora OrphanItem (ITEM sem SOURCE correspondente) — esperado ao validar
    um ITEM isolado.
    """
    try:
        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={"incorporate_test.syn": item_block},
            bibliography_content=ctx.get("bib_content"),
        )
        return not _has_structural_errors(result)
    except Exception as exc:
        _log.debug("synesis.load() levantou exceção durante validação: %s", exc)
        return False


def _apply_revision_tags(
    item_block: str,
    tags: dict[str, str],
    ctx: Optional[dict],
) -> tuple[str, int, int]:
    """Aplica sugestões de campo ao bloco ITEM com validação opcional.

    Para cada tag que não é meta-tag (suspicion_score, reason), tenta substituir
    o valor do campo correspondente no bloco ITEM e valida o resultado.

    Args:
        item_block: Texto do bloco ITEM a modificar.
        tags: Dict {key: value} extraído do bloco # REVISION.
        ctx: Contexto do projeto para validação. None = aceitar sem validar.

    Returns:
        (modified_block, fields_changed, fields_rejected)
    """
    modified = item_block
    changed = 0
    rejected = 0

    for key, new_value in tags.items():
        # Normaliza chaves numeradas (ex: "chain.1" → campo "chain")
        base_key = key.split(".")[0] if re.match(r"^[\w]+\.\d+$", key) else key
        if base_key in _META_TAGS or key.startswith("metrics."):
            continue

        candidate = _replace_field_value(modified, base_key, new_value)
        if candidate is None:
            _log.debug(
                "Campo '%s' não encontrado no bloco ITEM — sugestão ignorada", base_key
            )
            continue

        if ctx is not None:
            ok = _validate_item_block(candidate, ctx)
        else:
            ok = True

        if ok:
            modified = candidate
            changed += 1
            _log.debug("Campo '%s' atualizado para: %s", base_key, new_value)
        else:
            _log.warning(
                "Substituição de '%s' rejeitada — '%s' falhou validação Synesis",
                base_key,
                new_value,
            )
            rejected += 1

    return modified, changed, rejected


# ---------------------------------------------------------------------------
# Processamento dos blocos ITEM no conteúdo completo
# ---------------------------------------------------------------------------


def _process_item_blocks(
    content: str,
    item_revisions: list[tuple[str, dict[str, str]]],
    ctx: Optional[dict],
) -> tuple[str, dict]:
    """Itera pelos blocos ITEM do conteúdo, aplicando sugestões de revisão.

    Args:
        content: Conteúdo completo do arquivo .synr.
        item_revisions: Lista de (bibref, tags) por bloco ITEM, em ordem.
        ctx: Contexto do projeto para validação (ou None).

    Returns:
        (modified_content, metrics_dict)
    """
    metrics = {
        "total_items": len(item_revisions),
        "items_with_revision": sum(1 for _, tags in item_revisions if tags),
        "items_revised": 0,
        "fields_changed": 0,
        "fields_rejected": 0,
    }

    lines = content.splitlines(keepends=True)
    result: list[str] = []
    item_idx = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\r\n")

        if _ITEM_START.match(stripped):
            # Coletar todas as linhas do bloco ITEM
            item_lines = [line]
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i].rstrip("\r\n")):
                item_lines.append(lines[i])
                i += 1
            if i < len(lines):
                item_lines.append(lines[i])  # linha END ITEM

            item_block = "".join(item_lines)

            # Obter tags para este bloco
            if item_idx < len(item_revisions):
                _, tags = item_revisions[item_idx]
            else:
                tags = {}

            if tags:
                modified, changed, rejected = _apply_revision_tags(
                    item_block, tags, ctx
                )
                metrics["fields_changed"] += changed
                metrics["fields_rejected"] += rejected
                if changed > 0:
                    metrics["items_revised"] += 1
                result.append(modified)
            else:
                result.append(item_block)

            item_idx += 1
        else:
            result.append(line)

        i += 1

    return "".join(result), metrics


# ---------------------------------------------------------------------------
# Limpeza de metadados .synr
# ---------------------------------------------------------------------------


def _strip_revision_metadata(content: str) -> str:
    """Remove todas as linhas de metadados .synr do conteúdo.

    Remove:
    - Linhas `# $key: value` (header .synr e tags dentro de REVISION)
    - Linhas `# REVISION`

    Não remove comentários Synesis normais (# comentário sem $).
    Colapsa múltiplas linhas em branco consecutivas em no máximo uma.
    """
    lines = content.splitlines(keepends=True)
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Remover linha # $key: ...
        if _ANY_TAG_LINE.match(stripped):
            continue
        # Remover linha # REVISION
        if _REVISION_MARKER.match(stripped):
            continue
        result.append(line)

    # Colapsar linhas em branco consecutivas
    cleaned: list[str] = []
    prev_blank = False
    for line in result:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank

    return "".join(cleaned).strip()


# ---------------------------------------------------------------------------
# Cabeçalho de métricas
# ---------------------------------------------------------------------------


def _build_metrics_header(
    metrics: dict,
    synr_path: Path,
    synr_header: dict,
) -> str:
    """Constrói o cabeçalho de métricas para o .syn final gerado pelo incorporate.

    Inclui as métricas da Fase 4 (incorporate) com fórmulas explícitas e,
    quando disponíveis no cabeçalho do .synr de entrada, as métricas da fase
    anterior (critique ou normalize) igualmente documentadas.

    Args:
        metrics: Contadores da incorporação (total_items, fields_changed, etc.).
        synr_path: Caminho do .synr de origem (para rastreabilidade).
        synr_header: Cabeçalho do .synr lido por parse_synr() — pode conter
            métricas das fases 2 (critique) ou 3 (normalize).

    Returns:
        String multilinha com todas as linhas do cabeçalho.
    """
    total_fields = metrics["fields_changed"] + metrics["fields_rejected"]
    acs = (metrics["fields_changed"] / total_fields) if total_fields > 0 else 1.0
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []

    # --- Fase 4: Incorporate --------------------------------------------------
    lines += [
        "# --- Fase 4: Incorporate (determinístico, sem LLM) ---",
        f"# $metrics.source: {synr_path.name}",
        f"# $metrics.timestamp: {now}",
        f"# $metrics.total_items: {metrics['total_items']}",
        "# $metrics.total_items.formula: total de blocos ITEM encontrados no .synr de entrada",
        f"# $metrics.items_with_revision: {metrics['items_with_revision']}",
        "# $metrics.items_with_revision.formula: ITEMs com pelo menos uma tag de revisao ($campo:) no bloco de revisao",
        f"# $metrics.items_revised: {metrics['items_revised']}",
        "# $metrics.items_revised.formula: ITEMs onde >= 1 substituicao de campo foi aceita",
        f"# $metrics.fields_changed: {metrics['fields_changed']}",
        "# $metrics.fields_changed.formula: substituicoes aceitas apos validacao por synesis.load()",
        f"# $metrics.fields_rejected: {metrics['fields_rejected']}",
        "# $metrics.fields_rejected.formula: substituicoes rejeitadas (synesis.load() retornou erros estruturais)",
        f"# $metrics.acs: {acs:.3f}",
        "# $metrics.acs.formula: fields_changed / (fields_changed + fields_rejected)",
        "# $metrics.acs.description: proporcao de sugestoes aceitas; 1.0 = todas aceitas; 0.0 = todas rejeitadas",
    ]

    # --- Fase anterior (critique ou normalize), se disponível no .synr --------
    prior_phase = synr_header.get("phase")
    if prior_phase in ("critique", "normalize"):
        lines.append(f"# --- Fase anterior: {prior_phase} ---")
        _copy_header_metric(lines, synr_header, "model", f"modelo LLM usado na fase {prior_phase}")
        _copy_header_metric(lines, synr_header, "timestamp", "data/hora de execucao da fase")

        if prior_phase == "critique":
            _copy_header_metric(lines, synr_header, "threshold",
                "limiar de suspicion_score acima do qual o ITEM recebeu # REVISION")
            _copy_header_metric(lines, synr_header, "metrics.items_total",
                "total de blocos ITEM avaliados pelo modelo de critique")
            _copy_header_metric(lines, synr_header, "metrics.items_flagged",
                "ITEMs com suspicion_score >= threshold")
            _copy_header_metric_with_formula(
                lines, synr_header,
                "metrics.suspicion_rate",
                "metrics.suspicion_rate.formula",
                "metrics.suspicion_rate.description",
            )

        elif prior_phase == "normalize":
            _copy_header_metric(lines, synr_header, "confidence_threshold",
                "confianca minima exigida para aceitar sugestao do LLM normalizador")
            _copy_header_metric(lines, synr_header, "metrics.corpus_files",
                "numero de arquivos .synr processados em conjunto")
            _copy_header_metric(lines, synr_header, "metrics.corpus_items_total",
                "total de blocos ITEM em todos os arquivos do corpus")
            _copy_header_metric(lines, synr_header, "metrics.codes_unique",
                "codigos distintos encontrados no corpus (apos agrupamento por chave normalizada)")
            _copy_header_metric(lines, synr_header, "metrics.residual_groups",
                "grupos com > 1 variante apos normalizacao deterministica (enviados ao LLM)")
            _copy_header_metric(lines, synr_header, "metrics.llm_canonicals",
                "grupos onde o LLM sugeriu e a sugestao foi aceita acima de confidence_threshold")
            _copy_header_metric(lines, synr_header, "metrics.llm_canonicals.description", None)
            _copy_header_metric(lines, synr_header, "metrics.corpus_items_revised",
                "ITEMs do corpus com pelo menos um codigo substituido pela forma canonica")
            _copy_header_metric_with_formula(
                lines, synr_header,
                "metrics.normalization_rate",
                "metrics.normalization_rate.formula",
                "metrics.normalization_rate.description",
            )

    return "\n".join(lines)


def _copy_header_metric(
    lines: list[str],
    header: dict,
    key: str,
    description: str | None,
) -> None:
    """Copia uma métrica do cabeçalho .synr para as linhas do cabeçalho .syn.

    Emite `# $prior.<key>: <value>` e, se description fornecida, uma linha
    de descrição complementar. Silencioso se a chave não existir no header.
    """
    value = header.get(key)
    if value is None:
        return
    lines.append(f"# $prior.{key}: {value}")
    if description:
        lines.append(f"# $prior.{key}.description: {description}")


def _copy_header_metric_with_formula(
    lines: list[str],
    header: dict,
    key: str,
    formula_key: str,
    description_key: str,
) -> None:
    """Copia métrica com fórmula e descrição do cabeçalho .synr."""
    value = header.get(key)
    if value is None:
        return
    lines.append(f"# $prior.{key}: {value}")
    formula = header.get(formula_key)
    if formula:
        lines.append(f"# $prior.{key}.formula: {formula}")
    description = header.get(description_key)
    if description:
        lines.append(f"# $prior.{key}.description: {description}")


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_incorporate(
    synr_path: Path,
    project_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    format: str = "plain",
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Aplica revisões do .synr e emite um .syn final limpo.

    Lê o arquivo .synr, aplica as sugestões de campo validadas via synesis.load(),
    remove toda a metadada de revisão e escreve o .syn resultante com um cabeçalho
    de métricas.

    Args:
        synr_path: Caminho para o arquivo .synr com blocos # REVISION.
        project_path: Caminho para o .synp do projeto (para validação por campo).
            Se None, tenta auto-detectar buscando .synp no diretório do .synr
            e em diretórios pai (até 4 níveis).
        output_path: Caminho de saída do .syn final. Se None, usa o mesmo nome
            do .synr com extensão .syn.
        format: "plain" (resumo compacto) ou "verbose" (cabeçalho completo).
        overwrite: Se True, sobrescreve output existente sem confirmação.
        backup: Se True, cria backup (.syn.bak) antes de gravar.

    Returns:
        String com resumo da execução.

    Raises:
        FileNotFoundError: Se synr_path não existir.
        ValueError: Se o arquivo não puder ser interpretado como .synr válido.
    """
    synr_path = Path(synr_path).resolve()
    if not synr_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {synr_path}")

    # 1. Parsear o .synr
    doc = parse_synr(synr_path)

    # 2. Resolver contexto do projeto
    ctx = _resolve_project_context(synr_path, project_path)

    # 3. Aplicar revisões a cada bloco ITEM
    modified_content, metrics = _process_item_blocks(
        doc.content, doc.item_revisions, ctx
    )

    # 4. Limpar metadados .synr (# REVISION, # $key:, cabeçalho de fase)
    clean_content = _strip_revision_metadata(modified_content)

    # 5. Montar conteúdo final com cabeçalho de métricas
    metrics_header = _build_metrics_header(metrics, synr_path, doc.header)
    final_content = metrics_header + "\n\n" + clean_content + "\n"

    # 6. Determinar caminho de saída
    if output_path is None:
        stem = synr_path.stem
        output_path = synr_path.with_name(stem + ".syn")

    output_path = Path(output_path).resolve()
    safe_write_output(output_path, final_content, overwrite=overwrite, backup=backup)

    _log.info("Incorporate concluído: %s", output_path)

    # 7. Montar resumo
    total = metrics["total_items"]
    revised = metrics["items_revised"]
    changed = metrics["fields_changed"]
    rejected = metrics["fields_rejected"]
    total_fields = changed + rejected
    acs = (changed / total_fields) if total_fields > 0 else 1.0

    summary = (
        f"Incorporate concluído\n"
        f"  Origem:  {synr_path.name}\n"
        f"  Saída:   {output_path}\n"
        f"  ITEMs:   {total} total | {metrics['items_with_revision']} com revisão | "
        f"{revised} modificados\n"
        f"  Campos:  {changed} aceitos | {rejected} rejeitados | ACS {acs:.3f}\n"
        f"  Validação: {'com projeto' if ctx else 'sem projeto (sem validação)'}"
    )

    if format == "verbose":
        header = (
            f"# synesis-coder incorporate\n"
            f"# fonte: {synr_path.name}\n"
            f"# saída: {output_path.name}\n"
            f"# ITEMs: {total} | revisados: {revised} | ACS: {acs:.3f}\n"
        )
        return header + "\n" + summary

    return summary
