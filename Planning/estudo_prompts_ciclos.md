Verificar o modo como Synesis-Coder

## Como o synesis-coder lê o template e processa itens

### 1. Entrada — `load_project()` (project_loader.py)

O único ponto de acesso ao projeto é `synesis.load()`. O `project_loader` não lê o `.synt` diretamente; delega tudo ao compilador.

**Sequência interna de `load_project()`:**

1. Lê o `.synp` como texto puro
2. Extrai o caminho do template via regex (`TEMPLATE "caminho/arquivo.synt"`)
3. Lê o `.synt` como texto puro
4. Varre as diretivas `INCLUDE` do `.synp` e coleta `.syn` (annotations), `.syno` (ontology), `.bib` (bibliography)
5. Chama `synesis.load(project_content, template_content, ...)` — o compilador faz o parsing completo (Lark LALR(1) → AST)
6. Extrai do resultado compilado:
  - `field_specs` → separa por `Scope.SOURCE`, `Scope.ITEM`, `Scope.ONTOLOGY`
  - Para cada campo CHAIN em SCOPE ITEM: extrai `chain_relations` (`{ENABLES: "...", INFLUENCES: "...", ...}`)
  - `required_fields`, `bundled_fields` por escopo
  - `code_index` = union de `code_usage` (campos CODE) + nós de `all_triples` (campos CHAIN)
  - `topic_index` de `linked_project.topic_index`
  - `project_description` de `linked_project.project.description`

**O que cada `FieldSpec` carrega após o compilador:**

| Atributo | Origem no `.synt` |
| --- | --- |
| `spec.type` | `TYPE QUOTATION / MEMO / CHAIN / ORDERED / ...` |
| `spec.scope` | `SCOPE SOURCE / ITEM / ONTOLOGY` |
| `spec.guidelines` | Conteúdo literal do bloco `GUIDELINES ... END GUIDELINES` |
| `spec.description` | `DESCRIPTION ...` (linha única) |
| `spec.values` | `VALUES ... END VALUES` (para ORDERED/ENUMERATED) |
| `spec.relations` | `RELATIONS ... END RELATIONS` (para CHAIN) |
| `spec.format` | `FORMAT [0..5]` (para SCALE) |

> **Conclusão:** o `synesis-coder` nunca parseia `GUIDELINES` manualmente. O compilador entrega `spec.guidelines` como string pronta.

---

### 2. Construção do prompt — `build_item_prompt()` / `build_abstract_prompt()` (prompt_builder.py)

O system prompt é **estático por sessão** (marcado `cache: True` para Anthropic prompt caching). A user message é **dinâmica** por chamada.

**Hierarquia de instrução por campo (`_field_instruction`):**

```
spec.guidelines  →  spec.description  →  _generic_instruction(spec.type)
```

Para o template `social_acceptance.synt`, o campo `text (QUOTATION)` tem `GUIDELINES` com 300+ palavras → esse bloco inteiro vai diretamente como instrução do campo no system prompt.

**Montagem do system prompt (ordem):**

1. Regras absolutas de output ("ONLY ITEM...END ITEM blocks, no markdown")
2. `OUTPUT LANGUAGE` (se `SYNESIS_CODER_LANGUAGE` definido no `.env`)
3. `PROJECT CONTEXT` (bloco `DESCRIPTION...END DESCRIPTION` do `.synp`)
4. **ITEM FIELDS** — para cada campo em `SCOPE ITEM`:
  - Nome + tipo + [REQUIRED/OPTIONAL]
  - Instrução: `spec.guidelines` completo (ex: os ~300 words do campo `chain` do social_acceptance)
  - Extras por tipo:
    - CHAIN → lista de relações + sintaxe
    - ORDERED/ENUMERATED → valores com índices e descrições
    - SCALE → range
5. **EXISTING PROJECT CONCEPTS** — lista de conceitos do `code_index` (agrupados em linhas de 10)
6. **EXISTING TOPICS** — lista do `topic_index`
7. **OUTPUT FORMAT** — template de bloco com bundle constraints

No modo `abstract`, a montagem inclui adicionalmente a seção **SOURCE FIELDS** (antes de ITEM FIELDS), e as regras absolutas exigem `EXACTLY ONE SOURCE` + `ONE OR MORE ITEM`.

