# Synesis Language

## Plano de Implementação — Revisão 3.0

### `synesis-coder`: Processador de Anotações Qualitativas Guiado por Template

**Versão:** 3.0 | **Data:** Março 2026 | **Autor:** Christian Maciel De Britto — OTIC/USP

---

## 1. Contexto e Objetivo

O `synesis-coder` é um processador de codificação qualitativa guiado por template. Ele invoca o compilador Synesis como única interface com o projeto — para carregar template, extrair índices e validar output.

A proposta integra infraestruturas já existentes:

- O compilador Synesis (`synesis.load()`) para leitura de contexto e validação em memória
- Os scripts `abstract_processor10.py` e `interview_processor.py` como referência de arquitetura de pipeline
- O plano `synesis-mcp-plan.md` como arquitetura-alvo de longo prazo

> **Premissa central:** As `GUIDELINES` de cada campo `FIELD` no template `.synt` substituem os prompts manuais externos. O template torna-se o único ponto de verdade metodológica: estrutura, tipos, restrições e instruções de extração estão co-localizados no mesmo artefato versionável.

---

## 2. O que `synesis-coder` é e não é

| É | Não é |
|---|-------|
| Um pipeline de codificação guiado por template | Um agente autônomo com memória ou iniciativa própria |
| Um invocador de LLM com instruções derivadas do `.synt` | Um substituto para a decisão analítica do pesquisador |
| Um validador formal via compilador Synesis | Um sistema de execução ou runtime Synesis |
| Uma ferramenta invocável por linha de comando ou extensão VSCode | Um serviço persistente (isso é o MCP) |

---

## 3. Princípios Fundamentais

| # | Princípio | Implicação |
|---|-----------|-----------|
| 1 | **Acoplamento total ao compilador** | Toda leitura de template, projeto, bib e anotações ocorre via `synesis.load()`. Nenhuma leitura direta de arquivo por parsing próprio. Atualizações do `synesis` chegam automaticamente. |
| 2 | **Templates são dinâmicos** | Cada projeto tem seu próprio `.synt`. O `synesis-coder` não assume campos, escopos ou relações fixas. Um template pode não ter ONTOLOGY scope. Tudo é derivado de `result.template.field_specs` em tempo de execução. |
| 3 | **GUIDELINES como instruções primárias** | A GUIDELINES de cada campo é a instrução ao LLM. Fallback: DESCRIPTION. Se nem GUIDELINES nem DESCRIPTION existem: instrução genérica baseada no tipo de campo. |
| 4 | **Cases reais como testes** | Projetos em `d:\GitHub\case-studies\` são os fixtures de teste. `synesis init` gera o projeto mínimo para testes unitários básicos. Não criar fixtures fictícios. |
| 5 | **Programação procedural** | Funções puras sempre que possível. Classes apenas quando encapsulamento traz benefício claro (ex: `LLMClient` para gerenciar estado de rate limiting). Sem hierarquias. |
| 6 | **Consulta de índices antes de sugerir** | Antes de propor qualquer código/conceito em CHAIN ou CODE: consultar `CodeIndex`. Antes de propor tópico em campo TOPIC: consultar `TopicIndex`. A mesma lógica se aplica a qualquer campo com valores enumeráveis derivados do projeto. |
| 7 | **DESCRIPTION do projeto como contexto** | O bloco `DESCRIPTION ... END DESCRIPTION` do `.synp`, quando presente, é injetado no system prompt como contexto metodológico. Lido via `result.linked_project.project.description` — o compilador já expõe o valor processado. |
| 8 | **Eficiência máxima de tokens** | Prompts estáticos (instrução de sistema + campos do template + code index) são construídos uma única vez por sessão e marcados com `cache_control: {"type": "ephemeral"}` (Anthropic prompt caching). Apenas o conteúdo dinâmico (texto, bibref) é enviado a cada chamada. Objetivo: cache hit em todas as chamadas dentro de uma sessão. |
| 9 | **Credenciais em arquivo de ambiente** | A `ANTHROPIC_API_KEY` é lida exclusivamente de variáveis de ambiente. Um arquivo `.env.example` com instruções ao usuário é fornecido no repositório. O `.env` real é protegido por `.gitignore`. Nenhuma chave é hardcoded ou lida de `config.toml`. |

---

## 4. Dependência Total no Compilador

Todo acesso ao projeto passa por `synesis.load()`:

```python
import synesis

result = synesis.load(
    project_content=project_path.read_text(),
    template_content=template_path.read_text(),
    annotation_contents=existing_syn_files,   # Dict[filename, content]
    ontology_contents=existing_syno_files,    # Dict[filename, content] — pode ser {}
    bibliography_content=bib_content,         # pode ser None
)

