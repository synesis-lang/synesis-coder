# Pipeline de Anotação Multi-Fase no Synesis-Coder

## Especificação de Implementação

---

## 1. Definição

O Synesis-Coder passa a executar o processo de anotação em **quatro fases fixas**, selecionáveis por parâmetro de CLI. Nenhuma mudança é feita na linguagem Synesis: todas as instruções de geração de campos continuam declaradas nos blocos `GUIDELINES ... END GUIDELINES` do `.synt`, e toda a coordenação de fases é interna ao coder.

| Fase | Nome          | Entrada            | Saída                                               |
| ---- | ------------- | ------------------ | --------------------------------------------------- |
| 1    | Extraction    | Abstract + `.synt` | `.syn` bruto                                        |
| 2    | Critique      | `.syn` + abstract  | `.synr` (`.syn` + blocos `# REVISION`)              |
| 3    | Normalization | `.synr` do corpus  | `.synr` com sugestões de canonicalização de códigos |
| 4    | Incorporation | `.synr` final      | `.syn` definitivo + métricas no cabeçalho           |

O princípio é o de **codificação incremental**: cada fase assume como verdade o conteúdo do arquivo recebido e opera apenas sobre o delta necessário — análogo a compilação incremental, em que cada passo transforma a representação intermediária sem refazer o anterior. A revisão humana é opcional entre fases e consiste em editar diretamente o `.synr` (manter, modificar ou remover tags de sugestão).

A justificativa teórica é o framework ACT (Annotation with Critical Thinking): modelos distintos por fase e papel evitam o self-bias de auto-revisão, e scores contínuos de suspeição com correção candidata superam decisões binárias.

---

## 2. Conformidade com a gramática Synesis

A gramática `synesis.lark` está **congelada** para v1.x. A proposta não adiciona nem altera um único token:

| Elemento operacional              | Mecanismo Synesis usado                          | Status                      |
| --------------------------------- | ------------------------------------------------ | --------------------------- |
| Regras de geração por campo       | `GUIDELINES` em `FIELD` (já existe)              | Inalterado                  |
| Modelos LLM por fase              | Variáveis de ambiente (`.env`)                   | Fora da gramática           |
| Prompts de crítica e normalização | Configuração do Synesis-Coder                    | Fora da gramática           |
| Tags de revisão em `.synr`        | Comentários `# $var: value` (`%ignore` no lexer) | Não interfere na compilação |

**Decisão arquitetural:** `PHASE`, `REVIEW_POLICY` e `BATCH` **não são** blocos da linguagem. São conceitos exclusivamente do pipeline de execução do coder.

---

## 3. As quatro fases

### 3.1 Configuração em `.env`

Modelos LLM, chaves de API e thresholds são declarados em `.env`:

```
SYNESIS_CODER_EXTRACTION_MODEL=gemini-2.5-pro
SYNESIS_CODER_EXTRACTION_API_KEY=...

SYNESIS_CODER_CRITIQUE_MODEL=claude-sonnet-4-6
SYNESIS_CODER_CRITIQUE_API_KEY=...

SYNESIS_CODER_NORMALIZATION_MODEL=qwen3:14b
SYNESIS_CODER_NORMALIZATION_API_KEY=

SYNESIS_CODER_SUSPICION_THRESHOLD=0.70
SYNESIS_CODER_MERGE_CONFIDENCE_THRESHOLD=0.60
```

**Validação na inicialização:** antes de rodar qualquer fase, o Synesis-Coder verifica se a variável de modelo e (quando aplicável) a de chave estão presentes e consistentes para a fase solicitada. Na ausência, emite mensagem instrucional:

```
ERRO: Fase 2 (crítica) requer SYNESIS_CODER_CRITIQUE_MODEL e SYNESIS_CODER_CRITIQUE_API_KEY
em .env. Modelos recomendados: claude-sonnet-4-6, gemini-flash, gpt-4o-mini.
Exemplo:
    SYNESIS_CODER_CRITIQUE_MODEL=claude-sonnet-4-6
    SYNESIS_CODER_CRITIQUE_API_KEY=sk-ant-...
```

A fase 4 (incorporação) não requer LLM — é determinística.

### 3.2 Fase 1 — Extração

Executa o prompt construído a partir das `GUIDELINES` de cada campo do template. É o modo `item`/ `abstract` / `document` atual do Synesis-Coder, renomeado internamente para `EXTRACTION`  (não há necessidade de entrada de parâmetro extra). Nenhuma mudança de comportamento: lê `.synt` + abstract, gera `.syn` bruto respeitando a sintaxe do template.