---

### 3. Processamento — `item_mode` / `abstract_mode`

```
load_project(path)
    → ctx (dict com todos os FieldSpecs já processados)
        ↓
build_item_prompt(ctx, bibref, text)
    → [system_msg (cached), user_msg]
        ↓
LLMClient.call(messages, temperature=0)
    → raw LLM output
        ↓
validator.validate_and_fix(output, ctx, client)
    → synesis.load() valida o bloco gerado
    → se inválido: até 3 tentativas de correção (temperature 0.0 → 0.2 → 0.5)
        ↓
stdout: bloco(s) ITEM válidos
```

**Ponto crítico:** as `GUIDELINES` dos campos chegam ao LLM **integralmente preservadas** — o synesis-coder não filtra, trunca ou reformata o conteúdo dos blocos GUIDELINES. O que está no `.synt` é o que vai no prompt.

---

# Estudo de Viabilidade — Ciclos de Prompt no Synesis-Coder

**Data:** 2026-04-22
**Revisão:** 2 — Reformulação com foco em precisão; modelo único; ciclos conversacionais (contexto compartilhado); modo batch como alvo primário.
**Escopo:** Avaliar se a introdução de sub-blocos `CYCLE N ... END CYCLE` dentro de `GUIDELINES`, executados como revisão iterativa do próprio modelo dentro de um mesmo contexto de conversa, produz ganho real de precisão suficiente para justificar o aumento de latência. Custo é tratado como secundário.

## 0. Nota de Revisão (v2 vs v1)

A versão 1 deste estudo avaliou ciclos como **chamadas isoladas** a modelos potencialmente distintos (ex.: Gemma → Sonnet → Opus). A análise concluiu que o ganho econômico era marginal e o ganho de precisão era especulativo.

Esta revisão muda o enquadramento a pedido do usuário:

| Dimensão | v1 | v2 (esta revisão) |
|---|---|---|
| Objetivo primário | Economia de custo + precisão | **Precisão apenas** |
| Modelo por ciclo | Múltiplos (roteamento) | **Único** (mesmo modelo em todos os ciclos) |
| Contexto entre ciclos | Isolado (cada ciclo = nova chamada fresca) | **Compartilhado** (multi-turno dentro da mesma conversa — o modelo revisa a própria decisão) |
| Modo de uso | Todos (incluindo `item` interativo) | **Batch apenas** (`document`, `abstract`, `ontology`) |
| Custo | Métrica decisiva | Métrica informativa |
| Latência | Métrica secundária | **Métrica decisiva** |

---

## 1. Motivação e Contexto

### 1.1 Problema observado

Templates analíticos maduros (ex.: [social_acceptance.synt](../case-studies/Sociology/Social_Acceptance/social_acceptance.synt)) possuem `GUIDELINES` muito extensos:

| Campo | Tipo | Palavras em GUIDELINES |
|---|---|---|
| `text` | QUOTATION | ~180 |
| `note` | MEMO | ~170 |
| `chain` | CHAIN | **~720** |
| `aspect` | ORDERED | ~250 |
| `ontology_description` | TEXT | ~110 |

O campo `chain` do social_acceptance é o caso crítico — concentra **7 sub-regras heterogêneas** num único prompt monolítico:

1. Priorização de relações (ENABLES > INFLUENCES > ...)
2. Testes de seleção por relação
3. Direcionalidade por pistas linguísticas
4. Padrão de moderação/interação (gera chains múltiplas)
5. Sequencial vs. paralelo (decisão ITEM único ou múltiplos)
6. Convergência multi-fator
7. Controle de granularidade + regra de generalização hierárquica

**Consequência empírica esperada:** atenção difusa do LLM. Prompts longos e heterogêneos costumam produzir decaimento na aplicação de regras posteriores (*lost-in-the-middle*), erros de direcionalidade em CHAIN, e proliferação de conceitos por falha na aplicação da regra de granularidade.

### 1.2 Proposta reformulada

