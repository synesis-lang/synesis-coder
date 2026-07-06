# Estudo de Viabilidade — Modo `suggest`

**Data:** 2026-04-06  
**Escopo:** Análise de viabilidade para implementação de um novo modo `suggest` no synesis-coder,
com foco em execução local via Ollama usando modelos pequenos (gemma4:e2b ou equivalente).

---

## 1. Motivação

Os modos existentes (`item`, `abstract`, `document`, `ontology`) exigem que o LLM gere
**sintaxe Synesis válida** — blocos estruturados com campos obrigatórios, tipos, relações e
restrições derivadas do template. Isso exige modelos de 7B+ parâmetros com forte
instruction-following. Modelos menores falham sistematicamente nessa tarefa (confirmado
empiricamente com gemma3:4b e qwen3:4b em 2026-04-04).

O modo `suggest` tem um objetivo completamente diferente: **assistir o pesquisador antes da
codificação**, indicando quais códigos existentes podem ser relevantes para um trecho
de texto, ou — quando nenhum encaixa — sugerindo um conceito novo. A saída é texto
livre — sem sintaxe, sem compilação, sem validação.

**Objetivo estratégico:** reduzir a granularidade do corpus. Em vez de criar um código novo
para cada nuance, o pesquisador é incentivado a reusar conceitos já presentes, mantendo
consistência analítica. O modo `suggest` é o mecanismo que torna essa reutilização prática
em projetos com centenas de códigos.

---

## 2. Dados reais do projeto `social_acceptance`

Antes de desenhar o prompt, é essencial entender a escala real dos dados:

| Dimensão | Valor |
|---|---|
| Total de códigos | **1.384** |
| Tópicos (categorias) | **32** |
| Código mais frequente | Acceptance (396 ocorrências) |
| Entradas de ontologia | **1.388** (todas com `ontology_description`) |
| Maior tópico | Governance (209 códigos) |
| Descrição do projeto | 537 caracteres |

**Implicação crítica:** não é possível enviar 1.384 códigos no prompt de um modelo de 2B.
Isso seria ~5.000 tokens — só a lista consumiria 60% do contexto de 8K do gemma4:e2b.
É necessária uma estratégia de pré-filtragem obrigatória, não opcional.

### Distribuição por tópico (top 10)

| Tópico | Códigos |
|---|---|
| Governance | 209 |
| Technology | 189 |
| Worldview | 162 |
| Economics | 142 |
| Social | 131 |
| Environment | 85 |
| Research | 84 |
| Infrastructure | 74 |
| Planning | 62 |
| Knowledge | 54 |

---

## 3. Diferença fundamental em relação aos modos existentes

| Aspecto | Modos item/abstract/document | Modo suggest |
|---|---|---|
| Saída esperada | Sintaxe Synesis válida | Texto livre estruturado |
| Validação | `synesis.load()` + loop de correção | Nenhuma |
| Falha típica de modelos pequenos | Campos inventados, sintaxe quebrada | Irrelevante |
| Sensibilidade ao template | Alta (campos, tipos, relações) | Baixa (só códigos) |
| Tamanho mínimo viável de modelo | ~7-8B | ~2B efetivos |
| Custo por chamada | Alto (contexto longo, output longo) | Baixo |

---

## 4. O que o modo suggest precisa fazer

1. Receber um trecho de texto do pesquisador (curto — frase ou parágrafo)
2. Carregar o `code_index` e o `topic_index` do projeto
3. **Pré-filtrar** a lista de códigos (obrigatório em projetos grandes)
4. Enviar ao LLM: códigos filtrados + texto
5. Receber: quais códigos são relevantes e por quê
6. Exibir as sugestões de forma legível

---

## 5. Contexto disponível em `load_project()`

O `project_loader.py` já entrega tudo o que o modo `suggest` precisa:

```python
ctx["code_index"]["codes"]       # List[str] — 1.384 códigos (CODE + CHAIN)
ctx["code_index"]["stats"]       # Dict[str, int] — frequência de cada código
ctx["topic_index"]["topics"]     # List[str] — 32 tópicos
ctx["topic_index"]["topic_members"]  # Dict[str, List[str]] — códigos por tópico
ctx["ontology_index"]            # Dict[str, OntologyNode] — 1.388 entradas com:
                                 #   .fields["ontology_description"]  — descrição semântica
                                 #   .fields["topic"]                 — tópico do código
ctx["project_description"]       # str — contexto metodológico
ctx["chain_relations"]           # Dict[str, str] — relações CHAIN
```

**Recurso subutilizado nos modos existentes:** o `ontology_index` contém descrições
semânticas ricas para cada código. Nos modos de geração (item, abstract) isso não é usado
porque inflaria o prompt. No modo `suggest`, onde o prompt é curto, podemos usar
seletivamente essas descrições para melhorar a qualidade da sugestão.

