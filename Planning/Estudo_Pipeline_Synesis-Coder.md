---
# Estudo de Viabilidade — Pipeline em Etapas para Geração de Anotações no Synesis-Coder

**Data:** 2026-04-22 **Relação com estudo anterior:** Alternativa estrutural ao estudo `estudo_prompts_ciclos.md` (v2, 2026-04-22). Enquanto aquele propõe **ciclos conversacionais** (refinamento iterativo da mesma saída), este propõe **etapas cascateadas** (transformações entre tipos de saída distintos). **Escopo:** Avaliar a viabilidade de decompor a geração de anotações (`item`, `abstract`, `document`) em duas ou três etapas com responsabilidades distintas e artefatos intermediários estruturados. **Objetivo primário:** Máxima fidelidade ao texto-fonte e auditabilidade da cadeia de decisão. Custo e latência são secundários.

---

## 1. Motivação e Contexto

### 1.1 Observação-chave: o pipeline de `ontology` já opera em etapas

O `ontology` mode do synesis-coder não é um monólito single-shot. Ele é, na prática, um pipeline de duas camadas:

**Camada determinística (pré-processamento, sem LLM):**

`_build_semantic_ctx(code, ctx)` em `ontology_mode.py` coleta, para cada código:

- Frequência (nº de ITEMs que o usam)
- Fontes distintas (nº de SOURCEs)
- Relações em campos CHAIN (até 15)
- Co-ocorrências com outros códigos (até 20)
- Exemplos representativos (até 3 trechos de QUOTATION/NOTE)

Esse dicionário estruturado é **determinístico, rastreável, inspecionável**. Ele é produto exclusivo de leitura do `.syn` já compilado.

**Camada LLM (síntese, com acesso à camada determinística):**

`build_ontology_prompt(ctx, code, semantic_ctx)` monta o prompt com o dicionário como input. O LLM apenas transforma esses insumos em definição semântica no formato ONTOLOGY. Não decide o que é relevante — decide como **redigir** a partir do relevante já coletado.

**Consequência empírica observável:** as entradas ONTOLOGY geradas pelo synesis-coder têm fidelidade notavelmente alta ao corpus precisamente porque o LLM recebe evidências concretas do corpus (os 3 exemplos de QUOTATION funcionam como *verbatim anchors*), não apenas instruções abstratas.

### 1.2 O contraste com os modos `item`, `abstract`, `document`

Nos três modos de codificação, o pipeline é **single-stage**:

```
text + ctx (template + code_index + topic_index)
        ↓
build_item_prompt(ctx, bibref, text)
        ↓
LLM: lê texto + aplica TODAS as regras + decide campos + produz Synesis syntax
        ↓
validator: sintaxe → (correção até 3×)
        ↓
output
```

O LLM faz, em uma única passagem, todo o trabalho cognitivo:

1. Ler e compreender o texto
2. Identificar trechos quotáveis
3. Formular comentário interpretativo
4. Selecionar/criar códigos
5. Extrair relações causais com direcionalidade
6. Atribuir valores a campos ORDERED/ENUMERATED/SCALE
7. Formatar como Synesis syntax sintaticamente válida

O validator cobre apenas (7) — sintaxe. Erros em (1)–(6) — semântica — **não são detectáveis** pelo compilador e portanto não disparam o loop de correção.

### 1.3 A proposta

Aplicar aos modos de codificação o mesmo padrão que `ontology` já usa: **separar extração de síntese**.

**Etapa 1 (EXTRAÇÃO):** LLM lê o texto e produz um **artefato estruturado** — JSON com trechos candidatos, códigos candidatos, cadeias candidatas, ratings candidatos, cada um acompanhado de **evidência verbatim** do texto-fonte. Não emite Synesis syntax.

**Etapa 2 (SÍNTESE):** LLM recebe o artefato estruturado + texto original + regras do template, e **converte** o artefato em um bloco ITEM Synesis válido. Não toma novas decisões semânticas — aplica regras de formatação, ARITY, BUNDLE, generalização hierárquica, e resolve conflitos.

O validator continua como hoje, executado apenas após Etapa 2.

---

## 2. Comparação com Ciclos Conversacionais

Esta é a diferença estrutural central em relação ao estudo de ciclos:

| Dimensão                       | Ciclos conversacionais (estudo anterior) | Etapas cascateadas (este estudo)                     |
| ------------------------------ | ---------------------------------------- | ---------------------------------------------------- |
| **Tipo de saída por chamada**  | Sempre Synesis syntax (refinada)         | Diferentes: JSON estruturado → Synesis syntax        |
| **Natureza do progresso**      | Iterativo (mesmo output, melhorado)      | Transformacional (output distinto em cada etapa)     |
| **Artefato intermediário**     | Turnos de conversa (texto livre)         | JSON tipado e validável                              |
| **Auditabilidade**             | Turnos legíveis, mas desestruturados     | Artefato estruturado, campo a campo                  |
| **Cacheabilidade**             | Cache de prefixo cresce linearmente      | Etapa 1 cacheável independentemente da Etapa 2       |
| **Iteração em prompts**        | Mudar prompt = rerodar tudo              | Mudar prompt da Etapa 2 = reusar Etapa 1             |
| **Inspeção humana**            | Difícil intervir entre turnos            | Artefato da Etapa 1 pode ser editado manualmente     |
| **Validação intermediária**    | Impossível (texto livre)                 | Possível (JSON contra schema)                        |
| **Risco de anchoring**         | Alto (modelo vê própria decisão)         | Baixo (Etapa 2 transforma, não revisa)               |
| **Risco de perda de contexto** | Baixo (texto sempre visível)             | Médio (Etapa 2 depende da qualidade da Etapa 1)      |
| **Precedente no codebase**     | Nenhum                                   | Sim — `ontology_mode` + `finetune_mode` (Camada 1/2) |

**Observação crítica:** etapas cascateadas e ciclos conversacionais são **ortogonais, não mutuamente exclusivos**. Poder-se-ia ter ciclos dentro da Etapa 1 (extração iterativa) ou dentro da Etapa 2 (síntese com auto-revisão). Este estudo trata apenas da estrutura em etapas; ciclos dentro de etapa são extensão futura.

---

## 3. Desenho do Artefato Intermediário

O sucesso da abordagem depende fundamentalmente do desenho do JSON entregue pela Etapa 1. Esse schema precisa:

1. **Ser derivável do template** — diferentes templates têm campos diferentes; o schema não pode ser hardcoded.
2. **Forçar evidência verbatim** — cada extração deve vir acompanhada do trecho do texto que a sustenta.
3. **Admitir incerteza** — campo `confidence` por item permite à Etapa 2 desqualificar extrações fracas.
4. **Ser estruturalmente validável** — JSON Schema inferido do template.

### 3.1 Schema proposto (derivado do template)

```json
{
  "source_text": "<texto original, para referência da Etapa 2>",
  "bibref": "@authorYear",
  "fields": {
    "text": {
      "type": "QUOTATION",
      "candidates": [
        {
          "content": "trecho verbatim do texto",
          "position_hint": "paragraph 2, sentence 1",
          "relevance": "high"
        }
      ]
    },
    "note": {
      "type": "MEMO",
      "candidates": [
        {
          "content": "comentário interpretativo proposto",
          "anchor_text": "trecho do texto que fundamenta",
          "confidence": "medium"
        }
      ]
    },
    "code": {
      "type": "CODE",
      "candidates": [
        {
          "code_name": "trust",
          "exists_in_project": true,
          "anchor_text": "verbatim quote",
          "justification": "..."
        },
        {
          "code_name": "institutional_legitimacy",
          "exists_in_project": false,
          "anchor_text": "verbatim quote",
          "justification": "...",
          "proposed_as_new": true
        }
      ]
    },
    "chain": {
      "type": "CHAIN",
      "candidates": [
        {
          "subject": "trust",
          "relation": "REDUCES",
          "object": "opposition",
          "anchor_text": "verbatim sentence(s)",
          "directionality_cue": "reduces",
          "confidence": "high"
        }
      ],
      "available_relations": ["ENABLES", "INFLUENCES", "CONSTRAINS", "REDUCES", ...]
    },
    "aspect": {
      "type": "ORDERED",
      "candidate": {
        "value": 7,
        "label": "Trust & Legitimacy",
        "justification": "texto discute confiança institucional extensivamente",
        "anchor_text": "..."
      }
    },
    "scale": {
      "type": "SCALE",
      "range": [0, 5],
      "candidate": {
        "value": 3,
        "justification": "..."
      }
    }
  },
  "item_segmentation_hint": "single_item | multiple_items",
  "segmentation_justification": "..."
}
```

**Observações sobre o schema:**