Subdividir `GUIDELINES` em ciclos nomeados (`CYCLE 1 ... END CYCLE`, `CYCLE 2 ... END CYCLE`, ...), executados sequencialmente **no mesmo modelo, dentro da mesma sessão de conversa**. O modelo recebe o prompt do ciclo N+1 tendo como contexto visível o prompt e sua própria resposta dos ciclos anteriores. Conceitualmente: **o modelo revisa a própria decisão** em vez de refazê-la a partir do zero.

A inspiração metodológica permanece a mesma da v1 (*network identification → label refinement → evidence verification*), mas a execução muda: uma única conversa multi-turno, não três chamadas independentes.

### 1.3 Contexto técnico já disponível

- **Extended thinking** (v0.2.0) — raciocínio interno antes da resposta, sem alterar arquitetura. Cobre parcialmente o mesmo problema de precisão.
- **Correção iterativa no validator** — até 3 tentativas com escalação de temperatura (0.0 → 0.2 → 0.5). Mas é acionada apenas quando a saída é sintaticamente inválida; **não corrige erros semânticos** em saídas sintaticamente corretas.
- **Multi-turn nativo** — a API Anthropic Messages (e OpenAI Chat Completions) suporta naturalmente `[system, user, assistant, user, assistant, user]` como lista de mensagens. Não requer mudança na camada de transporte.

Qualquer ganho proposto por ciclos conversacionais deve ser **incremental sobre extended thinking + correção iterativa**, não sobre single-shot zero-shot.

---

## 2. Dois Modelos de Ciclos: Isolados vs. Conversacionais

A literatura e a prática de LLMs distinguem dois regimes de ciclos iterativos. Esta distinção é central para avaliar a proposta revisada.

### 2.1 Ciclos isolados (v1 deste estudo)

Cada ciclo = nova chamada com contexto reconstruído do zero. Ciclo N recebe `[system, user]` onde o `user` contém o texto original + a saída textual do ciclo N-1. O modelo do ciclo N **não tem memória da conversa** do ciclo N-1 — só vê o output final como se fosse dado externo.

**Equivalente na literatura:** pipeline composicional (*cascaded LLMs*). Adequado quando se quer isolar o julgamento de um ciclo do raciocínio do anterior (LLM-as-Judge clássico).

### 2.2 Ciclos conversacionais (foco da v2)

Uma única sessão multi-turno no mesmo modelo. A lista de mensagens cresce:

```
[
  {role: system,    content: [regras globais + ciclo 1 apenas]},
  {role: user,      content: [texto + pedido do ciclo 1]},
  {role: assistant, content: [output do ciclo 1]},
  {role: user,      content: [pedido do ciclo 2 — "revise a extração acima..."]},
  {role: assistant, content: [output do ciclo 2]},
  {role: user,      content: [pedido do ciclo 3 — "agora valide contra o texto original..."]},
  {role: assistant, content: [output final Synesis]}
]
```

O modelo vê a própria cadeia de raciocínio. Pode **corrigir-se** reconhecendo erro anterior; pode **estabilizar** mantendo decisões coerentes; pode **refinar** aplicando regra nova sobre a própria estrutura anterior.

**Equivalente na literatura:** *Self-Refine* (Madaan et al. 2023), *Self-Critique* (Saunders et al. 2022), *Chain-of-Verification* (Dhuliawala et al. 2023).

### 2.3 Comparação das duas abordagens

| Dimensão | Isolado | Conversacional |
|---|---|---|
| Memória entre ciclos | Nenhuma | Completa |
| Viés de ancoragem na saída anterior | Baixo | Alto |
| Capacidade de auto-correção | Baixa (relê como dado externo) | Alta (relê como própria decisão) |
| Coerência estilística entre ciclos | Baixa | Alta |
| Custo de input por ciclo | Prompt reconstruído | Prefixo cacheado; crescimento linear |
| Adequado para | Julgamento independente | Refinamento iterativo |
| Risco de repetir erro | Baixo | **Alto** (*anchoring*) |
| Risco de deriva | Alto (incoerência) | Baixo |

### 2.4 Qual abordagem serve ao problema do Synesis

O problema-alvo (CHAIN extraction em social_acceptance) tem estas características:

