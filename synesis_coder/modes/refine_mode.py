"""Modo refine: re-extração com feedback em loop (fase opcional do pipeline ACT).

Diferentemente do `incorporate` (determinístico, aplica a sugestão do crítico via
substituição textual), o `refine` devolve a GERAÇÃO ao gerador: para cada ITEM
suspeito, o crítico aponta o erro e o extrator raciocina de novo sobre o
texto-fonte, produzindo uma nova anotação. É o padrão Self-Refine/Reflexion.

É opt-in explícito (subcomando próprio) — o `incorporate` determinístico permanece
o caminho padrão, preservando reprodutibilidade metodológica.

Fluxo por ITEM:
    1. source_text = _get_source_text(item, bibref, ctx)          [reuso critique]
    2. score inicial via critique                                 [reuso critique]
    3. loop (até MAX_ITER, enquanto score >= threshold):
        a. tags = critique(current, source)                       [reuso critique]
        b. candidate = re_extrair(source, feedback=tags)          [NOVO]
        c. validate_and_fix_async(candidate)                      [reuso validator]
        d. rejeita se inválido / ponto-fixo / oscilação / regressão de score
        e. aceita apenas versão com score ESTRITAMENTE menor
    4. emite SEMPRE a melhor versão observada (nunca uma intermediária pior)

Segurança (cláusulas do estudo §3):
    - Não-regressão: só aceita candidate com score < best_score.
    - MAX_ITER rígido + detecção de ponto-fixo e oscilação (histórico normalizado).
    - Crítico != gerador (clients/modelos distintos) contra viés de auto-validação.
    - Validação estrutural obrigatória antes de aceitar.
    - Rastreabilidade: trace de score por iteração no cabeçalho de métricas.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from synesis_coder.block_assembler import assemble_items
from synesis_coder.llm_client import LLMClient, get_critique_connection
from synesis_coder.modes.critique_mode import (
    _critique_tags,
    _extract_item_blocks_with_bibrefs,
    _get_source_text,
    _resolve_project,
    _score_of,
)
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import (
    build_item_refinement_prompt,
    build_item_refinement_values_prompt,
)
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.schema_builder import build_item_schema
from synesis_coder.synr_io import _END_ITEM, _ITEM_START, safe_write_output
from synesis_coder.validator import validate_and_fix_async

_log = logging.getLogger(__name__)

# Defaults (sobrescrevíveis por env / CLI)
DEFAULT_REFINE_MAX_ITER = 2
DEFAULT_SUSPICION_THRESHOLD = 0.20


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationRecord:
    """Score de suspeição observado numa iteração do loop de refino."""

    iteration: int
    score: float


@dataclass
class RefineResult:
    """Resultado do refino de um único ITEM (na ordem de aparecimento)."""

    bibref: str
    final_block: str
    final_score: float
    initial_score: float
    trace: list[IterationRecord] = field(default_factory=list)
    improved: bool = False


# ---------------------------------------------------------------------------
# Normalização para detecção de ponto-fixo / oscilação
# ---------------------------------------------------------------------------


_WS_RE = re.compile(r"\s+")


def _normalize_block(block: str) -> str:
    """Chave de comparação de blocos ITEM robusta a ruído de espaçamento/caixa.

    Usada apenas para detectar ausência de progresso (ponto-fixo) e oscilação —
    NUNCA altera o texto emitido. Colapsa whitespace e normaliza caixa para que
    duas versões semanticamente idênticas com formatação distinta não sejam
    tratadas como "progresso".
    """
    return _WS_RE.sub(" ", block.strip().lower())


# ---------------------------------------------------------------------------
# Re-extração informada por feedback (uma iteração de geração)
# ---------------------------------------------------------------------------


async def _re_extract(
    ctx: dict,
    bibref: str,
    source_text: str,
    prev_item_block: str,
    critique_tags: dict,
    refine_client: LLMClient,
    thinking_budget: int,
) -> str:
    """Gera uma nova versão do ITEM corrigindo os campos apontados pelo crítico.

    Prefere o caminho JSON (valores → assembler) quando o backend suporta
    json_schema, espelhando _generate_item_syn do modo item; cai para texto
    livre caso contrário. A validação estrutural é responsabilidade do chamador.
    """
    if refine_client.supports_json_schema():
        schema = build_item_schema(ctx)
        messages = build_item_refinement_values_prompt(
            ctx, bibref, source_text, prev_item_block, critique_tags
        )
        data = await refine_client.call_json_async(messages, schema, temperature=0.0)
        if data is not None:
            return assemble_items(ctx, bibref, data)

    # Fallback: texto livre. thinking só é honrado se budget > 0 (§4.5).
    messages = build_item_refinement_prompt(
        ctx, bibref, source_text, prev_item_block, critique_tags
    )
    return await refine_client.call_async(
        messages,
        temperature=0.0,
        thinking=thinking_budget > 0,
        thinking_budget=thinking_budget or None,
    )


# ---------------------------------------------------------------------------
# Loop de refino de um único ITEM
# ---------------------------------------------------------------------------


async def _refine_single_item(
    item_block: str,
    bibref: str,
    ctx: dict,
    critique_client: LLMClient,
    refine_client: LLMClient,
    semaphore: asyncio.Semaphore,
    suspicion_threshold: float,
    max_iter: int,
    thinking_budget: int,
) -> RefineResult:
    """Executa o loop de re-extração com feedback para um bloco ITEM.

    Retorna SEMPRE a melhor versão observada (a de menor suspicion_score),
    nunca uma intermediária pior — cláusula de não-regressão (§3.1).
    """
    async with semaphore:
        source_text = _get_source_text(item_block, bibref, ctx)

        # Score inicial (baseline). Se o critique falhar, trata como 0 (sem suspeita).
        initial_tags = await _critique_tags(
            item_block, bibref, ctx, critique_client, source_text=source_text
        )
        initial_score = _score_of(initial_tags) if initial_tags else 0.0

        best_block = item_block
        best_score = initial_score
        current = item_block
        history = {_normalize_block(item_block)}
        trace = [IterationRecord(0, initial_score)]

        for it in range(1, max_iter + 1):
            if best_score < suspicion_threshold:
                break  # convergiu — abaixo do limiar de suspeição

            tags = await _critique_tags(
                current, bibref, ctx, critique_client, source_text=source_text
            )
            if tags is None:
                break  # falha de critique — para com a melhor versão atual

            candidate = await _re_extract(
                ctx, bibref, source_text, current, tags, refine_client, thinking_budget
            )
            candidate, ok = await validate_and_fix_async(candidate, ctx, refine_client)
            if not ok:
                _log.warning(
                    "ITEM @%s iter %d: re-extração inválida — mantendo versão anterior",
                    bibref, it,
                )
                break

            norm = _normalize_block(candidate)
            if norm in history:
                _log.info(
                    "ITEM @%s iter %d: ponto-fixo/oscilação detectada — parando",
                    bibref, it,
                )
                break
            history.add(norm)

            cand_tags = await _critique_tags(
                candidate, bibref, ctx, critique_client, source_text=source_text
            )
            cand_score = _score_of(cand_tags) if cand_tags else best_score
            trace.append(IterationRecord(it, cand_score))

            if cand_score >= best_score:
                _log.info(
                    "ITEM @%s iter %d: score %.2f não melhora %.2f — parando (não-regressão)",
                    bibref, it, cand_score, best_score,
                )
                break

            best_block, best_score, current = candidate, cand_score, candidate

        return RefineResult(
            bibref=bibref,
            final_block=best_block,
            final_score=best_score,
            initial_score=initial_score,
            trace=trace,
            improved=_normalize_block(best_block) != _normalize_block(item_block),
        )


# ---------------------------------------------------------------------------
# Reconstrução do .syn a partir dos resultados
# ---------------------------------------------------------------------------


def _reassemble_syn(content: str, results: list[RefineResult]) -> str:
    """Substitui cada bloco ITEM pelo final_block correspondente, em ordem.

    Espelha o walk de blocos de incorporate_mode._process_item_blocks: preserva
    tudo que não é ITEM (SOURCE, comentários, linhas em branco) e troca cada
    bloco ITEM pela sua versão refinada na mesma posição.
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    item_idx = 0
    i = 0

    while i < len(lines):
        stripped = lines[i].rstrip("\r\n")
        if _ITEM_START.match(stripped):
            # Consumir o bloco ITEM inteiro (até END ITEM, inclusive).
            i += 1
            while i < len(lines) and not _END_ITEM.match(lines[i].rstrip("\r\n")):
                i += 1
            if i < len(lines):
                i += 1  # linha END ITEM

            if item_idx < len(results):
                block = results[item_idx].final_block.rstrip("\n")
                out.append(block + "\n")
            item_idx += 1
        else:
            out.append(lines[i])
            i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Cabeçalho de métricas (rastreabilidade §3.5)
