# Plano: Avaliação e implementação da Opção 3 (JSON + assembler) com pré-validação de bibref

## Context

O estudo `Planning/Estudo_Reducao_Tokens_Fidelidade_Estrutura.md` propõe 10 opções para
reduzir tokens e aumentar a fidelidade estrutural dos blocos Synesis, recomendando a
**Opção 3** (LLM devolve JSON de valores → Python monta o bloco) como a de maior fidelidade.
O próprio estudo, porém, adverte (Opção 0): *"sem instrumentar a classe do primeiro erro
de cada tentativa, a decisão é especulação."*

Esta verificação foi feita contra uma depuração **real** — `case-studies/Quinto_Andar/
Dados_Lattes/annotations/lattes_gomez2026_debug.md` (run de 14/06/2026, modelo
`deepseek/deepseek-v4-flash`, 12 trechos, **OK: 3, falhas: 9**, 30 correções,
587k tokens, 753 s). Os dados desmentem parcialmente a premissa do estudo.

### O que a depuração real revela (369 ocorrências de `SYNESIS_E0xx`)

| Classe | Código | Freq. aprox. | Causa real | Opção 3 resolve? |
|---|---|---|---|---|
| Bibref inexistente no `.bib` | E001 | ~60 (em **todos** os 12 chunks) | `--bibref gomez2026` ≠ `@lattes-3355559305779367`, a chave real do `.bib` | ❌ Não |
| CODE multi-valor c/ separador errado | E033 / E015 | ~230 | LLM separa CODEs com `;` ou espaço; o compilador exige **vírgula** | ✅ Sim |
| SOURCE inventado durante a correção | E020 / E022 | ~80 | O fix em texto livre faz o LLM inventar SOURCE espúrio (`@gomez2026-1`, BIBTEX falso, `lattes_id: "123456789"`) | ⚠️ Parcial |

**Conclusões da verificação:**

1. **O estudo diagnostica a classe errada de erro.** Ele assume que os erros estruturais
   são nome-de-campo / fence markdown / enum inválido (o que JSON elimina). Na prática, o
   erro nº 1 é **bibref inválido (E001)**, que está **fora de toda a taxonomia do estudo** —
   nenhuma das 10 opções o resolve. A causa é de orquestração/uso, não de formato de saída.
   Evidência no `.synp`: *"bibref = ID Lattes prefixado com 'lattes-'"*; no `.bib`:
   `@lattes-3355559305779367`. O run usou `gomez2026`.

2. **A Opção 3 é a melhor solução para a 2ª classe (E033/E015), não para o conjunto.**
   O campo `area_tematica TYPE CODE` é multi-valor ("2 a 3 CODEs", `lattes.synt:95`), mas o
   prompt não ensina o separador. CODE cai no fallback genérico `"Assign a concise analytical
   code (snake_case)."` (`prompt_builder.py:184`) — sem nenhuma menção a vírgula. O compilador
   faz split por vírgula (`synesis/semantic/validator.py:_extract_code_values`,
   `synesis/semantic/linker.py:_split_codes_from_line`). A Opção 3 resolve isto **por
   construção**: o LLM devolve `["a","b","c"]` e o assembler junta com `, `.

3. **A cascata de correção que inventa SOURCE (E020/E022) é defeito de arquitetura do loop,
   não de formato.** Em `document_mode`, o SOURCE é gerado uma vez (`_process_document_async`
   ~L712) e nunca é revalidado nem reinjetado; cada chunk valida só ITEMs, mas **com o `.bib`**
   — daí o E001 reaparecer em todo chunk e o fix, sem ver o SOURCE real, inventar um.
   A Opção 3 sozinha não corrige isto.

**Veredito:** a Opção 3 **é uma boa solução, mas insuficiente isoladamente** — cobre ~1/3
dos erros reais (a classe E033/E015). Para que a Opção 3 entregue valor mensurável neste
cenário, o erro dominante (E001) precisa ser eliminado antes via **pré-validação de bibref
com abort precoce** (decisão do usuário). Sem isso, mesmo um JSON perfeito continuaria
falhando 12/12 por bibref ausente.

Este plano, conforme o escopo escolhido, concentra-se em **(A) implementar a Opção 3** e
**(B) a pré-validação de bibref** como pré-requisito que a torna efetiva.

---

## Escopo

- **Incluído:** Opção 3 (geração via JSON + assembler determinístico) para o caminho `item`
  e `document`/`abstract`; pré-validação de bibref com abort precoce.