1. **Erros dominantes** são de **direcionalidade** e **granularidade** — erros que o modelo **reconhece** quando forçado a revisar, porque as regras para detectá-los estão explícitas no GUIDELINES.
2. **Erros de recall** (omitir chains reais) exigem re-leitura do texto original — o modelo precisa do texto ainda acessível no contexto.
3. **Consistência inter-ITEM** é valiosa — se o modelo define "Trust" como conceito no primeiro item, deve reutilizar "Trust" (não "trust_in_institutions") nos seguintes.

Esses três pontos favorecem **ciclos conversacionais**. O risco principal (*anchoring* — modelo repete o próprio erro por viés de auto-confirmação) é mitigável com instruções explícitas de ceticismo no ciclo de revisão ("suponha que a saída anterior contém erros; procure-os ativamente").

**Conclusão:** ciclos conversacionais são a abordagem adequada para este caso.

---

## 3. Análise de Viabilidade Técnica

### 3.1 Sintaxe `CYCLE N ... END CYCLE` dentro de `GUIDELINES`

**Bloqueio importante:** a gramática `synesis/grammar/synesis.lark` está **CONGELADA para v1.x** (AI_INSTRUCTIONS §10). A sintaxe CYCLE não pode ser reconhecida pelo compilador.

**Solução:** parsing interno no synesis-coder. O `spec.guidelines` chega como string opaca; o synesis-coder faz regex split. Compilador e grammar permanecem intocados.

**Exemplo de sintaxe proposta (compatível com compilador atual):**

```
FIELD chain TYPE CHAIN
    SCOPE ITEM
    GUIDELINES
        # Regras globais visíveis em todos os ciclos
        RELATION SELECTION PRIORITY: ENABLES > INFLUENCES > CONSTRAINS > ...
        FACTOR NAMING: snake_case singular.

        CYCLE 1 EXTRACTION
            Read the text carefully. Identify every causal claim.
            For each claim: state subject, relation, object, and the verbatim
            sentence that supports it. Be exhaustive; prefer recall over precision.
            Output as a simple bullet list. Do not emit Synesis syntax yet.
        END CYCLE

        CYCLE 2 REFINEMENT
            Review your extraction above. For each edge:
            - Verify directionality using the linguistic cue rules.
            - Apply FACTOR GRANULARITY CONTROL — generalize where specificity
              is unjustified; split compound factors.
            - Ensure concepts are in snake_case and match EXISTING PROJECT CONCEPTS.
            Mark any edge you now believe is incorrect and remove it.
            Do not emit Synesis syntax yet.
        END CYCLE

        CYCLE 3 VALIDATION
            For each remaining edge, find the verbatim supporting text and quote it.
            Remove edges without clear verbatim evidence.
            Apply SEQUENTIAL vs PARALLEL logic to decide ITEM grouping.
            Emit the final Synesis ITEM block(s).
        END CYCLE
    END GUIDELINES
END FIELD
```

**Escopo da sintaxe CYCLE:** a princípio aplicável apenas ao nível de **campo CHAIN** (caso de maior complexidade) ou ao nível de **FIELD** em geral. A granularidade depende do que o benchmark da §7.1 demonstrar ser suficiente.

### 3.2 Parsing e execução

Fluxo em `cycle_runner.py` (novo módulo, sem alterar `project_loader.py`):

```
1. Detectar blocos CYCLE N ... END CYCLE em spec.guidelines (regex).
2. Se nenhum CYCLE detectado em NENHUM campo → single-shot atual (retrocompatível).
3. Se CYCLEs detectados:
   a. Extrair regras globais (fora de CYCLE blocks) → system prompt.
   b. Para cada ciclo N:
      - Montar user message com instruções do ciclo N.
      - Chamar client.call(messages_so_far, temperature=0).
      - Anexar resposta à lista de mensagens.
   c. Ciclo final → validator.validate_and_fix() como hoje.
```

Nenhuma mudança necessária em `project_loader.py` — o parsing de CYCLE é puramente cosmético sobre o string `spec.guidelines` já entregue pelo compilador.

### 3.3 Caching e estado

**Cache Anthropic** funciona por prefixo. Em ciclos conversacionais com mesmo modelo:

- System prompt (regras globais + ciclo 1) → cacheado após 1ª chamada
- Após assistant 1 → o prefixo `[system, user1, assistant1]` pode receber `cache_control` para virar cacheado no próximo turno
- A cada turno, o prefixo cacheado cresce; apenas o novo turno paga preço cheio

