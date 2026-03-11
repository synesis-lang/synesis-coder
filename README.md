# synesis-coder

Template-guided qualitative annotation generator for Synesis, powered by Claude.

`synesis-coder` reads your project template (`.synt`) and generates valid ITEM blocks
ready to append to `.syn` annotation files. Nothing is hardcoded: fields, relations,
allowed values, and coding instructions all come from the project template.

## Requirements

- Python 3.10+
- [synesis](https://github.com/usuario/synesis) ≥ 0.3.0 installed
- Anthropic API key (`ANTHROPIC_API_KEY`)

## Installation

```bash
cd d:/GitHub/synesis-coder
pip install -e ".[dev]"
```

Verify:

```bash
synesis-coder --version
# synesis-coder, version 0.0.1

python -m synesis_coder --version
# synesis-coder, version 0.0.1
```

## Configuration

Copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

```dotenv
# .env
ANTHROPIC_API_KEY=sk-ant-...

# Optional — overrides the default model (claude-opus-4-6)
# SYNESIS_CODER_MODEL=claude-sonnet-4-6
```

The `.env` file is loaded automatically. Never commit it to the repository
(already in `.gitignore`).

## Usage

### `item` — generate an ITEM block from text

```bash
synesis-coder item \
  --project path/to/project.synp \
  --bibref smith2024 \
  --text "Community trust is the most important factor for social acceptance."
```

**Options:**

| Option | Required | Description |
|--------|----------|-------------|
| `--project` | yes | Path to the `.synp` project file |
| `--bibref` | yes | Bibliographic reference key (e.g. `smith2024`) |
| `--text` | yes | Text to be coded |
| `--format` | no | `plain` (default) or `verbose` |
| `--model` | no | Model ID (overrides `SYNESIS_CODER_MODEL`) |

**Plain format** — Synesis block only (for piping to a file):

```bash
synesis-coder item \
  --project social_acceptance.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition." \
  >> annotations/ashworth2019.syn
```

**Verbose format** — includes a validation status header:

```bash
synesis-coder item \
  --project social_acceptance.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition." \
  --format verbose
```

Example verbose output:

```
# synesis-coder item
# bibref: @ashworth2019
# model: claude-opus-4-6
# validation: OK
# timestamp: 2026-03-10T14:32:11

ITEM @ashworth2019
  text "Local ownership models significantly reduce opposition."
  aspect 7
  dimension 2
  ...
END ITEM
```

**Alternative model:**

```bash
synesis-coder item \
  --project project.synp \
  --bibref smith2024 \
  --text "..." \
  --model claude-sonnet-4-6
```

### Other modes (future phases)

```bash
synesis-coder abstract   # Phase 2 — batch processing of .bib corpus
synesis-coder document   # Phase 3 — long document processing with chunking
synesis-coder ontology   # Phase 4 — ontology definition generation (.syno)
```

## How it works

```
synesis-coder item --project X --bibref Y --text Z
        │
        ▼
project_loader.load_project(X)          ← synesis.load() (compiler)
        │  extracts: template fields, CHAIN relations,
        │  code_index (existing concepts), topic_index
        ▼
prompt_builder.build_item_prompt(ctx, Y, Z)
        │  system (cached): rules + template + indexes
        │  user (dynamic): bibref + text
        ▼
LLMClient.call(messages, temperature=0)
        │  model: claude-opus-4-6 (default)
        ▼
validator.validate_and_fix(raw_output, ctx, client)
        │  synesis.load() validates the output
        │  if invalid: up to 3 correction attempts
        │  with escalating temperature (0.0 → 0.2 → 0.5)
        ▼
stdout: valid Synesis ITEM block(s)
```

The Synesis compiler is the sole interface with the project — it reads the template,
validates output, and provides detailed diagnostics for the correction loop.
Compiler updates are absorbed automatically.

## Supported project types

`synesis-coder` works with any Synesis project regardless of template complexity:

| Template type | Supported |
|---------------|-----------|
| With per-field GUIDELINES | ✓ |
| With CHAIN field and relations | ✓ |
| With ORDERED / ENUMERATED / SCALE fields | ✓ |
| Without ONTOLOGY scope | ✓ |
| Without CHAIN field | ✓ |
| Minimal template (`synesis init`) | ✓ |

## Tests

Tests use real projects from `d:/GitHub/case-studies/` as fixtures.

**Tests without API** (fast, no credentials needed):

```bash
pytest tests/test_item_mode.py -v -k "not Integration"
```

**Integration tests** (require `ANTHROPIC_API_KEY`):

```bash
pytest tests/test_item_mode.py -v
```

**Coverage:**

```bash
pytest tests/ --cov=synesis_coder --cov-report=term-missing
```

## Project structure

```
synesis-coder/
├── synesis_coder/
│   ├── __init__.py
│   ├── __main__.py          # python -m synesis_coder
│   ├── cli.py               # Click interface
│   ├── project_loader.py    # Synesis compiler interface
│   ├── prompt_builder.py    # Per-field prompt construction
│   ├── llm_client.py        # Anthropic client with rate limiting
│   ├── validator.py         # Output validation and correction
│   └── modes/
│       ├── item_mode.py     # Item mode (Phase 1 — implemented)
│       ├── abstract_mode.py # Abstract mode (Phase 2 — pending)
│       ├── document_mode.py # Document mode (Phase 3 — pending)
│       └── ontology_mode.py # Ontology mode (Phase 4 — pending)
├── tests/
│   └── test_item_mode.py
├── .env.example
├── .gitignore
├── CHANGELOG.md
└── pyproject.toml
```

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | yes | — | Anthropic API key |
| `SYNESIS_CODER_MODEL` | no | `claude-opus-4-6` | Default LLM model |
| `SYNESIS_CODER_MAX_RETRIES` | no | `3` | Correction attempts |
| `SYNESIS_CODER_TEMPERATURE` | no | `0.0` | Generation temperature |
| `SYNESIS_CODER_RPM_LIMIT` | no | `50` | Requests per minute limit |
| `SYNESIS_CODER_TPM_LIMIT` | no | `100000` | Tokens per minute limit |

## License

MIT