### Parâmetros de carregamento

```python
load_project(project_path, load_annotations=True, load_ontology=True)
#                          ↑ para code_index        ↑ para ontology_description
```

Diferente dos outros modos, `load_ontology=True` é necessário para ter acesso às
descrições semânticas dos códigos.

---

## 6. Estratégia de pré-filtragem de códigos

### 6.1 O problema

1.384 códigos é inviável para qualquer modelo em um prompt conciso. Mesmo modelos
grandes perdem foco com listas enormes.

### 6.2 Solução: filtragem em duas camadas

**Camada 1 — Filtro por tópico (estrutural):**

Os 32 tópicos agrupam os 1.384 códigos em categorias semânticas. O LLM pode ser
usado em dois passos:

1. **Passo 1:** Enviar apenas os 32 nomes de tópico → modelo identifica 2-4 tópicos
   relevantes (~32 tokens de lista)
2. **Passo 2:** Enviar apenas os códigos desses 2-4 tópicos → modelo sugere
   códigos específicos

Isso reduz a lista de ~1.384 para ~60-200 códigos no passo 2.

**Camada 2 — Filtro por frequência (quantitativo):**

Dentro dos tópicos selecionados, ordenar por frequência (`stats`) e enviar apenas
os top-N (ex: 50). Códigos com frequência 1 são outliers menos úteis para reutilização.

### 6.3 Alternativa: passo único com enriquecimento

Em vez de dois passos LLM, enviar os top-80 códigos globais (por frequência)
**com descrição curta** da ontologia:

```
Codes (80 most used):
  Acceptance (396) — social acceptance of energy transition technologies
  Deployment (220) — implementation of energy technologies
  Policy (118) — regulatory and policy frameworks
  ...
```

Custo: ~1.200 tokens (80 códigos × ~15 tokens cada). Cabe no budget de 8K.

### 6.4 Recomendação

**Implementar a abordagem de dois passos** (tópico → código) como estratégia
principal, com fallback para passo único em projetos pequenos (< 100 códigos):

```python
codes = ctx["code_index"]["codes"]
if len(codes) <= 100:
    # Passo único — enviar todos
    return _suggest_single_pass(ctx, text)
else:
    # Dois passos — primeiro tópicos, depois códigos do tópico
    return _suggest_two_pass(ctx, text)
```

A abordagem de dois passos tem vantagens adicionais:
- Cada chamada LLM é mais simples → melhor qualidade com modelos pequenos
- O passo 1 (32 tópicos) cabe em qualquer contexto
- O passo 2 é contextualizado pelo tópico → sugestões mais precisas
- Latência total: ~2-4s no Ollama local (modelo de 2B é rápido)

---

## 7. Design do prompt para modelos pequenos

### 7.1 Passo 1 — Identificação de tópicos (projetos grandes)

```
[system]
You are a research assistant. Given a text excerpt and a list of research topics,
identify the 2-4 most relevant topics. Reply with ONLY the topic names, one per line.

Topics:
Actors, Aesthetics, Behavior, Communication, Economics, Energy_Resources, ...

[user]
Text: "Local ownership models significantly reduce opposition to CCS technology."
```

- **Tokens de sistema:** ~80 (instrução) + ~50 (32 tópicos) = ~130 tokens
- **max_tokens:** 64
- **temperature:** 0.0 (determinístico — lista fechada)

### 7.2 Passo 2 — Sugestão de códigos (filtrado por tópico)

```
[system]
You are a research assistant. Given a text excerpt and a list of analytical codes,
suggest 3-5 existing codes that best match the text. For each, explain briefly why.
If no existing code fits, suggest ONE new code marked [NEW].

Reply format:
• Code_Name — brief reason (max 15 words)

Codes:
  Acceptance (396) — social acceptance of energy transition technologies
  Community_Benefit (28) — economic benefits distributed to local community
  Local_Ownership (15) — community ownership of energy infrastructure
  Opposition (42) — resistance or rejection of energy projects
  ...

[user]
Text: "Local ownership models significantly reduce opposition to CCS technology."
```

- **Tokens de sistema:** ~120 (instrução) + ~500-800 (códigos com descrição) = ~700 tokens
- **max_tokens:** 256
- **temperature:** 0.3

### 7.3 Passo único — projetos pequenos (< 100 códigos)

```
[system]
You are a research assistant. Given a text excerpt and a list of analytical codes,
suggest 3-5 existing codes that best match the text. For each, explain briefly why.
If no existing code fits, suggest ONE new code marked [NEW].

Prefer existing codes. Reply format:
• Code_Name — brief reason (max 15 words)

Project: {project_description truncada a 200 chars}

Existing codes ({N} total):
  Code_A (12), Code_B (8), Code_C (5), ...

[user]
Text: "{texto}"
```

