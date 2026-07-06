# Estudo: Flag `--debug` — Log de Auditoria do Pipeline LLM (modo `document`)

**Data:** 2026-06-14
**Versão alvo:** synesis-coder 0.4.x → 0.5.0
**Escopo inicial:** modo `document`. Arquitetura reaproveitável para `item`, `abstract`, `ontology`.

---

## 0. Resumo executivo

Hoje o `synesis-coder` é uma caixa-preta para o pesquisador qualitativo. Ele
fornece o texto e a referência, recebe blocos Synesis de volta, mas **não enxerga
como suas GUIDELINES viraram prompt, o que o LLM respondeu, nem por que houve
correções**. Quando o resultado sai estranho, não há como saber se o problema
está na diretriz que ele escreveu, no modelo, ou no compilador.

A flag `--debug` resolve isso gerando, ao lado do `.syn` de saída, um arquivo
**`<projeto>_<bibref>_debug.md`** legível por humanos — um relatório narrativo,
cronológico, que mostra o pipeline inteiro: prompt montado (com as GUIDELINES
destacadas) → resposta bruta do LLM → veredito do compilador (em linguagem
amigável) → cada ciclo de correção, até o resultado final.

**Princípio central de design:** o log é um **artefato de pesquisa**, não um dump
de telemetria. Markdown renderizável, sem JSON cru, sem stack traces, sem siglas
de erro não explicadas. Um pesquisador que não programa deve conseguir ler o
arquivo e entender exatamente o que aconteceu.

**Esforço estimado:** ~1,5 a 2 dias para o modo `document`; ~0,5 dia adicional por
modo subsequente reusando a mesma infraestrutura.

---

## 1. Conteúdo do Log (Arquitetura de Informação)

O log é organizado em **seções cronológicas**. Cada chamada ao LLM e cada ciclo
de correção vira um bloco. Abaixo, exatamente o que cada bloco captura e de onde
o dado vem no código.

### 1.1 Cabeçalho da sessão (uma vez, no topo)

| Informação | Fonte no código |
|---|---|
| Nome do projeto, caminho do `.synp` | `process_document(project_path=...)` |
| Bibref (`@chave`) | argumento `bibref` |
| Arquivo de entrada + tamanho em caracteres | `read_document(input_path)` em `document_mode.py` |
| Modelo LLM em uso | `llm_client.model` |
| Backend (anthropic / openai-compat) | `llm_client.backend` |
| Parâmetros de chunking: `chunk_size`, `overlap`, nº de chunks | `split_into_chunks(...)` |
| Timestamp de início (ISO 8601 legível) | `datetime.now()` no início de `_process_document_async` |
| Temperatura base, `max_tokens`, thinking budget | argumentos / env resolvidos |

### 1.2 Construção do SOURCE (bloco único, antes dos chunks)

O modo `document` faz uma chamada LLM separada para gerar o bloco `SOURCE`
(`_generate_source_block` em `document_mode.py:315`). O log registra:

- O **system prompt** enviado para o SOURCE (instrução + campos SOURCE do template).
- O **user message** (bibref + excerto de 500 chars).
- A **resposta bruta** do LLM.
- Se o bloco SOURCE foi extraído com sucesso ou se caiu no fallback mínimo.
- Latência da chamada.

### 1.3 Por chunk processado (um bloco por chunk)

Para cada chunk (`_process_chunk` em `document_mode.py:519`):

| Informação | Onde capturar |
|---|---|
| Índice do chunk (ex: "Trecho 2 de 7") | `chunk_index`, `total_chunks` |
| Tamanho do chunk + prévia do texto-fonte | `chunk` (truncar para legibilidade, com link "ver completo") |
| **System prompt exato** (com GUIDELINES por campo destacadas) | `messages[0]["content"]` de `build_document_prompt` |
| **User message exato** | `messages[1]["content"]` |
| **Parâmetros da API** (temperatura, max_tokens, modelo) | resolvidos em `_call_sync_inner` |
| Timestamp de envio + **latência (ms)** | medido em torno de `call_async` |
| **Resposta bruta (raw) do LLM** | retorno de `call_async`, antes de qualquer strip |
| Tokens da chamada (in / out) | delta de `llm_client.usage` antes/depois |

### 1.4 Validação e ciclo de correção (sub-blocos dentro de cada chunk)

Este é o coração do valor para o pesquisador. Para cada tentativa dentro de
`validate_and_fix_async` (`validator.py:108`):