As GUIDELINES de `social_acceptance.synt` já carregam toda a especificação necessária: escala analítica 1–5 para `text`, estrutura de mecanismo causal para `note`, hierarquia de relações e controle de granularidade para `chain`, classificações (Dooyeweerd, Wüstenhagen, RGT) para ONTOLOGY.

### 3.3 Fase 2 — Crítica

Lê o `.syn` produzido pela fase 1 e gera um `.synr` **de mesmo nome**, com **conteúdo idêntico** preservado, acrescido de um bloco `# REVISION` ao final de cada ITEM suspeito. O bloco contém:

- `# $suspicion_score:` — valor contínuo 0.0–1.0.

- `# $reason:` — uma de `anchor_missing` | `mechanism_unsupported` | `wrong_direction` | `optional_field_unfounded` | `granularity_violation` | `none`.

- `# $<field>:` — (opcional) valor sugerido para cada campo que o criticador propõe alterar.

- **Como aplicar:** O extrator (Fase 1 - Extraction) **deve** extrair o trecho exato do texto que justifica cada `chain` ou `note`.

- **A Crítica (Fase 2 - Critique):** Em vez de apenas "achar" que algo está errado, o crítico verifica se o `anchor_text` realmente existe no abstract original. Se não houver match de string, o `# $suspicion_score` sobe automaticamente para 1.0.

Critérios avaliados:

- (a) O excerto de campos tipo `text` existe literalmente ou como paráfrase próxima no abstract?
- (b) Campos `code` ou  `chain` são sustentados pelo excerto, não apenas inferidos?
- (c) Os tipos de relação estão sendo respeitados aos definidos pelo template (e.g. ENABLES , INFLUENCES , CONSTRAINS , CONTESTED-BY , RELATES-TO)?
- (d) A direção da chain, e seus tipos de relação,  obedece às regras linguísticas do template?
- (e) Campos opcionais têm suporte textual?
- IMPORTANTE: Os critérios avaliados devem ser flexíveis para se adaptar a templates diferentes. 

**Revisão humana (opcional).** O pesquisador abre o `.synr`, mantém, modifica ou remove as tags `# $<field>:`. Nenhum estado adicional é necessário — a ausência da tag significa "manter valor original", e sua presença significa "substituir por esta sugestão".

### 3.4 Fase 3 — Normalização

Opera **exclusivamente sobre códigos** (nós de `chain`, valores de `code`), com foco em redução de granularidade. Quando invocada, o coder assume que o conteúdo atual do `.synr` é a verdade corrente (eventuais edições humanas da fase 2 já foram incorporadas ao decidir rodar a fase 3).

Fluxo:

1. Construir inventário global de códigos do corpus com frequência e posições.
2. Aplicar regras determinísticas (case, separadores, deduplicação lexical exata).
3. **Otimização por Cluster:** Em vez de enviar o inventário global de uma vez, o Synesis-Coder pode agrupar códigos por similaridade semântica (usando embeddings leves) antes de invocar o LLM.
4. Invocar o LLM normalizador para grupos residuais, solicitando `suggested_canonical`, `merge_confidence` e `reason`.
5. Emitir sugestões como blocos `# REVISION` em cada ITEM afetado, propondo substituições via `# $chain:`, `# $code:`.

A revisão humana opera igual à da fase 2: edita o `.synr`, mantém ou ajusta tags.

IMPORTANTE: O processamento do arquivo ONTOLOGY segue fase própria já definida pelo parâmetro ONTOLOGY. As 4 fases propostas neste estudo dizem respeito ao processamento de anitação syn apenas. 

### 3.5 Fase 4 — Incorporação

Fase determinística, sem LLM. Lê o `.synr` final e:

1. Para cada ITEM, aplica as tags `# $<field>:` encontradas, substituindo os valores correspondentes.
2. **Tratamento de Erros:** Se uma edição humana no `.synr` quebrou a sintaxe (ex: um erro de indentação ou um token não definido na ontologia), o Coder deve abortar a incorporação daquele `ITEM` específico e emitir um alerta, preservando a versão anterior.
3. **Check de Integridade:** Antes de aplicar o "commit" final no arquivo `.syn`, o Coder deve rodar o parser (`synesis.lark`) sobre a sugestão contida na tag `# $<field>:`.
4. Remove todos os blocos `# REVISION`.
5. Consolida as métricas do cabeçalho (ACS por fase, contagens de revisão) e as grava no `.syn` final.
6. Aplica o mapeamento de normalização de códigos em cadeia a todas as ocorrências no corpus.

