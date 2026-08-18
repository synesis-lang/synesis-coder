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
# Fonte única, compartilhada com prompt_builder. Inclui os nomes do formato 1
# (suspicion_score, reason_detail) para que .synr antigos sigam legíveis, e o
# cabeçalho do .synr — sem o qual um template com campo homônimo receberia o
# metadado da revisão como se fosse correção de campo.
from synesis_coder.revision_vocab import META_TAGS as _META_TAGS  # noqa: E402

# Valores que o LLM emite para pedir a REMOÇÃO de uma ocorrência de campo.
# O formato de correção não previa remoção; modelos improvisaram `none` e
# `(none)` no corpus face85. Sem tratamento, a string seria gravada como valor.
_REMOVAL_SENTINELS = frozenset({"none", "(none)", "-", "n/a", "null"})

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
    consumed: Optional[set[int]] = None,
) -> Optional[str]:
    """Substitui (ou remove) o valor de um campo dentro de um bloco ITEM.

    Faz match case-insensitive no nome do campo. Quando há múltiplas ocorrências
    do mesmo campo (ex: vários `chain:` num ITEM complexo), casa pela ocorrência
    cujo valor atual compartilha o nó-fonte da sugestão — a ocorrência que o LLM
    estava endereçando.

    Estratégia de match quando há múltiplas ocorrências:
    1. Casa a linha cujo nó-fonte é igual ao da sugestão, ignorando as linhas já
       consumidas por uma correção anterior.
    2. **Sem casamento → retorna None** (correção rejeitada).

    O passo 2 é deliberado. A versão anterior caía na primeira ocorrência, o que
    aplicava a correção a uma chain que ela não endereçava — destruindo o valor
    original em silêncio enquanto o alvo real sobrevivia intacto. Ver
    Estudo_Critique_Escopo_e_Taxonomia §5 (caso souza2022c).

    Com ocorrência ÚNICA o casamento não é exigido: não há ambiguidade possível.

    Args:
        item_block: Texto completo do bloco ITEM.
        field_name: Nome do campo a substituir (ex: "chain", "code").
        new_value: Novo valor sugerido pelo LLM. Um valor em _REMOVAL_SENTINELS
            remove a linha casada em vez de substituí-la.
        consumed: Índices de linha já alterados por correções anteriores neste
            mesmo ITEM. Mutado in-place com o índice consumido. None desabilita
            o rastreamento (uma correção isolada).

    Returns:
        Bloco modificado, ou None se o campo não foi encontrado ou se o
        casamento foi ambíguo.
    """
    field_lower = field_name.lower()
    lines = item_block.splitlines(keepends=True)
    consumed_set = consumed if consumed is not None else set()

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

    available = [
        (idx, val)
        for idx, val in zip(candidate_indices, candidate_values)
        if idx not in consumed_set
    ]
    if not available:
        _log.warning(
            "Campo '%s': todas as ocorrências já foram consumidas por correções "
            "anteriores — sugestão rejeitada (mais correções que ocorrências)",
            field_name,
        )
        return None

    target_idx: Optional[int] = None

    if len(available) == 1 and len(candidate_indices) == 1:
        # Ocorrência única no bloco: não há ambiguidade a resolver.
        target_idx = available[0][0]
    else:
        new_root = _source_node(new_value)
        matches = [
            idx for idx, old_val in available
            if new_root and _source_node(old_val) == new_root
        ]

        if len(matches) == 1:
            target_idx = matches[0]
        elif not matches:
            _log.warning(
                "Campo '%s' com %d ocorrências: nenhuma casa o nó-fonte de '%s' "
                "— sugestão rejeitada (alvo não identificável)",
                field_name,
                len(candidate_indices),
                new_value,
            )
            return None
        else:
            # Várias ocorrências compartilham o nó-fonte — o padrão normal de
            # APPLIES no corpus. O casamento por raiz NÃO as distingue, e
            # escolher a primeira destruiria uma chain que a correção não
            # endereçava (estudo §5, souza2022c). Rejeitar é a única ação segura.
            _log.warning(
                "Campo '%s': %d ocorrências compartilham o nó-fonte %r — a correção "
                "%r não identifica qual substituir; sugestão rejeitada. O formato de "
                "correção precisa nomear a ocorrência original.",
                field_name,
                len(matches),
                new_root,
                new_value,
            )
            return None

    result = list(lines)
    m = _FIELD_LINE_RE.match(lines[target_idx].rstrip("\r\n"))
    if not m:
        return None

    if _is_removal(new_value):
        del result[target_idx]
        # Índices consumidos após a remoção deslocam-se uma posição.
        if consumed is not None:
            shifted = {i if i < target_idx else i - 1 for i in consumed}
            consumed.clear()
            consumed.update(shifted)
        return "".join(result)

    indent, fname, sep, _ = m.groups()
    eol = "\n" if lines[target_idx].endswith("\n") else ""
    result[target_idx] = f"{indent}{fname}{sep}{new_value}{eol}"

    if consumed is not None:
        consumed.add(target_idx)

    return "".join(result)


