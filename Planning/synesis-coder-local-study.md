# Plano: Suporte a Modelos Locais e Remotos via API OpenAI-Compatible

## Contexto

O synesis-coder usa exclusivamente a API Anthropic (claude-opus-4-6 por padrão). O objetivo é
adicionar suporte a modelos alternativos via qualquer servidor compatível com a API OpenAI —
incluindo Ollama (local), RunPod, Together AI, e similares.

**Estratégia em duas fases:**

1. **Fase 1 (este plano):** Testes locais com Qwen3, Gemma3 e **Gemma 4** via Ollama na
   RTX 3050 (6 GB VRAM). Objetivo: validar que o pipeline (prompt → geração → validação →
   correção) funciona com modelos alternativos. O **Gemma 4 E4B** (recém-lançado, Junho 2026)
   é o modelo mais promissor para esta GPU — derivado da mesma tecnologia do Gemini 3, com
   context window de 128K e forte instruction-following.

2. **Fase 2 (futura, fora do escopo):** Usar servidores cloud (RunPod, etc.) com modelos
   maiores (Gemma 4 26B MoE, Qwen3-32B, Llama4-70B) que se aproximem do desempenho de um
   Claude Sonnet 4.6. A variante **Gemma 4 26B-A4B** (MoE, 3.8B parâmetros ativos, LMArena
   1441) é especialmente interessante — requer ~8 GB VRAM, viável em RunPod/RTX 3090+.
   A abstração implementada na Fase 1 já suportará isto sem código novo — basta apontar
   `SYNESIS_CODER_API_URL` para o endpoint RunPod.

**Seleção de backend:** nova env var `SYNESIS_CODER_BACKEND=openai` (padrão: `anthropic`).

**Princípio de segurança:** O path Anthropic não é tocado. Toda a lógica existente de chamada,
tradução de mensagens, rate limiting e retry permanece encapsulada em métodos dedicados. A
bifurcação ocorre apenas nos pontos de dispatch.

---

## 1. Modelos para Testes Locais (Fase 1)

### Hardware: Dell G15 5530 — RTX 3050 6 GB VRAM, 16 GB RAM, i5

#### Gemma 4 (Google, Junho 2026) — Candidato principal

O Gemma 4 é derivado da mesma tecnologia do Gemini 3 e representa um salto significativo
em relação ao Gemma 3. Lançado sob licença Apache 2.0, com suporte nativo a multimodal
(imagem, vídeo, áudio nos modelos E2B/E4B). Requer **Ollama v0.20.0+**.

| Modelo | Arquitetura | VRAM (Q4) | Context | Viável RTX 3050? |
|--------|-------------|-----------|---------|------------------|
| `gemma4:e2b` | Dense, 2.3B efetivos | ~4 GB | 128K | **Sim** — confortável |
| `gemma4:e4b` | Dense, 4.5B efetivos | ~6 GB | 128K | **Sim** — no limite, mas cabe |
| `gemma4:26b` | MoE 128 experts, 3.8B ativos | ~18 GB (Q4) | 256K | **Não** — requer 8+ GB VRAM |
| `gemma4:31b` | Dense, 30.7B | ~20 GB (Q4) | 256K | **Não** — requer 24+ GB VRAM |

> **Destaque: `gemma4:e4b`** — Com 4.5B parâmetros efetivos e context window de 128K, é o
> modelo mais capaz que cabe na RTX 3050. A arquitetura Gemma 4 (RoPE dual, sliding+global
> attention) melhora significativamente o instruction-following em comparação ao Gemma 3.
> O LMArena score da família Gemma 4 supera modelos densos maiores para a mesma contagem
> de parâmetros ativos.

#### Qwen3 (Alibaba) — Comparativo

| Modelo | VRAM (Q4) | Context | Viável RTX 3050? |
|--------|-----------|---------|------------------|
| `qwen3:8b` | ~5.5 GB | 32K | **Sim** — no limite |
| `qwen3:1.7b` | ~1.5 GB | 32K | **Sim** — smoke test apenas |

#### Gemma 3 (Google, legado)

| Modelo | VRAM (Q4) | Context | Viável RTX 3050? |
|--------|-----------|---------|------------------|
| `gemma3:4b` | ~3.5 GB | 32K | **Sim** — referência para comparação com Gemma 4 |

