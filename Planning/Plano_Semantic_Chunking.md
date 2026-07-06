# Plano de Implementação — Semantic Chunking (structure-aware) no `document` mode

**Versão alvo:** synesis-coder 0.4.0 (minor — novo comportamento opcional, sem quebra de interface)
**Data:** 2026-06-11
**Arquivo principal:** [document_mode.py](../synesis_coder/modes/document_mode.py)
**Status:** implementado em 0.4.0 (2026-06-11)

---

## 1. Contexto e justificativa

### 1.1 O que NÃO é o problema (correção de premissa)

Uma análise anterior levantou a hipótese de que o `document` mode teria complexidade
**O(N×M)** — cada chunk (N) confrontado com cada prompt atômico de campo (M),
relendo o mesmo trecho várias vezes. **Isso não corresponde ao código.**

O synesis-coder já implementa **Multi-Target Extraction / single-pass**:
[build_document_prompt](../synesis_coder/prompt_builder.py#L467) monta **um único**
system prompt agregando **todos** os campos do template
([_build_item_fields_section](../synesis_coder/prompt_builder.py#L119) itera
`item_fields.items()` concatenando GUIDELINES numa só string) e **uma única**
mensagem de usuário com o chunk. Cada chunk → **1 chamada LLM**
([document_mode.py:446](../synesis_coder/modes/document_mode.py#L446)). A
complexidade real é **O(N)** + O(falhas) do fix-loop.

Portanto, as estratégias "Router/Classifier" e "Multi-Target" **não se aplicam**
(a segunda já existe; a primeira pressupõe múltiplos prompts por chunk para rotear).

### 1.2 O que É o problema (evidência do teste Lattes)

Run de 2026-06-11 (`gemini-2.5-pro`, doc de 95 328 chars → 12 chunks de 12k):

| Sintoma | Frequência | Causa raiz |
|---------|-----------|------------|
| `finish_reason=length` | ~8 de 12 chunks | Chunk de 12k chars denso → output estoura `max_tokens` e trunca |
| chunk `ok=False` | 4 de 12 | Validação falha sobre output truncado; fix-loop relê saída cortada |
| 321s wall-clock | — | Sequencialização por rate-limit + retries sobre chunks grandes |

O corte atual ([split_into_chunks](../synesis_coder/modes/document_mode.py#L96))
é **cego por tamanho**: divide por `\n\n` e acumula até `DEFAULT_CHUNK_SIZE = 12_000`
chars ([document_mode.py:58](../synesis_coder/modes/document_mode.py#L58)), caindo
para split por sentença só quando um parágrafo isolado excede o limite. Ele ignora
a estrutura hierárquica do documento.

Um currículo Lattes é **altamente estruturado** por cabeçalhos Markdown
(`## Formação acadêmica`, `## Produção bibliográfica`, `## Projetos de pesquisa`,
etc.). Cortar por contagem de chars mistura seções heterogêneas num mesmo chunk
gigante e força o LLM a produzir um output longo demais → trunca.

### 1.3 A solução: Semantic Chunking orientado a cabeçalhos

Dividir respeitando a hierarquia de headers Markdown **antes** de cair no fallback
por tamanho. Cada seção lógica (`##`/`###`) vira a unidade natural de chunk.
Benefícios diretos, todos atacando a causa raiz observada:

- **Elimina o truncamento** — seções menores e auto-contidas geram output que cabe
  em `max_tokens` (reforçado pelo P1-bis dinâmico já implementado em 0.3.3).
- **Reduz o nº de chamadas** — seções pequenas consecutivas agrupam-se num chunk
  só; seções grandes subdividem-se com fronteira limpa.
- **Melhora a fidelidade de extração** — o trecho contém a unidade lógica completa,
  não um fragmento cortado no meio de uma lista de publicações.

Esta é a estratégia **#1 (Semantic/Structure-aware Chunking)** — a única das quatro
analisadas com retorno claro e baixo risco para a arquitetura atual.

---

## 2. Princípios de design (não-quebra)

1. **Interface pública intacta.** A assinatura de
   [split_into_chunks(text, chunk_size, overlap)](../synesis_coder/modes/document_mode.py#L96)
   **não muda**. `process_document` e a CLI (`--chunk-size`, default 12 000) continuam
   funcionando byte-a-byte para quem não opta pelo novo comportamento.
2. **Degradação graciosa.** Documento **sem** cabeçalhos Markdown (ex.: transcrição
   de entrevista em texto corrido, `.txt`) cai **automaticamente** no algoritmo atual
   por parágrafo/sentença. Zero regressão para os casos já cobertos.
3. **`overlap` preservado.** O overlap entre chunks consecutivos continua existindo;
   a lógica de [_build_overlap_prefix](../synesis_coder/modes/document_mode.py#L183)
   é reutilizada.
4. **`chunk_size` vira teto, não meta.** No modo semântico, `chunk_size` deixa de ser
   o alvo de empacotamento e passa a ser o **limite** que dispara subdivisão de uma
   seção grande. Seções menores que o teto **não** são forçadas a crescer.
5. **Determinístico, sem LLM.** O chunking é puro processamento de texto — nenhuma
   chamada de API adicional (ao contrário do padrão Router, que custaria 1 skim/chunk).

---

## 3. Algoritmo proposto

### 3.1 Visão geral (3 estágios)

```
split_into_chunks(text, chunk_size, overlap)
  │
  ├─ 1. DETECTAR estrutura
  │     _has_markdown_structure(text) → bool
  │        (≥ 2 cabeçalhos ATX detectados?)
  │
  ├─ 2a. SE estruturado → _split_by_headers(text, chunk_size, overlap)
  │        a) Parsear em (nível, título, corpo) por header
  │        b) Empacotar seções consecutivas até chunk_size
  │        c) Seção isolada > chunk_size → subdividir o corpo
  │           reusando _split_by_sentences (com o título replicado
  │           como prefixo de contexto em cada subchunk)
  │        d) Aplicar overlap entre chunks consecutivos
  │
  └─ 2b. SENÃO → algoritmo atual (parágrafo → sentença)  [INALTERADO]
```

### 3.2 Detecção de estrutura — `_has_markdown_structure`

```python
_ATX_HEADER = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)

def _has_markdown_structure(text: str, min_headers: int = 2) -> bool:
    """True se o texto tem ≥ min_headers cabeçalhos ATX Markdown.

    Limiar de 2 evita tratar um documento com um único título de topo
    (sem subdivisão real) como estruturado — esse caso é melhor servido
    pelo split por parágrafo.
    """
    return len(_ATX_HEADER.findall(text)) >= min_headers
```

**Decisão:** apenas cabeçalhos **ATX** (`#`…`######`). Setext (`===`/`---` sob a
linha) é raro em exports Lattes/Markdown gerados por ferramenta e adiciona ambiguidade
com regras horizontais; fica fora do escopo (degrada para parágrafo, sem erro).

### 3.3 Parse em seções — `_parse_markdown_sections`

```python
def _parse_markdown_sections(text: str) -> List[Tuple[str, str]]:
    """Divide o texto em (cabeçalho, corpo) por cabeçalho ATX.

    O preâmbulo antes do primeiro cabeçalho (se houver) vira uma seção
    com cabeçalho vazio. Cada seção mantém seu próprio cabeçalho como
    primeira linha do conteúdo retornado, para preservar contexto.

    Returns:
        Lista de (header_line, section_text) onde section_text inclui
        o cabeçalho seguido do corpo até o próximo cabeçalho de nível
        igual ou superior.
    """
```

**Regra de aninhamento:** uma seção `##` **absorve** os `###`/`####` abaixo dela
*enquanto* o conjunto couber em `chunk_size`. Só quando o bloco `##` inteiro excede
o teto é que descemos um nível e tratamos cada `###` como candidato a chunk próprio.
Isto preserva o macro-contexto (a `##` pai) sempre que possível — eco leve da ideia
hierárquica da estratégia #4, sem o custo de manter uma árvore em memória.

### 3.4 Empacotamento — `_split_by_headers`

```python
def _split_by_headers(
    text: str, chunk_size: int, overlap: int
) -> List[str]:
    """Agrupa seções Markdown em chunks respeitando chunk_size.

    - Seções consecutivas pequenas → mesmo chunk (até chunk_size).
    - Seção isolada > chunk_size → subdivide o CORPO via
      _split_by_sentences; cada subchunk recebe o cabeçalho da seção
      como prefixo de contexto (ancora o LLM no que está lendo).
    - overlap entre chunks consecutivos via _build_overlap_prefix
      operando sobre a lista de seções acumuladas.
    """
```

Pontos de atenção na subdivisão de seção grande (3.4c):

- O **cabeçalho é replicado** no topo de cada subchunk daquela seção
  (`"## Produção bibliográfica\n\n<trecho>"`). Sem isso, o subchunk 2..n de uma
  lista de publicações perde o contexto de "estou numa lista de produções".
- A nota de posição já existente em
  [_build_document_user_message](../synesis_coder/prompt_builder.py#L509)
  (`[Excerpt i of N — extract only ITEMs with complete evidence in this excerpt]`)
  continua aplicável e complementa o cabeçalho replicado.

---

## 4. Mudanças concretas por arquivo

### 4.1 [document_mode.py](../synesis_coder/modes/document_mode.py)

| # | Mudança | Local |
|---|---------|-------|
| 4.1.1 | Constante `_ATX_HEADER` (regex compilada) | escopo de módulo, junto a `DEFAULT_CHUNK_SIZE` (~linha 58) |
| 4.1.2 | `_has_markdown_structure(text, min_headers=2)` | nova helper |
| 4.1.3 | `_parse_markdown_sections(text)` | nova helper |
| 4.1.4 | `_split_by_headers(text, chunk_size, overlap)` | nova helper |
| 4.1.5 | `split_into_chunks` ganha o **dispatch** no topo: se `_has_markdown_structure` → `_split_by_headers`, senão segue o corpo atual **inalterado** | [linha 116](../synesis_coder/modes/document_mode.py#L116) |
| 4.1.6 | Docstring do módulo (seção "Chunking", [linha 25](../synesis_coder/modes/document_mode.py#L25)) atualizada para descrever o modo semântico + fallback | topo |

O dispatch em `split_into_chunks` é mínimo e cirúrgico:

```python
def split_into_chunks(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP):
    if len(text) <= chunk_size:
        return [text]
    if _has_markdown_structure(text):
        return _split_by_headers(text, chunk_size, overlap)
    # --- algoritmo atual (parágrafo → sentença), 100% inalterado ---
    paragraphs = re.split(r"\n\n+", text)
    ...
```

`_split_by_sentences` ([linha 160](../synesis_coder/modes/document_mode.py#L160)) e
`_build_overlap_prefix` ([linha 183](../synesis_coder/modes/document_mode.py#L183))
são **reutilizadas sem alteração** por `_split_by_headers`.

### 4.2 CLI — opcional, fase 2

Default permanece automático (detecta estrutura → usa semântico). **Não** é
necessário adicionar flag para a v0.4.0. Se desejado depois, uma flag
`--chunking [auto|semantic|size]` em [cli.py:830](../synesis_coder/cli.py#L830)
permitiria forçar o modo; `auto` (= comportamento desta proposta) seria o default.
Fora do escopo inicial para manter a superfície mínima.

### 4.3 Sem mudança necessária

- [prompt_builder.py](../synesis_coder/prompt_builder.py) — o single-pass já está
  correto; a nota de posição já existe.
- [llm_client.py](../synesis_coder/llm_client.py) — o P1-bis (max_tokens dinâmico,
  0.3.3) já dimensiona o output por chunk; chunks menores se beneficiam
  automaticamente.
- CLI default, `process_document`, `validator.py` — intactos.

---

## 5. Impacto esperado no caso Lattes

| Métrica | Antes (size-based 12k) | Depois (semantic) — esperado |
|---------|------------------------|------------------------------|
| Nº de chunks | 12 (fixos por tamanho) | variável — provável **redução** (seções pequenas agrupam) |
| `finish_reason=length` | ~8/12 | **≈ 0** (chunks auto-contidos < teto) |
| chunks `ok=False` | 4/12 | **redução forte** (menos truncamento → menos fix-loop) |
| Fidelidade de extração | fragmentos cortados | unidades lógicas completas |

> O ganho de wall-clock é **secundário e indireto**: menos truncamento → menos
> tentativas de fix → menos chamadas sequenciais. O objetivo primário é
> **qualidade e robustez**, não velocidade pura.

---

## 6. Estratégia de verificação

### 6.1 Testes unitários novos — `tests/test_document_mode.py`

| Teste | Verifica |
|-------|----------|
| `test_has_markdown_structure_true` | doc com ≥2 `##` → True |
| `test_has_markdown_structure_false` | texto corrido sem headers → False |
| `test_split_by_headers_groups_small_sections` | 3 seções pequenas → 1 chunk |
| `test_split_by_headers_subdivides_large_section` | 1 seção > chunk_size → N subchunks, cada um com o cabeçalho replicado |
| `test_split_by_headers_preserves_overlap` | overlap presente entre chunks consecutivos |
| `test_split_into_chunks_fallback_no_headers` | texto sem estrutura → resultado **idêntico** ao algoritmo atual (regressão) |
| `test_split_into_chunks_short_text_unchanged` | `len(text) <= chunk_size` → `[text]` (inalterado) |

### 6.2 Teste de regressão (não-quebra)

Rodar a suíte completa antes/depois:
```powershell
cd d:\GitHub\synesis-coder; python -m pytest tests/ -q
```
Os testes existentes de `split_into_chunks` por parágrafo/sentença **devem continuar
verdes sem edição** — prova de que o fallback é fiel.

### 6.3 Teste end-to-end no Lattes

Rerodar o mesmo documento do teste 0.3.3 e comparar o log:
```powershell
python -m synesis_coder document `
  --project "d:\GitHub\case-studies\Quinto_Andar\Dados_Lattes\lattes.synp" `
  --bibref "lattes-3355559305779367" `
  --input  "d:\GitHub\case-studies\Quinto_Andar\Dados_Lattes\01_Martín-Gómez-Ravetti_3355559305779367.md" `
  --output "...v3.syn"
```
**Critério de aceite:** queda mensurável de `finish_reason=length` e de chunks
`ok=False` em relação ao baseline 0.3.3, sem perda de ITEMs legítimos.

---

## 7. Riscos e mitigações

| Risco | Probabilidade | Mitigação |
|-------|--------------|-----------|
| Doc com headers decorativos (não estruturais) fragmenta demais | Baixa | `min_headers=2` + agrupamento de seções pequenas até `chunk_size` |
| Seção única gigante (ex.: lista de 200 publicações sob um `##`) | Média | Subdivisão por sentença com cabeçalho replicado (3.4c) — mesmo mecanismo de hoje, agora com contexto |
| Setext headers ignorados | Baixa | Degrada para fallback por parágrafo — sem erro, comportamento atual |
| Regressão no caminho texto-corrido | Baixa | Dispatch isola o código novo; corpo atual permanece byte-a-byte; teste de regressão dedicado (6.2) |

---

## 8. Ordem de implementação

1. `_ATX_HEADER` + `_has_markdown_structure` + testes 6.1 (1, 2)
2. `_parse_markdown_sections` + teste de parse isolado
3. `_split_by_headers` (reusando `_split_by_sentences` e `_build_overlap_prefix`) + testes 6.1 (3–5)
4. Dispatch em `split_into_chunks` + teste de regressão 6.1 (6, 7) e 6.2
5. Docstring do módulo
6. E2E Lattes (6.3) + bump para **0.4.0** + entrada no CHANGELOG

---

## 9. Fora de escopo (roadmap futuro)

- **Hierarchical/Graph Chunking (estratégia #4):** indexar o documento como árvore
  Documento→Capítulo→Parágrafo para GraphRAG. Casa com a natureza de grafo do
  Synesis (CHAINs já são arestas), mas é reescrita de pipeline + estado em árvore —
  não uma otimização pontual. Direção estratégica, não próximo passo.
- **Router/Classifier (estratégia #3):** filtro de relevância pré-extração. Valor
  baixo na arquitetura single-pass atual e arrisca descartar evidência sutil
  (contra o princípio "em dúvida, preservar" do `merge_and_dedup`). Custaria 1
  chamada de skim por chunk — desfavorável em inferência local.