# Tudo que o synesis-coder precisa vem daqui:
result.template.field_specs          # Dict[str, FieldSpec] — fonte da estrutura
result.linked_project.code_usage     # Dict[str, List[ItemNode]] — CodeIndex
result.linked_project.topic_index    # Dict[str, List[str]] — TopicIndex
result.linked_project.all_triples    # List[(A, REL, B)]
result.linked_project.ontology_index # Dict[str, OntologyNode]
result.bibliography                  # Dict[str, BibEntry]
result.success                       # bool
result.get_diagnostics()             # str — erros formatados
```

**Quando o compilador for atualizado**, o `synesis-coder` herda as mudanças automaticamente — sem necessidade de alteração.

> **Nota crítica:** o acesso correto é `result.template.field_specs` (não `result.linked_project.template.field_specs` — o atributo `template` está em `MemoryCompilationResult` diretamente, não em `linked_project`).

---

## 5. Templates Dinâmicos — Variações Observadas nos Cases Reais

A exploração de `d:\GitHub\case-studies\` revelou 4 templates distintos:

| Template | ONTOLOGY scope | CHAIN | GUIDELINES | ORDERED/ENUMERATED | Complexidade |
|----------|---------------|-------|------------|-------------------|-------------|
| `social_acceptance.synt` | ✓ (9 campos) | ✓ (5 relações EN) | ✓ (10 campos) | ✓ (aspect 0-15, dimension 0-4, confidence) | Alta |
| `aids_corpus.synt` | ✓ (2 campos) | ✓ (3 relações PT) | ✗ | ✗ | Média |
| `nave.synt` | ✓ (1 campo) | ✗ | ✗ | ✗ | Baixa |
| `thompson_bible.synt` | **AUSENTE** | ✗ | ✗ | ✗ | Mínima |

O `synesis-coder` trata cada caso corretamente ao derivar tudo do template:
- Se não há ONTOLOGY scope → modo `ontology` não disponível para esse projeto (aviso ao usuário)
- Se não há CHAIN field → não instrui geração de chains
- Se não há GUIDELINES → usa DESCRIPTION como fallback, ou instrução genérica por tipo
- Se não há ORDERED/ENUMERATED → não lista valores no prompt

---

## 6. Modos de Operação

| Modo | Entrada | Saída | Análogo legado |
|------|---------|-------|----------------|
| `item` | Trecho de texto + bibref | Um bloco `ITEM` (stdout) | Novo — uso interativo |
| `abstract` | Arquivo `.bib` | Arquivo `.syn` com SOURCE+ITEMs | `abstract_processor10.py` |
| `document` | Arquivo `.txt`/`.md` + bibref | Arquivo `.syn` com ITEMs | `interview_processor.py` |
| `ontology` | Projeto `.synp` com `.syn` existentes | Arquivo `.syno` com entradas ONTOLOGY | `semantic_memory_builder.py` + `topic_processor.py` |

### Modo `item` *(MVP real)*

```
# Invocação via linha de comando
synesis-coder item \
  --project projeto.synp \
  --bibref smith2024 \
  --text "Community trust and environmental concern are the most important factors..."

# Saída (stdout)
ITEM @smith2024
    text: Community trust and environmental concern are the most important factors determining willingness to participate in renewable energy projects.

    note: Dual mechanism — trust and environmental concern independently determine participation willingness

    chain: Community_Trust -> INFLUENCES -> Participation_Willingness
END ITEM
```

### Modo `ontology`

O fluxo atual de produção do `social_acceptance` é um pipeline de 4 etapas manuais:

```
1. abstract_processor → .syn (items com chains)
2. semantic_memory_builder → JSON (frequência, relações, co-fatores por código)
3. topic_processor → CSV (classificação: topic, aspect, dimension, confidence)
4. Compilação manual → .syno
```

Com `synesis-coder ontology`:

```
1. synesis-coder abstract → .syn  (Fase 2)
2. synesis-coder ontology → .syno (Fase 4)
```

---

## 7. Índices Derivados do Compilador

### 7.1 `CodeIndex` — Consulta obrigatória antes de sugerir códigos/chains

Antes de propor qualquer conceito em campos CHAIN ou CODE, o `synesis-coder` consulta o índice de códigos existentes:

```python
# result.linked_project.code_usage: Dict[str, List[ItemNode]]
def build_code_index(result: MemoryCompilationResult) -> dict:
    if not result.linked_project:
        return {"codes": [], "stats": {}, "empty": True}
    usage = result.linked_project.code_usage
    return {
        "codes": sorted(usage.keys()),
        "stats": {code: len(items) for code, items in usage.items()},
        "empty": len(usage) == 0,
    }