- **Fora de escopo (registrar como follow-up):** reinjeção/revalidação do SOURCE no loop de
  correção por chunk (E020/E022) — recomendado, mas o usuário restringiu o plano à Opção 3.

---

## Parte B (pré-requisito) — Pré-validação de bibref com abort precoce

**Por quê primeiro:** elimina o erro dominante (E001) que invalidaria qualquer ganho da
Opção 3. É barato e de baixo risco.

**Onde:** ponto de entrada dos modos que recebem `--bibref` —
`synesis_coder/modes/document_mode.py::_process_document_async` (logo após
`load_project`, antes de `_generate_source_block`) e o equivalente em `abstract_mode.py`.

**O quê:**
- Após `ctx = load_project(...)`, obter as chaves do `.bib` já carregado pelo compilador
  (reusar `ctx`/`synesis.load` — o `.bib` já é parseado; localizar onde as chaves ficam
  expostas no resultado de `load_project`, evitando reparse).
- Se `bibref` (sem `@`) não estiver entre as chaves: **abortar** com mensagem clara:
  - listar as chaves disponíveis no `.bib`;
  - citar a convenção do `.synp` quando houver `DESCRIPTION` (ex.: "prefixado com 'lattes-'").
- Não adivinhar nem auto-derivar (decisão do usuário: "validar e abortar cedo").

**Reuso:** `load_project` (`synesis_coder/project_loader.py`) já chama `synesis.load()` e tem
acesso ao `bib_content`; expor as chaves do `.bib` em `ctx["bib_keys"]` é o ponto natural.

---

## Parte A — Opção 3: JSON + assembler

### A.1 Novo módulo `synesis_coder/schema_builder.py`

Converte `FieldSpec` (do template, via `ctx["item_fields"]`/`source_fields`) em JSON Schema.
Mapeamento por `FieldType` (enum em `synesis`):

| FieldType | JSON Schema |
|---|---|
| TEXT, QUOTATION, MEMO, DATE | `{"type": "string"}` |
| CODE (multi-valor) | `{"type": "array", "items": {"type": "string"}}` |
| CHAIN | `{"type": "array", "items": {hop estruturado}}` — ver A.1.1 |
| ENUMERATED / ORDERED | `{"enum": [valores permitidos]}` — elimina E027/E029 por construção |
| SCALE | `{"type": "integer", "minimum": lo, "maximum": hi}` derivado de `spec.format` |
| TOPIC | `{"type": "string"}` (ou array, conforme cardinalidade) |

- REQUIRED → entra em `"required": [...]`; OPTIONAL → ausência permitida.
- `"additionalProperties": false` → elimina E022 (campo desconhecido) por construção.

#### A.1.1 CHAIN como componentes estruturados (sem o LLM escrever a sintaxe)

Decisão de projeto (a pedido): o LLM **não** gera a string `A -> rel -> B`; gera apenas os
**componentes** da chain, e o Python insere os `->` deterministicamente. Isso elimina, por
construção, toda a classe de erros de sintaxe de chain: separador errado, `;`/espaço em
conceito (E033/E015), e seta malformada (E008/E011).

Forma confirmada no compilador (`synesis/grammar/synesis.lark:197` — `chain_expr:
CHAIN_ELEMENT ("->" CHAIN_ELEMENT)+`): uma chain qualificada é o interleave
`conceito -> relação -> conceito [-> relação -> conceito ...]`. A relação tem de ser uma das
declaradas no bloco `RELATIONS` do template (`synesis/semantic/validator.py:299-307`, senão
**E010**).

Schema do campo CHAIN (array de hops), modelo recomendado — **hop = `{source, relation,
target}`**, com `relation` restrita por `enum` às RELATIONS do template (elimina **E010**
por construção):
```json
{
  "type": "array",
  "minItems": 1,                         // refletindo ARITY >= 1 do template
  "items": {
    "type": "object",
    "properties": {
      "source":   {"type": "string"},
      "relation": {"enum": ["aplicada_a","usados_para","combina_com","gera","fundamenta"]},
      "target":   {"type": "string"}
    },
    "required": ["source", "relation", "target"],
    "additionalProperties": false
  }
}
```
- O `enum` de `relation` é gerado de `spec.relations` (chaves declaradas no template) — nada
  hardcoded.
