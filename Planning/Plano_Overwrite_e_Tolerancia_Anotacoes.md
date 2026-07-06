# Plano — `--overwrite`, escrita segura e tolerância a anotações inválidas

**Data:** 2026-06-16
**Status:** Aprovado para implementação (decisões do usuário registradas abaixo)
**Repositório alvo:** synesis-coder (synesis e synesis-lsp/explorer NÃO são afetados)

---

## 1. Contexto e Sintoma

Ao rodar:

```
synesis-coder document --project lattes.synp --bibref lattes-3355559305779367 \
    --input 01_Martín-Gómez-Ravetti_3355559305779367.md --output lattes.syn
```

O comando falhou com:

```
Erro: Erro ao compilar projeto 'lattes.synp':
=== ERROS ===
[!] Campo obrigatorio `nome` ausente no bloco SOURCE
[!] Tipo invalido em `score_sugerido`: esperado number, encontrado str   (×9)
```

O `lattes.synt` (template) foi atualizado, mas o `lattes.syn` (anotações) permaneceu no
formato antigo. O `.synp` declara `INCLUDE ANNOTATIONS "lattes.syn"` — exatamente o arquivo
que o comando `document` deveria **regenerar**.

---

## 2. Diagnóstico — Duas Falhas Distintas

### Falha A — Acoplamento indevido (a que causou o erro reportado)

`document_mode._process_document_async` chama `load_project(project_path, load_annotations=True)`
no **passo 3** ([document_mode.py:868](../synesis_coder/modes/document_mode.py)). Internamente,
`load_project` ([project_loader.py:86-100](../synesis_coder/project_loader.py)) compila
template + anotações via `synesis.load()` e, se `result.has_errors()`, levanta `ValueError`.

Como o `.syn` antigo é inválido sob o template novo, o erro ocorre **antes** de qualquer
extração LLM. O output só seria gravado no passo 11 — nunca alcançado.

**Natureza:** deadlock lógico. A geração do `.syn` depende da validade do `.syn` que ela
mesma vai substituir. Um modo *gerador* não deve exigir que sua própria saída pré-existente
seja válida.

### Falha B — Perda silenciosa de dados

`_process_document_async` grava o output incondicionalmente
([document_mode.py:978](../synesis_coder/modes/document_mode.py)):
`output_path.write_text(...)`. Não há flag de proteção nem confirmação. Se a interrupção
ocorrer no meio da escrita, o `.syn` (que é input do INCLUDE) fica truncado.

---

## 3. Decisões do Usuário (2026-06-16)

| Tema | Decisão |
|------|---------|
| Falha A | **R1a** — tolerar e avisar (erros de template/bibref abortam; erros de anotação viram warning) |
| Falha B | **R2 + R3 + R4** — `--overwrite` + confirmação + escrita atômica + `--backup` |
| Abrangência | **Todos os modos geradores** — `document`, `ontology`, `incorporate`, `finetune` |

---

## 4. Solução Detalhada (técnicas consagradas)

### R1a — Separar schema de instâncias (degradação graciosa por origem de erro)

**Princípio:** Fail-fast nas pré-condições reais (template, bibref); tolerância nas anotações.

**Mudança em `project_loader.load_project`:**
- Novo parâmetro `tolerate_annotation_errors: bool = False` (default preserva comportamento atual).
- Quando `True`:
  - Classificar diagnósticos de `result` por origem/escopo: erros cujo arquivo de origem é
    uma anotação `.syn` → degradados a warning (log), não abortam.
  - Erros de template (`.synt`), bibref/E001 e erros estruturais do `.synp` → continuam abortando.
- Retornar o `ctx` normalmente; o `code_index`/`topic_index` é construído com o que conseguiu
  ser linkado (parcial é aceitável — é só contexto de dedup/sugestão).

**Chamador:** `document_mode` passa `tolerate_annotation_errors=True`. Os demais modos NÃO
geradores (`item`, `critique`) mantêm `False`.

**Risco:** `load_project` é o único ponto de entrada do compilador no coder, usado por todos
os modos. **OBRIGATÓRIO** rodar `gitnexus_impact(load_project, direction=upstream)` antes de
editar e confirmar que o novo parâmetro com default não quebra nenhum caller (d=1).

**Classificação de erro por origem:** verificar se os diagnósticos do compilador carregam
`SourceLocation.file` confiável para distinguir `.syn` de `.synt`. Se não distinguível com
segurança, fallback: tolerar por **código de erro** (os erros de campo/tipo/required de ITEM
e SOURCE de anotação) mantendo abort para erros de definição de template. Validar contra a
arquitetura dual de diagnósticos (ver memória `diagnostics_dual_message_architecture`).

### R2 — `--overwrite` + confirmação (princípio do menor espanto)

Padrão consagrado (git, `cp -i`, `gcloud`): escrita destrutiva exige consentimento.

**No `cli.py`, para cada modo gerador, ANTES de chamar o processador:**