```

### 7.2 `TopicIndex` — Consulta obrigatória antes de sugerir tópicos

Para campos do tipo TOPIC, consultar os tópicos já definidos no projeto:

```python
# result.linked_project.topic_index: Dict[str, List[str]]
def build_topic_index(result: MemoryCompilationResult) -> dict:
    if not result.linked_project:
        return {"topics": [], "empty": True}
    ti = result.linked_project.topic_index
    return {
        "topics": sorted(ti.keys()),
        "topic_members": {t: sorted(members) for t, members in ti.items()},
        "empty": len(ti) == 0,
    }
```

### 7.3 `OntologyIndex` — Conceitos já definidos no .syno

Para o modo `ontology`, determinar quais códigos já têm entrada ONTOLOGY (para `--update`):

```python
# result.linked_project.ontology_index: Dict[str, OntologyNode]
already_defined = set(result.linked_project.ontology_index.keys())
codes_needing_ontology = [c for c in result.linked_project.code_usage
                          if c not in already_defined]
```

---

## 8. Arquitetura de Módulos (Procedural)

```
d:\GitHub\synesis-coder\
├── synesis_coder/
│   ├── __init__.py
│   ├── __main__.py          # Entry point: python -m synesis_coder
│   ├── cli.py               # Click CLI: item | abstract | document | ontology
│   │
│   ├── project_loader.py    # load_project(project_path) → dict (ctx completo)
│   │                        # Única função que chama synesis.load() para contexto.
│   │                        # Retorna: template, field_specs, code_index, topic_index, etc.
│   │
│   ├── prompt_builder.py    # Funções puras de construção de prompt por tipo de campo/modo
│   │                        # build_item_prompt(ctx, text, bibref) → List[Message]
│   │                        # build_ontology_prompt(ctx, code, semantic_ctx) → List[Message]
│   │
│   ├── llm_client.py        # LLMClient: estado de rate limiting + call()/call_async()
│   │                        # Única classe no projeto (necessária pelo estado de rate limiting)
│   │
│   ├── validator.py         # validate_and_fix(output, ctx, llm_client, max_tries) → (str, bool)
│   │                        # Chama synesis.load() para validar; devolve erros ao LLM
│   │
│   └── modes/
│       ├── item_mode.py     # process_item(project_path, bibref, text, format) → str
│       ├── abstract_mode.py # process_abstract(project_path, bib_path, output_dir, ...) → None
│       ├── document_mode.py # process_document(project_path, bibref, input, output) → None
│       └── ontology_mode.py # process_ontology(project_path, output_path, update) → None
│
├── pyproject.toml
├── tests/
│   └── test_item_mode.py    # Usa projects reais de d:\GitHub\case-studies\
├── abstract_processor10.py  # Mantido (legado, não modificar)
├── interview_processor.py   # Mantido (legado, não modificar)
├── semantic_memory_builder.py # Mantido (legado, não modificar)
├── topic_processor.py       # Mantido (legado, não modificar)
├── config.toml              # Mantido (legado)
└── synesis-coder-implementation-plan.md
```

**Princípio procedural aplicado:**
- `project_loader.py`: uma função `load_project()` que chama `synesis.load()` e retorna `dict` com todos os índices
- `prompt_builder.py`: funções puras, sem estado; recebem contexto e devolvem lista de mensagens
- `modes/`: cada modo é um módulo com uma função principal que orquestra o fluxo
- Única classe: `LLMClient` (necessária pelo estado de semáforo e rate limiting)

---

## 9. `project_loader.py` — Interface Central com o Compilador

```python
def load_project(project_path: Path, load_annotations: bool = True) -> dict:
    """
    Carrega o projeto via synesis.load() e retorna contexto completo.

    Retorna dict com:
        "result": MemoryCompilationResult
        "field_specs": Dict[str, FieldSpec]         — result.template.field_specs
        "source_fields": Dict[str, FieldSpec]       — filtrado por SCOPE SOURCE
        "item_fields": Dict[str, FieldSpec]         — filtrado por SCOPE ITEM
        "ontology_fields": Dict[str, FieldSpec]     — filtrado por SCOPE ONTOLOGY (pode ser {})
        "has_ontology_scope": bool                  — template define ONTOLOGY?
        "has_chain_field": bool                     — template tem campo CHAIN?
        "chain_relations": Dict[str, str]           — relações do campo CHAIN (se existe)
        "required_item": List[str]                  — campos REQUIRED no SCOPE ITEM
        "bundle_pairs": List[Tuple[str,str]]        — pares BUNDLE
        "code_index": dict                          — {"codes": [...], "stats": {...}, "empty": bool}
        "topic_index": dict                         — {"topics": [...], "topic_members": {...}}
        "ontology_index": Dict[str, OntologyNode]   — conceitos já definidos no .syno
        "project_description": Optional[str]        — conteúdo de DESCRIPTION...END DESCRIPTION do .synp
        "project_content": str
        "template_content": str
        "project_path": Path
    """