| Informação | Fonte |
|---|---|
| Nº da tentativa (0 = geração original, 1–3 = correções) | loop `attempt` |
| Texto Synesis submetido ao compilador | `output` após strip/extract |
| **Veredito do compilador, traduzido** | `result.get_diagnostics()` + mapa de tradução |
| Lista de erros estruturais vs. de valor (classificados) | `result.validation_result.errors` |
| Se houve correção: **prompt de `fix` enviado** (output anterior + erros) | argumentos de `fix_async` |
| Resposta bruta da correção | retorno de `fix_async` |
| Temperatura usada na correção (escalada 0.0 → 0.2 → 0.5) | `CORRECTION_TEMPERATURES[attempt]` |
| Veredito após a correção | próxima iteração |

### 1.5 Tradução dos erros do compilador (amigável)

O compilador retorna códigos como `SYNESIS_E022`. O pesquisador não sabe o que
isso significa. O log deve traduzir usando um **mapa estático** (sem chamar o
compilador — apenas formatação de apresentação). Exemplos:

| Código | Tradução no log |
|---|---|
| `E020` MissingRequiredField | "Faltou um campo obrigatório: **`{campo}`**. O template exige que todo ITEM tenha esse campo." |
| `E021` ForbiddenFieldPresent | "A IA usou um campo que não pertence a este template: **`{campo}`**." |
| `E022` UnknownFieldName | "A IA inventou um nome de campo que não existe: **`{campo}`**. Nomes válidos: {lista}." |
| `E010` InvalidChainRelation | "A relação **`{rel}`** usada na cadeia causal não está definida no template." |
| `E027` InvalidEnumeratedValue | "O valor **`{val}`** não é uma das opções permitidas para `{campo}`." |
| `E030` ScaleOutOfRange | "O número **`{val}`** está fora da escala permitida ({min}–{max})." |
| Erro de parse | "A IA produziu texto que não segue a sintaxe Synesis (possivelmente markdown ou bloco incompleto)." |

> A lista completa de classes de erro está em
> `d:\GitHub\synesis\synesis\ast\results.py`. O mapa de tradução vive no
> synesis-coder (camada de apresentação), não no compilador.

### 1.6 Rodapé da sessão (uma vez, no fim)

- Total de chunks, ITEMs gerados, ITEMs após deduplicação.
- Total de correções acionadas (e quantas tiveram sucesso).
- Resumo de tokens (`llm_client.usage.summary_line()`).
- Tempo total decorrido.
- Veredito da validação final do output combinado.

---

## 2. Design do Arquivo de Log (Mockup)

Nome do arquivo: **`<stem_do_synp>_<bibref>_debug.md`**, salvo no mesmo diretório
do `--output`. Exemplo: `social_acceptance_smith2024_debug.md`.

````markdown
# Relatório de Depuração — synesis-coder

**Projeto:** social_acceptance
**Documento:** entrevista_03.txt (18.420 caracteres)
**Referência:** @smith2024
**Modelo:** gemini-3.1-pro-preview (backend: openai-compat)
**Início:** 14/06/2026 09:32:07

**Configuração de chunking:** 7 trechos · tamanho 12.000 · sobreposição 2.400
**Parâmetros do modelo:** temperatura 0.0 · máx. tokens 4096 · thinking desativado

---

## Etapa 1 — Geração do bloco SOURCE

> Antes de codificar, a IA cria o cabeçalho bibliográfico do documento.

**Enviado ao modelo (instrução de sistema):**

> Você é um codificador de pesquisa qualitativa. Gere APENAS um bloco SOURCE…
> Campos do SOURCE neste template:
>   • `titulo` (texto) — título do documento
>   • `tipo` (lista: entrevista | artigo | relatório)

**Enviado (mensagem do usuário):**

> BIBREF: @smith2024
> <excerto>Esta entrevista foi conduzida em março de 2024 com…</excerto>

**Resposta da IA** *(latência: 1.240 ms)*:

```synesis
SOURCE @smith2024
    titulo: Entrevista sobre aceitação de energia eólica
    tipo: entrevista
END SOURCE
```

✅ Bloco SOURCE extraído com sucesso.

---

## Etapa 2 — Codificação dos trechos

### Trecho 2 de 7  ·  11.980 caracteres

**Prévia do texto-fonte:**

> "…a comunidade resistiu ao parque eólico porque sentiram que a decisão foi
> imposta de cima para baixo, sem consulta. A confiança nas autoridades
> despencou…" *(ver texto completo no apêndice deste trecho)*

#### Como suas diretrizes (GUIDELINES) foram montadas no prompt

A instrução de sistema abaixo foi construída a partir do seu template. As
diretrizes que **você escreveu** no arquivo `.synt` aparecem destacadas:

