"""Modo finetune: enriquecimento de dataset Alpaca via LLM (Camada 2).

Fluxo:
    1. Carregar pares Alpaca (Camada 1):
       a. Via --project: build_alpaca_pairs() internamente
       b. Via --input:   ler JSONL pré-gerado
    2. Filtrar pares de baixa qualidade (outputs vazios / muito curtos)
    3. Aplicar enriquecimentos LLM selecionados (concorrente):
       - vary          : variação de instruction (paráfrase)
       - didactic      : reformulação didática de chains/causais
       - counterfactual: pares contrafactuais a partir de chains
    4. Mesclar originais + enriquecidos, desduplicar, gravar JSONL final
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from synesis_coder.llm_client import LLMClient
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.synr_io import safe_write_output

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_MIN_OUTPUT_LEN = 10    # pares com output < N chars são descartados
_MIN_INSTRUCTION_LEN = 15  # pares com instruction < N chars são descartados

# Palavras-chave que identificam pares de chain/causal
_CHAIN_KEYWORDS = (
    "causal chain", "causal relationship", "chain annotation",
    "relationship between", "causes", "leads to", "results in",
    "What is the causal", "What causal", "chain between",
)


# ---------------------------------------------------------------------------
# Carregamento de pares
# ---------------------------------------------------------------------------


def _load_from_project(project_path: Path) -> List[Dict[str, str]]:
    """Gera pares Alpaca a partir do projeto via Camada 1."""
    from synesis_coder.project_loader import load_project

    ctx = load_project(project_path, load_annotations=True, load_ontology=False)
    result = ctx["result"]
    if not result.linked_project:
        raise ValueError(
            f"Projeto '{project_path.stem}' não compilou com sucesso. "
            "Verifique erros no projeto."
        )

    from synesis.exporters.alpaca_export import build_alpaca_pairs
    pairs = build_alpaca_pairs(
        result.linked_project,
        result.template,
        result.bibliography,
    )
    logger.info("Camada 1: %d pares gerados de '%s'", len(pairs), project_path.stem)
    return pairs


def _load_from_jsonl(input_path: Path) -> List[Dict[str, str]]:
    """Carrega pares Alpaca de arquivo JSONL pré-gerado."""
    pairs: List[Dict[str, str]] = []
    with input_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Linha %d ignorada (JSON inválido): %s", lineno, exc)
                continue
            if "instruction" in obj and "output" in obj:
                pairs.append(obj)
            else:
                logger.warning("Linha %d ignorada (campos obrigatórios ausentes)", lineno)
    logger.info("JSONL: %d pares carregados de '%s'", len(pairs), input_path.name)
    return pairs


# ---------------------------------------------------------------------------
# Filtro de qualidade
# ---------------------------------------------------------------------------


def _quality_filter(pairs: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], int]:
    """Remove pares de baixa qualidade.

    Critérios de descarte:
    - instruction vazia ou muito curta (< _MIN_INSTRUCTION_LEN chars)
    - output vazio ou muito curto (< _MIN_OUTPUT_LEN chars)

    Returns:
        (pares_filtrados, n_descartados)
    """
    kept: List[Dict[str, str]] = []
    discarded = 0
    for p in pairs:
        instr = (p.get("instruction") or "").strip()
        out = (p.get("output") or "").strip()
        if len(instr) < _MIN_INSTRUCTION_LEN or len(out) < _MIN_OUTPUT_LEN:
            discarded += 1
        else:
            kept.append(p)
    return kept, discarded


# ---------------------------------------------------------------------------
# Detecção de tipo de par
# ---------------------------------------------------------------------------


def _is_chain_pair(pair: Dict[str, str]) -> bool:
    """Retorna True se o par é de chain/causal."""
    instruction = (pair.get("instruction") or "").lower()
    return any(kw.lower() in instruction for kw in _CHAIN_KEYWORDS)


# ---------------------------------------------------------------------------
# Enriquecimentos LLM
# ---------------------------------------------------------------------------


def _build_vary_messages(pair: Dict[str, str]) -> List[dict]:
    """Monta mensagens para variação de instruction (paráfrase)."""
    return [
        {
            "role": "system",
            "content": (
                "You are a dataset diversification assistant for academic fine-tuning.\n"
                "Your task: rephrase the given instruction in a different way "
                "while preserving its exact meaning and specificity.\n"
                "Rules:\n"
                "- Keep all domain-specific terms, concept names, and field names intact.\n"
                "- Change sentence structure, wording, and phrasing.\n"
                "- Output ONLY the rephrased instruction. No explanations, no quotes."
            ),
            "cache": True,
        },
        {
            "role": "user",
            "content": pair["instruction"],
            "cache": False,
        },
    ]


def _build_didactic_messages(pair: Dict[str, str]) -> List[dict]:
    """Monta mensagens para reformulação didática de chain pair."""
    instruction = pair.get("instruction", "")
    inp = pair.get("input", "")
    out = pair.get("output", "")
    context = f"INSTRUCTION: {instruction}\nINPUT: {inp}\nOUTPUT: {out}"
    return [
        {
            "role": "system",
            "content": (
                "You are a research methodology teacher specializing in qualitative analysis.\n"
                "Given a causal chain annotation from a qualitative research corpus, "
                "reformulate it as a clear, accessible explanation suitable for teaching "
                "the causal mechanism to a new researcher.\n"
                "Output format — two fields on separate lines:\n"
                "QUESTION: <a pedagogical question about the causal mechanism>\n"
                "ANSWER: <a clear, 2-3 sentence explanation of the causal mechanism>"
            ),
            "cache": True,
        },
        {
            "role": "user",
            "content": context,
            "cache": False,
        },
    ]


def _build_counterfactual_messages(pair: Dict[str, str]) -> List[dict]:
    """Monta mensagens para geração de par contrafactual."""
    instruction = pair.get("instruction", "")
    inp = pair.get("input", "")
    out = pair.get("output", "")
    context = f"INSTRUCTION: {instruction}\nINPUT: {inp}\nOUTPUT: {out}"
    return [
        {
            "role": "system",
            "content": (
                "You are a critical thinking instructor for qualitative research.\n"
                "Given a causal chain annotation, generate a counterfactual scenario.\n"
                "Output format — two fields on separate lines:\n"
                "QUESTION: <a 'what if' question that inverts or removes the causal factor>\n"
                "ANSWER: <a 1-2 sentence explanation of what would change if the cause were absent or reversed>"
            ),
            "cache": True,
        },
        {
            "role": "user",
            "content": context,
            "cache": False,
        },
    ]


def _parse_qa_response(raw: str) -> Optional[Tuple[str, str]]:
    """Extrai QUESTION/ANSWER de resposta estruturada do LLM.

    Returns:
        (question, answer) ou None se parsing falhou.
    """
    question = None
    answer = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("QUESTION:"):
            question = line[len("QUESTION:"):].strip()
        elif line.startswith("ANSWER:"):
            answer = line[len("ANSWER:"):].strip()
        # Linhas de continuação do ANSWER (sem prefixo)
        elif answer is not None and line and not line.startswith("QUESTION:"):
            answer += " " + line
    if question and answer:
        return question, answer
    return None


# ---------------------------------------------------------------------------
# Processamento assíncrono de um par
# ---------------------------------------------------------------------------


async def _enrich_one(
    pair: Dict[str, str],
    enrich_types: Set[str],
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Enriquece um par com os tipos de enriquecimento selecionados.

    Returns:
        (novos_pares, contagem_por_tipo) — novos_pares não inclui o original.
    """
    async with semaphore:
        new_pairs: List[Dict[str, str]] = []
        counts: Dict[str, int] = {"vary": 0, "didactic": 0, "counterfactual": 0}

        # --- vary: paráfrase da instruction ---
        if "vary" in enrich_types:
            try:
                messages = _build_vary_messages(pair)
                rephrased = await llm_client.call_async(messages, temperature=0.7, max_tokens=512)
                rephrased = rephrased.strip()
                if rephrased and len(rephrased) >= _MIN_INSTRUCTION_LEN:
                    new_pairs.append({
                        "instruction": rephrased,
                        "input": pair.get("input", ""),
                        "output": pair["output"],
                    })
                    counts["vary"] += 1
            except Exception as exc:
                logger.warning("vary falhou: %s", exc)

        # --- didactic e counterfactual: apenas pares chain/causal ---
        if _is_chain_pair(pair):
            if "didactic" in enrich_types:
                try:
                    messages = _build_didactic_messages(pair)
                    raw = await llm_client.call_async(messages, temperature=0.3, max_tokens=512)
                    parsed = _parse_qa_response(raw)
                    if parsed:
                        q, a = parsed
                        new_pairs.append({
                            "instruction": q,
                            "input": pair.get("input", ""),
                            "output": a,
                        })
                        counts["didactic"] += 1
                except Exception as exc:
                    logger.warning("didactic falhou: %s", exc)

            if "counterfactual" in enrich_types:
                try:
                    messages = _build_counterfactual_messages(pair)
                    raw = await llm_client.call_async(messages, temperature=0.5, max_tokens=512)
                    parsed = _parse_qa_response(raw)
                    if parsed:
                        q, a = parsed
                        new_pairs.append({
                            "instruction": q,
                            "input": pair.get("input", ""),
                            "output": a,
                        })
                        counts["counterfactual"] += 1
                except Exception as exc:
                    logger.warning("counterfactual falhou: %s", exc)

        return new_pairs, counts