# ---------------------------------------------------------------------------


def _build_metrics_header(
    results: list[RefineResult],
    syn_path: Path,
    critique_model: str,
    refine_model: str,
    max_iter: int,
    suspicion_threshold: float,
) -> str:
    """Constrói o cabeçalho # $metrics.refine.* do .syn final.

    Documenta as métricas agregadas com fórmulas explícitas (molde de
    incorporate_mode._build_metrics_header) e, por ITEM, o trace de score por
    iteração — preservando a rastreabilidade que a re-extração introduz um LLM
    no caminho de correção.
    """
    total = len(results)
    entered = sum(1 for r in results if r.initial_score >= suspicion_threshold)
    improved = sum(1 for r in results if r.improved)
    iters = [len(r.trace) - 1 for r in results]  # nº de re-extrações executadas
    iters_mean = (sum(iters) / total) if total else 0.0
    reductions = [r.initial_score - r.final_score for r in results if r.improved]
    reduction_mean = (sum(reductions) / len(reductions)) if reductions else 0.0
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [
        "# --- Fase R: Refine (re-extração com feedback, LLM) ---",
        f"# $metrics.source: {syn_path.name}",
        f"# $metrics.timestamp: {now}",
        f"# $metrics.refine.critique_model: {critique_model}",
        f"# $metrics.refine.refine_model: {refine_model}",
        f"# $metrics.refine.max_iter: {max_iter}",
        f"# $metrics.refine.threshold: {suspicion_threshold}",
        f"# $metrics.refine.items_total: {total}",
        "# $metrics.refine.items_total.formula: total de blocos ITEM no .syn de entrada",
        f"# $metrics.refine.items_entered_loop: {entered}",
        "# $metrics.refine.items_entered_loop.formula: ITEMs com score inicial >= threshold",
        f"# $metrics.refine.items_improved: {improved}",
        "# $metrics.refine.items_improved.formula: ITEMs cuja melhor versão difere da original",
        f"# $metrics.refine.iterations_mean: {iters_mean:.3f}",
        "# $metrics.refine.iterations_mean.formula: media de re-extracoes executadas por ITEM",
        f"# $metrics.refine.score_reduction_mean: {reduction_mean:.3f}",
        "# $metrics.refine.score_reduction_mean.formula: media de (score_inicial - score_final) sobre ITEMs melhorados",
        "# --- Trace por ITEM (score de suspeicao por iteracao) ---",
    ]
    for r in results:
        trace_str = " -> ".join(f"{rec.score:.2f}" for rec in r.trace)
        lines.append(f"# $refine.@{r.bibref}.trace: {trace_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_refine(
    syn_path: Path,
    project_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    concurrent: int = 3,
    critique_model: Optional[str] = None,
    refine_model: Optional[str] = None,
    max_iter: int = DEFAULT_REFINE_MAX_ITER,
    suspicion_threshold: float = DEFAULT_SUSPICION_THRESHOLD,
    thinking_budget: int = 0,
    format: str = "plain",
    overwrite: bool = False,
    backup: bool = False,
    debug: bool = False,
) -> str:
    """Re-extrai ITEMs suspeitos com feedback do crítico e emite o .syn final.

    Args:
        syn_path: Caminho do .syn a refinar.
        project_path: Caminho do .synp. Auto-detectado se None.
        output_path: Caminho do .syn final. Default: <stem>_refined.syn (nunca
            sobrescreve a entrada por acidente — ver guarda de I/O na CLI).
        concurrent: Máximo de ITEMs processados simultaneamente.
        critique_model: Modelo do crítico (override SYNESIS_CODER_CRITIQUE_MODEL).
        refine_model: Modelo do gerador (override SYNESIS_CODER_REFINE_MODEL).
        max_iter: Teto de iterações por ITEM.
        suspicion_threshold: Score abaixo do qual o ITEM é considerado convergido.
        thinking_budget: Tokens de extended thinking na re-extração (0 = desligado).
        format: "plain" ou "verbose".
        overwrite/backup: Repassados a safe_write_output.

    Returns:
        String com resumo da execução.

    Raises:
        FileNotFoundError: Se syn_path ou o projeto não existirem.
    """
    return asyncio.run(
        _process_refine_async(
            syn_path=syn_path,
            project_path=project_path,
            output_path=output_path,
            concurrent=concurrent,
            critique_model=critique_model,
            refine_model=refine_model,
            max_iter=max_iter,
            suspicion_threshold=suspicion_threshold,
            thinking_budget=thinking_budget,
            format=format,
            overwrite=overwrite,
            backup=backup,
            debug=debug,
        )
    )


