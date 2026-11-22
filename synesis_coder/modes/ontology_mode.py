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

from synesis.ast.nodes import FieldType

from synesis_coder.block_assembler import assemble_ontology
from synesis_coder.concept_index import _as_list, concept_occurrences, item_field_value
from synesis_coder.llm_client import LLMClient
from synesis_coder.ontology_publish import (
    DestinationError,
    InputSnapshot,
    resolve_destination,
    validate_changes,
)
from synesis_coder.progress import BatchProgress
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import (
    build_ontology_prompt,
    build_ontology_values_prompt,
)
from synesis_coder.runtime_info import (
    campaign_summary,
    runtime_banner,
    warn_schema_fallbacks,
)
from synesis_coder.schema_builder import build_ontology_schema
from synesis_coder.syno_merge import (
    concept_key,
    entries_by_field,
    existing_blocks,
    field_names,
    merge_block,
    merge_into_syno,
)
from synesis_coder.synr_io import safe_write_output
from synesis_coder.validator import validate_ontology_entry_async

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Construção do semantic_ctx
# ---------------------------------------------------------------------------


def _build_semantic_ctx(code: str, ctx: dict) -> dict:
    """Constrói o contexto semântico de um código para injetar no prompt.

    Coleta dados do corpus já anotado, lendo os campos PELO TIPO declarado no
    template (nunca pelo nome):
    - Frequência: ITEMs em que o conceito aparece em qualquer campo CODE/CHAIN
    - Número de fontes bibliográficas distintas
    - Relações no grafo de chains (triples envolvendo o código)
    - Co-ocorrências: outros conceitos nos mesmos ITEMs
    - Exemplos concretos: até 3 ITEMs (fontes distintas primeiro), com os
      campos QUOTATION e MEMO e o valor do campo de termo onde o conceito aparece

    Antes, a função partia do `code_usage` do compilador (só CODE), lia o
    atributo `source_ref` (o ItemNode tem `bibref`) e procurava atributos
    `text`/`note`/`chain` que o ItemNode não tem: num projeto só-CHAIN (face85,
    Social Acceptance) todo conceito saía com frequência 0, 0 fontes e nenhum
    exemplo — as definições do .syno eram geradas sem evidência.

    Args:
        code: Nome do código (ex: "Social_Acceptance").
        ctx: Contexto do projeto (load_project).

    Returns:
        dict com chaves: frequency, sources, relations, co_codes, examples.
    """
    linked = ctx["result"].linked_project
    if not linked:
        return {"frequency": 0, "sources": 0, "relations": [], "co_codes": [], "examples": []}

    item_fields: dict = ctx.get("item_fields") or {}
    occ = ctx.get("_concept_occurrences")
    if occ is None:
        occ = concept_occurrences(linked, item_fields)
        ctx["_concept_occurrences"] = occ  # índice único por sessão (ctx é compartilhado)

    key = concept_key(code)
    ocorrencias = occ.get(key, [])
    frequency = len(ocorrencias)
    sources = len({bibref for bibref, _item, _t in ocorrencias})

    # Relações no grafo de chains (triple onde A==code ou B==code)
    all_triples: List[tuple] = getattr(linked, "all_triples", None) or []
    relations: List[tuple] = [
        (a, r, b) for a, r, b in all_triples
        if concept_key(a) == key or concept_key(b) == key
    ]

    # Co-ocorrências: outros conceitos nos mesmos ITEMs, por frequência
    co_code_counts: Dict[str, int] = {}
    for _bibref, _item, termos in ocorrencias:
        vistos = set()
        for pares in termos.values():
            for conceito, _texto in pares:
                ck = concept_key(conceito)
                if ck == key or ck in vistos:
                    continue
                vistos.add(ck)
                co_code_counts[conceito] = co_code_counts.get(conceito, 0) + 1
    co_codes = sorted(co_code_counts, key=lambda c: (-co_code_counts[c], c))

    # Exemplos: fontes distintas primeiro; campos de evidência e de interpretação
    # pelo TIPO, e o valor do campo de termo em que o conceito aparece.
    evid = [n for n, sp in item_fields.items() if sp.type == FieldType.QUOTATION]
    memo = [n for n, sp in item_fields.items() if sp.type == FieldType.MEMO]
    escolhidos: list = []
    vistas: set = set()
    for bibref, item, termos in ocorrencias:
        if bibref not in vistas:
            vistas.add(bibref)
            escolhidos.append((item, termos))
        if len(escolhidos) == 3:
            break
    for _bibref, item, termos in ocorrencias:
        if len(escolhidos) >= 3:
            break
        if all(item is not e for e, _t in escolhidos):
            escolhidos.append((item, termos))

    examples: List[dict] = []
    for item, termos in escolhidos:
        ex: dict = {}
        for name in evid + memo:
            vals = [str(v) for v in _as_list(item_field_value(item, name)) if v]
            if vals:
                ex[name] = " | ".join(vals)[:300]
        for name, pares in termos.items():
            textos = list(dict.fromkeys(
                t for c, t in pares if concept_key(c) == key))
            if textos:
                ex[name] = " ; ".join(textos)[:300]
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
            "--update: %d code(s) already defined in the .syno — skipping", skipped
        )

    return sorted(pending)


