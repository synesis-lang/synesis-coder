"""Interface de linha de comando do synesis-coder."""

from __future__ import annotations

import io
import logging
import os
import re
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

import click

_log = logging.getLogger(__name__)

# Custom log levels for structured CLI output labels
logging.addLevelName(21, "OK")
logging.addLevelName(22, "DEST")

from dotenv import find_dotenv as _find_dotenv
from dotenv import load_dotenv as _load_dotenv

# Precedência: variável de ambiente > .env do projeto > arquivo de padrões
# opcional (SYNESIS_CODER_ENV_FILE).
# O .env do projeto é procurado a partir da pasta onde o comando roda
# (usecwd=True); sem isso, o python-dotenv sobe a partir do PACOTE e o .env do
# pesquisador nunca era lido. É ali que o projeto declara, por exemplo,
# SYNESIS_CODER_LANGUAGE (Social Acceptance em inglês, face85 em português).
# Antes, o .env da pasta acima do pacote também era lido, sempre: o resultado
# dependia de ONDE o coder estava instalado (uma instalação editável puxava o
# .env do repositório para todo projeto). Agora um arquivo de padrões
# compartilhado só entra se declarado explicitamente (D-6, 2026-11-20).
_project_env = _find_dotenv(usecwd=True)
if _project_env:
    _load_dotenv(_project_env, override=False)
_defaults_env = os.environ.get("SYNESIS_CODER_ENV_FILE", "").strip()
if _defaults_env:
    _load_dotenv(_defaults_env, override=False)

if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "buffer") and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")


def _version_string() -> str:
    try:
        return _pkg_version("synesis-coder")
    except Exception:
        return "0.0.1"


def _synesis_version_string() -> str:
    try:
        return _pkg_version("synesis")
    except Exception:
        return "?"


def _default_model() -> str:
    return os.environ.get("SYNESIS_CODER_MODEL", "claude-opus-4-6")


def get_concurrent(default: int, flag: int | None = None) -> int:
    """Chamadas simultâneas: flag da CLI → `SYNESIS_CODER_CONCURRENT` → default.

    O valor adequado depende de QUE SERVIDOR atende — a mesma coisa que o
    `.env` já descreve em BACKEND/API_URL/MODEL. Contra uma API que responde em
    paralelo, 5 é bom; contra um servidor local de uma GPU as chamadas apenas
    disputam a mesma memória. Sem esta variável o pesquisador precisa lembrar de
    `--concurrent 1` em cada comando, e nada o avisa se esquecer.

    Args:
        default: Valor do comando quando nada foi configurado — preserva a
            divergência histórica entre modos (5 em abstract, 3 em document).
        flag: Valor de `--concurrent`; None quando a flag não foi informada.
            Tem precedência sobre o ambiente: o que se digita agora descreve
            esta execução, o `.env` descreve o ambiente habitual.
    """
    if flag is not None:
        return flag

    raw = os.environ.get("SYNESIS_CODER_CONCURRENT", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _log.warning(
            "SYNESIS_CODER_CONCURRENT=%r is not an integer — using %d.", raw, default
        )
        return default
    if value < 1:
        _log.warning(
            "SYNESIS_CODER_CONCURRENT=%d is invalid (minimum 1) — using %d.",
            value, default,
        )
        return default
    return value


def _validate_phase_env(phase_name: str) -> str:
    # O .env do projeto já foi carregado na importação deste módulo. Um
    # `load_dotenv()` aqui procuraria a partir da pasta do pacote (D-6).
    phase_upper = phase_name.upper()
    phase_model_var = f"SYNESIS_CODER_{phase_upper}_MODEL"

    model = os.environ.get(phase_model_var)
    if not model:
        model = os.environ.get("SYNESIS_CODER_MODEL", "claude-opus-4-6")

    backend = os.environ.get("SYNESIS_CODER_BACKEND", "anthropic").lower()
    if backend == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError(
            f"ANTHROPIC_API_KEY not found.\n"
            f"Set it in your .env:\n\n"
            f"  ANTHROPIC_API_KEY=sk-ant-...\n\n"
            f"To use a specific model in the '{phase_name}' stage, also add:\n\n"
            f"  {phase_model_var}=claude-sonnet-4-6"
        )
    return model


# ---------------------------------------------------------------------------
# Helpers de cor (só aplica se stdout for TTY)
# ---------------------------------------------------------------------------

def _tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, **kwargs) -> str:
    return click.style(text, **kwargs) if _tty() else text


def _configure_logging(verbose: int, quiet: int, print_header: bool = False) -> None:
    """Set root log level: -q → WARNING/ERROR, default → INFO, -v → DEBUG.

    Note: --format controls output *style* (plain/verbose token usage).
    -v/-q controls the Python logging level independently.
    """
    if quiet >= 2:
        level = logging.ERROR
    elif quiet == 1:
        level = logging.WARNING
    elif verbose >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO
    if verbose >= 1:
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    else:
        class _BracketFormatter(logging.Formatter):
            _LABELS = {"WARNING": "WARN", "CRITICAL": "CRIT"}

            def format(self, record: logging.LogRecord) -> str:
                label = self._LABELS.get(record.levelname, record.levelname)
                return f"[{label}] {record.getMessage()}"

        handler = logging.StreamHandler()
        handler.setFormatter(_BracketFormatter())
    # Replace any handlers basicConfig may have installed previously
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _log.setLevel(level)

    # Silenciar loggers ruidosos de bibliotecas de terceiros (httpx emite uma
    # linha "HTTP Request: POST ..." por chamada LLM). Só aparecem com -v.
    _noise_level = logging.DEBUG if verbose >= 1 else logging.WARNING
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(_noise_level)

    if print_header:
        from synesis_coder.runtime_info import print_product_header
        print_product_header(quiet=quiet)


def _emit_prompt(markdown: str, project: Path, mode: str, output: Path | None) -> None:
    """Grava o dump de prompt em disco e reporta o destino.

    Escrever em arquivo — em vez de stdout — é o padrão porque o artefato é
    para leitura e revisão: o prompt do face85 passa de 400 linhas, tamanho em
    que rolar o terminal não serve. `--output` escolhe o caminho; sem ele o
    nome deriva do projeto e do modo, ao lado do `.synp`.

    O destino vai a stderr por escrita direta, não pelo logger: `-q` eleva o
    nível para WARNING e engoliria um `log(22)` (DEST), deixando o usuário sem
    saber onde o arquivo foi parar — e `-q` é justamente o que se usa aqui para
    calar o banner. Sendo stderr, não interfere se stdout for redirecionado.
    """
    path = Path(output) if output else project.parent / f"{project.stem}_{mode}_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    sys.stderr.write(f"{path}\n")


# ---------------------------------------------------------------------------
# Help principal (Progressive Disclosure)
# ---------------------------------------------------------------------------