- `minItems` deriva da ARITY do `FieldSpec` quando expressa (`>= 1`).
- Encadeamento multi-hop (`A -> r1 -> B -> r2 -> C`): suportado representando cada CHAIN como
  uma lista de hops em que `target[i] == source[i+1]`; o assembler colapsa o conceito repetido
  ao interleavar (ver A.2). Para o caso lattes (triplas simples), cada hop vira uma linha
  `relacao_aplicada:` independente.

### A.2 Novo módulo `synesis_coder/block_assembler.py`

**Princípio central (a pedido): a moldura estrutural inteira é determinística.** O LLM
**nunca** escreve as palavras-chave de bloco, os nomes de campo, os `:`, a indentação, nem
`@{bibref}`. Tudo isso é emitido pelo Python a partir do template + do bibref já validado.
O LLM contribui **apenas valores**. Itens estruturais gerados deterministicamente:

| Elemento estrutural | Quem gera | Erro eliminado |
|---|---|---|
| `ITEM @{bibref}` / `END ITEM` | assembler (usa bibref já validado) | fence, bloco extra, `@bibref` errado |
| `SOURCE @{bibref}` / `END SOURCE` | assembler | idem (caminho SOURCE) |
| nome do campo + `:` + indentação (4 esp.) | assembler (itera `ctx` fields) | **E022** (campo desconhecido), nome digitado errado |
| presença de campos REQUIRED | assembler (sabe quais são) | **E020** (faltante) |
| omissão de campos OPTIONAL | assembler (chave ausente no JSON → não emite) | **E021** (proibido/indevido) |
| ordenamento BUNDLE (note+chain juntos) | assembler | quebra de paridade BUNDLE |

**Caso multi-ITEM (lattes/document):** um chunk normalmente gera **N ITEMs**. O JSON de
topo é, portanto, uma **lista de objetos-ITEM** (`{"items": [ {valores...}, ... ]}`); o
assembler emite N blocos `ITEM ... END ITEM` em sequência. O schema (A.1) descreve o objeto
de um ITEM; o envelope `items: array` aplica-o N vezes. O caminho `item` (1 ITEM) é o caso
degenerado N=1.

Conversão dos **valores** em texto Synesis determinístico:
- CODE/array → join com `", "` (resolve **E033/E015** — separador correto garantido).
- CHAIN/array de hops → o Python insere os `->`: para cada hop, monta
  `f"{source} -> {relation} -> {target}"`; chains de uma única tripla viram uma linha
  `campo:` por hop; multi-hop contíguos (`target[i]==source[i+1]`) são interleavados numa
  só linha (`A -> r1 -> B -> r2 -> C`). O LLM nunca digita `->` nem o separador — elimina
  **E008/E010/E011/E033/E015** de chain por construção.
- Normalizar conceitos de chain para snake_case/sem espaços antes de montar (defesa extra
  contra E015, já que `relation` vem do enum mas `source`/`target` são texto livre).
- QUOTATION/MEMO multi-linha → escapar/normalizar newlines que quebrariam o parser LALR.
- Campo ausente no JSON → simplesmente omitido (sem sentinel) → respeita OPTIONAL.
- Monta `ITEM @{bibref} ... END ITEM` (e `SOURCE` no caminho de SOURCE).

### A.3 `synesis_coder/llm_client.py` — novo caminho `call_json`

- Adicionar `response_format` ao `create_kwargs` já existente em
  `llm_client.py:484-495`:
  ```python
  if schema is not None:
      create_kwargs["response_format"] = {
          "type": "json_schema",
          "json_schema": {"name": "synesis_values", "schema": schema, "strict": True},
      }
  ```
- Novo método `call_json(messages, schema, ...)` que retorna `dict` (parse do JSON).
- **Fallback obrigatório:** se o backend não suportar `json_schema` (erro 400 ou JSON
  inválido), cair no caminho de texto livre atual (`call_async`) — preservar comportamento.
- Reusar telemetria/usage/recorder existentes (não duplicar rate-limiting nem token tracking).

### A.4 Wiring nos modos + prompt

- `prompt_builder.py`: nova função que monta o prompt "devolva apenas JSON de valores".
  No caminho JSON, o `_build_output_format_section` (`prompt_builder.py:239` — que hoje
  ensina `ITEM @{bibref}` / `{field}: {value}` / `END ITEM`) **não é usado**: a moldura não
  é mais responsabilidade do LLM. Reusar apenas as seções de GUIDELINES por campo (que
  descrevem o *conteúdo* de cada valor) e a lista de conceitos/tópicos existentes.
  Como complemento de baixo custo e independente do JSON, **corrigir o fallback genérico de
  CODE** (`prompt_builder.py:184`) para mencionar vírgula como separador — protege o caminho
  de texto livre/fallback.