**Resultado prático:** 3 ciclos conversacionais em Opus-4-7 custam aproximadamente `1.3×` de um único single-shot (não `3×`), porque o prefixo fica cacheado entre turnos. Isso assume que o mesmo item é processado de ponta a ponta antes de trocar para outro item (serial dentro do item).

### 3.4 Independência de modelo

A abordagem v2 (mesmo modelo em todos os ciclos) **simplifica drasticamente** a implementação:

- Um único `LLMClient`, sem fábrica ou roteamento
- Sem esquema `.env` multi-modelo
- Sem perda de eficácia de cache por troca de modelo
- Sem preocupação com compatibilidade de features (thinking budget, cache breakpoints) entre modelos distintos

Multi-modelo (v1) fica relegado a uma **Fase futura e opcional**, a ser avaliada apenas se ciclos conversacionais single-model se provarem precisos.

---

## 4. Análise de Custo e Latência

### 4.1 Custo (informativo, não decisivo)

Estimativa por item em ciclos conversacionais single-model com Opus-4-7 + thinking 8000:

| Componente | Por ciclo | Total 3 ciclos (com cache) |
|---|---|---|
| System prompt input | 4.000 tokens (cached após C1) | 1× preço cheio + 2× preço cache |
| User message input | 500 tokens | 3× preço cheio |
| Turn output (incluindo thinking) | ~5.500 tokens | 3× preço cheio |
| **Custo estimado por item** | — | **~$0.28** |

vs. baseline single-shot Opus-4-7 + thinking: ~$0.14/item.

**Custo em 3 ciclos: ~2× o baseline.** Aceitável dentro do enquadramento "custo é secundário se precisão melhorar".

Para modo batch de 500 itens: ~$140 → ~$70 a mais que o atual. Em contexto de pesquisa acadêmica (corpora de dezenas a centenas de abstracts), isso é absorvível.

### 4.2 Latência (métrica decisiva)

| Modo | Latência estimada por item (Opus + thinking 8000) |
|---|---|
| Single-shot atual | 8-15s |
| 3 ciclos conversacionais | **30-45s** |

Em modo `item` interativo no VSCode (Ctrl+Shift+I), 30+ segundos é inaceitável como UX — o usuário espera resposta em ≤ 10s.

Em modo batch (`document`, `abstract`, `ontology`), a latência por item é mitigada por paralelismo (`--concurrent 5` é o padrão). Tempo de parede de um documento com 50 abstracts:

| Modo | Tempo com `--concurrent=5` |
|---|---|
| Single-shot atual | ~2 minutos |
| 3 ciclos conversacionais | ~5-7 minutos |

**Conclusão de latência:**
- **Modo batch:** perfeitamente aceitável. 5 minutos de espera por um documento de 50 abstracts com precisão superior é um trade favorável para um pesquisador.
- **Modo interativo (`item`):** inaceitável. Ciclos devem ser **desabilitados** neste modo.

### 4.3 Rate limits

Em multi-turn no mesmo item, cada turno conta para RPM/TPM independentemente. Com `--concurrent=5` e 3 ciclos = até 15 chamadas simultâneas. Os limites atuais do `.env` (`MAX_RPM=50`, `MAX_INPUT_TPM=40000`) absorvem isso, mas podem ser saturados em corpora grandes. Configurável.

---

## 5. Análise de Ganho de Precisão

### 5.1 Literatura relevante (ciclos conversacionais single-model)

**Self-Refine (Madaan et al. 2023):** o mesmo modelo gera → critica a própria saída → revisa. Em 7 tarefas diversas, ganho médio de **~20% absoluto** sobre single-shot. Gains maiores em tarefas com estrutura complexa de output (code generation: +8%; dialog response: +25%; sentiment reversal: +35%).

**Chain-of-Verification — CoVe (Dhuliawala et al. 2023):** draft → plano de perguntas de verificação → responde perguntas isoladamente → revisa. Reduz alucinação em ~30-50% em geração factual de forma longa.

**Self-Consistency (Wang et al. 2023):** sample múltiplo na mesma temperatura → voto majoritário. Ortogonal a ciclos; mencionado apenas para contexto.

