# synesis-coder

Template-guided qualitative annotation pipeline for Synesis, powered by LLMs.

`synesis-coder` reads your project template (`.synt`) and generates, reviews, and
consolidates valid Synesis blocks — ITEM annotations, project abstracts, batch
document processing, ONTOLOGY entries, and fine-tuning datasets. Nothing is
hardcoded: fields, relations, allowed values, and coding instructions all come
from the project template.

## Requirements

- Python 3.10+
- [synesis](https://github.com/synesis-lang/synesis) ≥ 0.10.0 installed
- An LLM backend: Anthropic API key (`ANTHROPIC_API_KEY`), or an OpenAI-compatible
  endpoint (Ollama, RunPod, Together AI, etc.)

### Compatibility matrix

| Package | This version | Requires `synesis` | Python |
|---|---|---|---|
| synesis | 0.11.0 | — | ≥3.10 |
| synesis-coder | 0.8.0 | ≥0.10.0 | ≥3.10 |
| synesis-lsp | 0.22.0 | ≥0.10.0 | ≥3.10 |
| synesis-graph | 0.5.0 | ≥0.10.0 | ≥3.10 |

## Installation

```bash
pip install synesis-coder
```

Or from source:

```bash
git clone https://github.com/synesis-lang/synesis-coder.git
cd synesis-coder
pip install -e ".[dev]"
```

Verify:

```bash
synesis-coder --version
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```dotenv
# Backend Anthropic (default)
ANTHROPIC_API_KEY=sk-ant-...

# Or an OpenAI-compatible backend (Ollama, RunPod, Together AI, ...)
# SYNESIS_CODER_BACKEND=openai
# SYNESIS_CODER_API_URL=http://localhost:11434
# SYNESIS_CODER_API_KEY=no-key-required

# Optional — overrides the default model (claude-opus-4-6)
# SYNESIS_CODER_MODEL=claude-sonnet-4-6
```

See `.env.example` for the full set of options, including per-phase models for
the ACT pipeline, rate limiting, and extended thinking.

## The ACT pipeline

Beyond single-shot generation, `synesis-coder` implements a staged pipeline for
higher-fidelity annotation with auditability at each step:

```
Phase 1  item / abstract / document   → .syn   (LLM generates annotations)
Phase 2  critique                     → .synr  (LLM reviews .syn, flags suspect ITEMs)
Phase 3  normalize                    → .synr  (LLM canonicalizes codes cross-corpus)
Phase 4  incorporate                  → .syn   (deterministic — applies .synr, no LLM)
Phase R  refine (opt-in)              → .syn   (LLM re-extracts flagged ITEMs with feedback)
```

`critique` and `refine` can use a **separate LLM connection** from the generator
(e.g. generator on OpenRouter, critic on native Anthropic) for epistemic
independence — see `SYNESIS_CODER_CRITIQUE_*` in `.env.example`.

`incorporate` is the only phase with no LLM call: it deterministically merges
`.synr` revisions into the final `.syn` and validates the result.

---

## Modes

### `item` — generate an ITEM block from text

```bash
synesis-coder item \
  --project path/to/project.synp \
  --bibref smith2024 \
  --text "Community trust is the most important factor for social acceptance."
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--bibref` | yes | Bibliographic reference key (without `@`) |
| `--text` | yes | Text excerpt to be coded |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID (overrides `SYNESIS_CODER_MODEL`) |
| `--thinking-budget` | no | Extended-thinking tokens (e.g. `8000`) |
| `--language` | no | Output language for free-text fields (e.g. `pt-BR`, `en`) |
| `--max-tokens` | no | Maximum output tokens |
| `--temperature` | no | Model temperature (`0` = deterministic) |

**Plain format** — Synesis block only (suitable for piping to a file):

```bash
synesis-coder item \
  --project social_acceptance.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition." \
  >> annotations/ashworth2019.syn
```

**Verbose format** — includes validation status header:

```
# synesis-coder item
# bibref: @ashworth2019
# model: claude-opus-4-6
# validation: OK
# timestamp: 2026-03-23T14:32:11

ITEM @ashworth2019
  text "Local ownership models significantly reduce opposition."
  aspect 7
  dimension 2
  ...
END ITEM
```

---

### `abstract` — batch-generate annotations from a `.bib` corpus

Processes an entire `.bib` file, generating a structured SOURCE + ITEM synthesis
per reference.

```bash
synesis-coder abstract \
  --project path/to/project.synp \
  --input corpus.bib \
  --output annotations/
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--input` | yes | Path to the `.bib` file containing abstracts |
| `--output` | yes | Output directory for the generated `.syn` files |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--batch-size` | no | Batch size; project reloaded between batches (default: `25`) |
| `--per-reference` | no | One `.syn` file per reference (default: single file) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |
| `--thinking-budget` | no | Extended-thinking tokens |
| `--language` | no | Output language for free-text fields |
| `--max-tokens` | no | Maximum output tokens |
| `--temperature` | no | Model temperature |
| `--debug` | no | Write a Markdown audit log of the LLM pipeline |

---

### `dataset` — batch-generate annotations from a TOML corpus

Processes a TOML corpus declared by `INCLUDE DATASET` in the `.synp`, generating
a SOURCE + ITEMs per record. Fields declared `ON DATASET` are resolved
deterministically by the compiler; the interpretive ITEM fields are generated by
the LLM from the TOML context. Requires `synesis >= 0.10.0`.

```bash
synesis-coder dataset \
  --project path/to/project.synp \
  --output-dir annotations/
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file (declares `INCLUDE DATASET`) |
| `--output-dir` | yes | Output directory for the generated `.syn` files (one per record) |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--single-file` | no | Write a single `dataset.syn` instead of one `.syn` per record |
| `--dataset` | no | Overrides the `INCLUDE DATASET` glob for this run only; the project file is not modified |
| `--model` | no | Model ID |
| `--language` | no | Output language for free-text fields |
| `--temperature` | no | Model temperature |

---

### `document` — process a long document with chunking

Splits a `.txt`/`.md` document into overlapping chunks and generates a `.syn`
annotation file.

```bash
synesis-coder document \
  --project path/to/project.synp \
  --bibref interview_01 \
  --input interview_01.txt \
  --output interview_01.syn
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--bibref` | yes | Bibliographic reference key for this document |
| `--input` | yes | Path to the `.txt`/`.md` file to annotate |
| `--output` | yes | Output path for the generated `.syn` file |
| `--chunk-size` | no | Maximum chunk size in characters (default: `12000`) |
| `--overlap` | no | Overlap between chunks in characters (default: `2400`) |
| `--concurrent` | no | Simultaneous LLM calls (default: `3`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |
| `--thinking-budget` | no | Extended-thinking tokens |
| `--language` | no | Output language for free-text fields |
| `--max-tokens` | no | Maximum output tokens |
| `--temperature` | no | Model temperature |
| `--debug` | no | Write a Markdown audit log of the LLM pipeline |
| `--overwrite` | no | Overwrite output without confirmation |
| `--backup` | no | Create a `.bak` copy before overwriting |

Tolerates pre-existing annotation errors in the output file referenced by
`INCLUDE ANNOTATIONS` — lets you regenerate a `.syn` that used an outdated
template without a chicken-and-egg failure.

---

### `ontology` — generate ONTOLOGY entries for project codes

Reads all codes used in the project corpus, builds rich semantic context for each
(frequency, sources, relations, co-occurrences, representative examples), and
generates ONTOLOGY definitions for a `.syno` file.

```bash
synesis-coder ontology \
  --project path/to/project.synp \
  --output ontology.syno
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--output` | yes | Output `.syno` file |
| `--update` | no | Only generate entries for codes not yet defined in the `.syno` |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |
| `--thinking-budget` | no | Extended-thinking tokens |
| `--language` | no | Output language for free-text fields |
| `--max-tokens` | no | Maximum output tokens |
| `--temperature` | no | Model temperature |
| `--overwrite` | no | Overwrite output without confirmation |
| `--backup` | no | Create a `.bak` copy before overwriting |

**Template requirement:** the project template must define at least one field with
`SCOPE ONTOLOGY`. Projects without an ontology scope (e.g. `thompson_bible`) raise
a `ValueError` with a clear message.

---

### `critique` — [ACT Phase 2] review annotations for fidelity

Evaluates each ITEM in a `.syn` file against its source text and emits a `.synr`
file with correction suggestions for suspect items.

```bash
synesis-coder critique annotations/smith2024.syn --output smith2024.synr
```

| Option | Required | Description |
|--------|----------|-------------|
| `syn_file` (argument) | yes | Path to the `.syn` file to review |
| `--project` | no | Path to `.synp`; auto-detected from the `.syn` directory if omitted |
| `--output` | no | Output `.synr` path (default: same name, `.synr` extension) |
| `--concurrent` | no | Simultaneous LLM calls (default: `3`) |
| `--threshold` | no | Minimum suspicion score to emit a `# REVISION` block (default: `0.20`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID (overrides `SYNESIS_CODER_CRITIQUE_MODEL`) |
| `--debug` | no | DEBUG log with raw LLM response per ITEM |

Can use a separate LLM connection from the generator via
`SYNESIS_CODER_CRITIQUE_{BACKEND,API_URL,API_KEY}`.

---

### `normalize` — [ACT Phase 3] canonicalize codes cross-corpus

Builds a global code inventory across one or more `.synr` files and canonicalizes
case/spelling variants of the same code (e.g. `High_Ranking_Team` vs.
`high_ranking_team`).

```bash
synesis-coder normalize annotations/*.synr --output-dir normalized/
```

| Option | Required | Description |
|--------|----------|-------------|
| `synr_files` (argument, multiple) | yes | One or more `.synr` files |
| `--project` | no | Path to `.synp`; auto-detected if omitted |
| `--output-dir` | no | Output directory for normalized `.synr` files (default: input directory) |
| `--concurrent` | no | Simultaneous LLM calls (default: `3`) |
| `--confidence` | no | Minimum confidence to accept canonicalization (default: `0.65`) |
| `--inventory` | no | Path to save the code inventory as a TXT file |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID (overrides `SYNESIS_CODER_NORMALIZATION_MODEL`) |

---

### `incorporate` — [ACT Phase 4] apply revisions, no LLM

Deterministically applies `.synr` revisions (from `critique` and/or `normalize`)
to produce the final, validated `.syn` file. No LLM call.

```bash
synesis-coder incorporate annotations/smith2024.synr --output smith2024.syn
```

| Option | Required | Description |
|--------|----------|-------------|
| `synr_file` (argument) | yes | Path to the `.synr` file |
| `--project` | no | Path to `.synp`; auto-detected from the `.synr` directory if omitted |
| `--output` | no | Output path for the final `.syn` (default: same name, `.syn` extension) |
| `--format` | no | `plain` (default) or `verbose` |
| `--overwrite` | no | Overwrite output without confirmation |
| `--backup` | no | Create a `.bak` copy before overwriting |

---

### `refine` — [ACT Phase R, opt-in] re-extraction with feedback

For each ITEM flagged by `critique`, the **generator** re-reads the source text
with the critic's feedback and rewrites the annotation from scratch — instead of
mechanically applying the critic's suggested field values (as `incorporate` does).
Implements a Self-Refine/Reflexion loop with safety clauses: strict
non-regression (only accepts a version with a lower `suspicion_score`), a hard
`--max-iter`, fixed-point/oscillation detection, mandatory structural
re-validation, and critic ≠ generator models to avoid self-validation bias.

```bash
synesis-coder refine annotations/smith2024.syn --output smith2024_refined.syn
```

| Option | Required | Description |
|--------|----------|-------------|
| `syn_file` (argument) | yes | Path to the `.syn` file to refine |
| `--project` | no | Path to `.synp`; auto-detected from the `.syn` directory if omitted |
| `--output` | no | Output path (default: `<stem>_refined.syn`) |
| `--concurrent` | no | Simultaneous ITEMs refined (default: `3`) |
| `--max-iter` | no | Max refinement iterations per ITEM (default: `SYNESIS_CODER_REFINE_MAX_ITER` or `2`) |
| `--threshold` | no | Suspicion score below which an ITEM is considered converged (default: `0.20`) |
| `--critique-model` | no | Critic model ID (overrides `SYNESIS_CODER_CRITIQUE_MODEL`) |
| `--refine-model` | no | Generator model ID (overrides `SYNESIS_CODER_REFINE_MODEL`) |
| `--thinking-budget` | no | Extended-thinking tokens for re-extraction (`0` = off) |
| `--format` | no | `plain` (default) or `verbose` |
| `--overwrite` | no | Overwrite output without confirmation |
| `--backup` | no | Create a `.bak` copy before overwriting |
| `--debug` | no | DEBUG log with raw LLM responses per ITEM |

The final `.syn` carries a `# $metrics.refine.*` header with aggregate metrics
and a per-ITEM score trace (`# $refine.@bibref.trace: 0.62 -> 0.18`). Includes an
I/O guard that refuses to overwrite the source file unless `--overwrite` is
explicit.

---

### `suggest` — suggest relevant codes for a text excerpt

Identifies the 2–4 most relevant topics for a text excerpt, then returns an
enriched, ranked list of candidate codes (with frequency and ontological
description) for manual or assisted coding.

```bash
synesis-coder suggest \
  --project path/to/project.synp \
  --text "Local ownership models significantly reduce opposition."
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--text` | yes | Text excerpt for which to suggest codes |
| `--format` | no | `plain`: suggestions only; `verbose`: includes metadata |
| `--model` | no | Model ID |

Codes suggested by the LLM that don't yet exist in the project are marked `[NEW]`.

---

### `finetune` — enrich a fine-tuning dataset

Generates (or loads) an Alpaca-format dataset from a project's annotations and
enriches it with LLM-generated variations for fine-tuning.

```bash
synesis-coder finetune \
  --project path/to/project.synp \
  --output dataset_enriched.jsonl \
  --enrich vary --enrich didactic
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | no* | Path to `.synp` (generates the base dataset via the compiler) |
| `--input` | no* | Path to a pre-generated JSONL instead of `--project` |
| `--output` | yes | Output path for the enriched JSONL |
| `--enrich` | no | Enrichment type(s): `vary`, `didactic`, `counterfactual` (repeatable; default: `vary`) |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |
| `--overwrite` | no | Overwrite output without confirmation |
| `--backup` | no | Create a `.bak` copy before overwriting |

*One of `--project` or `--input` is required.

---

## VSCode integration

`synesis-coder item` is integrated into the **Synesis Explorer** extension (v0.5.25+).

Select a text excerpt in a `.syn` file, then:
- Right-click → **Synesis: Code Selection**
- Or press `Ctrl+Shift+I` / `Cmd+Shift+I`

The extension auto-detects the bibref from the SOURCE/ITEM block under the cursor,
calls `synesis-coder item`, and **replaces the selected text** with the generated
ITEM block.

Configure the executable path in VS Code settings:
```json
"synesisExplorer.coder.path": "synesis-coder"
```

---

## How it works

```
synesis-coder item --project X --bibref Y --text Z
        │
        ▼
project_loader.load_project(X)          ← synesis.load() (compiler)
        │  extracts: template fields, CHAIN relations,
        │  code_index, topic_index, ontology_index
        ▼
prompt_builder.build_item_prompt(ctx, Y, Z)
        │  system (cached): rules + template + indexes
        │  user (dynamic): bibref + text
        ▼
LLMClient.call(messages, temperature=0)
        │  model: claude-opus-4-6 (default); or any OpenAI-compatible backend
        │  structured-output path (JSON schema → assembler) when supported:
        │    · Anthropic: native structured outputs (requires anthropic>=0.77.1)
        │    · OpenAI-compatible: response_format json_schema
        │    · otherwise falls back to free-text extraction
        ▼
validator.validate_and_fix(output, ctx, client)
        │  synesis.load() validates the output
        │  if invalid: up to 3 correction attempts
        │  temperature escalation: 0.0 → 0.2 → 0.5
        ▼
stdout: valid Synesis ITEM block(s)
```

For `ontology` mode, `_build_semantic_ctx()` assembles per-code context before
prompt construction: frequency, distinct sources, CHAIN relations (up to 15),
co-occurring codes (up to 20), and representative text examples (up to 3).

Concurrent modes (`document`, `abstract`, `ontology`, `critique`, `normalize`,
`refine`, `finetune`) use `AsyncLLMClient` with shared RPM/TPM rate-limiting
semaphores.

`critique` and `refine` can be pointed at a **separate LLM connection** from the
generator for epistemic independence (e.g. generator via OpenRouter, critic on
native Anthropic) — see `get_critique_connection()` in `llm_client.py`.

The Synesis compiler is the sole interface with the project — compiler updates
are absorbed automatically.

---

## Supported project types

| Template feature | Supported |
|-----------------|-----------|
| Per-field GUIDELINES | ✓ |
| CHAIN field with relations | ✓ |
| ORDERED / ENUMERATED / SCALE fields | ✓ |
| With ONTOLOGY scope | ✓ |
| Without ONTOLOGY scope | ✓ (item/abstract/document modes) |
| Without CHAIN field | ✓ |
| Minimal template (`synesis init`) | ✓ |

---

## Tests

Tests use real projects from `d:/GitHub/case-studies/` as fixtures.

**Unit tests** (no API credentials needed):

```bash
pytest tests/ -v -k "not integration"
```

**Integration tests** (require `ANTHROPIC_API_KEY`):

```bash
pytest tests/ -v -m integration
```

**All tests with coverage:**

```bash
pytest tests/ --cov=synesis_coder --cov-report=term-missing
```

---

## Project structure

```
synesis-coder/
├── synesis_coder/
│   ├── __init__.py
│   ├── __main__.py          # python -m synesis_coder
│   ├── cli.py                # Click interface (all subcommands)
│   ├── project_loader.py     # Synesis compiler interface
│   ├── prompt_builder.py     # Per-mode prompt construction
│   ├── llm_client.py         # Anthropic/OpenAI-compatible client (sync + async), rate limiting
│   ├── validator.py          # Output validation and correction loop
│   ├── schema_builder.py     # Template-driven schema helpers
│   ├── block_assembler.py    # Structured-field → Synesis block assembly
│   ├── synr_io.py            # .synr I/O, safe/atomic output writes
│   ├── text_cleaner.py       # Text normalization helpers
│   ├── token_usage.py        # Token usage accounting
│   ├── debug_log.py          # Markdown audit log generation (--debug)
│   ├── prompt_dump.py        # --prompt-only prompt serialization
│   ├── runtime_info.py       # Console/version/header presentation
│   └── modes/
│       ├── item_mode.py         # Single ITEM generation
│       ├── abstract_mode.py     # Batch SOURCE+ITEM synthesis from a .bib corpus
│       ├── dataset_mode.py      # Batch SOURCE+ITEM synthesis from a TOML corpus
│       ├── document_mode.py     # Chunked ITEM generation for long documents
│       ├── ontology_mode.py     # ONTOLOGY entry generation
│       ├── critique_mode.py     # [ACT Phase 2] .syn → .synr fidelity review
│       ├── normalize_mode.py    # [ACT Phase 3] cross-corpus code canonicalization
│       ├── incorporate_mode.py  # [ACT Phase 4] .synr → .syn, deterministic
│       ├── refine_mode.py       # [ACT Phase R] re-extraction with feedback
│       ├── suggest_mode.py      # Code suggestions for a text excerpt
│       └── finetune_mode.py     # Fine-tuning dataset enrichment
├── tests/
├── .env.example
├── .gitignore
├── CHANGELOG.md
└── pyproject.toml
```

---

## Environment variables

See `.env.example` for the complete, commented reference. Key variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | yes* | — | Anthropic API key (*required when `SYNESIS_CODER_BACKEND=anthropic`, the default) |
| `SYNESIS_CODER_BACKEND` | no | `anthropic` | `anthropic` or `openai` (OpenAI-compatible: Ollama, RunPod, Together AI, ...) |
| `SYNESIS_CODER_API_URL` | no* | — | Base URL for the OpenAI-compatible backend |
| `SYNESIS_CODER_API_KEY` | no* | — | API key for the OpenAI-compatible backend |
| `SYNESIS_CODER_MODEL` | no | `claude-opus-4-6` | Default LLM model |
| `SYNESIS_CODER_MAX_RETRIES` | no | `3` | Correction attempts per item |
| `SYNESIS_CODER_TEMPERATURE` | no | `0.0` | Generation temperature |
| `SYNESIS_CODER_MAX_TOKENS` | no | model default | Maximum output tokens (overridden by `--max-tokens`) |
| `SYNESIS_CODER_LANGUAGE` | no | template default | Output language for free-text fields (overridden by `--language`) |
| `SYNESIS_CODER_MAX_RPM` | no | `50` | Requests per minute (Anthropic backend only) |
| `SYNESIS_CODER_MAX_INPUT_TPM` | no | `40000` | Input tokens per minute (Anthropic backend only) |
| `SYNESIS_CODER_MAX_OUTPUT_TPM` | no | `8000` | Output tokens per minute (Anthropic backend only) |
| `SYNESIS_CODER_THINKING_BUDGET` | no | `0` | Extended-thinking tokens (Anthropic Claude 4.x only) |
| `SYNESIS_CODER_CRITIQUE_MODEL` | no | falls back to `SYNESIS_CODER_MODEL` | Model for ACT Phase 2 (`critique`) and the critic in `refine` |
| `SYNESIS_CODER_CRITIQUE_BACKEND` / `_API_URL` / `_API_KEY` | no | inherits primary connection | Separate LLM connection for critique (epistemic independence) |
| `SYNESIS_CODER_NORMALIZATION_MODEL` | no | falls back to `SYNESIS_CODER_MODEL` | Model for ACT Phase 3 (`normalize`) |
| `SYNESIS_CODER_REFINE_MODEL` | no | falls back to `SYNESIS_CODER_MODEL` | Generator model for ACT Phase R (`refine`) |
| `SYNESIS_CODER_REFINE_MAX_ITER` | no | `2` | Max refinement iterations per ITEM |
| `SYNESIS_CODER_SUSPICION_THRESHOLD` | no | `0.20` | Minimum suspicion score to flag an ITEM (`critique`, `refine`) |
| `SYNESIS_CODER_MERGE_CONFIDENCE_THRESHOLD` | no | `0.65` | Minimum confidence to accept a code canonicalization (`normalize`) |

## License

This program is distributed under the **GNU Affero General Public License,
version 3 only (AGPL-3.0-only), with the Synesis Data-Output Exception** — see
[LICENSE](LICENSE) and [LICENSE.exception](LICENSE.exception).

SPDX identifier: `AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception`

**Your research data and generated outputs are yours.** The files you feed to
synesis-coder and the artifacts it produces from them — annotation blocks,
`.syn`/`.syno` files, datasets, and other exported material — are **not**
covered by the AGPL and carry no copyleft obligation toward Synesis. You may
license and use those outputs however you wish. This holds even when an output
carries Synesis's own runtime material. See `LICENSE.exception` for the terms.

The AGPL applies to synesis-coder itself: if you modify it and distribute it,
or run it as a network service, you must share your changes under the AGPL.

This license grants no rights to the "Synesis" name or logo.