#### Plano de testes recomendado

1. **`gemma4:e4b`** — Teste principal. Melhor relação qualidade/VRAM disponível.
   Context window de 128K acomoda facilmente o system prompt de ~5000-6000 tokens.
2. **`qwen3:8b`** — Comparativo de outra família. Mais parâmetros, mas context 4x menor.
3. **`gemma4:e2b`** — Validação rápida de pipeline. Rápido, mas qualidade limitada.
4. **`gemma3:4b`** — Baseline para medir a evolução Gemma 3 → Gemma 4.

### Expectativa de qualidade com o template social_acceptance

O template `social_acceptance.synt` é particularmente exigente:

- **Campo `chain`**: ~230 linhas de GUIDELINES com regras de direção causal, seleção de
  relações, granularidade de fatores, padrões sequenciais vs paralelos
- **Campo `text`**: Scoring de valor analítico 1-5 com critérios detalhados
- **Campo `note`**: Regras de *borderline*, *complex*, limites de palavras
- **Modo ontology**: 8 campos com instruções complexas (Dooyeweerd, Wüstenhagen, RGT)

O system prompt montado pelo `prompt_builder.py` para este projeto atinge **~5000-6000 tokens**
(GUIDELINES expandidas + code_index + topic_index + regras de formato). Cabe confortavelmente
no context window de 128K do Gemma 4, mas a **capacidade de seguir instruções complexas em
modelos < 8B é significativamente inferior** ao Claude.

**Expectativa realista para Fase 1:**
- **Gemma 4 E4B (4.5B):** geração de ITEM blocks sintaticamente válidos na maioria dos casos;
  context window de 128K é vantagem clara sobre Qwen3 (32K); instruction-following derivado
  do Gemini 3 deve produzir CHAINs razoáveis mas com granularidade de conceitos imprecisa
- **Qwen3 8B:** mais parâmetros que Gemma 4 E4B, potencialmente melhor raciocínio; context
  menor (32K) mas suficiente para o system prompt
- O loop de validação+correção (`validator.py`) corrige erros sintáticos mas não melhora
  a qualidade semântica
- O modo `ontology` (8 campos interrelacionados, classificação Dooyeweerd) provavelmente
  excederá a capacidade de modelos < 26B

**Expectativa para Fase 2 (RunPod com modelos maiores):**
- **Gemma 4 26B MoE** (3.8B ativos, ~8 GB VRAM, LMArena 1441) — candidato forte para
  RunPod; relação qualidade/custo excepcional; especialmente bom em structured output
- **Qwen3-32B, Llama4-70B** — alternativas dense para comparação
- Viabilidade dos 4 modos (item, abstract, document, ontology)

---

## 2. Arquitetura: Análise de Impacto

### Arquivo principal: `synesis_coder/llm_client.py`

Toda a integração com a API Anthropic está encapsulada neste arquivo (362 linhas). Nenhum
outro módulo importa `anthropic`. As mudanças ficam concentradas aqui.

### Módulos que NÃO precisam mudar

| Módulo | Razão |
|--------|-------|
| `prompt_builder.py` | Produz formato interno agnóstico `[{"role", "content", "cache"}]`. O campo `cache` é simplesmente ignorado pelo backend OpenAI na tradução. |
| `validator.py` | Usa `synesis.load()` e chama `llm_client.fix()` / `fix_async()` — interface inalterada. |
| `project_loader.py` | Não toca no LLM. |
| `modes/item_mode.py` | Instancia `LLMClient(model=model)` na linha 50 — sem alteração. |
| `modes/abstract_mode.py` | Instancia `LLMClient(model=model)` na linha 280 — sem alteração. |
| `modes/document_mode.py` | Instancia `LLMClient(model=model)` na linha 559 — sem alteração. |
| `modes/ontology_mode.py` | Instancia `LLMClient(model=model)` na linha 299 — sem alteração. |
| `cli.py` | Opção `--model` já existe em todos os 4 subcomandos — sem alteração. |

**Importante:** os modes **não passam `backend=`** — usam apenas `model=`. O backend é
determinado pela env var. Isso é intencional: o backend é uma escolha de infraestrutura
(onde rodar), não uma escolha per-request.

### Novos arquivos

