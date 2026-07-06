"""Modo ontology: geração de entradas ONTOLOGY (.syno) a partir do corpus anotado.

Fluxo:
    1. load_project(load_annotations=True, load_ontology=True) → ctx
    2. Verificar que o template tem ONTOLOGY scope → erro claro caso contrário
    3. Derivar codes pendentes (todos, ou apenas novos se --update)
    4. Para cada code (concorrente, rate-limited):
        a. Montar semantic_ctx (frequência, fontes, relações, co-ocorrências, exemplos)
        b. build_ontology_prompt(ctx, code, semantic_ctx) → messages
        c. LLMClient.call_async(messages) → raw_syno
        d. validate_ontology_entry_async(raw_syno, ctx, llm_client) → (syno, ok)
    5. Combinar entradas válidas → gravar .syno

O system prompt é construído uma vez (prompt caching).
O semantic_ctx injeta contexto rico derivado do corpus para que o LLM
produza definições baseadas no uso observado, não em conhecimento genérico.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_ontology_prompt
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.synr_io import safe_write_output
from synesis_coder.validator import validate_ontology_entry_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Construção do semantic_ctx
# ---------------------------------------------------------------------------


def _build_semantic_ctx(code: str, ctx: dict) -> dict:
    """Constrói o contexto semântico de um código para injetar no prompt.

    Coleta dados do corpus já anotado:
    - Frequência total de uso
    - Número de fontes bibliográficas distintas
    - Relações no grafo de chains (triples envolvendo o código)
    - Co-ocorrências (outros códigos no mesmo ITEM)
    - Exemplos concretos dos primeiros 3 ITEMs

    Args:
        code: Nome do código (ex: "Social_Acceptance").
        ctx: Contexto do projeto com code_usage, all_triples, etc.

    Returns:
        dict com chaves: frequency, sources, relations, co_codes, examples.
    """
    linked = ctx["result"].linked_project
    if not linked:
        return {"frequency": 0, "sources": 0, "relations": [], "co_codes": [], "examples": []}

    code_usage: Dict[str, list] = linked.code_usage
    all_triples: List[tuple] = linked.all_triples

    # Frequência e fontes
    items = code_usage.get(code, [])
    frequency = len(items)
    sources = len({getattr(item, "source_ref", None) for item in items} - {None})

    # Relações no grafo de chains (triple onde A==code ou B==code)
    relations: List[tuple] = [
        (a, r, b) for a, r, b in all_triples if a == code or b == code
    ]

    # Co-ocorrências: outros códigos que aparecem junto com este em algum ITEM
    co_code_counts: Dict[str, int] = {}
    for item in items:
        # Tentar derivar codes do mesmo ITEM via all_triples com mesma fonte/bibref
        item_ref = getattr(item, "source_ref", None)
        if item_ref is None:
            continue
        for other_code, other_items in code_usage.items():
            if other_code == code:
                continue
            for other_item in other_items:
                if getattr(other_item, "source_ref", None) == item_ref:
                    co_code_counts[other_code] = co_code_counts.get(other_code, 0) + 1

    # Ordenar co-ocorrências por frequência decrescente
    co_codes = sorted(co_code_counts, key=lambda c: -co_code_counts[c])

    # Exemplos concretos: campos dos primeiros 3 ITEMs
    examples: List[dict] = []
    for item in items[:3]:
        ex: dict = {}
        # Coletar campos disponíveis no ItemNode
        for field_name in ("text", "note", "chain", "topic", "code"):
            val = getattr(item, field_name, None)
            if val:
                ex[field_name] = str(val)[:200]  # truncar para não exceder tokens
        # Campos adicionais via __dict__ se disponível
        if not ex and hasattr(item, "__dict__"):
            for k, v in item.__dict__.items():
                if v and not k.startswith("_") and k not in ("source_ref",):
                    ex[k] = str(v)[:200]
        if ex:
            examples.append(ex)

    return {
        "frequency": frequency,
        "sources": sources,
        "relations": relations,
        "co_codes": co_codes,
        "examples": examples,
    }


# ---------------------------------------------------------------------------
# Derivar códigos pendentes
# ---------------------------------------------------------------------------


def _get_pending_codes(ctx: dict, update: bool) -> List[str]:
    """Retorna lista de códigos que precisam de entrada ONTOLOGY.

    Args:
        ctx: Contexto do projeto com code_index e ontology_index.
        update: Se True, gera apenas para códigos sem entrada existente.

    Returns:
        Lista de códigos ordenada alfabeticamente.
    """
    code_index = ctx["code_index"]
    all_codes: List[str] = code_index["codes"]

    if not update:
        return sorted(all_codes)

    # --update: filtrar códigos que já têm entrada no .syno
    ontology_index: dict = ctx.get("ontology_index", {})
    already_defined: Set[str] = set(ontology_index.keys())

    pending = [c for c in all_codes if c not in already_defined]
    skipped = len(all_codes) - len(pending)
    if skipped > 0:
        logger.info(
            "--update: %d código(s) já definido(s) no .syno — pulando", skipped
        )

    return sorted(pending)


# ---------------------------------------------------------------------------
# Processamento assíncrono de um código
# ---------------------------------------------------------------------------


async def _process_one_code(
    code: str,
    ctx: dict,
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> Tuple[str, str, bool]:
    """Processa um código individual: semantic_ctx → prompt → LLM → validação.

    Args:
        code: Nome do código a definir.
        ctx: Contexto do projeto.
        llm_client: Cliente LLM compartilhado.
        semaphore: Semáforo de concorrência.

    Returns:
        (code, syno_output, success)
    """
    async with semaphore:
        logger.info("Processando código: %s", code)

        semantic_ctx = _build_semantic_ctx(code, ctx)
        messages = build_ontology_prompt(ctx, code, semantic_ctx)

        try:
            raw_syno = await llm_client.call_async(messages, temperature=0.0)
        except Exception as exc:
            logger.error("Falha na chamada LLM para '%s': %s", code, exc)
            error_output = (
                f"# ERRO: chamada LLM falhou para ONTOLOGY {code}\n"
                f"# {exc}\n"
            )
            return code, error_output, False

        ontology_key = f"ontology_{code}.syno"
        final_syno, success = await validate_ontology_entry_async(
            raw_syno, ctx, llm_client, ontology_key=ontology_key,
        )

        if success:
            logger.info("OK: %s", code)
        else:
            logger.warning("Validação falhou para '%s'", code)

        return code, final_syno, success


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_ontology(
    project_path: Path,
    output_path: Path,
    update: bool = False,
    concurrent: int = 5,
    model: Optional[str] = None,
    format: str = "plain",
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Gera entradas ONTOLOGY (.syno) a partir do corpus anotado do projeto.

    Args:
        project_path: Caminho para o arquivo .synp.
        output_path: Caminho de saída para o arquivo .syno gerado.
        update: Se True, gera apenas para códigos sem entrada existente no .syno.
        concurrent: Número máximo de chamadas LLM simultâneas.
        model: ID do modelo LLM (sobrescreve env SYNESIS_CODER_MODEL).
        format: "plain" ou "verbose".
        overwrite: Se True, sobrescreve output existente sem confirmação.
        backup: Se True, cria backup (.syno.bak) antes de gravar.

    Returns:
        String com resumo da execução.

    Raises:
        FileNotFoundError: Se o projeto não for encontrado.
        ValueError: Se o template não tiver ONTOLOGY scope, ou se não houver
            códigos anotados no projeto.
    """
    return asyncio.run(
        _process_ontology_async(
            project_path, output_path, update, concurrent, model, format,
            overwrite, backup,
        )
    )


