# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

---

## [0.7.0] — 2026-07-05

### Added

- **Modo `refine` — re-extração com feedback (Fase R do pipeline ACT)** (`modes/refine_mode.py` *(novo)*, `prompt_builder.py`, `modes/critique_mode.py`, `cli.py`, `.env.example`)
  - Loop opt-in Self-Refine/Reflexion: para cada ITEM suspeito, o crítico aponta o erro e o **gerador** reescreve a anotação raciocinando de novo sobre o texto-fonte — em vez de aplicar mecanicamente o palpite do crítico (como o `incorporate`).
  - **Cláusulas de segurança** embutidas no loop: não-regressão estrita (só aceita versão com `suspicion_score` menor), `MAX_ITER` rígido, detecção de ponto-fixo/oscilação (histórico normalizado), validação estrutural obrigatória via `validate_and_fix_async`, e crítico ≠ gerador (clients/modelos distintos) contra viés de auto-validação.
  - **Rastreabilidade**: o `.syn` final traz cabeçalho `# $metrics.refine.*` com métricas agregadas (com fórmulas) e o trace de score por iteração por ITEM (`# $refine.@bibref.trace: 0.62 -> 0.18`).
  - **Aditivo**: reaproveita critique, validação, obtenção de source-text e assembler como biblioteca. Extraído `_critique_tags`/`_score_of` de `critique_mode` (sem mudança de comportamento do modo `critique`).
  - **CLI**: subcomando `synesis-coder refine` com `--max-iter`, `--threshold`, `--critique-model`, `--refine-model`, `--thinking-budget`, `--overwrite`/`--backup`, e guarda de I/O que impede sobrescrever a fonte por acidente.
  - **Config**: `SYNESIS_CODER_REFINE_MODEL` (gerador) e `SYNESIS_CODER_REFINE_MAX_ITER`; crítico reusa `SYNESIS_CODER_CRITIQUE_MODEL`; limiar reusa `SYNESIS_CODER_SUSPICION_THRESHOLD`.
  - **Novos prompts** puros: `build_item_refinement_prompt` (texto-livre) e `build_item_refinement_values_prompt` (caminho JSON), reusando o system prompt de valores para preservar o `cache` das GUIDELINES.

---

## [0.6.2] — 2026-06-16

### Added

- **Flags `--overwrite` e `--backup` em todos os modos geradores** (`cli.py`, `synr_io.py` *(novo: `safe_write_output`)*, `modes/document_mode.py`, `modes/ontology_mode.py`, `modes/incorporate_mode.py`, `modes/finetune_mode.py`)
  - Antes, qualquer modo sobrescrevia o arquivo de saída silenciosamente.
  - `--overwrite`: sobrescreve sem confirmação (útil em scripts/CI).
  - `--backup`: cria cópia `.bak` do arquivo existente antes de gravar.
  - Sem `--overwrite`: em TTY pergunta ao usuário; em não-TTY (CI/pipe) aborta com mensagem clara.
  - `safe_write_output(output_path, content, overwrite, backup)` em `synr_io.py`: escrita atômica via `tempfile.mkstemp` + `os.replace()` — arquivo nunca fica truncado em caso de Ctrl-C ou crash. Centraliza R2+R3+R4 para todos os modos.
  - Modo `ontology`: `--update` continua implicando sobrescrita intencional (`overwrite=update or overwrite`).

- **Tolerância a erros de anotação pré-existente no modo `document`** (`project_loader.py`, `modes/document_mode.py`)
  - Resolução do "deadlock de regeneração": o arquivo `.syn` de saída é referenciado em `INCLUDE ANNOTATIONS` do `.synp`, portanto `load_project` o compilava antes que a extração pudesse começar. Se o `.syn` usava um template antigo, o compilador abortava antes de qualquer chamada LLM — impossível regenerar o arquivo que o comando deveria substituir.
  - `load_project(tolerate_annotation_errors=True)`: erros cujo `location.file` aponta para um arquivo `.syn` são emitidos como warnings em vez de abortar. Erros de template, `.synp` ou bib continuam sendo fatais.
  - `_split_and_tolerate_errors()`: classifica cada erro por origem, agrega os tolerados com `Counter` e emite um `[WARN]` com bullet por categoria (`Nx mensagem`). Erros fatais reconstroem um `ValidationResult` parcial e abortam normalmente.

### Changed

- **Apresentação de console completamente reformulada** (`cli.py`, `runtime_info.py`, `modes/document_mode.py`, `project_loader.py`, `synr_io.py`)
  - Cabeçalho do produto impresso uma vez por invocação em stderr antes de qualquer log:
    ```
    SYNESIS CODER (v0.6.2) | Core (v0.5.7)
    Extraction engine for generating valid annotations in the Synesis ecosystem.
    The template defines all fields, relations, and constraints — nothing is hardcoded.
    ```
  - Rótulos de log padronizados: `[INFO]`, `[WARN]` (era `[WARNING]`), `[ERROR]`, `[OK]` *(novo — nível 21)*, `[DEST]` *(novo — nível 22)*.
  - `_BracketFormatter` em `_configure_logging`: formata `[LABEL] mensagem` sem nome do módulo; suprime `WARNING` em favor de `WARN`.
  - `[INFO] Motor: backend/model | JSON assembler` — banner de LLM compacto (era uma linha longa com versões e dica).
  - `[INFO] Origem: arquivo.md (94k chars, 12 chunks, −1% após limpeza)` — novo label de progresso.
  - `[WARN] Ignorando anotações anteriores (N erros):\n       - Nx mensagem` — erros tolerados agregados por tipo com bullet indentado.
  - `[INFO] Processando: [████████████] 12/12 chunks (0 falhas)` — barra de progresso preenchida in-place (era grade `[1][2][3]...`). Suprimida em não-TTY e com `-v`.
  - `[PROMPT] arquivo.syn já existe. Sobrescrever? [y/N]:` — prompt de confirmação com label explícito via stderr.
  - `[OK] Validação concluída. N itens únicos extraídos (de M totais) em Xs.` — resultado final.
  - `[DEST] D:\caminho\para\lattes.syn` — destino do arquivo gravado.
  - Modo `plain`: stdout silencioso (return `""`). Modo `verbose`: retorna header com metadados.

### Added (banner)

- **Banner de runtime no início de cada execução com LLM** (`runtime_info.py` *(novo)*, todos os modos com LLM)
  - `runtime_banner(llm_client, format)` emite uma linha única e legível por
    pesquisador não-técnico informando: versão do `synesis-coder`, versão do
    compilador `synesis`, backend/modelo LLM em uso e — crucialmente — se o
    caminho ativo é **JSON assembler (determinístico)** ou **texto-livre (regex)**.
    O caminho determinístico (Opção 3) só ativa quando o backend suporta
    `response_format json_schema` (`supports_json_schema()` → backend `openai`);
    no backend padrão `anthropic` o coder cai em texto-livre, antes sem nenhum
    sinal visível. Quando aplicável, a linha sugere `SYNESIS_CODER_BACKEND=openai`.
  - Emitido via `logger.info` (stderr na CLI), nunca em stdout — preserva o
    `.syn` cru do formato `plain` e respeita `-q`/`-qq`.
  - Chamado em `item`, `document`, `abstract`, `suggest`, `ontology`, `critique`
    e `finetune`. `incorporate` é determinístico (sem LLM) e não emite banner.
  - `tests/test_runtime_info.py` *(novo)*: 6 casos (4 infos presentes, rótulo de
    caminho conforme `supports_json_schema()`, dica só no anthropic, emissão via logger).

