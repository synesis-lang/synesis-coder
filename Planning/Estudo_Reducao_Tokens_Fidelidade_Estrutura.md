# Estudo: Redução de Tokens, Fidelidade de Estrutura e Velocidade

**Data:** 2026-06-14  
**Referência:** Artigo "Structured Output in LLMs: Why It Matters and How to
Implement It" (Harshin Ramesh, Medium 2025) — técnicas avaliadas abaixo em
relação ao ambiente real do synesis-coder.


**Objetivo:** Avaliar abordagens para (1) reduzir tokens de saída, (2) aumentar
fidelidade estrutural dos blocos Synesis, (3) aumentar velocidade de
processamento.

---

## Sequência completa do pipeline (modo `item`)

### Visão geral

```
CLI / extension
  → project_loader.load_project()          [1] carrega projeto + template
  → prompt_builder.build_item_prompt()     [2] monta mensagens
  → LLMClient._translate_messages()        [3] converte para formato do backend
  → LLMClient._call_with_retry()           [4] envia à API + recebe texto
  → validator._strip_markdown_fences()     [5] remove ``` se presentes
  → validator._extract_item_blocks()       [6] extrai ITEM...END ITEM via regex
  → synesis.load()                         [7] compila + valida
  → LLMClient.fix() [até 3×]              [8] ciclo de correção se erro
  → stdout / arquivo .syn                  [9] output final
```

---

### Tabela detalhada: o que é enviado, como e o que acontece

| Etapa | Onde | O que acontece | O que é enviado / retornado |
|---|---|---|---|
| **1 — Carregamento do projeto** | `project_loader.load_project()` | Chama `synesis.load()` sobre o `.synp` e `.synt`. Extrai campos, relações, índices de conceitos e tópicos. | **Retorna:** `ctx` dict com `item_fields`, `required_item`, `chain_relations`, `code_index`, `topic_index`, `bundle_pairs`, `chain_field_name`, `project_description`, `output_language` etc. |
| **2a — System prompt** | `prompt_builder._build_system_prompt()` | Concatena seções estáticas com `\n\n` entre elas. Resultado é o mesmo para todas as chamadas do mesmo projeto (cacheável). | **Seções:** papel do modelo → idioma de output → descrição do projeto → instruções por campo → índice de conceitos → índice de tópicos → instrução de formato de output |
| **2b — Instrução por campo** | `_build_item_fields_section()` + `_field_instruction()` | Para cada campo do template, emite `nome (TIPO) [REQUIRED/OPTIONAL]:` + instrução. Hierarquia: `spec.guidelines` > `spec.description` > instrução genérica por `FieldType`. Campos CHAIN recebem lista de relações disponíveis + sintaxe. ENUMERATED/ORDERED recebem lista de valores permitidos com index e descrição. SCALE recebe range. | Exemplo emitido: `aspecto (SCALE) [REQUIRED]:` / `  Indicador de intensidade. Range: 1–7` |
| **2c — Índice de conceitos** | `_build_code_index_section()` | Lista todos os conceitos já usados no projeto (agrupados em linhas de 10). Vazio se projeto novo. | `EXISTING PROJECT CONCEPTS (prefer these...): A, B, C, ...` |
| **2d — Formato de output** | `_build_output_format_section()` | Template textual do bloco esperado. Inclui regra BUNDLE se o template tiver campos vinculados. | `OUTPUT FORMAT: ITEM @{bibref} / {field}: {value} / ... / END ITEM` |
| **2e — User message** | `_build_user_message()` | Dinâmico — varia por chamada. Inclui bibref e texto a codificar. | `Reference: @smith2024` / `Text: "Community trust is the most important..."` |
| **3 — Tradução para backend** | `LLMClient._translate_messages()` | Separa system/user. **Anthropic:** system → `system_blocks` com `cache_control: ephemeral` se `cache=True`; messages em lista separada. **OpenAI-compat:** campo `cache` ignorado silenciosamente; system vira role=system em `api_messages`. | Dois formatos distintos de lista de mensagens para a API |
| **4 — Chamada à API** | `LLMClient._call_with_retry()` via tenacity | Retry automático em `RateLimitError`/`APIError`. **Anthropic:** `messages.create()` com `thinking` (se budget>0, temperatura forçada a 1.0), extrai bloco `type=text` da resposta. **OpenAI-compat:** `chat.completions.create()`, usa `extra_body` para Qwen3/Kimi reasoning. Registra tokens via `usage.record()`. | **Input:** system + messages + temperature + max_tokens. **Output:** `str` com o texto gerado |
| **5 — Strip de fences** | `validator._strip_markdown_fences()` | Remove ` ```synesis`, ` ````, ` ``` ` e variantes. Ocorre sempre, antes de qualquer validação. | `str` sem delimitadores markdown |
| **6 — Extração de blocos** | `validator._extract_item_blocks()` | Regex `^ITEM\s+@\S+.*?^END ITEM` (re.MULTILINE + re.DOTALL). Extrai apenas os blocos ITEM, descartando texto antes/depois e blocos de outro tipo (SOURCE, ONTOLOGY). | `str` com apenas os blocos ITEM concatenados, ou texto original se nenhum bloco encontrado |
| **7 — Compilação** | `synesis.load()` | Executa o compilador LALR(1) completo sobre o texto. Valida: sintaxe, nomes de campo, obrigatoriedade, valores ENUMERATED/ORDERED, relações CHAIN, SCALE range, BUNDLE/ARITY, referências de código. | `result` com `has_errors()` e `get_diagnostics()` |
| **7a — Filtro OrphanItem** | `validator._has_structural_errors()` | Remove `OrphanItem` da lista de erros (esperado: ITEM sem SOURCE correspondente é normal no modo `item` isolado). Retorna `True` se restarem outros erros. | `bool` — True = ainda há erros a corrigir |
| **8 — Ciclo de correção** | `validator.validate_and_fix()` loop | Até `max_tries=3` tentativas. Temperatura escalada: tentativa 1 → 0.0, tentativa 2 → 0.2, tentativa 3 → 0.5. Envia o bloco com erro + diagnósticos do compilador ao LLM via `client.fix()`. O output do `fix()` passa novamente por strip + extração antes de reentrar no loop. `thinking=False` nas correções (sem budget). | **Input para fix:** texto Synesis inválido + string de diagnósticos. **Output:** novo texto Synesis |
| **9 — Output final** | `item_mode.process_item()` | Se `success=True`: retorna `final_syn`. Se `format=verbose`: prefixa com header de status (`# OK` ou `# AVISO`) + sumário de uso de tokens. Se `success=False`: output começa com `# ERRO: validação falhou após N tentativa(s)` seguido dos diagnósticos comentados e do último bloco gerado. | `str` — bloco Synesis (ou bloco com comentários de erro) |