def _build_main_help() -> str:
    sc_ver = _version_string()
    syn_ver = _synesis_version_string()
    model = _default_model()

    title = (
        _c("SYNESIS CODER", fg="green", bold=True) + f" (v{sc_ver})"
        + "  " + _c("|", fg="bright_black") + "  "
        + _c("Core", fg="white", bold=True) + f" (v{syn_ver})"
    )
    credits = (
        "Copyright (c) 2011-2026 Christian Maciel de Britto\n\n"
        "https://github.com/synesis-lang/synesis-coder\n\n"
        "Inference engine for generating valid annotations in the Synesis ecosystem.\n"
        "The template defines all fields, relations, and constraints — nothing is hardcoded.\n\n"
        "Licensed under AGPL-3.0-only WITH the Synesis Data-Output Exception\n"
        "(annotations and exports you produce are yours — see LICENSE.exception)."
    )
    usage = (
        _c("Usage:", fg="yellow", bold=True)
        + " synesis-coder [OPTIONS] COMMAND [ARGUMENTS]..."
    )

    groups = [
        ("Annotating a corpus", [
            ("abstract",    "Annotates a .bib corpus in batch — SOURCE + ITEMs per reference"),
            ("document",    "Annotates a long document (.txt/.md), chunked automatically"),
            ("dataset",     "Annotates a TOML dataset — SOURCE + ITEMs per record"),
            ("item",        "Annotates a single excerpt, given its bibref"),
        ]),
        ("Building the knowledge model", [
            ("ontology",    "Generates model entries (.syno) from the annotated corpus"),
            ("phase",       "Runs a PHASE: fills one declared group of template fields\n"
                            "(ontology phases: descriptive, taxonomy, synonyms)"),
            ("review",      "Samples a phase's applied proposals for judgement; scores the sample"),
            ("consolidate", "Merges a duplicate model entry into the one that remains (ROLE USE); reversible"),
            ("suggest",     "Suggests codes already used in the project for an excerpt"),
        ]),
        ("Reviewing annotations (ACT pipeline)", [
            ("critique",    "[1] Reviews a .syn and emits .synr with # REVISION blocks"),
            ("normalize",   "[2] Canonicalizes codes across the whole corpus"),
            ("incorporate", "[3] Applies the .synr revisions and emits the final .syn"),
            ("refine",      "[R] Re-extracts flagged ITEMs using the review feedback"),
        ]),
        ("Checking without an LLM", [
            ("anchor",      "Checks that each ITEM excerpt occurs in its source (free, offline)"),
        ]),
        ("Training data", [
            ("finetune",    "Enriches an Alpaca dataset via LLM for fine-tuning"),
        ]),
    ]

    opt_rows = [
        ("--model TEXT",   f"Overrides the default model (Current: {_c(model, fg='cyan')})"),
        ("--format TEXT",  "Output format (Ex: 'verbose' to display token usage)"),
        ("-v, --verbose",  "Increase log verbosity (DEBUG). Repeatable."),
        ("-q, --quiet",    "Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable."),
        ("--require-guidelines", "Fail when a template field has no GUIDELINES"),
        ("--version",      "Show version and exit"),
        ("--help",         "Show this message and exit"),
    ]

    cmd_names_len = max(len(name) for _, rows in groups for name, _ in rows)
    opt_names_len = max(len(name) for name, _ in opt_rows)
    col = max(cmd_names_len, opt_names_len) + 2

    options = _c("Global Options:", fg="yellow", bold=True) + "\n" + "\n".join(
        f"  {_c(name.ljust(col), fg='cyan')}  {desc_}"
        for name, desc_ in opt_rows
    )

    def _render_group(label: str, rows: list[tuple[str, str]]) -> str:
        lines = [_c("  " + label, fg="yellow", bold=True)]
        for name, desc_ in rows:
            first, *more = desc_.split("\n")
            lines.append(f"    {_c(name.ljust(col), fg='green', bold=True)}  {first}")
            # Continuacao da descricao: alinhada sob a primeira linha.
            lines.extend(f"    {' ' * col}  {extra}" for extra in more)
        return "\n".join(lines)

    commands = _c("Commands:", fg="yellow", bold=True) + "\n\n" + "\n\n".join(
        _render_group(label, rows) for label, rows in groups
    )

    notes = (
        _c("Common flags:", fg="yellow", bold=True) + "\n"
        + _c(
            "  --prompt-only    Writes the assembled prompt (template GUIDELINES included)\n"
            "                   to Markdown and exits. No LLM call, no tokens spent.\n"
            "  --debug          Writes a Markdown audit log of the whole LLM pipeline.\n"
            "\n"
            "Long runs (abstract):\n"
            "  --resume         Skips references already annotated in --output-dir.\n"
            "  --split-every N  Writes N references per file instead of one big file.\n"
            "  --per-reference  Writes one .syn per reference.\n"
            "  --cooldown       Pause between batches: 'auto', seconds, or 0 to disable.",
            fg="bright_black",
        )
    )

    hint = _c(
        "Run 'synesis-coder COMMAND --help' for options and examples of each mode.",
        fg="bright_black",
    )

    return "\n\n".join([title, credits, usage, options, commands, notes, hint]) + "\n"


# ---------------------------------------------------------------------------
# Helpers de epilog (exemplos por subcomando)
# ---------------------------------------------------------------------------

def _ex(*lines: str) -> str:
    import re
    out = [_c("Examples:", fg="yellow", bold=True)]
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("#"):
            out.append(indent + _c(stripped, fg="bright_black"))
        else:
            tokens = re.split(r"(\s+)", stripped)
            result = []
            for tok in tokens:
                if tok == "synesis-coder":
                    result.append(_c(tok, fg="green", bold=True))
                elif re.match(r"^--[\w-]+=?", tok):
                    result.append(_c(tok, fg="cyan"))
                elif tok in ("item", "abstract", "document", "dataset", "ontology", "phase", "review", "consolidate",
                             "suggest", "finetune", "critique", "normalize",
                             "incorporate", "refine"):
                    result.append(_c(tok, fg="green"))
                else:
                    result.append(tok)
            out.append(indent + "".join(result))
    return "\n".join(out)


_EPILOG_ITEM = _ex(
    "",
    "  # Basic annotation:",
    '  synesis-coder item --project project.synp --bibref smith2024 --text "Community trust is key."',
    "",
    "  # Append output to a .syn file:",
    '  synesis-coder item --project p.synp --bibref smith2024 --text "..." >> annotations/smith2024.syn',
    "",
    "  # Extended thinking (maximum causal precision):",
    '  synesis-coder item --project p.synp --bibref smith2024 --text "..." --model claude-opus-4-7 --thinking-budget 8000',
    "",
    "  # Output in Portuguese:",
    '  synesis-coder item --project p.synp --bibref smith2024 --text "..." --language pt-BR',
    "",
    "  # Inspect the assembled prompt without calling the LLM (writes a .md):",
    '  synesis-coder item --project p.synp --bibref smith2024 --text "..." --prompt-only',
)