### Changed

- **Logging centralizado na CLI agora cobre TODOS os modos** (`modes/critique_mode.py`, `modes/finetune_mode.py`, `modes/normalize_mode.py`, `modes/ontology_mode.py`)
  - A migração anterior (ver abaixo) removeu `basicConfig(force=True)` apenas de
    `document` e `abstract`. Os modos `critique`, `finetune`, `normalize` e
    `ontology` ainda o chamavam com `level=INFO` fixo e `force=True`, **derrubando**
    a configuração da CLI: `-v`/`-q` eram ignorados e o ruído de `httpx`/`openai`/
    `anthropic` voltava. Agora todos delegam o logging a `_configure_logging`.
  - `critique --debug` preserva a elevação para `DEBUG` via
    `logging.getLogger().setLevel(DEBUG)`, sem reinstalar handlers.
  - `finetune` perdeu também o loop redundante de silenciamento de loggers de
    terceiros (a CLI já o faz, e o loop ignorava `-v`).
  - `tests/test_logging_centralized.py` *(novo)*: teste-guarda que falha se
    qualquer modo reintroduzir `basicConfig`.

- **Saída de console minimalista nos modos `document` e `abstract`** (`modes/document_mode.py`, `modes/abstract_mode.py`, `cli.py`)
  - Logs de bibliotecas de terceiros silenciados na saída padrão — `httpx`/`httpcore`/`openai`/`anthropic`/`urllib3` ficam em `WARNING` (eliminam a enxurrada de `HTTP Request: POST ...`, uma por chamada LLM). Voltam com `-v`.
  - Logs de progresso individual (chunk por chunk, referência por referência, batch headers, "Escrito:", cooldown) rebaixados de `INFO` para `DEBUG` — só aparecem com `-v`.
  - Modo `document`: barra de progresso compacta `[1][2][3]…` que se preenche conforme os chunks concluem (chunks com falha marcados com `✗`). Renderizada in-place só em TTY; suprimida em pipes/redireções e quando `-v` está ativo.
  - Mensagem de inicialização consolidada (`Inicializando… arquivo.md (94k chars, 12 chunks, −1% após limpeza)`) e linha `Concluído! N itens extraídos → M únicos em Xs`.
  - Sumário final reformatado com separadores e alinhamento de colunas.
  - `basicConfig(force=True)` removido dos modos — configuração de logging centralizada na CLI. Com `-v`, o formato inclui timestamp e nome do módulo; sem `-v`, apenas `[LEVEL] mensagem`.

- **Mensagens de diagnóstico compactas para o usuário pesquisador** (`project_loader.py`, `modes/document_mode.py`)
  - Erros de compilação exibidos ao usuário agora usam `get_diagnostics(verbose=False)`: uma linha por erro, avisos `UndefinedCode` agrupados por código com contagem de ocorrências e dica `synesis-coder ontology`.
  - O caminho do LLM (auto-correção em `validator.py`) continua usando `verbose=True` (mensagens pedagógicas completas), preservando a qualidade da auto-correção.
  - Caso típico: saída reduzida de ~500 linhas para ~10 linhas.

- **Bloco SOURCE do modo `document` migrado para JSON + assembler** (`modes/document_mode.py`, `prompt_builder.py`)
  - `_generate_source_block` agora usa o mesmo caminho determinístico já adotado para ITEMs e pelo modo `abstract`: `build_source_schema` → `call_json_async` → `assemble_source`. A moldura é montada em Python (indentação canônica, separadores, `NA` por construção), eliminando de raiz os erros de extração por regex, indentação inconsistente e campo REQUIRED ausente no SOURCE.
  - Novo `build_document_source_values_prompt` e `_build_values_system_prompt(scope="source")` no `prompt_builder`.
  - Caminho de texto livre preservado como fallback (extração por regex agora tolerante a indentação, texto explicativo antes/depois e whitespace em `END SOURCE`; com dedent automático via `_dedent_block`).

### Fixed

- **Extração frágil do bloco SOURCE no fallback de texto livre** (`modes/document_mode.py`)
  - A regex exigia `SOURCE`/`END SOURCE` no início absoluto da linha; quando o LLM indentava o bloco ou o precedia de explicação, a extração falhava e caía para o SOURCE mínimo. Agora tolera indentação (com dedent), caixa, whitespace variável em `END SOURCE` e texto ao redor. O warning de fallback passou a registrar os primeiros 200 chars da resposta para depuração.

- **Indentação inconsistente no SOURCE corrigido pelo modo `document`** (`modes/document_mode.py`)
  - `_patch_required_source_fields` agora detecta a indentação dos campos já presentes no bloco (via `_detect_block_indent`) e a replica ao inserir `campo: NA`. Antes inseria 4 espaços fixos; quando o LLM usava 2 espaços, o Indenter da gramática aninhava o campo (`_INDENT` extra) e ele sumia do SOURCE, reaparecendo como `Campo obrigatorio ausente`.

- **Campos REQUIRED ausentes no SOURCE gerado pelo modo `document`** (`modes/document_mode.py`)
  - `_generate_source_block` agora chama `_patch_required_source_fields` tanto no caminho do LLM quanto no fallback mínimo: campos REQUIRED omitidos pelo LLM recebem `campo: NA`, evitando erro de compilação `Campo obrigatorio ausente no bloco SOURCE`.
  - Espelha o comportamento já existente no `block_assembler` para blocos ITEM.

- **Normalização de case em códigos gerados pelo LLM** (`block_assembler.py`)
  - `_render_code` agora converte tokens CODE para lowercase antes de emitir, alinhando com `normalize_code()` do compilador Synesis.
  - `_normalize_concept` (CHAIN) também aplica lowercase, evitando que variantes como `Graduacao_Curso` e `graduacao_curso` apareçam como dois códigos distintos no relatório e no `code_index`.
  - 2 novos testes em `test_block_assembler.py` cobrem os casos de normalização.

---

## [0.6.1] — 2026-06-15


### Added

- **Caminho JSON + assembler no modo `abstract`** — os três modos de anotação
  (`item`, `document`, `abstract`) usam agora o caminho JSON por padrão.
  - `schema_builder.py`: `build_abstract_schema(ctx)` — envelope combinado
    `{"source": {...}, "items": [...]}` gerado a partir dos campos SOURCE e ITEM
    do template. `additionalProperties: false` em todos os níveis.
  - `prompt_builder.py`: `build_abstract_values_prompt(ctx, bibref, abstract)` /
    `_build_abstract_values_system_prompt(ctx)` — contrato JSON com `"source"` e
    `"items"` como chaves obrigatórias; reutiliza seções de GUIDELINES e índices;
    omite seção de formato de bloco.
  - `abstract_mode.py`: `_generate_abstract_syn(ctx, bibref, abstract, llm_client,
    context)` — caminho JSON com `call_json_async → assemble_source + assemble_items`;
    fallback automático para `build_abstract_prompt + call_async`.
  - `tests/test_abstract_mode.py`: `TestAbstractSchema` (4 casos), `TestAbstractValuesPrompt`
    (5 casos), `TestAssembleAbstractFromData` (2 casos, incluindo compilação real).