Nenhum.

### Dependência nova: `openai>=1.0`

Adicionada como dependência **core** em `pyproject.toml`:

```toml
dependencies = [
    "synesis>=0.3.0",
    "anthropic>=0.40.0",
    "openai>=1.0",           # <-- nova: backend OpenAI-compatible (Ollama, RunPod, etc.)
    "click>=8.0",
    "tenacity>=8.0",
    "bibtexparser>=1.4",
    "python-dotenv>=1.0",
]
```

**Justificativa:** o SDK `openai` é leve (~2 MB) e não tem dependências transitivas pesadas.
Incluí-lo como core evita complexidade desnecessária de optional dependencies e garante que
`pip install synesis-coder` instala tudo de uma vez. A presença do pacote `openai` instalado
não afeta o path Anthropic — é importado via lazy import apenas quando `backend=openai`.

---

## 3. Implementação Detalhada

### 3.1 Novas env vars (`.env.example`)

```dotenv
# Backend: "anthropic" (default) or "openai" (Ollama, RunPod, Together, etc.)
# SYNESIS_CODER_BACKEND=anthropic

# Base URL for OpenAI-compatible API (only when BACKEND=openai)
# Local Ollama:  http://localhost:11434
# RunPod:        https://{pod-id}-{port}.proxy.runpod.net
# Together:      https://api.together.xyz
# SYNESIS_CODER_API_URL=http://localhost:11434

# API key for OpenAI-compatible backend (Ollama ignores this; RunPod/Together require it)
# SYNESIS_CODER_API_KEY=

# When using backend=openai locally with Ollama, --concurrent 1 is recommended
# (GPU processes one request at a time; higher values just enqueue)
```

> **Nota sobre nomes de env vars:** `SYNESIS_CODER_API_URL` (não `_OLLAMA_URL`) porque o
> mesmo backend serve para Ollama local, RunPod, Together, e qualquer servidor
> OpenAI-compatible. `SYNESIS_CODER_API_KEY` é separada da `ANTHROPIC_API_KEY` porque são
> credenciais de serviços diferentes.

### 3.2 `pyproject.toml`

Apenas adicionar `"openai>=1.0"` à lista `dependencies`. Sem `[local]` extras.

### 3.3 `llm_client.py` — mudanças detalhadas

O arquivo tem 362 linhas. As mudanças tocam 6 áreas:

#### a) Constantes novas (após linha 42)

```python
_DEFAULT_BACKEND = "anthropic"
_DEFAULT_API_URL = "http://localhost:11434"
```

#### b) Helpers novos (após `_get_max_retries`)

```python
def _get_backend() -> str:
    return os.environ.get("SYNESIS_CODER_BACKEND", _DEFAULT_BACKEND).lower()

def _get_api_url() -> str:
    return os.environ.get("SYNESIS_CODER_API_URL", _DEFAULT_API_URL)

def _get_api_key() -> str:
    """API key para backend OpenAI-compatible (Ollama ignora, RunPod requer)."""
    return os.environ.get("SYNESIS_CODER_API_KEY", "no-key-required")
```

> **Nota:** a função `_get_api_key()` existente (linhas 31-38) que lê `ANTHROPIC_API_KEY`
> será renomeada para `_get_anthropic_api_key()` para evitar conflito com a nova
> `_get_api_key()` do backend OpenAI.

#### c) `LLMClient.__init__` (linhas 64–97) — adicionar `backend`

Novo parâmetro:
```python
def __init__(
    self,
    model: Optional[str] = None,
    backend: Optional[str] = None,   # "anthropic" | "openai"
    max_rpm: Optional[int] = None,
    max_input_tpm: Optional[int] = None,
    max_output_tpm: Optional[int] = None,
) -> None
```

Corpo — substituir o bloco `import anthropic` / `self._client` (linhas 79–82) por:

```python
self.backend = (backend or _get_backend()).lower()

if self.backend == "openai":
    import openai
    self._client = openai.OpenAI(
        base_url=f"{_get_api_url()}/v1",
        api_key=_get_api_key(),
    )
    self._rate_limit_enabled = False
    self._retryable_errors = (openai.APIStatusError, openai.APIConnectionError)
else:
    import anthropic
    self._client = anthropic.Anthropic(api_key=_get_anthropic_api_key())
    self._rate_limit_enabled = True
    self._retryable_errors = (
        anthropic.RateLimitError,
        anthropic.APIStatusError,
    )
```

