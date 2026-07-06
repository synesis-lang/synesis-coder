"""Modo abstract: processamento em lote de abstracts de um .bib em anotações Synesis.

Fluxo:
    1. load_project() → ctx
    2. parse_bib_entries() → lista de (bibref, abstract)
    3. Para cada entry (concorrente, rate-limited):
        a. build_abstract_prompt(ctx, bibref, abstract) → messages
        b. LLMClient.call_async(messages) → raw_syn
        c. validate_and_fix_async(raw_syn, ctx, llm_client) → (syn, ok)
        d. Gravar resultado em arquivo .syn
    4. Recarregar projeto periodicamente para atualizar code_index

O system prompt é construído uma vez e reutilizado (prompt caching).
Após cada batch, o projeto é recarregado para que o code_index reflita
os conceitos criados nas anotações recém-escritas.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bibtexparser

from synesis_coder.block_assembler import assemble_items, assemble_source
from synesis_coder.debug_log import DebugRecorder, now_human
from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_abstract_prompt, build_abstract_values_prompt
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.schema_builder import build_abstract_schema
from synesis_coder.validator import validate_and_fix_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BibTeX parsing
# ---------------------------------------------------------------------------


def parse_bib_entries(bib_path: Path) -> List[Dict[str, str]]:
    """Lê um .bib e retorna entradas com abstract.

    Args:
        bib_path: Caminho para o arquivo .bib.

    Returns:
        Lista de dicts com chaves "bibref" e "abstract".

    Raises:
        FileNotFoundError: Se o arquivo .bib não existir.
        ValueError: Se nenhuma entrada com abstract for encontrada.
    """
    bib_path = Path(bib_path).resolve()
    if not bib_path.exists():
        raise FileNotFoundError(f"Arquivo .bib não encontrado: {bib_path}")

    with open(bib_path, "r", encoding="utf-8") as f:
        bib_database = bibtexparser.load(f)

    entries: List[Dict[str, str]] = []
    skipped = 0

    for entry in bib_database.entries:
        bibref = entry.get("ID", "").strip()
        abstract = entry.get("abstract", "").strip()

        if not bibref:
            skipped += 1
            continue

        if not abstract:
            logger.info("Entrada '%s' sem abstract — pulando", bibref)
            skipped += 1
            continue

        entries.append({"bibref": bibref, "abstract": abstract})

    if not entries:
        raise ValueError(
            f"Nenhuma entrada com abstract encontrada em '{bib_path.name}' "
            f"({skipped} entrada(s) ignorada(s))."
        )

    logger.info(
        "Carregadas %d entradas com abstract de '%s' (%d ignoradas)",
        len(entries), bib_path.name, skipped,
    )
    return entries


# ---------------------------------------------------------------------------
# Processamento assíncrono de um abstract
# ---------------------------------------------------------------------------


async def _generate_abstract_syn(
    ctx: dict,
    bibref: str,
    abstract: str,
    llm_client: LLMClient,
    context: tuple,
) -> str:
    """Gera o texto Synesis de um abstract, preferindo o caminho JSON (Opção 3).

    Caminho JSON: envelope {"source": {...}, "items": [...]} → assembler monta
    SOURCE + N blocos ITEM. Cai para texto livre quando o backend não suporta
    json_schema ou a resposta não é JSON válido.
    """
    if llm_client.supports_json_schema():
        schema = build_abstract_schema(ctx)
        messages = build_abstract_values_prompt(ctx, bibref, abstract)
        data = await llm_client.call_json_async(
            messages, schema, temperature=0.0, context=context
        )
        if data is not None and "source" in data and "items" in data:
            source_block = assemble_source(ctx, bibref, data["source"])
            items_block = assemble_items(ctx, bibref, data)
            return source_block + "\n\n" + items_block

    messages = build_abstract_prompt(ctx, bibref, abstract)
    return await llm_client.call_async(messages, temperature=0.0, context=context)


async def _process_one_abstract(
    bibref: str,
    abstract: str,
    ctx: dict,
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
    entry_index: int = 0,
    total_entries: int = 0,
) -> Tuple[str, str, bool]:
    """Processa um abstract individual: prompt → LLM → validação.

    Args:
        bibref: Chave BibTeX.
        abstract: Texto do abstract.
        ctx: Contexto do projeto.
        llm_client: Cliente LLM (compartilhado).
        semaphore: Semáforo de concorrência.
        entry_index: Índice global da entrada (0-based) — usado pelo recorder.
        total_entries: Total de entradas do corpus — usado pelo recorder.

    Returns:
        (bibref, syn_output, success)
    """
    async with semaphore:
        logger.debug("Processando abstract: %s", bibref)
        context = ("entry", entry_index, total_entries, bibref)

        try:
            raw_syn = await _generate_abstract_syn(
                ctx, bibref, abstract, llm_client, context
            )
        except Exception as exc:
            logger.error("Falha na chamada LLM para %s: %s", bibref, exc)
            error_output = (
                f"# ERRO: chamada LLM falhou para @{bibref}\n"
                f"# {exc}\n"
            )
            return bibref, error_output, False

        annotation_key = f"{bibref}.syn"
        final_syn, success = await validate_and_fix_async(
            raw_syn, ctx, llm_client, annotation_key=annotation_key,
            recorder=llm_client.recorder, context=context,
        )

        if llm_client.recorder is not None:
            corrections = sum(
                1
                for c in llm_client.recorder._llm_calls
                if c.phase == "fix" and c.context and c.context[1] == entry_index
            )
            item_count = final_syn.count("ITEM @") if success else 0
            llm_client.recorder.record_chunk_summary(
                context=context,
                items_generated=item_count,
                corrections=corrections,
                success=success,
            )

        if success:
            logger.debug("OK: %s", bibref)
        else:
            logger.warning("Validação falhou para %s", bibref)

        return bibref, final_syn, success


# ---------------------------------------------------------------------------
# Processamento de batch
# ---------------------------------------------------------------------------


async def _process_batch(
    entries: List[Dict[str, str]],
    ctx: dict,
    llm_client: LLMClient,
    concurrent: int,
    output_dir: Path,
    per_reference: bool,
    progress_callback: Optional[callable] = None,
    index_base: int = 0,
    total_entries: int = 0,
) -> Tuple[int, int]:
    """Processa um batch de abstracts concorrentemente.

    Args:
        entries: Lista de dicts com "bibref" e "abstract".
        ctx: Contexto do projeto.
        llm_client: Cliente LLM compartilhado.
        concurrent: Número máximo de chamadas simultâneas.
        output_dir: Diretório de saída.
        per_reference: Se True, gera um .syn por referência.
        progress_callback: Callback para atualização de progresso.
        index_base: Índice global da primeira entrada deste batch (recorder).
        total_entries: Total de entradas do corpus (recorder).

    Returns:
        (total_ok, total_fail)
    """
    semaphore = asyncio.Semaphore(concurrent)

    tasks = [
        _process_one_abstract(
            entry["bibref"], entry["abstract"], ctx, llm_client, semaphore,
            entry_index=index_base + i, total_entries=total_entries,
        )
        for i, entry in enumerate(entries)
    ]

    total_ok = 0
    total_fail = 0
    all_outputs: List[Tuple[str, str]] = []  # (bibref, output)

    for coro in asyncio.as_completed(tasks):
        bibref, output, success = await coro
        all_outputs.append((bibref, output))
        if success:
            total_ok += 1
        else:
            total_fail += 1
        if progress_callback:
            progress_callback(bibref, success)

    # Gravar resultados
    if per_reference:
        for bibref, output in all_outputs:
            out_path = output_dir / f"{bibref}.syn"
            out_path.write_text(output + "\n", encoding="utf-8")
            logger.debug("Escrito: %s", out_path)
    else:
        # Arquivo único: todos os outputs concatenados
        combined_path = output_dir / "annotations.syn"
        combined = "\n\n".join(output for _, output in all_outputs)
        combined_path.write_text(combined + "\n", encoding="utf-8")
        logger.debug("Escrito: %s", combined_path)

    return total_ok, total_fail


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_abstract(
    project_path: Path,
    bib_path: Path,
    output_dir: Path,
    concurrent: int = 5,
    batch_size: int = 25,
    per_reference: bool = False,
    model: str | None = None,
    format: str = "plain",
    debug: bool = False,
) -> str:
    """Processa corpus .bib em lote, gerando anotações Synesis (.syn).

    Args:
        project_path: Caminho para o arquivo .synp.
        bib_path: Caminho para o arquivo .bib com abstracts.
        output_dir: Diretório de saída para os arquivos .syn.
        concurrent: Número máximo de chamadas LLM simultâneas.
        batch_size: Tamanho do batch (re-carrega projeto entre batches).
        per_reference: Se True, gera um .syn por referência bibliográfica.
            Se False (padrão), gera um único annotations.syn.
        model: ID do modelo LLM (sobrescreve env SYNESIS_CODER_MODEL).
        format: "plain" ou "verbose".
        debug: Se True, gera um relatório Markdown de auditoria do pipeline LLM
            no diretório de saída (<projeto>_abstract_debug.md).

    Returns:
        String com resumo da execução.
    """
    return asyncio.run(
        _process_abstract_async(
            project_path, bib_path, output_dir,
            concurrent, batch_size, per_reference, model, format, debug,
        )
    )


async def _process_abstract_async(
    project_path: Path,
    bib_path: Path,
    output_dir: Path,
    concurrent: int,
    batch_size: int,
    per_reference: bool,
    model: str | None,
    format: str,
    debug: bool = False,
) -> str:
    """Implementação assíncrona do processamento de abstracts."""

    # 1. Parsear .bib
    entries = parse_bib_entries(bib_path)
    total = len(entries)

    # 2. Criar diretório de saída
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 3. Inicializar LLM client (com recorder de debug se solicitado)
    recorder = DebugRecorder(
        unit_type="entry",
        unit_label="Referência",
        coding_step_title="Etapa 1 — Codificação dos abstracts",
    ) if debug else None
    llm_client = LLMClient(model=model, recorder=recorder)
    runtime_banner(llm_client, format=format)

    if recorder is not None:
        recorder.record_session_header(
            project=project_path.stem,
            input_name=bib_path.name,
            bibref=None,
            model=llm_client.model,
            backend=llm_client.backend,
            start=now_human(),
            total_chunks=total,
            temperature=0.0,
        )

    # 4. Processar em batches (recarregar projeto entre batches para
    #    atualizar code_index com conceitos recém-criados)
    total_ok = 0
    total_fail = 0
    start_time = time.monotonic()

    # Contador de progresso
    processed = 0

    def _progress(bibref: str, success: bool) -> None:
        nonlocal processed
        processed += 1
        status = "OK" if success else "FALHA"
        logger.debug("[%d/%d] %s: %s", processed, total, bibref, status)

    for batch_start in range(0, total, batch_size):
        batch_num = batch_start // batch_size + 1
        batch_entries = entries[batch_start : batch_start + batch_size]

        logger.debug(
            "Batch %d: %d entradas (%d/%d)",
            batch_num, len(batch_entries), batch_start + len(batch_entries), total,
        )

        # Recarregar projeto para incorporar .syn escritos no batch anterior
        ctx = load_project(project_path, load_annotations=True)

        ok, fail = await _process_batch(
            batch_entries, ctx, llm_client, concurrent,
            output_dir, per_reference, _progress,
            index_base=batch_start, total_entries=total,
        )
        total_ok += ok
        total_fail += fail

        # Cooldown entre batches (proporcional ao tempo do batch)
        if batch_start + batch_size < total:
            elapsed_so_far = time.monotonic() - start_time
            cooldown = min(30.0, max(5.0, elapsed_so_far * 0.1))
            logger.debug("Cooldown: %.1fs antes do próximo batch", cooldown)
            await asyncio.sleep(cooldown)

    elapsed = time.monotonic() - start_time
    rate = (total_ok / total * 100) if total > 0 else 0

    _sep = "-" * 50
    summary = (
        f"\n{_sep}\n"
        f"  Total     : {total} referências\n"
        f"  OK        : {total_ok} ({rate:.0f}%)\n"
        f"  Falhas    : {total_fail}\n"
        f"  Tempo     : {elapsed:.1f}s\n"
        f"  Saída     : {output_dir}\n"
        f"{_sep}"
    )
    logger.debug(summary)

    # Gravar relatório de debug (--debug)
    if recorder is not None:
        recorder.record_session_footer(
            total_chunks=total,
            total_ok=total_ok,
            total_fail=total_fail,
            tokens_line=llm_client.usage.summary_line(),
            elapsed=elapsed,
            validation="✅ OK" if total_fail == 0 else "⚠️ COM FALHAS",
            output_file=(
                "<um .syn por referência>" if per_reference else "annotations.syn"
            ),
        )
        debug_path = output_dir / f"{project_path.stem}_abstract_debug.md"
        recorder.write(debug_path)
        logger.debug("Relatório de debug escrito: %s", debug_path)

    if format == "verbose":
        header = (
            f"# synesis-coder abstract\n"
            f"# projeto: {project_path.stem}\n"
            f"# input: {bib_path.name}\n"
            f"# total: {total} | OK: {total_ok} | falhas: {total_fail}\n"
            f"# {llm_client.usage.summary_line()}\n"
            f"# tempo: {elapsed:.1f}s\n"
        )
        return header + "\n" + summary

    return summary
