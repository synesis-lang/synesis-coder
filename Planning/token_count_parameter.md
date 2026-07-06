# Estudo: Exibicao de Contagem de Tokens no Terminal

**Data:** 2026-04-08
**Escopo:** Todos os modos do `synesis-coder` (`item`, `abstract`, `document`, `ontology`, `suggest`)

---

## 1. Estado Atual

### O que ja existe

O `LLMClient` (`llm_client.py:398-403`) rastreia tokens **internamente** para controle de rate limiting:

```python
def _record_usage(self, usage) -> None:
    now = time.monotonic()
    self._request_times.append((now, 1))
    self._input_tokens.append((now, usage.input_tokens))
    self._output_tokens.append((now, usage.output_tokens))
```

As deques `_input_tokens` e `_output_tokens` armazenam pares `(timestamp, n_tokens)` numa janela deslizante de 60 segundos. Os valores sao descartados apos cada janela e nunca expostos ao chamador.

### Lacunas

| Lacuna | Impacto |
|--------|---------|
| `_record_usage()` nao acumula totais de sessao | Impossivel somar tokens de multiplas chamadas |
| Backend OpenAI nao chama `_record_usage()` | Tokens de backends alternativos nao registrados |
| Nenhum modo repassa tokens ao output do terminal | Usuario nao ve nenhum dado de uso |

---

## 2. Analise do Codigo

### Cadeia de delegacao no LLMClient

Um ponto critico que deve guiar o design: `fix()` e `fix_async()` **delegam** para `call()` e `call_async()` respectivamente:

```python
# llm_client.py:203
def fix(...) -> str:
    fix_messages = [...]
    return self.call(fix_messages, temperature=temperature, max_tokens=max_tokens)

# llm_client.py:260
async def fix_async(...) -> str:
    fix_messages = [...]
    return await self.call_async(fix_messages, temperature=temperature, max_tokens=max_tokens)
```

E `call_async()` delega a `_call_sync_inner()` via thread pool:

```python
# llm_client.py:233-234
async def call_async(...) -> str:
    await self._async_wait_if_rate_limited()
    return await asyncio.to_thread(
        self._call_sync_inner, messages, temperature, max_tokens, thinking
    )
```

**Implicacao:** Alterar o tipo de retorno de `_call_sync_inner()` propaga automaticamente para `call()`, `call_async()`, `fix()` e `fix_async()` — e para todos os chamadores em 5 modos + `validator.py`. Isso cria um blast radius desnecessario.

### Quem chama o LLMClient

| Chamador | Metodo | Arquivo:Linha |
|----------|--------|---------------|
| `item_mode.process_item()` | `client.call()` | `item_mode.py:51` |
| `suggest_mode._select_topics()` | `client.call()` | `suggest_mode.py:119` |
| `suggest_mode.process_suggest()` | `client.call()` | `suggest_mode.py:76` |
| `abstract_mode._process_one_abstract()` | `client.call_async()` | `abstract_mode.py:122` |
| `document_mode._process_chunk()` | `client.call_async()` | `document_mode.py:446` |
| `document_mode._generate_source_block()` | `client.call_async()` | `document_mode.py:252` |
| `ontology_mode._process_one_code()` | `client.call_async()` | `ontology_mode.py:182` |
| `validator.validate_and_fix()` | `llm_client.fix()` | `validator.py:82` |
| `validator.validate_and_fix_async()` | `llm_client.fix_async()` | `validator.py:143` |
| `validator.validate_ontology_entry()` | `llm_client.fix()` | `validator.py:271` |
| `validator.validate_ontology_entry_async()` | `llm_client.fix_async()` | `validator.py:319` |
| `test_abstract_mode.py:314` | `client.call_async()` | Teste integracao |

### Chamadas LLM por modo

| Modo | Chamadas de geracao | Chamadas de correcao (validator) | Total maximo |
|------|--------------------|---------------------------------|-------------|
| `item` | 1 | ate 3 (temperatura 0->0.2->0.5) | 4 |
| `abstract` | 1 por bibref | ate 3 por bibref | 4 x N |
| `document` | 1 SOURCE + N chunks | ate 3 por chunk | 4 x (N+1) |
| `ontology` | 1 por codigo | ate 3 por codigo | 4 x N |
| `suggest` | 1 (+ 1 topicos se > 100 codigos) | nenhuma | 1-2 |

### Backend OpenAI

