"""Modo critique: geração de revisão crítica de anotações .syn (Fase 2 do pipeline ACT).

Lê um arquivo .syn, invoca um modelo LLM *diferente* do extrator para revisar
cada bloco ITEM, e produz um arquivo .synr com blocos # REVISION para os ITEMs
que tenham score de suspeição acima do limiar configurado.

Fluxo:
    1. parse_synr(syn_path) → doc (funciona para .syn e .synr)
    2. load_project(project_path) → ctx (carrega GUIDELINES do template)
    3. Para cada bloco ITEM (concorrente, rate-limited):
        a. _get_source_text(item_block, bibref, ctx) → source_text
        b. build_critique_prompt(ctx, item_block, source_text) → messages
        c. LLMClient.call_async(messages) → raw critique
        d. _parse_critique_response(raw) → tags dict
        e. Se suspicion_score >= threshold → incluir no revisions list
    4. create_synr(syn_content, header, revisions) → SynrDocument
    5. write_synr(output_path, doc)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional

from synesis_coder.llm_client import LLMClient, get_critique_connection
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_critique_prompt
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.synr_io import (
    _END_ITEM,
    _ITEM_START,
    _TAG_RE,
    create_synr,
    parse_synr,
    write_synr,
)

_log = logging.getLogger(__name__)

# Limiar padrão: items com suspicion_score >= THRESHOLD recebem bloco # REVISION
DEFAULT_SUSPICION_THRESHOLD = 0.20

# Regex para parse de resposta sem prefixo # $ (fallback)
_PLAIN_TAG_RE = re.compile(r"^([\w.]+)\s*:\s*(.+)$")

# Regex para extrair o campo text de um bloco ITEM
_TEXT_FIELD_RE = re.compile(
    r"^\s*text\s*:\s*(.+?)(?=\n\s*[a-zA-Z_]+\s*:|\n\s*END ITEM|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Regex simples para extrair abstract de BibTeX
_BIB_ABSTRACT_RE = re.compile(
    r"abstract\s*=\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Extração de texto-fonte
# ---------------------------------------------------------------------------


def _extract_item_text(item_block: str) -> str:
    """Extrai o valor do campo `text` de um bloco ITEM."""
    m = _TEXT_FIELD_RE.search(item_block)
    if m:
        return m.group(1).strip()
    return ""


def _extract_abstract_from_bib(bibref: str, bib_content: str) -> Optional[str]:
    """Extrai o abstract de uma entrada BibTeX dado o bibref.

    Procura a entrada `@type{bibref, ...}` no conteúdo .bib e extrai o campo
    `abstract = {...}`. Suporta chaves balanceadas de um nível.

    Args:
        bibref: Chave BibTeX (ex: "smith2024").
        bib_content: Conteúdo completo do arquivo .bib.

    Returns:
        Texto do abstract, ou None se não encontrado.
    """
    # Localizar a entrada com a chave bibref
    entry_pattern = re.compile(
        rf"@\w+\s*\{{\s*{re.escape(bibref)}\s*,",
        re.IGNORECASE,
    )
    m = entry_pattern.search(bib_content)
    if not m:
        return None

    # Extrair o bloco da entrada (até a próxima @ de nível raiz)
    start = m.start()
    next_entry = re.search(r"\n@\w+\s*\{", bib_content[m.end():])
    end = m.end() + next_entry.start() if next_entry else len(bib_content)
    entry_text = bib_content[start:end]

    # Extrair campo abstract
    abstract_m = _BIB_ABSTRACT_RE.search(entry_text)
    if abstract_m:
        return abstract_m.group(1).strip()
    return None


def _get_source_text(item_block: str, bibref: str, ctx: dict) -> str:
    """Obtém o texto-fonte para critique: abstract do .bib ou campo text do ITEM.

    Prioridade:
    1. Abstract completo do arquivo .bib (melhor contexto para o crítico)
    2. Campo text do ITEM (fallback sempre disponível)
    """
    bib_content = ctx.get("bib_content") or ""
    if bib_content:
        abstract = _extract_abstract_from_bib(bibref, bib_content)
        if abstract:
            return abstract

    text_field = _extract_item_text(item_block)
    if text_field:
        return text_field

    return "(source text not available)"


# ---------------------------------------------------------------------------
# Parse da resposta do LLM
# ---------------------------------------------------------------------------


# Fragmentos que indicam linhas de template copiadas pelo modelo no raciocínio
_TEMPLATE_ARTIFACT_MARKERS = (
    "[complete corrected value]",
    "← ONLY when",
    "← when",
    "[0.00-1.00]",
    "[none|anchor_missing",
    "[optional free-text",
)

# Campos cujas duplicatas são rascunhos internos — só a última versão importa
_SCALAR_FIELDS = frozenset({"suspicion_score", "reason", "reason_detail"})


def _parse_critique_response(raw: str) -> dict[str, str]:
    """Faz parse da resposta do LLM de critique em um dict de tags.

    Aceita dois formatos:
    - `# $key: value` (formato preferido — com prefixo)
    - `key: value` (fallback para modelos que não seguem o formato exato)

    Para campos escalares (suspicion_score, reason, reason_detail), múltiplas
    ocorrências são tratadas como rascunhos de raciocínio interno: apenas a
    ÚLTIMA versão é mantida (modelos de raciocínio como kimi-k2.6 revisam o
    score durante o thinking antes de finalizar). Para campos de conteúdo
    (chain, note), ocorrências adicionais recebem sufixo numérico (.1, .2…).

    Linhas que contêm marcadores de template do prompt (ex: "[complete corrected
    value]") são silenciosamente ignoradas — são artefatos do raciocínio interno
    de modelos thinking que repetem os exemplos do system prompt.

    Args:
        raw: Texto bruto retornado pelo LLM.

    Returns:
        Dict {key: value} com todos os tags encontrados.
        Sempre inclui 'suspicion_score' e 'reason' (com defaults se ausentes).
    """
    tags: dict[str, str] = {}
    key_counts: dict[str, int] = {}

    def _add_tag(key: str, value: str) -> None:
        if key in _SCALAR_FIELDS:
            # Scalar: última versão vence (rascunhos são sobrescritos)
            tags[key] = value
        else:
            count = key_counts.get(key, 0)
            if count == 0:
                tags[key] = value
            else:
                tags[f"{key}.{count}"] = value
            key_counts[key] = count + 1

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("OUTPUT") or stripped.startswith("SCORING"):
            continue

        # Ignorar linhas com artefatos de template do raciocínio interno
        if any(marker in stripped for marker in _TEMPLATE_ARTIFACT_MARKERS):
            continue

        # Formato primário: # $key: value
        m = _TAG_RE.match(stripped)
        if m:
            key = m.group(1)
            # Ignorar chaves que começam com maiúscula — comentários do raciocínio
            # (ex: "# $Score: 0.65? Or higher?", "# $So: suspicion_score: 0.75")
            if key and key[0].isupper():
                continue
            _add_tag(key, m.group(2).strip())
            continue

        # Fallback: key: value (sem prefixo # $)
        m2 = _PLAIN_TAG_RE.match(stripped)
        if m2:
            key = m2.group(1).strip()
            # Evitar capturar linhas como "ITEM @ref" ou "END ITEM"
            if key.upper() not in ("ITEM", "END", "SOURCE", "ONTOLOGY"):
                _add_tag(key, m2.group(2).strip())

    # Garantir campos mínimos com defaults
    if "suspicion_score" not in tags:
        tags["suspicion_score"] = "0.0"
    if "reason" not in tags:
        tags["reason"] = "none"

    return tags


# ---------------------------------------------------------------------------
# Revisão assíncrona de um único ITEM
# ---------------------------------------------------------------------------


def _score_of(tags: dict[str, str]) -> float:
    """Extrai o suspicion_score (float) de um dict de tags de critique.

    Retorna 0.0 quando ausente ou não-numérico — mesma tolerância aplicada
    historicamente em _critique_single_item.
    """
    try:
        return float(tags.get("suspicion_score", "0.0"))
    except ValueError:
        return 0.0


async def _critique_tags(
    item_block: str,
    bibref: str,
    ctx: dict,
    llm_client: LLMClient,
    source_text: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Avalia um bloco ITEM e retorna as tags de critique (sem filtro de threshold).

    Faz UMA chamada ao LLM de critique e devolve o dict de tags parseado — sempre,
    independentemente do score. É o núcleo reutilizável do critique: o modo
    critique aplica o filtro de threshold por cima (via _critique_single_item);
    o modo refine consome as tags e o score diretamente no loop.

    Args:
        item_block: Texto do bloco ITEM a avaliar.
        bibref: Referência bibliográfica do ITEM (para logging).
        ctx: Contexto do projeto.
        llm_client: Cliente LLM de critique.
        source_text: Texto-fonte já resolvido. Se None, é obtido via
            _get_source_text (permite ao chamador reaproveitar o source entre
            múltiplas chamadas de um mesmo ITEM, como no loop de refine).

    Returns:
        Dict de tags de critique, ou None se a chamada LLM falhar.
    """
    if source_text is None:
        source_text = _get_source_text(item_block, bibref, ctx)
    messages = build_critique_prompt(ctx, item_block, source_text)

    try:
        raw = await llm_client.call_async(messages, temperature=0.0, thinking=False)
    except Exception as exc:
        _log.error("Falha na chamada LLM para ITEM @%s: %s", bibref, exc)
        return None

    _log.debug("ITEM @%s raw response:\n%s", bibref, raw)

    tags = _parse_critique_response(raw)

    _log.info(
        "ITEM @%s → score=%.2f reason=%s%s",
        bibref, _score_of(tags), tags.get("reason", "?"),
        f" detail={tags['reason_detail']!r}" if tags.get("reason_detail") else "",
    )

    return tags