_EPILOG_ABSTRACT = _ex(
    "",
    "  # Process all abstracts from a .bib corpus (one annotations.syn):",
    "  synesis-coder abstract --project project.synp --input refs.bib --output-dir annotations/",
    "",
    "  # Output layout — pick ONE:",
    "  #   one .syn per reference (easiest to review and re-run individually)",
    "  synesis-coder abstract --project p.synp --input refs.bib --output-dir out/ --per-reference",
    "  #   N references per file",
    "  synesis-coder abstract --project p.synp --input refs.bib --output-dir out/ --split-every 100",
    "",
    "  # Long campaigns: resume after an interruption (skips what is already done):",
    "  synesis-coder abstract --project p.synp --input refs.bib --output-dir out/ --per-reference --resume",
    "",
    "  # Control concurrency, batch size and the pause between batches:",
    "  synesis-coder abstract --project p.synp --input refs.bib --output-dir out/ --concurrent 3 --batch-size 10 --cooldown auto",
    "",
    "  # Inspect the assembled prompt without calling the LLM (review the template GUIDELINES):",
    "  synesis-coder abstract --project p.synp --input refs.bib --prompt-only",
)

_EPILOG_DOCUMENT = _ex(
    "",
    "  # Annotate an interview or report (.txt/.md):",
    "  synesis-coder document --project project.synp --bibref interview_01 --input E01.txt --output annotations/E01.syn",
    "",
    "  # Adjust chunking for dense documents:",
    "  synesis-coder document --project p.synp --bibref rep2024 --input rep.md --output rep.syn --chunk-size 8000 --overlap 1600",
    "",
    "  # Control concurrency:",
    "  synesis-coder document --project p.synp --bibref E02 --input E02.txt --output E02.syn --concurrent 2",
    "",
    "  # Inspect the assembled prompt (first chunk) without calling the LLM:",
    "  synesis-coder document --project p.synp --bibref E01 --input E01.txt --prompt-only",
)

_EPILOG_DATASET = _ex(
    "",
    "  # Process the whole TOML corpus declared by INCLUDE DATASET in the .synp",
    "  # (one .syn per record):",
    "  synesis-coder dataset --project lattes.synp --output-dir annotations/",
    "",
    "  # Control concurrency:",
    "  synesis-coder dataset --project lattes.synp --output-dir annotations/ --concurrent 3",
    "",
    "  # Test with a single TOML record (overrides the .synp glob for this run only):",
    "  synesis-coder dataset --project lattes.synp --output-dir _test/ "
    "--dataset curriculos/01_record.toml",
)

_EPILOG_ONTOLOGY = _ex(
    "",
    "  # Generate definitions for all codes in the corpus:",
    "  synesis-coder ontology --project project.synp --output project.syno",
    "",
    "  # Incremental update (skips already-defined codes):",
    "  synesis-coder ontology --project p.synp --output p.syno --update",
    "",
    "  # Control concurrency with a faster model:",
    "  synesis-coder ontology --project p.synp --output p.syno --concurrent 3 --model claude-sonnet-4-6",
    "",
    "  # Inspect the assembled prompt without calling the LLM:",
    "  synesis-coder ontology --project p.synp --prompt-only",
    "",
    "  # Custom destination for the prompt dump:",
    "  synesis-coder ontology --project p.synp --prompt-only --output prompts/ontology.md",
)

_EPILOG_SUGGEST = _ex(
    "",
    "  # Suggest codes for an excerpt:",
    '  synesis-coder suggest --project project.synp --text "Local ownership reduces opposition to CCS technology."',
    "",
    "  # Verbose output with metadata:",
    '  synesis-coder suggest --project p.synp --text "..." --format verbose',
    "",
    "  # Use a lightweight local model (via Ollama):",
    '  synesis-coder suggest --project p.synp --text "..." --model gemma4:e2b',
)

_EPILOG_FINETUNE = _ex(
    "",
    "  # Full pipeline: compile project and enrich (Layers 1+2):",
    "  synesis-coder finetune --project project.synp --output dataset.jsonl",
    "",
    "  # Enrich a pre-generated JSONL only (Layer 2):",
    "  synesis-coder finetune --input raw.jsonl --output enriched.jsonl",
    "",
    "  # Multiple enrichment types:",
    "  synesis-coder finetune --input raw.jsonl --output rich.jsonl --enrich vary --enrich didactic --enrich counterfactual",
)

_EPILOG_PHASE = _ex(
    "",
    "  # List the project's phases, their fields, destination and connection:",
    "  synesis-coder phase project.synp --list",
    "",
    "  # Run a descriptive phase (fills only empty fields of its FIELDS):",
    "  synesis-coder phase project.synp definicoes",
    "",
    "  # Show the proposed diff without writing:",
    "  synesis-coder phase project.synp definicoes --dry-run",
    "",
    "  # Write to another local .syno of the project (pending concepts there):",
    "  synesis-coder phase project.synp definicoes --output outro.syno",
    "",
    "  # Per-phase model and connection (.env), falling back to the global ones:",
    "  SYNESIS_CODER_PHASE_DEFINICOES_MODEL=claude-sonnet-5-5",
    "  SYNESIS_CODER_PHASE_DEFINICOES_BACKEND=anthropic",
    "",
    "  # Taxonomy phase (HIERARCHY fields), work divided by a field, one generation of new parents:",
    "  synesis-coder phase project.synp taxonomia --group-by aspecto --new-generations 1",
    "",
    "  # Evaluate by sampling (stratified, weights by inclusion probability):",
    "  synesis-coder review project.synp taxonomy --unit edge --sample 100 --stratify depth",
    "",
)

_EPILOG_CRITIQUE = _ex(
    "",
    "  # Review annotations and generate .synr with # REVISION blocks:",
    "  synesis-coder critique --project project.synp annotations/smith2024.syn",
    "",
    "  # Adjust suspicion threshold (default: 0.20):",
    "  synesis-coder critique --project p.synp --threshold 0.40 annotations/smith2024.syn",
    "",
    "  # Custom output path:",
    "  synesis-coder critique --project p.synp --output revisions/smith2024.synr annotations/smith2024.syn",
)

_EPILOG_NORMALIZE = _ex(
    "",
    "  # Normalize a single .synr file:",
    "  synesis-coder normalize --project project.synp revisions/smith2024.synr",
    "",
    "  # Normalize a full corpus (multiple .synr files):",
    "  synesis-coder normalize --project p.synp --output-dir revisions/ revisions/*.synr",
    "",
    "  # Save code inventory and adjust minimum confidence:",
    "  synesis-coder normalize --project p.synp --inventory inv.txt --confidence 0.75 revisions/*.synr",
)

_EPILOG_INCORPORATE = _ex(
    "",
    "  # Apply revisions and generate final .syn (no LLM):",
    "  synesis-coder incorporate revisions/smith2024.synr",
    "",
    "  # Custom output path:",
    "  synesis-coder incorporate --output annotations/smith2024_final.syn revisions/smith2024.synr",
    "",
    "  # Full ACT pipeline — Phase 2 -> 3 -> 4:",
    "  synesis-coder critique    --project p.synp annotations/corpus.syn",
    "  synesis-coder normalize   --project p.synp revisions/corpus.synr",
    "  synesis-coder incorporate revisions/corpus.synr",
)