O branch OpenAI (`_call_sync_inner:288-296`) retorna diretamente a string sem chamar `_record_usage()`:

```python
def _call_with_retry() -> str:
    response = self._client.chat.completions.create(...)
    return response.choices[0].message.content  # sem _record_usage
```

A API OpenAI usa campos diferentes: `response.usage.prompt_tokens` e `response.usage.completion_tokens` (vs `input_tokens`/`output_tokens` da Anthropic). Alem disso, backends locais como Ollama podem retornar `response.usage = None`.

---

## 3. Design da Solucao

### Decisao de arquitetura: acumulador interno no LLMClient

O estudo inicial propunha alterar o retorno de `call()` de `str` para `tuple[str, TokenUsage]`. Essa abordagem **e rejeitada** porque:

1. `fix()` faz `return self.call(...)` — qualquer mudanca no retorno de `call()` exige mudanca em `fix()`, e qualquer mudanca em `fix()` propaga para o validator
2. `fix_async()` faz `return await self.call_async(...)` — mesmo efeito em cascata
3. Testes de integracao chamam `client.call_async()` diretamente e esperam `str`
4. Blast radius: 12 chamadores em 7 arquivos + 4 arquivos de teste

**Abordagem adotada:** adicionar um **acumulador `TokenUsage` como atributo interno** do `LLMClient`. A interface publica (`call`, `fix`, `call_async`, `fix_async`) continua retornando `str`. Os modos acessam `client.usage` apos a execucao.

### 3.1 Classe `TokenUsage` (novo: `synesis_coder/token_usage.py`)

```python
"""Acumulador thread-safe de tokens consumidos por chamadas LLM."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TokenUsage:
    """Acumula totais de tokens de input/output ao longo de multiplas chamadas."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    corrections: int = 0
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def record(
        self,
        input_tok: int,
        output_tok: int,
        is_correction: bool = False,
    ) -> None:
        """Registra tokens de uma chamada."""
        with self._lock:
            self.input_tokens += input_tok
            self.output_tokens += output_tok
            self.api_calls += 1
            if is_correction:
                self.corrections += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary_line(self) -> str:
        """Linha formatada para exibicao no terminal."""
        parts = [
            f"tokens: in {self.input_tokens:,}",
            f"out {self.output_tokens:,}",
            f"total {self.total_tokens:,}",
            f"calls {self.api_calls}",
        ]
        if self.corrections:
            parts.append(f"corrections {self.corrections}")
        return " | ".join(parts)

    def reset(self) -> None:
        """Reinicia contadores (para reutilizar o cliente entre modos)."""
        with self._lock:
            self.input_tokens = 0
            self.output_tokens = 0
            self.api_calls = 0
            self.corrections = 0
```

Thread-safe via `threading.Lock`. Os modos concorrentes (`abstract`, `document`, `ontology`) usam `asyncio.to_thread()` que executa em threads separadas, portanto o lock e necessario.

### 3.2 Modificacao em `LLMClient` (`llm_client.py`)

**a) Novo atributo `usage` no `__init__`:**

```python
# Apos a linha 143 (apos criacao das deques de rate-limiting):
from synesis_coder.token_usage import TokenUsage
self.usage = TokenUsage()
```

**b) `_record_usage()` passa a acumular no `self.usage`:**

```python
def _record_usage(self, usage) -> None:
    """Registra uso de tokens (rate-limiting + acumulador de sessao)."""
    now = time.monotonic()
    self._request_times.append((now, 1))
    self._input_tokens.append((now, usage.input_tokens))
    self._output_tokens.append((now, usage.output_tokens))
    # Acumulador de sessao
    self.usage.record(usage.input_tokens, usage.output_tokens)
```

**c) Branch OpenAI em `_call_sync_inner` — registrar tokens tambem:**

```python
# Antes (linha 288-296):
def _call_with_retry() -> str:
    response = self._client.chat.completions.create(...)
    return response.choices[0].message.content

# Depois:
def _call_with_retry() -> str:
    response = self._client.chat.completions.create(...)
    if response.usage:
        self.usage.record(
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )
    return response.choices[0].message.content
```

**d) Nenhuma outra alteracao em `LLMClient`.** Os metodos `call()`, `call_async()`, `fix()`, `fix_async()` continuam retornando `str`. A acumulacao acontece silenciosamente via `_record_usage()` (Anthropic) ou diretamente no branch OpenAI.

### 3.3 Marcacao de correcoes no validator (`validator.py`)