### 7.4 Princípios de design para modelos de 2B

1. **Regras como lista numerada curta** — modelos pequenos seguem listas melhor que parágrafos
2. **Formato de resposta explícito** — "• Code — reason" é simples de seguir
3. **"One per line"** — evita que o modelo colapse tudo em um parágrafo
4. **Sem negações complexas** — "do NOT generate SOURCE" confunde; melhor omitir o conceito
5. **Exemplos no prompt** — usar 1-shot se a qualidade for instável

---

## 8. Enriquecimento via ontologia

O `ontology_index` é o diferencial do modo `suggest` em relação a uma busca simples.
Cada código tem:

| Campo | Exemplo (`cost`) | Utilidade |
|---|---|---|
| `ontology_description` | "Economic factor representing financial expenditure..." | Contexto semântico para o LLM |
| `topic` | "Economics" | Filtragem por tópico |
| `confidence` | "HIGH" | Priorizar códigos bem-definidos |
| `rgt_element_a/b` | "Low_Cost" / "High_Cost" | Dimensão bipolar (futuro) |

**No passo 2**, incluir `ontology_description` (truncada a ~80 chars) transforma a lista
de códigos de rótulos opacos em conceitos compreensíveis:

```
Sem ontologia:   Community_Benefit (28)
Com ontologia:   Community_Benefit (28) — economic benefits distributed to local community
```

Isso é especialmente valioso para modelos pequenos que não têm conhecimento prévio do domínio.

---

## 9. Formato de saída

### Formato plain (padrão)

```
Relevant topics: Social, Governance

Suggested codes:

• Local_Ownership (15) — text directly discusses ownership models
• Opposition (42) — "reduce opposition" is core claim
• CCS_Support (8) — CCS acceptance is the subject
• Community_Benefit (28) — ownership implies distributed benefits
```

### Formato verbose

```
# synesis-coder suggest
# project: social_acceptance
# model: gemma4:e2b (local)
# codes in project: 1384 (filtered to 67 via topics: Social, Governance)

Suggested codes:

• Local_Ownership (15) — text directly discusses ownership models
• Opposition (42) — "reduce opposition" is core claim
• CCS_Support (8) — CCS acceptance is the subject
• Community_Benefit (28) — ownership implies distributed benefits
```

O formato verbose inclui metadados de filtragem para que o pesquisador entenda
quais tópicos foram considerados e quantos códigos foram avaliados.

---

## 10. Avaliação de modelos locais

### 10.1 gemma4:e2b (Gemma 4 Effective 2B)

- **Parâmetros ativos:** ~2B (MoE com 8B total)
- **VRAM:** ~3 GB em Q4_K_M — cabe na RTX 3050 6GB com folga
- **Contexto:** 8K tokens
- **Adequação:** a tarefa é escolher de uma lista (classificação), não gerar
  estrutura — dentro das capacidades de 2B

**Preocupações específicas:**
- Pode ignorar a marca `[NEW]` → mitigação: pós-processamento que verifica se
  os códigos sugeridos existem no `code_index`
- Passo 1 (tópicos) pode falhar com nomes opacos → mitigação: tópicos são
  autodescritivos ("Economics", "Governance", "Social")

### 10.2 qwen3:4b

- **VRAM:** ~5 GB Q4 (no limite da RTX 3050)
- **Vantagem:** `<think>` reasoning pode melhorar justificativas
- **Melhor que gemma4:e2b** para o passo 2 (seleção com justificativa)

### 10.3 Ranking para o modo suggest

| Modelo | VRAM Q4 | Qualidade esperada | RTX 3050? |
|---|---|---|---|
| qwen3:4b | ~5 GB | Boa | No limite |
| gemma4:e2b | ~3 GB | Adequada | Confortável |
| gemma3:4b | ~5 GB | Boa | No limite |

**Recomendação:** gemma4:e2b como modelo padrão para `suggest`, com documentação
indicando qwen3:4b como upgrade para quem tem mais VRAM.

---

## 11. Fluxo de implementação

```
cli.py
  └── suggest  ← novo subcomando
        │
        └── modes/suggest_mode.py   ← process_suggest()
              │
              ├── project_loader.load_project(load_ontology=True)  ← sem modificação
              │
              ├── _filter_codes_by_topic()    ← NOVO: passo 1 (LLM ou direto)
              │     └── LLMClient.call()      ← reutiliza cliente existente
              │
              ├── _build_enriched_code_list() ← NOVO: monta lista com ontology_description
              │     └── usa ontology_index
              │
              ├── prompt_builder.build_suggest_prompt()  ← NOVA função
              │
              └── LLMClient.call()  ← passo 2 (sugestão final)
                    (sem validator — não há sintaxe para validar)
```

### Arquivos a criar/modificar

