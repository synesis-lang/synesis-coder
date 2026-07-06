# Estudo: Re-extração com Feedback em Loop Automático

> **Natureza deste documento:** estudo de viabilidade. Não altera código.
> Avalia uma **nova opção** para o synesis-coder — sem cancelar as estratégias já
> discutidas (critique como LLM-as-Judge, enum constraints, reordenação de campos,
> field-level prompting). É complementar ao pipeline ACT existente.

---

## 1. O que é a "re-extração com feedback"

### 1.1 Distinção em relação ao pipeline atual

O pipeline ACT atual é **linear e não-recursivo**:

```
extrator → critique → (revisão humana) → incorporate → .syn final
  (LLM)     (LLM)        (humano)          (determinístico)
```

No `incorporate` atual, a correção é **mecânica**: ele pega a sugestão de campo
que o *próprio critique* escreveu (`# $chain: ...`) e a aplica via substituição
textual, validando com `synesis.load()`. O extrator nunca é reinvocado. Quem
"corrige" é o crítico, não o gerador.

A **re-extração com feedback** introduz recursão informada:

```
extrator → critique → [score >= θ] → RE-EXTRATOR(texto + feedback) → critique → ...
  (LLM)     (LLM)                        (LLM, NOVA chamada)          (LLM)
                                              ↑
                          o gerador raciocina de novo, ciente do erro apontado
```

