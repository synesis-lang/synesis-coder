"""Modo dataset: processamento em lote de um corpus TOML (SOURCE + ITEMs por registro).

Espelha abstract_mode, mas a fonte é um dataset TOML estruturado (INCLUDE
DATASET no .synp) em vez de um .bib: cada registro (1 arquivo .toml) vira um
SOURCE + N ITEMs. Campos com origem-de-valor ON DATASET são resolvidos
deterministicamente pelo compilador (não pelo LLM); os campos interpretativos
(ITEM) são gerados pelo LLM a partir do contexto TOML relevante.

Fluxo:
    1. load_project() → ctx (ctx["dataset_index"] traz os registros indexados
       por chave, carregados a partir do INCLUDE DATASET do .synp).
    2. parse_dataset_records(ctx) → entradas {"bibref", "text"} — `text` é o
       contexto TOML serializado das seções relevantes.
    3. Para cada registro: gera SOURCE + ITEMs (caminho JSON do prompt_builder),
       valida via synesis.load (com dataset_index) e grava .syn.

AGNÓSTICO (D8): o loader e a chave são configurados pelo template; este modo
não presume schema de currículo Lattes. A parte determinística (parsing e
serialização de contexto) é testável sem qualquer chamada de LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from synesis_coder.block_assembler import count_item_blocks, dedupe_item_blocks
from synesis_coder.llm_client import LLMClient
from synesis_coder.modes.abstract_mode import _generate_abstract_syn
from synesis_coder.project_loader import load_project
from synesis_coder.validator import validate_and_fix_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing determinístico (SEM LLM) — testável offline
# ---------------------------------------------------------------------------


def parse_dataset_records(ctx: dict) -> List[Dict[str, str]]:
    """Extrai entradas (bibref, text) do dataset já carregado em ctx.

    O `text` é uma serialização legível das seções TOML do registro — insumo de
    contexto para o LLM gerar os campos interpretativos (ITEM). Os campos
    determinísticos ON DATASET NÃO dependem deste texto (o compilador os
    resolve).

    Args:
        ctx: Contexto de load_project(); usa ctx["dataset_index"].

    Returns:
        Lista de dicts {"bibref": chave, "text": contexto TOML serializado}.

    Raises:
        ValueError: Se não há dataset carregado (INCLUDE DATASET ausente ou
            template sem campo ON DATASET para descobrir a chave).
    """
    dataset_index: Optional[Dict[str, Any]] = ctx.get("dataset_index")
    if not dataset_index:
        raise ValueError(
            "Nenhum dataset carregado. Verifique se o .synp declara "
            'INCLUDE DATASET "<glob>.toml" e se o template tem ao menos um '
            "campo REQUIRED/OPTIONAL ... ON DATASET (a chave de indexação é "
            "descoberta do campo IDENTIFIES com ON DATASET)."
        )

    entries: List[Dict[str, str]] = [
        {"bibref": key, "text": _serialize_record(ctx, record)}
        for key, record in dataset_index.items()
    ]
    logger.info("Carregados %d registro(s) do dataset", len(entries))
    return entries


def _serialize_record(ctx: dict, record: Dict[str, Any]) -> str:
    """Serializa as seções TOML relevantes de um registro como contexto textual.

    Prioriza as seções declaradas em CONTEXT FROM DATASET nos campos do template
    (aplicando os pré-filtros do caminho); na ausência de declaração, serializa
    o registro inteiro exceto chaves internas.
    """
    from synesis.parser.dataset_loader import SOURCE_FILE_KEY, resolve_path

    sections = _declared_context_sections(ctx)
    if sections:
        parts: List[str] = []
        for path in sections:
            value = resolve_path(record, path)
            if value is None:
                continue
            parts.append(f"[{path}]\n{_dump(value)}")
        if parts:
            return "\n\n".join(parts)

    clean = {
        k: v
        for k, v in record.items()
        if not str(k).startswith("_") and k != SOURCE_FILE_KEY
    }
    return _dump(clean)


def _declared_context_sections(ctx: dict) -> List[str]:
    """Coleta as seções únicas de todos os CONTEXT FROM DATASET do template."""
    seen: List[str] = []
    for spec in ctx.get("field_specs", {}).values():
        sections = getattr(spec, "context_from_dataset", None)
        if not sections:
            continue
        for section in sections:
            if section not in seen:
                seen.append(section)
    return seen


def _dump(value: Any) -> str:
    """Serialização estável e legível (JSON indentado) de um valor TOML."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _external_origin_fields(ctx: dict) -> set:
    """Nomes de campos com origem-de-valor externa (ON BIBLIOGRAPHY / ON DATASET).

    Esses campos NUNCA devem aparecer no .syn — o compilador os resolve da fonte
    externa. Materializá-los grava valor errado (§11.3) e falha a validação
    (nome de campo desconhecido quando o LLM alucina uma variante).
    """
    return {
        name
        for name, spec in ctx.get("field_specs", {}).items()
        if getattr(spec, "value_origin", "document") in ("bibliography", "dataset")
    }


def _strip_external_fields(text: str, external: set) -> str:
    """Remove linhas `campo: valor` de campos externos de qualquer bloco gerado.

    Rede de segurança independente do caminho (JSON ou texto-livre): o caminho
    JSON já exclui via schema; o fallback texto-livre pode alucinar a linha. Aqui
    ela é removida antes da validação/escrita, garantindo que o .syn nunca
    materialize um campo ON DATASET/ON BIBLIOGRAPHY.
    """
    if not external:
        return text
    import re

    kept: List[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z_][\w]*)\s*:", line)
        if m and m.group(1) in external:
            continue
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Processamento (com LLM) — mesmo caminho de geração do abstract_mode
# ---------------------------------------------------------------------------