O validator precisa informar ao acumulador quais chamadas sao correcoes. Como `fix()` e `fix_async()` delegam para `call()` e `call_async()`, e a acumulacao acontece dentro de `_record_usage()`, precisamos de um mecanismo para marcar a proxima chamada como correcao.

**Abordagem: flag `_next_is_correction` no LLMClient:**

```python
# No LLMClient.__init__:
self._next_is_correction = False

# Em _record_usage():
def _record_usage(self, usage) -> None:
    now = time.monotonic()
    self._request_times.append((now, 1))
    self._input_tokens.append((now, usage.input_tokens))
    self._output_tokens.append((now, usage.output_tokens))
    self.usage.record(
        usage.input_tokens,
        usage.output_tokens,
        is_correction=self._next_is_correction,
    )
    self._next_is_correction = False  # reset apos uso

# No branch OpenAI de _call_sync_inner:
if response.usage:
    self.usage.record(
        response.usage.prompt_tokens,
        response.usage.completion_tokens,
        is_correction=self._next_is_correction,
    )
    self._next_is_correction = False

# Em fix() e fix_async():
def fix(self, previous_output, errors, temperature=0.2, max_tokens=4096) -> str:
    self._next_is_correction = True
    fix_messages = [...]
    return self.call(fix_messages, temperature=temperature, max_tokens=max_tokens)
```

**Alternativa mais simples**: nao distinguir correcoes de chamadas normais no acumulador. Apenas registrar `api_calls` total. A contagem de correcoes pode ser derivada no modo (o validator ja retorna `success: bool`).

> **Recomendacao:** Implementar a alternativa simples na Fase 1 (sem `_next_is_correction`). Adicionar a marcacao de correcoes na Fase 2 apenas se for necessario.

### 3.4 Exibicao nos modos

Nenhum modo precisa ser reestruturado. Apos a execucao, cada modo le `client.usage` e adiciona a linha ao output verbose.

**Exemplo `item_mode.py` (linhas 57-69):**

```python
# Formato verbose atual:
header = (
    f"# synesis-coder item\n"
    f"# projeto: {project_name}\n"
    f"# bibref: @{bibref}\n"
    f"{status_line}\n"
)

# Com tokens:
header = (
    f"# synesis-coder item\n"
    f"# projeto: {project_name}\n"
    f"# bibref: @{bibref}\n"
    f"# {client.usage.summary_line()}\n"
    f"{status_line}\n"
)
```

**Exemplo `ontology_mode.py` (linhas 355-361):**

```python
# Atual:
header = (
    f"# synesis-coder ontology\n"
    f"# projeto: {project_path.stem}\n"
    f"# total: {total} | OK: {total_ok} | falhas: {total_fail}\n"
    f"# tempo: {elapsed:.1f}s\n"
)

# Com tokens:
header = (
    f"# synesis-coder ontology\n"
    f"# projeto: {project_path.stem}\n"
    f"# total: {total} | OK: {total_ok} | falhas: {total_fail}\n"
    f"# {llm_client.usage.summary_line()}\n"
    f"# tempo: {elapsed:.1f}s\n"
)
```

**Padroes por modo:**

| Modo | Variavel do client | Onde adicionar `summary_line()` |
|------|-------------------|-------------------------------|
| `item` | `client` (local) | `item_mode.py:63-68`, bloco verbose |
| `suggest` | `client` (local) | `suggest_mode.py:92-98`, bloco verbose |
| `abstract` | `llm_client` (local) | `abstract_mode.py:338-344`, bloco verbose |
| `document` | `llm_client` (local) | `document_mode.py:644-652`, bloco verbose |
| `ontology` | `llm_client` (local) | `ontology_mode.py:355-361`, bloco verbose |

Em todos os modos, o `LLMClient` e instanciado localmente e nao compartilhado entre chamadas do CLI, portanto o acumulador de `usage` reflete exatamente a execucao corrente.

---

## 4. Onde exibir tokens

| Modo | `plain` | `verbose` |
|------|---------|-----------|
| `item` | nao exibir | sim, no cabecalho |
| `suggest` | nao exibir | sim, no cabecalho |
| `abstract` | nao exibir | sim, no cabecalho |
| `document` | nao exibir | sim, no cabecalho |
| `ontology` | nao exibir | sim, no cabecalho |

**Formato `plain`:** preservado inalterado para compatibilidade com pipes e com a extensao VSCode (que parseia a saida plain do modo `item` via `stdout`).