```

### Extração da DESCRIPTION do projeto

O bloco `DESCRIPTION` é processado pelo compilador e exposto diretamente em `result.linked_project.project.description`. Não há regex — o compilador lida com toda a complexidade do bloco (multilinha, indentação, caracteres especiais):

```python
# Em load_project(), após synesis.load():
project_description = result.linked_project.project.description  # Optional[str]
```

Quando presente, `project_description` é injetado no system prompt como contexto metodológico:

```
SYSTEM [CACHED]:
  Você é um codificador de pesquisa qualitativa.
  ...
  CONTEXTO DO PROJETO:
  {project_description}   ← injetado aqui quando não-nulo
  ...
```

Esta função é chamada **uma única vez** no início de cada invocação do `synesis-coder`. Todos os módulos subsequentes recebem o `dict` retornado como `ctx`.

---

## 10. `prompt_builder.py` — Construção Genérica por Tipo de Campo

O `prompt_builder` itera `ctx["item_fields"]` (ou `ctx["ontology_fields"]`) e monta instruções por tipo de campo, **sem hardcode de nomes de campos**:

```python
def _field_instruction(name: str, spec: FieldSpec, ctx: dict) -> str:
    """Gera instrução para um campo com base no seu tipo e guidelines."""
    instruction = spec.guidelines or spec.description or ""

    if spec.type == FieldType.CHAIN:
        # Injeta relações disponíveis do template + lista de códigos existentes
        ...
    elif spec.type == FieldType.TOPIC:
        # Injeta lista de tópicos existentes do topic_index
        ...
    elif spec.type in (FieldType.ORDERED, FieldType.ENUMERATED):
        # Injeta VALUES com labels e descrições do FieldSpec
        ...
    elif spec.type == FieldType.SCALE:
        # Injeta intervalo do FORMAT
        ...
    # TEXT, QUOTATION, MEMO, CODE: apenas guidelines/description
    return instruction
```

### Estrutura do prompt e estratégia de cache

A separação entre conteúdo estático (cacheável) e dinâmico é crítica para eficiência de tokens. O system prompt é construído **uma única vez** por invocação e reutilizado em todas as chamadas ao LLM na mesma sessão.

```
SYSTEM [cache_control: ephemeral — construído 1x por sessão]:
  Você é um codificador de pesquisa qualitativa.
  Gere apenas Synesis válido conforme o template abaixo.

  [se project_description existir]:
  CONTEXTO DO PROJETO:
  {project_description}

  CAMPOS DO ITEM (derivados de result.template.field_specs):
  [para cada field em item_fields]:
    {field_name} ({type}) [REQUIRED|OPTIONAL]: {guidelines ou description}
    [se CHAIN]: RELATIONS disponíveis: {FieldSpec.relations}
    [se ORDERED]: VALORES: {FieldSpec.values com labels e índices}
    [se ENUMERATED]: VALORES PERMITIDOS: {FieldSpec.values}
    [se SCALE]: INTERVALO: {FieldSpec.format}

  CONCEITOS EXISTENTES (use preferencialmente — extraídos de code_usage):
  {code_index["codes"] — agrupados em linhas de 10}

  TÓPICOS EXISTENTES (para campos TOPIC — use preferencialmente):
  {topic_index["topics"] — separados por vírgula}

USER [DYNAMIC — por chamada, NÃO cacheado]:
  BIBREF: @{bibref}
  <text>{text}</text>
  Gere o(s) bloco(s) ITEM correspondentes.
```

**Implementação do cache:**

```python
# Em LLMClient.call() — modo item síncrono
system_block = {
    "type": "text",
    "text": self._cached_system_prompt,   # construído 1x em build_item_prompt()
    "cache_control": {"type": "ephemeral"}
}
user_messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": dynamic_user_text   # bibref + text — varia por chamada
            }
        ]
    }
]
```

O system prompt deve ter **≥ 1024 tokens** para o cache ser ativado (limite mínimo da Anthropic). Para projetos com templates pequenos (ex: `thompson_bible`), o `code_index` e a `project_description` garantem que o threshold seja atingido.

---

## 11. Fluxos de Execução por Modo

### Modo `item` (Fase 1 — MVP)

```
synesis-coder item --project X --bibref Y --text Z [--format plain|verbose]
    ↓
project_loader.load_project(project_path)               → ctx
    (inclui code_index, topic_index extraídos dos .syn existentes)
    ↓
prompt_builder.build_item_prompt(ctx, bibref, text)     → messages
    (injeta code_index + topic_index no prompt cacheável)
    ↓