async def _critique_single_item(
    item_block: str,
    bibref: str,
    ctx: dict,
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
    suspicion_threshold: float,
) -> Optional[dict[str, str]]:
    """Revisa um bloco ITEM e retorna tags de revisão ou None.

    Args:
        item_block: Texto do bloco ITEM.
        bibref: Referência bibliográfica do ITEM.
        ctx: Contexto do projeto.
        llm_client: Cliente LLM compartilhado.
        semaphore: Semáforo de concorrência.
        suspicion_threshold: Score mínimo para incluir bloco # REVISION.

    Returns:
        Dict de tags se suspicion_score >= threshold; None caso contrário.
    """
    async with semaphore:
        tags = await _critique_tags(item_block, bibref, ctx, llm_client)
        if tags is None:
            return None

        if _score_of(tags) >= suspicion_threshold:
            return tags
        return None


# ---------------------------------------------------------------------------
# Extração de blocos ITEM do conteúdo
# ---------------------------------------------------------------------------


def _extract_item_blocks_with_bibrefs(
    content: str,
) -> list[tuple[str, str]]:
    """Extrai todos os blocos ITEM do conteúdo como lista de (bibref, item_block).

    Preserva o texto exato de cada bloco (incluindo indentação e newlines).
    """
    lines = content.splitlines(keepends=True)
    items: list[tuple[str, str]] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        m = _ITEM_START.match(line.rstrip("\r\n"))
        if m:
            bibref = m.group(1)
            item_lines = [line]
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i].rstrip("\r\n")):
                item_lines.append(lines[i])
                i += 1
            if i < len(lines):
                item_lines.append(lines[i])  # END ITEM
            items.append((bibref, "".join(item_lines)))
        i += 1

    return items


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_critique(
    syn_path: Path,
    project_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    concurrent: int = 3,
    model: Optional[str] = None,
    suspicion_threshold: float = DEFAULT_SUSPICION_THRESHOLD,
    format: str = "plain",
    debug: bool = False,
) -> str:
    """Revisa anotações .syn e emite .synr com blocos # REVISION.

    Para cada bloco ITEM, invoca o modelo LLM configurado para critique
    (SYNESIS_CODER_CRITIQUE_MODEL ou fallback SYNESIS_CODER_MODEL) para
    avaliar se os campos representam fielmente o texto-fonte. Items com
    suspicion_score >= suspicion_threshold recebem um bloco # REVISION.

    Args:
        syn_path: Caminho para o arquivo .syn (ou .synr) a revisar.
        project_path: Caminho para o .synp do projeto. Se None, auto-detecta
            buscando .synp no diretório do .syn e diretórios pai.
        output_path: Caminho de saída do .synr. Se None, usa o mesmo nome
            do .syn com extensão .synr.
        concurrent: Número máximo de chamadas LLM simultâneas.
        model: ID do modelo LLM (sobrescreve SYNESIS_CODER_CRITIQUE_MODEL).
        suspicion_threshold: Score mínimo para gerar bloco # REVISION [0.0-1.0].
        format: "plain" (resumo compacto) ou "verbose" (com header).

    Returns:
        String com resumo da execução.

    Raises:
        FileNotFoundError: Se syn_path ou project_path não existir.
        ValueError: Se o projeto não puder ser carregado.
    """
    return asyncio.run(
        _process_critique_async(
            syn_path=syn_path,
            project_path=project_path,
            output_path=output_path,
            concurrent=concurrent,
            model=model,
            suspicion_threshold=suspicion_threshold,
            format=format,
            debug=debug,
        )
    )