```
1. output não existe                       → grava normalmente
2. output existe E --overwrite             → grava (modo batch/script)
3. output existe E NÃO --overwrite:
   a. TTY (sys.stdin.isatty())             → click.confirm("X já existe. Sobrescrever?")
                                              negou → abort com mensagem clara
   b. não-TTY (pipe/CI)                    → abort, exit≠0, instruir uso de --overwrite
                                              NUNCA bloquear esperando input (regra CI)
```

Usar `click.confirm` e detecção de TTY da lib padrão. A verificação fica no CLI (camada de
borda), não no processador — mantém os modos puros e testáveis.

**Nota sobre `abstract` e `critique`/`normalize`:** `abstract` grava em diretório
(`--output annotations/`) com um arquivo por referência; a proteção deve ser por-arquivo
dentro do diretório, não no diretório. `critique`/`normalize` geram `.synr` (revisões), não
anotações canônicas — fora do escopo "modos geradores" desta rodada, mas a mesma mecânica
pode ser estendida depois.

### R3 — Escrita atômica (write-temp-then-rename)

**Mudança nos processadores (`document_mode` e demais):**
- Gravar em arquivo temporário no mesmo diretório (`output_path` + sufixo `.tmp` ou
  `tempfile.NamedTemporaryFile(dir=output_path.parent, delete=False)`).
- `os.replace(tmp, output_path)` ao final — rename atômico no mesmo filesystem.
- Em exceção/interrupção, remover o `.tmp`; o output original permanece intacto.

Elimina corrupção parcial do `.syn` (que é input do INCLUDE) em caso de Ctrl-C após a
fase LLM cara.

### R4 — Backup opt-in

- Flag `--backup` nos modos geradores.
- Antes do `os.replace` (R3), se `--backup` e o destino existe: copiar para
  `output_path` + `.bak` (sobrescreve o `.bak` anterior; manter simples — não versionar
  com timestamp salvo se solicitado depois).
- Rede de segurança barata sobre R3.

---

## 5. Escopo de Impacto

| Repo | Arquivo | Mudança | Risco |
|------|---------|---------|-------|
| synesis-coder | `cli.py` | flags `--overwrite/--no-overwrite`, `--backup`; lógica de confirmação/TTY nos 4 modos geradores | Baixo (aditivo) |
| synesis-coder | `modes/document_mode.py` | aceitar `overwrite`, `backup`; escrita atômica; passar `tolerate_annotation_errors=True` | Baixo |
| synesis-coder | `modes/ontology_mode.py`, `incorporate_mode.py`, `finetune_mode.py` | aceitar `overwrite`/`backup`; escrita atômica | Baixo-Médio |
| synesis-coder | `project_loader.py` | param `tolerate_annotation_errors` + classificação de erro por origem | **Médio** — entry point compartilhado; exige gitnexus_impact |
| synesis | — | nenhuma | — |
| synesis-lsp / explorer | — | nenhuma | — |

---

## 6. Ordem de Implementação

1. **R1a** em `project_loader.py` (desbloqueia o fluxo). Rodar `gitnexus_impact(load_project)` antes.
2. Ligar `document_mode` ao `tolerate_annotation_errors=True`.
3. **R2** no `cli.py` (`document` primeiro; depois replicar nos demais geradores).
4. **R3** (escrita atômica) nos processadores.
5. **R4** (`--backup`).
6. Estender R2-R4 a `ontology`, `incorporate`, `finetune`.

## 7. Testes

- `test_project_loader`: `tolerate_annotation_errors=True` com `.syn` inválido → retorna ctx
  + warning; erro de template ainda aborta; erro de bibref ainda aborta.
- `test_document_mode` / `test_cli`:
  - output inexistente → grava sem prompt.
  - output existe + `--overwrite` → grava.
  - output existe, sem flag, TTY simulado confirmando/negando.
  - output existe, sem flag, não-TTY → abort com exit≠0 e mensagem citando `--overwrite`.
  - escrita atômica: interrupção simulada não corrompe o output existente.
  - `--backup`: gera `.bak` com o conteúdo anterior.
- Contrato CLI (`test_cli.py`): `--help` dos modos geradores lista `--overwrite` e `--backup`.

## 8. DoD

- [ ] gitnexus_impact rodado para `load_project` e callers d=1 verificados
- [ ] Testes novos passando
- [ ] Type hints e docstrings nas funções modificadas
- [ ] CHANGELOG.md `[Unreleased]` atualizado
- [ ] gitnexus_detect_changes confirma escopo restrito a synesis-coder
- [ ] Sem regressão nos modos não-geradores (item, critique, normalize)

## 9. Observação — fora do escopo desta correção

O `.syn` gerado também viola o contrato de co-dependência `criterio_5a`↔`score_sugerido`
(marcado OPTIONAL no parser) e gerou `score_sugerido` como string (`Score_1`, `Presente`)
num campo `SCALE [1..4]`. Isso é qualidade da extração LLM (prompt/pós-processamento),
investigação separada — não é resolvido por este plano, que trata apenas do
desbloqueio do fluxo e da segurança de escrita. Ver memória `face85_gemini_coder_behavior`
para padrão correlato de defeitos por provider.