# Tipos que o modo ontology nao gera: sao estrutura (conceitos citados, cadeias),
# nao descricao. Ficam como estao no bloco (Spec A0, item 1: regra pelo tipo).
_STRUCTURAL_TYPES = (FieldType.CODE, FieldType.CHAIN)


def _generation_ctx(ctx: dict) -> dict:
    """Contexto com so os campos ONTOLOGY que o modo gera (sem CODE/CHAIN).

    Schema, prompt e montagem do bloco leem `ontology_fields`; com o filtro, o
    modelo nunca preenche nem ve como tarefa um campo estrutural.
    """
    # Fora: CODE/CHAIN (pelo tipo) e campos com papel LABEL (rótulos são
    # decisão terminológica, produzida pela fase de sinônimos, não pelo modo
    # descritivo — Spec A0, item 1).
    fields = {
        name: spec for name, spec in ctx.get("ontology_fields", {}).items()
        if spec.type not in _STRUCTURAL_TYPES
        and not str(getattr(spec, "role", None) or "").startswith("LABEL")
    }
    gen = dict(ctx)
    gen["ontology_fields"] = fields
    gen["required_ontology"] = [r for r in ctx.get("required_ontology", []) if r in fields]
    return gen


def _plan_work(
    codes: List[str],
    gen_ctx: dict,
    dest_blocks: dict,
    elsewhere: Set[str],
    update: bool,
) -> Tuple[Dict[str, Optional[Set[str]]], List[str]]:
    """O que gerar para cada codigo.

    Returns:
        ({codigo: campos a gravar (None = todos os gerados)}, codigos cujo bloco
        esta em outro .syno do projeto). Sem --update, todo codigo do corpus tem
        seus campos gerados reescritos. Com --update, um codigo sem bloco ganha
        bloco e um bloco ao qual falta campo REQUIRED geravel recebe so esse
        campo (antes, o bloco inteiro era pulado).
    """
    required = set(gen_ctx.get("required_ontology", []))
    plan: Dict[str, Optional[Set[str]]] = {}
    other_file: List[str] = []
    # `field_specs` tem todos os campos (o contexto de geração tira os CODE).
    use_name = next((n for n, s in (gen_ctx.get("field_specs") or {}).items()
                     if str(getattr(s, "role", None) or "") == "USE"), None)
    for code in codes:
        key = concept_key(code)
        block = dest_blocks.get(key)
        if block is not None and use_name and use_name in field_names(block):
            continue  # preterido (etapa U): o conceito é o alvo
        if block is None and key in elsewhere:
            other_file.append(code)
            continue
        if not update or block is None:
            plan[code] = None
            continue
        missing = required - set(field_names(block))
        if missing:
            plan[code] = missing
    return plan, other_file


# ---------------------------------------------------------------------------
# Processamento assíncrono de um código
# ---------------------------------------------------------------------------


