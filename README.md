# synesis-coder

Template-guided qualitative annotation generator for Synesis, powered by Claude.

`synesis-coder` reads your project template (`.synt`) and generates valid Synesis
blocks — ITEM annotations, project abstracts, batch document processing, and ONTOLOGY
entries. Nothing is hardcoded: fields, relations, allowed values, and coding
instructions all come from the project template.

## Requirements

- Python 3.10+
- [synesis](https://github.com/synesis-lang/synesis) ≥ 0.5.5 installed
- Anthropic API key (`ANTHROPIC_API_KEY`)

### Compatibility matrix

| Package | This version | Requires `synesis` | Python |
|---|---|---|---|
| synesis | 0.5.5 | — | ≥3.10 |
| synesis-coder | 0.4.1 | ≥0.5.5 | ≥3.10 |
| synesis-lsp | 0.15.4 | ≥0.5.5 | ≥3.10 |
| synesis-graph | 0.2.0 | ≥0.5.5 | ≥3.10 |

## Installation

```bash
pip install synesis-coder
```

Or from source:

```bash
cd synesis-coder
pip install -e ".[dev]"
```

Verify:

```bash
synesis-coder --version
# synesis-coder, version 0.1.0
```

## Configuration

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

```dotenv
ANTHROPIC_API_KEY=sk-ant-...

# Optional — overrides the default model (claude-opus-4-6)
# SYNESIS_CODER_MODEL=claude-sonnet-4-6
```

## Modes

### `item` — generate an ITEM block from text

Takes a text excerpt and a bibliographic reference, and returns a valid ITEM block
coded according to your project template.

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

### `abstract` — generate a structured abstract from a SOURCE corpus

Reads all ITEM blocks for a given bibref and generates a structured synthesis
abstract reflecting the coding analysis.

```bash
synesis-coder abstract \
  --project path/to/project.synp \
  --bibref smith2024
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--bibref` | yes | Bibliographic reference key |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |

---

### `document` — batch-generate ITEM blocks for an entire project

Processes all SOURCEs that have no ITEM annotations yet, generating ITEM blocks
concurrently and appending them to a `.syn` output file.

```bash
synesis-coder document \
  --project path/to/project.synp \
  --output batch_output.syn
```

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--output` | no | Output `.syn` file (default: `{project_stem}_coded.syn`) |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |

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
| `--output` | no | Output `.syno` file (default: `{project_stem}.syno`) |
| `--update` | no | Skip codes already defined in the existing `.syno` |
| `--concurrent` | no | Simultaneous LLM calls (default: `5`) |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID |

**Backup:** when `--update` is not used and the output `.syno` already exists, a
backup is automatically created as `{stem}_bkp.syno` before overwriting.

**Template requirement:** the project template must define at least one field with
`SCOPE ONTOLOGY`. Projects without an ontology scope (e.g. `thompson_bible`) raise
a `ValueError` with a clear message.

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
        │  model: claude-opus-4-6 (default)
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

Concurrent modes (`document`, `ontology`) use `AsyncLLMClient` with shared
RPM/TPM rate-limiting semaphores.

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
│   ├── cli.py               # Click interface (item, abstract, document, ontology)
│   ├── project_loader.py    # Synesis compiler interface
│   ├── prompt_builder.py    # Per-mode prompt construction
│   ├── llm_client.py        # Anthropic client (sync + async) with rate limiting
│   ├── validator.py         # Output validation and correction loop
│   └── modes/
│       ├── item_mode.py     # Single ITEM generation
│       ├── abstract_mode.py # Structured abstract from corpus
│       ├── document_mode.py # Batch ITEM generation for a project
│       └── ontology_mode.py # ONTOLOGY entry generation
├── tests/
│   ├── test_item_mode.py
│   └── test_ontology_mode.py
├── .env.example
├── .gitignore
├── CHANGELOG.md
└── pyproject.toml
```

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key |
| `SYNESIS_CODER_MODEL` | no | `claude-opus-4-6` | Default LLM model |
| `SYNESIS_CODER_MAX_RETRIES` | no | `3` | Correction attempts per item |
| `SYNESIS_CODER_TEMPERATURE` | no | `0.0` | Generation temperature |
| `SYNESIS_CODER_RPM_LIMIT` | no | `50` | Requests per minute limit |
| `SYNESIS_CODER_TPM_LIMIT` | no | `100000` | Tokens per minute limit |

## License

MIT