---

### Composição real do system prompt (modo item)

```
[1] Papel + ABSOLUTE RULES (sempre presente)
    "You are a specialized qualitative research coder. Generate valid..."
    ABSOLUTE RULES: Output ONLY ITEM...END ITEM / No markdown / No explanations...

[2] OUTPUT LANGUAGE (se ctx["output_language"] definido)
    "All free-text field values must be written in pt-BR. Exceptions: QUOTATION..."

[3] PROJECT CONTEXT (se ctx["project_description"] definido)
    "PROJECT CONTEXT: ..."

[4] ITEM FIELDS (sempre presente se template tem campos ITEM)
    "ITEM FIELDS (generate all REQUIRED fields; OPTIONAL only when relevant):"
    Para cada campo:
      "  nome (TIPO) [REQUIRED/OPTIONAL]:"
      "    instrução (guidelines > description > genérica)"
      Para CHAIN: lista de relações + sintaxe
      Para ENUMERATED/ORDERED: lista de valores com índice e descrição
      Para SCALE: range

[5] EXISTING PROJECT CONCEPTS (se code_index não vazio)
    "EXISTING PROJECT CONCEPTS (prefer these; create new ones only when...):"
    "  A, B, C, D, E, F, G, H, I, J,"
    "  K, L, M, ..."

[6] EXISTING TOPICS (se topic_index não vazio)
    "EXISTING TOPICS (for TOPIC fields — prefer these):"
    "  topico_A, topico_B, ..."

[7] OUTPUT FORMAT (sempre presente)
    "OUTPUT FORMAT:"
    "  ITEM @{bibref}"
    "    {field}: {value}"
    "    ..."
    "  END ITEM"
    [se bundle_pairs]: "  Bundled fields (BUNDLE): ..."
    "  Replace {bibref} with the provided reference..."
```

**Toda esta seção é enviada como uma única `system` message com `cache: True`.**  
A user message contém apenas: `Reference: @{bibref}\n\nText:\n{text}`.

---

### O que o LLM precisa "adivinhar" (fontes de erro)

| O que o LLM gera | Controlado por | Pode errar? |
|---|---|---|
| `ITEM @{bibref}` / `END ITEM` | ABSOLUTE RULES + OUTPUT FORMAT | Sim — fence markdown, bloco extra |
| Nome do campo (`relacao:`) | Lista em ITEM FIELDS | Sim — E022 UnknownFieldName |
| Presença de campos REQUIRED | `[REQUIRED]` na lista + OUTPUT FORMAT | Sim — E020 MissingRequiredField |
| Ausência de campos OPTIONAL | `[OPTIONAL]` na lista | Sim — E021 ForbiddenFieldPresent (campo inválido) |
| Valor ENUMERATED/ORDERED | Lista de valores permitidos no prompt | Sim — E027/E029 |
| Valor SCALE | Range indicado no prompt | Sim — E030 |
| Sintaxe CHAIN (`A -> REL -> B`) | Instrução de sintaxe + lista de relações | Sim — E010/E011 |
| Número de CHAIN por ITEM | Nenhum controle — LLM decide | Sim — cardinalidade variável |
| Conceitos no CODE | Lista de conceitos existentes no prompt | Sim — código inexistente |

---

## Avaliação: execução sequencial campo a campo

**Proposta:** em vez de gerar todos os campos de um ITEM em uma única chamada,
executar uma chamada por campo — cada uma com o system prompt focado apenas
naquele campo e suas GUIDELINES. O resultado de cada campo seria confrontado com
o schema antes de prosseguir.

### Por que parece atrativo

- O template `social_acceptance.synt` tem guidelines muito densas:
  `chain` sozinho tem **1.544 tokens de instrução** (719 palavras), incluindo
  regras de direção, padrões SEQUENTIAL vs PARALLEL, granularidade de conceitos,
  etc. Um LLM saturado com tudo ao mesmo tempo pode não honrar todas as regras.