- A chave `fields` é mapeada dinamicamente a partir de `ctx["field_specs"][Scope.ITEM]`. Nada hardcoded.
- Campos `OPTIONAL` podem ter `candidates: []`.
- Campos `REQUIRED` devem ter pelo menos um candidato.
- `anchor_text` é exigido sempre que tecnicamente aplicável (não aplicável a SCALE, por ex.).
- `item_segmentation_hint` captura a decisão "sequencial vs paralelo" explicitada nas GUIDELINES do `chain` de social_acceptance.

### 3.2 Formato de Stage 1: JSON estruturado vs. bullet markdown

Duas opções práticas:

**(A) JSON estrito via response format / tool use**

- Anthropic: forçar via `tools=[extraction_tool]`
- OpenAI-compat: `response_format: json_schema`
- Vantagem: parse determinístico, zero risco de malformação
- Desvantagem: alguns backends open-source não suportam bem

**(B) Bullet markdown estruturado**

- Modelo responde em formato livre mas delimitado por headers fixos (`## CAMPO: text`, `- ANCHOR: "..."`, etc.)
- Parser regex converte para JSON interno
- Vantagem: universal entre backends; mais amigável ao modelo
- Desvantagem: requer parser; erros de formatação possíveis

**Recomendação:** A para `SYNESIS_CODER_BACKEND=anthropic` (tool use nativo); B para backend `openai` com fallback para JSON mode quando disponível. O módulo `stage1_extractor.py` seria agnóstico ao formato, consumindo dict Python após parsing.

---

## 4. Arquitetura Proposta

### 4.1 Fluxo em duas etapas

```
┌────────────────────────────────────────────────────────────────┐
│                  ETAPA 0 — COMPILAÇÃO (determinística)          │
│  load_project()                                                  │
│    → ctx (template, field_specs, code_index, topic_index, ...) │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│            ETAPA 1 — EXTRAÇÃO (LLM, output = JSON)               │
│                                                                  │
│  Input:                                                          │
│    - text                                                        │
│    - bibref                                                      │
│    - ctx["field_specs"] (com GUIDELINES integrais)              │
│    - ctx["code_index"], ctx["topic_index"]                      │
│                                                                  │
│  Prompt: "Extract candidates with verbatim evidence.             │
│           Do NOT produce Synesis syntax."                        │
│                                                                  │
│  Output: ExtractionArtifact (dict ou JSON)                      │
│                                                                  │
│  Validação: JSON Schema contra template (etapa determinística)  │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│        ETAPA 2 — SÍNTESE (LLM, output = Synesis syntax)          │
│                                                                  │
│  Input:                                                          │
│    - ExtractionArtifact                                          │
│    - text original (para verificação de fidelidade)             │
│    - ctx (template rules: ARITY, BUNDLE, formato de saída)      │
│                                                                  │
│  Prompt: "Synthesize ITEM block from artifact.                   │
│           Enforce template rules. Resolve conflicts.             │
│           Verify each field matches anchor_text in source."      │
│                                                                  │
│  Output: ITEM block(s) Synesis                                  │
│                                                                  │
│  Validação: compiler (synesis.load) + correction loop (3×)      │
└────────────────────────────────────────────────────────────────┘
```

### 4.2 Responsabilidades por etapa

| Responsabilidade                             | Etapa 0 | Etapa 1    | Etapa 2                      | Validator |
| -------------------------------------------- | ------- | ---------- | ---------------------------- | --------- |
| Carregar template e project context          | ✓       |            |                              |           |
| Ler e compreender o texto                    |         | ✓          | ⚠ (re-leitura verificatória) |           |
| Identificar trechos quotáveis                |         | ✓          |                              |           |
| Formular comentários interpretativos         |         | ✓          |                              |           |
| Selecionar códigos existentes / propor novos |         | ✓          |                              |           |
| Extrair cadeias causais com direcionalidade  |         | ✓          |                              |           |
| Atribuir valores ORDERED/ENUMERATED/SCALE    |         | ✓          |                              |           |
| Aplicar regras de ARITY/BUNDLE               |         |            | ✓                            |           |
| Decidir segmentação em múltiplos ITEMs       |         | ⚠ (sugere) | ✓ (decide)                   |           |
| Aplicar generalização hierárquica            |         |            | ✓                            |           |
| Formatar como Synesis syntax                 |         |            | ✓                            |           |
| Validar sintaxe                              |         |            |                              | ✓         |
| Correção iterativa                           |         |            |                              | ✓         |

**Observação:** Etapa 2 relê o texto-fonte deliberadamente, não para refazer a extração, mas para **auditar** se cada `anchor_text` da Etapa 1 realmente existe no texto (defesa contra alucinação). Essa é uma função de verificação, não de re-extração.

### 4.3 Suporte para modo `document`