---

## [0.6.0] — 2026-06-15

### Added

- **Pré-validação de bibref com abort precoce** — elimina o erro dominante E001
  (bibref inexistente no `.bib`) antes de gastar qualquer chamada LLM.
  - `ctx["bib_keys"]` em `project_loader.py`: lista ordenada das chaves do `.bib`
    parseado pelo compilador, sem reparse adicional.
  - `assert_bibref_known(ctx, bibref)` *(novo)*: valida que o bibref (com ou sem `@`)
    existe em `bib_keys`; levanta `ValueError` com amostra das chaves disponíveis e,
    quando o `.synp` traz `DESCRIPTION`, cita a convenção do projeto.
  - `item_mode.py` e `document_mode.py`: guard chamado logo após `load_project`, antes
    de qualquer chamada LLM.
  - `tests/test_item_mode.py`: `TestAssertBibrefKnown` (4 casos) + `test_bib_keys_populated`.

- **Opção 3 — geração via JSON + assembler determinístico** — o LLM devolve apenas
  valores em JSON; o Python monta a moldura estrutural inteira (palavras-chave, nomes de
  campo, indentação, `@{bibref}`, setas `->` de chains). Elimina por construção E022
  (campo desconhecido), E033/E015 (separador de CODE), E008/E010/E011 (sintaxe de chain)
  e fences Markdown. Ativo por padrão no backend openai-compat, com fallback automático.
  - `schema_builder.py` *(novo)*: `FieldSpec` → JSON Schema. CODE→array, ENUMERATED/
    ORDERED→`enum`, SCALE→integer com min/max de `[lo..hi]`, CHAIN→array de hops
    `{source, relation, target}` com `relation` por enum. `additionalProperties:false`.
  - `block_assembler.py` *(novo)*: dict de valores → texto Synesis. CODE→`", "`,
    CHAIN hops→`A -> rel -> B` com interleave de hops contíguos e snake_case;
    campos REQUIRED ausentes → `NA`; OPTIONAL ausentes omitidos; chaves extras ignoradas.
  - `llm_client.py`: `call_json` / `call_json_async` (retornam `None` para acionar
    fallback); `supports_json_schema()`; `response_format: json_schema` em `create_kwargs`;
    `_parse_json_response()` tolera fences de markdown.
  - `prompt_builder.py`: `build_item_values_prompt` / `build_document_values_prompt` —
    prompts sem seção de formato de bloco. Fallback de CODE menciona vírgula.
  - `item_mode.py` / `document_mode.py`: caminho `call_json → assembler → validate_and_fix`,
    com fallback para texto livre.
  - Testes: `test_schema_builder.py`, `test_block_assembler.py`, `test_call_json.py` *(novos)*.

- **NA fallback para campos REQUIRED ausentes** — quando o LLM omite ou deixa vazio um
  campo obrigatório, o assembler emite `campo: NA` em vez de omiti-lo, garantindo
  conformidade estrutural sem retry adicional.
  - `block_assembler.py`: `_assemble_block` recebe `required: set`; `assemble_items` /
    `assemble_source` passam `set(ctx["required_item"])` / `set(ctx["required_source"])`.
  - `tests/test_block_assembler.py`: 3 novos casos (`test_required_absent_field_gets_na`,
    `test_required_empty_string_gets_na`, `test_optional_absent_never_gets_na`).

- **Filtragem de ruído pré-chunking** (`text_cleaner.py`) — saneamento de documentos
  longos antes do envio ao LLM. Quatro camadas em ordem:
  1. Seções ATX com marcador de ausência (`Não informado.`, `Nenhum item cadastrado.`,
     `N/A.`) — cabeçalho + marcador removidos.
  2. Boilerplate Lattes/CNPq: rodapé de geração, endereço de CV, data de atualização.
  3. Paginação (`Página X de Y`) e separadores visuais (`----`, `____`).
  4. Espaços/tabs múltiplos → espaço único; `\n{3+}` → `\n\n`.
  - `text_cleaner.py` *(novo)*: `clean_document(text) → str` (stateless, idempotente).
  - `document_mode.py`: `clean_document` chamado após `read_document`, antes de
    `split_into_chunks`; log reporta redução percentual; `input_chars` no debug recorder
    reflete o tamanho pós-limpeza.
  - `tests/test_text_cleaner.py` *(novo)*: 18 testes (cada camada, idempotência,
    preservação de conteúdo real).

### Notes

- O modo `abstract` permanece no caminho de texto livre (envelope JSON combinado
  SOURCE+ITEM fica como follow-up). Os modos `item` e `document` usam o caminho JSON.
- Reinjeção/revalidação do SOURCE no loop de correção por chunk (E020/E022): fora de
  escopo, registrado para follow-up.
- Impacto do `text_cleaner` depende da origem do documento. O conversor Lattes utilizado
  neste projeto já entrega Markdown limpo (~1% de redução medida em documento real de
  95 k chars). Efeito maior em exportações HTML→Markdown genéricas.

---

## [0.5.0] — 2026-06-14

### Added

- **Flag `--debug` no modo `document`** — gera um relatório Markdown de auditoria
  do pipeline LLM ao lado do `.syn` de saída (`<projeto>_<bibref>_debug.md`),
  legível por pesquisadores não-técnicos.
  - **`synesis_coder/debug_log.py`** *(novo)*:
    - `DebugRecorder` — acumulador thread-safe de eventos (chamadas LLM, ciclos
      de validação, correções). Contexto por chunk propagado via `threading.local`
      (mesmo padrão de `_correction_local`), pois as chamadas LLM rodam em threads
      worker via `asyncio.to_thread`. Quando ausente (sem `--debug`), overhead zero.
    - `render_markdown()` / `write(path)` — relatório cronológico: cabeçalho da
      sessão → bloco SOURCE → por trecho (system prompt com GUIDELINES, user
      message, resposta bruta, latência, tokens) → verificação do compilador
      tentativa a tentativa (✅/🔴) com correções → resumo final.
    - `classify_error(err)` → `"structural" | "value" | "other"` (mesma taxonomia
      da Opção 0 do `Estudo_Reducao_Tokens`).
    - `translate_diagnostics(result)` — converte erros do compilador em frases
      amigáveis reaproveitando `to_cli_line()`, com o código técnico
      (`SYNESIS_E0xx`) anexado como nota secundária. Ignora `OrphanItem`.
  - **`synesis_coder/llm_client.py`**: `LLMClient.__init__` aceita `recorder=None`.
    `_call_sync_inner` mede latência e emite evento bruto (system/user/resposta/
    tokens/params resolvidos) apenas quando há recorder. `call_async`/`fix_async`
    aceitam `context` e o setam dentro da thread worker.
  - **`synesis_coder/validator.py`**: `validate_and_fix_async` aceita
    `recorder`/`context` e registra cada tentativa de validação e correção.
  - **`synesis_coder/modes/document_mode.py`**: cria o recorder quando `debug=True`,
    propaga contexto por chunk, registra header/footer e grava o relatório.
  - **`synesis_coder/cli.py`**: opção `--debug` no comando `document`.