llm_client.call(messages)                               → raw_syn
    ↓
validator.validate_and_fix(raw_syn, ctx, llm_client)    → (syn, ok)
    (synesis.load() valida; warnings não bloqueiam se sem .syno)
    ↓
stdout: syn (plain) ou syn + log (verbose)
```

### Modo `abstract` (Fase 2)

```
synesis-coder abstract --project X --input corpus.bib --output DIR/
    ↓
project_loader.load_project(project_path)               → ctx inicial
bib_parser.load_bib(corpus.bib)                         → entries
    ↓
Para cada entry (concorrente, rate-limited):
    project_loader.load_project(project_path)           → ctx atualizado
        (reutiliza .syn já escritos para atualizar code_index)
    prompt_builder.build_abstract_prompt(ctx, bibref, abstract)
    llm_client.call_async(messages)                     → raw_syn
    validator.validate_and_fix(raw_syn, ctx, llm_client)
    gravar resultado em arquivo .syn
```

### Modo `document` (Fase 3)

```
synesis-coder document --project X --bibref Y --input texto.txt --output Y.syn
    ↓
project_loader.load_project(project_path)               → ctx
chunker.split(texto, max_tokens=6000, overlap=1500)     → chunks
    ↓
Para cada chunk (prompt_caching=True):
    prompt_builder.build_document_prompt(ctx, bibref, chunk)
    llm_client.call_async(messages)
    validator.validate_and_fix(...)
    ↓
combiner.merge_and_dedup(all_items)                     → combined_syn
validator.validate_final(combined_syn, ctx)
gravar combined_syn em output
```

### Modo `ontology` (Fase 4)

```
synesis-coder ontology --project X [--output X.syno] [--update]
    ↓
project_loader.load_project(project_path)
    → ctx com code_usage, all_triples, ontology_index, topic_index
    ↓
Se não há ONTOLOGY scope no template → erro claro ao usuário
Se --update: filtrar codes já em ontology_index
    ↓
Para cada code pendente (concorrente):
    semantic_ctx = {
        "frequency": len(code_usage[code]),
        "sources": len({item.source_ref for item in code_usage[code]}),
        "relations": [(c, r, t) for c,r,t in all_triples if c==code or t==code],
        "co_codes": co_occurrence(code, code_usage[code]),
        "examples": [item fields for item in code_usage[code][:3]],
    }
    prompt_builder.build_ontology_prompt(ctx, code, semantic_ctx)
        (injeta topic_index para sugestão de tópicos existentes)
    llm_client.call_async(messages)
    validator.validate_ontology_entry(raw_syno, ctx)
    ↓
Combinar entradas → gravar .syno
```

---

## 12. Validação e Ciclo de Correção

```python
def validate_and_fix(
    output: str,
    ctx: dict,
    llm_client: LLMClient,
    annotation_key: str = "output.syn",
    max_tries: int = 3,
) -> tuple[str, bool]:
    """
    Valida output via synesis.load(). Se inválido, envia erros ao LLM (max 3x).
    Retorna (output_final, success).
    Warnings não bloqueiam quando projeto não tem .syno ainda.
    """
    for attempt in range(max_tries):
        result = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={annotation_key: output},
        )
        if result.success or (not result.has_errors() and not ctx["has_ontology_scope"]):
            return output, True
        errors = result.get_diagnostics()
        output = llm_client.fix(output, errors)
    return f"# ERRO: validação falhou após {max_tries} tentativas\n# {errors}", False
```

---

## 13. Testes com Cases Reais

**Não criar fixtures fictícios.** Os projetos em `d:\GitHub\case-studies\` cobrem todos os casos:

| Projeto | Caminho | O que testa |
|---------|---------|-------------|
| `demo` (synesis init) | Gerado por `synesis init` em tmpdir | Caso mínimo: CODE + TOPIC, sem CHAIN, sem GUIDELINES |
| `aids_corpus` | `d:\GitHub\case-studies\Sociology\iramuteq_aids_corpus\` | CHAIN com relações em PT, sem GUIDELINES |
| `social_acceptance` | `d:\GitHub\case-studies\Sociology\Social_Acceptance\` | Template completo: GUIDELINES, ORDERED, ENUMERATED, SCALE, CHAIN |
| `nave` | `d:\GitHub\case-studies\Theology\Nave_Topical_Concordance\` | Sem CHAIN, ONTOLOGY com 1 campo |
| `thompson_bible` | `d:\GitHub\case-studies\Theology\Thompson_Chain_Reference\` | **Sem ONTOLOGY scope** — modo `ontology` deve ser recusado graciosamente |

---

## 14. Interface CLI

```bash
# Fase 1 — Modo item
synesis-coder item \
  --project projeto.synp \
  --bibref smith2024 \
  --text "Community trust and environmental concern..." \
  [--format plain|verbose] \
  [--model claude-opus-4-6]

