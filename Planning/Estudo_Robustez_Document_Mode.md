# Estudo de Robustez — `document` mode do synesis-coder

**Data:** 2026-06-11
**Escopo:** Diagnóstico da causa-raiz do alto índice de chunks `ok=False` no
modo `document`, com proposta de correções baseadas exclusivamente em
estratégias de codificação consagradas. **Nenhum código foi alterado.**
**Caso de teste:** `01_Martín-Gómez-Ravetti_3355559305779367.md` (95 328 chars,
12 chunks), template `lattes.synt` (campo CHAIN renomeado para `relacao_aplicada`).

---

## 1. Sintoma observado

Em execuções repetidas do `document` mode sobre o currículo Lattes, a taxa de
sucesso por chunk ficou consistentemente baixa, **independentemente do modelo**:

| Modelo | Tempo | ITEMs finais | Chunks OK / total | Correções |
|--------|------:|-------------:|:-----------------:|----------:|
| gemini-3.5-flash | 259 s | 51 | 3 / 12 | 30 |
| gemini-2.5-pro   | 291 s | 32 | 3 / 12 | 34 |

O fato de o modelo mais capaz (`pro`) **não** melhorar a taxa de sucesso é a
principal evidência de que o gargalo **não é qualidade de raciocínio do LLM**,
mas sim a lógica interna de captura/validação/deduplicação do coder.

> Observação preliminar já resolvida fora deste estudo: o `--bibref` numérico
> (`@3355559305779367`) é rejeitado pelo gram­ática (`BIBREF: "@" /[a-zA-Z]…/`).
> O bibref correto é `@lattes-3355559305779367`. Isto foi confirmado e não faz
> parte das causas abaixo.

---

## 2. Metodologia do diagnóstico

Sondagem isolada de cada chunk que falhou, capturando o **output bruto do LLM**
(`call_async`) antes de validação, contando headers `ITEM`, ocorrências de
`END ITEM` e presença de `SOURCE`, e rodando `synesis.load()` diretamente sobre
o resultado de `_extract_annotation_blocks`.

Resultado da sondagem (chunks que falharam no run real):

```
chunk 1: len=3681, END_ITEM=6, has_SOURCE=False   ← válido, mas marcado ok=False
chunk 2: len=5250, END_ITEM=6, has_SOURCE=False   ← válido, mas marcado ok=False
chunk 3: len=1124, END_ITEM=0, has_SOURCE=False   ← pouco/nenhum ITEM
chunk 5: len=0,    END_ITEM=0  (vazio)            ← output vazio do LLM
chunk 8: len=634,  END_ITEM=1, has_SOURCE=False
chunk 10: len=0,   END_ITEM=0  (vazio)            ← output vazio do LLM
```

Numa segunda sondagem do chunk 1 (9 headers `ITEM`, apenas 8 `END ITEM`):

```
raw ITEM headers (count): 9
item_pat matches:         8     ← 1 ITEM perdido
raw END ITEM count:       8     ← último ITEM truncado, sem END ITEM
```

E a validação direta do chunk 1 extraído:

```
structural_errors: False        ← deveria ser ok=True
total errors: 1  (OrphanItem)   ← único "erro" é esperado e ignorado
```

---

## 3. Causas-raiz identificadas

### Causa A — Truncamento silencioso por `max_tokens` (alto impacto)