> **Campo `chain` (CADEIA CAUSAL) — obrigatório**
> *Suas diretrizes:* "Codifique a cadeia causal ligando o fator de aceitação ao
> seu efeito. Use a direção causa → efeito. Prefira conceitos já existentes no
> projeto. Granularidade: um mecanismo por cadeia…"
> *Relações disponíveis:* ENABLES, INFLUENCES, REDUCES, CAUSES
>
> **Campo `note` (MEMO) — obrigatório (vinculado a `chain`)**
> *Suas diretrizes:* "Escreva uma nota analítica explicando o mecanismo da
> cadeia em linguagem natural…"
>
> **Conceitos já existentes no projeto (prefira estes):**
> community_trust, top_down_decision, perceived_fairness, …

**Mensagem do usuário enviada:**

> BIBREF: @smith2024
> [Trecho 2 de 7 — extraia apenas ITEMs com evidência completa neste trecho]
> <text>…a comunidade resistiu ao parque eólico porque…</text>

**Resposta bruta da IA** *(latência: 3.870 ms · tokens: entrada 6.557 · saída 198)*:

```synesis
ITEM @smith2024
    text: a decisão foi imposta de cima para baixo, sem consulta
    note: A imposição vertical reduziu a confiança da comunidade nas autoridades.
    chain: top_down_decision -> REDUCES -> community_trust
END ITEM
```

#### Verificação pelo compilador Synesis

🔴 **Tentativa 1 — 1 problema encontrado:**

- **Relação inválida na cadeia causal:** a relação `REDUCES` usada em
  `top_down_decision -> REDUCES -> community_trust` não está entre as relações
  definidas no seu template. Relações válidas: ENABLES, INFLUENCES, CAUSES.
  *(código técnico: SYNESIS_E010)*

> A IA será solicitada a corrigir. Temperatura elevada para 0.0.

**Correção enviada à IA:**

> O texto Synesis abaixo tem erros. Corrija-os.
> [output anterior]
> Erros do compilador: [diagnóstico]

**Resposta da correção** *(latência: 2.100 ms)*:

```synesis
ITEM @smith2024
    text: a decisão foi imposta de cima para baixo, sem consulta
    note: A imposição vertical reduziu a confiança da comunidade nas autoridades.
    chain: top_down_decision -> INFLUENCES -> community_trust
END ITEM
```

✅ **Tentativa 2 — validação bem-sucedida.** O ITEM é válido.

**Resultado deste trecho:** 1 ITEM gerado · 1 ciclo de correção.

---

### Trecho 3 de 7  ·  12.000 caracteres

*(…mesmo formato…)*

✅ **Tentativa 1 — validação bem-sucedida** (sem correções necessárias).

---

## Resumo da sessão

| Métrica | Valor |
|---|---|
| Trechos processados | 7 (7 OK, 0 falhas) |
| ITEMs gerados | 14 |
| ITEMs após deduplicação | 11 |
| Ciclos de correção acionados | 3 |
| Correções bem-sucedidas | 3 de 3 |
| Tokens totais | entrada 47.300 · saída 1.420 · total 48.720 |
| Chamadas à API | 9 (incluindo 3 correções) |
| Tempo total | 38,4 s |
| **Validação final** | ✅ OK |

**Arquivo gerado:** `social_acceptance_smith2024.syn`
````

**Decisões de apresentação:**

- Texto-fonte e respostas longas são **truncados na visão principal** com uma
  nota de "ver completo" — opcionalmente um apêndice ao fim do arquivo com os
  textos integrais (configurável; padrão truncado para legibilidade).
- Emojis de status (✅ 🔴) dão escaneabilidade imediata.
- O código técnico do erro (`SYNESIS_E010`) aparece como nota secundária, nunca
  como protagonista — o pesquisador lê a explicação, não a sigla.
- GUIDELINES escritas pelo pesquisador são **citadas literalmente e destacadas**,
  para que ele reconheça seu próprio texto e veja como ele virou prompt.

---

## 3. Análise de Implementação no Código

### 3.1 O desafio arquitetural central

Há uma tensão entre **dois locais que têm metade da informação cada**:

- `_call_sync_inner` (`llm_client.py:409`) é o **único chokepoint** por onde
  passam todos os payloads brutos da API (system blocks, api_messages, resposta,
  tokens) — mas ele **não sabe** a que chunk/ciclo/modo pertence a chamada.
- `_process_chunk` / `validate_and_fix_async` sabem o **contexto semântico**
  (chunk 2 de 7, tentativa 1, isto é uma correção) — mas só veem `messages` e
  `raw`, não os parâmetros finais resolvidos da API (max_tokens dinâmico, etc.).