_EPILOG_REFINE = _ex(
    "",
    "  # Re-extract flagged ITEMs with critique feedback (opt-in, LLM):",
    "  synesis-coder refine --project project.synp annotations/smith2024.syn",
    "",
    "  # Distinct critic and generator models (epistemic independence):",
    "  synesis-coder refine --project p.synp --critique-model claude-sonnet-4-6 "
    "--refine-model claude-opus-4-6 annotations/smith2024.syn",
    "",
    "  # Cap iterations and adjust the suspicion threshold:",
    "  synesis-coder refine --project p.synp --max-iter 3 --threshold 0.30 annotations/smith2024.syn",
    "",
    "  # Extended thinking on the re-extraction (co-dependent fields):",
    "  synesis-coder refine --project p.synp --thinking-budget 8000 annotations/smith2024.syn",
)


# ---------------------------------------------------------------------------
# Grupo principal com help customizado
# ---------------------------------------------------------------------------

class _SynesisCommand(click.Command):
    """Command com epilog pré-renderizado (sem refluxo pelo formatter do Click)."""

    def format_epilog(self, ctx, formatter):
        if self.epilog:
            formatter.write("\n")
            for line in self.epilog.splitlines():
                formatter.write(line + "\n")


class _SynesisGroup(click.Group):
    command_class = _SynesisCommand

    def format_help(self, ctx, formatter):
        pass  # não usado — get_help sobrescreve

    def get_help(self, ctx):
        out = _build_main_help()
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
            raise SystemExit(0)
        return out


@click.group(cls=_SynesisGroup, invoke_without_command=True)
@click.version_option(version=_version_string(), prog_name="synesis-coder")
@click.option("-v", "--verbose", count=True, default=0,
              help="Increase log verbosity (-v for DEBUG). Repeatable.")
@click.option("-q", "--quiet", count=True, default=0,
              help="Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable.")
@click.option("--require-guidelines", "require_guidelines", is_flag=True, default=False,
              help="Fail when a template field has no GUIDELINES "
                   "(same as SYNESIS_CODER_REQUIRE_GUIDELINES=1).")
@click.pass_context
def main(ctx: click.Context, verbose: int, quiet: int, require_guidelines: bool) -> None:
    if require_guidelines:
        from synesis_coder.template_lint import REQUIRE_GUIDELINES_ENV

        os.environ[REQUIRE_GUIDELINES_ENV] = "1"
    _configure_logging(verbose, quiet, print_header=ctx.invoked_subcommand is not None)
    if ctx.invoked_subcommand is None:
        out = _build_main_help()
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
        else:
            click.echo(out)


# ---------------------------------------------------------------------------
# Subcomandos
# ---------------------------------------------------------------------------

@main.command(epilog=_EPILOG_ITEM)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file.")
@click.option("--bibref", required=True, help="Bibliographic reference key (e.g. smith2024).")
@click.option("--text", required=True, help="Text excerpt to be annotated.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True,
              help="plain (Synesis only) or verbose (with log and token usage).")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--thinking-budget", "thinking_budget", default=None, type=int,
              help="Internal reasoning tokens (extended thinking). Ex: 8000.")
@click.option("--language", default=None, help="Output language for free-text fields (e.g. pt-BR, en).")
@click.option("--max-tokens", "max_tokens", default=None, type=int,
              help="Maximum output tokens. Overrides SYNESIS_CODER_MAX_TOKENS.")
@click.option("--temperature", default=None, type=float,
              help="Model temperature (0 = deterministic).")
@click.option("--prompt-only", "prompt_only", is_flag=True, default=False,
              help="Write the assembled prompt as Markdown and exit. No LLM call.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Destination of the --prompt-only Markdown file. "
                   "Default: <project>_item_prompt.md next to the .synp.")
def item(project, bibref, text, output_format, model, thinking_budget,
         language, max_tokens, temperature, prompt_only, output_path):
    """Generate a Synesis ITEM block from a text excerpt and bibliographic reference."""
    from synesis_coder.modes.item_mode import process_item

    if thinking_budget is not None:
        os.environ["SYNESIS_CODER_THINKING_BUDGET"] = str(thinking_budget)
    if language:
        os.environ["SYNESIS_CODER_LANGUAGE"] = language
    if max_tokens is not None:
        os.environ["SYNESIS_CODER_MAX_TOKENS"] = str(max_tokens)
    if temperature is not None:
        os.environ["SYNESIS_CODER_TEMPERATURE"] = str(temperature)

    try:
        result = process_item(project_path=project, bibref=bibref, text=text,
                              format=output_format, model=model,
                              prompt_only=prompt_only)
        if prompt_only:
            _emit_prompt(result, project, "item", output_path)
        else:
            click.echo(result)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_ABSTRACT)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file.")
@click.option("--input", "bib_path", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the .bib file containing abstracts.")
@click.option("--output-dir", "--output", "output_dir", default=None, type=click.Path(path_type=Path),
              help="Output directory for the generated .syn files (a folder, not a file path). "
                   "Default: current directory.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--batch-size", default=25, show_default=True,
              help="Batch size (project reloaded between batches).")
@click.option("--per-reference", is_flag=True, default=False,
              help="Generate one .syn file per reference (default: single file).")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--thinking-budget", "thinking_budget", default=None, type=int,
              help="Internal reasoning tokens (extended thinking). Ex: 8000.")
@click.option("--language", default=None, help="Output language for free-text fields (e.g. pt-BR, en).")
@click.option("--max-tokens", "max_tokens", default=None, type=int,
              help="Maximum output tokens. Overrides SYNESIS_CODER_MAX_TOKENS.")
@click.option("--temperature", default=None, type=float,
              help="Model temperature (0 = deterministic).")
@click.option("--debug", is_flag=True, default=False,
              help="Write a human-readable Markdown audit log of the LLM pipeline "
                   "(<project>_abstract_debug.md) in the output directory. "
                   "Overwrites if it exists.")
@click.option("--prompt-only", "prompt_only", is_flag=True, default=False,
              help="Write the assembled prompt as Markdown and exit. No LLM call. "
                   "Destination: <project>_abstract_prompt.md, or --output-dir if given.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Replace an existing annotations.syn without asking. "
                   "Checked once, before any API call.")
@click.option("--backup", is_flag=True, default=False,
              help="Copy an existing annotations.syn to .syn.bak before writing.")
@click.option("--cooldown", default="auto", show_default=True,
              help="Pause between batches: 'auto' (10% of each batch's duration, "
                   "5-30s), a fixed number of seconds, or 0 to disable.")
@click.option("--resume", is_flag=True, default=False,
              help="Skip references already processed successfully in --output-dir. "
                   "State comes from the .syn files themselves; failed records are "
                   "reprocessed.")
@click.option("--split-every", "split_every", default=None, type=int,
              help="Write N references per file (annotations_0001.syn, ...) instead "
                   "of one combined file. Cuts only at record boundaries. "
                   "Mutually exclusive with --per-reference.")