**Onde:** [llm_client.py:421-427](synesis-coder/synesis_coder/llm_client.py#L421)
(branch OpenAI-compat) e o branch Anthropic análogo (475-481).

```python
msg = response.choices[0].message
content = msg.content or ""
if not content.strip():
    content = getattr(msg, "reasoning_content", None) or ""
return content
```

O código **nunca inspeciona `finish_reason`** (`response.choices[0].finish_reason`).
Quando o provedor trunca a resposta por atingir `max_tokens` (default **4096**,
[llm_client.py:235](synesis-coder/synesis_coder/llm_client.py#L235)), o último
bloco ITEM volta sem `END ITEM`. A regex de extração
(`^ITEM\s+@\S+.*?^END ITEM`) descarta esse bloco incompleto, e o texto truncado
remanescente pode ainda quebrar o parse.

**Por que dispara aqui:** cada ITEM do Lattes carrega um `trecho:` (QUOTATION)
literal de 400–600 chars. 8–9 ITEMs por chunk facilmente excedem 4096 tokens de
saída. A evidência "9 headers / 8 END ITEM" é a assinatura clássica de
truncamento.

### Causa B — Output vazio em chunks de listas longas/homogêneas (alto impacto)

**Onde:** mesma região, [llm_client.py:422-427](synesis-coder/synesis_coder/llm_client.py#L422).

Chunks 5 e 10 retornaram `len=0`. São exatamente as seções de **listas densas e
repetitivas** (publicações em série; "Orientações → TCC → Concluídas"). Com
`temperature=0`, modelos tendem a degenerar (loop ou recusa silenciosa) sobre
listas longas homogêneas, devolvendo string vazia.

O coder trata `content` vazio apenas como fallback para `reasoning_content`; se
ambos estão vazios, **retorna `""` sem erro nem retry**. O chunk então produz 0
ITEMs e é contado como falha.

### Causa C — Deduplicação por assinatura usa nome de campo inexistente (médio-alto impacto)

**Onde:** [document_mode.py:347](synesis-coder/synesis_coder/modes/document_mode.py#L347),
dentro de `_item_signature`.

```python
text_match = re.search(r"^\s*text\s*:\s*(.+?)(?=\n\s*\w|\Z)", item_text, …)
```

A assinatura procura um campo `text:`. O template lattes usa `trecho:` (e o
campo canônico de citação no Synesis é `quotation`/`text` conforme o template).
Como **nenhum ITEM contém `text:`**, a assinatura reduz-se às tuplas de chain.

Consequência: dois ITEMs com a **mesma chain** mas `trecho:` distintos passam a
ter assinaturas idênticas. Com o limiar de overlap de **60%**
([document_mode.py:398](synesis-coder/synesis_coder/modes/document_mode.py#L398)),
ITEMs legítimos e distintos são colapsados como duplicatas. Isto explica a
redução agressiva observada (51 → 26 ITEMs).

> Nota: este é o mesmo padrão de "heurística de proximidade/similaridade que
> remove dados legítimos" já registrado em memória para o compilador
> (CHAIN last-occurrence bug). A lição se repete: dedup por similaridade
> parcial é perigoso.

### Causa D — Fix-loop não distingue "vazio/truncado" de "inválido" (médio impacto)

**Onde:** [validator.py:136-180](synesis-coder/synesis_coder/validator.py#L136)
(`validate_and_fix_async`).

O loop pede correção ao LLM enviando `output` + diagnóstico. Quando `output`
está **vazio** (Causa B) ou **truncado** (Causa A), o pedido de correção não tem
material útil para corrigir — o LLM frequentemente devolve vazio de novo.
Resultado: gastam-se as 3 tentativas (custo + latência: 30–36 correções por run)
sem recuperar o chunk. O loop está bem desenhado para *erros de validação*, mas
não para *ausência de conteúdo*.

---

## 4. Propostas de correção (estratégias consagradas)

Todas as propostas são padrões estabelecidos. **Nenhuma altera a superfície
pública** (`call`/`call_async`/`fix`/`fix_async`/`usage`).

### P1 — Detectar `finish_reason == "length"` e continuar a geração *(corrige A)*

Estratégia consagrada: **continuation on truncation**. Após a chamada, ler
`response.choices[0].finish_reason` (OpenAI-compat) / `response.stop_reason`
(Anthropic). Se for `length`/`max_tokens`:

1. **Caminho mínimo (recomendado primeiro):** registrar `WARNING` explícito de
   truncamento e **descartar o último bloco incompleto** de forma determinística
   (cortar no último `END ITEM`), em vez de deixar texto parcial chegar ao
   parser. Isto torna o truncamento visível e não-fatal.
2. **Caminho completo:** reemitir a chamada com `messages + assistant(parcial) +
   "continue"` e concatenar, até `finish_reason == "stop"`. Padrão clássico de
   *output continuation*.

Combinar com **`max_tokens` dimensionado dinamicamente** (P1-bis), já que o teto
fixo de 4096 é a causa proximal. O `max_tokens` é per-response e o modelo não o
"vê" — dimensioná-lo melhor não muda o conteúdo, só evita o corte.

### P1-bis — `max_tokens` dinâmico (teto do modelo via API + estimativa por chunk) *(corrige A na raiz)*

Estratégia consagrada: **capability discovery + request sizing**. Hoje
`max_tokens=4096` é hardcoded na assinatura de `call`/`call_async`/`fix`
([llm_client.py:235](synesis-coder/synesis_coder/llm_client.py#L235)) e só o env
`SYNESIS_CODER_MAX_TOKENS` o sobrescreve. A proposta **inverte a precedência**:
o valor dinâmico passa a ser o caminho normal e o hardcoded vira apenas fallback.

**Os dois backends expõem o teto de output do modelo via API** (verificado na
doc oficial):

| Backend | Endpoint | Campo |
|---------|----------|-------|
| Anthropic | `GET /v1/models` · `models.retrieve(id)` | `max_tokens` (e `max_input_tokens`) |
| Gemini (OpenAI-compat) | `models.get` (`v1beta/models/{id}`) | `outputTokenLimit` (e `inputTokenLimit`) |
| Ollama / LM Studio / RunPod | — | normalmente **não** expõem de forma confiável |

**Distinção crítica:** o teto do modelo (ex. Gemini 2.5 Pro = 65 536; Claude =
8 192–64 000) é o **máximo absoluto**, não uma meta. Usá-lo cru como `max_tokens`
por chamada é contraproducente — `max_tokens` é uma **reserva de segurança**, não
um alvo; pedir o teto inteiro remove a rede de proteção contra respostas
descontroladas e, no backend Anthropic com thinking, colide com o orçamento de
`budget`. O valor correto por chamada é:

```
max_tokens_efetivo = min(
    teto_do_modelo,        # descoberto via API, cacheado 1×/sessão — limite de segurança
    estimativa_por_chunk,  # ~ proporcional ao input — o que de fato dimensiona
)
```

**Precedência final (hardcoded só como fallback, conforme solicitado):**

```
1. SYNESIS_CODER_MAX_TOKENS (env)          ← override manual explícito, vence tudo
2. min(teto_via_API, estimativa_por_chunk) ← dinâmico (caminho padrão)
3. 4096 hardcoded                          ← fallback se API e estimativa indisponíveis
```

**Complementaridade com P1 (não substitui):** o dimensionamento dinâmico reduz a
*frequência* de truncamento; a detecção de `finish_reason` (P1) garante que,
quando ocorrer mesmo assim, não seja silencioso. As duas medidas trabalham
juntas — P1-bis é a prevenção, P1 é a rede de segurança.

### P2 — Tratar output vazio como falha retornável, com 1 retry e jitter *(corrige B)*

Estratégia consagrada: **retry com backoff/jitter para respostas degeneradas**.
Quando `content.strip() == ""` e `reasoning_content` também vazio:

- Em vez de retornar `""` silenciosamente, levantar uma exceção *retryable*
  específica (ex. `EmptyCompletionError`) capturada pelo `@retry` já existente,
  **com `temperature` ligeiramente elevada** (ex. 0.3) na reentrada — quebra o
  modo degenerado determinístico que produz vazio em listas homogêneas.
- Limitar a 1–2 reentradas para não inflar custo.

Isto reaproveita a infraestrutura `tenacity` já presente
([llm_client.py:449-454](synesis-coder/synesis_coder/llm_client.py#L449)).

### P3 — Corrigir a assinatura de dedup e endurecer o critério *(corrige C)*

Duas mudanças, ambas consagradas:

1. **Derivar o nome do campo de citação do template**, não hardcode `text:`.
   O `ctx` já conhece os campos; usar o campo cujo tipo é `QUOTATION`
   (no lattes, `trecho`). Assim a assinatura volta a incluir o conteúdo textual.
2. **Trocar overlap-60% por igualdade exata de assinatura** (ou exigir
   coincidência de *trecho normalizado* **E** chains). É o mesmo princípio que
   resolveu os bugs de dedup do compilador: **preferir dedup por correspondência
   exata a heurística de similaridade**. Em caso de dúvida, preservar.

### P4 — Curto-circuitar o fix-loop quando não há conteúdo *(corrige D)*

Estratégia consagrada: **guard clause / fail-fast**. Antes de entrar no loop de
correção, se `output` (após `_extract_annotation_blocks`) não contém nenhum
bloco `ITEM`/`SOURCE`, **não** gastar tentativas de `fix_async`; retornar
`("", False)` imediatamente (a Causa B/A já terá sido tratada por P1/P2 na
camada do cliente). Economiza as 3 chamadas inúteis por chunk vazio.

### P5 — Chunking ciente de estrutura para seções-lista *(reforço de A/B)*

Reforço opcional, padrão consagrado de *semantic/structural chunking*: as
seções "Produção Bibliográfica" e "Orientações" são listas longas. Reduzir o
`chunk_size` efetivo para essas seções (ou dividir por item de lista) diminui o
volume de output por chamada, atacando simultaneamente o truncamento (A) e a
degeneração em listas (B), sem depender do modelo.

---

## 5. Priorização

| Prioridade | Correção | Causa | Esforço | Risco | Ganho esperado |
|:---------:|----------|:-----:|:-------:|:-----:|----------------|
| **1** | P1 (finish_reason — rede de segurança) | A | Baixo | Baixo | Alto — torna truncamento visível e não-fatal |
| **2** | P1-bis (`max_tokens` dinâmico) | A | Médio | Baixo | Alto — elimina a *causa* do truncamento |
| **3** | P3 (assinatura de dedup) | C | Baixo | Baixo | Alto — para de descartar ITEMs legítimos |
| **4** | P2 (retry de vazio + jitter) | B | Baixo | Baixo | Médio-alto — recupera chunks-lista |
| **5** | P4 (fail-fast no fix-loop) | D | Muito baixo | Muito baixo | Médio — corta custo/latência |
| **6** | P5 (chunking estrutural) | A+B | Médio | Médio | Médio — preventivo |

**Sequência recomendada:** P1 + P1-bis + P3 primeiro (maior ganho, menor risco),
medir, depois P2 + P4. P5 só se o gargalo persistir em documentos lista-pesados.
P1 e P1-bis devem entrar **juntas**: P1-bis previne, P1 protege quando a
estimativa erra para baixo.

---

## 6. Critérios de verificação (sem quebrar nada)

- **Suíte atual verde** antes e depois (`pytest tests/ -v`), em especial
  `test_document_mode.py`, `test_token_usage.py`.
- **Determinismo de conteúdo:** com um chunk curto e não-truncado, o output deve
  ser **idêntico** ao atual (P1/P2 só agem em truncamento/vazio).
- **Métrica-alvo:** no caso Ravetti, chunks `ok` deve subir de 3/12; ITEMs após
  dedup não deve cair abaixo do número de ITEMs distintos reais (validar
  manualmente que ITEMs com mesma chain e trechos diferentes sobrevivem).
- **Logs:** truncamento e vazio passam a aparecer como `WARNING` explícito
  (hoje são invisíveis).
- **Custo:** nº de correções por run deve **cair** (P4 elimina tentativas
  inúteis), não subir.

---

## 7. Plano de implementação — P1-bis (`max_tokens` dinâmico)

Plano detalhado da correção de `max_tokens` dinâmico, com pontos de mudança
exatos, garantia de não-quebra e verificação. As demais propostas (P1, P2, P3,
P4) seguem o mesmo espírito mas estão fora do escopo deste plano específico.

### 7.1 Pré-requisitos verificados

- Anthropic `models.retrieve(id)` retorna `max_tokens` e `max_input_tokens`
  (doc oficial). O SDK Python expõe `client.models.retrieve(model_id)`.
- Gemini via OpenAI-compat: o teto está em `models.get`
  (`GET v1beta/models/{id}` → `outputTokenLimit`). O endpoint `/v1/models` do
  shim OpenAI **não** garante esse campo — pode ser necessário consultar o
  endpoint nativo `v1beta`. Tratar a ausência como "indisponível" → fallback.
- Pontos de uso de `max_tokens` hoje (grep confirmado):
  - assinatura default `4096`: [llm_client.py:235](synesis-coder/synesis_coder/llm_client.py#L235),
    [264](synesis-coder/synesis_coder/llm_client.py#L264),
    [303](synesis-coder/synesis_coder/llm_client.py#L303),
    [332](synesis-coder/synesis_coder/llm_client.py#L332)
  - injeção do env override: [llm_client.py:379-381](synesis-coder/synesis_coder/llm_client.py#L379)
  - uso no kwargs OpenAI: [llm_client.py:404](synesis-coder/synesis_coder/llm_client.py#L404)
  - uso no kwargs Anthropic: [llm_client.py:458](synesis-coder/synesis_coder/llm_client.py#L458)

### 7.2 Mudança 1 — Descoberta cacheada do teto do modelo

**Arquivo:** [llm_client.py](synesis-coder/synesis_coder/llm_client.py),
novo método privado em `LLMClient`, chamado *lazy* (na 1ª necessidade) e
cacheado em atributo de instância (`self._model_output_cap: Optional[int]`,
inicializado `None` no `__init__`).

```python
def _discover_model_output_cap(self) -> Optional[int]:
    """Teto de output do modelo via API. Cacheado. None se indisponível."""
    if self._model_output_cap is not None:
        return self._model_output_cap
    try:
        if self.backend == "anthropic":
            info = self._client.models.retrieve(self.model)
            cap = getattr(info, "max_tokens", 0) or None
        else:
            # OpenAI-compat: tentar endpoint nativo Gemini; demais → None
            cap = self._discover_openai_output_cap()  # ver Mudança 1b
        self._model_output_cap = cap or 0   # 0 = "consultado, indisponível"
        return cap
    except Exception as exc:
        _log.debug("Descoberta de teto de output falhou (%s) — usando fallback", exc)
        self._model_output_cap = 0
        return None
```

- **Por que lazy + cache:** o teto é estático; consultar 1× por sessão evita
  latência e ponto de falha por chamada. `0` como sentinela "já consultei e não
  achei" impede reconsultas repetidas.
- **Mudança 1b (Gemini):** consulta opcional ao endpoint nativo
  `https://generativelanguage.googleapis.com/v1beta/models/{model}` lendo
  `outputTokenLimit`. Se a URL não for Gemini ou a chamada falhar → `None`.
  Mantém Ollama/LM Studio/RunPod no fallback sem esforço.

### 7.3 Mudança 2 — Estimativa de `max_tokens` por chamada

**Arquivo:** [llm_client.py](synesis-coder/synesis_coder/llm_client.py),
helper de módulo (junto às demais helpers, ~linha 100).

```python
_DEFAULT_MAX_TOKENS = 4096          # fallback (hoje hardcoded na assinatura)
_OUTPUT_TO_INPUT_RATIO = 1.2        # output ~ 1.2× o input no document mode
_ESTIMATE_FLOOR = 4096              # nunca pedir menos que o default atual

def _estimate_max_tokens(messages: list, model_cap: Optional[int]) -> int:
    """Estima max_tokens a partir do tamanho do input, limitado pelo teto."""
    approx_input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    estimate = max(_ESTIMATE_FLOOR, int(approx_input_tokens * _OUTPUT_TO_INPUT_RATIO))
    if model_cap:
        return min(estimate, model_cap)
    return estimate
```

- Estimativa de tokens por `len/4` é a heurística consagrada (≈4 chars/token)
  e evita dependência de tokenizer externo. Pode evoluir para `count_tokens`
  da API se a precisão exigir, mas o `len/4` é suficiente para dimensionar uma
  *reserva*.
- O `ratio` 1.2 reflete o caso document (output da ordem do trecho citado);
  fica configurável via constante para ajuste sem mudar lógica.

### 7.4 Mudança 3 — Inverter a precedência em `_call_sync_inner`

**Arquivo:** [llm_client.py:378-381](synesis-coder/synesis_coder/llm_client.py#L378).

Hoje:
```python
if thinking:
    env_max = _get_max_tokens_override()
    if env_max is not None:
        max_tokens = env_max
```

Passa a (precedência: env > dinâmico > argumento hardcoded):
```python
env_max = _get_max_tokens_override()
if env_max is not None:
    max_tokens = env_max                       # 1. override manual vence tudo
elif max_tokens == _DEFAULT_MAX_TOKENS:        # 2. só dimensionar se o chamador
    cap = self._discover_model_output_cap()    #    não pediu um valor específico
    max_tokens = _estimate_max_tokens(messages, cap)
# else: chamador passou max_tokens explícito ≠ default → respeitar
```

- **Chave de não-quebra:** o dinâmico só age quando `max_tokens` ainda é o
  default `4096` (i.e., ninguém pediu valor específico). Chamadores que passam
  `max_tokens=N` explícito continuam respeitados. O env continua soberano.
- Remover a condição `if thinking:` do override do env é correto: o env deve
  valer também sem thinking (hoje há uma lacuna — sem thinking, o env é
  ignorado). Isto é uma correção secundária coerente.
- Aplicar a mesma lógica no caminho Anthropic com thinking
  ([llm_client.py:458](synesis-coder/synesis_coder/llm_client.py#L458)): a
  fórmula `max(max_tokens, budget + 4096)` permanece — agora `max_tokens` já
  chega dimensionado, e o `max(...)` garante espaço para o thinking budget.

### 7.5 Mudança 4 — Promover o default `4096` a constante nomeada

Trocar os quatro defaults literais `max_tokens: int = 4096` (linhas 235, 264,
303, 332) por `max_tokens: int = _DEFAULT_MAX_TOKENS`. Mantém o comportamento e
torna o "valor sentinela que dispara o dinâmico" explícito e único.

### 7.6 Ordem de aplicação e superfície pública

| Passo | Mudança | Toca superfície pública? |
|:-----:|---------|:------------------------:|
| 1 | Constantes + `_estimate_max_tokens` (helper de módulo) | Não |
| 2 | `_model_output_cap` no `__init__` + `_discover_model_output_cap` | Não (privado) |
| 3 | Inverter precedência em `_call_sync_inner` | Não (interno) |
| 4 | Default `4096` → `_DEFAULT_MAX_TOKENS` nas 4 assinaturas | Não (mesmo valor) |

`call`/`call_async`/`fix`/`fix_async`/`usage` mantêm assinatura e semântica.
Os modos (`document`, `item`, `abstract`, `ontology`, etc.) não mudam.

### 7.7 Verificação específica de P1-bis

1. **Suíte verde:** `pytest tests/ -v` antes e depois.
2. **Fallback puro (sem rede):** com backend local fictício que não expõe teto,
   confirmar que `max_tokens` cai em `_estimate_max_tokens(..., None)` ≥ 4096 —
   nunca abaixo do comportamento atual.
3. **Env soberano:** com `SYNESIS_CODER_MAX_TOKENS=2000`, confirmar que a chamada
   usa 2000 mesmo com teto/estimativa maiores.
4. **Chamador explícito respeitado:** chamada `call(..., max_tokens=512)`
   continua usando 512 (suggest mode), sem dimensionamento.
5. **Descoberta cacheada:** instrumentar `_discover_model_output_cap` e confirmar
   **uma** chamada à Models API por sessão, não por request.
6. **Caso Ravetti:** medir queda na frequência de `finish_reason == length`
   (após P1 logar truncamento) — esperado próximo de zero para chunks normais.

### 7.8 Riscos e mitigação

| Risco | Mitigação |
|-------|-----------|
| Models API indisponível / modelo local | `try/except` → `None` → fallback 4096. Já previsto. |
| Estimativa alta demais infla reserva | `min(estimativa, teto)` limita; reserva não é cobrada como output real (só tokens gerados contam). |
| Gemini shim OpenAI não traz `outputTokenLimit` | Consulta nativa `v1beta` opcional; se falhar, fallback. |
| Anthropic com thinking | `max(max_tokens, budget + 4096)` preservado — espaço de thinking garantido. |

---

## 8. Conclusão

O alto índice de falhas **não decorre da capacidade do LLM** — comprovado pelo
fato de `gemini-2.5-pro` não superar `gemini-3.5-flash`. As causas são quatro
defeitos na lógica interna do coder, todos tratáveis com estratégias
estabelecidas:

1. **Truncamento silencioso** por `max_tokens=4096` sem checar `finish_reason`.
2. **Output vazio** em chunks-lista sem retry específico.
3. **Assinatura de dedup** referenciando campo inexistente (`text` vs `trecho`),
   colapsando ITEMs legítimos via heurística de overlap-60%.
4. **Fix-loop** desperdiçado sobre conteúdo ausente.

O truncamento (causa 1) tem **resposta dupla e complementar**: P1-bis dimensiona
`max_tokens` dinamicamente (teto do modelo via API — `max_tokens` na Anthropic,
`outputTokenLimit` no Gemini — limitado por uma estimativa por chunk), com o
valor hardcoded `4096` rebaixado a **fallback** para modelos locais que não
publicam seus limites; e P1 detecta `finish_reason` como rede de segurança
quando a estimativa erra. A inversão de precedência (env > dinâmico > fallback)
mantém compatibilidade total.

As correções P1 + P1-bis + P3 entregam a maior parte do ganho com risco mínimo e
devem ser priorizadas. O campo CHAIN renomeado (`relacao_aplicada`) funcionou
corretamente em todos os testes — não tem relação com as falhas aqui descritas.
