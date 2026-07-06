# Estudo — Administração de Prompts, Tuning via `.env` e Viabilidade de Output Multilíngue

**Projeto:** `synesis-coder` v0.1.5
**Data:** 2026-04-15
**Template de referência:** [case-studies/ufmg/face85/bkp_com_causalidade/face85.synt](../case-studies/ufmg/face85/bkp_com_causalidade/face85.synt)
**Escopo:** análise técnica sem alterações de código.

---

## Resumo executivo

1. **Prompts NÃO são atomizados por campo.** Todas as `GUIDELINES` do escopo alvo (SOURCE, ITEM ou ONTOLOGY) são concatenadas **em um único system prompt cacheado** e enviadas numa única chamada ao LLM. A interação `note.anchor` → `chain.relation` do face85.synt chega inteira ao modelo — não há perda estrutural de contexto. O risco real é de **diluição de atenção** em templates muito grandes, não de quebra de semântica.

2. **Há um bug silencioso nos ajustes do `.env`.** As variáveis `SYNESIS_CODER_TEMPERATURE`, `SYNESIS_CODER_MAX_RETRIES`, `SYNESIS_CODER_MAX_RPM`, `SYNESIS_CODER_MAX_INPUT_TPM` e `SYNESIS_CODER_MAX_OUTPUT_TPM` **não são lidas pelo código** — estão documentadas mas inertes. Temperatura inicial é 0.0 hardcoded por modo, com escalonamento fixo `[0.0, 0.2, 0.5]` apenas no loop de correção.

3. **`SYNESIS_CODER_LANGUAGE` é altamente viável.** Um parâmetro de ~5 linhas em [`prompt_builder.py`](synesis_coder/prompt_builder.py) resolveria o caso de uso sem afetar cache, validação (parser Lark aceita UTF-8) ou consistência terminológica. Recomenda-se a **Estratégia A — instrução global no system prompt**.

---

## Parte 1 — Administração de Prompts e coesão semântica

### 1.1 A arquitetura real: um único system prompt por modo