# ---------------------------------------------------------------------------
# Deduplicação final
# ---------------------------------------------------------------------------


def _deduplicate(pairs: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], int]:
    """Remove pares duplicados por (instruction, input).

    Returns:
        (pares_únicos, n_removidos)
    """
    seen: Set[Tuple[str, str]] = set()
    unique: List[Dict[str, str]] = []
    for p in pairs:
        key = (p.get("instruction", "").strip(), p.get("input", "").strip())
        if key not in seen:
            seen.add(key)
            unique.append(p)
    removed = len(pairs) - len(unique)
    return unique, removed


# ---------------------------------------------------------------------------
# Escrita do JSONL
# ---------------------------------------------------------------------------


def _write_jsonl(pairs: List[Dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_finetune(
    output_path: Path,
    project_path: Optional[Path] = None,
    input_path: Optional[Path] = None,
    enrich: Optional[List[str]] = None,
    concurrent: int = 5,
    model: Optional[str] = None,
    format: str = "plain",
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Enriquece dataset Alpaca via LLM e grava JSONL.

    Args:
        output_path: Caminho de saída para o JSONL enriquecido.
        project_path: Caminho para .synp (gera Camada 1 internamente). Mutuamente
            exclusivo com input_path.
        input_path: Caminho para JSONL pré-gerado (Camada 1 externa). Mutuamente
            exclusivo com project_path.
        enrich: Lista de enriquecimentos a aplicar. Valores válidos: "vary",
            "didactic", "counterfactual". Padrão: ["vary"].
        concurrent: Número máximo de chamadas LLM simultâneas.
        model: ID do modelo LLM (sobrescreve env SYNESIS_CODER_MODEL).
        format: "plain" ou "verbose".
        overwrite: Se True, sobrescreve output existente sem confirmação.
        backup: Se True, cria backup (.jsonl.bak) antes de gravar.

    Returns:
        String com resumo da execução.

    Raises:
        ValueError: Se nenhuma fonte de dados for fornecida, ou se as fontes
            forem ambíguas.
        FileNotFoundError: Se project_path ou input_path não existir.
    """
    if project_path is None and input_path is None:
        raise ValueError(
            "Forneça --project (gera dataset via compilador) "
            "ou --input (carrega JSONL pré-gerado)."
        )
    if project_path is not None and input_path is not None:
        raise ValueError(
            "--project e --input são mutuamente exclusivos. Use apenas um."
        )

    enrich_types = set(enrich) if enrich else {"vary"}

    return asyncio.run(
        _process_finetune_async(
            output_path=output_path,
            project_path=project_path,
            input_path=input_path,
            enrich_types=enrich_types,
            concurrent=concurrent,
            model=model,
            format=format,
            overwrite=overwrite,
            backup=backup,
        )
    )


def _fmt_eta(seconds: float) -> str:
    """Formata segundos restantes como string legível."""
    if seconds < 60:
        return f"~{int(seconds)}s"
    elif seconds < 3600:
        return f"~{int(seconds / 60)}min"
    else:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"~{h}h{m:02d}m"


def _fmt_rate(pairs_per_sec: float) -> str:
    """Formata taxa de processamento."""
    if pairs_per_sec >= 1.0:
        return f"{pairs_per_sec:.1f} p/s"
    else:
        return f"{pairs_per_sec * 60:.1f} p/min"


# Intervalo mínimo entre linhas de progresso (segundos)
_PROGRESS_INTERVAL = 5.0


async def _process_finetune_async(
    output_path: Path,
    project_path: Optional[Path],
    input_path: Optional[Path],
    enrich_types: Set[str],
    concurrent: int,
    model: Optional[str],
    format: str,
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Implementação assíncrona do modo finetune."""

    # Logging (níveis -v/-q e silenciamento de loggers de terceiros) é
    # configurado centralmente pela CLI (_configure_logging); não reconfigurar
    # aqui para não sobrescrever a escolha do usuário.

    start_time = time.monotonic()

    # 1. Carregar pares (Camada 1)
    if project_path is not None:
        pairs = _load_from_project(project_path)
    else:
        assert input_path is not None
        pairs = _load_from_jsonl(input_path)

    total_loaded = len(pairs)

    # 2. Filtro de qualidade
    pairs, n_discarded = _quality_filter(pairs)
    logger.info(
        "Filtro de qualidade: %d mantidos, %d descartados (< %d chars)",
        len(pairs), n_discarded, _MIN_OUTPUT_LEN,
    )

    if not pairs:
        raise ValueError(
            "Nenhum par de qualidade suficiente após filtro. "
            "Verifique se o projeto tem anotações."
        )

    # 3. Enriquecimento LLM (se houver tipos selecionados)
    enriched_pairs: List[Dict[str, str]] = []
    type_totals: Dict[str, int] = {"vary": 0, "didactic": 0, "counterfactual": 0}

    if enrich_types:
        total_to_process = len(pairs)
        logger.info(
            "=== Enriquecendo %d pares | tipos: %s | concorrência: %d ===",
            total_to_process, ", ".join(sorted(enrich_types)), concurrent,
        )

        llm_client = LLMClient(model=model)
        runtime_banner(llm_client, format=format)
        semaphore = asyncio.Semaphore(concurrent)

        tasks = [
            _enrich_one(pair, enrich_types, llm_client, semaphore)
            for pair in pairs
        ]

        processed = 0
        total_new = 0
        enrich_start = time.monotonic()
        last_log_time = enrich_start

        for coro in asyncio.as_completed(tasks):
            new_pairs, counts = await coro
            enriched_pairs.extend(new_pairs)
            total_new += len(new_pairs)
            for t, n in counts.items():
                type_totals[t] += n
            processed += 1

            now = time.monotonic()
            is_last = processed == total_to_process
            if is_last or (now - last_log_time) >= _PROGRESS_INTERVAL:
                elapsed_e = now - enrich_start
                rate = processed / elapsed_e if elapsed_e > 0 else 0.0
                remaining = total_to_process - processed
                eta_str = _fmt_eta(remaining / rate) if rate > 0 and not is_last else ""
                pct = processed / total_to_process * 100

                parts = [
                    f"[{processed:>{len(str(total_to_process))}}/{total_to_process} | {pct:5.1f}%]",
                    f"+{total_new} novos",
                    f"| {_fmt_rate(rate)}",
                ]
                if eta_str:
                    parts.append(f"| ETA {eta_str}")

                logger.info(" ".join(parts))
                last_log_time = now
    else:
        llm_client = None  # type: ignore[assignment]

    # 4. Mesclar originais + enriquecidos
    all_pairs = pairs + enriched_pairs

    # 5. Deduplicar
    all_pairs, n_dupes = _deduplicate(all_pairs)
    if n_dupes > 0:
        logger.info("Deduplicação: %d duplicatas removidas", n_dupes)

    # 6. Gravar JSONL (escrita atômica com proteção de sobrescrita e backup)
    import io
    buf = io.StringIO()
    for pair in all_pairs:
        buf.write(json.dumps(pair, ensure_ascii=False) + "\n")
    safe_write_output(
        Path(output_path).resolve(), buf.getvalue(), overwrite=overwrite, backup=backup
    )
    logger.info("Escrito: %s (%d pares)", output_path, len(all_pairs))

    elapsed = time.monotonic() - start_time

    # Linha de breakdown por tipo (apenas tipos que foram usados)
    breakdown_parts = [
        f"{t}: {type_totals[t]}"
        for t in sorted(enrich_types)
        if t in type_totals
    ]
    breakdown_str = "  Breakdown:    " + " | ".join(breakdown_parts) if breakdown_parts else ""

    summary_lines = [
        f"Fine-tuning dataset gerado em {elapsed:.1f}s",
        f"  Carregados:   {total_loaded}",
        f"  Descartados:  {n_discarded} (qualidade)",
        f"  Originais:    {len(pairs)}",
        f"  Enriquecidos: {len(enriched_pairs)} novos pares",
    ]
    if breakdown_str:
        summary_lines.append(breakdown_str)
    summary_lines += [
        f"  Duplicatas:   {n_dupes} removidas",
        f"  Total final:  {len(all_pairs)}",
        f"  Saída:        {output_path}",
    ]
    summary = "\n".join(summary_lines)

    logger.debug(summary)

    if format == "verbose":
        source_label = (
            f"projeto: {project_path.stem}" if project_path
            else f"input: {input_path.name}"  # type: ignore[union-attr]
        )
        token_line = (
            llm_client.usage.summary_line()
            if llm_client is not None
            else "tokens: N/A (sem enriquecimento LLM)"
        )
        header = (
            f"# synesis-coder finetune\n"
            f"# {source_label}\n"
            f"# enrich: {', '.join(sorted(enrich_types)) or 'nenhum'}\n"
            f"# {token_line}\n"
            f"# tempo: {elapsed:.1f}s\n"
        )
        return header + "\n" + summary

    return summary