- **Flag `--debug` no modo `abstract`** — gera `<projeto>_abstract_debug.md` no
  diretório de saída, reaproveitando o `DebugRecorder`. O recorder foi
  generalizado para unidades arbitrárias de processamento: `DebugRecorder(
  unit_type, unit_label, coding_step_title)` permite que a mesma estrutura
  renderize "Trecho N de M" (document) ou "Referência N de M — @bibref"
  (abstract). O contexto de cada entrada é `("entry", índice, total, bibref)`;
  o bibref aparece no título da seção.
  - **`synesis_coder/modes/abstract_mode.py`**: `process_abstract`/
    `_process_one_abstract`/`_process_batch` aceitam `debug`/contexto; criam o
    recorder, propagam o índice global da entrada entre batches e gravam o relatório.
  - **`synesis_coder/cli.py`**: opção `--debug` no comando `abstract`.

### Changed

- **Relatório de debug não trunca mais o conteúdo** — instruções de sistema
  (com GUIDELINES), mensagens do usuário (documento/abstract) e respostas da IA
  são exibidas na íntegra. O truncamento anterior (`… (truncado para
  legibilidade)`, limites de 1200/1500/2000 chars) impedia o pesquisador de
  auditar exatamente como o prompt foi montado e o que o documento entregou —
  justamente o propósito da flag. Removidos `_truncate()` e `SOURCE_PREVIEW_CHARS`.

### Fixed

- **Bloco SOURCE ausente no relatório de debug do modo `document`** — o evento
  da chamada SOURCE recebia `phase="chunk"` (em vez de `"source"`) porque o
  contexto era setado via `set_context()` na thread do event-loop, invisível à
  thread worker do `asyncio.to_thread`. Agora `_generate_source_block` passa
  `context=("source",)` diretamente a `call_async`, e a "Etapa 1 — Geração do
  bloco SOURCE" volta a aparecer.

- **`tests/test_debug_log.py`** — além dos testes do modo document, cobre a
  unidade `entry` (rótulos "Referência", exibição do bibref, cabeçalho/rodapé
  adaptados) e a renderização ordenada das entradas.

## [0.4.2] — 2026-06-12

### Added

- **Verbosity flags `-v`/`-q` on `synesis-coder` CLI** (`synesis_coder/cli.py`)
  - `-v` / `--verbose` (count): raises log level to DEBUG. Repeatable.
  - `-q` / `--quiet` (count): lowers to WARNING (`-q`) or ERROR (`-qq`). Repeatable.
  - Both options added to `main` group; wired through `_configure_logging(verbose, quiet)`.
  - Distinct from `--format` (which controls output *style* — plain vs. verbose token usage); `-v/-q` controls Python logging only.
  - `-v, --verbose` and `-q, --quiet` added to `Global Options:` block in `_build_main_help()`.

## [0.4.1] — 2026-06-11

### Added

- **Quality toolchain and CI** (`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`)
  - `ruff==0.15.17` and `mypy==1.15.0` added to `dev` extras (pinned, in sync with ecosystem).
  - `[tool.ruff]`: `line-length=100`, `target-version="py310"`; rules `["E","F","I","UP","B","SIM","C4"]`.
  - `[tool.mypy]`: `ignore_missing_imports=true`, `disallow_untyped_defs=false`.
  - `.pre-commit-config.yaml`: `ruff` (lint + `--fix`), `ruff-format`, `mypy`, standard file-hygiene hooks.
  - CI workflow (3 OS × 3 Python): `test` (pytest, skips `integration` marker), `lint`, `build`, `integration` (`synesis-coder --help/--version`).

- **`synesis>=0.5.5` constraint** (`pyproject.toml`)
  - Updated from `>=0.3.0`; aligns with the compatibility matrix documented in the README.

- **CLI snapshot tests** (`tests/test_cli.py`)
  - Structural anchor assertions on `--help` output and `--version` — regression guard for CLI changes.

### Changed

**`synesis_coder/cli.py`**
- **CLI fully translated to English and aligned with the synesis compiler pattern.**
  - `_build_main_help()`: all user-facing strings translated to English — title, description, group labels, command summaries, option descriptions, and footer hint.
  - Group labels renamed: "Ingestão & Extração" → "Ingestion & Extraction", "Estruturação & LLM" → "Structuring & LLM", "Pipeline ACT (Revisão e Consolidação)" → "ACT Pipeline (Review & Consolidation)".
  - Phase tags translated: "[Fase N]" → "[Phase N]" in critique / normalize / incorporate summaries.
  - Section header renamed: "Opções Globais:" → "Global Options:", "Comandos:" → "Commands:", "Uso:" → "Usage:".
  - Footer hint: "Execute 'synesis-coder COMANDO --help'…" → "Run 'synesis-coder COMMAND --help'…".
  - `_ex()`: "Exemplos:" → "Examples:"; all inline comments and example paths translated.
  - All nine epilogs (`_EPILOG_ITEM` … `_EPILOG_INCORPORATE`) rewritten in English with paths matching the English terminology (e.g. `annotations/`, `revisions/`).
  - All subcommand `help=` strings and docstrings translated to English.
  - `_SynesisGroup.get_help()` now writes via `sys.stdout.buffer` with explicit UTF-8 encoding (matching the synesis compiler fix), preventing character corruption on Windows terminals when `--help` is passed.

---

## [0.4.0] — 2026-06-11

### Added

**`synesis_coder/modes/document_mode.py`**
- **Semantic Chunking (structure-aware)**: `split_into_chunks` agora detecta automaticamente documentos com estrutura Markdown (≥2 cabeçalhos ATX `#`…`######`) e usa o novo modo semântico antes de cair no algoritmo size-based existente.
  - `_ATX_HEADER`: regex compilada de escopo de módulo para detecção de cabeçalhos.
  - `_has_markdown_structure(text, min_headers=2)`: retorna `True` se o texto tem pelo menos `min_headers` cabeçalhos ATX. Threshold de 2 evita tratar documentos com título único (sem subdivisão real) como estruturados.
  - `_parse_markdown_sections(text)`: divide o texto em `(header_line, section_text)` por cabeçalho ATX; preâmbulo antes do primeiro cabeçalho vira seção com `header_line` vazia.
  - `_split_by_headers(text, chunk_size, overlap)`: empacota seções consecutivas até `chunk_size`; seções maiores que o teto são subdivididas via `_split_by_sentences` com o cabeçalho da seção replicado como prefixo de contexto em cada subchunk.
- **Degradação graciosa**: documentos sem cabeçalhos (entrevistas `.txt`, texto corrido) continuam usando o algoritmo size-based (parágrafo → sentença) sem nenhuma alteração no comportamento.
- Interface `split_into_chunks(text, chunk_size, overlap)` **inalterada** — nenhuma quebra de CLI, API ou integração.

**`tests/test_document_mode.py`**
- `TestHasMarkdownStructure` (5 casos): cobertura de `_has_markdown_structure`.
- `TestParseMarkdownSections` (4 casos): parse em seções, preâmbulo, ausência de cabeçalhos.
- `TestSplitByHeaders` (5 casos): agrupamento de seções pequenas, subdivisão de seção gigante, dispatch semântico, preservação de conteúdo, fallback size-based.
- `test_fallback_for_text_without_headers`: regressão — texto corrido produz resultado coerente via fallback.