### Exemplos de output verbose

**`item` mode:**
```
# synesis-coder item
# projeto: social_acceptance
# bibref: @ashworth2019
# tokens: in 4,231 | out 312 | total 4,543 | calls 1
# OK

ITEM @ashworth2019
  quotation "Local ownership models significantly reduce opposition."
  ...
END ITEM
```

**`ontology` mode (com correcoes):**
```
# synesis-coder ontology
# projeto: social_acceptance
# total: 12 | OK: 12 | falhas: 0
# tokens: in 48,201 | out 9,847 | total 58,048 | calls 14 | corrections 2
# tempo: 41.3s
```

**`document` mode:**
```
# synesis-coder document
# projeto: social_acceptance
# bibref: @entrevista_01
# input: E01.txt
# chunks: 5 | ITEMs: 12 | validacao: OK
# tokens: in 22,540 | out 4,128 | total 26,668 | calls 6
# tempo: 18.7s
```

---

## 5. Arquivos Afetados

### Modificados

| Arquivo | Mudanca | Risco |
|---------|---------|-------|
| `synesis_coder/llm_client.py` | Adicionar `self.usage`, acumular em `_record_usage()`, registrar tokens no branch OpenAI | Baixo: aditivo, sem mudanca de interface |
| `synesis_coder/modes/item_mode.py` | 1 linha no bloco verbose | Nenhum |
| `synesis_coder/modes/suggest_mode.py` | 1 linha no bloco verbose | Nenhum |
| `synesis_coder/modes/abstract_mode.py` | 1 linha no bloco verbose | Nenhum |
| `synesis_coder/modes/document_mode.py` | 1 linha no bloco verbose | Nenhum |
| `synesis_coder/modes/ontology_mode.py` | 1 linha no bloco verbose | Nenhum |

### Novo

| Arquivo | Conteudo |
|---------|----------|
| `synesis_coder/token_usage.py` | Classe `TokenUsage` (~50 linhas) |

### Nao afetados

`cli.py`, `validator.py`, `prompt_builder.py`, `project_loader.py`, `__init__.py`, `__main__.py`.

> **Nota:** `validator.py` **nao precisa de alteracao** com esta abordagem. O validator chama `llm_client.fix()` / `fix_async()`, que internamente chama `call()` / `call_async()`, que chama `_call_sync_inner()`, que chama `_record_usage()`. Os tokens de correcao ja sao acumulados automaticamente no `client.usage`.

### Testes existentes

Nenhum teste existente quebra:

- `test_item_mode.py` (17 testes): nao afetado — `process_item()` continua retornando `str`, `client.call()` continua retornando `str`
- `test_abstract_mode.py` (10 testes): nao afetado — `client.call_async()` continua retornando `str`
- `test_document_mode.py` (13 testes): nao afetado
- `test_ontology_mode.py` (18 testes): nao afetado

---

## 6. Testes Novos

### `tests/test_token_usage.py`

| Teste | Descricao |
|-------|-----------|
| `test_record_accumulates` | Multiplas chamadas a `record()` somam corretamente |
| `test_total_tokens_property` | `total_tokens == input_tokens + output_tokens` |
| `test_summary_line_format` | String formatada contem "in", "out", "total", "calls" |
| `test_summary_line_with_corrections` | Inclui "corrections" quando `corrections > 0` |
| `test_summary_line_without_corrections` | Nao inclui "corrections" quando `corrections == 0` |
| `test_reset` | `reset()` zera todos os campos |
| `test_thread_safety` | 10 threads concorrentes chamam `record()` sem race condition |

### Verificacao de integracao

Nos testes de integracao existentes que usam `format="verbose"` (`test_item_verbose_format`, `test_process_document_verbose_format`), adicionar uma assertion verificando que a linha de tokens aparece no output:

```python
assert "tokens:" in result  # presente no header verbose
```

---

## 7. Fases de Implementacao

### Fase 1 — Infraestrutura (token_usage.py + llm_client.py)

**Objetivo:** Acumular tokens em todas as chamadas LLM, sem exibir nada ainda.

**Arquivos:** `synesis_coder/token_usage.py` (novo), `synesis_coder/llm_client.py`

**Tarefas:**