async def _process_ontology_async(
    project_path: Path,
    output_path: Path,
    update: bool,
    concurrent: int,
    model: Optional[str],
    format: str,
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Implementação assíncrona da geração de ontologia."""

    # Logging é configurado centralmente pela CLI (_configure_logging); não
    # reconfigurar aqui para preservar os níveis de -v/-q e o silenciamento de
    # loggers de terceiros.

    # 1. Carregar projeto com anotações e ontologia existente
    ctx = load_project(
        project_path,
        load_annotations=True,
        load_ontology=True,
    )

    # 2. Verificar que o template tem ONTOLOGY scope
    if not ctx["has_ontology_scope"]:
        raise ValueError(
            f"O template do projeto '{project_path.stem}' não define campos ONTOLOGY. "
            "O modo 'ontology' requer que o template tenha pelo menos um campo "
            "com SCOPE ONTOLOGY."
        )

    # 3. Verificar que há códigos anotados
    code_index = ctx["code_index"]
    if code_index["empty"]:
        raise ValueError(
            "Nenhum código encontrado nas anotações do projeto. "
            "Execute 'abstract' ou 'document' primeiro para gerar anotações .syn."
        )

    # 4. Derivar códigos pendentes
    pending_codes = _get_pending_codes(ctx, update)
    if not pending_codes:
        return (
            "Nenhum código pendente — todos os códigos já possuem entrada ONTOLOGY.\n"
            "Use sem --update para regenerar todas as entradas."
        )

    total = len(pending_codes)
    logger.info(
        "=== Gerando ONTOLOGY para %d código(s) (concorrência: %d) ===",
        total, concurrent,
    )

    # 5. Inicializar LLM client
    llm_client = LLMClient(model=model)
    runtime_banner(llm_client, format=format)

    # 6. Processar concorrentemente
    semaphore = asyncio.Semaphore(concurrent)
    start_time = time.monotonic()

    tasks = [
        _process_one_code(code, ctx, llm_client, semaphore)
        for code in pending_codes
    ]

    processed = 0
    total_ok = 0
    total_fail = 0
    results: List[Tuple[str, str]] = []  # (code, syno_output)

    for coro in asyncio.as_completed(tasks):
        code, syno_output, success = await coro
        results.append((code, syno_output))
        processed += 1
        if success:
            total_ok += 1
        else:
            total_fail += 1
        status = "OK" if success else "FALHA"
        logger.info("[%d/%d] %s: %s", processed, total, code, status)

    # 7. Combinar entradas e gravar .syno
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Ordenar por nome de código para output determinístico
    results.sort(key=lambda x: x[0])
    combined = "\n\n".join(syno for _, syno in results)

    # Escrita atômica com proteção de sobrescrita e backup opcional
    # Em modo --update o arquivo já existe e é intencionalmente sobrescrito
    safe_write_output(
        output_path, combined + "\n",
        overwrite=update or overwrite,
        backup=backup,
    )
    logger.info("Escrito: %s", output_path)

    elapsed = time.monotonic() - start_time
    rate = (total_ok / total * 100) if total > 0 else 0

    summary = (
        f"Processamento concluído em {elapsed:.1f}s\n"
        f"  Total: {total}\n"
        f"  OK: {total_ok} ({rate:.0f}%)\n"
        f"  Falhas: {total_fail}\n"
        f"  Saída: {output_path}"
    )
    logger.debug(summary)

    if format == "verbose":
        header = (
            f"# synesis-coder ontology\n"
            f"# projeto: {project_path.stem}\n"
            f"# total: {total} | OK: {total_ok} | falhas: {total_fail}\n"
            f"# {llm_client.usage.summary_line()}\n"
            f"# tempo: {elapsed:.1f}s\n"
        )
        return header + "\n" + summary

    return summary