def abstract(project, bib_path, output_dir, concurrent, batch_size, per_reference,
             output_format, model, thinking_budget, language, max_tokens, temperature,
             debug, prompt_only, overwrite, backup, cooldown, resume, split_every):
    """Process a .bib corpus in batch, generating Synesis annotations (SOURCE + ITEMs)."""
    concurrent = get_concurrent(5, concurrent)
    from synesis_coder.modes.abstract_mode import process_abstract

    if output_dir is None and not prompt_only:
        # Diretório atual como padrão: é onde o pesquisador está e onde ele
        # espera encontrar o resultado. Exigir a flag interrompia o comando
        # mais curto — o da primeira tentativa de quem experimenta o modo.
        output_dir = Path.cwd()

    if thinking_budget is not None:
        os.environ["SYNESIS_CODER_THINKING_BUDGET"] = str(thinking_budget)
    if language:
        os.environ["SYNESIS_CODER_LANGUAGE"] = language
    if max_tokens is not None:
        os.environ["SYNESIS_CODER_MAX_TOKENS"] = str(max_tokens)
    if temperature is not None:
        os.environ["SYNESIS_CODER_TEMPERATURE"] = str(temperature)

    try:
        result = process_abstract(project_path=project, bib_path=bib_path,
                                  output_dir=output_dir, concurrent=concurrent,
                                  batch_size=batch_size, per_reference=per_reference,
                                  model=model, format=output_format, debug=debug,
                                  prompt_only=prompt_only,
                                  overwrite=overwrite, backup=backup,
                                  cooldown=cooldown, resume=resume,
                                  split_every=split_every)
        if prompt_only:
            # --output-dir é uma PASTA neste modo; o dump é um arquivo dentro dela.
            dest = (output_dir / f"{project.stem}_abstract_prompt.md") if output_dir else None
            _emit_prompt(result, project, "abstract", dest)
        else:
            click.echo(result)
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_DATASET)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file (declares INCLUDE DATASET).")
@click.option("--output-dir", "--output", "output_dir", default=None, type=click.Path(path_type=Path),
              help="Output directory for the generated .syn files (one per record). "
                   "Default: current directory.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--single-file", "single_file", is_flag=True, default=False,
              help="Write a single dataset.syn instead of one .syn per record.")
@click.option("--dataset", "dataset_glob", default=None,
              help="Overrides the INCLUDE DATASET glob from the .synp for this run only "
                   "(e.g. a single .toml file for a quick test) — the project file is not modified.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--language", default=None, help="Output language for free-text fields (e.g. pt-BR, en).")
@click.option("--temperature", default=None, type=float,
              help="Model temperature (0 = deterministic).")
def dataset(project, output_dir, concurrent, single_file, dataset_glob, model, language, temperature):
    """Process a TOML dataset corpus (INCLUDE DATASET), generating SOURCE + ITEMs per record.

    The dataset path comes from the .synp `INCLUDE DATASET "<glob>.toml"`; fields
    declared `ON DATASET` are resolved deterministically by the compiler, and the
    interpretive ITEM fields are generated by the LLM from the TOML context.
    """
    concurrent = get_concurrent(5, concurrent)
    from synesis_coder.modes.dataset_mode import process_dataset

    if output_dir is None:
        output_dir = Path.cwd()

    if language:
        os.environ["SYNESIS_CODER_LANGUAGE"] = language
    if temperature is not None:
        os.environ["SYNESIS_CODER_TEMPERATURE"] = str(temperature)

    try:
        click.echo(process_dataset(
            project_path=project, output_dir=output_dir, concurrent=concurrent,
            per_record=not single_file, model=model, dataset_glob=dataset_glob,
        ))
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_DOCUMENT)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file.")
@click.option("--bibref", required=True,
              help="Bibliographic reference key for this document (e.g. interview_01).")
@click.option("--input", "input_path", required=True,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .txt or .md file to be annotated.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output path for the generated .syn file. Required unless --prompt-only.")
@click.option("--chunk-size", default=12000, show_default=True,
              help="Maximum chunk size in characters.")
@click.option("--overlap", default=2400, show_default=True,
              help="Overlap between consecutive chunks in characters.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--thinking-budget", "thinking_budget", default=None, type=int,
              help="Internal reasoning tokens (extended thinking). Ex: 8000.")
@click.option("--language", default=None, help="Output language for free-text fields (e.g. pt-BR, en).")
@click.option("--max-tokens", "max_tokens", default=None, type=int,
              help="Maximum output tokens. Overrides SYNESIS_CODER_MAX_TOKENS.")
@click.option("--temperature", default=None, type=float,
              help="Model temperature (0 = deterministic).")
@click.option("--debug", is_flag=True, default=False,
              help="Write a human-readable Markdown audit log of the LLM pipeline "
                   "(<project>_<bibref>_debug.md) next to the output. Overwrites if it exists.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite the output file without confirmation if it already exists.")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the existing output before overwriting.")
@click.option("--prompt-only", "prompt_only", is_flag=True, default=False,
              help="Write the assembled prompt as Markdown and exit. No LLM call. "
                   "Destination: --output, or <project>_document_prompt.md.")
def document(project, bibref, input_path, output_path, chunk_size, overlap,
             concurrent, output_format, model, thinking_budget, language,
             max_tokens, temperature, debug, overwrite, backup, prompt_only):
    """Process a long document (.txt/.md) with chunking, generating a .syn annotation file."""
    concurrent = get_concurrent(3, concurrent)
    from synesis_coder.modes.document_mode import process_document

    if output_path is None and not prompt_only:
        raise click.UsageError("Missing option '--output' (required unless --prompt-only).")

    if thinking_budget is not None:
        os.environ["SYNESIS_CODER_THINKING_BUDGET"] = str(thinking_budget)
    if language:
        os.environ["SYNESIS_CODER_LANGUAGE"] = language
    if max_tokens is not None:
        os.environ["SYNESIS_CODER_MAX_TOKENS"] = str(max_tokens)
    if temperature is not None:
        os.environ["SYNESIS_CODER_TEMPERATURE"] = str(temperature)

    try:
        result = process_document(project_path=project, bibref=bibref,
                                  input_path=input_path, output_path=output_path,
                                  chunk_size=chunk_size, overlap=overlap,
                                  concurrent=concurrent, model=model,
                                  format=output_format, debug=debug,
                                  overwrite=overwrite, backup=backup,
                                  prompt_only=prompt_only)
        if prompt_only:
            _emit_prompt(result, project, "document", output_path)
        else:
            click.echo(result)
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_ONTOLOGY)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output path for the generated .syno file. Required unless --prompt-only.")
@click.option("--update", is_flag=True, default=False,
              help="Only generate entries for codes not yet defined in the .syno.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--thinking-budget", "thinking_budget", default=None, type=int,
              help="Internal reasoning tokens (extended thinking). Ex: 16000.")
@click.option("--language", default=None, help="Output language for free-text fields (e.g. pt-BR, en).")
@click.option("--max-tokens", "max_tokens", default=None, type=int,
              help="Maximum output tokens. Overrides SYNESIS_CODER_MAX_TOKENS.")
@click.option("--temperature", default=None, type=float,
              help="Model temperature (0 = deterministic).")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite the output file without confirmation if it already exists.")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the existing output before overwriting.")