**Solução recomendada: um objeto `DebugRecorder` injetado no `LLMClient`**, que
recebe eventos de ambos os lados e os correlaciona. Isso evita poluir a lógica
de negócio com `if debug:` espalhados.

### 3.2 Componentes a criar

**Novo módulo `synesis_coder/debug_log.py`** contendo:

1. `DebugRecorder` — acumula eventos estruturados em memória durante a sessão:
   - `record_session_header(...)`
   - `record_llm_call(phase, label, system, user, raw, latency_ms, tokens, params)`
   - `record_validation(attempt, submitted, diagnostics, classified_errors)`
   - `record_fix(attempt, prev_output, errors, fix_prompt, fix_raw, temperature)`
   - `record_session_footer(...)`
   - `render_markdown() -> str` — transforma os eventos no relatório final.
   - `write(path)` — persiste.

2. `ERROR_TRANSLATIONS: dict[str, callable]` — mapa código→formatador amigável
   (a tabela da seção 1.5). Função `translate_diagnostics(result) -> list[str]`.

3. Função `classify_error(err) -> Literal["structural", "value"]` — reaproveitar
   a categorização já desenhada no `Estudo_Reducao_Tokens` (Opção 0).

> O `DebugRecorder` é **thread-safe** (lock interno), porque os chunks rodam
> concorrentemente via `asyncio.Semaphore`. Cada evento carrega o `chunk_index`
> para que o `render_markdown` reordene cronologicamente na escrita final.

### 3.3 Pontos de interceptação (mínimos e localizados)

| Arquivo | Mudança |
|---|---|
| `cli.py` | Adicionar `@click.option("--debug", is_flag=True)` ao comando `document`; passar `debug` para `process_document`. |
| `llm_client.py` | `__init__` aceita `recorder: DebugRecorder \| None = None`. Em `_call_sync_inner`, **após** resolver `max_tokens`/temperatura e obter a resposta, se `recorder` existe, emitir um evento "raw call" com params finais + resposta + tokens. Captura latência com `time.monotonic()` em torno do `_call_with_retry()`. |
| `document_mode.py` | `process_document`/`_process_document_async` criam o `DebugRecorder` quando `debug=True`, passam ao `LLMClient`, registram header/footer, e ao fim chamam `recorder.write(path)`. Passam um "rótulo de contexto" (chunk N de M) via um campo thread-local no recorder antes de cada `call_async`. |
| `validator.py` | `validate_and_fix_async` aceita `recorder=None`; dentro do loop, registra cada tentativa (texto submetido, diagnósticos, classificação) e cada `fix`. |

**Princípio:** quando `recorder is None` (padrão, sem `--debug`), o overhead é
**zero** — apenas um `if recorder is not None` por ponto. Nenhuma mudança de
comportamento no caminho normal.

### 3.4 Correlação chunk ↔ chamada raw

Como `_call_sync_inner` roda em `asyncio.to_thread`, o vínculo entre a chamada
crua e o chunk se faz por **`threading.local`** no recorder (mesmo padrão já
usado em `_correction_local` para o flag de correção em `llm_client.py:241`).
Antes de cada `call_async`, o chamador seta `recorder.context = ("chunk", 2, 7)`;
o evento raw lê esse contexto na thread worker. Esse padrão já é comprovado no
código atual — é a mesma técnica que distingue chamadas de correção.

### 3.5 Onde e como o arquivo é salvo

- **Local:** mesmo diretório de `--output`, derivado de `output_path.parent`.
- **Nome:** `f"{project_path.stem}_{bibref}_debug.md"`.
- **Encoding:** UTF-8 explícito (consistente com o resto do projeto, que já
  força UTF-8 — ver `cli.py` correção de encoding 0.1.1).
- **Momento da escrita:** uma única vez, ao fim de `_process_document_async`,
  via `recorder.write(path)`. Acumular em memória e escrever no fim evita I/O
  concorrente e mantém a ordem cronológica garantida.
- **Conflito de nomes:** se o arquivo já existir, sobrescrever (é um artefato de
  diagnóstico efêmero, não dado curado). Documentar no `--help`.

---

## 4. Análise de Custo e Expansibilidade

### 4.1 Esforço para o modo `document` (inicial)

| Tarefa | Esforço |
|---|---|
| `debug_log.py` (`DebugRecorder` + renderizador markdown) | ~250–300 linhas, 0,75 dia |
| Mapa de tradução de erros (`ERROR_TRANSLATIONS`) | ~80 linhas, 0,25 dia |
| Interceptação em `llm_client._call_sync_inner` (latência + evento raw) | ~15 linhas, baixo |
| Wiring em `document_mode.py` (header/footer/contexto) | ~30 linhas, baixo |
| Interceptação em `validator.validate_and_fix_async` | ~20 linhas, baixo |
| Flag `--debug` na CLI | ~5 linhas, trivial |
| Testes (renderização, tradução, recorder thread-safe, no-op quando off) | ~0,5 dia |
| **Total** | **~1,5–2 dias** |