- Cada chamada poderia usar um **prompt focado**, potencialmente mais preciso.
- A validação por campo permitiria **feedback imediato** antes de gerar o
  próximo — se `text` (QUOTATION) for inválido, não faz sentido gerar `chain`.

### Medição de custo real (social_acceptance maduro, 1.384 conceitos)

| Campo | Sys (tokens) | User | Output | Total/chamada |
|---|---|---|---|---|
| `text` (QUOTATION) | 6.557 | 200 | 120 | **6.877** |
| `note` (MEMO) | 6.535 | 200 | 60 | **6.795** |
| `chain` (CHAIN) | 7.850 | 200 | 50 | **8.100** |
| **TOTAL sequencial** | | | | **21.772 tokens + 3 round-trips** |

| Abordagem atual (1 chamada) | 8.600 sys + 200 user + 230 out | **9.030 tokens + 1 round-trip** |

**O custo sequencial é 141% maior e 3× mais lento.** O motivo é estrutural:
o `code_index` (6.226 tokens com 1.384 conceitos) precisa ser repetido em
**cada** chamada porque qualquer campo pode referenciar conceitos existentes
(`chain` precisa de todos; `text` precisa para saber o que extrair; `note`
precisa para nomear mecanismos). Não há como eliminar essa repetição sem perder
contexto entre chamadas.

### Por que a dependência inter-campo inviabiliza o sequencial puro

O template `social_acceptance` tem `BUNDLE note + chain` — ambos devem aparecer
juntos ou nenhum. Isso cria dependência estrutural:

```
text  → extrai o excerto
note  → interpreta o mecanismo do excerto (depende de text)
chain → codifica o mecanismo como relação (depende de text + note)
```

Se `chain` for gerado sem ver o `note` correspondente, o LLM perde o contexto
analítico que o `note` estabelece. Se `note` for gerado sem saber que `chain`
virá depois (e o que estará na chain), a descrição do mecanismo pode ser
inconsistente. A coerência semântica entre os três campos é um produto da
geração conjunta.

### Quando o sequencial faria sentido

O argumento para sequencial funciona num cenário específico:
- Template com **campos independentes** (sem BUNDLE, sem dependência semântica).
- **Sem code_index grande** (projeto novo ou com < 50 conceitos).
- Campo com guidelines **excepcionalmente complexas** que precisam de foco total
  (ex: só `chain` poderia ser uma chamada separada se os outros campos já
  fossem resolvidos).

No `social_acceptance` maduro, nenhuma dessas condições vale.

### Alternativa que captura o benefício sem o custo: foco progressivo no prompt

Em vez de chamar separadamente, **reordenar e hierarquizar** as GUIDELINES no
system prompt: apresentar o campo mais simples primeiro (`text`), depois `note`,
depois `chain` — com a instrução de `chain` iniciando com um resumo de 3 linhas
das regras mais críticas, e o detalhe completo logo abaixo. O LLM recebe tudo,
mas a estrutura direciona a atenção. Custo: zero tokens extras.

---

## Problema atual

O `synesis-coder` pede ao LLM que gere **blocos Synesis completos** — estrutura
e valores juntos — como texto livre. O `validate_and_fix` corrige até 3 vezes
com temperatura escalada (0.0 → 0.2 → 0.5). Cada ciclo de correção multiplica
latência e tokens gastos.

```
LLM gera (estrutura + valores)
  → validate_and_fix
      → [correção 1] → [correção 2] → [correção 3]  ← até 3× mais tokens
```

### Fonte real de erros observados

| Classe de erro | Exemplos | Eliminável por estrutura? |
|---|---|---|
| **Estrutural** | nome de campo errado (E022), campo obrigatório ausente (E020), campo proibido (E021), fence markdown, bloco extra | ✅ Sim |
| **De valor** | relação CHAIN inválida (E010), SCALE fora do range (E030), valor ENUMERATED inválido (E027), código inexistente | ❌ Não |

Se a maioria dos ciclos de correção for por erros **estruturais**, qualquer
abordagem que elimine esses erros por construção tem alto ROI. Se forem de
**valor**, o ganho é marginal.

> **Ação mínima recomendada antes de implementar qualquer opção:** instrumentar
> `validate_and_fix` para logar a classe do primeiro erro de cada tentativa.
> Sem esse dado, a decisão é especulação.

---

## Análise do artigo em relação ao synesis-coder

O artigo apresenta três métodos via LangChain — **Pydantic**, **TypedDict** e
**JSON Schema raw** — todos mapeados sobre o mesmo mecanismo subjacente:
`llm.with_structured_output(schema)`, que internamente usa `response_format` ou
tool-use dependendo do provider. A análise abaixo testa o que cada técnica do
artigo significa concretamente neste projeto:

### Método 1 — Pydantic/TypedDict (`with_structured_output`)
O artigo usa `langchain_openai.ChatOpenAI`. O synesis-coder **não usa
LangChain** — usa o SDK OpenAI diretamente (`openai.OpenAI`) no path
`openai`-compat e `anthropic.Anthropic` no path Anthropic. Portanto:

- Pydantic e TypedDict **não se aplicam diretamente** sem adicionar LangChain
  como dependência (o que seria uma mudança arquitetural grande).