@click.option("--prompt-only", "prompt_only", is_flag=True, default=False,
              help="Write the assembled prompt as Markdown and exit. No LLM call. "
                   "Destination: --output, or <project>_ontology_prompt.md.")
def ontology(project, output_path, update, concurrent, output_format, model,
             thinking_budget, language, max_tokens, temperature, overwrite, backup,
             prompt_only):
    """Generate model entries (ONTOLOGY blocks, .syno) from the annotated corpus."""
    concurrent = get_concurrent(5, concurrent)
    from synesis_coder.modes.ontology_mode import process_ontology

    if output_path is None and not prompt_only:
        raise click.UsageError("Missing option '--output' (required unless --prompt-only).")

    if thinking_budget is not None:
        os.environ["SYNESIS_CODER_THINKING_BUDGET"] = str(thinking_budget)
    if language:
        os.environ["SYNESIS_CODER_LANGUAGE"] = language
    if max_tokens is not None:
        os.environ["SYNESIS_CODER_MAX_TOKENS"] = str(max_tokens)
    if temperature is not None:
        os.environ["SYNESIS_CODER_TEMPERATURE"] = str(temperature)

    try:
        result = process_ontology(project_path=project, output_path=output_path,
                                  update=update, concurrent=concurrent,
                                  model=model, format=output_format,
                                  overwrite=overwrite, backup=backup,
                                  prompt_only=prompt_only)
        if prompt_only:
            _emit_prompt(result, project, "ontology", output_path)
        else:
            click.echo(result)
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_PHASE)
@click.argument("project", type=click.Path(exists=True, path_type=Path))
@click.argument("phase_name", required=False)
@click.option("--list", "list_only", is_flag=True, default=False,
              help="List the PHASE blocks of the project and exit.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Destination .syno for this run (overrides the phase OUTPUT).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Generate and validate, show the diff, write nothing.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Rewrite the phase fields even when already filled.")
@click.option("--retry-rejected", "retry_rejected", is_flag=True, default=False,
              help="Retry proposals rejected before with the same inputs.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--model", default=None,
              help="LLM model ID (overrides SYNESIS_CODER_PHASE_<NAME>_MODEL).")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the destination before writing.")
@click.option("--group-by", "group_by", default=None,
              help="Taxonomy: ONTOLOGY field that divides the work (default: the TOPIC field).")
@click.option("--group-size", "group_size", default=None, type=click.IntRange(min=2),
              help="Taxonomy: maximum concepts per call (default 60).")
@click.option("--new-generations", "new_generations", default=None, type=click.IntRange(min=0),
              help="Taxonomy: generations of NEW concepts (default 1; 0 disables creation).")
@click.option("--within-topic", "within_topic", is_flag=True, default=False,
              help="Taxonomy policy: a new parent only groups children of the same topic.")
@click.option("--min-children", "min_children", default=None, type=click.IntRange(min=1),
              help="Taxonomy policy: minimum children for a new parent.")
@click.option("--max-new", "max_new", default=None, type=click.IntRange(min=0),
              help="Taxonomy policy: maximum new parents in this run.")
@click.option("--max-calls", "max_calls", default=None, type=click.IntRange(min=1),
              help="Budget: maximum model calls in this run (taxonomy default 60).")
@click.option("--hide-relations", "hide_relations", default=None,
              help="Taxonomy: CHAIN relations kept out of the prompt (e.g. the gold standard: TIPO_DE,PARTE_DE).")
def phase(project, phase_name, list_only, output_path, dry_run, overwrite, retry_rejected,
          concurrent, model, backup, group_by, group_size, new_generations, within_topic,
          min_children, max_new, max_calls, hide_relations):
    """Run a PHASE declared in the .synp: fill its group of template fields (ontology phases)."""
    from synesis_coder.manifest import ManifestConflict
    from synesis_coder.modes.phase_mode import list_phases, run_phase

    try:
        if list_only:
            click.echo(list_phases(project), nl=False)
            return
        if not phase_name:
            raise click.UsageError("Missing PHASE_NAME (or use --list).")
        report = run_phase(
            project, phase_name, output_path=output_path, dry_run=dry_run,
            overwrite=overwrite, retry_rejected=retry_rejected,
            concurrent=get_concurrent(5, concurrent), model=model, backup=backup,
            options={"group_by": group_by, "group_size": group_size,
                     "new_generations": new_generations, "within_topic": within_topic,
                     "min_children": min_children, "max_new": max_new, "max_calls": max_calls,
                     "hide_relations": hide_relations},
        )
        click.echo(report, nl=False)
    except click.UsageError:
        raise
    except ManifestConflict as exc:
        click.echo(f"Conflito: {exc}", err=True); sys.exit(2)
    except (FileNotFoundError, ValueError, EnvironmentError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)


@main.command()
@click.argument("project", type=click.Path(exists=True, path_type=Path))
@click.argument("alias")
@click.option("--into", "target", default=None, help="Concept that remains (the alias points to it).")
@click.option("--revert", is_flag=True, default=False, help="Undo the consolidation of ALIAS (from the manifest).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Validate and report; write nothing.")
@click.option("--backup", is_flag=True, default=False, help="Create a .bak copy of the .syno before writing.")
def consolidate(project, alias, target, revert, dry_run, backup):
    """Merge a duplicate model entry into the one that remains (ROLE USE); reversible."""
    from synesis_coder.manifest import ManifestConflict
    from synesis_coder.modes.consolidate_mode import consolidate as run

    try:
        click.echo(run(project, alias, target, revert=revert, dry_run=dry_run, backup=backup), nl=False)
    except ManifestConflict as exc:
        click.echo(f"Conflito: {exc}", err=True); sys.exit(2)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)


@main.command()
@click.argument("project", type=click.Path(exists=True, path_type=Path))
@click.argument("phase_name")
@click.option("--unit", type=click.Choice(["edge", "label", "concept"]), default="edge",
              show_default=True, help="Sampling unit.")
@click.option("--sample", "size", default=100, show_default=True, type=click.IntRange(min=1),
              help="Sample size.")
@click.option("--stratify", type=click.Choice(["depth", "topic", "origin", "none"]),
              default="depth", show_default=True, help="Strata of the sample.")
@click.option("--seed", default=1, show_default=True, type=int, help="Random seed (reproducible sample).")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Destination .syno of the phase run (default: the phase OUTPUT).")
@click.option("--score", "score_path", default=None, type=click.Path(exists=True, path_type=Path),
              help="Score a judged sample CSV instead of drawing a new one.")
def review(project, phase_name, unit, size, stratify, seed, output_path, score_path):
    """Sample the applied proposals of a phase for judgement, or score a judged sample."""
    from synesis_coder.modes import review_mode

    try:
        if score_path is not None:
            click.echo(review_mode.score(score_path), nl=False)
        else:
            click.echo(review_mode.sample(project, phase_name, unit=unit, size=size,
                                          stratify=stratify, seed=seed, output=output_path), nl=False)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_CRITIQUE)