# Fase 2 — Modo abstract
synesis-coder abstract \
  --project projeto.synp \
  --input corpus.bib \
  --output anotacoes/ \
  [--concurrent 5] \
  [--batch-size 25] \
  [--per-reference]

# Fase 3 — Modo document
synesis-coder document \
  --project projeto.synp \
  --bibref entrevista_01 \
  --input entrevistas/entrevista_01.txt \
  --output anotacoes/entrevista_01.syn \
  [--chunk-size 6000] \
  [--overlap 1500]

# Fase 4 — Modo ontology
synesis-coder ontology \
  --project projeto.synp \
  --output social_acceptance.syno \
  [--update] \
  [--concurrent 5]

# Invocação pela extensão VSCode
python -m synesis_coder item \
  --project "${workspaceFolder}/projeto.synp" \
  --bibref "${activeBibref}" \
  --text "${selectedText}" \
  --format plain
```

---

## 15. `pyproject.toml`

```toml
[project]
name = "synesis-coder"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "synesis>=0.3.0",
    "anthropic>=0.40.0",
    "click>=8.0",
    "tenacity>=8.0",
    "bibtexparser>=1.4",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-cov"]

[project.scripts]
synesis-coder = "synesis_coder.cli:main"
```

---

## 16. Decisões Arquiteturais

| Decisão | Escolha | Justificativa |
|---------|---------|---------------|
| Interface com compilador | 100% via `synesis.load()` | Atualizações do compilador chegam automaticamente |
| Templates | Totalmente dinâmicos — zero hardcode de campos | Cada projeto tem seu próprio `.synt` |
| GUIDELINES | Primárias; fallback: DESCRIPTION; fallback final: instrução genérica por tipo | Template é fonte da verdade metodológica |
| Fixtures de teste | Cases reais + `synesis init` | Cobertura real; evita divergência entre fixtures e realidade |
| Estilo de código | Procedural — funções puras; única classe: `LLMClient` | Facilita manutenção |
| CodeIndex | Consultado antes de sugerir qualquer CODE/CHAIN | Evita proliferação de conceitos |
| TopicIndex | Consultado antes de sugerir qualquer TOPIC | Mantém taxonomia coesa |
| Modo `item` | Síncrono | MVP simples; async necessário apenas no lote |
| Modos em lote | Assíncronos, rate-limited | Eficiência para corpora grandes |
| Prompt caching | Desde o modo `item` | Reduz latência e custo |
| Modelo padrão | `claude-opus-4-6` | Mais capaz para extração semântica rigorosa |
| `temperature` | 0 | Determinismo para ciclo de correção repetível |
| API key | `ANTHROPIC_API_KEY` via `.env` (python-dotenv) | Sem config externo; `.env` no `.gitignore`; `.env.example` documentado no repo |
| Sem ONTOLOGY scope | Modo `ontology` recusado com mensagem clara | `thompson_bible` prova que isso é real |
| Warnings vs Errors | Warnings não bloqueiam no modo `item` | Projeto pode não ter `.syno` ainda |
| DESCRIPTION do .synp | Injetada no system prompt quando presente | Fornece contexto metodológico sem custo adicional de tokens nas chamadas subsequentes |
| Prompt caching | System prompt marcado com `cache_control: ephemeral` | Cache hit em todas as chamadas na mesma sessão; mínimo 1024 tokens para ativar |
| Construção do prompt | Uma vez por sessão (modo item) ou por lote (modos abstract/document) | Evita reconstrução a cada chamada; reutiliza bloco estático em memória |

---

## 17. Riscos Operacionais e Mitigações

### R1 — Resolução de fronteiras no modo `document` (chunking)

**Risco:** Um conceito analítico pode ser construído ao longo de vários parágrafos. Com `max_tokens=6000` e `overlap=1500`, CHAINs que iniciam em um chunk e terminam no próximo podem gerar duplicatas parciais ou ser descartadas pela deduplicação.

**Mitigação:**
- Deduplicação baseada em **normalização de conceitos** (lowercase, strip), não em string exata: `Cost` e `cost` são o mesmo código
- CHAINs são consideradas duplicatas apenas se `(from_code, relation, to_code)` for idêntico após normalização — nunca por similaridade de texto
- Overlap de 1500 tokens garante que a maioria das unidades semânticas (~3–5 frases) esteja inteiramente dentro de pelo menos um chunk
- Bloco ITEM duplicado que compila corretamente é preferível a bloco descartado por heurística — em caso de dúvida, **preservar**

### R2 — Loop de validação determinístico (temperatura 0)

**Risco:** Com `temperature=0`, se o LLM não compreender o erro de sintaxe na primeira tentativa, ele pode repetir exatamente a mesma resposta errônea nas tentativas 2 e 3, desperdiçando tokens sem progresso.

**Mitigação:** Escalonamento de temperatura exclusivamente no loop de correção do `validator.py`:

```python
CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]  # por tentativa