- O mecanismo equivalente no SDK OpenAI atual é passar
  `response_format={"type": "json_object"}` ou
  `response_format={"type": "json_schema", "json_schema": {...}}` diretamente
  em `chat.completions.create()`.

### Método 3 — JSON Schema raw (aplicável hoje, sem dependências novas)
Este é o método relevante. O `llm_client.py` já possui `extra_body` no path
`openai`-compat (linha 471), e `create_kwargs` aceita qualquer parâmetro
adicional. Portanto, **`response_format` pode ser passado hoje, sem upgrade de
SDK**, apenas adicionando o campo ao `create_kwargs` do backend `openai`:

```python
# Já existe em llm_client.py — trecho atual (linha ~461):
create_kwargs: dict = {
    "model": self.model,
    "messages": api_messages,
    "max_tokens": max_tokens,
}
# Adicionar apenas:
if schema is not None:
    create_kwargs["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "item_values", "schema": schema, "strict": True},
    }
```

**Suporte por backend** (confirmado com o `.env` atual):

| Backend ativo no `.env` | `json_object` | `json_schema` (strict) |
|---|---|---|
| Gemini 3.1 Pro (BLOCO 9, **ativo**) | ✅ | ✅ (dialeto OpenAPI subset) |
| OpenRouter (BLOCO 11) | ✅ | ⚠️ Depende do modelo roteado |
| RunPod (BLOCOS 7–8) | ✅ | ⚠️ Depende do modelo |
| Ollama local (BLOCOS 3b–5) | ⚠️ Modelo a modelo | ⚠️ Modelos menores ignoram |
| Anthropic (BLOCOS 1–3) | via `output_config` (SDK≥novo) | via `output_config` (SDK≥novo) |

**Conclusão sobre o artigo:** as técnicas do artigo são válidas e aplicáveis,
mas via JSON Schema raw (Método 3) — não via Pydantic/LangChain. A boa notícia
é que o path `openai`-compat do `llm_client.py` já está estruturado para
receber o `response_format` sem nenhuma mudança de dependência.

---

## Contexto do ambiente (restrição crítica)

O `.env` ativo usa **BLOCO 9 — Gemini via backend `openai`-compatível**.
Dos 11 blocos configurados, **9 usam `SYNESIS_CODER_BACKEND=openai`**.
Só 2 são Anthropic. Qualquer solução que dependa de `output_config` (API
Anthropic) ou de recurso exclusivo de um provider será silenciosamente ignorada
na maioria dos cenários de uso real.

---

## Opções avaliadas

### Opção 0 — Instrumentação (pré-requisito de qualquer decisão)

**O que é:** Adicionar logging de telemetria em `validate_and_fix` para
categorizar o primeiro erro de cada ciclo de correção.

**Implementação:** ~20 linhas em `validator.py`. Nenhum módulo novo.

**Ganho:** Dados reais que orientam a decisão entre as opções abaixo.

**Custo:** Mínimo. Sem risco de regressão.

**Recomendação: Fazer primeiro.**

---

### Opção 1 — Prompt engineering: separar REQUIRED/OPTIONAL + apertar OUTPUT FORMAT

**O que é:** Sem alterar a arquitetura, reforçar o prompt em
`prompt_builder._build_output_format_section` e
`_build_item_fields_section`:
- Mostrar esqueleto com campos REQUIRED já preenchidos com placeholders e
  campos OPTIONAL claramente marcados.
- Instrução explícita: "Não use markdown fences. Não adicione campos fora da
  lista. Omita campos OPTIONAL sem conteúdo relevante."
- Listar valores permitidos inline para ENUMERATED/ORDERED (já feito
  parcialmente; tornar exaustivo).

**Arquivos tocados:** `prompt_builder.py` (~30–50 linhas).

**Ganho esperado:**
- Reduz erros E020/E021/E022 nos modelos mais capazes.
- Funciona em todos os backends (Gemini, Ollama, RunPod, Anthropic).
- Zero latência extra.

**Limitação:**
- Modelos menores (gemma:4b, qwen:8b) continuam errando nomes de campo.
- Não elimina erros estruturais por construção — apenas os torna menos
  frequentes.
- Não reduz tokens de saída (LLM ainda gera a estrutura).

**Quando escolher:** Melhoria imediata, de baixo risco, válida para qualquer
backend. Pode ser feita em paralelo com qualquer outra opção.

---

### Opção 2 — Esqueleto determinístico no prompt (prefill de texto)

**O que é:** Python monta o cabeçalho e a lista de nomes de campo no prompt de
forma determinística. O LLM recebe o esqueleto preenchido e retorna **apenas os
valores**, completando o bloco.

**Variante 2a — Prefill via `assistant` role:**  
```
[system]: regras + template
[user]: bibref + texto
[assistant]: ITEM @smith2024\n    relacao:    ←  Python preenche até aqui
```
O LLM continua a partir daí. Elimina estrutura, reduz tokens de saída.

> ⚠️ Prefill (`assistant` role com conteúdo pré-definido) **não é suportado
> na API Anthropic 4.6+** e pode não funcionar em backends OpenAI-compat
> dependendo do provider (Gemini rejeita, Ollama varia). Verificar suporte
> antes de implementar.

