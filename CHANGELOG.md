# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [0.0.1] — 2026-03-10

### Added — Phase 1: `item` mode

This release implements the MVP of `synesis-coder`: generating Synesis ITEM blocks
from text and a bibliographic reference, with compiler-based validation and an
automatic LLM correction loop.

#### New modules

**`synesis_coder/project_loader.py`**
- `load_project(project_path, load_annotations, load_ontology)` — the single
  function that invokes `synesis.load()` to load project context
- Separates fields by scope (`SOURCE`, `ITEM`, `ONTOLOGY`) from
  `result.template.field_specs`
- Detects the `CHAIN` field in `ITEM` scope and extracts its relations
- Builds `code_index` by combining `code_usage` (from `CODE` fields) with nodes
  from `all_triples` — so CHAIN-only projects (no `CODE` field) still get a
  populated index
- Builds `topic_index` from `linked_project.topic_index`
- Reads project description via `result.linked_project.project.description`
  (the compiler already processes the `DESCRIPTION...END DESCRIPTION` block)
- `load_ontology=False` by default — prevents errors when loading projects
  whose `.syno` references fields absent from the current template
- Bibliography (`.bib`) always loaded regardless of `load_annotations` flag,
  since it is required for compiler validation

**`synesis_coder/prompt_builder.py`**
- `build_item_prompt(ctx, bibref, text)` — assembles the Anthropic API message
  list with prompt caching on the system message
- Cached system prompt contains: absolute Synesis format rules, project
  description, per-field instructions derived from the template, existing
  concept index (`code_index`), and existing topic index (`topic_index`)
- `_field_instruction(name, spec, ctx)` — generates per-field instruction using
  `guidelines` > `description` > generic instruction by `FieldType`
- `CHAIN` fields: injects available relations and list of existing concepts
- `ORDERED`/`ENUMERATED` fields: injects allowed values with labels
- `SCALE` fields: injects range from format string
- Dynamic user message: `BIBREF: @{bibref}` + `<text>{text}</text>`
- Prompt caching active from `item` mode (reduces latency and cost per session)

**`synesis_coder/llm_client.py`**
- `LLMClient` class — the only module that imports `anthropic`
- Loads `ANTHROPIC_API_KEY` via `python-dotenv` (`.env` in project root)
- Supports alternative model via `model` parameter or `SYNESIS_CODER_MODEL` env var
- Default model: `claude-opus-4-6`
- Rate limiting: RPM semaphore + 60-second sliding window for TPM
  (input and output tokens tracked separately)
- `call(messages, temperature)` — translates internal format to Anthropic API
- `fix(previous_output, errors, temperature)` — correction call with previous
  output and compiler diagnostics
- `_translate_messages()` — converts `[{"role", "content", "cache"}]` to
  `system` blocks with `cache_control` and the API `messages` list

**`synesis_coder/validator.py`**
- `validate_and_fix(output, ctx, llm_client, annotation_key, max_tries)` —
  validates output via `synesis.load()` and requests LLM corrections if invalid
- `_has_structural_errors(result)` — filters `OrphanItem` from the error list;
  `OrphanItem` (ITEM without a corresponding SOURCE) is expected when validating
  an isolated ITEM — the SOURCE exists in the project's `.syn` but is not loaded
  to avoid exceeding API token limits
- `_extract_item_blocks(text)` — extracts only `ITEM...END ITEM` blocks from
  the output, discarding `SOURCE`, `ONTOLOGY`, or markdown blocks the LLM adds
  even when instructed not to