def process_dataset(
    project_path: Path,
    output_dir: Path,
    concurrent: int = 5,
    per_record: bool = True,
    model: str | None = None,
    format: str = "plain",
    debug: bool = False,
    dataset_glob: str | None = None,
) -> str:
    """Processa um corpus TOML em lote, gerando anotações Synesis (.syn).

    Args:
        project_path: Caminho do .synp (declara INCLUDE DATASET).
        output_dir: Diretório de saída dos .syn (um por registro).
        concurrent: Máximo de chamadas LLM simultâneas.
        per_record: Um .syn por registro (padrão).
        model: ID do modelo LLM.
        format: "plain" ou "verbose".
        debug: Relatório de auditoria (reservado; paridade de assinatura).
        dataset_glob: Se informado, substitui pontualmente o glob de
            `INCLUDE DATASET` do .synp (ex.: um único arquivo .toml para
            teste), sem editar o projeto no disco.

    Returns:
        Resumo da execução.
    """
    return asyncio.run(
        _process_dataset_async(
            project_path, output_dir, concurrent, per_record, model, format, debug,
            dataset_glob,
        )
    )


async def _process_dataset_async(
    project_path: Path,
    output_dir: Path,
    concurrent: int,
    per_record: bool,
    model: str | None,
    format: str,
    debug: bool,
    dataset_glob: str | None = None,
) -> str:
    """Implementação assíncrona: gera SOURCE + ITEMs por registro TOML."""
    ctx = load_project(
        project_path,
        load_annotations=False,
        tolerate_annotation_errors=True,
        dataset_glob_override=dataset_glob,
    )
    entries = parse_dataset_records(ctx)
    if not entries:
        return "Nenhum registro no dataset — nada a processar."

    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(
            f"'{output_dir}' existe como arquivo; --output-dir espera uma pasta."
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    llm_client = LLMClient(model=model) if model else LLMClient()
    semaphore = asyncio.Semaphore(concurrent)
    external = _external_origin_fields(ctx)

    logger.info("Iniciando geração (concurrent=%d)", concurrent)
    start_time = time.monotonic()

    async def _one(index: int, entry: Dict[str, str]) -> Tuple[str, bool]:
        async with semaphore:
            bibref, text = entry["bibref"], entry["text"]
            context = ("record", index, len(entries), bibref)
            try:
                raw = await _generate_abstract_syn(ctx, bibref, text, llm_client, context)
            except Exception as exc:  # noqa: BLE001
                logger.error("Falha na chamada LLM para %s: %s", bibref, exc)
                _write_syn(output_dir, bibref, f"# ERRO: LLM falhou para @{bibref}\n# {exc}\n", per_record)
                logger.info("[%d/%d] %s — ERRO: %s", index + 1, len(entries), bibref, exc)
                return bibref, False
            # Rede de segurança §11.3: remove campos ON DATASET/BIBLIOGRAPHY que o
            # caminho texto-livre possa ter alucinado (o caminho JSON já exclui).
            raw = _strip_external_fields(raw, external)
            final, ok = await validate_and_fix_async(
                raw, ctx, llm_client, annotation_key=f"{bibref}.syn"
            )
            # Re-strip: o loop de correção do validador pode reintroduzir um campo
            # externo alucinado. A rede de segurança roda por último, antes da
            # escrita, garantindo que o .syn nunca materialize ON DATASET/BIB.
            final = _strip_external_fields(final, external)

            # Loop degenerativo: modelos fracos re-emitem o mesmo ITEM até
            # esgotar tokens. É sintaticamente válido, logo invisível ao
            # compilador — a dedup é determinística, por texto do bloco.
            final, dupes = dedupe_item_blocks(final)
            if dupes:
                logger.warning(
                    "%s: %d bloco(s) ITEM duplicado(s) removido(s) — possível "
                    "loop degenerativo do modelo.", bibref, dupes,
                )

            # Cobertura: a validação garante SINTAXE, não que algo foi anotado.
            # Um .syn só com SOURCE é válido e seria reportado OK.
            n_items = count_item_blocks(final)
            if ok and n_items == 0:
                ok = False
                logger.error(
                    "%s: nenhum bloco ITEM gerado — o registro não produziu "
                    "anotação alguma (o .syn contém apenas SOURCE).", bibref,
                )

            _write_syn(output_dir, bibref, final, per_record)
            logger.info(
                "[%d/%d] %s — %s", index + 1, len(entries), bibref,
                "OK" if ok else ("SEM ITEMs" if n_items == 0 else "FALHA NA VALIDAÇÃO"),
            )
            return bibref, ok

    results = await asyncio.gather(*(_one(i, e) for i, e in enumerate(entries)))
    ok = sum(1 for _, s in results if s)
    elapsed = time.monotonic() - start_time
    return (
        f"Processados {len(results)} registro(s): {ok} OK, "
        f"{len(results) - ok} com falha ({elapsed:.1f}s)."
    )


def _write_syn(output_dir: Path, bibref: str, content: str, per_record: bool) -> None:
    """Grava o .syn de um registro (um arquivo por registro quando per_record)."""
    safe = bibref.lstrip("@").strip().replace("/", "_")
    target = output_dir / (f"{safe}.syn" if per_record else "dataset.syn")
    mode = "w" if per_record else "a"
    with open(target, mode, encoding="utf-8") as fh:
        fh.write(content.rstrip() + "\n")