**Variante 2b — Esqueleto no `user` message + instrução "complete":**  
```
[user]: Complete o bloco Synesis abaixo preenchendo apenas os valores:

ITEM @smith2024
    relacao: ___
    aspecto: ___
    ...
END ITEM
```
Funciona em todos os providers porque é texto comum no turno do usuário.

**Ganho:**
- Elimina erros de nome de campo (E022) — o LLM não digita os nomes.
- Reduz tokens de saída: o LLM não repete estrutura que já está no prompt.
- Implementação simples: nova função em `prompt_builder.py` (~50 linhas) +
  ajuste no `item_mode.py`.

**Limitação:**
- Campos CHAIN de cardinalidade variável (0–N linhas): o esqueleto fixo precisa
  de um placeholder especial (ex: `chain: ___ [repita para cada relação]`).
- Multi-linha (QUOTATION/MEMO): o LLM pode inserir newlines que quebram o
  parser LALR.
- `validate_and_fix` continua necessário para erros de valor.

**Quando escolher:** Se os erros forem predominantemente estruturais e o
ambiente for multi-backend. É a opção de melhor custo-benefício depois de
confirmar com a Opção 0.

---

### Opção 3 — Geração separada: estrutura Python + valores LLM via JSON

**O que é:** O LLM devolve **apenas um dicionário JSON** com os valores dos
campos. Python monta o bloco Synesis deterministicamente a partir desse dict.

```
LLM → {"relacao": "A -> REL -> B", "aspecto": 3, "dimensao": "cultural"}
Python → "ITEM @smith2024\n    relacao: A -> REL -> B\n    aspecto: 3\n    dimensao: cultural\nEND ITEM"
```

**Sub-opções de enforcement JSON:**

| Mecanismo | Backends que suportam | Exige mudança de SDK? |
|---|---|---|
| `response_format={"type":"json_object"}` | Gemini ✅, OpenRouter ✅, Ollama ⚠️ | **Não** — já em `create_kwargs` |
| `response_format={"type":"json_schema","json_schema":{...}}` | Gemini ✅, OpenRouter ⚠️ | **Não** — já em `create_kwargs` |
| `output_config={"format":{"type":"json_schema",...}}` | **Apenas Anthropic** | Sim (SDK 0.69.0 não suporta) |

> **Achado de implementação:** o `llm_client.py` linha 461 monta `create_kwargs`
> como um `dict` aberto antes de chamar `chat.completions.create(**create_kwargs)`.
> Adicionar `response_format` é uma linha — sem refatoração.

**O que mudar:**
- Novo módulo `schema_builder.py`: `FieldSpec → JSON Schema` por tipo de campo
  (CHAIN → array, ENUMERATED → enum, etc.).
- Novo módulo `block_assembler.py`: dict → texto Synesis. Lida com CHAIN
  (array de strings = N linhas), CODE (comma-join = 1 linha), QUOTATION
  multi-linha.
- `LLMClient`: novo método `call_json(messages, schema)` que passa
  `response_format` adequado por backend. Fallback para texto livre quando
  backend não suporta.
- `item_mode.py`: wiring do novo caminho + fallback.

**Ganho:**
- Elimina por construção: nome de campo errado, campo ausente/proibido, fence
  markdown, bloco extra, valor ENUMERATED/ORDERED inválido (via enum no schema).
- Menor output do LLM: apenas os valores, sem palavras-chave de estrutura.
- CHAIN com cardinalidade variável resolvida elegantemente (array JSON).
- OPTIONAL: campo ausente no JSON = omitido pelo assembler (sem sentinel).

**Limitação:**
- Dois novos módulos (~150–200 linhas cada).
- `validate_and_fix` **continua necessário** para erros de valor (SCALE range,
  CHAIN relation-name, BUNDLE/ARITY, código inexistente).
- Suporte a `json_schema` no backend `openai`-compat varia: testar Gemini,
  Ollama, RunPod individualmente.
- O caminho de correção (`fix`) recebe texto Synesis, não JSON — a correção
  continua em texto livre (exceto se re-gerar JSON no fix também, o que
  adiciona complexidade).

**Quando escolher:** Quando a Opção 0 confirmar que erros estruturais dominam
**e** o ambiente suportar `response_format` nos backends em uso.

---

### Opção 4 — Few-shot examples no prompt

**O que é:** Incluir 1–2 exemplos completos de bloco ITEM corretamente
formatado no system prompt (cacheável).

**Implementação:** ~10 linhas em `prompt_builder._build_item_system_prompt`.

**Ganho:**
- Reduz significativamente erros estruturais em modelos menores (gemma, qwen).
- Funciona em todos os backends.
- Efeito imediato, zero risco.

**Limitação:**
- Os exemplos ocupam tokens do contexto de input (mas são cacheáveis).
- Não elimina erros por construção — melhora por aprendizado contextual.
- Exemplos precisam ser por template (ou genéricos o bastante para não
  confundir campos do template real).

**Quando escolher:** Complemento a qualquer outra opção. Especialmente útil
para modelos locais menores.

---

### Opção 5 — Cache agressivo do system prompt (já parcialmente implementado)

**O que é:** Garantir que o system prompt (regras + template + índices) seja
sempre marcado `cache: True` e nunca variar entre chamadas do mesmo projeto.

**Estado atual:** `build_item_prompt` já retorna `{"cache": True}` no system
message. O backend Anthropic aplica `cache_control`. Backend `openai`-compat
ignora silenciosamente (campo `cache` não é passado à API).