@click.argument("syn_file", type=click.Path(exists=True, path_type=Path))
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file. Auto-detected from the .syn directory if omitted.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output path for the .synr file. Defaults to same name as .syn with .synr extension.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--sensitivity", default=None,
              help="How readily to flag: lenient | standard | strict, or a number [0.0-1.0]. "
                   "Default: standard (0.20).")
@click.option("--threshold", "suspicion_threshold", default=None, type=float,
              help="[deprecated] Numeric form of --sensitivity.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None,
              help="LLM model ID (overrides SYNESIS_CODER_CRITIQUE_MODEL).")
@click.option("--debug", "debug_mode", is_flag=True, default=False,
              help="Enable DEBUG log with raw LLM response per ITEM.")
def critique(syn_file, project_path, output_path, concurrent, sensitivity,
             suspicion_threshold, output_format, model, debug_mode):
    """[ACT Phase 2] Review .syn annotations and emit .synr with correction suggestions."""
    concurrent = get_concurrent(3, concurrent)
    from synesis_coder.modes.critique_mode import process_critique
    from synesis_coder.revision_vocab import resolve_sensitivity

    try:
        resolved_model = model or _validate_phase_env("critique")
        # Precedência: --sensitivity > --threshold (legado) > env > default.
        raw_sensitivity = sensitivity
        if raw_sensitivity is None and suspicion_threshold is not None:
            raw_sensitivity = suspicion_threshold
        if raw_sensitivity is None:
            raw_sensitivity = os.environ.get("SYNESIS_CODER_SUSPICION_THRESHOLD")
        suspicion_threshold, _ = resolve_sensitivity(raw_sensitivity)
        click.echo(process_critique(syn_path=syn_file, project_path=project_path,
                                    output_path=output_path, concurrent=concurrent,
                                    model=resolved_model, suspicion_threshold=suspicion_threshold,
                                    format=output_format, debug=debug_mode))
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


_EPILOG_ANCHOR = _ex(
    "",
    "  # Check every ITEM excerpt against its source abstract:",
    "  synesis-coder anchor annotations.syn --project project.synp",
    "",
    "  # Fail the build when any excerpt is not anchored (CI):",
    "  synesis-coder anchor annotations.syn --project p.synp --strict",
    "",
    "  # Check a field other than `text`:",
    "  synesis-coder anchor annotations.syn --project p.synp --field quotation",
    "",
    "  # Interviews/documents: sources named <bibref>.txt or .md in a folder",
    "  synesis-coder anchor annotations.syn --project p.synp --source-dir fontes/",
    "",
    "  # Exact comparison (no normalization) and a JSON report:",
    "  synesis-coder anchor annotations.syn --project p.synp --literal --json anchoring.json",
)


@main.command(epilog=_EPILOG_ANCHOR)
@click.argument("syn_file", type=click.Path(exists=True, path_type=Path))
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file. Auto-detected from the .syn directory if omitted.")
@click.option("--field", default=None,
              help="ITEM field holding the source excerpt. Default: the QUOTATION "
                   "field(s) declared in the template.")
@click.option("--strict", is_flag=True, default=False,
              help="Exit with code 1 unless every quotation was found "
                   "(not found or unverifiable quotations fail).")
@click.option("--source-dir", "source_dir", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Folder with source texts named <bibref>.txt or <bibref>.md "
                   "(interviews, documents). They take precedence over .bib abstracts. "
                   "Use the same text that was coded.")
@click.option("--literal", is_flag=True, default=False,
              help="Compare quotations exactly, without normalization.")
@click.option("--json", "json_path", default=None, type=click.Path(path_type=Path),
              help="Also write the full report (every quotation and its status) as JSON.")
def anchor(syn_file, project_path, field, strict, source_dir, literal, json_path):
    """Check that each ITEM quotation occurs in its source.

    Every QUOTATION field of every ITEM is checked and gets one of three states:
    found, not found, or unverifiable (no source text for that reference).
    Sources: --source-dir texts first, then the abstract in the .bib.
    Deterministic and free — no LLM call.
    """
    import json as _json

    from synesis_coder.anchor_check import (
        NORMALIZATION,
        check_quotations,
        format_results,
        items_without_excerpt,
        summarize,
    )
    from synesis_coder.modes.critique_mode import _resolve_project, quotation_fields

    try:
        resolved = _resolve_project(Path(syn_file), project_path)
        if resolved is None:
            click.echo(
                "Error: .synp project not found. Use --project.", err=True
            )
            sys.exit(1)

        from synesis_coder.project_loader import load_project
        ctx = load_project(resolved, load_annotations=False)
        bib_content = ctx.get("bib_content") or ""

        sources: dict = {}
        if source_dir is not None:
            from synesis_coder.modes.document_mode import read_document
            from synesis_coder.text_cleaner import clean_document

            for path in sorted(Path(source_dir).iterdir()):
                if path.suffix.lower() in (".txt", ".md"):
                    # The same cleaning the `document` mode applies before coding.
                    sources[path.stem] = clean_document(read_document(path))
        if not bib_content and not sources:
            click.echo(
                "Error: no source text: the project has no bibliography and no "
                "--source-dir was given.",
                err=True,
            )
            sys.exit(1)

        content = Path(syn_file).read_text(encoding="utf-8")
        total = len(re.findall(r"^ITEM\s+@\S+\s*$", content, re.MULTILINE))
        # O trecho está nos campos QUOTATION do template (o nome varia por projeto).
        fields = [field] if field else quotation_fields(ctx)
        results = check_quotations(content, bib_content, fields, sources=sources, literal=literal)
        missing = items_without_excerpt(content, fields)
        click.echo(format_results(results, total, missing, fields, literal=literal))

        summary = summarize(results)
        if json_path is not None:
            report = {
                "file": Path(syn_file).name,
                "mode": "literal" if literal else "normalized",
                "normalization": [] if literal else list(NORMALIZATION),
                "fields": fields,
                "items": total,
                "items_without_quotation": missing,
                "summary": summary,
                "results": [r.to_dict() for r in results],
            }
            Path(json_path).write_text(
                _json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if strict and not summary["verified"]:
            sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_NORMALIZE)
@click.argument("synr_files", nargs=-1, required=True,
                type=click.Path(exists=True, path_type=Path))
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file. Auto-detected if omitted.")
@click.option("--output-dir", "output_dir", default=None, type=click.Path(path_type=Path),
              help="Output directory for normalized .synr files. Defaults to input directory.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--confidence", "confidence_threshold", default=None, type=float,
              help="Minimum confidence to accept canonicalization [0.0–1.0]. Default: 0.65.")
@click.option("--inventory", "inventory_path", default=None, type=click.Path(path_type=Path),
              help="Path to save the code inventory as a TXT file.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None,
              help="LLM model ID (overrides SYNESIS_CODER_NORMALIZATION_MODEL).")
