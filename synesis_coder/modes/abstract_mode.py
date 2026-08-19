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
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bibtexparser

from synesis_coder.block_assembler import (
    assemble_items,
    assemble_source,
    count_item_blocks,
    dedupe_item_blocks,
)
from synesis_coder.debug_log import DebugRecorder, now_human
from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import load_project
from synesis_coder.prompt_builder import build_abstract_prompt, build_abstract_values_prompt
from synesis_coder.runtime_info import runtime_banner, warn_schema_fallbacks
from synesis_coder.schema_builder import build_abstract_schema
from synesis_coder.synr_io import safe_write_output
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
        if data is not None:
            if "source" in data and "items" in data:
                source_block = assemble_source(ctx, bibref, data["source"])
                items_block = assemble_items(ctx, bibref, data)
                return source_block + "\n\n" + items_block
            # JSON válido, mas sem o envelope que este modo exige. Sem este
            # registro a queda para texto livre seria invisível: `call_json`
            # devolveu um dict (não contabilizou fallback) e o bloco resultante
            # sai válido, marcado OK, porém sem as garantias do schema.
            logger.warning(
                "%s: resposta JSON sem as chaves 'source'/'items' (recebido: "
                "%s) — gerando em TEXTO LIVRE, sem as garantias do schema.",
                bibref, sorted(data.keys())[:5],
            )
            llm_client.usage.record_schema_fallback()

    messages = build_abstract_prompt(ctx, bibref, abstract)
    return await llm_client.call_async(messages, temperature=0.0, context=context)


# Marca de registro que falhou. Precede um SOURCE sintaticamente válido para
# que o arquivo continue parseável — ver _build_failure_block.
FAILURE_MARKER = "# $status: failed"


def _placeholder_value(spec) -> str:
    """Valor sentinela válido para o TIPO do campo.

    Um texto livre em campo ENUMERATED produziria `InvalidEnumeratedValue` e
    quebraria a validação do projeto — por isso o valor deriva do tipo, não é
    uma string fixa.
    """
    type_name = getattr(spec.type, "name", str(spec.type))
    if type_name in ("ENUMERATED", "ORDERED") and getattr(spec, "values", None):
        return spec.values[0].label
    if type_name == "SCALE":
        return "0"
    if type_name == "DATE":
        return "1900-01-01"
    return "(falha no processamento)"


def _build_failure_block(ctx: dict, bibref: str, reason: str) -> str:
    """Monta um .syn de falha que o compilador consegue parsear.

    Um arquivo contendo APENAS comentários não é parseável (`Token inesperado
    <EOF>`). Como o batch seguinte recarrega o projeto com glob de
    `annotations/*.syn`, um único registro falho derrubava `load_project()` e
    abortava a campanha inteira. Ver Estudo_Saida_Particionada_e_Incremental §8.1.

    O bloco emitido carrega a marca FAILURE_MARKER (para `--resume` distinguir
    falha de sucesso) e um SOURCE com os campos REQUIRED preenchidos por
    sentinelas compatíveis com cada tipo.
    """
    source_fields = ctx.get("source_fields", {})
    required = ctx.get("required_source", [])

    header = f"{FAILURE_MARKER}\n# ERRO: {reason}\n"

    if not source_fields or not required:
        # Template sem campos SOURCE obrigatórios: um SOURCE vazio já é válido.
        return header + f"SOURCE @{bibref}\nEND SOURCE\n"

    data = {
        name: _placeholder_value(source_fields[name])
        for name in required
        if name in source_fields
    }
    return header + assemble_source(ctx, bibref, data) + "\n"


def is_failed_output(content: str) -> bool:
    """True se o conteúdo de um .syn foi gravado como falha."""
    return FAILURE_MARKER in content


# --- Particionamento da saída (--split-every) -------------------------------

SPLIT_FILE_TEMPLATE = "annotations_{index:04d}.syn"
_SPLIT_FILE_RE = re.compile(r"^annotations_\d{4}\.syn$")