O resultado é um `.syn` limpo, compilável e pronto para ser incluído no projeto via `.synp`.

---

## 4. Formato `.synr`

Um `.synr` é um `.syn` válido pela gramática Synesis. A diferença é a presença de duas famílias de comentários qualificados:

- **Cabeçalho do arquivo:** metadados mínimos da fase atual.
- **Blocos `# REVISION` por ITEM:** sugestões propostas pela fase 2 ou 3.

### 4.1 Cabeçalho

Três tags no topo do arquivo, suficientes para rastreabilidade sem verbosidade:

```
# $phase: critique
# $model: claude-sonnet-4-6
# $timestamp: 2026-04-24T14:23:01Z
```

### 4.2 Bloco `# REVISION`

Colocado no final do bloco ITEM, separado do conteúdo por uma linha em branco. Começa com `# REVISION` seguido de tags `# $var: value`. A presença da tag `# $<field>:` indica proposta de substituição; a ausência indica "manter original".

### 4.3 Exemplo baseado em `social_acceptance.synt`

```synesis
# $phase: critique
# $model: claude-sonnet-4-6
# $timestamp: 2026-04-24T14:23:01Z

SOURCE @chen2023
    description: Quantitative study of community trust and environmental concern as predictors of participation in offshore wind projects in coastal China
    epistemic_model: Technology Acceptance Model
    method: survey
END SOURCE

ITEM @chen2023
    text: Community trust and environmental concern are the most important factors determining willingness to participate in offshore wind projects
    note: *complex* Dual mechanism: Trust and Environmental_Concern independently enable participation via complementary pathways
    chain: Trust -> ENABLES -> Participation

    # REVISION
    # $suspicion_score: 0.84
    # $reason: wrong_direction
    # $note: *complex* Dual mechanism: Trust and Environmental_Concern independently influences participation via complementary pathways
    # $chain: Trust -> INFLUENCES -> Community_Participation
END ITEM

ITEM @chen2023
    text: Community trust and environmental concern are the most important factors determining willingness to participate in offshore wind projects
    note: Environmental_Concern is a co-prerequisite alongside Trust
    chain: Environmental_Concern -> ENABLES -> Participation

    # REVISION
    # $suspicion_score: 0.18
    # $reason: none
END ITEM
```

No primeiro ITEM, a fase 2 sugere mudar `ENABLES` para `INFLUENCES` e `Participation` para `Community_Participation`. O segundo ITEM é considerado correto (score baixo, nenhuma alteração).

### 4.4 Arquivo `.syn` final após fase 4

Após incorporação, o `.syn` final inclui apenas métricas no cabeçalho; os blocos `# REVISION` são removidos e as substituições aplicadas:

```synesis
# $metrics.acs_extraction: 0.71
# $metrics.acs_critique: 0.84
# $metrics.acs_normalization: 0.87
# $stats.items_total: 34
# $stats.items_flagged: 7
# $stats.normalizations_applied: 21

SOURCE @chen2023
    ...
END SOURCE

ITEM @chen2023
    text: Community trust and environmental concern are the most important factors determining willingness to participate in offshore wind projects
    note: *complex* Dual mechanism: Trust and Environmental_Concern independently enable participation via complementary pathways
    chain: Trust -> INFLUENCES -> Community_Participation
END ITEM
...
```

---

## 5. Codificação incremental

Cada fase recebe um arquivo e o trata como verdade corrente. As consequências práticas:

- **Simplicidade de política.** Não há fila de revisão separada, estado oculto ou `decision: pending`. O estado é o próprio `.synr`.
- **Reversibilidade.** Versões intermediárias do `.synr` podem ser arquivadas; rodar uma fase é idempotente se o arquivo de entrada não muda.
- **Humano no loop sem orquestrador.** Um editor de texto é suficiente para revisão: manter, editar ou apagar tags.
- **Paralelismo natural.** Fase 1 em lote, fase 2 em lote; fase 3 é global ao corpus; fase 4 é determinística.

É compilação incremental aplicada à anotação qualitativa — cada fase é um passe que enriquece a representação sem duplicar trabalho anterior.

---

## 6. Modelos e custos