**Garantias para o path Anthropic:**
- `_get_anthropic_api_key()` é chamado **apenas** quando `backend=anthropic`
- `self._client` é `anthropic.Anthropic` — mesmo tipo, mesma inicialização
- `self._rate_limit_enabled = True` — rate limiting ativo como antes
- `self._retryable_errors` — mesmas exceções que o código atual hardcoded

#### d) `_translate_messages()` (linhas 328–361) — bifurcar

Renomear o método atual para `_translate_messages_anthropic()` (intocado) e criar dispatcher:

```python
def _translate_messages(self, messages: List[dict]) -> tuple[list, list]:
    if self.backend == "openai":
        return self._translate_messages_openai(messages)
    return self._translate_messages_anthropic(messages)
```

Novo método `_translate_messages_openai()`:
```python
def _translate_messages_openai(self, messages: List[dict]) -> tuple[list, list]:
    """Formato OpenAI: system como role normal, sem cache_control."""
    api_messages = []
    for msg in messages:
        api_messages.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    return [], api_messages  # system_blocks vazio; system vai em api_messages
```

> **Retorno `([], api_messages)`:** mantém a assinatura `tuple[list, list]`. `system_blocks`
> vazio → o `if system_blocks:` na linha 239 do `_call_sync_inner` não injeta `system` no
> kwargs. Correto: na API OpenAI, system vai dentro de `messages`.

> **`_translate_messages_anthropic()`:** é exatamente o código atual (linhas 328-361),
> apenas renomeado. **Nenhuma linha alterada.** Produz `system_blocks` separados com
> `cache_control` — comportamento Anthropic preservado bit a bit.

#### e) `_call_sync_inner()` (linhas 214–246) — bifurcar lógica de chamada

O método atual define um `@retry` decorator inline com exceções hardcoded do Anthropic
(linha 227: `anthropic.RateLimitError, anthropic.APIStatusError`). Mudar para
`self._retryable_errors`:

```python
def _call_sync_inner(self, messages, temperature, max_tokens):
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

    system_blocks, api_messages = self._translate_messages(messages)

    @retry(
        retry=retry_if_exception_type(self._retryable_errors),  # <- dinâmico
        stop=stop_after_attempt(_get_max_retries()),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        reraise=True,
    )
    def _call_with_retry() -> str:
        if self.backend == "openai":
            response = self._client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        else:
            # --- PATH ANTHROPIC: INALTERADO ---
            kwargs = {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": api_messages,
            }
            if system_blocks:
                kwargs["system"] = system_blocks
            response = self._client.messages.create(**kwargs)
            self._record_usage(response.usage)
            return response.content[0].text

    return _call_with_retry()
```

> **`self._retryable_errors` no decorator:** o `@retry` é recriado a cada chamada (closure
> local dentro do método) — comportamento atual inalterado. `self._retryable_errors` é
> acessível no escopo do closure. Para Anthropic, é exatamente a mesma tupla de exceções.

> **`_record_usage`** não é chamado no path OpenAI porque `self._rate_limit_enabled = False`
> e não há necessidade de tracking de tokens para servidores locais ou cloud sem cota RPM.

#### f) Rate limiting — condicionar ao flag

`_wait_if_rate_limited()` (linha 284) e `_async_wait_if_rate_limited()` (linha 248):
adicionar `if not self._rate_limit_enabled: return` **como primeira linha de cada método**.

`_record_usage()` (linha 317): adicionar `if not self._rate_limit_enabled: return`.

O resto do corpo de cada método permanece **intocado**. Para `backend=anthropic`, o flag é
`True` e o comportamento é idêntico ao atual.

> **Nota sobre `response.usage` de servidores OpenAI-compatible:** Ollama retorna usage no
> formato OpenAI (`prompt_tokens`, `completion_tokens`), não Anthropic (`input_tokens`,
> `output_tokens`). Se no futuro quisermos rate limiting para RunPod (que tem cotas),
> será necessário adaptar `_record_usage()`. Por ora, desabilitado para backend OpenAI.

---

## 4. Verificação de Integridade: Path Anthropic