1. Criar `synesis_coder/token_usage.py` com a classe `TokenUsage`
2. No `LLMClient.__init__()`, adicionar `self.usage = TokenUsage()` (apos linha 143)
3. Em `_record_usage()` (linha 398-403), adicionar `self.usage.record(usage.input_tokens, usage.output_tokens)` apos as linhas existentes de rate-limiting
4. No branch OpenAI de `_call_sync_inner()` (linha 288-296), registrar tokens com `if response.usage: self.usage.record(response.usage.prompt_tokens, response.usage.completion_tokens)`
5. Criar `tests/test_token_usage.py` com os 7 testes unitarios

**Validacao:**

```bash
pytest tests/test_token_usage.py -v
pytest tests/ -v -k "not integration"   # todos os testes existentes passam
```

**Risco:** Nenhum. Aditivo puro, sem mudanca de interface.

---

### Fase 2 — Exibicao nos modos (5 modos)

**Objetivo:** Mostrar `summary_line()` no output verbose de cada modo.

**Arquivos:** `item_mode.py`, `suggest_mode.py`, `abstract_mode.py`, `document_mode.py`, `ontology_mode.py`

**Tarefas:**

1. Em `item_mode.py:63-68` — adicionar `f"# {client.usage.summary_line()}\n"` ao header verbose
2. Em `suggest_mode.py:92-98` — adicionar linha similar ao header verbose
3. Em `abstract_mode.py:338-344` — adicionar linha ao header verbose
4. Em `document_mode.py:644-652` — adicionar linha ao header verbose
5. Em `ontology_mode.py:355-361` — adicionar linha ao header verbose
6. Atualizar assertions nos testes de integracao verbose (`test_item_verbose_format`, `test_process_document_verbose_format`) para verificar a presenca de `"tokens:"` no output

**Validacao:**

```bash
# Testes unitarios (todos)
pytest tests/ -v -k "not integration"

# Teste manual rapido
synesis-coder item \
  --project path/to/project.synp \
  --bibref smith2024 \
  --text "Community trust is key." \
  --format verbose
```

**Risco:** Nenhum. Adiciona uma linha ao output verbose — o output plain permanece inalterado.

---

### Fase 3 (opcional) — Marcacao de correcoes

**Objetivo:** Distinguir chamadas de geracao vs correcao no acumulador.

**Arquivos:** `synesis_coder/llm_client.py`

**Tarefas:**

1. Adicionar `self._next_is_correction: bool = False` ao `__init__`
2. Em `_record_usage()` e no branch OpenAI: passar `is_correction=self._next_is_correction` para `self.usage.record()` e resetar o flag
3. Em `fix()` (linha 188-203): adicionar `self._next_is_correction = True` antes de `return self.call(...)`
4. Em `fix_async()` (linha 244-260): adicionar `self._next_is_correction = True` antes de `return await self.call_async(...)`

**Nota sobre thread-safety:** `_next_is_correction` e escrito antes da chamada e lido dentro da mesma chamada. Nos modos concorrentes, cada `fix_async()` e precedido pela escrita do flag e seguido pela leitura em `_record_usage()` dentro do mesmo `asyncio.to_thread()`. Porem, ha um risco teorico de race condition: se duas threads executam `fix()` simultaneamente, o flag pode ser sobrescrito. Para garantir seguranca, o flag deve ser protegido por `self.usage._lock` ou substituido por um `threading.local()`.

**Alternativa segura sem flag**: contar `corrections` no nivel do modo. O validator retorna `success: bool` e o numero de tentativas e conhecido. O modo pode calcular `corrections = total_calls - initial_calls`. Isso evita qualquer complicacao de thread-safety.

**Recomendacao:** Implementar apenas se a contagem de correcoes for realmente util apos avaliar o uso real na Fase 2. A informacao de `calls` total ja e suficiente na maioria dos casos.

---

## 8. Resumo

| # | Fase | Arquivos | Testes quebrados | Risco |
|---|------|----------|-----------------|-------|
| 1 | Infraestrutura | 2 (1 novo + 1 mod) | 0 | Nenhum |
| 2 | Exibicao | 5 modos | 0 | Nenhum |
| 3 | Correcoes (opcional) | 1 mod | 0 | Baixo (thread-safety) |

**Dependencias:** Fase 2 depende de Fase 1. Fase 3 e independente (pode ser implementada a qualquer momento apos Fase 1).

**Nota sobre `cli.py`:** Nenhuma alteracao necessaria. O CLI chama `click.echo(result)` onde `result` e a string retornada pelo modo. A linha de tokens faz parte dessa string quando `format="verbose"`.