**Aplicabilidade ao Synesis CHAIN extraction:**
- Estrutura de output complexa (CHAIN com direcionalidade, múltiplos concepts, regras de granularidade) → perfil próximo de *code generation* em Self-Refine (+8-15% absoluto esperado)
- Presença de regras explícitas e testáveis (linguistic cues, ARITY, relation priority) → perfil próximo de CoVe (reduz violações de regra em ~30%)
- **Estimativa realista:** F1 melhora **10-20% absoluto** em chains com direcionalidade complexa. Menor ganho em chains triviais.

### 5.2 Modos de falha conhecidos de ciclos conversacionais

1. **Anchoring / auto-confirmação.** Modelo repete o próprio erro por viés de coerência. Mitigação: prompt do ciclo de revisão deve conter instrução adversarial explícita ("assume your previous output contains errors; find them").

2. **Over-revision.** Modelo modifica saída correta por excesso de zelo — cria novos erros ao tentar "melhorar" algo já certo. Mitigação: cycle 2 pede **justificativa explícita** para cada mudança proposta.

3. **Drift estilístico.** Ao longo de muitos ciclos, saída afasta-se do formato solicitado. Mitigação: último ciclo (emissão final) reitera o OUTPUT FORMAT rigorosamente.

4. **Context pollution.** Se ciclo 1 extrai mal e ciclo 2 refina em cima, o erro se cristaliza. Mitigação: ciclo de validação deve comparar contra o **texto original**, não contra saída de ciclos anteriores.

5. **Diminishing returns após 3 iterações** (Madaan et al.). Raramente vale mais de 3-4 ciclos.

### 5.3 Extended thinking vs. ciclos conversacionais

Ambos dão ao modelo "mais compute" para raciocinar. Diferenças:

| Característica | Thinking (interno) | Ciclos conversacionais |
|---|---|---|
| Visibilidade do raciocínio | Opaca ao usuário | Cada turno é registrável / inspecionável |
| Estruturação do raciocínio | Livre, não guiada | Guiada por prompt de cada ciclo |
| Custo de output | Tokens de thinking = output | Tokens de turnos intermediários = output |
| Ganho típico | ~5-15% absoluto em precisão | ~10-25% absoluto em precisão (estrutura complexa) |
| Auditabilidade | Baixa | **Alta** — cada ciclo pode ser salvo como JSON de depuração |

**Ganho provável dos dois combinados:** thinking + ciclos são ortogonais em parte. Thinking ajuda cada turno; ciclos estruturam a cadeia. Ganho combinado provavelmente é sub-aditivo (ex.: thinking sozinho +10%, ciclos sozinhos +15%, combinados +20% — não +25%).

**Implicação:** a comparação relevante para o benchmark **não é** ciclos vs. single-shot puro, mas **ciclos (com thinking) vs. single-shot com thinking + retries**.

### 5.4 Valor específico para o projeto Synesis

Ciclos conversacionais trazem **um valor adicional não econômico** que single-shot não oferece: **auditoria do raciocínio do modelo**.

Em pesquisa qualitativa, justificar decisões analíticas é parte da metodologia. Poder inspecionar ciclos intermediários ("por que o modelo escolheu CONSTRAINS em vez de INFLUENCES?") é um ganho metodológico, não apenas de precisão. Cada ciclo pode ser salvo como log para revisão humana.

---

## 6. Análise de Risco

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Latência 30-45s/item inviabiliza modo interativo | Certa | — | **Desabilitar ciclos em modo `item`**. Ativar apenas em `document`, `abstract`, `ontology` |
| Anchoring — modelo repete próprio erro | Alta | Alto | Ciclo de revisão usa prompt adversarial explícito ("assume errors exist; find them"); referência forçada ao texto original |
| Over-revision — modelo corrompe saída correta | Média | Alto | Ciclo de revisão deve justificar cada mudança; emitir **diff** entre ciclos, não reescrita do zero |
| Drift estilístico na saída final | Média | Médio | Último ciclo reitera OUTPUT FORMAT integralmente |
| Autores escrevem CYCLE blocks mal estruturados | Média | Médio | Validar estrutura no cycle_parser; fallback silencioso para single-shot se parsing falhar |
| Complexidade dobra custo de manutenção do synesis-coder | Certa | Médio | Encapsular em `cycle_runner.py`; manter fallback single-shot como caminho default |
| Incompatibilidade com templates existentes | Baixa | Alto | Templates sem CYCLE continuam funcionando em single-shot (retrocompatibilidade obrigatória) |
| Rate limit saturado em batch grande | Média | Médio | Ajustar `MAX_RPM`/`MAX_INPUT_TPM`; reduzir `--concurrent` quando ciclos ativos |
| Ganho de precisão marginal (< 5%) não justifica latência | Média | Alto | **Pré-condição obrigatória:** benchmark da §7.1 antes de implementação de produção |