- `_strip_markdown_fences(text)` — removes ` ``` ` delimiters from LLM output
- Temperature escalation across correction attempts:
  `CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]` — avoids deterministic loops
- Error fallback: commented error header prepended to last output when all
  correction attempts are exhausted

**`synesis_coder/modes/item_mode.py`**
- `process_item(project_path, bibref, text, format, model)` — orchestrates the
  full pipeline: load project → build prompt → call LLM → validate
- `plain` format: returns only the Synesis ITEM blocks (for piping to `.syn`
  files or editor use)
- `verbose` format: prepends a header with validation status, model, bibref,
  and timestamp (for interactive terminal use)

**`synesis_coder/cli.py`**
- Click CLI with four subcommands: `item`, `abstract`, `document`, `ontology`
- `--version` flag shows `0.0.1` (read from `pyproject.toml` via
  `importlib.metadata`)
- Usage examples included in the root command `--help`
- `abstract`, `document`, `ontology` subcommands print an informative message
  and exit with code 1 (pending implementation in future phases)

**`synesis_coder/__main__.py`**
- Enables invocation via `python -m synesis_coder`

#### Support files

**`pyproject.toml`**
- Dependencies: `synesis>=0.3.0`, `anthropic>=0.40.0`, `click>=8.0`,
  `tenacity>=8.0`, `bibtexparser>=1.4`, `python-dotenv>=1.0`
- Entry point: `synesis-coder = "synesis_coder.cli:main"`
- Build backend: `setuptools.build_meta`

**`.env.example`**
- Configuration template with required `ANTHROPIC_API_KEY` and optional vars:
  `SYNESIS_CODER_MODEL`, `SYNESIS_CODER_MAX_RETRIES`, `SYNESIS_CODER_TEMPERATURE`,
  and rate limiting limits

**`.gitignore`**
- `.env` and variants excluded (except `.env.example`)
- Python build artifacts: `__pycache__`, `*.pyc`, `.eggs`, `dist`, `build`, `.venv`

#### Tests

**`tests/test_item_mode.py`** — 17 tests using real projects from `d:/GitHub/case-studies/`:

*`TestProjectLoader` (6 tests — no LLM required):*
- `test_load_social_acceptance` — full template with GUIDELINES, ORDERED,
  ENUMERATED, SCALE, CHAIN
- `test_load_thompson_no_ontology_scope` — template without ONTOLOGY scope
- `test_load_nave` — template without CHAIN field
- `test_load_aids_corpus` — template with CHAIN and Portuguese relations, no GUIDELINES
- `test_code_index_populated` — projects with existing `.syn` populate `code_index`
- `test_project_not_found_raises` — `FileNotFoundError` for invalid path

*`TestPromptBuilder` (6 tests — no LLM required):*
- `test_prompt_structure` — system (cacheable) + user (dynamic) messages
- `test_system_prompt_contains_project_description` — DESCRIPTION block injected
- `test_system_prompt_contains_field_instructions` — ITEM fields listed
- `test_system_prompt_contains_chain_relations` — CHAIN relations included
- `test_user_message_contains_bibref_and_text` — bibref and text in user message
- `test_prompt_no_ontology_scope` — works correctly without ONTOLOGY scope

*`TestItemModeIntegration` (5 tests — require `ANTHROPIC_API_KEY`):*
- `test_item_social_acceptance_compiles` — output compiles for complex template
- `test_item_thompson_no_ontology_scope` — item mode works without ONTOLOGY scope
- `test_item_aids_corpus_compiles` — template with Portuguese relations
- `test_item_verbose_format` — status header present in verbose format
- `test_item_synesis_init_project` — compatibility with `synesis init` projects

#### Architectural decisions

- **Total compiler coupling**: all template, project, bibliography, and annotation
  reads go through `synesis.load()` — compiler updates are absorbed automatically
- **Dynamic templates**: no field name, scope, or relation is hardcoded — everything
  derived from `result.template.field_specs` at runtime
- **GUIDELINES as primary instruction**: `guidelines` > `description` > generic
  instruction by `FieldType`
- **DESCRIPTION via compiler**: `result.linked_project.project.description`
  instead of regex over `project_content`
- **`OrphanItem` ignored in item mode validation**: isolated ITEM has no SOURCE
  in the same file — filtered in `_has_structural_errors()`
- **`code_index` for CHAIN-only projects**: combines `code_usage` (CODE fields)
  with nodes from `all_triples` (CHAIN fields)
- **`load_ontology=False` default**: prevents errors in projects whose `.syno`
  references fields absent from the template (thompson_bible case)
- **`.bib` always loaded**: required for compiler validation regardless of
  `load_annotations` flag
- **Output cleaning pipeline**: `_strip_markdown_fences` → `_extract_item_blocks`
  → validation → correction loop
- **Prompt caching from item mode**: system prompt built once per session,
  marked with `cache_control: ephemeral`

---

[0.0.1]: https://github.com/usuario/synesis-coder/releases/tag/v0.0.1