Em `document` mode, cada SOURCE pendente seria processado por esse pipeline de duas etapas. O `AsyncLLMClient` já existente suporta paralelismo. Cada item em concorrência executaria Etapa 1 → Etapa 2 serialmente dentro de si, paralelamente a outros items.

### 4.4 Suporte para modo `abstract`

Abstract requer um ajuste estrutural: a Etapa 1 precisa extrair dos **ITEMs já anotados** (não do texto bruto) os elementos-chave para o resumo. A Etapa 2 sintetiza o abstract a partir desses elementos. Pode ser tratado como caso particular — a Etapa 1 vira uma função de sumarização estruturada.

### 4.5 Modo `item` (interativo VSCode)

Como o pipeline envolve **duas chamadas LLM sequenciais**, a latência esperada (cf. §6) inviabiliza modo `item` interativo — tal qual ocorreu com ciclos. Recomendação: ativável apenas em `document`, `abstract`, `ontology` (este último já em pipeline).

---

## 5. Viabilidade Técnica

### 5.1 Mudança no código — escopo estimado

Sem alterar gramática, compilador, ou outras partes do ecossistema:

| Módulo                                 | Natureza da mudança                                     | LOC aprox.         |
| -------------------------------------- | ------------------------------------------------------- | ------------------ |
| `synesis_coder/extractor.py` (novo)    | Etapa 1: prompt + chamada + parse + validação de schema | ~300               |
| `synesis_coder/synthesizer.py` (novo)  | Etapa 2: prompt + chamada + verificação de anchors      | ~200               |
| `synesis_coder/prompt_builder.py`      | `build_extraction_prompt()`, `build_synthesis_prompt()` | ~250               |
| `synesis_coder/modes/item_mode.py`     | Orquestração condicional (flag de pipeline)             | ~40                |
| `synesis_coder/modes/document_mode.py` | Orquestração condicional                                | ~40                |
| `synesis_coder/modes/abstract_mode.py` | Adaptação do schema de extração                         | ~60                |
| `synesis_coder/cli.py`                 | Flag `--pipeline [single                                | staged]` + env var |
| Testes                                 | Cobertura de extractor/synthesizer + integração         | ~500               |

Total: **~1400 LOC novas**, nenhuma remoção. O modo single-shot atual permanece intocado como default.

### 5.2 Retrocompatibilidade

- Default: `SYNESIS_CODER_PIPELINE=single` — comportamento atual preservado.
- Staged pipeline ativado via `SYNESIS_CODER_PIPELINE=staged` ou `--pipeline staged`.
- Templates não precisam de nenhuma modificação para o modo staged (o schema de extração é derivado automaticamente de `field_specs`).

### 5.3 Suporte a backends

- **Anthropic (claude-opus-4-x, claude-sonnet-4-x):** tool use para Etapa 1 (schema estrito); texto para Etapa 2. Thinking budget aplicável a ambas as etapas; Etapa 1 provavelmente se beneficia mais de thinking extensivo.
- **OpenAI-compatível:** `response_format: json_schema` para Etapa 1 (quando suportado); fallback para markdown estruturado quando não.
- **Cache:** a Etapa 1 cacheia o system prompt (field specs + code_index + topic_index). A Etapa 2 cacheia o system prompt (regras de formatação Synesis). Os prompts das duas etapas são independentes — **não há interferência de cache entre elas**.

### 5.4 Integração com extended thinking

Thinking na Etapa 1 melhora qualidade de extração (raciocínio sobre quais cadeias causais estão de fato no texto). Thinking na Etapa 2 melhora aplicação de regras (ARITY, BUNDLE, segmentação). Pode-se usar budgets diferentes:

- Etapa 1: `thinking_budget=16000` (análise profunda do texto)
- Etapa 2: `thinking_budget=4000` (apenas formatação e consolidação)

Controle via `SYNESIS_CODER_STAGE1_THINKING_BUDGET` / `SYNESIS_CODER_STAGE2_THINKING_BUDGET`, preservando o `SYNESIS_CODER_THINKING_BUDGET` como default comum.

---

## 6. Custo e Latência

### 6.1 Custo por item (Opus-4-7)

| Componente                     | Single-shot atual | Staged proposto                      |
| ------------------------------ | ----------------- | ------------------------------------ |
| System prompt input (cacheado) | 4.000 tok         | 3.000 (E1) + 2.500 (E2)              |
| User message input             | 500 tok           | 500 (E1) + ~1.200 (E2, com artifact) |
| Output (inclui thinking)       | ~5.500 tok        | ~3.000 (E1, JSON) + ~3.500 (E2)      |
| **Custo estimado**             | **~$0.14**        | **~$0.22**                           |

