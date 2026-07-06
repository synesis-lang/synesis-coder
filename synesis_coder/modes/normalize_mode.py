"""Modo normalize: canonicalização de códigos cross-corpus (Fase 3 do pipeline ACT).

Lê um conjunto de arquivos .synr (ou .syn), constrói um inventário global de
códigos extraídos de campos `chain` e `code`, aplica regras determinísticas de
normalização (case, separadores) e — para grupos residuais — invoca um LLM
para sugerir formas canônicas. Emite .synr atualizados com blocos # REVISION
propondo substituições via # $chain: e # $code:.

Fluxo:
    1. Para cada arquivo da lista:
        a. parse_synr(path) → SynrDocument
        b. _extract_codes_from_doc(doc) → inventário parcial
    2. Mesclar em inventário global: {normalized_key: CodeGroup}
    3. _apply_deterministic_normalization(inventory) → canonicals definidos
       por regra (grupo com única forma após normalização → resolvido)
    4. Para grupos residuais (>1 variante sobrevivente):
        a. build_normalization_prompt(ctx, code_group_batch) → messages
        b. LLMClient.call_async(messages) → sugestões LLM
        c. _parse_normalization_response(raw) → list[NormalizationSuggestion]
    5. Para cada arquivo, _apply_normalizations_to_doc(doc, canonicals) → .synr
    6. write_synr(output_path, updated_doc)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_normalization_prompt
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.synr_io import (
    _END_ITEM,
    _ITEM_START,
    SynrDocument,
    create_synr,
    parse_synr,
    write_synr,
)

_log = logging.getLogger(__name__)

# Separadores reconhecidos em cadeias CHAIN
_CHAIN_SEP_RE = re.compile(r"\s*->\s*")

# Campo chain ou code em bloco ITEM
_FIELD_RE = re.compile(r"^\s*(chain|code)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Relações em CHAIN: palavras completamente em maiúsculas (ex: ENABLES, INFLUENCES)
_RELATION_RE = re.compile(r"^[A-Z][A-Z_\-]+[A-Z]$")

# Chunk máximo de grupos a enviar ao LLM por chamada (grupos com ≥2 variantes)
_LLM_CHUNK_SIZE = 30

# Confiança mínima do LLM para aceitar uma sugestão de canonicalização
DEFAULT_MERGE_CONFIDENCE = 0.65


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------


@dataclass
class CodeGroup:
    """Grupo de variantes de um mesmo código (normalização determinística).

    Attributes:
        normalized_key: Chave de agrupamento (lowercase, separadores unificados).
        variants: Counter-like dict {raw_form: count} das formas encontradas.
        occurrences: Lista de (file_path, bibref, field_name, full_field_value)
            para cada ocorrência de qualquer variante do grupo.
        canonical: Forma canônica escolhida (None até ser determinada).
        llm_suggested: True se o canonical veio de sugestão LLM.
    """

    normalized_key: str
    variants: dict[str, int] = field(default_factory=dict)
    occurrences: list[tuple[Path, str, str, str]] = field(default_factory=list)
    canonical: Optional[str] = None
    llm_suggested: bool = False

    def add_occurrence(
        self, raw_form: str, file_path: Path, bibref: str, field_name: str, field_value: str
    ) -> None:
        self.variants[raw_form] = self.variants.get(raw_form, 0) + 1
        self.occurrences.append((file_path, bibref, field_name, field_value))

    @property
    def is_resolved(self) -> bool:
        return self.canonical is not None

    @property
    def needs_normalization(self) -> bool:
        """True se alguma variante difere do canonical."""
        if self.canonical is None:
            return False
        return any(v != self.canonical for v in self.variants)


# ---------------------------------------------------------------------------
# Extração de códigos
# ---------------------------------------------------------------------------


def _normalize_code_key(raw: str) -> str:
    """Converte um código para chave de agrupamento normalizada.

    Rules: lowercase, substituir espaços e hífens por underscore, strip.
    """
    key = raw.strip().lower()
    key = re.sub(r"[\s\-]+", "_", key)
    return key


def _extract_concepts_from_chain(chain_value: str) -> list[str]:
    """Extrai nós-conceito de uma expressão de chain.

    Em `A -> RELATION -> B -> RELATION2 -> C`, extrai [A, B, C].
    Ignora tokens que parecem relações (all-caps com underscores).
    """
    tokens = [t.strip() for t in _CHAIN_SEP_RE.split(chain_value)]
    concepts = []
    for i, token in enumerate(tokens):
        if not token:
            continue
        # Posição par = conceito; posição ímpar = relação
        if i % 2 == 0:
            concepts.append(token)
        # Mesmo nas posições ímpares, se não parece relação, inclui
        elif not _RELATION_RE.match(token):
            concepts.append(token)
    return concepts


def _extract_codes_from_item_block(item_block: str) -> list[tuple[str, str, str]]:
    """Extrai todos os códigos de um bloco ITEM.

    Returns:
        Lista de (field_name, raw_code, full_field_value) para cada código encontrado.
    """
    results: list[tuple[str, str, str]] = []
    for m in _FIELD_RE.finditer(item_block):
        field_name = m.group(1).strip().lower()
        field_value = m.group(2).strip()

        if field_name == "chain":
            concepts = _extract_concepts_from_chain(field_value)
            for concept in concepts:
                results.append((field_name, concept, field_value))
        elif field_name == "code":
            results.append((field_name, field_value, field_value))

    return results


def _extract_codes_from_doc(
    doc: SynrDocument, file_path: Path
) -> list[tuple[str, str, str, str, str]]:
    """Extrai todos os códigos do documento para o inventário global.

    Returns:
        Lista de (bibref, field_name, raw_code, full_field_value, normalized_key).
    """
    content = doc.content
    lines = content.splitlines(keepends=True)
    results: list[tuple[str, str, str, str, str]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ITEM_START.match(line.rstrip("\r\n"))
        if m:
            bibref = m.group(1)
            item_lines = [line]
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i].rstrip("\r\n")):
                # Skip revision comment lines to avoid double-extracting
                if not lines[i].strip().startswith("#"):
                    item_lines.append(lines[i])
                i += 1
            if i < len(lines):
                item_lines.append(lines[i])

            item_block = "".join(item_lines)
            for field_name, raw_code, full_value in _extract_codes_from_item_block(item_block):
                nkey = _normalize_code_key(raw_code)
                results.append((bibref, field_name, raw_code, full_value, nkey))
        i += 1

    return results


# ---------------------------------------------------------------------------
# Inventário e normalização determinística
# ---------------------------------------------------------------------------


def build_code_inventory(
    docs_with_paths: list[tuple[Path, SynrDocument]]
) -> dict[str, CodeGroup]:
    """Constrói inventário global de códigos a partir de múltiplos documentos.

    Returns:
        Dict {normalized_key: CodeGroup} com todas as variantes encontradas.
    """
    inventory: dict[str, CodeGroup] = {}

    for file_path, doc in docs_with_paths:
        codes = _extract_codes_from_doc(doc, file_path)
        for bibref, field_name, raw_code, full_value, nkey in codes:
            if nkey not in inventory:
                inventory[nkey] = CodeGroup(normalized_key=nkey)
            inventory[nkey].add_occurrence(raw_code, file_path, bibref, field_name, full_value)

    return inventory


def apply_deterministic_normalization(inventory: dict[str, CodeGroup]) -> None:
    """Aplica normalização determinística ao inventário (in-place).

    Para cada grupo:
    - Se há apenas uma variante → essa é o canonical (sem mudança de escrita).
    - Se há múltiplas variantes → canonical = variante mais frequente.
      Em empate, escolhe a que tem underscores (padrão do projeto) ou a
      primeira em ordem alfabética.
    """
    for group in inventory.values():
        if not group.variants:
            continue

        if len(group.variants) == 1:
            group.canonical = next(iter(group.variants))
        else:
            # Ordenar: primeiro por frequência (desc), depois por preferência de formato
            def _sort_key(item: tuple[str, int]) -> tuple[int, int, str]:
                form, count = item
                # Prefere forma com underscore (convenção do projeto)
                has_underscore = int("_" in form)
                return (-count, -has_underscore, form)

            sorted_variants = sorted(group.variants.items(), key=_sort_key)
            group.canonical = sorted_variants[0][0]


# ---------------------------------------------------------------------------
# Parse da resposta LLM
# ---------------------------------------------------------------------------


@dataclass
class NormalizationSuggestion:
    """Sugestão de canonicalização emitida pelo LLM."""

    group_codes: list[str]
    suggested_canonical: str
    merge_confidence: float
    reason: str


def _parse_normalization_response(raw: str) -> list[NormalizationSuggestion]:
    """Faz parse da resposta do LLM de normalização em lista de sugestões.

    Formato esperado:
        # $group: Trust, social_trust, Community_Trust
        # $suggested_canonical: Trust
        # $merge_confidence: 0.85
        # $reason: all_variants_refer_to_same_concept
        ---

    Cada bloco separado por `---` é uma sugestão.
    """
    suggestions: list[NormalizationSuggestion] = []

    blocks = re.split(r"\n\s*---\s*\n|\n\s*---\s*$", raw.strip())
    for block in blocks:
        tags: dict[str, str] = {}
        for line in block.splitlines():
            stripped = line.strip()
            m = re.match(r"^#\s*\$([\w.]+):\s*(.+)$", stripped)
            if m:
                tags[m.group(1)] = m.group(2).strip()
            # Fallback: plain key: value
            elif re.match(r"^[\w_]+\s*:", stripped):
                m2 = re.match(r"^([\w_]+)\s*:\s*(.+)$", stripped)
                if m2 and m2.group(1) not in tags:
                    tags[m2.group(1)] = m2.group(2).strip()

        if "suggested_canonical" not in tags:
            continue

        group_raw = tags.get("group", "")
        group_codes = [c.strip() for c in group_raw.split(",") if c.strip()]

        try:
            confidence = float(tags.get("merge_confidence", "0.0"))
        except ValueError:
            confidence = 0.0

        suggestions.append(
            NormalizationSuggestion(
                group_codes=group_codes,
                suggested_canonical=tags["suggested_canonical"],
                merge_confidence=confidence,
                reason=tags.get("reason", "none"),
            )
        )

    return suggestions


# ---------------------------------------------------------------------------
# Aplicar canonicals LLM ao inventário
# ---------------------------------------------------------------------------


def _apply_llm_suggestions(
    inventory: dict[str, CodeGroup],
    suggestions: list[NormalizationSuggestion],
    confidence_threshold: float = DEFAULT_MERGE_CONFIDENCE,
) -> int:
    """Aplica sugestões LLM ao inventário (in-place).

    Apenas sugestões com merge_confidence >= threshold são aceitas.
    Mescla grupos distintos do inventário sob o canonical sugerido.

    Returns:
        Número de canonicals atualizados por LLM.
    """
    updated = 0

    for suggestion in suggestions:
        if suggestion.merge_confidence < confidence_threshold:
            _log.debug(
                "Sugestão LLM ignorada (confiança %.2f < %.2f): %s → %s",
                suggestion.merge_confidence, confidence_threshold,
                suggestion.group_codes, suggestion.suggested_canonical,
            )
            continue

        for code in suggestion.group_codes:
            nkey = _normalize_code_key(code)
            if nkey in inventory:
                old_canonical = inventory[nkey].canonical
                if old_canonical != suggestion.suggested_canonical:
                    inventory[nkey].canonical = suggestion.suggested_canonical
                    inventory[nkey].llm_suggested = True
                    updated += 1
                    _log.debug(
                        "LLM: %r → %r (era: %r, confiança: %.2f)",
                        code, suggestion.suggested_canonical, old_canonical,
                        suggestion.merge_confidence,
                    )

    return updated


# ---------------------------------------------------------------------------
# Geração de revisões por arquivo
# ---------------------------------------------------------------------------


def _substitute_code_in_chain(chain_value: str, old_code: str, new_code: str) -> str:
    """Substitui um código específico numa expressão de chain preservando relações.

    Faz substituição token-a-token, apenas nos nós-conceito (posições pares).
    """
    tokens = _CHAIN_SEP_RE.split(chain_value)
    result = []
    for i, token in enumerate(tokens):
        stripped = token.strip()
        if i % 2 == 0 and stripped == old_code:
            result.append(new_code)
        else:
            result.append(stripped)
    return " -> ".join(result)


def _build_revisions_for_doc(
    doc: SynrDocument, file_path: Path, inventory: dict[str, CodeGroup]
) -> list[Optional[dict[str, str]]]:
    """Gera lista de revisões (ou None) por bloco ITEM do documento.

    Para cada ITEM, verifica se algum código precisa ser normalizado.
    Se sim, retorna um dict {field_name: new_value} para os campos afetados.

    Returns:
        Lista de dicts (ou None) em ordem de aparecimento dos ITEMs.
    """
    content = doc.content
    lines = content.splitlines(keepends=True)
    revisions: list[Optional[dict[str, str]]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ITEM_START.match(line.rstrip("\r\n"))
        if m:
            item_lines = [line]
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i].rstrip("\r\n")):
                if not lines[i].strip().startswith("#"):
                    item_lines.append(lines[i])
                i += 1
            if i < len(lines):
                item_lines.append(lines[i])

            item_block = "".join(item_lines)
            item_tags: dict[str, str] = {}

            for fm in _FIELD_RE.finditer(item_block):
                field_name = fm.group(1).strip().lower()
                field_value = fm.group(2).strip()

                if field_name == "chain":
                    new_value = field_value
                    concepts = _extract_concepts_from_chain(field_value)
                    changed = False
                    for concept in concepts:
                        nkey = _normalize_code_key(concept)
                        group = inventory.get(nkey)
                        if group and group.canonical and group.canonical != concept:
                            new_value = _substitute_code_in_chain(new_value, concept, group.canonical)
                            changed = True
                    if changed:
                        item_tags["chain"] = new_value

                elif field_name == "code":
                    nkey = _normalize_code_key(field_value)
                    group = inventory.get(nkey)
                    if group and group.canonical and group.canonical != field_value:
                        item_tags["code"] = group.canonical

            if item_tags:
                item_tags["phase"] = "normalize"
                revisions.append(item_tags)
            else:
                revisions.append(None)
        i += 1

    return revisions


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_normalize(
    synr_paths: list[Path],
    project_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    concurrent: int = 3,
    model: Optional[str] = None,
    confidence_threshold: float = DEFAULT_MERGE_CONFIDENCE,
    inventory_path: Optional[Path] = None,
    format: str = "plain",
) -> str:
    """Normaliza códigos em um corpus de arquivos .synr e emite .synr atualizados.

    Fase 3 do pipeline ACT. Construção de inventário cross-file, normalização
    determinística e LLM para grupos residuais.

    Args:
        synr_paths: Lista de caminhos para arquivos .synr (ou .syn).
        project_path: Caminho para o .synp do projeto. Se None, auto-detecta.
        output_dir: Diretório de saída para .synr atualizados. Se None, usa o
            mesmo diretório de cada arquivo de entrada.
        concurrent: Número máximo de chamadas LLM simultâneas.
        model: ID do modelo LLM para normalização.
        confidence_threshold: Confiança mínima para aceitar sugestão LLM.
        inventory_path: Caminho para salvar inventário TXT. Se None, não salva.
        format: "plain" ou "verbose".

    Returns:
        String com resumo da execução.
    """
    return asyncio.run(
        _process_normalize_async(
            synr_paths=synr_paths,
            project_path=project_path,
            output_dir=output_dir,
            concurrent=concurrent,
            model=model,
            confidence_threshold=confidence_threshold,
            inventory_path=inventory_path,
            format=format,
        )
    )


async def _process_normalize_async(
    synr_paths: list[Path],
    project_path: Optional[Path],
    output_dir: Optional[Path],
    concurrent: int,
    model: Optional[str],
    confidence_threshold: float,
    inventory_path: Optional[Path],
    format: str,
) -> str:
    """Implementação assíncrona do modo normalize."""
    # Logging é configurado centralmente pela CLI (_configure_logging); não
    # reconfigurar aqui para preservar os níveis de -v/-q e o silenciamento de
    # loggers de terceiros.

    start_time = time.monotonic()

    # 1. Resolver projeto
    first_path = Path(synr_paths[0]).resolve() if synr_paths else None
    resolved_project = _resolve_project(first_path, project_path)
    if resolved_project is None:
        raise FileNotFoundError(
            "Projeto .synp não encontrado. Use --project para especificá-lo."
        )
    ctx = load_project(resolved_project)

    # 2. Ler todos os documentos
    docs_with_paths: list[tuple[Path, SynrDocument]] = []
    for p in synr_paths:
        p = Path(p).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {p}")
        docs_with_paths.append((p, parse_synr(p)))

    _log.info("Normalize: %d arquivo(s) lido(s)", len(docs_with_paths))

    # 3. Construir inventário global
    inventory = build_code_inventory(docs_with_paths)
    total_codes = len(inventory)
    _log.info("Inventário: %d código(s) único(s) encontrado(s)", total_codes)

    # 4. Normalização determinística
    apply_deterministic_normalization(inventory)
    resolved_deterministic = sum(1 for g in inventory.values() if not g.needs_normalization)
    residual_groups = [
        g for g in inventory.values()
        if len(g.variants) > 1
    ]
    _log.info(
        "Determinístico: %d resolvido(s), %d grupo(s) residual(is)",
        resolved_deterministic, len(residual_groups),
    )

    # 5. Salvar inventário em TXT se solicitado
    if inventory_path:
        _write_inventory_txt(inventory, Path(inventory_path))

    # 6. LLM para grupos residuais (chunked, concurrent)
    llm_client = LLMClient(model=model)
    runtime_banner(llm_client, format=format)
    llm_updates = 0

    if residual_groups:
        chunks = [
            residual_groups[i: i + _LLM_CHUNK_SIZE]
            for i in range(0, len(residual_groups), _LLM_CHUNK_SIZE)
        ]
        semaphore = asyncio.Semaphore(concurrent)

        async def _process_chunk(chunk: list[CodeGroup]) -> list[NormalizationSuggestion]:
            async with semaphore:
                messages = build_normalization_prompt(ctx, chunk)
                try:
                    raw = await llm_client.call_async(messages, temperature=0.0, thinking=False)
                except Exception as exc:
                    _log.error("Falha LLM em chunk de normalização: %s", exc)
                    return []
                return _parse_normalization_response(raw)

        chunk_results = await asyncio.gather(*[_process_chunk(chunk) for chunk in chunks])
        all_suggestions = [s for chunk in chunk_results for s in chunk]

        llm_updates = _apply_llm_suggestions(inventory, all_suggestions, confidence_threshold)
        _log.info("LLM: %d sugestão(ões) aplicada(s)", llm_updates)

    # 7. Gerar revisões por arquivo e escrever .synr
    import datetime
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Pré-computar revisões para obter total de ITEMs do corpus
    all_file_revisions: list[tuple[Path, SynrDocument, list]] = []
    total_items_corpus = 0
    items_revised = 0
    for file_path, doc in docs_with_paths:
        revisions = _build_revisions_for_doc(doc, file_path, inventory)
        total_items_corpus += len(revisions)
        n_revised = sum(1 for r in revisions if r is not None)
        items_revised += n_revised
        all_file_revisions.append((file_path, doc, revisions))

    normalization_rate = items_revised / total_items_corpus if total_items_corpus > 0 else 0.0

    files_updated = 0
    for file_path, doc, revisions in all_file_revisions:
        n_revised = sum(1 for r in revisions if r is not None)

        header = {
            "phase": "normalize",
            "model": llm_client.model,
            "timestamp": timestamp,
            "confidence_threshold": str(confidence_threshold),
            "metrics.corpus_files": str(len(docs_with_paths)),
            "metrics.corpus_items_total": str(total_items_corpus),
            "metrics.codes_unique": str(total_codes),
            "metrics.residual_groups": str(len(residual_groups)),
            "metrics.llm_canonicals": str(llm_updates),
            "metrics.llm_canonicals.description": (
                "grupos onde normalizacao deterministica foi insuficiente "
                "e sugestao LLM foi aceita acima de confidence_threshold"
            ),
            "metrics.corpus_items_revised": str(items_revised),
            "metrics.normalization_rate": f"{normalization_rate:.3f}",
            "metrics.normalization_rate.formula": "corpus_items_revised / corpus_items_total",
            "metrics.normalization_rate.description": (
                "proporcao de ITEMs do corpus com pelo menos um codigo normalizado"
            ),
        }

        synr_doc = create_synr(
            syn_content=doc.content,
            header=header,
            item_revisions=revisions,
        )

        if output_dir:
            out_path = Path(output_dir) / file_path.name
        else:
            out_path = file_path.with_suffix(".synr")

        out_path = Path(out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_synr(out_path, synr_doc)
        files_updated += 1
        _log.info("Escrito: %s (%d revisão/ões)", out_path.name, n_revised)

    elapsed = time.monotonic() - start_time

    summary = (
        f"Normalize concluído em {elapsed:.1f}s\n"
        f"  Arquivos:  {len(docs_with_paths)} entrada(s) | {files_updated} saída(s)\n"
        f"  Códigos:   {total_codes} únicos | {len(residual_groups)} grupos residuais\n"
        f"  Revisões:  {items_revised} ITEM(s) com sugestão de normalização\n"
        f"  LLM:       {llm_updates} canonicals via LLM\n"
        f"  Modelo:    {llm_client.model}\n"
        f"  {llm_client.usage.summary_line()}"
    )

    if format == "verbose":
        header_str = (
            f"# synesis-coder normalize\n"
            f"# arquivos: {len(docs_with_paths)}\n"
            f"# códigos: {total_codes} únicos\n"
            f"# modelo: {llm_client.model} | confiança mínima: {confidence_threshold}\n"
            f"# {llm_client.usage.summary_line()}\n"
        )
        return header_str + "\n" + summary

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_inventory_txt(inventory: dict[str, CodeGroup], path: Path) -> None:
    """Escreve inventário de códigos em arquivo TXT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Synesis-Coder Code Inventory",
        f"# Grupos: {len(inventory)}",
        "",
    ]
    for nkey, group in sorted(inventory.items()):
        canonical = group.canonical or "(não resolvido)"
        source = "LLM" if group.llm_suggested else "determinístico"
        total = sum(group.variants.values())
        lines.append(f"[{nkey}] canonical={canonical} source={source} total={total}")
        for form, count in sorted(group.variants.items(), key=lambda x: -x[1]):
            marker = "*" if form == group.canonical else " "
            lines.append(f"  {marker} {form!r:40s}  n={count}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_project(syn_path: Optional[Path], project_path: Optional[Path]) -> Optional[Path]:
    if project_path is not None:
        return project_path
    if syn_path is None:
        return None

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
