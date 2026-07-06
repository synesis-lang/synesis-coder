# Estudo: Backend/API por Fase (crítico ≠ gerador em provedores distintos)

> **Natureza deste documento:** estudo de viabilidade. Não altera código.
> Avalia o que seria necessário para permitir que cada fase do pipeline
> (extração, critique, normalize, refine) use não só um **modelo** distinto,
> mas um **backend/URL/chave** distintos — ex.: gerador no OpenRouter e crítico
> na Anthropic nativa, para independência epistêmica real.

---

## 1. Motivação

O modo `refine` e o pipeline ACT já suportam **modelo por fase**
(`SYNESIS_CODER_CRITIQUE_MODEL`, `SYNESIS_CODER_REFINE_MODEL`, etc.). A
recomendação metodológica de "crítico de família diferente do gerador" (para
evitar viés de auto-validação) só é **plenamente** realizável se o crítico puder
rodar num **provedor** diferente — p.ex.:

- Gerador: `deepseek/deepseek-v4-pro` via **OpenRouter** (openai-compat)
- Crítico: `claude-sonnet-4-6` via **Anthropic nativa**

Hoje isso é **impossível**, porque backend/URL/chave são globais e únicos.

---

## 2. O que o código faz hoje (fonte da verdade)

### 2.1 O que varia por fase: apenas o MODELO