for attempt in range(max_tries):
    result = synesis.load(...)
    if result.success or (not result.has_errors() and not ctx["has_ontology_scope"]):
        return output, True
    errors = result.get_diagnostics()
    temperature = CORRECTION_TEMPERATURES[min(attempt, len(CORRECTION_TEMPERATURES) - 1)]
    output = llm_client.fix(output, errors, temperature=temperature)
```

A temperatura 0 é preservada na chamada inicial (geração original). O escalonamento ocorre apenas nas **tentativas de correção** — onde diversidade é desejável.

### R3 — Rate limiting em modos de lote (Fases 2 e 4)

**Risco:** A execução concorrente sem controle de TPM (tokens por minuto) pode estourar cotas da Anthropic antes do RPM (requisições por minuto), causando erros 429 silenciosos ou throttling agressivo.

**Mitigação no `LLMClient`:**
- Semáforo controla **concorrência simultânea** (RPM)
- `deque` com janela deslizante de 60s rastreia **TPM de input e output separadamente**
- Sleep proativo quando `tokens_used_last_60s + estimated_next_call > 0.85 * TPM_limit` (margem de segurança de 15%)
- Valores padrão conservadores para Tier 1 da Anthropic: `max_rpm=50`, `max_input_tpm=40_000`, `max_output_tpm=8_000`
- Configuráveis via `.env`: `SYNESIS_CODER_MAX_RPM`, `SYNESIS_CODER_MAX_INPUT_TPM`, `SYNESIS_CODER_MAX_OUTPUT_TPM`

### R4 — Acoplamento ao provedor (Anthropic/Claude)

**Risco:** O uso de `cache_control: ephemeral` e `anthropic.Anthropic()` acopla o código à API da Anthropic. Migração futura para GPT-4o ou Gemini exigiria refatoração ampla.

**Mitigação por isolamento em `llm_client.py`:**
- `LLMClient` é a **única classe que importa `anthropic`**
- `prompt_builder.py` retorna `List[dict]` no formato canônico interno — não no formato Anthropic diretamente
- `LLMClient.call()` e `call_async()` recebem esse formato e fazem a tradução para o formato do provedor atual
- Formato canônico interno:

```python
# Formato interno (agnóstico ao provedor)
messages = [
    {"role": "system", "content": system_text, "cache": True},
    {"role": "user",   "content": user_text,   "cache": False},
]

# LLMClient traduz para Anthropic internamente:
system_blocks = [{"type": "text", "text": m["content"],
                  "cache_control": {"type": "ephemeral"}} if m["cache"] else ...]
```

- Migração futura: implementar novo adaptador dentro de `LLMClient` sem tocar em `prompt_builder.py` ou nos modos

---

## 18. Restrições Técnicas (Invioláveis)

- Nenhum campo hardcoded — todos derivados de `result.template.field_specs`
- Type hints completos em todas as funções
- Docstring de módulo obrigatória em todos os arquivos `.py`
- Não adicionar "AI", "Claude" em campos de autor/mantenedor

---

## 19. Fases de Implementação

| Fase | Escopo | Arquivos centrais | Critério de aceitação |
|------|--------|-------------------|-----------------------|
| **1** | Modo `item` + `project_loader` + `prompt_builder` + `validator` | `item_mode.py`, `project_loader.py`, `prompt_builder.py`, `validator.py`, `cli.py` | ITEM válido nos 5 projetos de teste |
| **2** | Modo `abstract` | `abstract_mode.py` + `llm_client.py` async | 10 abstracts do `social_acceptance` com taxa > 90% |
| **3** | Modo `document` | `document_mode.py` + `chunker.py` | Documento longo sem erros de compilação |
| **4** | Modo `ontology` | `ontology_mode.py` | Ontologia gerada compila sem erros |
| **5** | Integração VSCode (`Synesis: Code Selection`) | `synesis-explorer/` | ITEM inserido no cursor |
| **6** | Integração MCP | `synesis-mcp/` | Tools via Claude Desktop |

---

## 20. Verificação (Fase 1)

```bash
# 1. Instalar
cd d:\GitHub\synesis-coder
pip install -e ".[dev]"

# 2. Testar com projeto mínimo (synesis init)
synesis init /tmp/demo_project && cd /tmp/demo_project
synesis-coder item \
  --project project.synp \
  --bibref smith2024 \
  --text "Social cohesion enables collective action in resilient communities."