### Changed

- Docstring do módulo `document_mode.py` atualizada para descrever modo semântico + fallback.

---

## [0.3.3] — 2026-06-11

### Fixed

**`synesis_coder/llm_client.py`**
- **P1 — Detecção de truncamento**: `_call_sync_inner` agora inspeciona `finish_reason` (branch OpenAI) e `stop_reason` (branch Anthropic) após cada chamada LLM. Quando o modelo trunca a resposta por limite de tokens, emite `WARNING` com o valor de `max_tokens` usado. Antes, truncamentos eram silenciosos — o chunk retornava um bloco ITEM cortado no meio sem nenhuma indicação.
- **P1-bis — `max_tokens` dinâmico**: introduzida precedência de três camadas para `max_tokens`:
  1. `SYNESIS_CODER_MAX_TOKENS` (env) — vence tudo
  2. `min(teto_via_API, estimativa_por_chunk)` — dinâmico: `_estimate_max_tokens()` calcula `len(chars) / 4 × 1.2` com piso `_DEFAULT_MAX_TOKENS`; `_discover_model_output_cap()` consulta o teto do modelo via API (lazy, cacheado)
  3. Valor explícito do chamador (ex.: `suggest_mode` com 512)
  Corrige também um bug em que `SYNESIS_CODER_MAX_TOKENS` era ignorado quando `thinking=False` (agora o override de env se aplica a todos os modos).
- **`_DEFAULT_MAX_TOKENS = 4096`**: literal promovido a constante nomeada nas quatro assinaturas públicas (`call`, `fix`, `call_async`, `fix_async`) — sem quebra de interface.

**`synesis_coder/modes/document_mode.py`**
- **P3 — `_item_signature` sem hardcode**: o campo de quotation buscado para a assinatura de deduplicação era hardcoded como `text:`, que não existe no template lattes (usa `trecho:`). A assinatura retornava só chains → deduplicação excessiva (51→26 ITEMs com 60% overlap). Corrigido: `_item_signature(item_text, quotation_field=None)` recebe o nome real do campo derivado do `ctx["item_fields"]`.
- **P3 — deduplicação exata**: `merge_and_dedup` substituiu o threshold de 60% de overlap por igualdade exata de `frozenset`. ITEMs com chains distintas mas com alguma sobreposição não são mais descartados. Resultado imediato: 53 ITEMs extraídos → 53 ITEMs após deduplicação (zero perdas), vs. 26 ITEMs na versão anterior para o mesmo documento.

---

## [0.3.2] — 2026-06-10

### Changed

**`synesis_coder/llm_client.py`**
- `_wait_honoring_retry_after()` — nova função de espera usada pelos dois decoradores `@retry` (backends Anthropic e OpenAI-compat). Quando a API devolve um erro 429 com header `Retry-After`, aguarda exatamente o tempo indicado em vez de calcular backoff exponencial cego. Fallback: mantém o `wait_exponential(multiplier=2, min=4, max=60)` original quando o header está ausente. Comportamento sem 429 é byte-a-byte idêntico ao anterior.
- Os dois decoradores `@retry` em `_call_sync_inner` (branch OpenAI linha ~397 e branch Anthropic linha ~452) substituem `wait=wait_exponential(...)` por `wait=_wait_honoring_retry_after`. O controle reativo de `Retry-After` agora cobre ambos os backends; o sleep proativo por janela de tokens continua exclusivo do backend Anthropic.

---

## [0.3.1] — 2026-04-25

### Fixed

**`synesis_coder/modes/incorporate_mode.py`**
- `_META_TAGS` ampliado: adicionados `"note"`, `"reason_detail"` e `"phase"`. O LLM de critique usava `# $note:` como raciocínio livre; anteriormente `incorporate` tentava substituir o campo `note:` do ITEM com esse texto.
- `_replace_field_value`: quando um ITEM tem múltiplos campos de mesmo nome (ex: vários `chain:` num ITEM complexo), a substituição agora faz match por **nó-fonte** — extrai o primeiro token antes de `->` da sugestão e seleciona a ocorrência cujo valor atual começa com o mesmo nó. Antes, sempre substituía a primeira ocorrência, independente de qual chain o LLM endereçava.
- `_apply_revision_tags`: chaves numeradas geradas pelo parser de critique (`chain.1`, `chain.2`) são normalizadas para o nome de campo base antes da substituição.

**`synesis_coder/modes/critique_mode.py`**
- `_parse_critique_response`: quando o LLM emite múltiplos `# $chain:` (um por chain a corrigir num ITEM com vários campos chain), as ocorrências adicionais são armazenadas com sufixo numérico (`chain.1`, `chain.2`) em vez de sobrescrever a chave anterior.

**`synesis_coder/prompt_builder.py`**
- `_build_critique_output_format`: introduzido `# $reason_detail:` como tag explícita para explicações livres do LLM (substitui o uso incorreto de `# $note:`). Adicionadas instruções explícitas: (a) nunca emitir `# $note:`, (b) ao corrigir múltiplos chains num ITEM, manter o mesmo nó-fonte em cada `# $chain:` para identificação unívoca da ocorrência.

---

## [0.3.0] — 2026-04-25

### Added

**Pipeline ACT (Annotation with Critical Thinking) — 4 fases**

Implementação das Fases 2, 3 e 4 do pipeline ACT descrito em `Estudo_Phases_Coder.md`.
A Fase 1 (Extração) era o comportamento pré-existente de `document` / `item`.

**`synesis_coder/synr_io.py`** *(novo)*
- Formato `.synr` — superconjunto sintático de `.syn`: comentários `# $key: value` e blocos `# REVISION` são ignorados pelo compilador Synesis (gramática inalterada)
- `SynrDocument` — dataclass com `header`, `content` e `item_revisions`
- `parse_synr(path)` — lê `.synr` ou `.syn`; extrai cabeçalho e blocos `# REVISION` por ITEM
- `write_synr(path, doc)` — persiste `SynrDocument` em disco
- `create_synr(syn_content, header, item_revisions)` — injeta blocos `# REVISION` no conteúdo `.syn` original sem alterar nenhum byte fora dos ITEMs afetados
- `extract_revision_tags(item_block)` — extrai tags `# $key: value` de um bloco ITEM
- `serialize_revision_block(tags)` — serializa dict de tags para formato `# REVISION`

**`synesis_coder/modes/critique_mode.py`** *(novo)*
- Subcomando `critique` — Fase 2 do pipeline ACT
- Por ITEM: invoca LLM configurado para critique (`SYNESIS_CODER_CRITIQUE_MODEL`) e avalia fidelidade textual dos campos ao abstract/fonte
- Items com `suspicion_score >= threshold` (padrão: 0.20) recebem bloco `# REVISION` no `.synr` gerado
- Concorrência via `asyncio.gather` com `Semaphore` configurável
- Prioridade de texto-fonte: abstract completo do `.bib` > campo `text` do ITEM > sentinel
- `_parse_critique_response()` aceita formato `# $key: value` (preferido) e `key: value` (fallback)