| Arquivo | Ação | O que muda |
|---|---|---|
| `synesis_coder/modes/suggest_mode.py` | **Criar** | `process_suggest()`, `_filter_codes_by_topic()`, `_build_enriched_code_list()` |
| `synesis_coder/prompt_builder.py` | **Adicionar** | `build_suggest_prompt()`, `build_topic_filter_prompt()` |
| `synesis_coder/cli.py` | **Adicionar** | Subcomando `suggest` |

Nenhuma modificação em `llm_client.py` ou `project_loader.py`.

### Pós-processamento da resposta (em `suggest_mode.py`)

O LLM retorna texto livre, mas convém fazer verificação mínima:

```python
def _postprocess_suggestions(raw_output: str, code_index: dict) -> str:
    """Marca códigos que não existem no projeto como [NEW] se o LLM esqueceu."""
    existing = set(c.lower() for c in code_index["codes"])
    lines = []
    for line in raw_output.strip().split("\n"):
        # Extrair nome do código da linha (ex: "• Code_Name — reason")
        if line.strip().startswith("•"):
            code = line.split("—")[0].replace("•", "").strip()
            if code.lower() not in existing and "[NEW]" not in line:
                line = line.replace(code, f"[NEW] {code}")
        lines.append(line)
    return "\n".join(lines)
```

---

## 12. Parâmetros do subcomando `suggest`

```bash
synesis-coder suggest \
  --project projeto.synp \
  --text "Local ownership models significantly reduce opposition to CCS technology." \
  [--model gemma4:e2b]        # sobrescreve SYNESIS_CODER_MODEL
  [--format plain|verbose]    # plain: só sugestões; verbose: + metadados
```

**Parâmetros removidos do plano anterior:**
- `--bibref`: removido — `suggest` é pré-codificação, bibref é irrelevante
- `--top-codes`: removido — a filtragem por tópico é mais inteligente que um corte
  arbitrário; o threshold interno é suficiente

---

## 13. Riscos e mitigações

| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| Passo 1 retorna tópico inexistente | Média | Baixo | Validar contra `topic_index`; fallback: top-5 tópicos por frequência |
| Modelo sugere código que não existe sem [NEW] | Alta | Baixo | Pós-processamento: cruzar com `code_index` |
| Modelo repete texto em vez de analisar | Média | Médio | Instrução "do not quote", `max_tokens=256` |
| Sugestões genéricas (sempre os mais frequentes) | Média | Médio | Incluir `ontology_description` para dar contexto semântico |
| Projeto sem ontologia (`.syno` vazio) | Baixa | Baixo | Fallback: lista de códigos sem descrição (como no plano original) |
| Projeto com < 10 códigos | Baixa | Nenhum | Passo único; sugestão de [NEW] mais provável e útil |
| Latência de dois passos no Ollama | Baixa | Baixo | gemma4:e2b gera ~50 tok/s na RTX 3050; total ~2-4s |

---

## 14. Integração futura com synesis-explorer

O modo `suggest` é um stepping stone natural para integração no synesis-explorer
(extensão VSCode). O fluxo de uso seria:

1. Pesquisador seleciona um trecho de texto no editor
2. Menu de contexto: "Suggest Codes"
3. Painel lateral exibe sugestões do `synesis-coder suggest`
4. Pesquisador seleciona um código sugerido
5. Aciona `synesis-coder item` com o código pré-selecionado para gerar o ITEM completo

Este fluxo já está alinhado com o plano de integração do synesis-coder no
synesis-explorer (documentado em memória). O modo `suggest` adiciona o passo de
triagem que torna o fluxo mais ágil para projetos com muitos códigos.

---

## 15. Conclusão

**O modo `suggest` é viável com gemma4:e2b**, com as seguintes premissas:

1. **Filtragem obrigatória** em projetos grandes — a abordagem de dois passos
   (tópico → código) reduz 1.384 códigos para ~60-200 antes de enviar ao LLM
2. **Enriquecimento via ontologia** — usar `ontology_description` para contextualizar
   os códigos no prompt, compensando a falta de conhecimento de domínio do modelo
3. **Prompt curto e direto** — < 800 tokens por passo, formato bullet com justificativa
4. **Pós-processamento** — verificação automática de códigos inexistentes (marca [NEW])
5. **Sem validação sintática** — a saída é texto livre, não Synesis

A tarefa "escolher de uma lista com justificativa" é classificação semântica simples —
dentro das capacidades de modelos de 2B efetivos. O `ontology_index` com 1.388
descrições semânticas é o diferencial que torna as sugestões úteis mesmo com modelos
pequenos.

**Próximo passo:** instalar `gemma4:e2b` via Ollama, implementar protótipo mínimo
(suggest_mode.py + build_suggest_prompt + CLI), e testar com o projeto
`social_acceptance` para validar empiricamente a qualidade das sugestões.