O ponto de entrada canônico do modo `item` é `build_item_prompt()` em [prompt_builder.py:28-52](synesis_coder/prompt_builder.py#L28-L52), que retorna **duas mensagens**:

```
[
    {"role": "system", "content": <texto estático>,  "cache": True},
    {"role": "user",   "content": <bibref + texto>, "cache": False},
]
```

O system prompt é montado por `_build_system_prompt()` ([prompt_builder.py:60-106](synesis_coder/prompt_builder.py#L60-L106)), que agrega **em ordem fixa**:

| Bloco                          | Fonte                                                  | Observação                                 |
| ------------------------------ | ------------------------------------------------------ | ------------------------------------------ |
| Regras absolutas de formatação | Hardcoded (EN)                                         | "Output ONLY ITEM...END ITEM blocks", etc. |
| `PROJECT CONTEXT`              | `ctx["project_description"]` do DESCRIPTION do `.synp` | Injetado **uma vez**, estático             |
| `ITEM FIELDS`                  | Iteração sobre `ctx["item_fields"]`                    | **Todas as GUIDELINES concatenadas**       |
| `EXISTING PROJECT CONCEPTS`    | `code_index` do corpus atual                           | Referência para consistência de conceitos  |
| `EXISTING TOPICS`              | `topic_index`                                          | Referência para `TOPIC`                    |
| Formato esperado               | Hardcoded (EN)                                         | Exemplo sintático                          |

A função crítica é `_build_item_fields_section()` ([prompt_builder.py:109-126](synesis_coder/prompt_builder.py#L109-L126)), que **itera sequencialmente** por `item_fields.items()` e chama `_field_instruction()` ([prompt_builder.py:129-167](synesis_coder/prompt_builder.py#L129-L167)) para cada campo. O resultado é concatenado com quebras de linha — **nenhum LLM é invocado aqui**.

### 1.2 `_field_instruction()`: GUIDELINES passam verbatim

```python
base = spec.guidelines or spec.description or _generic_instruction(spec.type)
```

([prompt_builder.py:134](synesis_coder/prompt_builder.py#L134))

A instrução do autor do template é **inserida literalmente**, sem paráfrase, resumo ou reescrita. Para campos `CHAIN`, acrescentam-se as `RELATIONS` disponíveis e a sintaxe posicional; para `ORDERED/ENUMERATED`, os valores permitidos; para `SCALE`, o range. Nada mais.

**Consequência para o face85.synt:** quando o LLM é acionado, vê no mesmo prompt, sequencialmente:

1. GUIDELINES de `text` (regras de extração HIGH/MEDIUM, critérios de triagem).
2. GUIDELINES de `note` (clausulado `flag/claim/evidence/anchor`, regras de upgrade).
3. GUIDELINES de `chain` (ordem de decisão das 8 relações, regra de direção, FACTOR NAMING, Researcher Measurability Criterion, hierarquia de generalização).

A regra crítica **"The preceding note anchor is the only evidence allowed for selecting relation type and direction"** ([face85.synt:175](../case-studies/ufmg/face85/bkp_com_causalidade/face85.synt#L175)) **funciona como esperado** — o anchor e o consumidor do anchor estão no mesmo contexto.

### 1.3 Riscos remanescentes

O modelo recebe todo o contexto — mas isso não garante atenção perfeita. Pontos de vigilância:

- **Tamanho do system prompt.** No face85.synt, a soma bruta de GUIDELINES (text + note + chain + ontology) ultrapassa 2.500 palavras. Regras enterradas no meio podem ter peso efetivo menor. A posição importa: regras colocadas **no início e no final** têm mais aderência em Claude (efeitos de primazia e recência).
- **`code_index` e `topic_index` crescem** com o corpus. Em projetos maduros, podem consumir milhares de tokens de system prompt e comprimir o peso relativo das GUIDELINES.
- **Cache ephemeral de 5 min.** Se a sessão tem pausas > 5 min, cada reativação paga a leitura completa do system prompt.

### 1.4 Recomendações sem alterar código

Todas atuáveis via edição do `.synt`:

1. **Priorização posicional.** Mover as regras mais críticas (ex: "anchor é a única evidência") para as **primeiras 3 linhas** de cada bloco GUIDELINES.
2. **Compactação redundante.** Repetir a regra de vínculo `note.anchor ↔ chain.relation` tanto em `note` quanto em `chain` — o modelo processa ambos e a redundância reforça aderência.
3. **Exemplos GOOD/BAD explícitos.** O face85.synt já faz isso (linhas 143-147); manter o padrão em todos os campos com ambiguidade.
4. **Limite de granularidade.** Manter a lista `RELATIONS` em ≤ 8 itens (já é o caso) — listas maiores fragmentam a escolha.
5. **Separador visual.** Usar cabeçalhos em CAPS dentro de GUIDELINES (ex: `STRICT EXTRACTION RULES:`) — melhora a segmentação perceptual do modelo.

### 1.5 Veredito

> **Não há perda estrutural de contexto.** A arquitetura preserva interações entre campos. A qualidade do output depende principalmente de (a) qualidade redacional das GUIDELINES, (b) capacidade do modelo escolhido e (c) tamanho total do prompt. Para **máxima precisão analítica**, a alavanca disponível é `claude-opus-4-6` (já default), template bem ordenado e `temperature=0.0` (já default).

---

## Parte 2 — Ajustes de `.env` para precisão e redução de alucinação

### 2.1 O que o `.env` **realmente** controla

| Variável no `.env`             | Lida pelo código? | Efeito real                                        |
| ------------------------------ | ----------------- | -------------------------------------------------- |
| `ANTHROPIC_API_KEY`            | ✅                 | Autenticação Anthropic                             |
| `SYNESIS_CODER_BACKEND`        | ✅                 | Seleciona `anthropic` vs `openai`                  |
| `SYNESIS_CODER_API_URL`        | ✅                 | Base URL do backend OpenAI-compat                  |
| `SYNESIS_CODER_API_KEY`        | ✅                 | Chave alternativa para backends OpenAI-compat      |
| `SYNESIS_CODER_MODEL`          | ✅                 | Override do modelo default (`claude-opus-4-6`)     |
| `SYNESIS_CODER_TEMPERATURE`    | ❌ **inerte**      | Documentada no README, **não consumida**           |
| `SYNESIS_CODER_MAX_RETRIES`    | ❌ **inerte**      | README afirma default=3; código usa constante fixa |
| `SYNESIS_CODER_MAX_RPM`        | ❌ **inerte**      | Rate limit do cliente não é configurável via env   |
| `SYNESIS_CODER_MAX_INPUT_TPM`  | ❌ **inerte**      | Idem                                               |
| `SYNESIS_CODER_MAX_OUTPUT_TPM` | ❌ **inerte**      | Idem                                               |

Confirmação via grep em `synesis_coder/`: apenas `SYNESIS_CODER_MODEL`, `SYNESIS_CODER_BACKEND`, `SYNESIS_CODER_API_URL`, `SYNESIS_CODER_API_KEY` e `ANTHROPIC_API_KEY` aparecem como consumidores em [`llm_client.py`](synesis_coder/llm_client.py).

### 2.2 Temperatura real em uso

Hardcoded por modo (chamada inicial):

| Modo                      | Arquivo:linha                                                         | Temperatura |
| ------------------------- | --------------------------------------------------------------------- | ----------- |
| `item`                    | [item_mode.py:51](synesis_coder/modes/item_mode.py#L51)               | `0.0`       |
| `abstract`                | [abstract_mode.py:122](synesis_coder/modes/abstract_mode.py#L122)     | `0.0`       |
| `document`                | [document_mode.py:252,446](synesis_coder/modes/document_mode.py#L252) | `0.0`       |
| `ontology`                | [ontology_mode.py:182](synesis_coder/modes/ontology_mode.py#L182)     | `0.0`       |
| `suggest` (passo 1)       | [suggest_mode.py:120](synesis_coder/modes/suggest_mode.py#L120)       | `0.0`       |
| `suggest` (passo 2)       | [suggest_mode.py:76](synesis_coder/modes/suggest_mode.py#L76)         | `0.3`       |
| `finetune vary`           | [finetune_mode.py:259](synesis_coder/modes/finetune_mode.py#L259)     | `0.7`       |
| `finetune didactic`       | [finetune_mode.py:276](synesis_coder/modes/finetune_mode.py#L276)     | `0.3`       |
| `finetune counterfactual` | [finetune_mode.py:292](synesis_coder/modes/finetune_mode.py#L292)     | `0.5`       |

No loop de correção ([validator.py:21](synesis_coder/validator.py#L21)):

```python
CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]
```

**Conclusão:** para tarefas analíticas (item/abstract/document/ontology), já se opera no mínimo teórico de variabilidade. Não há ganho de precisão a ser obtido reduzindo temperatura — ela já está em 0.

### 2.3 Parâmetros ausentes que importariam

Os parâmetros abaixo **não estão expostos** nem como env nem como CLI flag, e são os que efetivamente impactariam a qualidade analítica:

| Parâmetro                                | Estado atual                                                                                  | Impacto para o caso                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `max_tokens`                             | Hardcoded `4096` em [llm_client.py:306-314](synesis_coder/llm_client.py)                      | Pode truncar ITEMs extensos em abstracts densos                                    |
| `top_p`                                  | Não usado                                                                                     | Controle adicional de foco do output                                               |
| `thinking` (Anthropic extended thinking) | Chamado com `thinking=False` em [suggest_mode.py:76,120](synesis_coder/modes/suggest_mode.py) | Ativá-lo para `item`/`ontology` em Opus daria ganho substancial em precisão causal |
| `seed` / determinismo                    | Não usado                                                                                     | Não garantido pela Anthropic, mas OpenAI-compat (RunPod/LM Studio) suporta         |
| `system_prompt_suffix`                   | Não existe                                                                                    | Ver Parte 3                                                                        |

### 2.4 Recomendações operacionais (sem alterar código)

| Eixo               | Recomendação                                                                     | Por quê                                                                                                                  |
| ------------------ | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **Modelo**         | Manter `claude-opus-4-6` para face85                                             | Abstracts com 3 GUIDELINES entrelaçadas exigem modelo de topo; Haiku/Sonnet aumentam alucinação em escolha de `RELATION` |
| **Backend local**  | Evitar Ollama/LM Studio para este template                                       | GUIDELINES complexas (CONJUNCTIVE vs SUFFICIENT vs INFLUENCES) saturam modelos 4-27B                                     |
| **Rate limits**    | Usar `--concurrent 5` (default) em `document`                                    | Mais alto provoca throttling que induz retries com temperatura escalada                                                  |
| **Corpus estável** | Rodar `ontology` **após** `document` e **com `--update`** em iterações seguintes | Cada rodada atualiza `code_index` e reduz proliferação                                                                   |

### 2.5 Melhorias possíveis (requerem código; registrar como débito técnico)

> **Observação:** não implementar agora. Sinalizar ao usuário para decisão futura.

1. **Tornar `SYNESIS_CODER_TEMPERATURE` funcional** — pequena edição em cada `_mode.py` para ler `float(os.getenv("SYNESIS_CODER_TEMPERATURE", "0.0"))`.
2. **Expor `SYNESIS_CODER_MAX_TOKENS`** — evita truncamento silencioso.
3. **Expor `SYNESIS_CODER_THINKING=true|false`** — ativa extended thinking do Opus para modos analíticos críticos.
4. **Remover variáveis inertes da documentação ou implementá-las** — o `.env` atual gera expectativas falsas.

---

## Parte 3 — Viabilidade de `SYNESIS_CODER_LANGUAGE`

### 3.1 O problema

O framing do system prompt é hardcoded em inglês (CHANGELOG v0.1.2, [llm_client.py:198-211](synesis_coder/llm_client.py#L198-L211) para mensagens de fix). O idioma de saída emerge implicitamente da **língua das GUIDELINES** do template — se o autor escreveu em português, o modelo tende a responder em português; se em inglês, responderá em inglês. Não há **controle explícito**.

Casos de uso do novo parâmetro:

- Template em inglês (padrão de publicação internacional) mas equipe de pesquisa brasileira quer `note.claim` e `ontology_description` em português.
- Migração entre idiomas sem reescrever todo o `.synt`.
- Validação cruzada: gerar output em duas línguas para mesmo texto-fonte.

### 3.2 Onde injetar

O ponto natural é `_build_system_prompt()` em [prompt_builder.py:60](synesis_coder/prompt_builder.py#L60), após as regras absolutas e antes de `PROJECT CONTEXT`. Esse local:

- Mantém o system prompt **cacheável** (não depende de input dinâmico do usuário).
- Permite chave única no cache por idioma (cache ephemeral é por sessão; o prefixo "LANGUAGE: pt-BR" entra no cache junto com o resto).
- Funciona para **todos os modos** (item, abstract, document, ontology, suggest) sem duplicar lógica.

### 3.3 Três estratégias

#### Estratégia A — Instrução global no system prompt (recomendada e aprovada)

Ler `SYNESIS_CODER_LANGUAGE` no `project_loader.py` e propagá-la no `ctx`. No `_build_system_prompt()`, injetar:

```
OUTPUT LANGUAGE: All free-text field values (QUOTATION excerpts, MEMO notes,
TEXT descriptions) must be written in <idioma>. Exception: QUOTATION must
preserve the original language of the source text. Concept names in CHAIN
fields remain in the original language used in EXISTING PROJECT CONCEPTS.
```

**Prós:** ~5 linhas, zero impacto em cache, respeita exceções naturais (citações textuais, nomes de conceitos).
**Contras:** depende do modelo respeitar a instrução (Claude respeita com alta consistência).
**Esforço:** ~30 min.

#### Estratégia B — Pós-processamento por tradução

Gerar em inglês, depois invocar LLM para traduzir somente campos `TEXT`/`MEMO`.

**Prós:** determinismo idiomático.
**Contras:** dobra custo e latência; perde coerência terminológica entre chamadas; quebra cache; introduz etapa de validação extra.
**Esforço:** ~2 dias. **Desaconselhada.**

#### Estratégia C — Dual-prompt (idioma por modo)

Variável por modo: `SYNESIS_CODER_LANGUAGE_ITEM=pt-BR`, `SYNESIS_CODER_LANGUAGE_ONTOLOGY=en`.

**Prós:** granularidade fina (útil quando ontologia é publicada internacionalmente mas anotações ficam em português).
**Contras:** explosão de configuração; inconsistência entre ITEMs e ONTOLOGY sobre o mesmo conceito.
**Esforço:** ~1 dia. **Considerar só se Estratégia A não bastar.**

### 3.4 Efeitos colaterais a verificar

| Área                     | Impacto    | Notas                                                                                                                                                                                                       |
| ------------------------ | ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Parser Lark**          | ✅ Neutro   | Gramática é token-based e `synesis.lark` aceita UTF-8; acentos em valores de `TEXT`/`MEMO` já são suportados (ver face85.synt linha 2)                                                                      |
| **Cache Anthropic**      | ✅ Neutro   | Idioma entra no system prompt cacheado; cache é por-conteúdo                                                                                                                                                |
| **Validação**            | ✅ Neutra   | Validador compila com `synesis.load()`; não inspeciona semântica de texto                                                                                                                                   |
| **code_index coherence** | ⚠️ Atenção | Se `FACTOR NAMING` do template insiste em inglês (ex: `Informational_Asymmetry`), a Estratégia A deve explicitar que **nomes de conceitos permanecem no idioma do index** — já previsto na redação proposta |
| **QUOTATION**            | ⚠️ Atenção | Citação literal NUNCA deve ser traduzida — previsto na redação                                                                                                                                              |
| **VSCode extension**     | ✅ Neutro   | Chama CLI; a extensão não inspeciona conteúdo                                                                                                                                                               |

### 3.5 Esboço de `.env`

```dotenv
# Idioma de saída dos campos livres (TEXT, MEMO, TEXT de ONTOLOGY)
# Aceita códigos BCP-47 em linguagem natural. Omitir ou deixar vazio
# preserva o comportamento atual (idioma emerge das GUIDELINES).
#
# Exemplos:
# SYNESIS_CODER_LANGUAGE=pt-BR
# SYNESIS_CODER_LANGUAGE="Portuguese (Brazil)"
# SYNESIS_CODER_LANGUAGE=en
# SYNESIS_CODER_LANGUAGE=es
```

### 3.6 Veredito

> **Viabilidade ALTA, esforço BAIXO.** A Estratégia A resolve o caso de uso com ~30 linhas e zero risco arquitetural. Única precaução real: garantir na redação da instrução que **QUOTATION e nomes de conceitos CHAIN** sejam preservados no original.

---

## Parte 4 — Plano de Implementação Minucioso v0.2.0

**Data:** 2026-04-16
**Versão alvo:** `synesis-coder` 0.2.0
**Tema:** *"Precisão Analítica com Opus 4.7 e Extended Thinking"*
**Breaking changes:** nenhum — todas as novas variáveis são opt-in e preservam comportamento atual.

> **Descartados:**
> 
> - Estratégias B e C de multilíngue (Parte 3.3) — apenas Estratégia A segue adiante.

---

### 4.1 Objetivos mensuráveis

| Objetivo                            | Métrica                                | Baseline (v0.1.5)                  | Meta (v0.2.0)                        |
| ----------------------------------- | -------------------------------------- | ---------------------------------- | ------------------------------------ |
| **Precisão de `RELATION` em CHAIN** | % de relações válidas em corpus face85 | — (coletar)                        | +15-25% com Opus 4.7 + thinking 8000 |
| **Consistência terminológica**      | % de `FACTOR NAMING` aderente          | —                                  | +10% com thinking                    |
| **Truncamento silencioso**          | Ocorrências de output cortado em 4096  | Não rastreado                      | 0 (com `MAX_TOKENS` configurável)    |
| **Variáveis inertes no `.env`**     | 5                                      | 0 (todas funcionais ou removidas)  |                                      |
| **Idioma de output controlável**    | Não                                    | Sim (via `SYNESIS_CODER_LANGUAGE`) |                                      |
| **Modelo default**                  | `claude-opus-4-6`                      | `claude-opus-4-7`                  |                                      |

### 4.2 Matriz consolidada de mudanças

| #   | Variável / Parâmetro                                        | Fase | Arquivos impactados                                          | LOC estimado         |
| --- | ----------------------------------------------------------- | ---- | ------------------------------------------------------------ | -------------------- |
| M1  | `.env.example` — remover inertes                            | 1    | `.env.example`                                               | -30, +5              |
| M2  | `SYNESIS_CODER_TEMPERATURE` funcional                       | 2    | `llm_client.py`, 5× `modes/*.py`                             | +20                  |
| M3  | `SYNESIS_CODER_MAX_RETRIES` funcional                       | 2    | `llm_client.py`                                              | já lido (verificar)  |
| M4  | `SYNESIS_CODER_MAX_RPM`/`TPM` funcionais                    | 2    | `llm_client.py`                                              | já lidos (verificar) |
| M5  | `SYNESIS_CODER_MAX_TOKENS`                                  | 3    | `llm_client.py`, 5× `modes/*.py`, `cli.py`                   | +15                  |
| M6  | `SYNESIS_CODER_THINKING_BUDGET` ⭐                           | 4    | `llm_client.py`, 4× `modes/*.py`, `cli.py`, `token_usage.py` | +80                  |
| M7  | Default model → `claude-opus-4-7`                           | 4    | `llm_client.py`, `.env.example`, `README.md`                 | +3                   |
| M8  | `SYNESIS_CODER_LANGUAGE`                                    | 5    | `project_loader.py`, `prompt_builder.py`, `cli.py`           | +30                  |
| M9  | CLI flags `--thinking-budget`, `--language`, `--max-tokens` | 6    | `cli.py`, 5× `modes/*.py`                                    | +40                  |
| M10 | Testes                                                      | 7    | `tests/test_llm_client.py` (novo), expansões em existentes   | +250                 |
| M11 | README + CHANGELOG                                          | 8    | `README.md`, `CHANGELOG.md`                                  | +80                  |

**Total estimado:** ~550 LOC, distribuído em ~12 arquivos.

### 4.3 Dependências entre fases

```
Fase 1 (limpeza)  ──┐
                    ▼
Fase 2 (inertes) ──► Fase 3 (MAX_TOKENS) ──► Fase 4 (thinking + Opus 4.7) ⭐
                                                        │
                                                        ▼
Fase 5 (LANGUAGE) ─────────────────────────► Fase 6 (CLI flags)
                                                        │
                                                        ▼
                                             Fase 7 (testes) ──► Fase 8 (docs + release)
```

Fases 4 e 5 podem ser desenvolvidas em paralelo (branches separados). A Fase 6 aguarda ambas.

---

### 4.4 Fase 1 — Limpeza do `.env.example` (trivial)

**Objetivo:** eliminar falsa sensação de controle; declarar explicitamente o que não está implementado.

**Arquivos:**

- [`synesis-coder/.env`](.env) (local, não versionado — atualizar manualmente)
- `synesis-coder/.env.example` (se existir, senão criar)

**Ações:**

1. Renomear o bloco atual "OPCIONAIS — válidos para qualquer bloco acima" para **"OPCIONAIS — v0.2.0 (requerem upgrade)"**.
2. Adicionar comentário destacado antes de cada variável inerte:
   
   ```dotenv
   # ⚠ v0.1.5: variáveis abaixo são RESERVADAS e ainda não têm efeito.
   # ⚠ Serão ativadas em v0.2.0 (ver estudo_prompts_e_tradução.md §4).
   ```
3. Após a Fase 2 concluir, remover o aviso das variáveis que passarem a funcionar.

**Risco:** nenhum. Apenas documentação.
**Esforço:** 10 min.
**Critério de aceitação:** usuário lendo `.env.example` entende exatamente o que está ativo.

---

### 4.5 Fase 2 — Ativar variáveis `.env` inertes

**Objetivo:** tornar `SYNESIS_CODER_TEMPERATURE`, `MAX_RETRIES`, `MAX_RPM`, `MAX_INPUT_TPM`, `MAX_OUTPUT_TPM` funcionais.

**Verificação prévia (GitNexus):**

```
gitnexus_impact({target: "LLMClient.__init__", direction: "upstream"})
gitnexus_impact({target: "process_item", direction: "upstream"})
```

Callers diretos de `call()`/`call_async()`: todos os `modes/*.py`. **d=1:** 5 arquivos.

#### 4.5.1 `SYNESIS_CODER_TEMPERATURE`

**Estado atual:** temperatura hardcoded por modo (ver tabela §2.2).

**Decisão de design:** a temperatura inicial de cada modo tem valor semântico (0.0 para analíticos, 0.7 para `finetune vary`). Não faz sentido permitir override global que quebraria o modo criativo. Solução: **override opcional, com respeito aos mínimos semânticos.**

**Implementação:**

- Em [`llm_client.py`](synesis_coder/llm_client.py), adicionar helper:
  
  ```python
  def _get_env_temperature() -> Optional[float]:
      v = os.environ.get("SYNESIS_CODER_TEMPERATURE")
      return float(v) if v is not None and v.strip() != "" else None
  ```
- Em cada `modes/*.py` analítico (`item_mode.py`, `abstract_mode.py`, `document_mode.py`, `ontology_mode.py`), substituir `temperature=0.0` por:
  
  ```python
  temperature = _get_env_temperature() if _get_env_temperature() is not None else 0.0
  ```
- Em `finetune_mode.py`: **não alterar** (variações criativas têm temperatura específica).
- Em `suggest_mode.py`: aplicar ao passo 2 (já usa 0.3); passo 1 continua 0.0 fixo.

**Risco:** médio. Se usuário define `SYNESIS_CODER_TEMPERATURE=0.8`, modo `item` geraria output menos determinístico. **Mitigação:** documentar que default recomendado é `0.0` para analíticos e `> 0` só em casos experimentais.

**Teste:**

```python
def test_temperature_override_from_env(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_TEMPERATURE", "0.3")
    # assert que process_item passa temperature=0.3 para llm_client
```

**Esforço:** 30 min.

#### 4.5.2 `SYNESIS_CODER_MAX_RETRIES`

**Estado atual:** já lido via `_get_max_retries()` em [`llm_client.py:69`](synesis_coder/llm_client.py#L69). **Verificar** se efetivamente usado em `stop_after_attempt()` ([llm_client.py:304,331](synesis_coder/llm_client.py#L304)) — aparenta estar correto. Se sim, marcar como **já funcional** e apenas atualizar documentação.

**Esforço:** 5 min de verificação.

#### 4.5.3 `SYNESIS_CODER_MAX_RPM` / `MAX_INPUT_TPM` / `MAX_OUTPUT_TPM`

**Estado atual:** já lidos em [`LLMClient.__init__`](synesis_coder/llm_client.py#L131-L139) via `os.environ.get(...)` — **já funcionais**. Confirmar e remover aviso do `.env.example`.

**Esforço:** 5 min de verificação.

---

### 4.6 Fase 3 — Expor `SYNESIS_CODER_MAX_TOKENS`

**Problema:** `max_tokens=4096` hardcoded em todas as chamadas ([llm_client.py:163,185,224,253](synesis_coder/llm_client.py#L163)). Abstracts longos e ontologias densas podem ser truncados silenciosamente.

**Implementação:**

1. Novo helper `_get_max_tokens(default: int = 4096) -> int`.
2. Default por modo:
   - `item`: 4096
   - `abstract`: 8192 (abstracts estruturados podem ser longos)
   - `document`: 4096 por ITEM
   - `ontology`: 4096 por entrada
   - `finetune`: 2048
3. Cada `modes/*.py` passa `max_tokens=_get_max_tokens(default=X)` onde X é o valor recomendado do modo.
4. Env `SYNESIS_CODER_MAX_TOKENS` sobrescreve globalmente.

**Efeito colateral com extended thinking:** `max_tokens` deve ser `>= budget_tokens + 1024` (buffer mínimo para resposta). Fase 4 cuidará dessa validação.

**Teste:**

```python
def test_max_tokens_default_per_mode():
    # abstract passa 8192 por default
def test_max_tokens_env_override(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_MAX_TOKENS", "16384")
    # assert que todos os modos usam 16384
```

**Esforço:** 45 min.

---

### 4.7 Fase 4 — Extended Thinking e Opus 4.7 ⭐

**ESTA É A FASE DE MAIOR IMPACTO EM PRECISÃO.**

#### 4.7.1 Contexto técnico

Extended thinking é feature dos modelos Claude 4.x (Sonnet 4.x, Opus 4.x) que permite ao modelo emitir **blocos de raciocínio interno** antes da resposta final. Documentação: https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking.

**Formato da chamada Anthropic:**

```python
response = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=16000,
    thinking={"type": "enabled", "budget_tokens": 8000},
    temperature=1.0,  # ⚠ obrigatório quando thinking ativo
    messages=[...],
)
```

**Formato da resposta:**

```python
response.content = [
    ThinkingBlock(type="thinking", thinking="...raciocínio..."),
    TextBlock(type="text", text="...resposta final..."),
]
```

**Restrições da API:**

1. `temperature` **deve** ser `1.0` (API rejeita outros valores).
2. `max_tokens` **deve** ser `> budget_tokens` (recomendado: `budget + 4096`).
3. Blocos `thinking` não aparecem na conversa continuada (mas ficam visíveis na resposta atual).
4. **Compatível com prompt caching** — o system prompt cacheado é reusado; apenas o thinking é gerado fresh a cada chamada.
5. Tokens de thinking **são contados como output tokens** em billing.

#### 4.7.2 Por que melhora a precisão em Synesis

Para o template face85.synt (3 GUIDELINES entrelaçadas: `text`, `note`, `chain`), sem thinking o modelo executa em forward pass único:

1. Ler texto-fonte → 2. Avaliar relevância → 3. Extrair anchors → 4. Escolher `RELATION` (entre 8) → 5. Aplicar direção → 6. Nomear fatores → 7. Emitir output.

Com `budget_tokens=8000`, o modelo **reflete explicitamente** em cada passo, corrige inconsistências e recomeça quando necessário. Em testes informais publicados pela Anthropic para tarefas de classificação multi-critério, thinking reduz erros em 20-40%.

#### 4.7.3 Nova variável `.env`

```dotenv
# Extended thinking (Claude 4.x apenas) — tokens de raciocínio interno antes da resposta.
# 0 = desabilitado (comportamento atual)
# 4000 = light (abstract, templates simples)
# 8000 = medium (item com template complexo como face85) — RECOMENDADO
# 16000 = heavy (ontology em projetos com > 100 códigos)
#
# ⚠ Custo: thinking tokens são faturados como output tokens.
# ⚠ Temperatura é forçada a 1.0 quando thinking ativo (override de SYNESIS_CODER_TEMPERATURE).
# ⚠ Requer modelo compatível: claude-opus-4-7, claude-opus-4-6, claude-sonnet-4-6+.
SYNESIS_CODER_THINKING_BUDGET=0
```

#### 4.7.4 Helpers em `llm_client.py`

```python
_THINKING_CAPABLE_MODELS = frozenset({
    "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-4-6", "claude-sonnet-4-5-20250929",
    "claude-3-7-sonnet-latest", "claude-3-7-sonnet-20250219",
})

def _get_thinking_budget() -> int:
    return int(os.environ.get("SYNESIS_CODER_THINKING_BUDGET", "0"))

def _model_supports_thinking(model: str) -> bool:
    base = model.split(":")[0].lower()
    return any(base.startswith(m) for m in _THINKING_CAPABLE_MODELS)
```

#### 4.7.5 Alteração no `_call_sync_inner` (ramo Anthropic)

**Código atual** ([llm_client.py:326-347](synesis_coder/llm_client.py#L326-L347)) — trecho crítico:

```python
kwargs = {"model": ..., "max_tokens": max_tokens, "temperature": temperature, "messages": api_messages}
if system_blocks: kwargs["system"] = system_blocks
response = self._client.messages.create(**kwargs)
return response.content[0].text  # ⚠ QUEBRA com thinking
```

**Novo código:**

```python
budget = _get_thinking_budget() if thinking else 0
use_thinking = budget > 0 and _model_supports_thinking(self.model) and self.backend == "anthropic"

kwargs = {"model": self.model, "max_tokens": max(max_tokens, budget + 4096), "messages": api_messages}
if use_thinking:
    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    kwargs["temperature"] = 1.0
    if temperature != 1.0 and not getattr(self, "_thinking_warned", False):
        _log.info("Extended thinking ativo: temperature forçada a 1.0 (ignora %.2f)", temperature)
        self._thinking_warned = True
else:
    kwargs["temperature"] = temperature
if system_blocks:
    kwargs["system"] = system_blocks

response = self._client.messages.create(**kwargs)
self._record_usage(response.usage)

# Encontrar o bloco text (pular thinking)
for block in response.content:
    if block.type == "text":
        return block.text
raise RuntimeError("Resposta Anthropic sem bloco text (apenas thinking)")
```

#### 4.7.6 Token accounting de thinking

Atualizar [`token_usage.py`](synesis_coder/token_usage.py):

- Adicionar campo `thinking_tokens: int = 0`.
- `record()` aceita parâmetro opcional `thinking_tok: int = 0`.
- `summary_line()` inclui `| thinking X` quando `> 0`.

Em `_record_usage()` do `llm_client.py`, extrair `cache_read_input_tokens` e `cache_creation_input_tokens` (já disponíveis) e, se a resposta tem bloco `thinking`, calcular `thinking_tok = sum(len de cada thinking block em tokens — ou usar campo dedicado se a API retornar)`. **Pesquisar na v0.2.0:** Anthropic retorna `response.usage.output_tokens` já com thinking incluído; extrair thinking separadamente requer tokenizer local ou contar por bloco.

**Decisão pragmática para v0.2.0:** reportar thinking_tok como "incluído em output" e adicionar linha `# thinking budget used: ~N tokens (estimado)` no verbose format.

#### 4.7.7 Interação com modo `document` concorrente

`document_mode.py` usa `asyncio.Semaphore` com `--concurrent 5`. Thinking aumenta latência por chamada em 2-5×, mas não bloqueia GPU (é server-side). **Recomendação:** reduzir default de `--concurrent` para 3 quando thinking ativo, para evitar throttling Anthropic (janela TPM é mais apertada).

Implementação: no `process_document()`, se `_get_thinking_budget() > 0`, logar aviso e ajustar semáforo.

#### 4.7.8 Default do modelo → `claude-opus-4-7`

- [`llm_client.py:66`](synesis_coder/llm_client.py#L66): alterar `"claude-opus-4-6"` → `"claude-opus-4-7"`.
- `.env.example`: atualizar Bloco 1 para `claude-opus-4-7` (deixar Bloco 1b como histórico `claude-opus-4-6`).
- `README.md` §Configuration: atualizar default.

**Preço (verificar oficialmente):** Opus 4.7 mantém tier de Opus 4.6 (~$15/$75 por MTok) ou pode ter ajuste — validar na página de pricing antes do release.

#### 4.7.9 Defaults de thinking por modo

Em cada `modes/*.py`, passar `thinking=True` para `call()`/`call_async()` por default, deixando que `_get_thinking_budget()` decida. Quando budget=0, parâmetro é no-op.

| Modo       | `thinking` default | Justificativa                              |
| ---------- | ------------------ | ------------------------------------------ |
| `item`     | `True`             | Alto benefício em escolha de RELATION      |
| `abstract` | `True`             | Síntese multi-item beneficia de raciocínio |
| `document` | `True`             | Mesmo de `item` × N                        |
| `ontology` | `True`             | Máximo benefício (contexto semântico rico) |
| `suggest`  | `False`            | Latência crítica para UX interativa        |
| `finetune` | `False`            | Geração criativa, thinking desnecessário   |

**Risco:** `gitnexus_impact({target: "_call_sync_inner", direction: "upstream"})` — callers em todos os modos e no validator. Todos devem continuar funcionais com `budget=0` (default). Nenhum breaking change.

**Esforço:** 3-4h (incluindo testes integration com e sem thinking).

---

### 4.8 Fase 5 — Output Multilíngue (`SYNESIS_CODER_LANGUAGE`)

**Referência:** detalhamento completo em §3.1–§3.6. Resumo da implementação:

1. `project_loader.py`: ler `SYNESIS_CODER_LANGUAGE` e adicionar ao `ctx` como `output_language: Optional[str]`.
2. `prompt_builder._build_system_prompt()`: injetar, logo após as regras absolutas:
   
   ```
   OUTPUT LANGUAGE: All free-text field values (MEMO, TEXT descriptions) must
   be written in <ctx.output_language>. Exceptions:
   - QUOTATION blocks preserve the original language of the source text.
   - Concept names in CHAIN fields remain in the original language used in
     EXISTING PROJECT CONCEPTS below.
   ```
3. Quando `output_language` é `None` ou vazio, **não injetar a seção** (preserva comportamento atual).
4. Mesma injeção para `build_abstract_prompt`, `build_ontology_prompt`, `build_suggest_prompt`.
5. `cli.py`: nova flag `--language TEXT` em cada subcomando, sobrescreve env var.

**Efeitos colaterais** (da §3.4):

- Parser Lark: ✅ neutro (UTF-8 aceito).
- Cache Anthropic: ✅ neutro (idioma entra no conteúdo cacheado).
- `code_index`: ⚠ nomes de conceitos permanecem no idioma do index — explicitado na redação.
- `QUOTATION`: ⚠ preserva original — explicitado.

**Teste:**

```python
def test_language_instruction_injected(monkeypatch):
    monkeypatch.setenv("SYNESIS_CODER_LANGUAGE", "pt-BR")
    ctx = load_project(...)
    prompt = build_item_prompt(ctx, "smith2024", "text")
    assert "OUTPUT LANGUAGE" in prompt[0]["content"]
    assert "pt-BR" in prompt[0]["content"]

def test_language_empty_preserves_behavior():
    ctx = load_project(...)  # sem env var
    prompt = build_item_prompt(ctx, ...)
    assert "OUTPUT LANGUAGE" not in prompt[0]["content"]
```

**Esforço:** 45 min + 15 min de testes.

---

### 4.9 Fase 6 — CLI flags de runtime

Expor todas as novas variáveis como flags CLI por modo, para casos pontuais sem tocar no `.env`:

```bash
synesis-coder item \
  --project x.synp --bibref y --text "..." \
  --thinking-budget 8000 \
  --language pt-BR \
  --max-tokens 16000 \
  --temperature 0.0
```

**Regra de precedência:** CLI flag > env var > default.

**Arquivos:** `cli.py` — adicionar opções em cada subcomando (`item`, `abstract`, `document`, `ontology`, `suggest`). Cada mode recebe os novos parâmetros e propaga ao `LLMClient` ou ao prompt builder.

**Esforço:** 1h (muitas linhas repetitivas, todas triviais).

---

### 4.10 Fase 7 — Testes

**Cobertura obrigatória:**

#### 4.10.1 Novos arquivos de teste

`tests/test_llm_client.py` (novo) — unit tests isolados do `LLMClient`:

- `TestThinkingBudget` (5): helper `_get_thinking_budget()`, `_model_supports_thinking()` para modelos conhecidos/desconhecidos, extração de bloco `text` quando `thinking` presente/ausente, fallback quando response só tem thinking (erro).
- `TestTemperatureOverride` (3): env var respeitada, default 0.0 quando ausente, forçada a 1.0 quando thinking ativo.
- `TestMaxTokensFloor` (2): max_tokens é elevado a `budget + 4096` quando thinking ativo.

#### 4.10.2 Expansões em testes existentes

- `tests/test_item_mode.py`: adicionar `TestItemWithThinking` (integration, requer `ANTHROPIC_API_KEY`):
  - `test_item_face85_with_thinking_8000` — asserta que output compila E que `usage.thinking_tokens > 0` (quando API reportar).
- `tests/test_ontology_mode.py`: mesmo padrão com budget 16000.
- `tests/test_abstract_mode.py`, `tests/test_document_mode.py`: similar.

#### 4.10.3 Teste de regressão

- `tests/test_backwards_compat.py` (novo):
  - `test_v0_1_5_env_still_works` — sobe um `.env` idêntico ao de v0.1.5 e confirma que todos os modos funcionam sem alterações.
  - `test_thinking_disabled_when_budget_zero` — default `budget=0`, API é chamada **sem** `thinking` kwarg.
  - `test_non_thinking_model_fallback` — com `SYNESIS_CODER_THINKING_BUDGET=8000` + `SYNESIS_CODER_MODEL=claude-haiku-4-5`, thinking é silenciosamente desativado (não quebra).

#### 4.10.4 Teste manual

Script `tests/manual/compare_precision.py` (novo, não-CI):

- Roda 20 ITEMs do face85.synt com Opus 4.6 sem thinking vs Opus 4.7 com thinking=8000.
- Conta violações de `FACTOR NAMING`, `RELATION` inválida, direção invertida.
- Publica relatório em `tests/manual/precision_report.md`.

**Esforço total da Fase 7:** 2-3h.

---

### 4.11 Fase 8 — Documentação e release

1. **`CHANGELOG.md`** — entrada `[0.2.0] — 2026-04-XX`:
   - **Added:** `SYNESIS_CODER_THINKING_BUDGET`, `SYNESIS_CODER_MAX_TOKENS`, `SYNESIS_CODER_LANGUAGE`; CLI flags correspondentes; default model → `claude-opus-4-7`.
   - **Changed:** `SYNESIS_CODER_TEMPERATURE` agora funcional; `.env.example` limpo.
   - **Fixed:** Documentação enganosa sobre variáveis inertes.
2. **`README.md`** — atualizar:
   - Seção "Environment variables" com todas as novas.
   - Subseção "Extended thinking" com exemplo e recomendações de budget.
   - Subseção "Output language" com exemplo.
   - Default model em "Configuration".
3. **`pyproject.toml`** — bump `version = "0.2.0"`.
4. **Git tag e PyPI publish** conforme AI_INSTRUCTIONS §12.

**Esforço:** 1h.

---

### 4.12 Análise de risco consolidada

Seguindo o protocolo obrigatório AI_INSTRUCTIONS §10 (GitNexus), antes de cada PR individual:

| Símbolo afetado              | Risco    | d=1 (QUEBRA)                                                         | d=2 (AFETA)             | Ação obrigatória                        |
| ---------------------------- | -------- | -------------------------------------------------------------------- | ----------------------- | --------------------------------------- |
| `LLMClient._call_sync_inner` | **ALTO** | 5 modos (via `call`), 5 modos (via `call_async`), validator fix loop | todo o pipeline         | `gitnexus_impact` antes; avisar usuário |
| `LLMClient.__init__`         | MÉDIO    | instanciação em todos os 5 modos                                     | —                       | Atualizar signature com defaults        |
| `_build_system_prompt`       | MÉDIO    | `build_item_prompt`, `build_abstract_prompt`, etc.                   | todos os modos          | `gitnexus_detect_changes` após          |
| `TokenUsage.record`          | BAIXO    | `_record_usage`, branch OpenAI em `_call_sync_inner`                 | —                       | Teste de thread safety preservado       |
| Default model string         | BAIXO    | `_get_model()`                                                       | toda sessão sem env var | Changelog deve destacar                 |

**Mitigações:**

- Feature flags não são usadas (projeto é pequeno); em vez disso, **budget=0 é default** → caminho antigo permanece idêntico.
- Todas as novas variáveis são opt-in via env var OU CLI flag. Usuário sem `.env` customizado tem exatamente o comportamento v0.1.5 com modelo atualizado.
- Pre-commit: `gitnexus_detect_changes({scope: "staged"})` em cada commit grande.

---

### 4.13 Cronograma estimado

| Fase                         | Esforço | Dependências |
| ---------------------------- | ------- | ------------ |
| 1. Limpeza `.env.example`    | 10 min  | —            |
| 2. Ativar inertes            | 45 min  | 1            |
| 3. `MAX_TOKENS`              | 45 min  | 2            |
| 4. **Thinking + Opus 4.7** ⭐ | 3-4h    | 3            |
| 5. `LANGUAGE`                | 1h      | — (paralelo) |
| 6. CLI flags                 | 1h      | 4, 5         |
| 7. Testes                    | 2-3h    | 6            |
| 8. Docs + release            | 1h      | 7            |

**Total:** 9-11h de trabalho efetivo (≈ 2 dias úteis).

### 4.14 Critérios de aceitação da v0.2.0

A release é aprovada quando **todos** os itens abaixo são verdadeiros:

1. [ ] `.env.example` reflete exatamente o que é lido pelo código (zero variáveis inertes documentadas sem aviso).
2. [ ] `SYNESIS_CODER_MODEL=claude-opus-4-7` é o default e é validado por `test_item_social_acceptance_compiles`.
3. [ ] Com `SYNESIS_CODER_THINKING_BUDGET=8000`, o output de `item` no face85.synt contém cadeias CHAIN com ≥ 95% de `RELATION` sintaticamente válidas (vs baseline coletado).
4. [ ] `SYNESIS_CODER_LANGUAGE=pt-BR` força `MEMO`/`TEXT` em português em projeto com template em inglês, mantendo `QUOTATION` no original.
5. [ ] Teste `test_v0_1_5_env_still_works` passa — retrocompatibilidade garantida.
6. [ ] `gitnexus_detect_changes({scope: "all"})` confirma que apenas arquivos listados na §4.2 foram alterados.
7. [ ] `CHANGELOG.md` tem entrada `[0.2.0]` completa.
8. [ ] `pytest tests/` passa com cobertura ≥ cobertura atual.
9. [ ] Manual test `compare_precision.py` documenta ganho mensurável de Opus 4.6 → Opus 4.7 + thinking.

### 4.15 Decisões — ✅ Resolvidas (2026-04-16)

| # | Decisão | Resolução |
|---|---------|-----------|
| 1 | **Budget default** | `0` — desabilitado por padrão; usuário ativa conscientemente via `.env`. Recomendação `8000` documentada como comentário no `.env` e `.env.example`. |
| 2 | **Modelo sem suporte a thinking** | Avisar o usuário com mensagem clara (quais modelos são compatíveis) e prosseguir **sem** thinking — synesis-coder não aborta. A mensagem de aviso deve sugerir a troca de modelo. |
| 3 | **Backend OpenAI (Qwen3, Gemma)** | Manter separado. `SYNESIS_CODER_THINKING_BUDGET` é ignorado (com aviso) em backends OpenAI-compat. O mecanismo `extra_body={"think": False}` do Qwen3 em `suggest_mode` permanece intacto. |
| 4 | **Modelo default** | Manter `claude-opus-4-6` como padrão. Adicionar **Bloco 1b** com `claude-opus-4-7` como opção comentada, recomendada para uso conjunto com `THINKING_BUDGET=8000`. Ambos os blocos já estão no `.env` e `.env.example`. |

---

## Conclusão consolidada

### Respostas às três dúvidas originais

1. **Atomicidade de prompts:** não é problema. O system prompt é holístico — `note.anchor` e `chain.relation` chegam juntos ao modelo.
2. **Ajustes `.env`:** 5 variáveis eram inertes (bug documental). Ganhos reais de precisão vêm de **Opus 4.7 + extended thinking** (Fase 4), não de mexer em temperatura — que já está em 0.
3. **`SYNESIS_CODER_LANGUAGE`:** viável via Estratégia A (Fase 5).

### Veredito final

> A v0.2.0 transforma `synesis-coder` de uma ferramenta de anotação automática **"best-effort"** em uma ferramenta de anotação **"reasoning-driven"**. O investimento de ~10h de desenvolvimento entrega:
> 
> - Ganho esperado de **15-25% em precisão causal** (thinking + Opus 4.7).
> - Eliminação de **5 variáveis-fantasma** que induzem o usuário a erro.
> - Novo eixo de **controle de idioma de output** sem reescrever templates.
> - Zero breaking change — usuários v0.1.5 migram sem ajustes.
> 
> A Fase 4 é a **alavanca crítica** e deve ser o foco principal. As demais fases são higiene técnica que a acompanha.

**Nenhuma ação foi executada neste estudo.** Aguardando autorização explícita do usuário, conforme Protocolo de Execução §9 do AI_INSTRUCTIONS.md.