Checklist de não-quebra para o backend Anthropic:

| Ponto | Verificação | Status |
|-------|-------------|--------|
| `_get_anthropic_api_key()` | Chamado apenas quando `backend=anthropic` | OK — mesmo comportamento |
| `self._client` | `anthropic.Anthropic(api_key=...)` — tipo inalterado | OK |
| `_translate_messages_anthropic()` | Código atual renomeado, nenhuma linha alterada | OK |
| `system_blocks` + `cache_control` | Produzidos por `_translate_messages_anthropic()` — inalterado | OK |
| `self._client.messages.create(**kwargs)` | Path else em `_call_with_retry` — inalterado | OK |
| `response.content[0].text` | Acesso ao resultado Anthropic — inalterado | OK |
| `self._record_usage(response.usage)` | Chamado no path Anthropic — `usage.input_tokens` etc. | OK |
| Rate limiting | `self._rate_limit_enabled = True` para Anthropic — todos os waits ativos | OK |
| Retry exceptions | `(anthropic.RateLimitError, anthropic.APIStatusError)` — mesma tupla | OK |
| `_wait_if_rate_limited` | Guard `if not self._rate_limit_enabled` é `False` para Anthropic → executa normalmente | OK |

**Teste de regressão:** rodar a suite existente `pytest tests/ -v -k "not integration"` após
a implementação. Se todos os 17 testes de `test_item_mode.py` + 15 de `test_ontology_mode.py`
passam, o path Anthropic está intacto.

---

## 5. Riscos e Mitigações

### 5.1 Qualidade com templates complexos (social_acceptance)

**Risco ALTO:** O template `social_acceptance.synt` exige:
- Seleção de 5 tipos de relações CHAIN com regras de prioridade e direção
- Scoring de valor analítico 1-5 antes de extração
- Limites de palavras em campos MEMO
- Classificação Dooyeweerd (15 aspectos modais) no modo ontology
- Construtos bipolares RGT (Repertory Grid Theory)

Modelos < 32B provavelmente não seguirão consistentemente estas instruções.

**Mitigação:** O pipeline de validação (`validator.py`) captura erros sintáticos. Erros
semânticos (relação CHAIN incorreta, aspecto Dooyeweerd errado) **não são detectáveis**
pelo compilador — requerem revisão humana.

**Recomendação:** Para o template social_acceptance, usar modelos 8B apenas como smoke test
do pipeline. Qualidade de produção requer Fase 2 (RunPod com 32B+).

### 5.2 Timeout em modelos lentos

**Risco BAIXO:** `qwen3:8b` no limite da VRAM gera ~15 tok/s. Output de 1000+ tokens ≈ 60s.
O SDK `openai` tem timeout padrão de 10 minutos.

**Mitigação:** Nenhuma ação necessária.

### 5.3 Servidor não iniciado (Ollama)

**Risco MÉDIO:** `openai.OpenAI()` não valida conexão na criação. Falha na primeira chamada
com `ConnectionRefusedError`.

**Mitigação:** Capturado por `tenacity` retry (`openai.APIConnectionError` incluído na tupla
de exceções retryable). Após todas as tentativas, propaga com mensagem clara.

### 5.4 Concorrência com GPU local

**Risco MÉDIO:** Modos `abstract`, `document` e `ontology` usam `Semaphore` (padrão 3-5).
Ollama serializa na GPU — requests extras ficam na fila interna.

**Mitigação:** Funciona sem alteração (fila transparente). Documentar no `.env.example`
que `--concurrent 1` é recomendado para Ollama local.

### 5.5 Compatibilidade API entre servidores

**Risco BAIXO:** Ollama, vLLM, TGI, e RunPod endpoints todos implementam o subset
`/v1/chat/completions` que usamos (`model`, `messages`, `temperature`, `max_tokens`).
Diferenças existem em features avançadas (function calling, structured output) que
**não usamos**.

**Mitigação:** Nenhuma ação necessária. Se um servidor específico não implementar
`max_tokens`, a chamada falha com erro claro.

---

## 6. Limitações Conhecidas