---

## 7. Protocolo de Validação Empírica

### 7.1 Benchmark obrigatório

Antes de implementação de produção, executar comparação em 20 abstracts de social_acceptance com gold standard humano:

| Configuração | Modelo | Ciclos | Thinking |
|---|---|---|---|
| **(A)** Baseline atual | Opus-4-7 | 1 | 8000 |
| **(B)** Thinking máximo | Opus-4-7 | 1 | 16000 |
| **(C)** 2 ciclos conversacionais | Opus-4-7 | 2 (extract → validate) | 8000 |
| **(D)** 3 ciclos conversacionais | Opus-4-7 | 3 (extract → refine → validate) | 8000 |
| **(E)** Self-review simples | Opus-4-7 | 2 (gen → review, sem CYCLE template) | 8000 |

### 7.2 Métricas

1. **F1 de chains** — direcionalidade correta, relação correta, concepts válidos (comparação contra gold)
2. **Granularidade** — número de conceitos únicos por abstract (quanto menor, melhor, até o limite de 150 total no corpus)
3. **Precisão de direcionalidade isolada** — % de chains com direção correta (subset do F1)
4. **Consistência inter-execução** — desvio em N=3 execuções idênticas (temperatura 0)
5. **Latência média por item** — wall time
6. **Custo por item** — USD

### 7.3 Regra de decisão

| Resultado | Decisão |
|---|---|
| (D) > (A) em F1 por ≥ 10% absoluto | **GO** — implementar ciclos conversacionais |
| (C) > (A) em F1 por ≥ 7% absoluto | **GO com 2 ciclos** (mais simples, menos latência) |
| (E) > (A) em F1 por ≥ 5% absoluto **E** ≈ (D) | **GO com self-review simples** — dispensa sintaxe CYCLE no template |
| (B) ≈ (D) em F1 | **NO-GO** — dobrar thinking é suficiente; ciclos não agregam |
| Nenhum cenário > (A) em F1 por ≥ 5% | **NO-GO** — problema não é capacidade de raciocínio; revisar templates |

### 7.4 Esforço

3-5 dias. Inclui: construção de gold standard (maior parcela), harness de benchmark, coleta de métricas, relatório.

---

## 8. Recomendação

**Viabilidade técnica:** alta. Parsing interno sem alterar gramática; multi-turn suportado nativamente pela API; mesmo modelo simplifica drasticamente a implementação.

**Viabilidade de latência:** alta em modo batch; zero em modo interativo.

**Ganho de precisão:** **provável e substancial** (10-20% absoluto em F1 de chains), com base em literatura robusta (Self-Refine, CoVe). Mas **não demonstrado** no domínio Synesis — benchmark obrigatório antes de implementação de produção.

**Valor colateral:** auditabilidade. Cada ciclo é inspecionável, útil para justificação metodológica em pesquisa qualitativa.

**Caminho recomendado:**

1. **Executar Fase 0** (benchmark §7). **Pré-condição absoluta.**
2. Se resultado indica **self-review simples suficiente** (caso E ≈ D): implementar Fase 1 apenas, sem sintaxe CYCLE.
3. Se resultado indica **ciclos estruturados valem a pena** (caso D > B): implementar Fases 1 → 2.
4. Multi-modelo (v1 original) fica arquivado como **Fase 3 opcional futura**, reavaliável apenas se houver pressão de custo.

---

## 9. Fases de Implementação

**Pré-condição:** §7.1 validado com decisão GO.

### Fase 0 — Benchmark (pré-implementação, obrigatória)