**Ganho de velocidade (Anthropic):** Tokens cacheados têm latência ~85% menor
no primeiro hit e custo de input 90% menor. Para projetos com system prompt de
2k–5k tokens, o ganho é substancial na segunda chamada em diante.

**Limitação:** Sem efeito no backend `openai`-compat (Gemini, Ollama etc.) —
essas APIs não expõem cache de prompt. O Gemini tem implicit caching próprio
(≥32k tokens, transparente), ativado automaticamente.

**Quando escolher:** Já implementado para Anthropic. Nenhuma ação necessária
exceto não quebrar o agrupamento do system prompt em refatorações.

---

### Opção 6 — Modo "valores curtos": instruir o LLM a ser conciso

**O que é:** Adicionar instrução explícita no system prompt pedindo respostas
mínimas: sem explicações, sem repetição do texto-fonte nas QUOTATION exceto
quando o campo exige, sem tokens de "raciocínio" visíveis.

**Ganho:**
- Reduz tokens de saída em modelos propensos a verbosidade (Gemini, Opus com
  thinking visível).
- Simples de implementar.

**Limitação:**
- Pode reduzir qualidade em campos que requerem contexto (MEMO, QUOTATION).
- Não resolve problemas estruturais.

---

### Opção 7 — Instructor (retry loop com validação Pydantic)