| Feature Anthropic | Status com backend OpenAI |
|---|---|
| Prompt caching (`cache_control`) | **Ignorado** — campo `cache` descartado na tradução |
| Rate limiting (RPM/TPM) | **Desabilitado** — sem cotas locais; RunPod pode ter, mas por ora desabilitado |
| Token usage tracking | **Desabilitado** — formato OpenAI (`prompt_tokens`) difere do Anthropic (`input_tokens`) |
| Retry em rate limit | Retry em `APIStatusError` + `APIConnectionError` mantido; sem `RateLimitError` específico |
| Qualidade de output | Dependente do modelo; < 8B insuficiente para templates complexos |
| Concorrência GPU local | Serializada pelo Ollama — `--concurrent` > 1 apenas enfileira |

---

## 7. Arquivos a Modificar

| Arquivo | Natureza da mudança | Escopo |
|---------|---------------------|--------|
| `synesis_coder/llm_client.py` | Abstração de backend (bifurcação `__init__`, `_translate`, `_call_sync_inner`, rate limiting) | ~80 linhas novas/modificadas |
| `pyproject.toml` | Adicionar `"openai>=1.0"` em `dependencies` | 1 linha |
| `.env.example` | Adicionar `SYNESIS_CODER_BACKEND`, `SYNESIS_CODER_API_URL`, `SYNESIS_CODER_API_KEY` | ~8 linhas |
| `CHANGELOG.md` | Entrada `[0.1.2] — Unreleased` | ~10 linhas |

---

## 8. Setup e Verificação (pós-implementação)

### 8.1 Instalar Ollama e modelos

```bash
# 1. Instalar Ollama v0.20.0+ (necessário para Gemma 4)
winget install Ollama.Ollama
ollama --version   # verificar >= 0.20.0

# 2. Baixar modelos de teste (ordem de prioridade)
ollama pull gemma4:e4b     # Teste principal — Gemma 4, 4.5B efetivos, 128K ctx (~6 GB VRAM)
ollama pull qwen3:8b       # Comparativo Alibaba (~5.5 GB VRAM)
ollama pull gemma4:e2b     # Smoke test rápido — Gemma 4, 2.3B efetivos (~4 GB VRAM)
ollama pull gemma3:4b      # Baseline legado para comparação (~3.5 GB VRAM)

# 3. Reinstalar synesis-coder (agora inclui openai SDK)
cd d:/GitHub/synesis-coder
pip install -e ".[dev]"
```

### 8.2 Configurar `.env`

```dotenv
# Para testes locais com Ollama:
SYNESIS_CODER_BACKEND=openai
SYNESIS_CODER_API_URL=http://localhost:11434
SYNESIS_CODER_MODEL=qwen3:8b

# Para RunPod (Fase 2):
# SYNESIS_CODER_BACKEND=openai
# SYNESIS_CODER_API_URL=https://{pod-id}-8000.proxy.runpod.net
# SYNESIS_CODER_API_KEY=rp_xxxxxxxx
# SYNESIS_CODER_MODEL=Qwen/Qwen3-32B
```

### 8.3 Testes de regressão (path Anthropic)

```bash
# PRIMEIRO: garantir que nada quebrou no backend Anthropic
# (requer ANTHROPIC_API_KEY no .env, SYNESIS_CODER_BACKEND=anthropic ou ausente)
set SYNESIS_CODER_BACKEND=anthropic
pytest tests/ -v -k "not integration"
pytest tests/ -v -m integration   # se ANTHROPIC_API_KEY disponível
```

### 8.4 Smoke tests com Ollama

```bash
set SYNESIS_CODER_BACKEND=openai
set SYNESIS_CODER_API_URL=http://localhost:11434

# Teste 1: Gemma 4 E4B — candidato principal
set SYNESIS_CODER_MODEL=gemma4:e4b
synesis-coder item \
  --project d:/GitHub/case-studies/Sociology/Social_Acceptance/analysis.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition to wind energy." \
  --format verbose

# Teste 2: Qwen3 8B — comparativo
set SYNESIS_CODER_MODEL=qwen3:8b
synesis-coder item \
  --project d:/GitHub/case-studies/Sociology/Social_Acceptance/analysis.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition to wind energy." \
  --format verbose

# Teste 3: Gemma 4 E2B — smoke test rápido (qualidade menor, muito rápido)
set SYNESIS_CODER_MODEL=gemma4:e2b
synesis-coder item \
  --project d:/GitHub/case-studies/Sociology/Social_Acceptance/analysis.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition to wind energy." \
  --format verbose

# Teste 4: Gemma 3 4B — baseline para medir evolução Gemma 3 → 4
set SYNESIS_CODER_MODEL=gemma3:4b
synesis-coder item \
  --project d:/GitHub/case-studies/Sociology/Social_Acceptance/analysis.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition to wind energy." \
  --format verbose

# Teste 5: voltar ao Anthropic — confirmar que o path padrão continua intacto
set SYNESIS_CODER_BACKEND=anthropic
synesis-coder item \
  --project d:/GitHub/case-studies/Sociology/Social_Acceptance/analysis.synp \
  --bibref ashworth2019 \
  --text "Local ownership models significantly reduce opposition to wind energy." \
  --format verbose
```