Aproximadamente **1.6× o baseline** — mais barato que os ciclos conversacionais (~2×) porque não há crescimento linear de prefixo cacheado ao longo de turnos.

Para batch de 500 itens: ~$110 (vs ~$70 atual, ~$140 ciclos). Absorvível em contexto de pesquisa acadêmica.

### 6.2 Latência

| Modo                     | Latência por item |
| ------------------------ | ----------------- |
| Single-shot atual        | 8-15s             |
| Staged (E1 + E2 serial)  | **~18-28s**       |
| 3 ciclos conversacionais | 30-45s            |

Latência staged fica aproximadamente **2× do baseline** — melhor que ciclos, mas ainda inviável para modo interativo `item`.

Para `document` com `--concurrent=5` em 50 abstracts:

- Single-shot: ~2 min
- Staged: **~4 min**
- 3 ciclos: 5-7 min

### 6.3 Rate limits

Staged dobra o nº de chamadas por item, mas cada chamada é menor (sem crescimento de prefixo). RPM atual (`MAX_RPM=50`) absorve. TPM pode ser mais restritivo se Etapa 1 receber textos longos: considerar `MAX_INPUT_TPM` bump para 60000 em corpora grandes.

---

## 7. Análise de Precisão

### 7.1 Hipótese principal

**H1:** A decomposição extração → síntese produz saídas Synesis mais fiéis ao texto-fonte porque (a) cada etapa enfrenta um problema cognitivo mais restrito, (b) a obrigatoriedade de `anchor_text` na Etapa 1 inibe alucinação, (c) a Etapa 2 dispõe de uma estrutura validada em vez de ter de re-ler o texto sob carga de formatação.

### 7.2 Fundamentos na literatura

O padrão "extração estruturada → síntese" está bem estudado em três contextos próximos:

**Structured generation via intermediate representation (Wei et al. 2022, *Chain-of-Thought*).** Forçar saída intermediária estruturada antes da resposta final melhora precisão em tarefas multi-passo com ganho de 15-40% em raciocínio.

**Extract-then-Generate (Dou et al. 2021).** Em sumarização abstrativa, gerar primeiro um conjunto de fatos-chave ancorados no texto e depois sintetizar reduz alucinação em ~30%. Paralelo direto ao caso Synesis.

**Retrieval-Augmented Structured Generation.** Trabalhos em NER/relation extraction mostram que separar "identificação de spans com evidência" de "classificação/formatação" supera pipelines end-to-end em datasets com anotações complexas.

**Estimativa realista para Synesis:**

- Redução de alucinação (trechos/citações inventadas): **alta confiança**, ~40-60% de redução.
- Melhoria em direcionalidade CHAIN: **média confiança**, ~15-25% absoluto em F1.
- Melhoria em granularidade (uso consistente de conceitos existentes): **alta confiança**, especialmente se Etapa 1 recebe lista explícita de `code_index` e marca `exists_in_project: true/false`.
- Melhoria em segmentação ITEM (sequencial vs paralelo): **média confiança**, porque Etapa 2 tem estrutura clara para decidir.

### 7.3 Modos de falha esperados

1. **Erros na Etapa 1 contaminam Etapa 2.** Se a extração inventa uma cadeia, a síntese a formaliza. Mitigação: Etapa 2 deve **verificar anchors** contra o texto original (comparação de string ou semântica); itens sem match são descartados.

2. **Sobre-extração na Etapa 1.** Instrução "prefer recall over precision" na Etapa 1 pode produzir muitos candidatos de baixa qualidade. Mitigação: Etapa 2 aplica filtros por `confidence`; candidatos `low` descartados exceto quando o campo é REQUIRED e único disponível.

3. **Sub-extração na Etapa 1.** Modelo omite candidatos reais. Mitigação: já é o modo de falha do single-shot atual; staged não piora isso. Seria endereçado por ciclos de revisão **dentro da Etapa 1** (extensão ortogonal).

4. **Formato de artefato quebrado.** JSON mal-formado da Etapa 1. Mitigação: tool use / response_format mode (backend-dependente); fallback para re-extração com prompt de correção (1 tentativa).

5. **Desalinhamento entre Etapa 1 e Etapa 2.** Etapa 2 interpreta mal o artefato. Mitigação: schema explícito no system prompt da Etapa 2 com exemplos de cada tipo de campo.