**O que é:** [Instructor](https://github.com/jxnl/instructor) é uma biblioteca
Python que envolve (`patch`) qualquer cliente OpenAI-compatível ou Anthropic e
adiciona dois mecanismos:
1. Converte um `BaseModel` Pydantic em `response_format` / tool-schema e passa
   para a API automaticamente.
2. Se a resposta falhar a validação Pydantic, forma uma nova mensagem com o erro
   e re-envia ao LLM (`reask`) — até `max_retries` vezes.

```python
import instructor
from pydantic import BaseModel

client = instructor.from_openai(openai_client)  # patch no cliente existente

class ItemValues(BaseModel):
    text: str
    note: str
    chain: list[str]

result = client.chat.completions.create(
    model="gemini-3.1-pro-preview",
    response_model=ItemValues,
    messages=[system_msg, user_msg],
    max_retries=3,
)
```

**Relação com o `validate_and_fix` atual:**

| Aspecto | `validate_and_fix` atual | Instructor |
|---|---|---|
| Valida | Semântica Synesis completa (compiler LALR) | JSON schema + tipos Pydantic |
| Erros que detecta | E010/E020/E021/E022/E027/E030/BUNDLE/ARITY | Campo ausente, tipo errado, enum inválido |
| Erros que **não** detecta | — | CHAIN relation-name, SCALE range, BUNDLE, código inexistente |
| Retry loop | Sim (até 3×, temperature escalada) | Sim (até N×, tenacity) |
| Mensagem de retry | Diagnóstico do compilador Synesis | ValidationError Pydantic formatado |
| Temperatura escalada | Sim (0.0→0.2→0.5) | Não (mesma temperatura) |
| Backend suportado | Anthropic + OpenAI-compat | OpenAI-compat ✅, Anthropic ✅ (via `from_anthropic`) |

**Mecanismo interno confirmado:** `instructor.patch` intercepta `chat.completions.create`, 
adiciona `response_format` (modo `JSON_SCHEMA` ou `TOOLS`), parseia o JSON retornado com
Pydantic, e em caso de `ValidationError` chama `handle_reask_kwargs` que forma um novo
`messages` com o erro — estruturalmente igual ao `client.fix()` do synesis-coder mas
para validação de schema, não de semântica Synesis.

**O que o instructor resolve para o synesis-coder:**

| Problema | Instructor resolve? |
|---|---|
| Fence markdown (```) | ✅ — recebe JSON, não texto livre |
| Nome de campo errado (E022) | ✅ — schema Pydantic define campos exatos |
| Campo REQUIRED ausente (E020) | ✅ — Pydantic marca como obrigatório |
| Campo proibido (E021) | ✅ — `additionalProperties=false` |
| Valor ENUMERATED inválido (E027) | ✅ — `Literal[...]` no Pydantic |
| CHAIN relation-name inválida (E010) | ❌ — Pydantic valida só o tipo `str` |
| SCALE fora do range (E030) | ❌ — Pydantic valida tipo `int`, não range |
| BUNDLE paridade (note+chain) | ❌ — Pydantic não sabe de BUNDLE |
| ARITY de CHAIN | ❌ — schema não expressa |

**Custo de integração:**
- Instructor já está instalado no ambiente (`1.15.1`) — **zero nova dependência**.
- O cliente OpenAI existente em `llm_client.py` (linha 197–202) pode ser
  envolvido com `instructor.from_openai(self._client)` sem refatoração maior.
- Modo detectado para a URL do Gemini: `Provider.OPENAI` → usa
  `Mode.TOOLS` ou `Mode.JSON_SCHEMA` automaticamente.
- Para Anthropic: `instructor.from_anthropic(self._client)` com
  `Mode.ANTHROPIC_TOOLS` — funciona com SDK 0.69.0 (usa tool-use, não
  `output_config`).

**Limitação crítica para o synesis-coder:** o instructor substitui a validação
Pydantic pelo `validate_and_fix`, mas não o substitui — ele elimina os erros
**estruturais** (E020/E021/E022/E027) mas não os **semânticos** (E010/E030/
BUNDLE/ARITY). O `validate_and_fix` com `synesis.load()` ainda é necessário
depois do instructor para capturar a classe de erros que Pydantic não alcança.
Na prática: instructor seria a camada 1 (estrutura), synesis.load() seria a
camada 2 (semântica) — dois ciclos de retry em vez de um.

**Quando usar:** Se a Opção 3 (JSON + assembler) for escolhida, o instructor
pode substituir o `call_json` manual — é o mesmo mecanismo com retry automático
e uma API mais limpa. Não é uma alternativa independente; é um enabler da
Opção 3.

---

### Opção 8 — Pydantic AI (framework de agente)

**O que é:** [Pydantic AI](https://ai.pydantic.dev/) é um framework de agentes
LLM que usa Pydantic como camada de validação de I/O. Diferente do Instructor
(que é um wrapper fino sobre o cliente existente), o Pydantic AI é um framework
completo com gerenciamento de estado, ferramentas, dependency injection e
streaming.

```python
from pydantic_ai import Agent
from pydantic import BaseModel

class ItemValues(BaseModel):
    text: str
    note: str
    chain: list[str]

agent = Agent("openai:gemini-3.1-pro-preview", result_type=ItemValues)
result = agent.run_sync(user_prompt)
print(result.data)  # ItemValues validado
```

**Por que não se aplica ao synesis-coder:**

O synesis-coder já tem sua própria orquestração: `load_project → build_prompt
→ LLMClient.call → validate_and_fix`. Pydantic AI substituiria toda essa cadeia
por um Agent — o que implicaria:
- Reescrever o `LLMClient` como `Agent` do Pydantic AI.
- Perder o controle granular de `temperature escalation`, `thinking budget`,
  `rate limiting` por RPM/TPM, `usage tracking` e o `backend=openai` vs
  `backend=anthropic` com suas traduções específicas.
- Pydantic AI `1.107.0` está disponível mas **não está instalado** — seria
  nova dependência pesada (puxa Anthropic, OpenAI, Gemini, Groq, etc.).

É um framework para quem está construindo do zero, não para adicionar sobre
uma arquitetura existente. **Não recomendado para o synesis-coder.**

---

### Opção 9 — Outlines (constrained decoding / token masking)

**O que é:** A biblioteca [Outlines](https://github.com/dottxt-ai/outlines) aplica
**decodificação constrangida** durante a geração: usa uma FSM (Finite State
Machine) para mascarar tokens inválidos na distribuição de probabilidade do
modelo, garantindo que a saída siga o schema exato — não por validação
pós-geração, mas porque tokens inválidos têm probabilidade zero durante a
geração.

```
Token masking na geração:
  softmax(logits) → zera tokens inválidos → sample → próximo token sempre válido
```

**Diferença crítica em relação a todas as outras opções:**  
As opções 1–6 são "pós-geração" ou "prompt-side". O Outlines é "durante-a-geração".
O LLM **não pode** produzir um campo errado — não é que ele foi orientado a não
produzir; é que os tokens desse campo têm probabilidade 0.

**Requisito central: acesso ao modelo local com controle sobre logits.**

Isso é tudo. O Outlines requer:
- Modelo rodando **localmente** (via `transformers`, `llama.cpp`, `vllm`) com
  acesso ao loop de geração para aplicar o token mask.
- **Não funciona** com APIs remotas (Gemini, Anthropic, OpenAI, RunPod) porque
  a API expõe apenas o texto final — o logit masking acontece dentro do servidor
  do provider, ao qual não há acesso.

**Mapeamento para o `.env` do synesis-coder:**

| Backend / bloco | Outlines compatível? |
|---|---|
| Gemini via API (BLOCO 9, **ativo**) | ❌ API remota — sem acesso aos logits |
| OpenRouter (BLOCO 11) | ❌ API remota |
| RunPod nuvem (BLOCOS 7–8) | ❌ API remota |
| Anthropic (BLOCOS 1–3) | ❌ API remota |
| **Ollama local** (BLOCOS 3b–5) | ⚠️ Parcial — Outlines tem adaptador Ollama, mas via `response_format`, não logit masking real (recai na Opção 3) |
| **llama.cpp / vllm local** | ✅ Outlines nativo com logit masking real |
| **HuggingFace Transformers local** | ✅ Outlines nativo |

O synesis-coder não tem nenhum bloco configurado para llama.cpp ou vllm direto.
Os blocos Ollama usam HTTP compat, onde o Outlines usa `response_format` — que é
equivalente à Opção 3, não constrained decoding real.

**Custo de adoção:**
- `pip install outlines` (não está no `pyproject.toml`, seria nova dependência).
- Requer refatorar o path de geração para modelos locais — substituir o caminho
  `backend=openai` (HTTP) por `outlines.from_transformers(model, tokenizer)` ou
  `outlines.from_vllm(...)`.
- Adicionar `transformers` ou `llama-cpp-python` como dependências pesadas
  (~GB de modelos).
- O synesis-coder foi projetado como cliente de API; introduzir geração local
  muda o modelo de deployment completamente.

**Quando faz sentido:**  
Se o objetivo for rodar modelos **totalmente offline** em hardware local com
garantia absoluta de estrutura — cenário onde nenhuma API remota é usada. Para
o ambiente atual (Gemini, Anthropic, OpenRouter), Outlines não se aplica. O
cenário Ollama local (BLOCOS 3b–5) pode se beneficiar, mas apenas da interface
Outlines-Ollama, que internamente usa `response_format` (= Opção 3).

**Resumo:** Outlines é a técnica mais robusta tecnicamente (elimina erros
estruturais a nível de token), mas pressupõe um modelo de deployment incompatível
com a arquitetura atual do synesis-coder (cliente de APIs remotas). Aplicar
Outlines implicaria criar um novo modo de execução local paralelo ao existente.

---

## Comparação consolidada

| Opção | Tokens output↓ | Fidelidade estrutural↑ | Velocidade↑ | Multi-backend | Complexidade |
|---|---|---|---|---|---|
| 0 — Instrumentar | — | — | — | ✅ | 🟢 Trivial |
| 1 — Prompt REQUIRED/OPTIONAL | 🟡 Marginal | 🟡 Melhora | 🟡 Indireta | ✅ Todos | 🟢 Baixa |
| 2b — Esqueleto no prompt | ✅ Médio | ✅ Alto | ✅ Médio | ✅ Todos | 🟡 Média |
| 3 — JSON + assembler | ✅ Alto | ✅ Máximo | ✅ Alto | ⚠️ Parcial | 🟡 Média* |
| 4 — Few-shot examples | — | 🟡 Melhora | — | ✅ Todos | 🟢 Trivial |
| 5 — Cache system prompt | — | — | ✅ Alto (Anthropic) | ⚠️ Anthropic | 🟢 Já feito |
| 6 — Instrução de concisão | 🟡 Marginal | — | 🟡 Indireta | ✅ Todos | 🟢 Trivial |
| 7 — Instructor (retry Pydantic) | ✅ Alto | ✅ Alto (estrutural) | ✅ Médio | ✅ Todos | 🟢 Baixa* |
| 8 — Pydantic AI (framework) | ✅ Alto | ✅ Alto | ✅ Médio | ✅ Todos | 🔴 Muito alta |
| 9 — Outlines (constrained dec.) | ✅ Alto | ✅ Absoluto | ✅ Alto | ❌ Local only | 🔴 Muito alta |

*Opção 3 rebaixada de Alta para Média: `create_kwargs` em `llm_client.py` já aceita `response_format` sem refatoração do cliente.  
*Opção 7 (Instructor): já instalado no ambiente, zero nova dependência, pode envolver o cliente existente em 1 linha.

**Relação entre Opção 3 e Opção 7:** o Instructor é o enabler natural da Opção 3 —
em vez de implementar `call_json` manualmente com `response_format` e retry loop,
o Instructor entrega esse mecanismo pronto. Usados juntos: Instructor faz a camada
de estrutura (JSON/Pydantic), `synesis.load()` faz a camada semântica (CHAIN/SCALE/BUNDLE).
O `validate_and_fix` continua necessário, mas só dispara para erros de valor.

---

## Sequência recomendada

### Fase 1 — Impacto imediato, zero risco (fazer agora)
1. **Opção 4** (few-shot) + **Opção 1** (prompt REQUIRED/OPTIONAL) + **Opção 6**
   (instrução de concisão): todas em `prompt_builder.py`, ~80 linhas total.
2. **Opção 0** (instrumentação): logar classe do primeiro erro de cada ciclo em
   `validate_and_fix`.

### Fase 2 — Decisão guiada por dados (após acumular ≥20 itens instrumentados)
- Se erros estruturais > 60% dos ciclos de correção → **Opção 2b** (esqueleto
  no prompt). Sem novos módulos, funciona em todos os backends.
- Se Opção 2b ainda deixar erros residuais e o backend principal suportar
  `response_format` → **Opção 3** (JSON + assembler).

### Fase 3 — Apenas se Opção 3 for escolhida
- Verificar suporte a `json_schema` em cada backend do `.env` ativo.
- Implementar `schema_builder.py` + `block_assembler.py` + `call_json` no
  `LLMClient` com fallback obrigatório para texto livre.

---

## Arquivos a tocar por opção

| Opção | Arquivos |
|---|---|
| 0 | `synesis_coder/validator.py` |
| 1, 4, 6 | `synesis_coder/prompt_builder.py` |
| 2b | `synesis_coder/prompt_builder.py`, `synesis_coder/modes/item_mode.py` |
| 3 | `synesis_coder/schema_builder.py` (novo), `synesis_coder/block_assembler.py` (novo), `synesis_coder/llm_client.py` (+2 linhas em `create_kwargs`), `synesis_coder/modes/item_mode.py`, `synesis_coder/prompt_builder.py` |
| 7 (Instructor) | `synesis_coder/llm_client.py` (envolver `self._client` com `instructor.from_openai/from_anthropic`), `synesis_coder/schema_builder.py` (novo — gera `BaseModel` Pydantic de `FieldSpecs`), `synesis_coder/block_assembler.py` (novo). **Instructor já instalado — zero nova dependência.** |
| 8 (Pydantic AI) | Reescrita completa do `LLMClient` e da orquestração — não recomendado |
| 9 (Outlines) | Nova dependência pesada, novo modo de execução local; incompatível com backends de API remota |