### 8.5 Testes automatizados novos

**Novo arquivo:** `tests/test_llm_backend.py`

**Unit tests (sem LLM, sem rede):**

| Teste | O que verifica |
|-------|---------------|
| `test_backend_default_is_anthropic` | `_get_backend()` retorna `"anthropic"` sem env var |
| `test_backend_from_env` | `_get_backend()` lê `SYNESIS_CODER_BACKEND` |
| `test_api_url_default` | `_get_api_url()` retorna `http://localhost:11434` |
| `test_api_url_from_env` | `_get_api_url()` lê `SYNESIS_CODER_API_URL` |
| `test_translate_openai_no_cache_control` | Mensagens OpenAI não contêm `cache_control` |
| `test_translate_openai_system_as_role` | System message vira `{"role": "system", ...}` em `api_messages` |
| `test_translate_anthropic_unchanged` | Backend anthropic produz `system_blocks` separados (regressão) |
| `test_rate_limit_disabled_for_openai` | `_rate_limit_enabled` é `False` quando backend=openai |
| `test_rate_limit_enabled_for_anthropic` | `_rate_limit_enabled` é `True` quando backend=anthropic |

**Integration tests (requer Ollama rodando localmente, marcados `@pytest.mark.integration`):**

| Teste | O que verifica |
|-------|---------------|
| `test_openai_item_compiles` | Output do backend OpenAI compila com `synesis.load()` |
| `test_openai_correction_loop` | `validator.py` consegue corrigir output inválido via backend OpenAI |

---

## 9. Ordem de Execução

1. Renomear `_get_api_key()` → `_get_anthropic_api_key()` em `llm_client.py`
2. Adicionar constantes e helpers novos (`_get_backend`, `_get_api_url`, `_get_api_key`)
3. Refatorar `__init__` com bifurcação de backend + `_retryable_errors`
4. Renomear `_translate_messages()` → `_translate_messages_anthropic()` (código intocado)
5. Criar `_translate_messages_openai()` e dispatcher `_translate_messages()`
6. Bifurcar `_call_sync_inner()` (path OpenAI + retry dinâmico)
7. Condicionar rate limiting ao flag `_rate_limit_enabled` (3 métodos: 1 linha cada)
8. Adicionar `"openai>=1.0"` em `pyproject.toml` dependencies
9. Atualizar `.env.example` com novas env vars
10. Rodar `pytest tests/ -v -k "not integration"` — todos devem passar (regressão Anthropic)
11. Instalar Ollama + modelos e rodar smoke tests
12. Criar `tests/test_llm_backend.py` com unit + integration tests
13. CHANGELOG

---

## 10. Fora do Escopo (Fase 1)

- **Gemma 4 26B MoE em RunPod** — candidato forte para Fase 2 (LMArena 1441 com 3.8B
  ativos, ~8 GB VRAM, bom em structured output); a abstração OpenAI-compatible
  implementada aqui já suporta, basta apontar `SYNESIS_CODER_API_URL`
- Rate limiting para RunPod/Together (Fase 2 — adaptar `_record_usage` para formato OpenAI)
- UI no synesis-explorer para seleção de backend
- Benchmarks comparativos sistemáticos Anthropic vs modelos locais/cloud
- Streaming de resposta
- Ajuste de prompts para modelos menores (few-shot, simplificação de GUIDELINES)
- Opção `--backend` no CLI (backend é infraestrutura, determinado por env var)