def split_filename(position: int, split_every: int) -> str:
    """Nome do arquivo de partição para o registro na `position` (0-based).

    O índice deriva da POSIÇÃO GLOBAL do registro no corpus, não de um contador
    de escrita. Isso mantém o mapeamento registro→arquivo estável entre
    execuções e sob `--resume`: retomar no meio não desloca nada do que já foi
    gravado.
    """
    return SPLIT_FILE_TEMPLATE.format(index=position // split_every + 1)


def is_split_file(name: str) -> bool:
    """True para nomes gerados por `split_filename`."""
    return bool(_SPLIT_FILE_RE.match(name))


def resolve_output_mode(per_reference: bool, split_every: Optional[int]) -> str:
    """Valida a combinação de modos de saída e devolve o escolhido.

    Returns:
        "per_reference" | "split" | "single".

    Raises:
        ValueError: se as opções forem mutuamente exclusivas ou inválidas.
    """
    if per_reference and split_every:
        raise ValueError(
            "--per-reference e --split-every são mutuamente exclusivos: o "
            "primeiro já gera um arquivo por referência."
        )
    if split_every is not None:
        if split_every < 1:
            raise ValueError("--split-every deve ser >= 1.")
        return "split"
    return "per_reference" if per_reference else "single"


# --- Retomada (--resume) ----------------------------------------------------
# O estado é derivado do DISCO, não de um manifesto de progresso: um arquivo de
# controle seria uma segunda fonte de verdade capaz de divergir do que foi
# realmente gravado. O .syn escrito É o registro.

_SOURCE_LINE = re.compile(r"^SOURCE\s+@(\S+)\s*$", re.MULTILINE)
_ITEM_LINE = re.compile(r"^\s*ITEM\s+@(\S+)\s*$", re.MULTILINE)


def is_complete_output(content: str) -> bool:
    """True se o conteúdo representa um registro processado COM SUCESSO.

    Exige as três condições simultaneamente:

    - não carrega a marca de falha (Etapa 0);
    - tem ao menos um bloco SOURCE;
    - tem ao menos um bloco ITEM.

    A terceira é o que impede o defeito descrito no Estudo §8.2: um registro que
    falhou por *zero ITEMs* grava um SOURCE completo e seria confundido com
    trabalho pronto se a verificação olhasse apenas o SOURCE. Vale também para
    `.syn` legados, gravados antes da marca existir.
    """
    if not content or is_failed_output(content):
        return False
    return bool(_SOURCE_LINE.search(content)) and bool(_ITEM_LINE.search(content))


def completed_bibrefs(
    output_dir: Path,
    per_reference: bool,
    split_every: Optional[int] = None,
) -> set[str]:
    """Bibrefs já processados com sucesso, lidos do diretório de saída.

    Args:
        output_dir: Diretório onde os .syn foram gravados.
        per_reference: True quando cada bibref tem seu próprio arquivo.
        split_every: Definido no modo particionado — varre `annotations_NNNN.syn`.

    Returns:
        Conjunto de bibrefs a pular. Vazio se não houver saída anterior.
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return set()

    done: set[str] = set()

    if per_reference:
        for path in sorted(output_dir.glob("*.syn")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Ignorando %s ao retomar: %s", path.name, exc)
                continue
            if is_complete_output(content):
                # O nome do arquivo é a fonte do bibref; o SOURCE interno pode
                # divergir se o arquivo foi editado à mão.
                done.add(path.stem)
        return done

    if split_every:
        for path in sorted(output_dir.glob("*.syn")):
            if not is_split_file(path.name):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Ignorando %s ao retomar: %s", path.name, exc)
                continue
            for bibref, chunk in _iter_records(content):
                if is_complete_output(chunk):
                    done.add(bibref)
        return done

    combined = output_dir / "annotations.syn"
    if not combined.exists():
        return set()
    try:
        content = combined.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Não foi possível ler %s ao retomar: %s", combined.name, exc)
        return set()

    # No arquivo único os registros são concatenados. Fatiar por linha em branco
    # partiria blocos ao meio (SOURCE e ITEMs de um mesmo registro são separados
    # por linha vazia); o corte correto é no início de cada SOURCE.
    for bibref, chunk in _iter_records(content):
        if is_complete_output(chunk):
            done.add(bibref)
    return done


def _iter_records(content: str):
    """Fatia o arquivo único em (bibref, trecho), um por bloco SOURCE.

    O corte recua sobre os comentários que precedem o `SOURCE`: a marca de
    falha é emitida ANTES dele, e cortar exatamente no `SOURCE` a deixaria no
    trecho do registro anterior — contaminando um registro válido e liberando o
    inválido.
    """
    lines = content.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _SOURCE_LINE.match(line.rstrip("\r\n") + "\n")
        if not m:
            continue
        # Recua sobre comentários/linhas em branco imediatamente anteriores.
        begin = i
        while begin > 0:
            prev = lines[begin - 1].strip()
            if prev.startswith("#") or not prev:
                begin -= 1
            else:
                break
        starts.append((begin, m.group(1)))

    for idx, (begin, bibref) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        yield bibref, "".join(lines[begin:end])


# --- Cooldown entre batches -------------------------------------------------
# Pausa para aliviar rate limit da API entre rajadas de chamadas.

COOLDOWN_FRACTION = 0.1   # fração da duração do batch
COOLDOWN_MIN = 5.0        # piso, em segundos
COOLDOWN_MAX = 30.0       # teto, em segundos


def compute_cooldown(
    batch_elapsed: float,
    fraction: float = COOLDOWN_FRACTION,
    minimum: float = COOLDOWN_MIN,
    maximum: float = COOLDOWN_MAX,
) -> float:
    """Pausa proporcional à duração do BATCH que acabou de rodar.

    A versão anterior usava o tempo acumulado desde o início da execução, o que
    fazia a pausa saturar no teto por volta do 5º batch e permanecer lá. Numa
    campanha de 2.800 registros (112 batches) isso custava ~55 min de `sleep`,
    contra ~12 min desta fórmula — sem relação alguma com pressão de rate
    limit, que depende da taxa recente de requisições, não de há quanto tempo o
    processo está rodando. Ver Estudo_Saida_Particionada_e_Incremental §5.4.

    Args:
        batch_elapsed: Duração, em segundos, do batch recém-concluído.
        fraction: Proporção da duração usada como pausa.
        minimum: Piso da pausa.
        maximum: Teto da pausa.

    Returns:
        Segundos de pausa. Sempre em [minimum, maximum].
    """
    return min(maximum, max(minimum, batch_elapsed * fraction))


def resolve_cooldown_setting(value: str | float | None) -> Optional[float]:
    """Interpreta `--cooldown`: `auto`, um número de segundos, ou `0`.

    Returns:
        None para modo automático (proporcional ao batch); caso contrário, o
        número fixo de segundos (0 desliga a pausa).
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("auto", ""):
            return None
        try:
            seconds = float(text)
        except ValueError as exc:
            raise ValueError(
                f"Valor inválido para --cooldown: {value!r}. "
                "Use 'auto' ou um número de segundos (0 desliga)."
            ) from exc
    else:
        seconds = float(value)

    if seconds < 0:
        raise ValueError("--cooldown não pode ser negativo.")
    return seconds


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
            # Bloco PARSEÁVEL: um .syn só de comentários derrubaria o
            # load_project() do batch seguinte e abortaria a campanha.
            error_output = _build_failure_block(
                ctx, bibref, f"chamada LLM falhou para @{bibref}: {exc}"
            )
            return bibref, error_output, False

        annotation_key = f"{bibref}.syn"
        final_syn, success = await validate_and_fix_async(
            raw_syn, ctx, llm_client, annotation_key=annotation_key,
            recorder=llm_client.recorder, context=context,
        )

        # Loop degenerativo: modelos fracos re-emitem o mesmo ITEM até esgotar
        # tokens. Sintaticamente válido, logo invisível ao compilador.
        final_syn, dupes = dedupe_item_blocks(final_syn)
        if dupes:
            logger.warning(
                "%s: %d bloco(s) ITEM duplicado(s) removido(s) — possível "
                "loop degenerativo do modelo.", bibref, dupes,
            )

        # Cobertura: a validação garante SINTAXE, não que algo foi anotado.
        if success and count_item_blocks(final_syn) == 0:
            success = False
            logger.error(
                "%s: nenhum bloco ITEM gerado — o registro não produziu "
                "anotação alguma (o .syn contém apenas SOURCE).", bibref,
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
            # Marca o bloco para que `--resume` não o confunda com registro
            # pronto. O conteúdo gerado é preservado: serve de diagnóstico e
            # pode ser aproveitado numa correção manual.
            if not is_failed_output(final_syn):
                final_syn = f"{FAILURE_MARKER}\n{final_syn}"

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
    accumulated: Optional[List[str]] = None,
    split_every: Optional[int] = None,
    accumulated_by_file: Optional[Dict[str, List[str]]] = None,
    positions: Optional[List[int]] = None,
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
        accumulated: Lista viva com os blocos dos batches anteriores (modo
            arquivo único). Mutada in-place — é o que impede que cada batch
            trunque o trabalho dos anteriores.
        split_every: Registros por arquivo no modo particionado.
        accumulated_by_file: Blocos já gravados, por nome de arquivo (modo
            particionado). Mutado in-place, mesma razão de `accumulated`.
        positions: Posição GLOBAL de cada entrada deste batch no corpus. Sob
            `--resume` a lista de entradas é filtrada, então o índice local
            deixa de corresponder à posição original — e sem isto o registro
            cairia numa partição diferente da da execução anterior.

    Returns:
        (total_ok, total_fail)
    """
    if accumulated is None:
        accumulated = []
    if accumulated_by_file is None:
        accumulated_by_file = {}
    semaphore = asyncio.Semaphore(concurrent)

    total_ok = 0
    total_fail = 0
    # Indexado pela posição no batch para restaurar a ordem do .bib: as tarefas
    # completam fora de ordem, e sem isto a ordem dos blocos no arquivo varia
    # entre execuções, tornando diffs inúteis e a retomada confusa.
    results: List[Optional[Tuple[str, str]]] = [None] * len(entries)

    async def _run(position: int, entry: Dict[str, str]) -> None:
        nonlocal total_ok, total_fail
        bibref, output, success = await _process_one_abstract(
            entry["bibref"], entry["abstract"], ctx, llm_client, semaphore,
            entry_index=index_base + position, total_entries=total_entries,
        )
        results[position] = (bibref, output)
        if success:
            total_ok += 1
        else:
            total_fail += 1
        if progress_callback:
            progress_callback(bibref, success)

    # gather preserva a concorrência do semáforo; o progresso é reportado por
    # tarefa, à medida que cada uma termina.
    await asyncio.gather(*(_run(i, e) for i, e in enumerate(entries)))

    all_outputs: List[Tuple[str, str]] = [r for r in results if r is not None]

    # Gravar resultados. `safe_write_output` dá escrita atômica (tmp +
    # os.replace): uma interrupção nunca deixa arquivo truncado.
    if per_reference:
        for bibref, output in all_outputs:
            out_path = output_dir / f"{bibref}.syn"
            # overwrite=True: o arquivo é do próprio registro e a proteção
            # contra retrabalho é `--resume`, não confirmação por arquivo.
            safe_write_output(out_path, output + "\n", overwrite=True)
            logger.debug("Escrito: %s", out_path)

    elif split_every:
        # Partições: o arquivo de cada registro deriva da sua POSIÇÃO GLOBAL,
        # e o corte é sempre em fronteira de registro — nunca no meio de um
        # bloco, o que produziria .syn inválido.
        for position, item in enumerate(results):
            if item is None:
                continue
            global_position = (
                positions[position] if positions else index_base + position
            )
            filename = split_filename(global_position, split_every)
            accumulated_by_file.setdefault(filename, []).append(item[1])

        for filename in sorted(accumulated_by_file):
            path = output_dir / filename
            safe_write_output(
                path,
                "\n\n".join(accumulated_by_file[filename]) + "\n",
                overwrite=True,
            )
        logger.debug(
            "Escritas %d partição(ões) em %s",
            len(accumulated_by_file), output_dir,
        )

    else:
        # Arquivo único: acumula ENTRE batches. Antes, cada batch truncava o
        # arquivo e só o último sobrevivia — numa campanha de 2.800 restariam
        # 25 anotações. Ver Estudo_Saida_Particionada_e_Incremental §2.
        accumulated.extend(output for _, output in all_outputs)
        combined_path = output_dir / "annotations.syn"
        safe_write_output(
            combined_path, "\n\n".join(accumulated) + "\n", overwrite=True
        )
        logger.debug("Escrito: %s (%d registros)", combined_path, len(accumulated))

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
    prompt_only: bool = False,
    overwrite: bool = False,
    backup: bool = False,
    cooldown: str | float | None = None,
    resume: bool = False,
    split_every: Optional[int] = None,
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
        prompt_only: Se True, retorna o prompt montado em Markdown e não chama
            o LLM (nenhum arquivo é escrito).

    Returns:
        String com resumo da execução, ou o prompt em Markdown quando
        prompt_only=True.
    """
    # Valida a combinação de modos ANTES de qualquer trabalho: descobrir a
    # incompatibilidade depois de horas de processamento seria inaceitável.
    resolve_output_mode(per_reference, split_every)

    if prompt_only:
        from synesis_coder.prompt_dump import dump_prompt

        entries = parse_bib_entries(bib_path)
        first = entries[0]
        # Anotações desatualizadas em relação ao template não devem impedir a
        # inspeção do prompt: revisar as GUIDELINES é justamente o que se faz
        # ENQUANTO o template muda e o corpus antigo ainda não foi migrado.
        ctx = load_project(
            project_path, load_annotations=True, tolerate_annotation_errors=True
        )
        return dump_prompt(
            ctx, mode="abstract",
            bibref=first["bibref"], text=first["abstract"],
        )

    return asyncio.run(
        _process_abstract_async(
            project_path, bib_path, output_dir,
            concurrent, batch_size, per_reference, model, format, debug,
            overwrite=overwrite, backup=backup,
            cooldown_seconds=resolve_cooldown_setting(cooldown),
            resume=resume,
            split_every=split_every,
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
    overwrite: bool = False,
    backup: bool = False,
    cooldown_seconds: Optional[float] = None,
    resume: bool = False,
    split_every: Optional[int] = None,
) -> str:
    """Implementação assíncrona do processamento de abstracts.

    cooldown_seconds: None = pausa automática, proporcional à duração de cada
    batch; um número = pausa fixa (0 desliga).
    """

    # 1. Parsear .bib
    entries = parse_bib_entries(bib_path)
    total_in_corpus = len(entries)
    skipped_resume = 0

    # 2. Criar diretório de saída
    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(
            f"'{output_dir}' já existe como arquivo, mas este comando espera um "
            f"diretório de saída (os .syn são escritos dentro dele). "
            f"Aponte --output-dir para uma pasta, ex.: --output-dir annotations"
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Posição de cada entrada no corpus ORIGINAL. Sob --resume a lista é
    # filtrada, mas a posição global precisa sobreviver: é ela que decide em
    # qual partição o registro cai (modo --split-every).
    positions_all = list(range(total_in_corpus))

    # 2b. Retomada: filtrar o que já foi processado com sucesso.
    if resume:
        done = completed_bibrefs(output_dir, per_reference, split_every)
        if done:
            kept = [
                (pos, e)
                for pos, e in zip(positions_all, entries)
                if e["bibref"] not in done
            ]
            positions_all = [pos for pos, _ in kept]
            entries = [e for _, e in kept]
            skipped_resume = total_in_corpus - len(entries)
            logger.info(
                "Retomada: %d de %d registros já processados — restam %d.",
                skipped_resume, total_in_corpus, len(entries),
            )
            if not entries:
                return (
                    f"Nada a fazer: os {total_in_corpus} registros de "
                    f"'{bib_path.name}' já foram processados em {output_dir}."
                )
        else:
            logger.info("Retomada solicitada, mas não há saída anterior utilizável.")

    total = len(entries)

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

    # Carga inicial fora do loop: se o projeto não compila ANTES de começar, é
    # erro de configuração e deve abortar. O try dentro do loop protege apenas
    # contra .syn malformados surgidos DURANTE a campanha.
    ctx = load_project(project_path, load_annotations=True)

    # Blocos já gravados, acumulados entre batches (modo arquivo único).
    accumulated: List[str] = []
    # Idem para o modo particionado, indexado por nome de arquivo.
    accumulated_by_file: Dict[str, List[str]] = {}

    if split_every:
        if resume:
            # Semear cada partição com o que já existe: como no arquivo único,
            # cada gravação reescreve o arquivo inteiro.
            for path in sorted(output_dir.glob("*.syn")):
                if not is_split_file(path.name):
                    continue
                previous = path.read_text(encoding="utf-8")
                blocks = [c.rstrip("\n") for _, c in _iter_records(previous)]
                if blocks:
                    accumulated_by_file[path.name] = blocks
            if accumulated_by_file:
                logger.info(
                    "Retomada: %d partição(ões) preservada(s).",
                    len(accumulated_by_file),
                )
        else:
            existing = [
                p for p in output_dir.glob("*.syn") if is_split_file(p.name)
            ]
            if existing and not overwrite:
                raise FileExistsError(
                    f"{len(existing)} arquivo(s) de partição já existem em "
                    f"{output_dir}. Use --overwrite para substituí-los, ou "
                    "--resume para continuar de onde parou."
                )

    elif not per_reference:
        combined_path = output_dir / "annotations.syn"

        if resume and combined_path.exists():
            # Semear o acumulador com o que já está no arquivo: cada batch
            # reescreve o arquivo inteiro, então sem isto a retomada APAGARIA
            # o trabalho anterior — o oposto do que --resume promete.
            previous = combined_path.read_text(encoding="utf-8")
            accumulated.extend(
                chunk.rstrip("\n") for _, chunk in _iter_records(previous)
            )
            logger.info(
                "Retomada: %d registro(s) preservados de %s",
                len(accumulated), combined_path.name,
            )
        elif combined_path.exists() and not overwrite:
            # Proteção de sobrescrita: verificada UMA vez, antes de gastar
            # qualquer chamada de API. Dentro do loop cada batch grava com
            # overwrite=True, pois o arquivo é da própria execução corrente.
            raise FileExistsError(
                f"'{combined_path.name}' já existe em {output_dir}. "
                "Use --overwrite para substituí-lo, ou --resume para continuar "
                "de onde parou."
            )

        if backup and combined_path.exists():
            backup_path = combined_path.with_suffix(".syn.bak")
            backup_path.write_bytes(combined_path.read_bytes())
            logger.info("Backup criado: %s", backup_path)

    for batch_start in range(0, total, batch_size):
        batch_num = batch_start // batch_size + 1
        batch_entries = entries[batch_start : batch_start + batch_size]

        logger.debug(
            "Batch %d: %d entradas (%d/%d)",
            batch_num, len(batch_entries), batch_start + len(batch_entries), total,
        )

        # Recarregar projeto para incorporar .syn escritos no batch anterior.
        # Defesa em profundidade: um .syn malformado no diretório de saída (de
        # outra ferramenta, edição manual ou versão anterior do coder) não pode
        # abortar uma campanha de horas — seguimos com o ctx do batch anterior.
        try:
            ctx = load_project(project_path, load_annotations=True)
        except Exception as exc:
            logger.warning(
                "Batch %d: recarga do projeto falhou (%s) — seguindo com o "
                "contexto do batch anterior; conceitos novos podem não ser "
                "reusados neste batch.",
                batch_num, exc,
            )

        batch_started = time.monotonic()
        ok, fail = await _process_batch(
            batch_entries, ctx, llm_client, concurrent,
            output_dir, per_reference, _progress,
            index_base=batch_start, total_entries=total,
            accumulated=accumulated,
            split_every=split_every,
            accumulated_by_file=accumulated_by_file,
            positions=positions_all[batch_start : batch_start + batch_size],
        )
        batch_elapsed = time.monotonic() - batch_started
        total_ok += ok
        total_fail += fail

        # Cooldown entre batches: proporcional à duração DESTE batch, não ao
        # tempo total decorrido (que saturava o teto e custava ~55 min numa
        # campanha de 2.800 — ver compute_cooldown).
        if batch_start + batch_size < total:
            cooldown = (
                compute_cooldown(batch_elapsed)
                if cooldown_seconds is None
                else cooldown_seconds
            )
            if cooldown > 0:
                logger.debug(
                    "Batch %d levou %.1fs — cooldown de %.1fs antes do próximo",
                    batch_num, batch_elapsed, cooldown,
                )
                await asyncio.sleep(cooldown)

    elapsed = time.monotonic() - start_time
    rate = (total_ok / total * 100) if total > 0 else 0

    # Degradação silenciosa: registros que caíram para texto livre contam como
    # OK, mas rodaram sem as restrições do schema. Avisar antes do resumo.
    warn_schema_fallbacks(llm_client)

    _sep = "-" * 50
    resume_line = (
        f"  Retomados : {skipped_resume} já processados (pulados)\n"
        if skipped_resume
        else ""
    )
    summary = (
        f"\n{_sep}\n"
        f"  Total     : {total} referências\n"
        f"{resume_line}"
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