# 3. Testar com social_acceptance (template complexo, com GUIDELINES)
synesis-coder item \
  --project "d:\GitHub\case-studies\Sociology\Social_Acceptance\social_acceptance.synp" \
  --bibref ashworth2019 \
  --text "Community trust and environmental concern are the most important factors..."

# 4. Testar com thompson_bible (sem ONTOLOGY scope) — modo item deve funcionar normalmente
synesis-coder item \
  --project "d:\GitHub\case-studies\Theology\Thompson_Chain_Reference\thompson_bible.synp" \
  --bibref genesis1 \
  --text "In the beginning God created the heavens and the earth."

# 5. Testar com aids_corpus (relações em português, sem GUIDELINES)
synesis-coder item \
  --project "d:\GitHub\case-studies\Sociology\iramuteq_aids_corpus\aids_corpus.synp" \
  --bibref participante_01 \
  --text "A falta de informação sobre HIV faz com que as pessoas tenham medo e preconceito."

# 6. Rodar testes unitários
pytest tests/test_item_mode.py -v

# 7. Formato plain (para VSCode)
synesis-coder item \
  --project "d:\GitHub\case-studies\Sociology\Social_Acceptance\social_acceptance.synp" \
  --bibref ashworth2019 \
  --text "..." \
  --format plain
```

---

## 21. Configuração de Ambiente

### Arquivo `.env` (não versionado)

A `ANTHROPIC_API_KEY` e configurações opcionais são lidas de `.env` via `python-dotenv`:

```dotenv
# .env  ← NUNCA versionar; está no .gitignore

ANTHROPIC_API_KEY=sk-ant-...

# Opcionais (valores padrão usados se ausentes)
SYNESIS_CODER_MODEL=claude-opus-4-6
SYNESIS_CODER_MAX_RETRIES=3
SYNESIS_CODER_TEMPERATURE=0
```

### Arquivo `.env.example` (versionado — instruções ao usuário)

```dotenv
# .env.example  ← copie para .env e preencha com suas credenciais

# Obrigatório: chave de API Anthropic
# Obtenha em: https://console.anthropic.com/
ANTHROPIC_API_KEY=sk-ant-SUBSTITUA_PELA_SUA_CHAVE

# Opcional: modelo Claude a usar (padrão: claude-opus-4-6)
# SYNESIS_CODER_MODEL=claude-opus-4-6

# Opcional: número máximo de tentativas de correção por LLM (padrão: 3)
# SYNESIS_CODER_MAX_RETRIES=3

# Opcional: temperatura do modelo (padrão: 0 — determinístico)
# SYNESIS_CODER_TEMPERATURE=0
```

### `.gitignore` (entradas obrigatórias)

```gitignore
.env
*.env
!.env.example
```

### Leitura no código

```python
# Em synesis_coder/__init__.py ou llm_client.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # carrega .env se existir; variáveis de ambiente têm precedência

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise EnvironmentError(
        "ANTHROPIC_API_KEY não encontrada. "
        "Crie um arquivo .env baseado em .env.example e defina sua chave."
    )
```

---

## 22. Referências

| Componente | Localização | O que reutilizar |
|------------|------------|-----------------|
| `abstract_processor10.py` | `d:\GitHub\synesis-coder\` | Rate limiting com deque, semáforo assíncrono, retry com tenacity |
| `interview_processor.py` | `d:\GitHub\synesis-coder\` | Prompt caching, chunking, dedup SequenceMatcher |
| `semantic_memory_builder.py` | `d:\GitHub\synesis-coder\` | Lógica de frequência/relações/co-ocorrência de códigos |
| `topic_processor.py` | `d:\GitHub\synesis-coder\` | Estrutura do prompt de classificação |
| `synesis.api` | `d:\GitHub\synesis\synesis\api.py` | `synesis.load()` — API completa |
| `synesis.ast.nodes` | `d:\GitHub\synesis\synesis\ast\nodes.py` | `FieldSpec` dataclass — atributos: type, scope, values, relations, guidelines, description, arity, format |
| `social_acceptance.synt` | `d:\GitHub\case-studies\Sociology\Social_Acceptance\` | Template com todos os tipos de campo e GUIDELINES |
| `aids_corpus.synt` | `d:\GitHub\case-studies\Sociology\iramuteq_aids_corpus\` | Template simples, relações PT |
| `thompson_bible.synt` | `d:\GitHub\case-studies\Theology\Thompson_Chain_Reference\` | Template sem ONTOLOGY scope |

---

*Christian Maciel De Britto | OTIC/USP | Março 2026*
*Revisão 3.0 — Princípios de acoplamento total ao compilador, templates dinâmicos, GUIDELINES como instrução primária, cases reais como testes, programação procedural e consulta obrigatória de índices antes de sugerir.*