async def _process_critique_async(
    syn_path: Path,
    project_path: Optional[Path],
    output_path: Optional[Path],
    concurrent: int,
    model: Optional[str],
    suspicion_threshold: float,
    format: str,
    debug: bool = False,
) -> str:
    """Implementação assíncrona do modo critique."""
    # Logging é configurado centralmente pela CLI (_configure_logging); não
    # reconfigurar aqui para preservar -v/-q e o silenciamento de terceiros.
    # --debug ainda eleva o nível para DEBUG (sem reinstalar handlers).
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    start_time = time.monotonic()

    syn_path = Path(syn_path).resolve()
    if not syn_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {syn_path}")

    # 1. Ler o arquivo .syn (ou .synr)
    doc = parse_synr(syn_path)
    syn_content = doc.content

    # 2. Carregar contexto do projeto
    resolved_project = _resolve_project(syn_path, project_path)
    if resolved_project is None:
        raise FileNotFoundError(
            f"Projeto .synp não encontrado próximo a {syn_path}. "
            "Use --project para especificá-lo."
        )
    ctx = load_project(resolved_project, load_annotations=True)

    # 3. Extrair blocos ITEM em ordem
    items_with_bibrefs = _extract_item_blocks_with_bibrefs(syn_content)
    total_items = len(items_with_bibrefs)
    _log.info(
        "Critique de %s: %d ITEM(s) encontrado(s), limiar=%.2f",
        syn_path.name, total_items, suspicion_threshold,
    )

    # 4. Inicializar LLM client com modelo e conexão de critique.
    #    A conexão de crítica (2ª API opcional) permite avaliar num provedor
    #    distinto do gerador; sem vars CRITIQUE_* de conexão, herda a global.
    llm_client = LLMClient(model=model, **get_critique_connection())
    runtime_banner(llm_client, format=format)

    # 5. Processar ITEMs de forma concorrente
    semaphore = asyncio.Semaphore(concurrent)

    tasks = [
        _critique_single_item(
            item_block=item_block,
            bibref=bibref,
            ctx=ctx,
            llm_client=llm_client,
            semaphore=semaphore,
            suspicion_threshold=suspicion_threshold,
        )
        for bibref, item_block in items_with_bibrefs
    ]

    # gather preserva ordem: revision_results[i] corresponde a items_with_bibrefs[i]
    revision_results: list[Optional[dict]] = await asyncio.gather(*tasks)

    # 6. Contabilizar
    items_flagged = sum(1 for r in revision_results if r is not None)
    _log.info(
        "Critique concluído: %d/%d ITEMs com score >= %.2f",
        items_flagged, total_items, suspicion_threshold,
    )

    # 7. Construir .synr via create_synr
    import datetime
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    suspicion_rate = items_flagged / total_items if total_items > 0 else 0.0
    header = {
        "phase": "critique",
        "model": llm_client.model,
        "timestamp": timestamp,
        "threshold": str(suspicion_threshold),
        "metrics.items_total": str(total_items),
        "metrics.items_flagged": str(items_flagged),
        "metrics.suspicion_rate": f"{suspicion_rate:.3f}",
        "metrics.suspicion_rate.formula": "items_flagged / items_total",
        "metrics.suspicion_rate.description": (
            "proporcao de ITEMs com score >= threshold; "
            "< 0.30 indica anotacoes de boa qualidade"
        ),
    }

    synr_doc = create_synr(
        syn_content=syn_content,
        header=header,
        item_revisions=revision_results,
    )

    # 8. Determinar caminho de saída e escrever
    if output_path is None:
        stem = syn_path.stem
        output_path = syn_path.with_name(stem + ".synr")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_synr(output_path, synr_doc)

    elapsed = time.monotonic() - start_time

    summary = (
        f"Critique concluído em {elapsed:.1f}s\n"
        f"  Origem:    {syn_path.name}\n"
        f"  Saída:     {output_path}\n"
        f"  ITEMs:     {total_items} total | {items_flagged} com revisão\n"
        f"  Modelo:    {llm_client.model}\n"
        f"  Limiar:    {suspicion_threshold}\n"
        f"  {llm_client.usage.summary_line()}"
    )

    if format == "verbose":
        header_str = (
            f"# synesis-coder critique\n"
            f"# origem: {syn_path.name}\n"
            f"# saída: {output_path.name}\n"
            f"# ITEMs: {total_items} | revisados: {items_flagged}\n"
            f"# modelo: {llm_client.model} | limiar: {suspicion_threshold}\n"
            f"# {llm_client.usage.summary_line()}\n"
        )
        return header_str + "\n" + summary

    return summary


def _resolve_project(syn_path: Path, project_path: Optional[Path]) -> Optional[Path]:
    """Resolve o caminho do projeto .synp a partir do .syn ou do argumento."""
    if project_path is not None:
        return project_path

    search_dir = syn_path.parent
    for _ in range(5):
        synp_files = list(search_dir.glob("*.synp"))
        if synp_files:
            return synp_files[0]
        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent

    return None