### 7.4 Vantagem qualitativa — auditabilidade estruturada

O artefato da Etapa 1, persistido em disco (`{output}.extraction.json`), torna-se um **documento de pesquisa em si**. Pesquisadores podem:

- Inspecionar quais chains o modelo considerou e rejeitou
- Verificar ancoragem verbatim de cada decisão
- Comparar artefatos de dois modelos (Opus vs Sonnet) lado a lado
- Usar artefatos como *inter-coder reliability baseline* entre sessões

Este é um ganho **metodológico estrutural**, não apenas quantitativo. Para pesquisa qualitativa o valor é elevado.

### 7.5 Comparação formal com ciclos conversacionais

| Critério                                     | Ciclos (estudo anterior)             | Etapas (este estudo)                     |
| -------------------------------------------- | ------------------------------------ | ---------------------------------------- |
| Ganho esperado em F1 de chains               | 10-20% abs.                          | 15-25% abs.                              |
| Redução de alucinação                        | Média (modelo vê saída prévia)       | **Alta** (anchor verbatim obrigatório)   |
| Auditabilidade                               | Turnos inspecionáveis                | **Artefato estruturado**                 |
| Custo adicional                              | ~2×                                  | ~1.6×                                    |
| Latência                                     | ~3×                                  | ~2×                                      |
| Complexidade de implementação                | Média (review_runner + cycle_parser) | Média (extractor + synthesizer + schema) |
| Compatibilidade com open-source LLMs menores | Alta                                 | Média (tool use inconsistente)           |

Etapas cascateadas são **competitivas ou superiores em todas as dimensões** exceto compatibilidade com modelos menores (que podem não emitir JSON bem estruturado). Para o caso de uso primário (Opus/Sonnet em batch), etapas parecem superiores.

### 7.6 As duas abordagens podem ser combinadas

Nada impede:

- Etapa 1 com ciclo de auto-revisão interno (re-extrair após checklist)
- Etapa 2 com ciclo de verificação de ancoragem

Este estudo recomenda **implementar etapas primeiro**, avaliar ganho, e só então considerar ciclos intra-etapa como extensão.

---

## 8. Análise de Risco

| Risco                                                             | Prob.         | Impacto | Mitigação                                                                                                        |
| ----------------------------------------------------------------- | ------------- | ------- | ---------------------------------------------------------------------------------------------------------------- |
| Latência ~2× inviabiliza modo `item` interativo                   | Certa         | —       | Ativar staged apenas em `document`/`abstract`                                                                    |
| Erros de extração propagam para síntese                           | Alta          | Alto    | Verificação obrigatória de anchor verbatim na Etapa 2; descarte de items sem anchor válido                       |
| JSON malformado na Etapa 1                                        | Média         | Médio   | Tool use / response_format quando disponível; 1 tentativa de correção; fallback para parser tolerante            |
| Etapa 2 "reinventa" em vez de converter                           | Média         | Alto    | Prompt da Etapa 2 explícito: "transformar, não re-decidir"; testes de integração que comparam artefato vs output |
| Schema de artefato não cobre template customizado                 | Média         | Alto    | Schema derivado de `field_specs` em runtime; teste contra todos os projetos em `case-studies/`                   |
| Sobre-extração sobrecarrega prompt da Etapa 2                     | Média         | Médio   | Truncar candidatos por `confidence` antes de passar para Etapa 2; limites explícitos (e.g. top-5 por campo)      |
| Backends open-source não produzem JSON confiável                  | Alta (em OSS) | Alto    | Markdown estruturado como fallback; documentar que staged requer LLMs capazes                                    |
| Custo dobrado inaceitável para pesquisador com orçamento limitado | Baixa         | Médio   | Flag opt-in; documentação clara do trade-off custo/qualidade                                                     |
| Manutenção de dois prompts duplica esforço                        | Certa         | Médio   | Encapsulamento estrito; testes de regressão para cada prompt; evitar crescimento não-controlado                  |
| Ganho real < 5% F1                                                | Média         | Alto    | **Pré-condição:** benchmark §9 antes de implementação                                                            |

---

## 9. Protocolo de Validação Empírica

### 9.1 Benchmark comparativo

Executar em 20 abstracts de `social_acceptance` (mesmo corpus do estudo de ciclos, para comparabilidade direta):