**Entregável:** relatório em `docs/bench_cycles.md` com tabelas de F1/granularidade/latência/custo para configurações A-E. GO/NO-GO documentado.

**Esforço:** 3-5 dias.

---

### Fase 1 — Self-review conversacional single-model (MVP)

**Escopo:** implementar o mecanismo de ciclos conversacionais com **um único ciclo de revisão genérico**, sem sintaxe CYCLE em template. Usuário ativa via flag; prompt de revisão é fixo.

- Flag `.env`: `SYNESIS_CODER_REVIEW_PASSES=0|1|2` (default 0 = atual)
- Flag CLI equivalente: `--review-passes N`
- Aplicável apenas a `document`, `abstract`, `ontology` (`item` rejeita com mensagem)
- Implementação: novo módulo `synesis_coder/review_runner.py` (~250 linhas)
- Prompt de revisão embutido no código, por modo (document tem prompt diferente de ontology)
- Validator rodado apenas após o último passe

**Entregáveis:**
- `synesis_coder/review_runner.py`
- Ajuste em `modes/document_mode.py`, `modes/abstract_mode.py`, `modes/ontology_mode.py`
- Testes em `tests/test_review_runner.py`
- Documentação em README + CHANGELOG

**Critério de saída:** reproduz em produção o ganho medido no benchmark (dentro de ±2% absoluto).

**Esforço:** 1-1.5 semanas.

---

### Fase 2 — Sintaxe CYCLE em template (opcional, condicional)

**Pré-condição:** Fase 1 em produção + demanda explícita de pesquisador para prompts de revisão customizados por campo/projeto.

**Escopo:** permitir que o template declare ciclos específicos por campo em `GUIDELINES`, dando ao pesquisador controle metodológico fino.

- Parser em `synesis_coder/cycle_parser.py`
- Integração em `review_runner.py` — se CYCLE blocks detectados, usar; senão, prompt genérico da Fase 1
- Template de teste em `case-studies/_cycles/social_acceptance_cycled.synt`

**Entregáveis:**
- `synesis_coder/cycle_parser.py`
- Extensão de `review_runner.py`
- Template de exemplo + documentação em `docs/cycles.md`

**Critério de saída:** templates com CYCLE customizado produzem ganho ≥ 3% absoluto sobre prompt genérico da Fase 1, em pelo menos um projeto real.

**Esforço:** 1.5-2 semanas.

---

### Fase 3 — Multi-modelo por ciclo (arquivada — reavaliação futura)

Escopo original da v1 deste estudo. Mantida aqui como referência, a ser retomada somente se surgir pressão explícita de custo em corpora muito grandes (milhares de abstracts) e se benchmark específico mostrar ganho custo/precisão favorável.

Sem estimativa de esforço até reavaliação.

---

## 10. Conclusão

A mudança de enquadramento (precisão > custo; mesmo modelo; conversacional > isolado; batch apenas) **torna a proposta consideravelmente mais atrativa**:

- A literatura de *Self-Refine* e *CoVe* sustenta fortemente o ganho de precisão esperado (10-20% absoluto).
- O mesmo modelo em conversa multi-turno é **trivialmente implementável** — a API Messages do Anthropic já suporta nativamente. Sem refatoração arquitetural.
- A limitação a modo batch remove a única objeção forte (latência).
- O custo aproximadamente dobra, mas é absorvível no contexto de pesquisa qualitativa.
- A auditabilidade do raciocínio é um ganho metodológico qualitativo, não apenas quantitativo.

**Caminho mais curto para valor:** executar benchmark, e se GO, implementar a Fase 1 (self-review genérico) que já colhe a maior parte do ganho sem exigir mudança de template.

A Fase 2 (sintaxe CYCLE customizada) é um upgrade opcional que só se justifica se pesquisadores expressarem necessidade de controle fino sobre o prompt de revisão por projeto.

O risco estratégico é **implementar a Fase 2 antes de validar a Fase 1**. A hipótese "prompts de revisão customizados por template produzem ganho adicional" é uma camada de complexidade que precisa de sua própria validação empírica.

---

*Revisão 2 elaborada em 2026-04-22. Requer execução do benchmark §7.1 antes de decisão de implementação.*