**Risco de regressão:** baixo. O caminho sem `--debug` é protegido por
`if recorder is not None`. Nenhuma assinatura pública muda de forma incompatível
(novos parâmetros têm default `None`).

### 4.2 Reaproveitamento para `item`, `abstract`, `ontology`

A arquitetura foi desenhada para reuso. O `DebugRecorder` e o renderizador são
**agnósticos de modo** — eles registram "chamadas LLM" e "ciclos de validação",
conceitos comuns a todos os modos. O que muda por modo:

| Modo | Trabalho adicional | Esforço |
|---|---|---|
| `item` | Wiring em `item_mode.process_item` (síncrono — usa `call`/`validate_and_fix`). Mesmo recorder, sem concorrência (mais simples). | ~0,25 dia |
| `abstract` | Wiring em `abstract_mode` + reuso de `validate_and_fix`/`_extract_annotation_blocks`. | ~0,25 dia |
| `ontology` | Wiring + `validate_ontology_entry`. Já tem estrutura concorrente análoga ao `document`. | ~0,5 dia |

**Chave da expansibilidade:** a interceptação no `llm_client._call_sync_inner`
**já cobre todos os modos automaticamente** (todos passam por esse chokepoint).
A única coisa específica por modo é (a) criar/escrever o recorder no entrypoint
do modo e (b) registrar os ciclos de validação na função de validação daquele
modo. As funções `validate_and_fix`, `validate_ontology_entry` são compartilhadas,
então instrumentar `validator.py` uma vez beneficia múltiplos modos.

**Generalização do nome do arquivo:** trocar o template de nome para incluir o
modo quando não houver bibref único (ex: `<projeto>_ontology_debug.md`,
`<projeto>_item_<timestamp>_debug.md`).

### 4.3 Considerações de escala

- **Tamanho do arquivo:** um documento de 7 chunks com correções gera ~15–40 KB
  de markdown — trivial. Documentos muito grandes (50+ chunks) podem gerar
  centenas de KB; por isso o truncamento de texto-fonte na visão principal é
  padrão, com apêndice opcional.
- **Memória:** acumular eventos em memória é aceitável para a escala atual
  (dezenas de chunks). Se no futuro houver documentos com milhares de chunks,
  migrar para escrita incremental por chunk (append) — mas isso não é necessário
  agora e complicaria a ordenação cronológica.

---

## 5. Recomendações e sequência de implementação

1. **Fase 1 (este escopo):** `debug_log.py` + interceptação no `_call_sync_inner`
   + wiring no `document_mode` + flag CLI. Entregar o modo `document` completo.
2. **Fase 2:** estender para `item` e `abstract` (baixo custo, mesmo recorder).
3. **Fase 3:** `ontology`.
4. **Sinergia com a Opção 0** do `Estudo_Reducao_Tokens`: a função
   `classify_error` (estrutural vs. valor) é a mesma. Implementar `--debug` e a
   instrumentação da Opção 0 juntas evita duplicar a lógica de classificação — o
   recorder consome a classificação e a telemetria a agrega.

**Não recomendado:** logar JSON cru, expor stack traces ao pesquisador, ou
escrever o log incrementalmente por padrão (quebra a ordem cronológica em
execução concorrente). Manter o recorder em memória e renderizar uma vez.

---

## 6. Arquivos a tocar (resumo)

| Arquivo | Natureza da mudança |
|---|---|
| `synesis_coder/debug_log.py` | **Novo.** `DebugRecorder`, renderizador markdown, mapa de tradução de erros. |
| `synesis_coder/cli.py` | Flag `--debug` no comando `document`; repasse a `process_document`. |
| `synesis_coder/llm_client.py` | `__init__` aceita `recorder`; `_call_sync_inner` mede latência e emite evento raw (guardado por `if recorder`). |
| `synesis_coder/modes/document_mode.py` | Cria/escreve o recorder; seta contexto por chunk; registra header/footer. |
| `synesis_coder/validator.py` | `validate_and_fix_async` aceita `recorder`; registra tentativas e correções. |
| `tests/test_debug_log.py` | **Novo.** Renderização, tradução de erros, thread-safety, no-op sem `--debug`. |
| `CHANGELOG.md` | Entrada `[Unreleased]`. |