`_validate_phase_env(phase)` ([cli.py:51](../synesis_coder/cli.py#L51)) resolve
`SYNESIS_CODER_<FASE>_MODEL` com fallback para `SYNESIS_CODER_MODEL`. É o
**único** eixo de variação por fase.

### 2.2 O que é GLOBAL e único: backend, URL, chave

`LLMClient.__init__` ([llm_client.py:206](../synesis_coder/llm_client.py#L206)):

```python
self.backend = (backend or _get_backend()).lower()          # aceita param backend
if self.backend == "openai":
    self._client = openai.OpenAI(
        base_url=f"{_get_api_url()}/v1",   # ← LÊ DO AMBIENTE, não é parâmetro
        api_key=_get_api_key(),            # ← LÊ DO AMBIENTE, não é parâmetro
    )
else:
    self._client = anthropic.Anthropic(api_key=_get_anthropic_api_key())  # ← idem
```

Achado central: `__init__` **já aceita `backend=`** como argumento, mas
**`api_url` e `api_key` NÃO são parâmetros** — vêm de `_get_api_url()` /
`_get_api_key()` / `_get_anthropic_api_key()`, todos lendo `os.environ`
diretamente. Logo, dois `LLMClient` no mesmo processo **compartilham
obrigatoriamente a mesma URL e chave**, mesmo que se passe `backend=` diferente.

### 2.3 Como cada modo instancia (10 call-sites)

Todos passam apenas `model=` (e alguns `recorder=`):

| Modo | Call-site |
|---|---|
| item / suggest / ontology / normalize / critique / finetune | `LLMClient(model=model)` |
| abstract / document | `LLMClient(model=model, recorder=recorder)` |
| **refine** | `LLMClient(model=critique_model)` **e** `LLMClient(model=refine_model)` |

O `refine` é o único com dois clients — e é exatamente onde "provedor por fase"
mais importa (crítico vs gerador).

### 2.4 Conclusão do diagnóstico

Adicionar `SYNESIS_CODER_CRITIQUE_API_URL` **apenas no `.env`** seria **config
morta**: nenhuma função a lê. Suportar API por fase **exige mudança de código**
em `llm_client.py` e nos modos que se beneficiam (no mínimo `refine`).

---

## 3. Desenho proposto

### 3.1 Princípio: `LLMClient` recebe uma conexão explícita

Tornar `api_url` e `api_key` **parâmetros** de `__init__`, com fallback para o
ambiente (preservando 100% o comportamento atual quando não passados):

```python
def __init__(
    self,
    model: Optional[str] = None,
    backend: Optional[str] = None,
    api_url: Optional[str] = None,    # NOVO — fallback _get_api_url()
    api_key: Optional[str] = None,    # NOVO — fallback _get_api_key()/_get_anthropic_api_key()
    ...
):
```

- Quando `api_url`/`api_key` são `None` → comportamento idêntico ao atual
  (lê do ambiente). **Retrocompatível por construção.**
- Quando fornecidos → o client usa aquela conexão específica.

**Risco:** baixo. É aditivo na assinatura; os 10 call-sites existentes seguem
funcionando sem mudança (parâmetros novos são opcionais).

### 3.2 Abstração de "perfil de fase" (PhaseProfile)

Encapsular a resolução de `(backend, api_url, api_key, model)` por fase num
único ponto — evita espalhar `os.environ.get` pelos modos:

```python
@dataclass(frozen=True)
class LLMProfile:
    backend: str
    api_url: str | None
    api_key: str | None
    model: str | None

def resolve_profile(phase: str | None) -> LLMProfile:
    """Resolve o perfil de conexão+modelo de uma fase.

    Para cada eixo, precedência: var da fase → var global → default.
      backend:  SYNESIS_CODER_<FASE>_BACKEND  → SYNESIS_CODER_BACKEND
      api_url:  SYNESIS_CODER_<FASE>_API_URL  → SYNESIS_CODER_API_URL
      api_key:  SYNESIS_CODER_<FASE>_API_KEY  → SYNESIS_CODER_API_KEY / ANTHROPIC_API_KEY
      model:    SYNESIS_CODER_<FASE>_MODEL    → SYNESIS_CODER_MODEL
    """
```

O `LLMClient` ganharia um construtor de conveniência:
`LLMClient.from_profile(profile, recorder=...)`.

### 3.3 Convenção de env vars (retrocompatível)

Nova família **opt-in**, com fallback para as globais existentes:

```
SYNESIS_CODER_CRITIQUE_BACKEND=anthropic
SYNESIS_CODER_CRITIQUE_API_KEY=sk-ant-...
SYNESIS_CODER_CRITIQUE_MODEL=claude-sonnet-4-6
# (CRITIQUE_API_URL não necessário p/ backend anthropic)
```

Se nenhuma var `<FASE>_*` for definida → a fase herda a conexão global (seção 1
do `.env`) — **exatamente o comportamento de hoje**. Ninguém que não usar o
recurso é afetado.

---

## 4. Escopo da implementação

| Item | Arquivo | Linhas aprox. | Risco |
|---|---|---|---|
| (a) `api_url`/`api_key` como params + fallback | `llm_client.py::__init__` | ~15-25 | Médio (núcleo compartilhado) |
| (b) `LLMProfile` + `resolve_profile` + `from_profile` | `llm_client.py` (ou novo `profiles.py`) | ~60-90 | Baixo (aditivo) |
| (c) `refine` usa perfis distintos p/ crítico e gerador | `modes/refine_mode.py` | ~15-25 | Baixo |
| (d) CLI: flags `--critique-api-url`/`--critique-api-key`? | `cli.py` | ~20-40 | Baixo |
| (e) `.env` / `.env.example`: documentar família `<FASE>_*` | config | ~20 | Baixo |
| (f) Refatorar `_validate_phase_env` p/ usar `resolve_profile` | `cli.py` | ~10-20 | Médio (usado por critique/normalize/refine) |
| Testes | `tests/` | ~120-180 | Médio |
| **Total** | — | **~260-400** | **Médio** |

### 4.1 Ponto de atenção: impacto de `_validate_phase_env`

`_validate_phase_env` é chamado por `critique`, `normalize` e `refine` (2×).
Refatorá-lo (item f) tem blast-radius nesses três comandos — exige
`gitnexus_impact` antes de editar e a suíte desses modos verde depois. Se o
risco for indesejado, alternativa: **não** tocar `_validate_phase_env`;
introduzir `resolve_profile` em paralelo e migrar incrementalmente.

### 4.2 Rate limiting por conexão

Hoje o rate limit (deques RPM/TPM) é **por instância** de `LLMClient`. Com
clients em provedores distintos, cada um já tem seu próprio contador — o que
está **correto** (Anthropic tem cota; OpenRouter não). Nenhuma mudança
necessária aqui; só confirmar que o crítico-anthropic ativa `_rate_limit_enabled`
e o gerador-openai não, o que já ocorre por `self.backend`.

---

## 5. Riscos e mitigações

| Risco | Severidade | Mitigação |
|---|---|---|
| Quebrar os 10 call-sites atuais | Alta | Parâmetros novos são **opcionais** com fallback ao ambiente; comportamento default idêntico. Suíte completa como portão. |
| `ANTHROPIC_API_KEY` vs `SYNESIS_CODER_<FASE>_API_KEY` ambíguo p/ backend anthropic | Média | `resolve_profile` define precedência explícita e documentada; teste dedicado. |
| Config morta se `.env` mudar antes do código | Média | **Este estudo** — não escrever env vars que o código não lê até (a)+(b) existirem. |
| Complexidade de UX no `.env` (mais vars) | Baixa | Família `<FASE>_*` é opt-in; `.env` padrão continua com uma só conexão. |
| `_validate_phase_env` blast-radius | Média | `gitnexus_impact` antes; migração incremental sem tocar a função se necessário. |

---

## 6. Alternativa mínima (se o objetivo for só o `refine`)

Se o caso de uso real é **apenas** "crítico e gerador em provedores distintos no
`refine`", há um caminho de escopo bem menor (~80-120 linhas):

1. Só o item (a): `api_url`/`api_key` como params de `LLMClient`.
2. `refine_mode` lê `SYNESIS_CODER_CRITIQUE_{BACKEND,API_URL,API_KEY}` e
   `SYNESIS_CODER_REFINE_{...}` e instancia os dois clients com conexões
   explícitas.
3. Não refatorar `_validate_phase_env`; não tocar os outros 8 modos.

Entrega o valor metodológico central (independência epistêmica real) com
risco mínimo. Os demais modos ganham API-por-fase depois, se desejado.

---

## 7. Recomendação

1. **Confirmar o caso de uso**: é só o `refine` (crítico ≠ gerador em provedores
   distintos) ou todas as fases ACT? Isso decide entre o escopo mínimo (§6) e o
   completo (§4).
2. **Independente do escopo**, o item (a) — `api_url`/`api_key` como parâmetros
   opcionais de `LLMClient` — é a fundação e é retrocompatível. É o primeiro
   passo em qualquer caminho.
3. **Não** adicionar vars de API por fase ao `.env` **antes** de (a)+(b), para
   não criar config morta.
4. Sobre a **reorganização do `.env` que motivou este estudo**: ela é
   **independente** e pode ser feita já, no modelo atual (uma conexão global +
   modelos por fase), consolidando na seção "ATIVO AGORA" exatamente as vars que
   o código lê hoje. Quando/se a API-por-fase for implementada, o `.env` ganha a
   família `<FASE>_*` opcional.

---

## 8. Plano de Implementação (escopo travado: segunda conexão para critique + refine)

> **Decisão do usuário:** a segunda API será usada **apenas** em `critique` e
> `refine` (o crítico). Extração (item/abstract/document), normalize, ontology,
> suggest e o **gerador** do refine seguem na conexão primária.
>
> **Convenção:** `SYNESIS_CODER_CRITIQUE_{BACKEND,API_URL,API_KEY,MODEL}` — uma
> "conexão de crítica" coerente (a fase critique e o crítico do refine são a
> mesma coisa: avaliação). `_MODEL` já existe hoje e é preservado.
>
> **Fallback:** se qualquer var `CRITIQUE_*` de conexão não for definida, aquele
> eixo herda a conexão primária (seção "ATIVO AGORA"). Sem nenhuma var
> `CRITIQUE_*` de conexão → comportamento **idêntico ao de hoje**.

### 8.1 Peça (a) — `api_url`/`api_key` como parâmetros de `LLMClient`

Fundação retrocompatível. Em `LLMClient.__init__`
([llm_client.py:206](../synesis_coder/llm_client.py#L206)):

```python
def __init__(self, model=None, backend=None,
             api_url=None, api_key=None,     # NOVOS — opcionais
             max_rpm=None, ...):
    ...
    self.backend = (backend or _get_backend()).lower()
    if self.backend == "openai":
        self._client = openai.OpenAI(
            base_url=f"{api_url or _get_api_url()}/v1",
            api_key=api_key or _get_api_key(),
        )
    else:
        self._client = anthropic.Anthropic(
            api_key=api_key or _get_anthropic_api_key(),
        )
```

- `None` → fallback ao ambiente = comportamento atual **bit-a-bit**.
- Os 10 call-sites existentes não mudam (params novos são opcionais).

**Custo:** ~10-15 linhas. **Risco médio** (núcleo compartilhado) — mitigado por
suíte completa como portão e por os defaults serem inalterados.

### 8.2 Peça (b) — resolvedor da conexão de crítica

Helper único (em `llm_client.py`, junto dos outros `_get_*`), sem espalhar
`os.environ` pelos modos:

```python
def _get_critique_connection() -> dict:
    """Resolve backend/api_url/api_key da conexão de CRÍTICA.

    Cada eixo: var CRITIQUE_* → var global → default.
    Retorna kwargs prontos para LLMClient(**conn, model=...).
    """
    return {
        "backend": os.environ.get("SYNESIS_CODER_CRITIQUE_BACKEND")
                   or _get_backend(),
        "api_url": os.environ.get("SYNESIS_CODER_CRITIQUE_API_URL")
                   or os.environ.get("SYNESIS_CODER_API_URL"),   # None→_get_api_url no __init__
        "api_key": os.environ.get("SYNESIS_CODER_CRITIQUE_API_KEY")
                   or _resolve_primary_api_key_or_none(),
    }
```

Detalhe da chave: para backend anthropic a chave primária é `ANTHROPIC_API_KEY`;
para openai é `SYNESIS_CODER_API_KEY`. O helper devolve `None` quando nenhuma
`CRITIQUE_*` foi definida, deixando o `__init__` cair no `_get_*` correto do
backend **resolvido para a crítica** (que pode diferir do primário). Um teste
dedicado cobre a matriz (crítica anthropic + primário openai e vice-versa).

**Custo:** ~25-40 linhas. **Risco baixo** (aditivo).

### 8.3 Peça (c) — `critique_mode` usa a conexão de crítica

Uma linha muda em `_process_critique_async`
([critique_mode.py:456](../synesis_coder/modes/critique_mode.py#L456)):

```python
# antes:  llm_client = LLMClient(model=model)
conn = _get_critique_connection()
llm_client = LLMClient(model=model, **conn)
```

`model` continua vindo de `--model` / `SYNESIS_CODER_CRITIQUE_MODEL` (inalterado).
**Custo:** ~3-5 linhas. **Risco baixo.**

### 8.4 Peça (d) — `refine_mode`: crítico na 2ª conexão, gerador na 1ª

Em `_process_refine_async`
([refine_mode.py:441](../synesis_coder/modes/refine_mode.py#L441)):

```python
# crítico → conexão de crítica (2ª API)
critique_client = LLMClient(model=critique_model, **_get_critique_connection())
# gerador → conexão primária (inalterado)
refine_client = LLMClient(model=refine_model)
```

Isso concretiza a independência epistêmica real: o crítico pode rodar em
provedor/família distinta do gerador. **Custo:** ~3-5 linhas. **Risco baixo.**

### 8.5 Peça (e) — CLI (opcional, override por flag)

Flags de conexão de crítica são **opcionais** — o caso comum é configurar via
`.env`. Se desejado, adicionar a `critique` e `refine`:

```
--critique-backend / --critique-api-url / --critique-api-key
```

que sobrescrevem as env vars. Se o usuário não quiser essa superfície de CLI
agora, **omitir** — o `.env` cobre o caso de uso. **Custo:** 0 ou ~20-30 linhas.
**Recomendação:** omitir na v1 (env é suficiente); adicionar depois se pedido.

### 8.6 Peça (f) — `.env` / `.env.example`

Na seção "ATIVO AGORA" do `.env`, um subbloco **opt-in** logo após a conexão
primária:

```bash
# ── 2ª conexão: CRÍTICA (fases critique e refine-crítico) ────────────
#  Opcional. Se omitida, a crítica herda a conexão primária acima.
#  Ex.: gerador no OpenRouter (primária) + crítico na Anthropic nativa:
# SYNESIS_CODER_CRITIQUE_BACKEND=anthropic
# SYNESIS_CODER_CRITIQUE_API_KEY=sk-ant-...
# SYNESIS_CODER_CRITIQUE_MODEL=claude-sonnet-4-6
```

**Custo:** ~10-15 linhas de doc. **Risco baixo.**

### 8.7 `_validate_phase_env` — NÃO tocar

Decisão de contenção de risco: `_validate_phase_env`
([cli.py:51](../synesis_coder/cli.py#L51)) resolve **modelo** e valida chave; ele
continua como está. A conexão de crítica é resolvida por `_get_critique_connection`
no momento da instanciação do client, em paralelo. Isso evita blast-radius nos
três comandos que usam `_validate_phase_env` (critique/normalize/refine).

> ⚠ Antes de editar `LLMClient.__init__` (peça a): rodar
> `gitnexus_impact({target:"LLMClient", direction:"upstream"})` e reportar o
> blast-radius. É o símbolo mais compartilhado do projeto — os params novos são
> opcionais, mas a validação de impacto é obrigatória por CLAUDE.md.

### 8.8 Testes

| Suíte | Casos |
|---|---|
| `test_llm_client_connection` | `api_url`/`api_key` params sobrescrevem env; `None` cai no fallback; matriz backend crítica≠primário |
| `test_get_critique_connection` | precedência CRITIQUE_* → global → default; herança quando ausente |
| `test_critique_uses_critique_conn` | `critique_mode` instancia client com a conexão de crítica (mock de `LLMClient`) |
| `test_refine_critic_distinct_conn` | crítico usa 2ª conexão, gerador usa primária |
| regressão | suíte existente de critique/refine/item **inalterada** passa (retrocompat) |

**Custo:** ~100-150 linhas.

### 8.9 Resumo do escopo

| Peça | Arquivo | Linhas | Risco |
|---|---|---|---|
| (a) params de conexão em `LLMClient` | `llm_client.py` | ~10-15 | Médio |
| (b) `_get_critique_connection` | `llm_client.py` | ~25-40 | Baixo |
| (c) critique usa a conexão | `modes/critique_mode.py` | ~3-5 | Baixo |
| (d) refine: crítico na 2ª conexão | `modes/refine_mode.py` | ~3-5 | Baixo |
| (e) CLI (opcional — omitir v1) | `cli.py` | 0-30 | Baixo |
| (f) `.env` / `.env.example` | config | ~15 | Baixo |
| Testes | `tests/` | ~100-150 | Médio |
| **Total** | — | **~160-260** | **Baixo-Médio** |

Escopo enxuto e majoritariamente aditivo: nenhuma fase além de critique/refine é
tocada, `_validate_phase_env` fica intacto, e a ausência de config `CRITIQUE_*`
de conexão preserva o comportamento atual bit-a-bit.

---

*Estudo e plano de implementação gerados a pedido. Nenhum código foi alterado.*