**`synesis_coder/modes/normalize_mode.py`** *(novo)*
- Subcomando `normalize` — Fase 3 do pipeline ACT
- Constrói inventário global de códigos cross-file: conceitos de campos `chain` e valores de campos `code`
- Normalização determinística: agrupa variantes pela chave `lowercase+underscore`; canonical = variante mais frequente (desempate: forma com underscore > ordem alfabética)
- LLM em chunks (`SYNESIS_CODER_NORMALIZATION_MODEL`) para grupos residuais com ≥2 variantes após normalização determinística
- Aceita sugestões LLM com `merge_confidence >= confidence_threshold` (padrão: 0.65)
- `_substitute_code_in_chain()` — substituição token-a-token preservando relações (`ENABLES`, `INFLUENCES`, etc.)
- `_write_inventory_txt()` — inventário de códigos em TXT com contagens e canonical por grupo
- Emite um `.synr` por arquivo de entrada com blocos `# REVISION` contendo `# $chain:` e/ou `# $code:`

**`synesis_coder/modes/incorporate_mode.py`** *(novo)*
- Subcomando `incorporate` — Fase 4 do pipeline ACT (determinístico, sem LLM)
- Aplica tags `# $<field>:` por ITEM com validação sintática via `synesis.load()` antes de cada substituição; rejeita com rollback e warning se a substituição quebrar a compilação
- Remove todos os blocos `# REVISION` e linhas de metadados `# $key: value`
- Grava métricas no cabeçalho do `.syn` final: `fields_changed`, `fields_rejected`, `items_revised`, `ACS` (Annotation Change Score = changed / (changed + rejected)), `timestamp`, `source`
- `_validate_phase_env()` em `cli.py` — valida variáveis de ambiente por fase com fallback para `SYNESIS_CODER_MODEL` e mensagem instrucional quando ausentes

**`synesis_coder/prompt_builder.py`**
- `build_critique_prompt(ctx, item_block, source_text)` — prompt parametrizado pelos `FIELD` + `GUIDELINES` do template; inclui critérios por campo e guia de pontuação de suspeição (0.00–1.00)
- `build_normalization_prompt(ctx, code_groups)` — prompt para canonicalização semântica de grupos de códigos residuais; output estruturado com `# $group:`, `# $suggested_canonical:`, `# $merge_confidence:`

**`synesis_coder/cli.py`**
- `_validate_phase_env(phase_name)` — valida `SYNESIS_CODER_<PHASE>_MODEL` com fallback para `SYNESIS_CODER_MODEL`; verifica `ANTHROPIC_API_KEY` quando backend = anthropic
- Subcomando `critique` — `SYN_FILE`, `--project`, `--output`, `--concurrent`, `--threshold`, `--format`, `--model`
- Subcomando `normalize` — `SYNR_FILES...` (múltiplos), `--project`, `--output-dir`, `--concurrent`, `--confidence`, `--inventory`, `--format`, `--model`
- Subcomando `incorporate` — `SYNR_FILE`, `--project`, `--output`, `--format`
- Help principal atualizado com seção "PIPELINE ACT" e exemplos para cada novo subcomando
- Novos subcomandos exibidos em ciano no bloco `Commands:` para distinguir dos modos de extração (verdes)

**Testes**
- `tests/test_synr_io.py` — 30 testes: round-trip, parsing de tags, namespaces, integração com `synesis.load()`
- `tests/test_phase_env_validator.py` — 17 testes: precedência de variáveis, fallbacks, backends, mensagens de erro
- `tests/test_incorporate_mode.py` — 36 testes: substituição de campos, rejeição sintática, métricas ACS, integração com `synesis.load()`
- `tests/test_critique_mode.py` — 31 testes: extração de texto-fonte, parse de resposta LLM, fluxo com mock LLM, integração com compilador
- `tests/test_normalize_mode.py` — 44 testes: normalização de chave, extração de conceitos de chain, inventário cross-file, normalização determinística, parse LLM, geração de revisões

### Changed

- `synesis_coder/cli.py` — `_cmd_line()` reconhece `critique`, `normalize`, `incorporate` como nomes de subcomandos para colorização
- `synesis_coder/prompt_builder.py` — prompt de critique parametrizado pelos GUIDELINES do template (sem campos hardcoded)

---

## [0.2.0] — 2026-04-16

### Added

**`synesis_coder/llm_client.py`**
- `SYNESIS_CODER_THINKING_BUDGET` — ativa extended thinking (Anthropic Claude 4.x); 0 = desabilitado (padrão). Valores recomendados: 4000 leve / 8000 médio (face85) / 16000 pesado (ontology)
- `_get_thinking_budget()`, `_model_supports_thinking()`, `_THINKING_CAPABLE_MODELS` — detecta suporte ao thinking por modelo; emite aviso claro quando modelo incompatível, continua sem thinking
- `_get_env_temperature()` — `SYNESIS_CODER_TEMPERATURE` agora é lida e aplicada a todos os modos analíticos (`thinking=True`); antes era variável inerte
- `_get_max_tokens_override()` — `SYNESIS_CODER_MAX_TOKENS` agora é lida e aplicada a todos os modos analíticos; antes era variável inerte
- `call()` e `call_async()` aceitam `thinking_budget: int | None` — permite override pontual sem alterar o `.env`
- Bloco 1b no `.env` e `.env.example` com `claude-opus-4-7` (recomendado com `THINKING_BUDGET=8000`)

**`synesis_coder/prompt_builder.py`**
- Injeção de instrução `OUTPUT LANGUAGE` nos system prompts de item, abstract e ontology quando `output_language` está definido no contexto

**`synesis_coder/project_loader.py`**
- Lê `SYNESIS_CODER_LANGUAGE` do ambiente e expõe como `ctx["output_language"]`; `None` quando não definida (preserva comportamento v0.1.x)

**`synesis_coder/cli.py`**
- Flags `--thinking-budget INT`, `--language TEXT`, `--max-tokens INT`, `--temperature FLOAT` adicionadas aos comandos `item`, `abstract`, `document` e `ontology`
- Precedência: flag CLI > variável `.env` > default do modo
- Exemplos de extended thinking e `--language` adicionados ao help de `item`

### Changed

- `fix()` e `fix_async()` passam `thinking=False` — chamadas de correção não ativam extended thinking (economia de custo)
- Branch Anthropic de `_call_sync_inner` itera `response.content` por `block.type == "text"` em vez de acessar `content[0].text` diretamente — necessário para receber resposta correta quando `ThinkingBlock` precede o `TextBlock`
- `SYNESIS_CODER_TEMPERATURE` e `SYNESIS_CODER_MAX_TOKENS` passam a ter efeito real (antes documentadas mas inertes); `SYNESIS_CODER_MAX_RETRIES`, `MAX_RPM`, `MAX_INPUT_TPM` e `MAX_OUTPUT_TPM` já eram funcionais

### Fixed

- Documentação enganosa no `.env.example`: seção OPCIONAIS agora reflete corretamente quais variáveis são funcionais
- Mock de testes `_make_mock_anthropic_response` atualizado para `block.type = "text"` (compatível com nova iteração de content blocks)

---

## [0.1.5] — 2026-04-09

### Added