def normalize(synr_files, project_path, output_dir, concurrent, confidence_threshold,
              inventory_path, output_format, model):
    """[ACT Phase 3] Canonicalize codes cross-corpus and emit an updated .synr."""
    concurrent = get_concurrent(3, concurrent)
    from synesis_coder.modes.normalize_mode import process_normalize

    try:
        resolved_model = model or _validate_phase_env("normalization")
        if confidence_threshold is None:
            confidence_threshold = float(
                os.environ.get("SYNESIS_CODER_MERGE_CONFIDENCE_THRESHOLD", "0.65")
            )
        click.echo(process_normalize(synr_paths=list(synr_files),
                                     project_path=project_path, output_dir=output_dir,
                                     concurrent=concurrent, model=resolved_model,
                                     confidence_threshold=confidence_threshold,
                                     inventory_path=inventory_path, format=output_format))
    except (FileNotFoundError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_INCORPORATE)
@click.argument("synr_file", type=click.Path(exists=True, path_type=Path))
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file. Auto-detected from the .synr directory if omitted.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output path for the final .syn. Defaults to same name as .synr with .syn extension.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite the output file without confirmation if it already exists.")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the existing output before overwriting.")
def incorporate(synr_file, project_path, output_path, output_format, overwrite, backup):
    """[ACT Phase 4] Apply .synr revisions and emit a validated final .syn. No LLM."""
    from synesis_coder.modes.incorporate_mode import process_incorporate

    try:
        click.echo(process_incorporate(synr_path=synr_file, project_path=project_path,
                                       output_path=output_path, format=output_format,
                                       overwrite=overwrite, backup=backup))
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_REFINE)
@click.argument("syn_file", type=click.Path(exists=True, path_type=Path))
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file. Auto-detected from the .syn directory if omitted.")
@click.option("--output", "output_path", default=None, type=click.Path(path_type=Path),
              help="Output path for the refined .syn. Defaults to <stem>_refined.syn.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of ITEMs refined simultaneously.")
@click.option("--max-iter", "max_iter", default=None, type=int,
              help="Maximum refinement iterations per ITEM. Default: SYNESIS_CODER_REFINE_MAX_ITER or 2.")
@click.option("--threshold", "suspicion_threshold", default=None, type=float,
              help="Suspicion score below which an ITEM is considered converged [0.0–1.0]. Default: 0.20.")
@click.option("--critique-model", "critique_model", default=None,
              help="Critic model ID (overrides SYNESIS_CODER_CRITIQUE_MODEL).")
@click.option("--refine-model", "refine_model", default=None,
              help="Generator model ID for re-extraction (overrides SYNESIS_CODER_REFINE_MODEL).")
@click.option("--thinking-budget", "thinking_budget", default=None, type=int,
              help="Extended-thinking tokens for the re-extraction (0 = off). Ex: 8000.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite the output file without confirmation if it already exists.")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the existing output before overwriting.")
@click.option("--debug", "debug_mode", is_flag=True, default=False,
              help="Enable DEBUG log with raw LLM responses per ITEM.")
def refine(syn_file, project_path, output_path, concurrent, max_iter,
           suspicion_threshold, critique_model, refine_model, thinking_budget,
           output_format, overwrite, backup, debug_mode):
    """[ACT Phase R] Re-extract flagged ITEMs with critique feedback (opt-in, LLM). Emits final .syn."""
    concurrent = get_concurrent(3, concurrent)
    from synesis_coder.modes.refine_mode import process_refine

    try:
        # Resolve o modelo por fase (valida ANTHROPIC_API_KEY no backend anthropic).
        resolved_critique = critique_model or _validate_phase_env("critique")
        resolved_refine = refine_model or _validate_phase_env("refine")

        if max_iter is None:
            max_iter = int(os.environ.get("SYNESIS_CODER_REFINE_MAX_ITER", "2"))
        if suspicion_threshold is None:
            suspicion_threshold = float(
                os.environ.get("SYNESIS_CODER_SUSPICION_THRESHOLD", "0.20")
            )
        if thinking_budget is None:
            thinking_budget = 0

        # Guarda de I/O: nunca sobrescrever a fonte por acidente (§6.4).
        if output_path is not None:
            resolved_out = Path(output_path).resolve()
            if resolved_out == Path(syn_file).resolve() and not overwrite:
                click.echo(
                    "Error: --output points to the input file. "
                    "Choose another path or use --overwrite --backup explicitly.",
                    err=True,
                )
                sys.exit(1)

        click.echo(process_refine(
            syn_path=syn_file, project_path=project_path, output_path=output_path,
            concurrent=concurrent, critique_model=resolved_critique,
            refine_model=resolved_refine, max_iter=max_iter,
            suspicion_threshold=suspicion_threshold, thinking_budget=thinking_budget,
            format=output_format, overwrite=overwrite, backup=backup, debug=debug_mode,
        ))
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_FINETUNE)
@click.option("--project", "project_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to the .synp file (generates dataset via compiler).")
@click.option("--input", "input_path", default=None,
              type=click.Path(exists=True, path_type=Path),
              help="Path to a pre-generated JSONL (external Layer 1).")
@click.option("--output", "output_path", required=True, type=click.Path(path_type=Path),
              help="Output path for the enriched JSONL.")
@click.option("--enrich", "enrich", multiple=True,
              type=click.Choice(["vary", "didactic", "counterfactual"]),
              default=["vary"], show_default=True,
              help="LLM enrichment type(s). Can be repeated.")
@click.option("--concurrent", default=None, type=click.IntRange(min=1),
              help="Maximum number of simultaneous LLM calls.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True, help="Summary output format.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
@click.option("--overwrite", is_flag=True, default=False,
              help="Overwrite the output file without confirmation if it already exists.")
@click.option("--backup", is_flag=True, default=False,
              help="Create a .bak copy of the existing output before overwriting.")
def finetune(project_path, input_path, output_path, enrich, concurrent,
             output_format, model, overwrite, backup):
    """Enrich an Alpaca dataset with LLM variations for fine-tuning."""
    concurrent = get_concurrent(5, concurrent)
    from synesis_coder.modes.finetune_mode import process_finetune

    try:
        click.echo(process_finetune(output_path=output_path, project_path=project_path,
                                    input_path=input_path,
                                    enrich=list(enrich) if enrich else None,
                                    concurrent=concurrent, model=model,
                                    format=output_format, overwrite=overwrite,
                                    backup=backup))
    except FileExistsError as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)


@main.command(epilog=_EPILOG_SUGGEST)
@click.option("--project", required=True, type=click.Path(exists=True, path_type=Path),
              help="Path to the project .synp file.")
@click.option("--text", required=True,
              help="Text excerpt for which to suggest codes.")
@click.option("--format", "output_format", type=click.Choice(["plain", "verbose"]),
              default="plain", show_default=True,
              help="plain: suggestions only; verbose: includes metadata.")
@click.option("--model", default=None, help="LLM model ID (overrides SYNESIS_CODER_MODEL).")
def suggest(project, text, output_format, model):
    """Suggest codes already used in the project for a text excerpt."""
    from synesis_coder.modes.suggest_mode import process_suggest

    try:
        click.echo(process_suggest(project_path=project, text=text,
                                   format=output_format, model=model))
    except (FileNotFoundError, ValueError, EnvironmentError) as exc:
        click.echo(f"Error: {exc}", err=True); sys.exit(1)
    except Exception as exc:
        click.echo(f"Unexpected error: {exc}", err=True); sys.exit(1)