| Config                                                  | Pipeline           | Thinking  | Ciclos |
| ------------------------------------------------------- | ------------------ | --------- | ------ |
| **(A)** Baseline atual                                  | single-shot        | 8000      | —      |
| **(B)** Thinking máximo                                 | single-shot        | 16000     | —      |
| **(C)** Staged — JSON strict                            | extração→síntese   | 8000 cada | —      |
| **(D)** Staged — markdown fallback                      | extração→síntese   | 8000 cada | —      |
| **(E)** 3 ciclos conversacionais (ref. estudo anterior) | —                  | 8000      | 3      |
| **(F)** Staged + ciclo intra-extração                   | extração×2→síntese | 8000 cada | —      |

### 9.2 Métricas (idênticas ao estudo anterior + novas)

1. **F1 de chains** — direcionalidade, relação, concepts válidos
2. **Taxa de alucinação** — fração de trechos citados que não aparecem verbatim no texto (só mensurável em C/D/F, via inspeção do artifact)
3. **Granularidade** — nº de conceitos únicos
4. **Precisão de direcionalidade** — subset do F1
5. **Consistência inter-execução** — N=3 em temperatura 0
6. **Latência por item** — wall time
7. **Custo por item** — USD
8. **Taxa de erro estrutural** — artefatos JSON inválidos / total (C/D/F apenas)
9. **Utilidade do artefato para auditoria** — avaliação qualitativa por um pesquisador em 5 amostras

### 9.3 Regra de decisão

| Resultado                                                    | Decisão                                                     |
| ------------------------------------------------------------ | ----------------------------------------------------------- |
| (C) > (A) em F1 por ≥ 10% abs. **E** taxa de alucinação < 5% | **GO — staged JSON strict**                                 |
| (D) > (A) em F1 por ≥ 8% abs.                                | **GO — staged com fallback**                                |
| (C) ≈ (E) em F1                                              | GO — preferir staged (menor latência, maior auditabilidade) |
| (C) < (E) em F1 mas > (A)                                    | GO — ambos, por valor colateral da auditabilidade           |
| (F) > (C) por ≥ 3% abs.                                      | Implementar Fase 2 (ciclo intra-Etapa 1)                    |
| (B) ≈ (C)                                                    | NO-GO staged — aumentar thinking resolve                    |
| Nenhuma config > (A) em ≥ 5%                                 | NO-GO — problema estrutural; revisar templates              |

### 9.4 Esforço de benchmark

4-6 dias. Inclui:

- Gold standard (pode-se reutilizar o construído para o estudo de ciclos, se executado)
- Implementação protótipo staged (~500 LOC descartáveis, ou em branch experimental)
- Execução e análise

---

## 10. Fases de Implementação (Condicionadas ao Benchmark)

**Pré-condição absoluta:** §9.1 executado com decisão GO para alguma configuração staged.

### Fase 0 — Benchmark (obrigatório)

Entregável: relatório `docs/bench_staged.md` (ou compartilhado com `bench_cycles.md`). GO/NO-GO documentado.

**Esforço:** 4-6 dias.

---

### Fase 1 — MVP staged pipeline em `document` mode

**Escopo:** implementação mínima do pipeline extração→síntese aplicado a um único modo para validação em produção.

- Novos módulos: `extractor.py`, `synthesizer.py`
- Schema de extração derivado dinamicamente do template em runtime
- Integração em `document_mode.py` atrás de flag `--pipeline staged`
- `item_mode.py`: rejeita `--pipeline staged` com mensagem (latência inviável)
- Persistência do artefato: `{output_stem}.extraction.json` para auditoria
- Prompts iniciais em inglês, consistentes com v0.1.2+
- Testes: schema derivation, anchor verification, integração com projetos do case-studies

**Entregáveis:**

- `synesis_coder/extractor.py`
- `synesis_coder/synthesizer.py`
- Ajustes em `prompt_builder.py`, `document_mode.py`, `cli.py`
- `tests/test_staged_pipeline.py`
- Documentação em README + CHANGELOG
- Exemplo de artefato em `docs/staged_artifact_example.json`

**Critério de saída:** reproduz em produção o ganho de F1 medido em (C) ou (D), dentro de ±3% absoluto. Taxa de alucinação mensurada < 5%.

**Esforço:** 2-2.5 semanas.

---

### Fase 2 — Extensão para `abstract` mode

**Escopo:** adaptar schema de extração para sumarização (input = corpus de ITEMs, não texto bruto).

- Schema alternativo `AbstractExtractionArtifact`: temas-chave, evidências cruzadas, inconsistências detectadas
- Etapa 2 sintetiza abstract a partir desses temas

**Entregáveis:** extensão de `extractor.py`, ajuste de `abstract_mode.py`.