**`synesis_coder/modes/finetune_mode.py`** *(novo)*
- `process_finetune(output_path, project_path, input_path, enrich, concurrent, model, format)` — enriquece dataset Alpaca via LLM (Camada 2)
- Duas fontes de entrada mutuamente exclusivas: `--project` (compila e gera Camada 1 internamente via `build_alpaca_pairs()`) ou `--input` (carrega JSONL pré-gerado)
- `_quality_filter()` — descarta pares com instruction < 15 chars ou output < 10 chars; sempre aplicado antes do enriquecimento
- `_enrich_one()` — enriquece um par via LLM com concorrência controlada por `asyncio.Semaphore`
- Três tipos de enriquecimento (flag `--enrich`, repetível):
  - `vary` (padrão): paráfrase da instruction via LLM; aplicado a todos os pares; duplica aproximadamente o dataset
  - `didactic`: reformula chains como explicação pedagógica; apenas pares chain/causal
  - `counterfactual`: gera par "e se X fosse diferente?"; apenas pares chain/causal
- `_is_chain_pair()` — detecta pares chain/causal por palavras-chave na instruction
- `_parse_qa_response()` — extrai campos QUESTION/ANSWER de resposta estruturada do LLM
- `_deduplicate()` — remove pares com (instruction, input) idênticos após mescla
- Formato `verbose`: exibe fonte, tipos de enriquecimento, tokens e estatísticas

**`synesis_coder/cli.py`**
- Comando `finetune` adicionado ao grupo principal
- `--project PATH` / `--input PATH` (mutuamente exclusivos): fonte dos pares Alpaca
- `--output PATH` (obrigatório): destino do JSONL enriquecido
- `--enrich [vary|didactic|counterfactual]` (múltiplo, padrão `vary`)
- `--concurrent INTEGER` (padrão 5), `--format [plain|verbose]`, `--model TEXT`
- Help (`--help`) e seção "MODO finetune" no help global explicam as duas formas de uso e os três tipos de enriquecimento com exemplos comentados

---

## [0.1.4] — 2026-04-08

### Added

**`synesis_coder/token_usage.py`** *(novo)*
- `TokenUsage` — dataclass thread-safe que acumula `input_tokens`, `output_tokens`, `api_calls` e `corrections` ao longo de uma execução
- `record(input_tok, output_tok, is_correction)` — registra tokens de uma chamada com lock; `is_correction=True` incrementa `corrections`
- `summary_line()` — formata linha para o terminal: `tokens: in X,XXX | out X,XXX | total X,XXX | calls N [| corrections N]`
- `reset()` — reinicia todos os contadores

**`synesis_coder/llm_client.py`**
- `self.usage: TokenUsage` — acumulador de sessão, exposto publicamente; reflete todas as chamadas do cliente desde sua instanciação
- `self._correction_local: threading.local` — flag por thread para marcação de correções; garante segurança em modos concorrentes (`abstract`, `document`, `ontology`) onde `fix_async()` corre em threads separadas via `asyncio.to_thread()`
- `_record_usage()` — agora acumula em `self.usage` além das deques de rate-limiting existentes; lê e reseta o flag `_correction_local.is_correction`
- `fix()` — seta `_correction_local.is_correction = True` antes de delegar para `call()`
- `fix_async()` — usa wrapper `_fix_in_thread()` para setar o flag *dentro* da thread worker, evitando que o flag do event loop seja lido pela thread errada; o rate-limiting proativo permanece no event loop
- Branch OpenAI de `_call_sync_inner` — registra tokens em `self.usage` (anteriormente ignorava `response.usage`)

**`synesis_coder/modes/`** — todos os 5 modos
- Formato `verbose` exibe `# tokens: in X | out X | total X | calls N` no cabeçalho de saída
- Modos afetados: `item_mode.py`, `suggest_mode.py`, `abstract_mode.py`, `document_mode.py`, `ontology_mode.py`
- Formato `plain` preservado inalterado (compatibilidade com pipes e extensão VSCode)

**`tests/test_token_usage.py`** *(novo)*
- `TestTokenUsageRecord` (3): acumulação, `total_tokens`, flag `is_correction`
- `TestTokenUsageSummaryLine` (3): formatação com/sem correções, estado zerado
- `TestTokenUsageReset` (1): `reset()` zera todos os campos
- `TestTokenUsageThreadSafety` (1): 10 threads concorrentes sem race condition
- `TestLLMClientCorrectionFlag` (6): `call()` não marca correção; `fix()` marca; reset após uso; `fix_async()` marca; concorrência de `fix_async()` sem colisão de flags

### Changed

**`tests/test_item_mode.py`**, **`tests/test_document_mode.py`**
- Testes de formato verbose (`test_item_verbose_format`, `test_process_document_verbose_format`) verificam presença de `"tokens:"` no output

---

## [0.1.3] — 2026-04-06

### Added

**`synesis_coder/modes/suggest_mode.py`** *(novo)*
- `process_suggest(project_path, text, format, model)` — sugere códigos relevantes para um trecho de texto
- Fluxo adaptativo: dois passos (tópico → código) para projetos com > 100 códigos; passo único para projetos menores
- `_select_topics()` — passo 1: LLM identifica 2-4 tópicos relevantes dentre os disponíveis no projeto; fallback por frequência se resposta inválida
- `_build_enriched_code_list()` — filtra e enriquece a lista de códigos com frequência (`code_index["stats"]`) e descrição semântica (`ontology_index["ontology_description"]`); limita a 60 códigos por chamada
- `_postprocess()` — verifica sugestões e marca automaticamente `[NEW]` em códigos que não existem no projeto

**`synesis_coder/prompt_builder.py`**
- `build_topic_filter_prompt(available_topics, text)` — prompt mínimo para passo 1 (identificação de tópicos); ~130 tokens, temperatura 0.0
- `build_suggest_prompt(ctx, text, enriched_codes)` — prompt para sugestão de códigos; inclui contexto do projeto (truncado a 200 chars), lista enriquecida e formato de resposta bullet

**`synesis_coder/cli.py`**
- Subcomando `suggest`: `--project`, `--text`, `--format` (plain/verbose), `--model`
- Seção de exemplos do `suggest` adicionada ao help do CLI

### Changed

**`synesis_coder/cli.py`**
- Help do CLI atualizado com seção "MODO suggest" e exemplos

---

## [0.1.2] — 2026-04-04

### Added

**`synesis_coder/llm_client.py`**
- Suporte a backends OpenAI-compatíveis via `SYNESIS_CODER_BACKEND=openai`
- `SYNESIS_CODER_API_URL` — base URL do endpoint (Ollama local, RunPod, Together AI, etc.)
- `SYNESIS_CODER_API_KEY` — chave para APIs que exigem autenticação (Ollama: ignorada)
- Rate limiting desabilitado automaticamente no backend OpenAI (sem cotas externas)
- Retry adaptado por backend: `openai.APIStatusError` / `openai.APIConnectionError`
- `_translate_messages_openai()` — converte formato interno para OpenAI Chat Completions (campo `cache` ignorado silenciosamente)
- `_translate_messages_anthropic()` — código anterior renomeado, comportamento inalterado
- Fix messages traduzidos para inglês (melhor instruction-following em modelos menores)
- `openai>=1.0` adicionado como dependência core

### Fixed

**`synesis_coder/cli.py`**
- Modelo padrão exibido no help agora reflete corretamente o valor de `SYNESIS_CODER_MODEL` no `.env` — `load_dotenv()` chamado dentro de `_default_model()` antes de ler a variável de ambiente