def _field_occurs(item_block: str, field_name: str) -> bool:
    """True se o campo aparece ao menos uma vez no bloco ITEM."""
    field_lower = field_name.lower()
    for line in item_block.splitlines():
        m = _FIELD_LINE_RE.match(line.rstrip("\r\n"))
        if m and m.group(2).lower() == field_lower:
            return True
    return False


def _source_node(value: str) -> str:
    """Extrai o nó-fonte de uma chain (primeiro token antes de '->'), normalizado.

    Para campos não-CHAIN o valor inteiro é o 'nó-fonte' — o que faz o casamento
    exigir igualdade literal, corretamente conservador para múltiplas ocorrências.
    """
    return value.split("->")[0].strip().lower()


def _is_removal(new_value: str) -> bool:
    """True quando a sugestão pede remoção da ocorrência em vez de substituição."""
    return new_value.strip().lower() in _REMOVAL_SENTINELS


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

    # Índices de linha já alterados neste ITEM, por campo. Impede que duas
    # correções do mesmo campo colidam na mesma linha (sobrescrita silenciosa).
    consumed_by_field: dict[str, set[int]] = {}
    # Valores já aplicados por campo — correções byte-idênticas são rascunho do
    # modelo, não duas correções legítimas (ver estudo §5, caso souza2022c).
    seen_by_field: dict[str, set[str]] = {}

    for key, new_value in tags.items():
        # Normaliza chaves numeradas (ex: "chain.1" → campo "chain")
        base_key = key.split(".")[0] if re.match(r"^[\w]+\.\d+$", key) else key
        if base_key in _META_TAGS or key.startswith("metrics."):
            continue

        normalized = new_value.strip()
        seen = seen_by_field.setdefault(base_key, set())
        if normalized in seen:
            _log.warning(
                "Campo '%s': correção duplicada (byte-idêntica) descartada — %r",
                base_key,
                normalized,
            )
            rejected += 1
            continue

        # Campo ausente do ITEM: sugestão inaplicável, não conflito. Mantido
        # como "ignorada" (não conta como rejeição) — contrato pré-Fase 1.
        if not _field_occurs(modified, base_key):
            _log.debug(
                "Campo '%s' não encontrado no bloco ITEM — sugestão ignorada", base_key
            )
            continue

        consumed = consumed_by_field.setdefault(base_key, set())
        # Trabalha sobre uma cópia: o consumo só é confirmado se a correção
        # sobreviver à validação Synesis.
        trial = set(consumed)
        candidate = _replace_field_value(modified, base_key, new_value, trial)
        if candidate is None:
            # Campo existe mas a correção não pôde ser endereçada com segurança
            # (casamento ambíguo ou ocorrências esgotadas). _replace_field_value
            # já registrou o motivo. Isto É uma rejeição.
            rejected += 1
            continue

        if ctx is not None:
            ok = _validate_item_block(candidate, ctx)
        else:
            ok = True

        if ok:
            modified = candidate
            changed += 1
            seen.add(normalized)
            # Confirma o consumo apenas quando a correção é de fato aplicada.
            consumed.clear()
            consumed.update(trial)
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
    # "review" é o nome do formato 2; "critique" o do formato 1.
    if prior_phase in ("review", "critique", "normalize"):
        lines.append(f"# --- Fase anterior: {prior_phase} ---")
        _copy_header_metric(lines, synr_header, "model", f"modelo LLM usado na fase {prior_phase}")
        _copy_header_metric(lines, synr_header, "timestamp", "data/hora de execucao da fase")

        if prior_phase in ("review", "critique"):
            # Cada par (formato 2, formato 1): o que existir é copiado.
            _copy_header_metric(lines, synr_header, "sensitivity",
                "sensibilidade da revisao (lenient/standard/strict)")
            _copy_header_metric(lines, synr_header, "threshold",
                "limiar de divergencia acima do qual o ITEM recebeu # REVISION")
            _copy_header_metric(lines, synr_header, "metrics.items_total",
                "total de blocos ITEM avaliados pelo revisor")
            _copy_header_metric(lines, synr_header, "metrics.items_to_review",
                "ITEMs com divergencia acima da sensibilidade")
            _copy_header_metric(lines, synr_header, "metrics.items_flagged",
                "ITEMs com suspicion_score >= threshold (formato 1)")
            _copy_header_metric(lines, synr_header, "metrics.agreement",
                "proporcao de ITEMs sem divergencia; abaixo de 0.70 sugere revisor descalibrado")
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