- `modes/item_mode.py`, `modes/document_mode.py`, `modes/abstract_mode.py`: novo caminho
  `call_json → block_assembler → validate_and_fix`. `validate_and_fix` **continua necessário**
  para erros semânticos (SCALE range, BUNDLE/ARITY, código inexistente).

### A.5 Suporte por backend (verificar antes de ativar por padrão)

O `.env` ativo usa Gemini via backend `openai`-compat (suporta `json_schema`). Ollama/RunPod
variam. Manter o caminho JSON atrás de detecção de capacidade + fallback; **não** torná-lo o
default sem confirmar suporte no backend em uso.

---

## Arquivos a tocar

| Arquivo | Mudança |
|---|---|
| `synesis_coder/project_loader.py` | expor `ctx["bib_keys"]` |
| `synesis_coder/modes/document_mode.py` | abort precoce de bibref; wiring `call_json` |
| `synesis_coder/modes/abstract_mode.py` | abort precoce de bibref; wiring `call_json` |
| `synesis_coder/modes/item_mode.py` | wiring `call_json` + fallback |
| `synesis_coder/schema_builder.py` *(novo)* | `FieldSpec → JSON Schema` |
| `synesis_coder/block_assembler.py` *(novo)* | dict → texto Synesis |
| `synesis_coder/llm_client.py` | `call_json` + `response_format` em `create_kwargs` |
| `synesis_coder/prompt_builder.py` | prompt JSON; corrigir instrução de CODE (vírgula) |
| `tests/` | ver verificação |

---

## Verificação

1. **Unidade (sem LLM):**
   - `schema_builder`: cada FieldType → schema esperado (CODE→array, ENUMERATED→enum, SCALE→min/max).
   - `block_assembler`: dict → texto; foco em CODE multi-valor virar `a, b, c` (regressão direta
     do E033/E015); CHAIN hops → `A -> rel -> B`; OPTIONAL ausente → omitido; **moldura
     determinística** — `ITEM @bibref`/`END ITEM`, nomes de campo e indentação saem corretos
     mesmo quando o JSON traz chaves extras/ausentes (extras ignoradas, REQUIRED ausente
     sinalizado); envelope `items: array` → N blocos ITEM.
   - bibref guard: bibref inexistente → exceção/abort com as chaves listadas; bibref válido → segue.
2. **Compilação real do assembler:** alimentar o output do `block_assembler` em `synesis.load()`
   (mesmo padrão dos testes existentes em `tests/test_*_mode.py`) e exigir zero E033/E015/E022.
3. **Integração (com `.env` Gemini):** reprocessar
   `case-studies/Quinto_Andar/Dados_Lattes` com o **bibref correto**
   `--bibref lattes-3355559305779367` e `--debug`; comparar o novo
   `lattes_<bibref>_debug.md` com a baseline: esperado **0 ocorrências de E001**, queda
   acentuada de E033/E015, e taxa de sucesso de chunks ≫ 3/12.
4. `ruff check` no pacote; suíte não-integração verde.

---

## Riscos / notas

- **Cobertura parcial assumida:** este plano (por escopo escolhido) **não** corrige a cascata
  que inventa SOURCE no loop de correção (E020/E022). Registrar como follow-up: isolar o SOURCE
  da validação por chunk e/ou reinjetar o SOURCE válido já gerado antes do fix.
- **Backend-dependência:** `json_schema` strict não é universal; o fallback para texto livre é
  obrigatório para não regredir Ollama/modelos menores.
- **CHAIN como hops estruturados `{source, relation, target}`** com `relation` por `enum`
  elimina **E010** (relação inválida), **E008/E011** (chain malformada) e a sintaxe da seta
  por construção — uma melhoria sobre a Opção 3 original do estudo, que mantinha a chain como
  string livre. Permanece com `validate_and_fix` apenas a validação semântica residual: SCALE
  range, BUNDLE/ARITY entre campos, e código/conceito inexistente na ontologia.
- **Conceitos de chain (`source`/`target`) continuam texto livre** no JSON; a normalização
  determinística (snake_case) no assembler é a defesa contra E015 nesses campos.
