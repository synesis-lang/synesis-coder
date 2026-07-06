# Estudo de Viabilidade: Métricas de Qualidade para Anotações Synesis

## Controle de Qualidade no Pipeline de Geração de Anotações

> **Contexto:** OTIC/Hu-Tech Lab — Synesis-Coder | USP | Projeto Shell Brasil  
> **Foco:** Verificação de conformidade das anotações geradas por LLM com o template (.synt)  
> **Versão:** 1.0 | Abril 2026

---

## Sumário

1. [Diagnóstico do pipeline atual](#1-diagnóstico-do-pipeline-atual)
2. [Taxonomia de falhas de qualidade](#2-taxonomia-de-falhas-de-qualidade)
3. [Métricas propostas](#3-métricas-propostas)
4. [Análise de viabilidade por métrica](#4-análise-de-viabilidade)
5. [Pontos de inserção no pipeline](#5-pontos-de-inserção-no-pipeline)
6. [Plano de ação](#6-plano-de-ação)
7. [O que não implementar](#7-o-que-não-implementar)

---

## 1. Diagnóstico do Pipeline Atual

### 1.1 Fluxo de geração

```
Template (.synt)
    ↓
load_project() → context dict (field_specs, indexes, relations)
    ↓
build_*_prompt() → system + user messages
    ↓
LLMClient.call() → raw text output
    ↓
validate_and_fix() → synesis.load() + correction loop (até 3x)
    ↓
Output final (.syn / .syno)
```

### 1.2 O que já é verificado

| Camada              | Mecanismo                                                          | Arquivo             |
| ------------------- | ------------------------------------------------------------------ | ------------------- |
| Sintaxe             | `synesis.load()` valida estrutura de blocos ITEM/SOURCE/ONTOLOGY   | `validator.py`      |
| Campos obrigatórios | Compilador verifica presença de REQUIRED fields                    | `validator.py`      |
| Tipos de campo      | SCALE em range, ENUMERATED/ORDERED com valores permitidos          | `synesis.load()`    |
| Relações            | CHAIN relations devem existir no template                          | `synesis.load()`    |
| Correção automática | Loop de até 3 tentativas com escalada de temperatura (0.0→0.2→0.5) | `validator.py`      |
| Reuso de códigos    | code_index injetado no prompt com instrução "prefer existing"      | `prompt_builder.py` |
| Reuso de tópicos    | topic_index injetado no prompt                                     | `prompt_builder.py` |

### 1.3 O que NÃO é verificado

| Falha                               | Consequência                                    | Status       |
| ----------------------------------- | ----------------------------------------------- | ------------ |
| **Alucinação de códigos**           | LLM inventa codes sem evidência no texto        | Sem detecção |
| **Codificação forçada**             | LLM preenche campos opcionais sem base textual  | Sem detecção |
| **Não-conformidade com guidelines** | LLM ignora instruções metodológicas do template | Sem detecção |
| **Cadeia causal fabricada**         | CHAIN não reflete relação expressa no texto     | Sem detecção |
| **Fidelidade da citação**           | QUOTATION field não corresponde ao texto fonte  | Sem detecção |
| **Granularidade inadequada**        | Códigos específicos demais ou genéricos demais  | Sem detecção |
| **Confiança da anotação**           | Sem score indicando qualidade/certeza           | Inexistente  |

### 1.4 Conclusão do diagnóstico

O pipeline atual garante **validade sintática** mas não **qualidade semântica**. O compilador Synesis responde "isto é um bloco ITEM válido?", mas não responde "esta anotação reflete fielmente o texto fonte?". Esta lacuna é o foco deste estudo.

---

## 2. Taxonomia de Falhas de Qualidade

### 2.1 Falhas na geração de anotações

Classificação baseada na análise do pipeline `prompt_builder.py` → `llm_client.py` → `validator.py` e das guidelines do template `social_acceptance.synt`.

#### F1: Alucinação de Código (Code Hallucination)

**Definição:** LLM gera um code (campo CODE ou nó de CHAIN) que não tem fundamentação no texto fonte.

**Mecanismo:** O prompt injeta o code_index existente e diz "prefer these; create new only when none apply". Quando nenhum code existente se aplica, o LLM pode criar um code que reflete seu conhecimento geral, não o conteúdo do texto.

**Exemplo:**

- Texto: "Community engagement increases project acceptance"
- Code gerado: `Stakeholder_Salience` (conceito real de gestão, mas não mencionado no texto)
- Code correto: `Community_Engagement` ou `Acceptance`

**Detectabilidade:** Média — pode ser verificado por presença lexical do conceito no texto fonte.

#### F2: Codificação Forçada (Forced Coding)

**Definição:** LLM preenche campos opcionais (OPTIONAL) mesmo quando o texto não fornece informação suficiente, gerando valores genéricos ou vagos.

**Mecanismo:** O system prompt instrui "OPTIONAL only when relevant", mas o LLM tende a preencher todos os campos por padrão (viés de completude).

**Exemplo:**

- Texto: "Wind energy costs have decreased significantly"
- Campo opcional `rgt_element_a` preenchido com: "High Cost" / `rgt_element_b`: "Low Cost"
- Problema: O texto não discute um constructo bipolar — o LLM fabricou a polaridade

**Detectabilidade:** Baixa — difícil distinguir preenchimento legítimo de forçado sem análise semântica profunda.

#### F3: Cadeia Causal Fabricada (Fabricated Chain)

**Definição:** CHAIN field contém uma relação causal (A → RELATION → B) que não é expressa ou implicada no texto fonte.

**Mecanismo:** O template `social_acceptance.synt` define 5 tipos de relação (ENABLES, INFLUENCES, CONSTRAINS, CONTESTED-BY, RELATES-TO) com guidelines detalhadas. O LLM pode construir cadeias plausíveis mas não fundamentadas.

**Exemplo:**

- Texto: "Trust and participation are important for acceptance"
- Chain gerada: `Trust -> ENABLES -> Participation -> INFLUENCES -> Acceptance`
- Problema: O texto diz que ambos são importantes, não que Trust habilita Participation. A cadeia sequencial foi fabricada — o correto seriam duas cadeias paralelas.

**Detectabilidade:** Média-Alta — o template explicita regras de SEQUENTIAL vs PARALLEL chains. A violação é verificável por análise do texto.

#### F4: Infidelidade na Citação (Quotation Infidelity)

**Definição:** Campo QUOTATION (tipo TEXT com scope ITEM no template, campo `text`) contém texto que não aparece literalmente no fonte, ou é uma paráfrase apresentada como citação direta.

**Mecanismo:** O campo `text` é do tipo QUOTATION com guideline "Extract COMPLETE, SELF-CONTAINED semantic units (1-3 sentences)". O LLM pode parafrasear em vez de extrair.

**Detectabilidade:** Alta — verificável por string matching/fuzzy matching contra o texto fonte.

#### F5: Não-Conformidade com Guidelines (Guideline Non-Compliance)

**Definição:** O output do LLM viola instruções específicas do template, mesmo sendo sintaticamente válido.

**Exemplos de violação (do template `social_acceptance.synt`):**

- `note` excede 25 palavras (guideline: "Maximum 25 words")
- `chain` usa relação RELATES-TO frequentemente (guideline: "LAST RESORT, use in less than 5%")
- `text` extraído tem score 1-2 de valor analítico (guideline: "Extract only score 3-5")
- Códigos compostos violam regra de granularidade (guideline: "AVOID COMPOUND FACTORS")

**Detectabilidade:** Variável — algumas regras são verificáveis programaticamente (contagem de palavras, % de RELATES-TO), outras requerem julgamento semântico.

#### F6: Proliferação de Conceitos (Concept Proliferation)

**Definição:** LLM cria conceitos novos em excesso em vez de reutilizar os existentes do code_index.

**Mecanismo:** O prompt informa "prefer existing codes" mas o LLM pode gerar variações desnecessárias (e.g., `Public_Trust`, `Community_Trust`, `Social_Trust`, `Institutional_Trust` quando `Trust` seria suficiente).

**Detectabilidade:** Alta — taxa de codes novos vs existentes é calculável automaticamente. O template define: "Target: fewer than 150 unique factors across the full corpus."

---

## 3. Métricas Propostas

### 3.1 Visão geral

| ID  | Métrica                     | Falha detectada | Automação | Custo |
| --- | --------------------------- | --------------- | --------- | ----- |
| M1  | Quotation Fidelity Score    | F4              | Total     | Baixo |
| M2  | Code Grounding Rate         | F1              | Parcial   | Baixo |
| M3  | Chain Evidence Score        | F3              | Parcial   | Médio |
| M4  | Guideline Compliance Checks | F5              | Parcial   | Baixo |
| M5  | Code Reuse Rate             | F6              | Total     | Baixo |
| M6  | Optional Field Fill Rate    | F2              | Total     | Baixo |
| M7  | RELATES-TO Frequency        | F5              | Total     | Baixo |
| M8  | Annotation Confidence Score | Todas           | Composta  | Médio |

### 3.2 Detalhamento das métricas

#### M1: Quotation Fidelity Score (QFS)

**O que mede:** Grau em que o campo `text` (tipo QUOTATION) corresponde ao texto fonte original.

**Cálculo:**

```
QFS = média(similarity(text_field, melhor_match_no_fonte)) para cada ITEM
```

Onde `similarity` pode ser:

- **Nível 1 (exato):** Longest Common Substring normalizado — barato, detecta cópias literais
- **Nível 2 (fuzzy):** Levenshtein/SequenceMatcher — detecta paráfrases leves
- **Nível 3 (semântico):** Embedding similarity — detecta paráfrases profundas (requer modelo de embeddings)

**Implementação recomendada:** Nível 1 + Nível 2 (sem dependência de embeddings).

**Threshold:**

- QFS ≥ 0,85 → citação fidedigna
- QFS 0,60–0,84 → possível paráfrase (flag ⚠️)
- QFS < 0,60 → provável fabricação (flag ❌)

**Viabilidade:** ✅ Alta — os dados necessários (texto fonte e campo text) já estão disponíveis no pipeline. O abstract/text é passado como input do prompt; o campo text está no output.

---

#### M2: Code Grounding Rate (CGR)

**O que mede:** Proporção de codes gerados que têm fundamentação lexical no texto fonte.

**Cálculo:**

```
CGR = (codes cujo conceito aparece no texto) / (total de codes gerados)
```

"Aparecer no texto" = o code ou seus componentes (split por `_`) são encontrados como tokens no texto fonte (case-insensitive, com stemming básico).

**Exemplo:**

- Code: `Community_Engagement` → tokens: ["community", "engagement"]
- Texto: "community engagement increases acceptance" → ambos presentes → grounded ✅
- Code: `Stakeholder_Salience` → tokens: ["stakeholder", "salience"]
- Texto: "community engagement increases acceptance" → nenhum presente → ungrounded ❌

**Nuance:** Códigos existentes do code_index reutilizados pelo LLM podem legitimamente não aparecer no texto (o conceito é inferido). A métrica deve distinguir:

- **Codes novos** (não no code_index): CGR rigoroso
- **Codes reutilizados** (do code_index): CGR informativo (warning, não erro)

**Threshold:**

- Codes novos: CGR ≥ 0,80
- Codes reutilizados: sem threshold bloqueante (informativo)

**Viabilidade:** ✅ Alta — requer apenas análise lexical do output contra o input. Sem dependência de modelo externo.

---

#### M3: Chain Evidence Score (CES)

**O que mede:** Grau em que a cadeia causal (CHAIN) reflete relações expressas no texto fonte.

**Cálculo em dois níveis:**

**Nível 1 — Presença de nós:** Cada nó da chain (concept) deve ser fundamentado no texto (mesma lógica de M2).

```
CES_nodes = (nós da chain com grounding) / (total de nós)
```

**Nível 2 — Presença de relação:** Para cada aresta (A → REL → B), verificar se o texto contém indicadores linguísticos da relação:

- ENABLES: "enables", "allows", "permits", "necessary for", "prerequisite"
- INFLUENCES: "affects", "impacts", "leads to", "causes", "drives"
- CONSTRAINS: "limits", "restricts", "constrains", "barriers", "obstacles"
- CONTESTED-BY: "opposes", "resists", "challenges", "conflicts"
- RELATES-TO: (qualquer associação explícita)

```
CES_edges = (arestas com indicador linguístico presente) / (total de arestas)
```

**CES composto:**

```
CES = 0,4 × CES_nodes + 0,6 × CES_edges
```

Peso maior nas arestas porque a fabricação de relações (F3) é a falha mais grave.

**Threshold:**

- CES ≥ 0,70 → cadeia bem fundamentada
- CES 0,40–0,69 → cadeia parcialmente fundamentada (flag ⚠️)
- CES < 0,40 → cadeia possivelmente fabricada (flag ❌)

**Viabilidade:** ⚠️ Média — Nível 1 é trivial. Nível 2 requer dicionário de indicadores linguísticos por tipo de relação (não é complexo mas requer curadoria). Falsos negativos são prováveis (texto pode expressar relação sem usar palavras-chave padrão).

**Mitigação de falsos negativos:** Aceitar score baixo em CES_edges como warning, não como bloqueio. O campo `note` (memo) pode ser usado como evidência complementar — se o note descreve o mecanismo, a cadeia está justificada.

---

#### M4: Guideline Compliance Checks (GCC)

**O que mede:** Conformidade com regras programaticamente verificáveis do template.

**Checks automáticos derivados de `social_acceptance.synt`:**

| Check                              | Regra do template                                                     | Implementação                                                          |
| ---------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| GCC-1: Note word count             | `note`: "Maximum 25 words (up to 50 words if flagged *complex*)"      | `len(note.split()) <= 25` ou `<= 50` se contém `*complex*`             |
| GCC-2: Text self-containment       | `text`: "Each excerpt must include: subject + verb + object"          | Heurística: detectar presença de sujeito+verbo via POS tagging simples |
| GCC-3: Text sentence count         | `text`: "1-3 sentences"                                               | `1 <= count_sentences(text) <= 3`                                      |
| GCC-4: Chain arity                 | `chain`: "ARITY >= 2"                                                 | Compilador já verifica (≥2 nós)                                        |
| GCC-5: Chain direction             | `chain`: guidelines de direção linguística                            | Parcialmente verificável (ver M3)                                      |
| GCC-6: Ontology description length | `ontology_description`: "40-80 words"                                 | `40 <= len(desc.split()) <= 80`                                        |
| GCC-7: Reasoning length            | `reasoning`: "40-60 words"                                            | `40 <= len(reasoning.split()) <= 60`                                   |
| GCC-8: Factor naming               | `chain`: "Use singular substantive forms, underscores for multi-word" | Regex: `^[A-Z][a-z_]+(_[A-Z][a-z_]+)*$`                                |

**Viabilidade:** ✅ Alta para GCC-1, GCC-3, GCC-4, GCC-6, GCC-7, GCC-8 (regex e contagem). ⚠️ Média para GCC-2 (requer parsing linguístico). GCC-5 depende de M3.

---

#### M5: Code Reuse Rate (CRR)

**O que mede:** Proporção de codes no output que já existiam no code_index do projeto.

**Cálculo:**

```
CRR = (codes do output que existem no code_index) / (total de codes no output)
```

**Contexto:** O template diz "Target: fewer than 150 unique factors across the full corpus" e o prompt diz "prefer existing codes". CRR baixo indica proliferação de conceitos (F6).

**Threshold:**

- Em projetos maduros (code_index > 50 codes): CRR ≥ 0,60
- Em projetos novos (code_index < 20 codes): CRR sem threshold (esperado que muitos sejam novos)

**Nota:** CRR alto demais (> 0,95) em projetos maduros pode indicar codificação forçada — o LLM está encaixando o texto em codes existentes sem que o texto justifique.

**Viabilidade:** ✅ Alta — code_index já está disponível no ctx. Cálculo trivial.

---

#### M6: Optional Field Fill Rate (OFFR)

**O que mede:** Proporção de campos opcionais preenchidos no output.

**Cálculo:**

```
OFFR = (campos opcionais preenchidos) / (total de campos opcionais disponíveis)
```

**Contexto:** OFFR muito alto (> 0,90) em modo abstract/item sugere codificação forçada (F2) — o LLM preenche tudo mesmo quando o texto não fornece informação suficiente. OFFR moderado (0,40–0,70) é esperado.

**Campos opcionais em `social_acceptance.synt`:** `topic`, `aspect`, `dimension`, `confidence`, `reasoning`, `rgt_element_a`, `rgt_element_b`, `theoretical_significance`.

**Threshold (para item/abstract mode):**

- OFFR 0,30–0,80 → range normal
- OFFR > 0,90 → flag ⚠️ possível codificação forçada
- OFFR < 0,20 → flag ⚠️ output muito esparso

**Nota:** Em modo ontology, OFFR alto é esperado e desejado (o LLM tem contexto semântico completo).

**Viabilidade:** ✅ Alta — os field_specs e o output parsed estão disponíveis.

---

#### M7: RELATES-TO Frequency (RTF)

**O que mede:** Proporção de relações RELATES-TO no total de relações CHAIN.

**Cálculo:**

```
RTF = (arestas RELATES-TO) / (total de arestas CHAIN)
```

**Contexto:** O template diz explicitamente: "RELATES-TO: Generic significant association. LAST RESORT, use in less than 5% of relations." RTF > 0,05 indica que o LLM está usando o fallback em excesso.

**Threshold:**

- RTF ≤ 0,05 → conforme o template ✅
- RTF 0,05–0,15 → acima do tolerado (warning ⚠️)
- RTF > 0,15 → violação significativa (flag ❌)

**Viabilidade:** ✅ Alta — parsing da chain syntax já existe no compilador.

---

#### M8: Annotation Confidence Score (ACS)

**O que mede:** Score composto indicando confiança global na qualidade de uma anotação ITEM.

**Cálculo:**

```
ACS = w1×QFS + w2×CGR + w3×CES + w4×GCC_rate + w5×(1-RTF)
```

Pesos sugeridos:

- w1 = 0,30 (Quotation Fidelity — a âncora empírica)
- w2 = 0,20 (Code Grounding)
- w3 = 0,25 (Chain Evidence)
- w4 = 0,15 (Guideline Compliance)
- w5 = 0,10 (RELATES-TO penalty)

**Threshold:**

- ACS ≥ 0,75 → anotação confiável
- ACS 0,50–0,74 → anotação aceitável com ressalvas
- ACS < 0,50 → anotação requer revisão humana

**Viabilidade:** ✅ Alta — é computado a partir das demais métricas. Sem dependência adicional.

---

## 4. Análise de Viabilidade

### 4.1 Matriz de viabilidade

| Métrica   | Dados necessários                          | Disponível no pipeline               | Dependência externa                         | Complexidade | Viabilidade |
| --------- | ------------------------------------------ | ------------------------------------ | ------------------------------------------- | ------------ | ----------- |
| M1 (QFS)  | texto fonte + output text                  | ✅ Sim (prompt input + parsed output) | Nenhuma                                     | Baixa        | ✅ Alta      |
| M2 (CGR)  | codes no output + texto fonte + code_index | ✅ Sim                                | Nenhuma                                     | Baixa        | ✅ Alta      |
| M3 (CES)  | chain no output + texto fonte              | ✅ Sim                                | Dicionário de indicadores linguísticos      | Média        | ⚠️ Média    |
| M4 (GCC)  | output parsed + template field_specs       | ✅ Sim                                | Nenhuma (GCC-2 requer POS tagger, opcional) | Baixa-Média  | ✅ Alta      |
| M5 (CRR)  | codes no output + code_index               | ✅ Sim                                | Nenhuma                                     | Baixa        | ✅ Alta      |
| M6 (OFFR) | output parsed + field_specs                | ✅ Sim                                | Nenhuma                                     | Baixa        | ✅ Alta      |
| M7 (RTF)  | chain relations no output                  | ✅ Sim                                | Nenhuma                                     | Baixa        | ✅ Alta      |
| M8 (ACS)  | M1-M7                                      | Derivado                             | Nenhuma                                     | Baixa        | ✅ Alta      |

### 4.2 Dados já disponíveis no pipeline

O pipeline atual (`prompt_builder.py` → `llm_client.py` → `validator.py`) já possui todos os dados necessários para M1-M8:

1. **Texto fonte:** passado como argumento para `build_item_prompt(ctx, bibref, text)` e `build_abstract_prompt(ctx, bibref, abstract)` — disponível como parâmetro de entrada
2. **Output parsed:** `synesis.load()` já parseia o output em estruturas `ItemNode`, `SourceNode`, `OntologyNode` com campos acessíveis
3. **code_index:** disponível em `ctx["code_index"]`
4. **field_specs:** disponível em `ctx["field_specs"]` com tipo, scope, guidelines, required/optional
5. **Chain parsed:** o compilador já parseia chains em listas de nós e arestas com relation types

**Nenhuma dependência externa é necessária** para implementar M1-M2, M4-M8. Apenas M3 (Chain Evidence Score nível 2) requer curadoria de dicionário de indicadores linguísticos por tipo de relação.

### 4.3 Onde inserir no pipeline

Duas opções arquiteturais:

**Opção A: Pós-validação (depois de `validate_and_fix`)**

```
LLM output → validate_and_fix() → synesis.load() OK → compute_metrics() → output + metrics
```

- Vantagem: não interfere no pipeline existente
- Vantagem: output já está parsed pelo compilador
- Desvantagem: métricas são informativas (não bloqueiam output ruim)

**Opção B: Gate com re-geração (antes do output final)**

```
LLM output → validate_and_fix() → compute_metrics() → ACS < threshold? → re-generate → output
```

- Vantagem: output final tem qualidade garantida
- Desvantagem: custo adicional de LLM (re-geração) e complexidade
- Risco: loop infinito se o LLM consistentemente gera output de baixa qualidade para um texto

**Recomendação:** Opção A para a primeira implementação — métricas como observabilidade, não como gate. Opção B como evolução futura, com re-geração limitada a 1 tentativa adicional e apenas para ACS < 0,50.

---

## 5. Pontos de Inserção no Pipeline

### 5.1 Novo módulo: `quality.py`

Módulo responsável por calcular todas as métricas. Recebe o output parsed e o contexto.

```
synesis_coder/
├── quality.py          ← NOVO: cálculo de métricas M1-M8
├── validator.py         (existente: validação sintática)
├── prompt_builder.py    (existente: construção de prompts)
├── llm_client.py        (existente: chamadas LLM)
└── modes/
    ├── item_mode.py     ← modificar: chamar quality.py após validate_and_fix
    ├── abstract_mode.py ← modificar: chamar quality.py após validate_and_fix
    ├── document_mode.py ← modificar: chamar quality.py após validate_and_fix
    └── ontology_mode.py ← modificar: chamar quality.py (subset de métricas)
```

### 5.2 Interface proposta

```python
# quality.py (interface conceitual)

def compute_item_metrics(
    parsed_output,      # ItemNode(s) do compilador
    source_text: str,   # texto/abstract original
    ctx: dict,          # contexto do projeto (code_index, field_specs, etc.)
) -> QualityReport:
    """Calcula M1-M8 para um conjunto de ITEMs gerados."""

class QualityReport:
    qfs: float          # M1: Quotation Fidelity Score
    cgr: float          # M2: Code Grounding Rate
    ces: float          # M3: Chain Evidence Score
    gcc: dict           # M4: Guideline Compliance Checks (check_id → pass/fail)
    crr: float          # M5: Code Reuse Rate
    offr: float         # M6: Optional Field Fill Rate
    rtf: float          # M7: RELATES-TO Frequency
    acs: float          # M8: Annotation Confidence Score (composto)
    flags: list[str]    # warnings e erros detectados
```

### 5.3 Output de métricas

Duas formas de output:

**1. Header no arquivo .syn (modo verbose)**

```
# synesis-coder item
# projeto: social_acceptance
# bibref: @author2024
# tokens: in 2,180 | out 550 | total 2,730 | calls 2
# quality: ACS=0.82 | QFS=0.91 | CGR=0.85 | CES=0.74 | CRR=0.67 | RTF=0.02
# flags: GCC-1 WARN note exceeds 25 words (32)
# OK
```

**2. Relatório de batch (modo abstract)**

```
# === BATCH QUALITY REPORT ===
# Items gerados: 127
# ACS médio: 0.78
# QFS médio: 0.88 | < 0.60 (fabricação): 3 items
# CGR médio: 0.82 | codes novos não fundamentados: 7
# CES médio: 0.71 | chains sem evidência: 12
# RELATES-TO global: 3.2% (dentro do limite de 5%)
# Code reuse: 64% (41 existentes / 64 total)
# Items com ACS < 0.50 (revisão recomendada): 5
```

### 5.4 Integração com modos existentes

#### Item mode (`item_mode.py`)

Ponto de inserção: após `validate_and_fix()`, antes do output final.

```
# Fluxo atual:
raw = client.call(messages)
output, ok = validate_and_fix(raw, ctx, client)
# → inserir aqui:
report = compute_item_metrics(output, text, ctx)
# → incluir report no header verbose
```

Impacto: mínimo — 1 chamada de função adicional, sem I/O.

#### Abstract mode (`abstract_mode.py`)

Ponto de inserção: após cada `validate_and_fix_async()`, acumular métricas por item. Ao final do batch, gerar relatório agregado.

Impacto: acumular métricas por item adiciona overhead negligível. O relatório de batch é gerado uma vez no final.

#### Document mode (`document_mode.py`)

Mesmo padrão do abstract mode. Nota: a deduplicação (`merge_and_dedup`) deve ocorrer antes do cálculo de métricas (não faz sentido avaliar qualidade de items duplicados que serão removidos).

#### Ontology mode (`ontology_mode.py`)

Subset de métricas: apenas M4 (GCC-6, GCC-7), M5 (CRR para topics), e M6 (OFFR). Não se aplicam: M1 (sem citação direta), M3 (sem chain), M7 (sem chain).

---

## 6. Plano de Ação

### 6.1 Fases de implementação

| Fase  | Escopo                        | Métricas                                               | Prioridade |
| ----- | ----------------------------- | ------------------------------------------------------ | ---------- |
| **1** | Métricas automáticas triviais | M1-QFS, M5-CRR, M6-OFFR, M7-RTF                        | Alta       |
| **2** | Métricas de fundamentação     | M2-CGR, M4-GCC (checks simples)                        | Alta       |
| **3** | Score composto + output       | M8-ACS, headers, relatório de batch                    | Média      |
| **4** | Chain evidence                | M3-CES (nível 1: nós), GCC avançados                   | Média      |
| **5** | Chain evidence avançado       | M3-CES (nível 2: arestas com indicadores linguísticos) | Baixa      |

### 6.2 Detalhamento por fase

#### Fase 1 — Métricas triviais

**Arquivos:** Criar `quality.py`. Não modifica nenhum arquivo existente.

**M1 (QFS):** Usar `difflib.SequenceMatcher` para calcular similaridade entre campo `text` do ITEM e o texto fonte. Normalizar por comprimento do campo `text`.

**M5 (CRR):** Comparar codes extraídos do output com `ctx["code_index"]["codes"]`.

**M6 (OFFR):** Iterar `ctx["field_specs"]` para campos OPTIONAL, verificar presença no output parsed.

**M7 (RTF):** Contar relações RELATES-TO vs total de relações no output.

Dependências: apenas stdlib Python (`difflib`, `re`).

#### Fase 2 — Métricas de fundamentação

**M2 (CGR):** Para cada code no output, tokenizar por `_`, verificar presença de cada token no texto fonte (case-insensitive). Score = proporção de tokens presentes.

**M4 (GCC):** Implementar checks GCC-1 (note word count), GCC-3 (text sentence count), GCC-6 (ontology_description length), GCC-7 (reasoning length), GCC-8 (factor naming regex). Ler limites das guidelines via `field_specs[field].guidelines` (parsear números).

Dependências: apenas stdlib Python (`re`).

#### Fase 3 — Score composto + output

**M8 (ACS):** Combinar M1-M7 com pesos definidos.

**Output:** Adicionar header de qualidade no output verbose. Adicionar relatório de batch no summary do abstract mode.

**Arquivos modificados:**

- `modes/item_mode.py` — chamar `compute_item_metrics()` após validação
- `modes/abstract_mode.py` — acumular métricas, gerar relatório
- `modes/document_mode.py` — mesmo padrão
- `modes/ontology_mode.py` — subset de métricas

#### Fase 4 — Chain evidence (nós)

**M3-CES nível 1:** Para cada nó da chain, aplicar mesma lógica de M2 (token matching). Simples extensão do M2 para o contexto de chains.

#### Fase 5 — Chain evidence avançado

**M3-CES nível 2:** Criar dicionário de indicadores linguísticos por tipo de relação, derivado das guidelines do template. Verificar presença no texto fonte.

Dicionário inicial (derivado de `social_acceptance.synt` linhas 125-128):

```python
RELATION_INDICATORS = {
    "ENABLES": ["enables", "allows", "permits", "necessary", "prerequisite", 
                "needed for", "required for", "in order to"],
    "INFLUENCES": ["affects", "impacts", "leads to", "causes", "drives", 
                   "results in", "increases", "decreases", "determines"],
    "CONSTRAINS": ["limits", "restricts", "constrains", "barrier", "obstacle",
                   "reduces", "hinders", "prevents", "impedes"],
    "CONTESTED-BY": ["opposes", "resists", "challenges", "conflicts", 
                     "disputed", "controversial", "contested"],
    "RELATES-TO": []  # Genérico — não requer indicador específico
}
```

---

## 7. O que NÃO Implementar

| Proposta descartada                                                | Motivo                                                                                                                                                          |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Validação semântica via LLM** (segundo LLM avaliando o primeiro) | Custo proibitivo (dobra chamadas LLM), circularidade (LLM avaliando LLM), complexidade de integração                                                            |
| **Gate bloqueante com re-geração automática** (na primeira versão) | Risco de loop, custo adicional, pipeline ainda não tem dados para calibrar thresholds. Implementar como observabilidade primeiro.                               |
| **Embedding similarity para M1**                                   | Adiciona dependência de modelo de embeddings (sentenceTransformers ou similar). SequenceMatcher é suficiente para detectar fabricação vs. citação literal.      |
| **POS tagging para GCC-2**                                         | Dependência de spaCy ou similar. Heurística simples (presença de verbo via regex) é suficiente como proxy.                                                      |
| **Métricas de inter-annotator agreement**                          | Requer múltiplas anotações do mesmo texto (não disponível no pipeline atual). Relevante para validação de pesquisa, não para controle de qualidade de produção. |
| **Métricas em tempo real no prompt** (feedback loop)               | Modificar o prompt com métricas de qualidade anteriores criaria instabilidade e viés. As métricas devem ser pós-processamento.                                  |
| **Comparação com anotações humanas**                               | Não há ground truth de anotações humanas no pipeline. Se disponível no futuro, seria a métrica mais valiosa (mas fora do escopo atual).                         |

---

## 8. Estimativa de Impacto

### 8.1 Overhead de performance

| Fase           | Overhead estimado por ITEM | Operações                 |
| -------------- | -------------------------- | ------------------------- |
| 1 (M1, M5-M7)  | < 1ms                      | String matching, contagem |
| 2 (M2, M4)     | < 2ms                      | Tokenização, regex        |
| 3 (M8, output) | < 1ms                      | Aritmética, formatação    |
| 4-5 (M3)       | < 5ms                      | Matching expandido        |

**Total: < 10ms por ITEM** — negligível comparado com a latência do LLM (1-5 segundos por chamada).

### 8.2 Ganho esperado

| Antes                                      | Depois                                           |
| ------------------------------------------ | ------------------------------------------------ |
| "Anotação sintaticamente válida" (binário) | Score contínuo de confiança (ACS 0.0–1.0)        |
| Sem detecção de alucinação                 | CGR e CES flaggam codes/chains sem fundamentação |
| Sem detecção de codificação forçada        | OFFR flagga preenchimento excessivo de opcionais |
| Sem conformidade com guidelines            | GCC verifica regras quantificáveis do template   |
| Revisão humana "às cegas"                  | Revisão dirigida: foco nos items com ACS < 0.50  |

### 8.3 Compatibilidade

- **Sem breaking changes:** quality.py é um módulo novo, chamado opcionalmente
- **Sem dependência externa:** apenas stdlib Python
- **Retrocompatível:** output sem métricas permanece idêntico se flag `--quality` não for passada
- **Template-agnóstico:** métricas leem field_specs do template, não hardcodam regras de `social_acceptance.synt`

---

*Estudo de viabilidade concluído em 10 de abril de 2026. Próximo passo: implementar Fase 1 (M1, M5, M6, M7) em `quality.py`.*