| Fase | Papel        | Modelos indicados                          | Erro característico                            |
| ---- | ------------ | ------------------------------------------ | ---------------------------------------------- |
| 1    | Extrator     | Gemini 2.5 Pro, Claude Sonnet/Opus, GPT-4o | Over-extraction, confusão mecanismo/associação |
| 2    | Criticador   | Claude Sonnet, Gemini Flash, GPT-4o-mini   | Rejeição falsa em paráfrases legítimas         |
| 3    | Normalizador | Qwen3 7B/14B (Ollama), Gemma 3             | Colapso semântico de fatores distintos         |
| 4    | Incorporador | — (determinístico)                         | Perda de tag mal formatada                     |

**Custo estimado para o corpus ERSS (484 abstracts):**

| Fase      | Tokens/abstract          | Custo total |
| --------- | ------------------------ | ----------- |
| 1         | ~3.000 (Gemini 2.5 Pro)  | ~$10        |
| 2         | ~3.500 (Claude Sonnet)   | ~$7         |
| 3         | ~3.000 (Qwen3 14B local) | $0          |
| 4         | —                        | $0          |
| **Total** |                          | **~$17**    |

A escolha de modelos deve ser validada empiricamente em 20–30 abstracts antes do corpus completo.

---

## 7. Métricas ACS por fase

As métricas M1–M8 (definidas em `synesis-coder-quality-control`) são computadas ao final de cada transição. Valores `Δ(ACS₂ − ACS₁)` e `Δ(ACS₃ − ACS₂)` por abstract permitem avaliar empiricamente o ganho de cada fase.

| Métrica                            | Fase 1                 | Fase 2                | Fase 3      |
| ---------------------------------- | ---------------------- | --------------------- | ----------- |
| M1 (QFS) Quote Fidelity            | Base                   | Melhora               | Inalterada  |
| M2 (CGR) Chain Groundedness        | Base                   | Melhora               | Inalterada  |
| M3 (CES) Causal Evidence Strength  | Base                   | Melhoria principal    | Inalterada  |
| M4 (GCC) Guideline Compliance      | Base                   | Verificação adicional | Inalterada  |
| M5 (CRR) Code Reuse Rate           | Base                   | Inalterada            | Melhora     |
| M6 (OFFR) Optional Field Fill Rate | Potencialmente inflada | Reduz                 | Inalterada  |
| M7 (RTF) RELATES-TO Frequency      | Base                   | Pode reduzir          | Inalterada  |
| M8 (ACS) Composto                  | ACS₁                   | ACS₂ ≥ ACS₁           | ACS₃ ≥ ACS₂ |

Se `ACS₂ < ACS₁` em um piloto, o prompt da fase 2 está destrutivo e precisa ser recalibrado antes de ser aplicado ao corpus completo.

---

## 8. Implementação no Synesis-Coder

Sobre a arquitetura atual (`item`/`abstract`/`document`/`ontology`/`suggest`), a proposta requer:

- Parâmetro de CLI `--phase {1|2|3|4}` (ou `extract|critique|normalize|incorporate`) caso vazio assume automaticamente extract.
- Validador de `.env` específico por fase, com mensagens instrucionais.
- Leitor/escritor de `.synr`: preserva o `.syn` subjacente, anexa blocos `# REVISION` por ITEM, escreve o cabeçalho de três linhas.
- Prompt de crítica (fase 2) parametrizado pelos `FIELD` + `GUIDELINES` do template carregado.
- Inventário de códigos cross-file para fase 3, usar arquivo TXT comum.
- Incorporador da fase 4: parse do `.synr`, aplicação de tags, consolidação de métricas, emissão do `.syn` final.

Esforço estimado: 2–3 semanas sobre a base atual.

---

## 9. Conclusão

Quatro fases fixas, seleção por parâmetro de CLI, zero mudanças na gramática Synesis. As GUIDELINES por campo no `.synt` continuam sendo a única fonte de verdade para extração. O `.synr` — um `.syn` com blocos `# REVISION` ao final de cada ITEM — serve como artefato intermediário humanamente legível, editável em qualquer editor de texto e compilável pelo Synesis padrão.

A política de revisão é reduzida ao mínimo: o arquivo é o estado. Manter, editar ou remover tags é toda a interface necessária. A fase 4 aplica as sugestões presentes, remove os comentários de revisão e emite um `.syn` final com métricas consolidadas no cabeçalho — artefato auditável e pronto para publicação.

O princípio de **codificação incremental** — inspirado em compilação incremental, com um giro original para anotação qualitativa assistida por LLM — é a contribuição metodológica central deste pipeline.

---

## 10. Plano de Implementação

### 10.1 Análise de impacto no ecossistema

Contratos externos que **não podem** ser quebrados:

| Contrato | Dono | Risco se quebrado |
|---|---|---|
| `synesis-coder item --project --bibref --text` (stdout = bloco ITEM) | synesis-explorer (Ctrl+Shift+I) | CRÍTICO — extensão VSCode quebra |
| `synesis.load()` aceita `.syn` com comentários `#` | synesis/synesis.lark | Já garantido — comentários são `%ignore` |
| Subcomandos `abstract`, `document`, `ontology`, `suggest`, `finetune` | synesis-docs-sources/pt/howto/synesis_coder.qmd | ALTO — usuários CLI quebram |
| `SYNESIS_CODER_MODEL`, `ANTHROPIC_API_KEY`, `SYNESIS_CODER_BACKEND` | .env.example | ALTO — instalações existentes quebram |
| Token tracking via `token_usage.py` | Modos verbose | MÉDIO — métricas reportadas mudam |

Propagação multi-repo necessária:

| Repo | Mudança |
|---|---|
| synesis-coder | Novos subcomandos + `.synr` I/O + validador `.env` por fase |
| synesis-explorer | Adicionar `.synr` em `package.json:languages.extensions` |
| synesis-lsp | Adicionar `.synr` em `workspace_diagnostics.py:79` e demais matchings de sufixo |
| synesis-docs-sources | Documentar fluxo de 4 fases em `pt/howto/synesis_coder.qmd` e `en/` |
| synesis (compilador) | Nenhuma — comentários já são `%ignore` |

### 10.2 Decisão arquitetural

**Modos atuais permanecem intactos.** Adicionamos **três novos subcomandos** dedicados ao pipeline:

| Subcomando | Papel | Status |
|---|---|---|
| `item`, `document`, `abstract`, `ontology`, `suggest`, `finetune` | Modos existentes | INALTERADOS |
| `critique` (NOVO) | Fase 2 — gera `.synr` com blocos `# REVISION` | NOVO |
| `normalize` (NOVO) | Fase 3 — sugere canonicalização de códigos | NOVO |
| `incorporate` (NOVO) | Fase 4 — aplica `# REVISION`, emite `.syn` final + métricas | NOVO |

A Fase 1 (Extração) reusa `item`/`document` existentes, emitindo `.syn` normalmente. O subcomando `critique` é responsável por ler o `.syn` produzido e gerar o `.synr`. Não há flag adicional em `document`.

### 10.3 Arquivos a modificar / criar

**Em `synesis-coder/` (novos):**

- `synesis_coder/synr_io.py` — Reader/writer de `.synr`. Funções: `parse_synr(path)`, `write_synr(path, doc)`, `extract_revision_tags(item_block)`. Regex `^\s*#\s*\$([\w.]+):\s*(.+)$`.
- `synesis_coder/modes/critique_mode.py` — `process_critique()`.
- `synesis_coder/modes/normalize_mode.py` — `process_normalize()`.
- `synesis_coder/modes/incorporate_mode.py` — `process_incorporate()` (determinístico, sem LLM).
- `tests/test_synr_io.py`, `test_critique_mode.py`, `test_normalize_mode.py`, `test_incorporate_mode.py`.

**Em `synesis-coder/` (editados):**

- `synesis_coder/cli.py` — três novos subcomandos + helper `_validate_phase_env(phase_name)`.
- `synesis_coder/llm_client.py` — variáveis `SYNESIS_CODER_<PHASE>_MODEL` e `_API_KEY` com **fallback para `SYNESIS_CODER_MODEL`** (retrocompatibilidade).
- `synesis_coder/prompt_builder.py` — `build_critique_prompt()` e `build_normalization_prompt()`, parametrizados pelas `GUIDELINES` do `.synt`.
- `.env.example` — nova seção com variáveis por fase.
- `CHANGELOG.md` — entrada `[0.3.0]`.

**Em outros repos:**

- `synesis-explorer/package.json:28` — adicionar `.synr` em `languages.extensions`.
- `synesis-lsp/synesis_lsp/workspace_diagnostics.py:79` — adicionar `.synr`.
- `synesis-lsp/synesis_lsp/template_diagnostics.py:172,184` — aceitar `.synr` como alias de `.syn`.
- `synesis-lsp/synesis_lsp/rename.py:208,222-227` — tratar `.synr` em rename.
- `synesis-docs-sources/pt/howto/synesis_coder.qmd` + mirror `en/`.

### 10.4 Compatibilidade retroativa (garantias)