**Critério de saída:** abstracts gerados são avaliados qualitativamente como mais estruturados/fiéis em 10 amostras.

**Esforço:** 1-1.5 semanas.

---

### Fase 3 (opcional) — Ciclos intra-etapa

**Pré-condição:** Fase 1 em produção **E** benchmark (F) > (C) por ≥ 3% abs.

**Escopo:** auto-revisão dentro da Etapa 1 (re-extração com checklist) ou dentro da Etapa 2 (verificação de ancoragem com ceticismo).

**Esforço:** 1-2 semanas.

---

### Fase 4 (opcional) — Sintaxe declarativa em template

**Pré-condição:** demanda explícita de pesquisadores por controle fino sobre prompts de extração.

**Escopo:** permitir que template declare instruções específicas para Etapa 1 por campo, análogo à sintaxe `CYCLE` do estudo anterior mas com semântica distinta (`EXTRACTION INSTRUCTIONS ... END EXTRACTION` dentro de `GUIDELINES`).

**Esforço:** 1.5-2 semanas.

---

## 11. Comparação Final: Etapas vs. Ciclos vs. Status Quo

| Dimensão               | Status quo (single-shot) | Ciclos conversacionais | Etapas cascateadas           |
| ---------------------- | ------------------------ | ---------------------- | ---------------------------- |
| Ganho esperado em F1   | —                        | 10-20% abs.            | **15-25% abs.**              |
| Redução de alucinação  | —                        | Média                  | **Alta**                     |
| Auditabilidade         | Baixa                    | Média                  | **Alta**                     |
| Custo relativo         | 1×                       | 2×                     | **1.6×**                     |
| Latência relativa      | 1×                       | 3×                     | **2×**                       |
| Complexidade de código | —                        | Média                  | Média                        |
| Precedente no codebase | N/A                      | Nenhum                 | **Sim (ontology, finetune)** |
| Modo interativo `item` | ✓                        | ✗                      | ✗                            |
| Modo batch             | ✓                        | ✓                      | ✓                            |
| Requer grammar change  | N/A                      | Não                    | **Não**                      |
| Retrocompatível        | N/A                      | ✓                      | ✓                            |

**Etapas dominam ou empatam em todas as dimensões exceto compatibilidade com LLMs OSS menores** (onde ciclos podem ser mais robustos por não dependerem de JSON estruturado).

---

## 12. Recomendação

**Viabilidade técnica:** alta. Nenhuma mudança em gramática, compilador, LSP, ou outras partes do ecossistema. Escopo de código bem delimitado (~1400 LOC, sem remoções).

**Viabilidade de precisão:** provável e substancial. Fundamento literário robusto (Extract-then-Generate, Chain-of-Thought structured). Precedente interno em `ontology_mode` reforça que o padrão funciona no domínio Synesis.

**Viabilidade de latência:** alta em batch; zero em interativo. Melhor que ciclos.

**Valor metodológico:** artefato estruturado da Etapa 1 é um **documento de pesquisa auditável**. Este ganho é qualitativo e particularmente valioso para pesquisa qualitativa.

**Caminho recomendado:**

1. **Fase 0 (benchmark):** executar com pelo menos as configurações (A), (B), (C), e idealmente (E) se o benchmark de ciclos não foi executado — comparação direta entre as duas abordagens alternativas na mesma suíte.
2. **Se GO:** implementar Fase 1 em `document` mode primeiro (maior beneficiário do ganho).
3. **Se Fase 1 bem-sucedida:** estender para `abstract`.
4. **Ciclos como extensão ortogonal:** avaliar apenas se etapas isoladamente forem insuficientes.

**Risco estratégico central:** decidir implementar sem benchmark. A hipótese de ganho é fundamentada, mas o domínio Synesis não é idêntico aos domínios da literatura. Validar com corpus real antes de investir nas 2.5 semanas da Fase 1.

**Recomendação final:** etapas cascateadas são preferíveis a ciclos conversacionais como **primeira intervenção estrutural** no synesis-coder, pelos motivos convergentes: (a) custo menor, (b) latência menor, (c) auditabilidade maior, (d) precedente interno demonstrando viabilidade do padrão, (e) maior controle sobre ancoragem verbatim — que é o mecanismo principal de fidelidade ao texto-fonte, objetivo declarado deste estudo.

---

*Estudo elaborado em 2026-04-22. Companheiro do estudo `estudo_prompts_ciclos.md` (v2). Requer execução do benchmark §9 antes de decisão de implementação. Nenhum código foi alterado.*

---