### Changed

**`synesis_coder/prompt_builder.py`**
- Todos os prompts traduzidos de português para inglês para melhor instruction-following com modelos open-source menores (Qwen3, Gemma)

**`tests/test_abstract_mode.py`**, **`tests/test_document_mode.py`**
- Assertions atualizadas para refletir strings em inglês nos prompts

---

## [0.1.1] — 2026-04-01

### Fixed

**`synesis_coder/cli.py`**
- Forçar encoding UTF-8 em `sys.stdout`, `sys.stderr` e `sys.stdin` no topo do
  módulo, antes de qualquer `click.echo` — corrige corrupção de acentos e
  caracteres especiais quando invocado como processo filho pelo VSCode (Windows
  usa cp1252 por padrão)

**`synesis_coder/__main__.py`**
- Mesma correção de encoding UTF-8 para invocação via `python -m synesis_coder`

### Changed

**`synesis_coder/cli.py`**
- Help CLI completamente reformulado: exibe versão do `synesis-coder`, versão
  do compilador `synesis` e modelo LLM padrão em uso (lidos em runtime)
- Exemplos de uso para todos os 4 modos (`item`, `abstract`, `document`,
  `ontology`) com todas as opções relevantes documentadas
- Cores ANSI aplicadas ao help para facilitar leitura: títulos de seção em
  amarelo, comandos em verde, flags em ciano, comentários em cinza —
  automaticamente suprimidas em pipes e redirecionamentos (detecção de TTY)
- Subclasse `_SynesisGroup` bypassa o formatter do Click, preservando
  indentação e quebras de linha exatas nos exemplos de código

---

## [0.1.0] — 2026-03-23

### Added — Phase 2: `abstract` mode

**`synesis_coder/modes/abstract_mode.py`**
- `process_abstract(project_path, bibref, format, model)` — generates structured
  academic abstracts from the project's corpus of ITEM blocks for a given bibref
- Loads all ITEM blocks, excerpts QUOTATION/MEMO/NOTE fields (template-driven),
  and injects them as context into the LLM prompt
- Validates output via compiler; correction loop with temperature escalation

**`synesis_coder/prompt_builder.py`**
- `build_abstract_prompt(ctx, bibref, excerpts)` — assembles Anthropic API message
  list for abstract generation; system prompt cached, user message dynamic
- Excerpt injection: bibref metadata (author/year via BibTeX), field content per
  ITEM block, ordered by document position

---

### Added — Phase 3: `document` mode

**`synesis_coder/modes/document_mode.py`**
- `process_document(project_path, output, format, model)` — batch-generates ITEM
  blocks for all SOURCEs in a project that have no ITEM annotations yet
- Concurrent processing via `asyncio.Semaphore` (default 5 simultaneous calls)
- Progress reporting per source; appends results to a `.syn` output file

**`synesis_coder/llm_client.py`**
- `AsyncLLMClient` — async counterpart of `LLMClient` using `anthropic.AsyncAnthropic`
- Shared rate-limiting logic (RPM + TPM semaphores) across concurrent requests
- `call_async(messages, temperature)` / `fix_async(previous_output, errors, temperature)`

---

### Added — Phase 4: `ontology` mode

**`synesis_coder/modes/ontology_mode.py`**
- `process_ontology(project_path, output_path, update, concurrent, model, format)` —
  batch-generates ONTOLOGY entries for all codes found in the project corpus
- `_build_semantic_ctx(code, ctx)` — assembles rich per-code context:
  frequency (# items using the code), sources (# distinct SOURCEs), relations
  from CHAIN fields (up to 15), co-occurrences with other codes (up to 20),
  representative examples (up to 3 excerpts from QUOTATION/NOTE fields)
- `_get_pending_codes(ctx, update)` — with `--update`: skips codes already
  defined in `ontology_index`; without: generates all codes
- Concurrent processing via `asyncio.Semaphore`
- Raises `ValueError` if the project template has no ONTOLOGY scope fields

**`synesis_coder/prompt_builder.py`**
- `build_ontology_prompt(ctx, code, semantic_ctx)` — ontology prompt with cached
  system message (template fields, project description, available TOPIC codes)
  and dynamic user message (code name, semantic stats, relations, co-occurrences,
  examples)

**`synesis_coder/validator.py`**
- `validate_ontology_entry(output, ctx, llm_client, ontology_key, max_tries)` —
  validates ONTOLOGY blocks via `synesis.load(..., ontology_contents={key: output})`
- `validate_ontology_entry_async(...)` — async counterpart for concurrent use
- `_extract_ontology_blocks(text)` — extracts only `ONTOLOGY...END ONTOLOGY`
  blocks from LLM output (discards ITEM/SOURCE noise)

**`synesis_coder/project_loader.py`**
- `load_project()` now returns `required_ontology: List[str]` — required fields
  in ONTOLOGY scope, derived from `result.template.required_fields[Scope.ONTOLOGY]`
- `has_ontology_scope: bool` — True when template defines at least one ONTOLOGY field

**`synesis_coder/cli.py`**
- `ontology` subcommand fully implemented: `--project`, `--output`, `--update`,
  `--concurrent` (default 5), `--format`, `--model`

---

### Added — Backup feature

- When running `ontology` mode **without** `--update` and the output `.syno` already
  exists, a backup is automatically created as `{stem}_bkp.syno` before overwriting
- Prevents accidental loss of hand-curated ontology entries

---

### Added — Tests

**`tests/test_ontology_mode.py`** — 15 unit tests + 3 integration tests:
- `TestGetPendingCodes` (3): all codes returned without `--update`; defined codes
  excluded with `--update`; empty result when all codes already defined
- `TestBuildSemanticCtx` (4): frequency/source counts; relation extraction from
  CHAIN triples; examples from QUOTATION fields; graceful empty ctx for codes with
  no linked data
- `TestOntologyPromptBuilder` (5): system + user structure; system prompt cached;
  code name in user message; frequency/source stats in user message; relations in
  user message
- `TestValidateOntologyEntry` (3): single ONTOLOGY block extracted; ITEM/SOURCE
  blocks discarded; empty string on no blocks
- `TestOntologyModeIntegration` (3, require `ANTHROPIC_API_KEY`): social_acceptance
  generates valid entry; `--update` skips existing codes; thompson project raises
  `ValueError` (no ONTOLOGY scope)

---

### Changed

- `--version` flag now reports `0.1.0`

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

[0.6.2]: https://github.com/usuario/synesis-coder/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/usuario/synesis-coder/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/usuario/synesis-coder/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/usuario/synesis-coder/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/usuario/synesis-coder/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/usuario/synesis-coder/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/usuario/synesis-coder/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/usuario/synesis-coder/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/usuario/synesis-coder/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/usuario/synesis-coder/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/usuario/synesis-coder/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/usuario/synesis-coder/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/usuario/synesis-coder/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/usuario/synesis-coder/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/usuario/synesis-coder/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/usuario/synesis-coder/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/usuario/synesis-coder/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/usuario/synesis-coder/releases/tag/v0.1.0
[0.0.1]: https://github.com/usuario/synesis-coder/releases/tag/v0.0.1