A diferença epistêmica é central: em vez de o crítico ditar o valor corrigido,
o **gerador** recebe o diagnóstico ("o campo `code` violou granularidade — veja
PAR CRÍTICO X") e produz uma nova anotação raciocinando sobre o texto-fonte de
novo. Este é o padrão **Self-Refine** (Madaan et al., 2023) e **Reflexion**
(Shinn et al., 2023), com ganhos documentados em tarefas de raciocínio.

### 1.2 Por que isso supera o `incorporate` mecânico

O `incorporate` atual tem um limite estrutural: o crítico é bom em **detectar**
problemas (avaliação é mais fácil que geração), mas a *sugestão* de correção que
ele escreve é de qualidade incerta — ele a produz sem o foco total da geração.
Quando a sugestão do crítico está errada, o `incorporate` a aplica mesmo assim
(desde que passe `synesis.load()`, que só valida estrutura, não fidelidade
semântica).

A re-extração devolve a geração a quem é especializado em gerar, usando o crítico
apenas para o que ele faz melhor: apontar onde está o erro.

---

## 2. Custos de Implementação no Código Atual

### 2.1 O que já existe e pode ser reaproveitado

| Componente | Arquivo | Reutilizável? |
|---|---|---|
| Geração de ITEM (caminho JSON + fallback) | `modes/item_mode.py::_generate_item_syn` | ✅ Direto |
| Validação estrutural | `validator.py::validate_and_fix` + `_has_structural_errors` | ✅ Direto |
| Critique por ITEM (assíncrono, rate-limited) | `modes/critique_mode.py::_critique_single_item` | ✅ Direto |
| Parse de score/reason/sugestões | `critique_mode.py::_parse_critique_response` | ✅ Direto |
| Extração de blocos ITEM com bibref | `critique_mode.py::_extract_item_blocks_with_bibrefs` | ✅ Direto |
| Obtenção do texto-fonte (abstract/.bib ou campo text) | `critique_mode.py::_get_source_text` | ✅ Direto |
| Substituição/validação de campo | `incorporate_mode.py::_apply_revision_tags` | ⚠️ Parcial |

**Conclusão importante:** ~70% das peças já existem. A re-extração não exige
reconstruir a infraestrutura — exige **orquestrá-la em loop**.

### 2.2 O que precisa ser construído (peças novas)

**(a) Um prompt builder de re-extração com feedback.**
O `prompt_builder.py` tem `build_item_prompt` / `build_item_values_prompt`, mas
nenhum aceita um bloco de feedback. Seria necessário um
`build_item_refinement_prompt(ctx, bibref, text, prev_item, critique_tags)` que
injeta:
- o texto-fonte original,
- a anotação anterior (o que o modelo gerou),
- o diagnóstico do crítico (`reason`, `reason_detail`, sugestões de campo),
- instrução explícita: "regenere corrigindo APENAS os campos apontados".

Custo estimado: ~80-120 linhas, espelhando os builders existentes. **Risco baixo**
(não toca nos builders atuais; é função aditiva).

**(b) Um orquestrador de loop.**
Um novo modo (`modes/refine_mode.py` ou integração em `critique_mode`) que executa:

```
para cada ITEM:
    item_atual = item_original
    para iteração em 1..MAX_ITER:
        tags = critique(item_atual, source_text)          # chamada LLM #1
        se score(tags) < θ:  break                         # convergiu
        item_novo = re_extrair(source, feedback=tags)      # chamada LLM #2
        se not validate(item_novo): break                  # rejeita, mantém anterior
        se item_novo == item_atual: break                  # sem progresso (anti-loop)
        item_atual = item_novo
```

Custo estimado: ~150-200 linhas. **Risco médio** — é onde mora a lógica de
convergência, anti-loop e contabilidade de tokens.

**(c) Nova subcomando CLI.**
Seguindo o padrão de `cli.py` (cada modo é ~30 linhas de boilerplate Click +
epilog). Acrescentar `refine` ou estender `critique` com flag `--refine`.
**Risco baixo** — padrão repetitivo já consolidado no arquivo.

**(d) Critérios de parada e configuração via env.**
Variáveis novas: `SYNESIS_CODER_REFINE_MAX_ITER` (default sugerido: 2),
`SYNESIS_CODER_REFINE_THRESHOLD` (reusar suspicion threshold), modelo de
re-extração. **Risco baixo.**

### 2.3 Estimativa global de esforço

| Item | Linhas aprox. | Risco | Toca código existente? |
|---|---|---|---|
| (a) prompt builder de refinamento | 80-120 | Baixo | Não (aditivo) |
| (b) orquestrador de loop | 150-200 | Médio | Não (novo módulo) |
| (c) subcomando CLI | 30-40 | Baixo | Sim (adiciona comando) |
| (d) config/env | 10-20 | Baixo | Não |
| Testes | 150-250 | Médio | — |
| **Total** | **~420-630** | **Médio** | **Mínimo** |

O ponto forte: a re-extração é **majoritariamente aditiva**. O extrator, o
critique e o validador permanecem intocados — são chamados como bibliotecas. Isso
mantém o `incorporate` determinístico atual como caminho alternativo intacto.

---

## 3. Segurança na Implementação

A "segurança" aqui é a garantia de que o loop **não degrade** anotações nem
produza saídas inválidas/instáveis. Há cinco vetores de risco.

### 3.1 Regressão de qualidade (o loop pioria a anotação)

**Risco:** A re-extração pode substituir uma anotação boa por uma pior — o crítico
falsamente sinaliza, ou a re-extração introduz erro novo.

**Mitigações disponíveis:**
- **Cláusula de não-regressão:** só aceitar `item_novo` se o critique da nova
  versão tiver score *menor* que o da anterior. Caso contrário, manter a anterior.
  Isso transforma o loop em monotônico-decrescente em suspeição.
- **Validação estrutural obrigatória** via `validate_and_fix` antes de aceitar
  cada nova versão (a infraestrutura já existe e já é usada no `incorporate`).
- **Preservação do original:** ao final, se nenhuma iteração melhorou, emitir a
  anotação original — nunca uma intermediária pior.

### 3.2 Loops infinitos / não-convergência

**Risco:** O crítico oscila (score sobe e desce) ou nunca cai abaixo do threshold.

**Mitigações:**
- **`MAX_ITER` rígido** (sugerido: 2, no máximo 3). A literatura de Self-Refine
  mostra retornos marginais decrescentes após 2-3 iterações.
- **Detecção de ponto-fixo:** se `item_novo == item_atual` (texto idêntico),
  parar — o modelo não tem mais o que corrigir.
- **Detecção de oscilação:** se a nova versão repete uma versão de 2 iterações
  atrás, parar.

### 3.3 Viés de auto-validação

**Risco:** Se o mesmo modelo gera e critica, ele tende a endossar o próprio output
(o segundo critique aprova porque "reconhece" seu estilo).

**Mitigação:** Reusar a separação que o critique já oferece via
`SYNESIS_CODER_CRITIQUE_MODEL` — crítico ≠ extrator. Isso já é arquitetura
existente; o loop apenas se beneficia dela. **Recomendação forte:** crítico de
modelo distinto (idealmente de família diferente) para independência epistêmica.

### 3.4 Estabilidade do ITEM no contexto do bloco

**Risco:** A re-extração regenera o ITEM **isolado**, mas ele vive num arquivo
`.syn` com outros ITEMs e um SOURCE. Campos como `chain` referenciam nós que
podem existir noutros ITEMs; regenerar isolado pode quebrar referências cruzadas.

**Mitigações:**
- A validação atual via `synesis.load()` ignora `OrphanItem` (ITEM sem SOURCE)
  — ver `incorporate_mode.py::_validate_item_block`. Isso já lida com validação
  isolada, mas **não** detecta inconsistências de chain cross-item.
- Para templates com forte acoplamento entre ITEMs (chains que cruzam blocos),
  a re-extração isolada é **mais arriscada**. Recomenda-se restringir a re-extração
  a substituições de campo intra-ITEM, não a regeneração total do bloco, quando o
  template usa chains cross-item.

### 3.5 Determinismo e reprodutibilidade

**Risco:** O `incorporate` atual é determinístico (sem LLM) — é auditável e
reproduzível. A re-extração introduz LLM no caminho de "correção", quebrando essa
propriedade. Em contexto de **pesquisa qualitativa acadêmica**, reprodutibilidade
é um valor metodológico, não apenas técnico.

**Mitigações:**
- `temperature=0.0` em todas as chamadas (já é o default no `item_mode` e no
  `critique`).
- Registrar no cabeçalho de métricas do `.syn` final: número de iterações por
  ITEM, scores em cada passo, modelo usado. Isso preserva **rastreabilidade** (o
  `_build_metrics_header` do `incorporate` já é um modelo de como documentar isso).
- **Manter o `incorporate` determinístico como caminho default.** A re-extração
  deve ser **opt-in explícito** (`--refine`), nunca o comportamento padrão. Assim
  o pesquisador escolhe conscientemente trocar reprodutibilidade por qualidade.

### 3.6 Resumo de segurança

| Risco | Severidade | Mitigação existe hoje? |
|---|---|---|
| Regressão de qualidade | Alta | Parcial (validação estrutural sim; não-regressão não) |
| Loop infinito | Média | Não (precisa MAX_ITER + ponto-fixo) |
| Viés de auto-validação | Média | Sim (`CRITIQUE_MODEL` separado) |
| Quebra de chain cross-item | Alta (templates acoplados) | Não |
| Perda de reprodutibilidade | Média (contexto acadêmico) | Parcial (temp=0; falta logging de iterações) |

---

## 4. Gastos com Tokens

### 4.1 Modelo de custo

Seja **N** o número de ITEMs e **F** a fração que ultrapassa o threshold de
suspeição (`metrics.suspicion_rate` — documentado como "< 0.30 indica boa
qualidade"). Para cada ITEM refinado, cada iteração custa:

- **1 chamada de critique** (input: GUIDELINES cacheadas + 1 ITEM + source; output: score+sugestões — curto)
- **1 chamada de re-extração** (input: GUIDELINES cacheadas + source + ITEM anterior + feedback; output: ITEM completo)

### 4.2 Comparação com o pipeline atual

| Pipeline | Chamadas LLM | Observação |
|---|---|---|
| Atual (extrair + critique + incorporate) | N + N + 0 = **2N** | incorporate é determinístico (0 LLM) |
| Re-extração, 1 iteração | N + N·F·(1 crit + 1 reextr) ≈ **N + 2·N·F** | só ITEMs suspeitos entram no loop |
| Re-extração, K iterações (pior caso) | N + 2·N·F·K | F decresce a cada passo se converge |

**Exemplo concreto** (N=100 ITEMs, F=0.30, K=2):
- Atual: 100 (extr) + 100 (crit) = **200 chamadas**.
- Re-extração: 100 (extr inicial) + ~2×100×0.30×... ≈ 100 + 60 + ~36 ≈ **~196-220 chamadas** na 1ª-2ª iteração (F cai entre iterações).

O acréscimo é **proporcional a F**, não a N. Se a qualidade da extração é boa
(F baixo, o que o próprio sistema espera), o custo adicional é modesto —
**~30-60% a mais de chamadas apenas sobre o subconjunto suspeito**, não sobre todo
o corpus.

### 4.3 O fator de economia decisivo: prompt caching

O `build_critique_prompt` e os builders de item já marcam o system prompt como
`cache: True`. Como as GUIDELINES (a maior parte dos tokens de input nos templates
grandes — `face85` 584 linhas, `lattes` 757 linhas, `social_acceptance` 482
linhas) são **idênticas** entre todas as chamadas do mesmo template, o cache da
Anthropic reduz drasticamente o custo de input nas chamadas repetidas do loop.

**Implicação:** o custo dominante da re-extração **não** é reprocessar GUIDELINES
(cacheadas), mas:
1. o **output** da re-extração (ITEM completo regenerado), e
2. os tokens **dinâmicos de input** (ITEM anterior + feedback), que são pequenos.

Isso torna o loop economicamente viável justamente para os templates grandes,
onde sem cache seria proibitivo.

### 4.4 Alavancas de controle de custo

| Alavanca | Efeito |
|---|---|
| `MAX_ITER=2` | Teto rígido no multiplicador K |
| Threshold de suspeição mais alto | Reduz F (menos ITEMs entram no loop) |
| Modelo de critique mais barato (Haiku/Sonnet) | Critique é avaliação, não geração — tolera modelo menor |
| Não-regressão como parada antecipada | Encurta loops que não melhoram |
| `thinking=False` no critique (já é o caso) | Critique sem thinking; re-extração pode usar thinking só se necessário |

### 4.5 Trade-off thinking vs. custo na re-extração

O critique já roda com `thinking=False` (`critique_mode.py:257`) — correto. Para a
**re-extração**, habilitar extended thinking melhora a correção de erros de
raciocínio (co-dependências do `lattes`, PARES CRÍTICOS do `face85`), mas aumenta
o custo de output. Recomendação: thinking na re-extração **apenas** para campos/
templates com co-dependência declarada, não como default.

---

## 5. Recomendação

### 5.1 Veredito

A re-extração com feedback é a estratégia de **maior potencial de ganho de
qualidade** entre as estudadas, porque é a única que ataca a causa-raiz: dá ao
*gerador* a chance de raciocinar de novo sobre o erro, em vez de aplicar
mecanicamente o palpite do crítico. É também a mais alinhada com a literatura
consagrada (Self-Refine, Reflexion).

O custo de implementação é **moderado e majoritariamente aditivo** (~420-630
linhas, ~70% da infraestrutura já existe), e o custo de tokens é **controlável**
(proporcional a F, amortizado por prompt caching).

### 5.2 Condições para implementação segura

1. **Opt-in explícito** (`--refine`), preservando o `incorporate` determinístico
   como default — protege a reprodutibilidade metodológica.
2. **Cláusula de não-regressão** obrigatória (só aceita versão com score menor).
3. **`MAX_ITER ≤ 2-3`** + detecção de ponto-fixo/oscilação.
4. **Crítico de modelo distinto** (`SYNESIS_CODER_CRITIQUE_MODEL`) para evitar
   viés de auto-validação.
5. **Logging completo de iterações** no cabeçalho de métricas para rastreabilidade.
6. **Cautela com chains cross-item:** preferir correção de campo intra-ITEM à
   regeneração total quando o template tem chains que cruzam blocos.

### 5.3 Posição relativa às outras opções

| Estratégia | Ganho de qualidade | Custo implementação | Custo tokens | Status |
|---|---|---|---|---|
| Critique como LLM-as-Judge | Médio | — (existe) | Baixo | ✅ Implementado |
| Incorporate mecânico | Baixo | — (existe) | Zero (sem LLM) | ✅ Implementado |
| Enum constraints no schema | Médio-Alto | Baixo | Zero | Proposto |
| Reordenação de campos (primacy) | Médio | Baixo | Zero | Proposto |
| Field-level prompting | Médio | Alto | Alto | Proposto |
| **Re-extração com feedback** | **Alto** | **Médio** | **Médio (controlável)** | **Este estudo** |

A re-extração **não cancela** as demais — ela se beneficia delas. Enum constraints
e reordenação de campos melhoram tanto a extração inicial quanto a re-extração,
reduzindo F (logo, reduzindo o custo do próprio loop). São complementares.

---

## 6. Plano de Implementação

> **Escopo:** este plano detalha a construção do modo `refine` como comando ACT
> opt-in, majoritariamente aditivo, reaproveitando a infraestrutura existente
> como biblioteca. Segue os padrões de codificação já consolidados no projeto:
> módulos em `modes/`, orquestração `asyncio` + `Semaphore`, `LLMClient`
> compartilhado, prompts puros em `prompt_builder.py`, cabeçalho `# $metrics.*`,
> boilerplate Click com epilog, e configuração via env `SYNESIS_CODER_*`.

### 6.0 Princípios de projeto (alinhamento com o código existente)

O plano respeita seis invariantes observadas no código atual:

1. **Modos são orquestradores finos.** Cada `process_*` é síncrono e delega a um
   `_process_*_async` (padrão de [critique_mode.py:321](../synesis_coder/modes/critique_mode.py#L321)).
   A lógica de rede vive no `LLMClient`; o modo apenas compõe chamadas.
2. **Prompts são funções puras** que retornam `[{"role","content","cache"}]`, com
   system `cache=True` e user `cache=False` — nunca lógica de I/O no builder.
3. **Reuso, não reconstrução.** Critique, validação e obtenção de source-text são
   invocados como bibliotecas. A re-extração **não duplica** `_critique_single_item`
   nem `_get_source_text`; ela os chama.
4. **Determinismo por default; LLM opt-in.** O `refine` é um novo subcomando —
   `incorporate` permanece o caminho determinístico intocado.
5. **Rastreabilidade via cabeçalho de métricas**, no molde de `_build_metrics_header`.
6. **Configuração via env com fallback**, no molde de `_validate_phase_env` e
   `SYNESIS_CODER_<PHASE>_MODEL`.

### 6.1 Arquitetura de módulos

```
synesis_coder/
├── modes/
│   └── refine_mode.py          ← NOVO — orquestrador do loop (peça (b))
├── prompt_builder.py           ← +build_item_refinement_prompt (peça (a), aditivo)
├── cli.py                      ← +subcomando `refine` (peça (c), aditivo)
└── (item_mode, critique_mode, validator — INTOCADOS, usados como lib)
```

Nenhuma função existente é modificada em assinatura. As únicas edições em
arquivos existentes são **adições** (nova função em `prompt_builder`, novo
comando em `cli`), o que mantém o blast-radius mínimo — a ser confirmado com
`gitnexus_impact` antes de cada edição (ver §6.8).

### 6.2 Peça (a) — `build_item_refinement_prompt` em `prompt_builder.py`

**Contrato.** Função pura, espelhando `build_item_values_prompt`
([prompt_builder.py:123](../synesis_coder/prompt_builder.py#L123)):

```python
def build_item_refinement_prompt(
    ctx: dict,
    bibref: str,
    source_text: str,
    prev_item_block: str,
    critique_tags: dict[str, str],
) -> List[dict]:
    """Monta o prompt de re-extração informada por feedback.

    Reusa _build_values_system_prompt(ctx, scope="item") como base (GUIDELINES,
    índices, bundles cacheados) e injeta na mensagem do usuário: (1) o texto-fonte,
    (2) a anotação anterior, (3) o diagnóstico do crítico, (4) a instrução de
    correção cirúrgica ("regenere corrigindo APENAS os campos apontados").
    """
```

**Decisões de design:**

- **Reutilizar o system prompt de valores** (`_build_values_system_prompt`), não
  criar um novo. Isso preserva o `cache=True` sobre as GUIDELINES — o vetor de
  economia decisivo (§4.3). O feedback vai **apenas** na mensagem do usuário
  (dinâmica, não cacheada), como já ocorre com bibref/texto.
- **Caminho JSON quando disponível.** Se `client.supports_json_schema()`, o
  refinamento também deve devolver valores (envelope `items`) via `call_json`,
  reaproveitando `assemble_items` — mantendo simetria com `_generate_item_syn`
  ([item_mode.py:79](../synesis_coder/modes/item_mode.py#L79)). Um prompt-builder
  irmão `build_item_refinement_values_prompt` cobre esse caminho; o de texto-livre
  cobre o backend Anthropic nativo.
- **Serialização do feedback.** Um helper privado `_format_critique_feedback(tags)`
  converte o dict de tags do critique em texto legível para o modelo:
  `reason`, `reason_detail`, e as sugestões de campo (`# $chain:`, etc.) como
  "pistas do revisor", explicitando que são *hipóteses* — o gerador decide.

**Esboço da mensagem do usuário:**

```
BIBREF: @{bibref}
<source>{source_text}</source>

PREVIOUS ANNOTATION (contains an issue flagged by the reviewer):
{prev_item_block}

REVIEWER DIAGNOSIS:
  reason: {reason}
  detail: {reason_detail}
  field hints (hypotheses — verify against the source, do not copy blindly):
    chain: {suggested_chain}
    ...

Re-extract this annotation from the SOURCE, correcting ONLY the flagged
field(s). Keep all correct fields unchanged. Return the JSON object of values.
```

**Custo:** ~90–130 linhas (2 builders + helper). **Risco baixo** — puramente
aditivo, sem tocar builders existentes. Coberto por testes de estrutura de
prompt (§6.7), no molde de `test_prompt_structure`.

### 6.3 Peça (b) — `refine_mode.py` (orquestrador do loop)

**Ponto de entrada público** (padrão `process_*` síncrono → `_async`):

```python
def process_refine(
    syn_path: Path,
    project_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    concurrent: int = 3,
    critique_model: Optional[str] = None,
    refine_model: Optional[str] = None,
    max_iter: int = 2,
    suspicion_threshold: float = 0.20,
    thinking_budget: int = 0,
    format: str = "plain",
    debug: bool = False,
) -> str:
    return asyncio.run(_process_refine_async(...))
```

**Núcleo do loop por ITEM** — função `_refine_single_item`, assíncrona,
protegida por `Semaphore` (mesmo padrão de `_critique_single_item`):

```python
async def _refine_single_item(
    item_block, bibref, ctx,
    critique_client, refine_client,   # DOIS clients: crítico ≠ gerador (§3.3)
    semaphore, suspicion_threshold, max_iter, thinking_budget,
) -> RefineResult:
    async with semaphore:
        source_text = _get_source_text(item_block, bibref, ctx)   # REUSO

        current = item_block
        best = item_block
        best_score = await _score(current, source_text, critique_client, ...)
        history = [_normalize(current)]         # anti-oscilação
        trace = [IterationRecord(0, best_score)]

        for it in range(1, max_iter + 1):
            if best_score < suspicion_threshold:
                break                            # convergiu (§3.2)

            tags = await _critique(current, source_text, critique_client, ...)  # REUSO
            candidate = await _re_extract(
                ctx, bibref, source_text, current, tags,
                refine_client, thinking_budget,
            )
            candidate, ok = validate_and_fix_async(candidate, ctx, refine_client)  # REUSO
            if not ok:
                break                            # rejeita output inválido (§3.1)

            norm = _normalize(candidate)
            if norm in history:
                break                            # ponto-fixo / oscilação (§3.2)
            history.append(norm)

            cand_score = await _score(candidate, source_text, critique_client, ...)
            if cand_score >= best_score:
                break                            # NÃO-REGRESSÃO: sem melhora → para (§3.1)

            best, best_score, current = candidate, cand_score, candidate
            trace.append(IterationRecord(it, cand_score))

        return RefineResult(bibref, best, best_score, trace)   # SEMPRE a melhor versão
```

**Decisões-chave que implementam as cláusulas de segurança do §3:**

| Mecanismo | Linha conceitual | Cláusula do §3 |
|---|---|---|
| `best` inicia = original; só troca se `cand_score < best_score` | não-regressão estrita | §3.1 |
| `for it in range(1, max_iter+1)` | teto rígido de iterações | §3.2 |
| `_normalize` + `history` (set de formas normalizadas) | ponto-fixo e oscilação | §3.2 |
| `critique_client` ≠ `refine_client` (modelos distintos) | anti auto-validação | §3.3 |
| `validate_and_fix_async` antes de aceitar | validação estrutural | §3.1 |
| `RefineResult.trace` (score por iteração) | logging de rastreabilidade | §3.5 |

- **`_normalize`**: remove whitespace/case-noise para detecção de ponto-fixo
  robusta (dois outputs semanticamente idênticos com espaçamento diferente não
  devem contar como "progresso"). Não altera o texto emitido — só a chave de
  comparação.
- **`_score`/`_critique`**: fatoração do critique em (a) obter tags e (b) extrair
  o score. Reusa `_parse_critique_response` e `build_critique_prompt`
  ([critique_mode.py:150](../synesis_coder/modes/critique_mode.py#L150)); evita
  duplicar a lógica de parse. Idealmente expõe um helper reutilizável em
  `critique_mode` (`_critique_tags(item, source, client, threshold=0)` retornando
  sempre as tags, sem filtro de threshold) — pequena refatoração aditiva que
  **ambos** os modos passam a usar, sem alterar comportamento do critique atual.
- **Chains cross-item (§3.4):** por default, o `refine` regenera o ITEM isolado.
  Para respeitar a cautela do §3.4, adicionar um guard: se o template declara
  `chain_relations` **e** o corpus usa chains que referenciam nós definidos
  noutros ITEMs, emitir aviso e (config `SYNESIS_CODER_REFINE_CROSS_ITEM=strict`)
  degradar para **substituição de campo intra-ITEM** via `_apply_revision_tags`
  ([incorporate_mode.py:180](../synesis_coder/modes/incorporate_mode.py#L180)) em
  vez de regeneração total. Detecção cross-item é heurística conservadora
  (default: permitir regeneração; strict: só campo). Documentar como limitação
  conhecida na primeira versão.

**Saída.** O `refine` emite um `.syn` final diretamente (não um `.synr`), pois
já resolve as revisões via LLM. O cabeçalho de métricas segue
`_build_metrics_header` estendido com a seção da fase refine:

```
# --- Fase R: Refine (re-extração com feedback, LLM) ---
# $metrics.refine.items_total: N
# $metrics.refine.items_entered_loop: <ITEMs com score inicial >= threshold>
# $metrics.refine.items_improved: <ITEMs cuja melhor versão != original>
# $metrics.refine.iterations_mean: <média de iterações executadas>
# $metrics.refine.score_reduction_mean: <média (score_inicial - score_final)>
# $metrics.refine.max_iter: <teto configurado>
# $metrics.refine.critique_model / refine_model: <modelos>
# --- por ITEM (rastreabilidade §3.5) ---
# $refine.@bibref.trace: 0.62 -> 0.31 -> 0.18   (score por iteração)
```

**Custo:** ~180–230 linhas. **Risco médio** — é a peça com lógica de convergência.
Mitigado por: (1) reuso máximo de primitivas testadas; (2) suíte de testes de
convergência dedicada (§6.7).

### 6.4 Peça (c) — subcomando CLI `refine`

Espelha o boilerplate de `critique` ([cli.py:609](../synesis_coder/cli.py#L609)),
com epilog `_EPILOG_REFINE` no mesmo estilo dos demais. Opções:

```
synesis-coder refine [SYN_FILE]
  --project PATH              (auto-detect se omitido)
  --output PATH              (default: <stem>.syn — CUIDADO: não sobrescreve entrada)
  --concurrent INT=3
  --max-iter INT             (default: env SYNESIS_CODER_REFINE_MAX_ITER ou 2)
  --threshold FLOAT          (default: env SYNESIS_CODER_SUSPICION_THRESHOLD ou 0.20)
  --critique-model TEXT      (override SYNESIS_CODER_CRITIQUE_MODEL)
  --refine-model TEXT        (override SYNESIS_CODER_REFINE_MODEL / SYNESIS_CODER_MODEL)
  --thinking-budget INT      (re-extração com extended thinking; default 0 — §4.5)
  --format [plain|verbose]
  --overwrite / --backup     (via safe_write_output, como incorporate)
  --debug
```

- Reusar `_validate_phase_env("critique")` e `_validate_phase_env("refine")` para
  a checagem de `ANTHROPIC_API_KEY` e resolução de modelo por fase.
- Adicionar `refine` ao grupo **"ACT Pipeline"** no `_build_main_help` e à lista
  de tokens coloridos em `_ex`. Descrição: `"[Phase R] Re-extract flagged ITEMs
  with critique feedback (opt-in, LLM). Emits final .syn."`
- **Guarda de segurança de I/O:** se `--output` resolver para o mesmo caminho do
  `SYN_FILE` de entrada, abortar com erro claro (evita corromper a fonte) a menos
  que `--overwrite` explícito + `--backup`.

**Custo:** ~35–45 linhas. **Risco baixo** — padrão repetitivo já consolidado.

### 6.5 Peça (d) — configuração via env

Adicionar ao `.env.example` (seção ACT), no molde existente:

```bash
# --- Fase R: Refine (re-extração com feedback) ---
# SYNESIS_CODER_REFINE_MODEL=claude-opus-4-6      # gerador da re-extração
# SYNESIS_CODER_REFINE_MAX_ITER=2                 # teto de iterações (§3.2)
# SYNESIS_CODER_REFINE_CROSS_ITEM=lenient         # lenient|strict (§3.4)
# (crítico reusa SYNESIS_CODER_CRITIQUE_MODEL; threshold reusa SUSPICION_THRESHOLD)
```

`_validate_phase_env` já resolve `SYNESIS_CODER_REFINE_MODEL` automaticamente
(basta chamar com `phase_name="refine"`). **Risco baixo**, ~10–15 linhas.

### 6.6 Estruturas de dados

Dataclasses locais em `refine_mode.py` (padrão `SynrDocument`/frozen dataclass):

```python
@dataclass(frozen=True)
class IterationRecord:
    iteration: int
    score: float

@dataclass
class RefineResult:
    bibref: str
    final_block: str
    final_score: float
    trace: list[IterationRecord]
    improved: bool          # final_block != original
```

`asyncio.gather` preserva ordem (como em critique), então
`results[i]` casa com `items_with_bibrefs[i]` para reconstruir o `.syn` na ordem
original via um `_reassemble_syn(content, results)` — espelhando o walk de blocos
de `_process_item_blocks` ([incorporate_mode.py:240](../synesis_coder/modes/incorporate_mode.py#L240)).

### 6.7 Estratégia de testes

Seguir a convenção de `tests/test_critique_mode.py` (classes por unidade,
`tmp_path`, LLM fake por monkeypatch). Cobertura mínima:

| Suíte | Casos | Objetivo |
|---|---|---|
| `test_build_refinement_prompt` | estrutura system/user, presença de source+prev+feedback, `cache` flags | contrato do prompt (peça a) |
| `test_refine_convergence` | score cai < threshold → para; `max_iter` respeitado; ponto-fixo (output idêntico) para; oscilação (A→B→A) para | lógica do loop (§3.2) |
| `test_refine_non_regression` | candidato com score ≥ atual é **rejeitado**; original preservado quando nada melhora | cláusula de não-regressão (§3.1) |
| `test_refine_invalid_rejected` | `validate_and_fix` falha → mantém melhor versão anterior | validação estrutural (§3.1) |
| `test_refine_distinct_models` | crítico e gerador usam clients/modelos distintos | anti auto-validação (§3.3) |
| `test_refine_metrics_header` | cabeçalho contém `metrics.refine.*` e `refine.@bibref.trace` | rastreabilidade (§3.5) |
| `test_refine_output_guard` | `--output == input` sem `--overwrite` → erro | segurança de I/O (§6.4) |
| `test_cli_refine` | subcomando registrado, help, epilog | integração CLI (peça c) |

O LLM é sempre **fake determinístico** nos testes (uma sequência scriptada de
respostas de critique e re-extração por ITEM) — nenhuma chamada de rede, no
molde dos testes existentes de critique/incorporate. **Custo:** ~180–260 linhas.

### 6.8 Ordem de execução e portões de qualidade

Sequência incremental, cada passo com verificação isolada:

1. **Peça (a)** — builders de prompt + `_format_critique_feedback`.
   Portão: `test_build_refinement_prompt` verde; `ruff`/`mypy` limpos.
2. **Refator aditivo mínimo** em `critique_mode`: extrair `_critique_tags` (helper
   sem filtro de threshold). Antes: `gitnexus_impact({target:"_critique_single_item"})`
   — confirmar blast-radius; a extração não muda o comportamento público de `critique`.
   Portão: suíte `test_critique_mode` **inalterada** passa.
3. **Peça (b)** — `refine_mode.py` (loop + dataclasses + reassembly + métricas).
   Portão: `test_refine_*` verdes.
4. **Peça (c)+(d)** — CLI + env + help. Portão: `test_cli_refine`, help renderiza.
5. **Verificação end-to-end** (skill `verify`): rodar `refine` num `.syn` de
   fixture pequeno com LLM fake, confirmar `.syn` de saída válido via `synesis.load()`
   e cabeçalho de métricas correto.
6. **Conformidade CLAUDE.md**: `gitnexus_detect_changes()` confirma que só
   `refine_mode.py` (novo), `prompt_builder.py`, `cli.py`, `.env.example` e testes
   mudaram; nenhum símbolo fora do escopo esperado foi tocado.

**Toolchain de qualidade** (conforme memória `synesis_quality_toolchain`):
`ruff` + `mypy` nos pins do projeto, contrato de CLI testado, `synesis>=`
constraint intacta. Não introduzir dependências novas — tudo é reuso de
`asyncio`, `click`, `tenacity`, `anthropic`/`openai` já presentes.

### 6.9 Resumo do plano

| Peça | Arquivo | Linhas | Toca existente | Risco |
|---|---|---|---|---|
| (a) builders de refinamento | `prompt_builder.py` | ~90–130 | Aditivo | Baixo |
| refator `_critique_tags` | `critique_mode.py` | ~15–25 | Extração sem mudança de comportamento | Baixo |
| (b) orquestrador do loop | `refine_mode.py` (novo) | ~180–230 | Não | Médio |
| (c) subcomando CLI | `cli.py` | ~35–45 | Aditivo | Baixo |
| (d) config/env | `.env.example` | ~10–15 | Aditivo | Baixo |
| Testes | `tests/test_refine_*.py` | ~180–260 | Não | Médio |
| **Total** | — | **~510–705** | **Mínimo** | **Médio** |

O plano confirma a estimativa do §2.3 e mantém a re-extração **opt-in,
majoritariamente aditiva e reproduzível-por-rastreabilidade**, cumprindo as seis
condições de implementação segura do §5.2 diretamente no desenho do loop e do
cabeçalho de métricas.

---

*Estudo e plano de implementação gerados a pedido. Nenhum código foi alterado.*