async def _generate_ontology_syno(
    code: str,
    ctx: dict,
    semantic_ctx: dict,
    llm_client: LLMClient,
) -> str:
    """Gera o texto Synesis de uma entrada ONTOLOGY, preferindo o caminho JSON.

    Caminho JSON (Opção 3): o LLM devolve apenas os VALORES conforme JSON Schema
    → o assembler monta o bloco `ONTOLOGY <code> ... END ONTOLOGY`. Cai para o
    caminho de texto livre quando o backend não suporta json_schema ou a resposta
    não é JSON válido. No caminho JSON, o LLM nunca digita a moldura do bloco —
    a linha alucinada `ITEM <code> TYPE variable` que corrompia o .syno torna-se
    impossível (chaves fora de ONTOLOGY FIELDS são barradas por schema/assembler).
    """
    if llm_client.supports_json_schema():
        topics = ctx.get("topic_index", {}).get("topics", [])
        schema = build_ontology_schema(ctx, topics=topics)
        messages = build_ontology_values_prompt(ctx, code, semantic_ctx)
        data = await llm_client.call_json_async(messages, schema, temperature=0.0)
        if isinstance(data, dict):
            return assemble_ontology(ctx, code, data)

    messages = build_ontology_prompt(ctx, code, semantic_ctx)
    return await llm_client.call_async(messages, temperature=0.0)


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
        logger.info("Processing code: %s", code)

        semantic_ctx = _build_semantic_ctx(code, ctx)

        try:
            raw_syno = await _generate_ontology_syno(
                code, ctx, semantic_ctx, llm_client
            )
        except Exception as exc:
            logger.error("LLM call failed for '%s': %s", code, exc)
            error_output = (
                f"# ERROR: LLM call failed for ONTOLOGY {code}\n"
                f"# {exc}\n"
            )
            return code, error_output, False

        # Bloco existente: valida o bloco JA MESCLADO (campos curados + gerados),
        # para que um campo estrutural obrigatorio que ja existe conte. O que vai
        # para o arquivo e decidido depois, so pelos campos gerados.
        existing = ctx.get("_existing_blocks", {}).get(concept_key(code))
        wanted = ctx.get("_wanted_fields", {}).get(concept_key(code))
        candidate = raw_syno
        generated = entries_by_field(raw_syno)
        if existing is not None and generated:
            keep = {n: v for n, v in generated.items() if wanted is None or n in wanted}
            candidate = "".join(merge_block(existing, keep).lines)

        ontology_key = f"ontology_{code}.syno"
        final_syno, success = await validate_ontology_entry_async(
            candidate, ctx, llm_client, ontology_key=ontology_key,
        )

        if success:
            logger.info("OK: %s", code)
        else:
            logger.warning("Validation failed for '%s'", code)

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
    prompt_only: bool = False,
    *,
    client: Optional[object] = None,
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
        client: Cliente de modelo a usar no lugar de `LLMClient(...)`, com a mesma
            interface (teste, registro de chamadas, handoff). None = padrão.
        prompt_only: Se True, retorna o prompt montado em Markdown e não chama
            o LLM (nenhum arquivo é escrito).

    Returns:
        String com resumo da execução, ou o prompt em Markdown quando
        prompt_only=True.

    Raises:
        FileNotFoundError: Se o projeto não for encontrado.
        ValueError: Se o template não tiver ONTOLOGY scope, ou se não houver
            códigos anotados no projeto.
    """
    if prompt_only:
        from synesis_coder.prompt_dump import dump_prompt

        # Anotações desatualizadas em relação ao template não impedem a
        # inspeção do prompt — ver nota em abstract_mode.process_abstract.
        # O .syno é dispensado aqui (load_ontology=False): o prompt de ontology
        # deriva do template e do corpus .syn, e uma ontologia escrita sob um
        # template anterior abortaria a carga sem nada a acrescentar ao dump.
        ctx = load_project(
            project_path, load_annotations=True, load_ontology=False,
            tolerate_annotation_errors=True,
        )
        # Usa o primeiro código do corpus quando houver — o user message de
        # ontology deriva do corpus, não de um arquivo de entrada.
        codes = ctx.get("code_index", {}).get("codes", [])
        return dump_prompt(
            ctx, mode="ontology", text=codes[0] if codes else None
        )

    return asyncio.run(
        _process_ontology_async(
            project_path, output_path, update, concurrent, model, format,
            overwrite, backup, client=client,
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
    client: Optional[object] = None,
) -> str:
    """Implementação assíncrona da geração de ontologia."""

    # Logging é configurado centralmente pela CLI (_configure_logging); não
    # reconfigurar aqui para preservar os níveis de -v/-q e o silenciamento de
    # loggers de terceiros.

    # 1. Carregar projeto com anotações, sem compilar a ontologia existente.
    #
    # O .syno de destino é lido como TEXTO e mesclado por bloco (etapa A0a);
    # a validação do conjunto, no fim, compara com o estado anterior e só
    # rejeita o que a aplicação piorou. Exigir que a ontologia antiga compile
    # seria um impasse: um bloco sem campo REQUIRED — justamente o que
    # `--update` agora completa — ou escrito sob regras anteriores (ex.: ORDERED
    # com rótulo, hoje E088) abortaria a carga. Anotações .syn desatualizadas
    # também não impedem a geração: o modo lê delas os códigos e o contexto.
    ctx = load_project(
        project_path,
        load_annotations=True,
        load_ontology=False,
        tolerate_annotation_errors=True,
    )

    # 2. Verificar que o template tem ONTOLOGY scope
    if not ctx["has_ontology_scope"]:
        raise ValueError(
            f"The template of project '{project_path.stem}' defines no ONTOLOGY fields. "
            "The 'ontology' mode requires the template to have at least one field "
            "with SCOPE ONTOLOGY."
        )

    # 3. Verificar que há códigos anotados
    code_index = ctx["code_index"]
    if code_index["empty"]:
        raise ValueError(
            "No code found in the project annotations. "
            "Run 'abstract' or 'document' first to generate .syn annotations."
        )

    # 4. Destino e plano. O destino e um unico .syno (Spec A0, item 3): o
    # --output, que precisa ser local quando e um INCLUDE do projeto.
    try:
        destination = resolve_destination(project_path, output_path)
    except DestinationError as exc:
        raise ValueError(str(exc)) from exc
    snapshot = InputSnapshot.take(project_path, destination)
    # newline="": os fins de linha do original chegam intactos à mesclagem, que
    # os preserva (CRLF continua CRLF); read_text os normalizaria para LF.
    original = ""
    if destination.path.is_file():
        with open(destination.path, encoding="utf-8", newline="") as fh:
            original = fh.read()
    dest_blocks = existing_blocks(original)
    elsewhere: Set[str] = set()
    for other in destination.ontology_files.values():
        try:
            elsewhere.update(existing_blocks(other.read_text(encoding="utf-8")))
        except OSError:
            continue

    gen_ctx = _generation_ctx(ctx)
    if not gen_ctx["ontology_fields"]:
        raise ValueError(
            "The template only has CODE/CHAIN fields in the ONTOLOGY scope: the ontology mode "
            "generates descriptive fields and has nothing to produce."
        )
    plan, other_file = _plan_work(_get_pending_codes(ctx, update=False), gen_ctx, dest_blocks, elsewhere, update)
    if other_file:
        logger.info(
            "%d code(s) have a block in another .syno of the project and are left for a "
            "run with that destination (--output): %s",
            len(other_file), ", ".join(sorted(other_file)[:10]),
        )
    if not plan:
        return (
            "No pending code — every code already has a complete ONTOLOGY entry "
            "in the destination.\nRun without --update to regenerate the generated fields."
        )
    gen_ctx["_existing_blocks"] = {concept_key(c): dest_blocks[concept_key(c)]
                                   for c in plan if concept_key(c) in dest_blocks}
    gen_ctx["_wanted_fields"] = {concept_key(c): w for c, w in plan.items() if w is not None}
    pending_codes = sorted(plan)

    total = len(pending_codes)
    logger.info(
        "=== Generating ONTOLOGY for %d code(s) (concurrency: %d) ===",
        total, concurrent,
    )

    # 5. Inicializar LLM client
    llm_client = client if client is not None else LLMClient(model=model)
    runtime_banner(llm_client, format=format, concurrent=concurrent)

    # 6. Processar concorrentemente
    semaphore = asyncio.Semaphore(concurrent)
    start_time = time.monotonic()

    tasks = [
        _process_one_code(code, gen_ctx, llm_client, semaphore)
        for code in pending_codes
    ]

    total_ok = 0
    total_fail = 0
    results: List[Tuple[str, str]] = []  # (code, syno_output) — apenas válidos
    rejected: List[Tuple[str, str]] = []  # (code, syno_output) — falharam validação

    progress = BatchProgress(total, unit="code", usage=llm_client.usage)
    progress.start()
    for coro in asyncio.as_completed(tasks):
        code, syno_output, success = await coro
        if success:
            total_ok += 1
            results.append((code, syno_output))
        else:
            total_fail += 1
            rejected.append((code, syno_output))
        progress.mark(success=success, detail=code)

    # 7. Mesclar no destino. Bloco existente: so os campos gerados (os demais
    # campos, a ordem, os comentarios e os campos de varias linhas ficam). Sem
    # bloco: bloco novo no fim do arquivo. O resto do arquivo volta como estava.
    generated_names = set(gen_ctx["ontology_fields"])
    changes: Dict[str, Dict[str, List[str]]] = {}
    new_blocks: Dict[str, str] = {}
    for code, syno in sorted(results):
        key = concept_key(code)
        if key in dest_blocks:
            planned = plan[code]
            allowed = planned if planned is not None else generated_names
            fields = {n: v for n, v in entries_by_field(syno).items() if n in allowed}
            if fields:
                changes[key] = fields
        else:
            new_blocks[key] = syno

    def build(accepted: Set[str]) -> str:
        return merge_into_syno(
            original,
            {k: v for k, v in changes.items() if k in accepted},
            {k: v for k, v in new_blocks.items() if k in accepted},
        )

    # 8. Validar o CONJUNTO (projeto inteiro em memoria, com o destino alterado)
    # e conferir as entradas lidas no inicio antes de gravar.
    changed_keys = set(changes) | set(new_blocks)
    combined, set_rejected = validate_changes(ctx, destination, original, build, changed_keys)
    dest_changed, others_changed = snapshot.changed()
    if dest_changed:
        raise RuntimeError(
            f"{destination.path} was changed during the run; nothing was written. "
            "Run again on the current version."
        )
    if others_changed:
        # R3 (avaliação de 2026-11-17): a geração e a validação usaram o
        # contexto lido no início; com outra entrada mudada, nada é gravado.
        names = ", ".join(sorted(p.name for p in others_changed))
        raise RuntimeError(
            f"Project inputs changed during the run ({names}); nothing was written. "
            "The entries were generated and validated against the previous version. Run again."
        )
    for key, messages in sorted(set_rejected.items()):
        total_ok -= 1
        total_fail += 1
        rejected.append((key, f"# ERROR: rejected by the project validation: {key}\n"
                              + "\n".join(f"#   {m}" for m in messages)))

    output_path = destination.path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_write_output(
        output_path, combined if combined.endswith("\n") else combined + "\n",
        overwrite=update or overwrite,
        backup=backup,
        newline="",  # bytes como compostos: os fins de linha do original ficam
    )
    logger.info("Written: %s", output_path)

    if rejected:
        rejected.sort(key=lambda x: x[0])
        rejected_path = output_path.with_suffix(output_path.suffix + ".rejected")
        rejected_text = "\n\n".join(syno for _, syno in rejected)
        safe_write_output(
            rejected_path, rejected_text + "\n", overwrite=True, backup=False,
        )
        logger.warning(
            "%d block(s) failed validation and were written to %s "
            "(NOT included in the .syno, to avoid corrupting it)",
            len(rejected), rejected_path,
        )

    elapsed = time.monotonic() - start_time

    # Degradação silenciosa: entradas que caíram para texto livre rodaram sem
    # as restrições do schema (enum de topic, additionalProperties).
    warn_schema_fallbacks(llm_client)

    summary = campaign_summary(
        mode="ontology",
        project=project_path.stem,
        total=total,
        ok=total_ok,
        failed=total_fail,
        elapsed=elapsed,
        output=str(output_path),
        llm_client=llm_client,
        unit="entries",
    )
    # Sumário é o valor de RETORNO do modo; a CLI o imprime. Logar duplicaria.

    if format == "verbose":
        header = (
            f"# synesis-coder ontology\n"
            f"# project: {project_path.stem}\n"
            f"# total: {total} | OK: {total_ok} | failures: {total_fail}\n"
            f"# {llm_client.usage.summary_line()}\n"
            f"# time: {elapsed:.1f}s\n"
        )
        return header + "\n" + summary

    return summary