async def _process_refine_async(
    syn_path: Path,
    project_path: Optional[Path],
    output_path: Optional[Path],
    concurrent: int,
    critique_model: Optional[str],
    refine_model: Optional[str],
    max_iter: int,
    suspicion_threshold: float,
    thinking_budget: int,
    format: str,
    overwrite: bool,
    backup: bool,
    debug: bool = False,
) -> str:
    """Implementação assíncrona do modo refine."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    start_time = time.monotonic()

    syn_path = Path(syn_path).resolve()
    if not syn_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {syn_path}")

    syn_content = syn_path.read_text(encoding="utf-8")

    resolved_project = _resolve_project(syn_path, project_path)
    if resolved_project is None:
        raise FileNotFoundError(
            f"Projeto .synp não encontrado próximo a {syn_path}. "
            "Use --project para especificá-lo."
        )
    ctx = load_project(resolved_project, load_annotations=True)

    items_with_bibrefs = _extract_item_blocks_with_bibrefs(syn_content)
    total_items = len(items_with_bibrefs)
    _log.info(
        "Refine de %s: %d ITEM(s), max_iter=%d, limiar=%.2f",
        syn_path.name, total_items, max_iter, suspicion_threshold,
    )

    # Crítico != gerador — clients (e modelos) distintos (§3.3).
    # Crítico → conexão de crítica (2ª API opcional); gerador → conexão primária.
    # Isso concretiza a independência epistêmica: crítico pode rodar em
    # provedor/família distinta do gerador. Sem vars CRITIQUE_* de conexão,
    # ambos usam a conexão global (comportamento atual).
    critique_client = LLMClient(model=critique_model, **get_critique_connection())
    refine_client = LLMClient(model=refine_model)
    runtime_banner(refine_client, format=format)

    semaphore = asyncio.Semaphore(concurrent)
    tasks = [
        _refine_single_item(
            item_block=item_block,
            bibref=bibref,
            ctx=ctx,
            critique_client=critique_client,
            refine_client=refine_client,
            semaphore=semaphore,
            suspicion_threshold=suspicion_threshold,
            max_iter=max_iter,
            thinking_budget=thinking_budget,
        )
        for bibref, item_block in items_with_bibrefs
    ]
    # gather preserva ordem: results[i] casa com items_with_bibrefs[i].
    results: list[RefineResult] = await asyncio.gather(*tasks)

    items_improved = sum(1 for r in results if r.improved)
    _log.info(
        "Refine concluído: %d/%d ITEMs melhorados", items_improved, total_items,
    )

    # Reconstruir o .syn com os blocos refinados + cabeçalho de métricas.
    refined_content = _reassemble_syn(syn_content, results)
    metrics_header = _build_metrics_header(
        results, syn_path, critique_client.model, refine_client.model,
        max_iter, suspicion_threshold,
    )
    final_content = metrics_header + "\n\n" + refined_content.strip() + "\n"

    if output_path is None:
        output_path = syn_path.with_name(syn_path.stem + "_refined.syn")
    output_path = Path(output_path).resolve()
    safe_write_output(output_path, final_content, overwrite=overwrite, backup=backup)

    elapsed = time.monotonic() - start_time

    # Uso combinado dos dois clients (crítico + gerador).
    total_calls = critique_client.usage.api_calls + refine_client.usage.api_calls
    total_in = critique_client.usage.input_tokens + refine_client.usage.input_tokens
    total_out = critique_client.usage.output_tokens + refine_client.usage.output_tokens

    summary = (
        f"Refine concluído em {elapsed:.1f}s\n"
        f"  Origem:    {syn_path.name}\n"
        f"  Saída:     {output_path}\n"
        f"  ITEMs:     {total_items} total | {items_improved} melhorados\n"
        f"  Crítico:   {critique_client.model}\n"
        f"  Gerador:   {refine_client.model}\n"
        f"  Iterações: max {max_iter} | limiar {suspicion_threshold}\n"
        f"  tokens: in {total_in} | out {total_out} | calls {total_calls}"
    )

    if format == "verbose":
        header = (
            f"# synesis-coder refine\n"
            f"# origem: {syn_path.name}\n"
            f"# saída: {output_path.name}\n"
            f"# ITEMs: {total_items} | melhorados: {items_improved}\n"
            f"# crítico: {critique_client.model} | gerador: {refine_client.model}\n"
        )
        return header + "\n" + summary

    return summary