1. `synesis-coder item` mantém assinatura e stdout idênticos.
2. `SYNESIS_CODER_MODEL` continua sendo o modelo default; fallback quando variável por fase ausente (logado em DEBUG).
3. `document` e `abstract` continuam emitindo `.syn` exclusivamente.
4. `.synr` é superset de `.syn`: `synesis.load()` carrega sem erro, comentários ignorados.
5. `TokenUsage` instanciado por fase; output individual preserva estrutura atual.

### 10.5 Etapas de implementação

Ordem sugerida (cada etapa = PR isolado, mergeável independente):

1. **Reader/writer `.synr`** (sem LLM). Testes: round-trip idempotente; `synesis.load()` aceita `.synr` gerado.
2. **Validador `.env` por fase**. Testes: mensagens de erro instrucionais por variável ausente.
3. **Subcomando `incorporate`** (determinístico). Valida cada substituição via `synesis.load()` antes de aplicar; rollback per-item se quebrar. **Primeiro porque estabiliza o formato `.synr` sem depender de LLM.**
4. **Subcomando `critique`** (LLM). Prompt derivado de `FIELD` + `GUIDELINES` do template. Testes de integração com `case-studies/Sociology/Social_Acceptance/social_acceptance.synt`.
5. **Subcomando `normalize`** (LLM). Inventário cross-file em txt + clusterização lexical determinística antes do LLM.
6. **Propagação ecossistema**: `synesis-explorer/package.json`, `synesis-lsp/`.
7. **Documentação**: `synesis-docs-sources/pt/`, `en/`.
8. **CHANGELOG e bump** para `0.3.0`.

### 10.6 Plano de testes

**Unitários (cada PR):**

- `test_synr_io.py`: round-trip, parsing de tags, edge cases.
- `test_critique_mode.py`: mock LLM; `# REVISION` apenas em items com score ≥ threshold.
- `test_normalize_mode.py`: deduplicação determinística + LLM apenas em grupos residuais.
- `test_incorporate_mode.py`: aplicação de tags, rejeição de tag inválida, métricas.
- `test_phase_env_validator.py`: mensagens de erro por fase.

**Integração (case-studies):**

Pipeline E2E com 3 abstracts de `case-studies/Sociology/Social_Acceptance/`:

```bash
synesis-coder document --project ... --output out.syn
synesis-coder critique --project ... out.syn         # gera out.synr
synesis-coder normalize --project ... out.synr       # atualiza out.synr
synesis-coder incorporate out.synr                   # gera out.syn final
```

**Critério:** `synesis.load()` aceita `out.syn` final sem erros; métricas `acs_*` no cabeçalho.

**Regressão (CRÍTICOS):**

- **Extension contract**: `synesis-coder item --project --bibref --text` com baseline byte-comparison.
- **Backwards-compat `.env`**: instalação sem `SYNESIS_CODER_*_MODEL` continua funcionando.
- **Subcomandos existentes**: `pytest tests/test_item_mode.py tests/test_abstract_mode.py tests/test_document_mode.py tests/test_ontology_mode.py` passa inalterado.

**Ecossistema:**

- Abrir `.synr` no VSCode → syntax highlighting OK.
- LSP carrega `.synr` no workspace → sem warnings de extensão desconhecida.
- `synesis check` em `.synp` com `INCLUDE ANNOTATIONS "*.synr"` → compila sem erros.

### 10.7 Riscos e mitigações

| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| Quebra do contrato `synesis-coder item` | Baixa | Crítico | Não tocar em `item_mode.py` nem em `cli.py:item`. Teste de regressão byte-comparison. |
| Incompatibilidade `.synr` ↔ `synesis.load()` | Baixa | Alto | Etapa 1 valida round-trip; `incorporate` valida cada substituição antes de aplicar. |
| Edição humana quebra sintaxe do `.synr` | Média | Médio | `incorporate` faz parse antes de commit; rollback per-item com warning. |
| Esquecimento de propagação LSP/Explorer | Média | Médio | Etapa 6 dedicada. Checklist de arquivos no PR. |
| Fallback `.env` mascara erro de config | Baixa | Baixo | Logar em DEBUG quando fallback é usado; documentar em `.env.example`. |
| Custo subestimado (2–3 semanas) | Alta | Baixo | Implementar incrementalmente; cada etapa é mergeável independente. |
| Token tracking misturado entre fases | Média | Baixo | Instanciar `TokenUsage` por fase; reportar separado em `--format verbose`. |

---

*Christian Maciel De Britto | OTIC/USP | Abril 2026*
