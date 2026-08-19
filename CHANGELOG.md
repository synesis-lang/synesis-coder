# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [0.9.0] — 2026-08-18

Robustez para campanhas longas (milhares de registros) e revisão calibrada.

Motivada por duas frentes de estudo com medição sobre corpus real: a lacuna de
escopo e calibração do modo `critique`, e a integridade da saída do modo
`abstract` em processamento de larga escala.

### Fixed — perda de dados no modo `abstract` (crítico)

- **Um único timeout abortava a campanha inteira.** No caminho de exceção do
  LLM, o `.syn` gravado continha **apenas comentários** — e um `.syn` só de
  comentários não é parseável. Como cada batch recarrega o projeto com glob de
  `annotations/*.syn`, o batch seguinte levantava `SynesisSyntaxError` e a
  execução morria. O bloco de falha agora traz um `SOURCE` sintaticamente
  válido, com sentinelas compatíveis com o TIPO de cada campo obrigatório
  (texto livre em `ENUMERATED` produziria `InvalidEnumeratedValue`).
  `tolerate_annotation_errors` não cobria o caso: a exceção é de *parse*,
  anterior à tolerância.
- **A recarga de projeto entre batches virou tolerante a falha**, seguindo com
  o contexto anterior. Um `.syn` malformado no diretório de saída — de edição
  manual ou de outra ferramenta — não derruba mais horas de trabalho.
- **O modo arquivo único truncava a saída a cada batch.** `annotations.syn` era
  reescrito com os registros *do batch corrente*: numa campanha de 2.800
  referências com `--batch-size 25` restariam **25 anotações**, e o resumo
  reportaria "2.800 OK". Os blocos passam a acumular entre batches.
- **Escrita agora é atômica** (`safe_write_output`, tmp + `os.replace`) em todos
  os modos. Antes, `Path.write_text` podia deixar arquivo truncado numa
  interrupção. Medido: reescrever o arquivo inteiro a cada batch custa o mesmo
  que append (0,17 s em 112 batches), com garantia mais forte.
- **Ordem de saída determinística.** As tarefas concluíam fora de ordem
  (`as_completed`), então os blocos apareciam embaralhados e o arquivo variava
  entre execuções. A gravação passa a seguir a ordem do `.bib`.
- **Metadados do `.synr` deixaram de virar valor de campo.** `_META_TAGS` não
  incluía `model`, `timestamp` nem `threshold`: um template com campo homônimo
  receberia o cabeçalho da revisão como correção.

### Fixed — perda silenciosa de dados no `incorporate`

- **Correções de `chain` colidiam e destruíam anotações.** Quando várias chains
  compartilham o nó-fonte — padrão comum de `APPLIES` —, o casamento por raiz
  não as distingue: a correção era aplicada à **primeira** ocorrência,
  apagando um valor que ela não endereçava enquanto o alvo real sobrevivia,
  e o relatório informava `changed=2` para uma linha alterada. Agora linhas
  consumidas são marcadas, e casamento ambíguo é **rejeitado** em vez de
  adivinhado. Sobre o corpus face85: 25 correções perigosas rejeitadas (15%),
  nenhuma chain perdida.
- **Correções byte-idênticas** para o mesmo campo são descartadas (rascunho do
  modelo, não duas correções legítimas).
- **Primitiva de remoção**: `# $chain: none` / `(none)` remove a ocorrência em
  vez de gravar a string literal `none` como valor.

### Added — campanhas longas (`abstract`)

- **`--resume`** — pula referências já anotadas com sucesso. O estado vem dos
  próprios `.syn`, não de um manifesto: um arquivo de progresso seria uma
  segunda fonte de verdade capaz de divergir do disco. Exige `SOURCE` **e**
  ao menos um `ITEM` **e** ausência da marca de falha — sem a terceira
  condição, um registro que falhou por zero ITEMs seria confundido com
  trabalho pronto.
- **`--split-every N`** — grava N referências por arquivo
  (`annotations_0001.syn`, …). O índice deriva da **posição global** do
  registro, o que mantém o mapeamento estável sob `--resume`. Corte apenas em
  fronteira de registro. Mutuamente exclusivo com `--per-reference`, validado
  antes de qualquer chamada de API.
- **`--overwrite` / `--backup`** — o comando não os tinha, ao contrário de
  `ontology`, `document` e `incorporate`. Verificados **uma vez**, antes de
  gastar API.
- **`--cooldown`** (`auto` | segundos | `0`).

### Changed — pausa entre batches proporcional ao trabalho

A fórmula usava o tempo **acumulado desde o início da execução**, saturando o
teto de 30 s por volta do 5º batch e permanecendo lá. Numa campanha de 112
batches isso custava ~55 min de `sleep`, sem relação com pressão de rate limit
— que depende da taxa recente de requisições, não de há quanto tempo o processo
roda. A pausa passa a ser proporcional à duração do **batch corrente**:
**~43 min economizados** em 2.800 registros.

### Added — `anchor`, verificação sem LLM

Novo comando que confere se o trecho de cada ITEM ocorre de fato na fonte.
Determinístico e gratuito. Complementa o compilador, que já valida enums,
faixas, `REQUIRED`, `BUNDLE` e relações de chain, mas não a ancoragem do
trecho — a única classe da proposta original que ele não cobria.

Ancoragem **factual**, não literalidade byte-a-byte: normaliza aspas
tipográficas, espaçamento, travessões, caixa e escapes LaTeX (`BM\&FBOVESPA`).
Sobre o face85 encontrou 4 defeitos reais em 108 ITEMs — três originados de
sujeira de PDF que o extrator limpou em silêncio.

### Changed — modo `critique` reescrito

- **Escopo do trecho.** O crítico recebia o abstract **inteiro** como fonte,
  nunca o recorte que o ITEM anota — logo julgava cada ITEM contra o artigo
  completo. O campo `text` passa a ser o `<target>`, com o abstract como janela
  de ±300 caracteres ao redor. Medido: **−45,8% de tokens** e 92,6% de
  ancoragem. Acompanha o índice `ITEM n de N` por bibref.
- **Regra de deferência.** Sem contrapeso, o modelo era recompensado por
  encontrar problemas: 78 revisões em 108 ITEMs. A régua instrui a atribuir
  `none` quando o template **admite** a anotação sob leitura razoável, e a não
  exigir precisão que as guidelines não pedem. Efeito isolado medido:
  concordância **0,306 → 0,463**, 5,6× o ganho do escopo.
- **Taxonomia universal de `reason`.** As cinco categorias anteriores chegavam
  ao modelo como nomes crus, sem definição, e duas concentravam 73% das
  ocorrências; `anchor_missing` cobria seis defeitos não relacionados. Sete
  categorias novas — `unsupported`, `overstated`, `inverted`, `granularity`,
  `infidelity`, `incomplete`, `none` — definidas pela **relação** entre
  anotação e fonte, sem citar nome de campo. A aplicabilidade é **derivada do
  template**: num template sem `CHAIN`, `inverted` e `granularity` não são
  emitidas. Validado em três templates estruturalmente distintos.
- **`reason` é validado contra o enum.** Antes qualquer string passava com
  default silencioso.
- **Aviso de calibração** quando a concordância fica abaixo de 0,70 — sinal de
  revisor descalibrado ou limiar baixo, não necessariamente de anotação ruim.

### Changed — vocabulário do `.synr` (formato 2)

O cabeçalho falava a língua da auditoria (`suspicion`, `flagged`, `threshold`),
inadequada a pesquisa qualitativa, cuja prática de referência é revisão por
pares. Precisava anexar `.formula` e `.description` a cada métrica para ser
legível — sintoma de que o nome não comunicava.

| formato 1 | formato 2 |
|---|---|
| `phase: critique` | `phase: review` |
| `suspicion_score` | `divergence` |
| `reason_detail` | `comment` |
| `threshold: 0.2` | `sensitivity: standard` |
| `metrics.items_flagged` | `metrics.items_to_review` |
| `metrics.suspicion_rate: 0.722` | `metrics.agreement: 0.278` |

A inversão para `agreement` alinha a métrica à direção em que o pesquisador
pensa — quer maximizar — e usa a mesma escala já adotada nos estudos para
comparação com padrão ouro, dispensando a nota explicativa.

`--sensitivity lenient|standard|strict` substitui o número mágico;
`--threshold` segue aceito como forma legada. **`.synr` do formato 1 continuam
legíveis** por `refine` e `incorporate`.

### Changed — ajuda da CLI

Reorganizada segundo o modelo do compilador: copyright, URL e licença no
cabeçalho, e comandos agrupados por **finalidade do pesquisador** (anotar um
corpus, construir a ontologia, revisar anotações, verificar sem LLM, dados de
treino) em vez de por estágio interno do pipeline.

### Internal

- `revision_vocab.py` e `critique_taxonomy.py` como fontes únicas. `_META_TAGS`
  e `_CRITIQUE_META_TAGS` eram duas listas literais duplicadas dos mesmos
  nomes, sem nada que as mantivesse sincronizadas — renomear com a duplicação
  de pé teria reintroduzido o defeito.
- `_build_critique_source` separada de `_get_source_text`: o `refine` consome a
  segunda para **re-extração** e precisa do texto integral; marcadores
  `<target>` vazariam para um prompt onde não fazem sentido.
- Suíte: 695 testes (era 557).

---

## [0.8.0] — 2026-08-11

Primeira publicação no PyPI.

### Security — CI e publicação

- **Todas as GitHub Actions pinadas por SHA de commit** (`.github/workflows/ci.yml`).
  As entradas `uses:` referenciavam tags mutáveis (`@v4`, `@v5`,
  `@release/v1`), então uma release comprometida ou re-taggeada rodaria no CI
  sem qualquer alteração neste repositório. Alinha com `synesis`,
  `synesis-lsp` e `synesis-graph`, que já pinavam por SHA. Cada SHA foi
  verificado contra a API do GitHub antes de ser aplicado.
- **Novo job `security`**, espelhando os outros pacotes Python do ecossistema —
  era o único dos quatro sem ele:
  - `pip-audit` sobre as dependências de runtime declaradas no
    `pyproject.toml`. Não instala o próprio pacote nem os extras `[dev]`, para
    que a auditoria seja determinística e não falhe por CVE em ferramenta de
    build.
  - Varredura de segredos com Gitleaks sobre o histórico completo
    (`fetch-depth: 0`).
  - O runner da auditoria é fixado em Python 3.11 porque o passo lê o
    `pyproject.toml` com `tomllib` (stdlib só a partir do 3.11). É a versão do
    RUNNER, não o piso do pacote: `requires-python = ">=3.10"` continua valendo
    e a matriz de testes segue cobrindo 3.10.
- **`on.push` não disparava em tags.** Faltava `tags: [ 'v*' ]`, então
  `git push origin v0.8.0` nunca acionaria o workflow — e portanto nunca
  publicaria. Sem isso toda a automação de release seria inerte.
- **Scripts órfãos removidos do controle de versão** — `interview_processor.py`,
  `abstract_processor10.py`, `semantic_memory_builder.py` e
  `topic_processor.py` (protótipos anteriores ao pacote, ~160 KB) viviam na
  raiz, rastreados desde o commit inicial. Já constavam do `.gitignore` sob
  "Sample codes", mas `.gitignore` não desrastreia o que já está rastreado.
  Nenhum era importado pelo pacote, pelos testes ou pela documentação.

### Fixed — `_get_model_output_cap` quebrava com client mockado

- **10 testes falhavam com `TypeError` fora do ambiente de desenvolvimento**
  (`llm_client.py`). `getattr(model_info, "max_tokens", 0)` num `MagicMock`
  devolve **outro MagicMock**, não o default `0` — então o `cap > 0` seguinte
  comparava MagicMock com int e estourava.
  - Invisível localmente: só aparece quando o client é mockado *e* o corpus de
    fixtures está ausente, combinação que só ocorria no CI.
  - O helper `_int_attr()` já existia exatamente para isto (checa o tipo em vez
    de confiar no default do `getattr`) e era usado em 4 pontos — menos neste.
    Agora também aqui.

### Fixed — suíte dependia de caminho absoluto da máquina de desenvolvimento

- **9 dos 12 jobs de teste falhavam no CI.** Doze arquivos de teste fixavam
  `d:/GitHub/case-studies` — um caminho absoluto que não existe em runner
  algum. A suíte passava localmente e falhava em todas as plataformas.
  - Não era regressão: o CI nunca havia executado a suíte de verdade, porque
    o passo antigo tolerava o exit code 5 do pytest (corrigido nesta mesma
    versão). Ao passar a executar, o defeito latente apareceu.
  - `tests/conftest.py` passa a resolver o corpus por `SYNESIS_CASE_STUDIES`,
    com o caminho antigo como default — comportamento local inalterado.
    Ausente o diretório, os testes que dependem dele são **pulados**, não
    falham.
  - A detecção lê o atributo `CASES_DIR`/`_PROJECT` do módulo em vez de manter
    uma lista de arquivos no conftest, para continuar correta quando um teste
    novo passar a depender do corpus.
  - Verificado apontando `SYNESIS_CASE_STUDIES` para um caminho inexistente:
    **272 passed, 272 skipped, 0 failed** (antes: 9 jobs vermelhos). Com o
    corpus presente: 544 passed, inalterado.

### Fixed — guarda dos testes de integração era inerte

- **O `skipif` que protegia os testes de chamada real de API nunca disparava.**
  Os módulos definiam `HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))`
  no nível do módulo — avaliado no **import**, poucas linhas depois de
  `load_dotenv()`. Em qualquer máquina com `.env` (todo ambiente de dev) o valor
  era sempre `True` e o `skipif` era código morto. A única proteção efetiva era
  o marker `integration`, deselecionado por `addopts`.
  - O sintoma inverso também existia: **sem** chave, esses testes não pulavam —
    falhavam adiante com `OSError: ANTHROPIC_API_KEY não encontrada`, vindo de
    dentro do client. Medido: `pytest -m integration` sem chave dava
    **5 failed em 61s**; agora dá **14 skipped em 0,7s**.
  - Novo `tests/conftest.py` com `pytest_collection_modifyitems`, que roda
    **depois** de todos os imports e portanto lê o ambiente já com o `.env`
    aplicado. Uma única guarda cobre os quatro módulos de integração e não
    depende de ordem de import.
  - Cobre também `test_ontology_mode.py`, cujos 3 testes de integração não
    tinham guarda de credencial alguma — só o marker.
  - `HAS_API_KEY` e o `skipif` removidos dos três módulos (e o import `os` que
    ficou órfão). O marker `integration` é preservado: a defesa em profundidade
    passa a ser `addopts` + comando explícito no CI + ausência de credencial no
    CI + esta guarda.

### Added — GitHub Release automática

- O job `publish` passa a criar a **GitHub Release** ao publicar no PyPI,
  extraindo o corpo da seção `## [X.Y.Z]` correspondente deste arquivo — sem
  duplicar o texto à mão. Antes, uma tag publicava no PyPI e deixava a aba
  Releases desatualizada.
  - Exige `permissions: contents: write` além do `id-token: write` do OIDC.
  - A extração falha explicitamente (`::error::` + `exit 1`) quando a versão
    não tem seção no CHANGELOG, em vez de criar uma Release vazia em silêncio.
  - `softprops/action-gh-release` pinada por SHA, como as demais.
  - Processo documentado em `RELEASING.md` (novo), no `synesis-graph`.

### Added — contrato de empacotamento (pré-PyPI)

- **`tests/test_packaging.py`** (9 testes) — constrói o sdist de verdade e
  inspeciona o `PKG-INFO` gerado, em vez de confiar no que o `pyproject.toml`
  declara. Publicar no PyPI é irreversível: o nome fica reservado para sempre e
  uma versão enviada nunca pode ser sobrescrita.
  - Licença: `License-Expression` PEP 639 correta, **ausência** do campo
    obsoleto `License:`, e ambos os arquivos (`LICENSE`, `LICENSE.exception`)
    declarados **e** empacotados — a exceção só vale se o arquivo dela viajar
    junto.
  - Conteúdo: nenhum `config.toml` (carrega chave real), `.db`, `.env` ou
    `.vsix` no artefato; e nenhum dos quatro scripts órfãos removidos acima.
  - Consistência: versão do sdist, do `CITATION.cff` e deste arquivo conferidas
    contra o `pyproject.toml` — o CFF defasado já aconteceu duas vezes no
    ecossistema.
  - Verificado por mutação: trocando a licença pela sintaxe legada
    `{text = "..."}`, 7 dos 9 falham. `twine check` **passa** nesse cenário — é
    por isso que ele não basta.

### Fixed — metadados de publicação

- **`pyproject.toml` não declarava `authors`** — o pacote seria publicado sem
  creditar o autor. Adicionados também `description`, `readme`, `keywords`,
  `classifiers` e `[project.urls]`, todos ausentes.
- **`CITATION.cff` citava a obra errada** — `title` trazia *"Synesis: A DSL
  compiler for knowledge engineering"*, o título do compilador, então quem
  citasse o synesis-coder creditaria o pacote errado. (O mesmo título copiado
  está no `synesis-lsp` — vale corrigir lá também.)

### Fixed — documentação

- **Matriz de compatibilidade do README desatualizada em todas as linhas:**
  `synesis 0.5.5` (real: 0.11.0), `synesis-coder 0.7.3` (0.8.0), `synesis-lsp
  0.15.4` (0.22.0), `synesis-graph 0.2.0` (0.5.0), e o constraint listado como
  `≥0.5.5` quando os três consumidores exigem `>=0.10.0`. A seção Requirements
  repetia o mesmo `≥ 0.5.5`.
- **O subcomando `dataset` não era documentado** no README, embora exposto na
  CLI e descrito abaixo nesta mesma versão. Seção adicionada com as opções
  reais lidas do `--help`. `dataset_mode.py` e `prompt_dump.py` também faltavam
  na árvore de arquivos.
- **Duas variáveis de ambiente lidas em runtime não constavam do README:**
  `SYNESIS_CODER_MAX_TOKENS` (`llm_client.py`) e `SYNESIS_CODER_LANGUAGE`
  (`project_loader.py`).

### Fixed — CI

- **CI reportava verde com zero testes executados.** O passo de testes
  tolerava o exit code 5 do pytest (`no tests collected`) como sucesso:

  ```
  python -c "... sys.exit(0 if code == 5 else code)"      # antes
  pytest -m "not integration" --cov=synesis_coder ...     # agora
  ```

  Não era hipotético: o mesmo padrão mascarou uma quebra real no
  `synesis-lsp`, onde um `jsonschema` desatualizado fazia `test_contract.py`
  falhar no import, abortando a coleta da suíte inteira enquanto o CI seguia
  verde. Corrigido em `synesis`, `synesis-lsp`, `synesis-graph` e
  `synesis-coder`. O filtro `-m "not integration"` foi preservado.

- **`ruff check synesis_coder/` falhava** por blocos de import fora de ordem em
  `modes/abstract_mode.py`, `modes/dataset_mode.py` e `modes/document_mode.py`.
  Como o ruff é bloqueante no CI, isso sozinho quebrava o pipeline. Suíte
  reverificada após a correção: 505 passaram, 14 deselecionados.

### Fixed

- **Campos SOURCE não recebiam os valores permitidos no prompt**
  (`prompt_builder.py`) — `_build_source_fields_section` montava a instrução com
  `guidelines or description or genérica`, **sem** chamar `_field_instruction`.
  Os escopos ITEM e ONTOLOGY chamavam. Consequência: um `ENUMERATED` em SOURCE
  chegava ao modelo sem a lista de valores, um `SCALE` sem a faixa, um `CHAIN`
  sem as RELATIONS.
  - **Medido no face85** (FACE/UFMG, ~2.500 abstracts): a GUIDELINE de
    `knowledge_area` diz *"O valor deve ser exatamente uma das opções acima"* —
    e nenhuma opção era enviada. No caminho JSON o `enum` do schema ainda
    restringia a saída; no caminho de **texto livre** não havia defesa alguma.
    Coerente com os valores inválidos encontrados no `.syn` do projeto
    (`Business_Administration`, `Not_Specified`, ambos fora dos 8 valores).
  - As **descrições** dos valores (`Administração — Teoria organizacional,
    pesquisa operacional…`) nunca chegavam ao modelo por via alguma: o `enum`
    do schema carrega apenas os labels. São elas que desambiguam a escolha.
  - `_field_instruction` ganha o parâmetro `fallback`, e SOURCE passa o seu
    genérico **por nome** (`_generic_source_instruction`). Sem isso, adotar o
    genérico por tipo degradaria campos de metadado documental — `description`
    cairia de *"Describe the study objective and scope"* para *"Provide relevant
    descriptive text"*. Há teste de regressão para exatamente esse ponto.
- **Resposta não-JSON não era re-tentada nem contabilizada** (`llm_client.py`) —
  `call_json`/`call_json_async` devolviam `None` quando `_parse_json_response`
  falhava, sem re-tentar **e sem incrementar `schema_fallbacks`**. O registro
  saía em texto livre, sem as restrições do schema, e nada no pipeline
  registrava a degradação — nem o aviso de fim de execução, que lê esse mesmo
  contador.
  - Verificado antes da correção: resposta `"isto nao e json"` →
    `retorno=None`, `schema_fallbacks=0`, `api_calls=0`.
  - Dos gatilhos de fallback, apenas `TokenBudgetExhausted` re-tentava. Erro do
    backend (400/rede) segue sem retry — repetir não ajuda —, mas já
    contabilizava.
  - Agora: **1 re-tentativa** com `temperature=0.2`. A geração roda em `0.0`;
    repetir com o mesmo valor tende a reproduzir a mesma saída malformada — é a
    lógica de `validator.CORRECTION_TEMPERATURES`. Retry bem-sucedido **não**
    conta como fallback (o schema foi preservado); só a desistência conta.
    Caminho feliz inalterado: uma chamada, custo zero.
- **Envelope JSON com forma errada caía em silêncio** (`abstract_mode.py`) — um
  dict sem as chaves `source`/`items` fazia o modo cair para texto livre sem
  registro algum: `call_json` já havia devolvido um dict (logo, não contabilizou
  fallback) e o bloco resultante saía válido e marcado OK. Agora emite WARNING
  **nomeando o bibref** e as chaves recebidas, e contabiliza o fallback.
- **A indentação do bloco deixa de depender do modelo** (`block_assembler.py`,
  `validator.py`) — a moldura do bloco é FORMA, não conteúdo. No caminho JSON o
  `_assemble_block` já emitia `_INDENT` por construção; o caminho de texto
  livre entregava o que o LLM digitasse. Novo `normalize_indentation()`
  reescreve a indentação para a forma canônica (abertura/`END` na coluna 0,
  campos com 4 espaços), encadeado em `_strip_markdown_fences` — o ponto de
  passagem obrigatório de todo texto que chega ao validador (12 sítios), o que
  torna a garantia universal sem tocar em cada laço.
  - **Medido em produção** (`inclusionai/ling-2.6-flash`): um registro emitiu o
    SOURCE sem indentação alguma, o parser LALR rejeitou com `Token inesperado
    TEXT_LINE`, as 3 correções falharam e o registro foi **perdido**. Outro usou
    2 espaços. Reprocessando o arquivo real: **PARSE FAIL → compila com 0
    erros**, 15 ITEMs recuperados.
  - Deliberadamente mínima: não altera valores, preserva linhas em branco e
    texto fora de blocos (`# ERRO`), e é idempotente.
- **Loop degenerativo do modelo gerava ITEMs duplicados sem detecção**
  (`block_assembler.py`, `dataset_mode.py`, `abstract_mode.py`) — modelos fracos
  re-emitem o mesmo ITEM até esgotar o orçamento. Medido: **121 ITEMs para 22
  únicos** num registro, com um `criterio` repetido 88 vezes. Nada barrava:
  ITEMs repetidos são sintaticamente válidos (um código PODE reaparecer em
  trechos distintos), o compilador aceita, o schema não limita contagem e a
  guarda de idempotência só rejeita PERDA. Novo `dedupe_item_blocks()`,
  determinístico por texto do bloco (espaços colapsados) — dois ITEMs com o
  mesmo `criterio` mas trechos diferentes são preservados. O `document_mode` já
  tinha dedup para o caso análogo de chunks; abstract/dataset não tinham.
  Reprocessando o arquivo real: **121 → 22 ITEMs, 99 duplicatas removidas**.
- **Registro sem nenhuma anotação era reportado como OK**
  (`dataset_mode.py`, `abstract_mode.py`) — `validate_and_fix` garante SINTAXE,
  não COBERTURA. Um `.syn` com SOURCE e zero ITEMs é válido: `ok=True` voltava,
  o arquivo era gravado e o resumo contava como sucesso. Em lote grande isso é
  invisível — o operador vê "N OK" sem saber que k registros saíram vazios.
  Agora `count_item_blocks() == 0` rebaixa para falha, com log em ERROR e status
  `SEM ITEMs` distinto de `FALHA NA VALIDAÇÃO`.
  - Testes: `tests/test_output_normalization.py` (novo, 18 casos) — indentação
    (incluindo os dois casos reais), idempotência, preservação de valores e de
    texto fora de blocos, dedup (incluindo a forma real 22/121) e contagem.
    Suíte: 487 → 505 passed.

- **Caminho JSON caía para texto livre em silêncio quando o raciocínio esgotava
  o orçamento de tokens** (`llm_client.py`) — modelos de raciocínio podem pensar
  por conta própria mesmo com `thinking=False` no payload (o coder não controla
  isso). Quando o raciocínio consumia todo o `max_tokens`, a resposta chegava
  com `stop_reason="max_tokens"` e só blocos de thinking; o `RuntimeError`
  genérico era capturado por `call_json`, logado em WARNING e convertido em
  `None` — "caia para texto livre". O registro saía válido e marcado OK, sem
  nada indicando que perdera enum/minimum/maximum/additionalProperties.
  - Nova exceção tipada `TokenBudgetExhausted` distingue a condição operacional
    (orçamento insuficiente, corrigível) da limitação de backend (schema não
    suportado) — que exigem ações opostas e antes se confundiam no mesmo log.
  - **Retry automático** com o dobro do orçamento (`_retry_max_tokens`, teto de
    64.000) antes de desistir. Os tokens da primeira tentativa já foram pagos;
    refazer em texto livre pagaria de novo sem recuperar as garantias.
  - `_call_sync_inner` ganha `force_max_tokens=`, que **vence
    `SYNESIS_CODER_MAX_TOKENS`** — sem isso o retry repetiria o mesmo orçamento
    que acabou de estourar (o env var vencia tudo na precedência anterior),
    tornando a correção inerte exatamente no cenário de produção que a motivou.
  - Desistência definitiva agora loga em **ERROR** (não WARNING), nomeando as
    garantias perdidas e a variável a ajustar.
- **Fallbacks de schema eram invisíveis** (`token_usage.py`, `llm_client.py`) —
  em lote, alguns registros rodavam com schema e outros sem, e a diferença não
  aparecia em lugar nenhum. Novo contador `schema_fallbacks` +
  `record_schema_fallback()`, exibido em `summary_line()` só quando não-zero.
- **O loop de correção podia truncar o arquivo** (`validator.py`) — caso
  documentado em produção: 19 ITEMs → 1, com perda do bloco SOURCE. Como a saída
  seguia sintaticamente válida, nada detectava a mutilação. Nova guarda
  `_accept_fix()` rejeita correções que **perdem** blocos (menos ITEMs, ou
  SOURCE que existia e sumiu) e mantém a versão anterior, logando o motivo.
  Deliberadamente conservadora: contagem igual ou maior passa, porque dividir um
  ITEM malformado em dois é resultado legítimo. Aplicada nos 4 pontos de
  reatribuição de `output` (item e annotation, ambos os laços).
- **`cache_control` era descartado para Anthropic via OpenRouter**
  (`_translate_messages_openai`) — a maioria dos provedores OpenAI-compatíveis
  faz caching automático por prefixo, mas Anthropic e Qwen exigem breakpoints
  explícitos. Sem eles, o system prompt (grande e estável) era reprocessado a
  preço cheio em toda chamada. `_provider_requires_explicit_cache()` detecta
  pelo prefixo do ID (`anthropic/`, `qwen/`) e emite o content-block com
  `cache_control`; os demais seguem como string simples.
  - Testes: `tests/test_json_path_and_guards.py` (novo, 25 casos) cobrindo os
    quatro defeitos — exceção tipada vs. genérica, retry e teto, precedência
    sobre o env var, contabilização, guarda de idempotência (incluindo o caso
    real de truncagem) e detecção de provedor. Suíte: 462 → 487 passed.

- **O loop de correção descartava o system prompt (GUIDELINES)** (`llm_client.py`,
  `validator.py`) — `fix()`/`fix_async()` montavam a chamada de correção como uma
  única mensagem `role: "user"` (bloco anterior + diagnóstico + "conserte"), sem
  nenhuma mensagem `system`. Como o branch Anthropic só anexa `system` quando
  `system_blocks` é não-vazio, e o branch OpenAI apenas repassa as mensagens, a
  correção ia à API **sem as GUIDELINES do template** — réguas de score,
  proibições de domínio, regras de multiplicidade e `code_index`.
  - **O efeito era cumulativo:** `validate_and_fix[_async]` reatribui `output` a
    cada iteração (até 3, com temperatura escalando 0.0→0.2→0.5), então cada
    rodada corrigia um artefato já produzido sem as regras, também sem as regras.
    Isso explica a degradação progressiva observada em produção.
  - **Agnóstico de modelo:** diferente dos defeitos que um modelo forte mascara,
    aqui a instrução literalmente não estava na chamada — nenhum modelo podia
    obedecer uma régua que não recebeu.
  - Correção: `_build_fix_messages()` (novo) insere o system como PRIMEIRA
    mensagem, marcada `cache: True`. `fix`/`fix_async` ganham os parâmetros
    `system=` e `schema=` (ambos opcionais, default `None` → comportamento
    antigo preservado).
  - `validator.py` ganha `_fix_system_prompt(ctx, scope)`, que remonta o prompt
    a partir do mesmo `ctx` da geração — `"item"` (item/document/refine),
    `"abstract"` (abstract/dataset) e `"ontology"`. Falha na remontagem degrada
    graciosamente para `None` (correção cega, como antes), nunca derruba a
    validação. As **8** chamadas a `fix`/`fix_async` no validator repassam o
    system, incluindo os caminhos de erro de parse e as duas funções de ontology.
  - **Ganho de custo, não só de qualidade:** sendo byte-a-byte idêntico ao da
    geração, o prefixo casa com o cache já gravado — o reenvio custa ~0.1x
    (Anthropic) ou ~0.25–0.5x (OpenAI-compat, cache automático) em vez de 1.0x.
    Antes desta correção o coder pagava o prêmio de escrita do cache em toda
    geração e descartava o prefixo justamente na única chamada que o reusaria.
  - **`schema=` foi adicionado à assinatura de `fix`/`fix_async`, mas o
    validator deliberadamente NÃO o usa** — e o defeito irmão (fix perde as
    garantias estruturais do schema) **continua aberto**. Motivo: com schema o
    modelo devolve JSON de valores, enquanto o laço de correção trata o retorno
    como texto Synesis (`_extract_*` → `synesis.load()`). Propagá-lo faria o
    JSON cru chegar ao compilador e falhar em toda tentativa. Fechar aquele
    defeito exige um caminho JSON completo para a correção (prompt de valores +
    schema + `block_assembler` no retorno) — mudança de escopo maior,
    documentada em `validator.py` e no estudo §6.2.
  - `document_mode` e `refine_mode` passam `scope="item"` (geram só ITEMs);
    os demais usam o default do escopo correspondente.
  - **Testes**: `tests/test_fix_preserves_context.py` (novo, 16 casos) —
    montagem das mensagens, presença de `system` + `cache_control` no payload
    real dos dois backends, propagação de `schema`, os três escopos de
    reconstrução, degradação graciosa e repasse em ambos os validators.
    15 dos 16 foram verificados falhando contra a versão anterior.
    Suíte: 446 → 462 passed.
  - Estudo completo: `Planning/Estudo_Fix_Perde_System_Prompt.md`.

### Added

- **`--prompt-only`: extração do prompt montado** (`prompt_dump.py` novo,
  `cli.py`, modos `item`/`abstract`/`document`/`ontology`) — grava em Markdown
  o prompt que **seria** enviado ao modelo, e encerra. Nenhuma chamada LLM,
  nenhum token gasto.
  - Destino padrão `<projeto>_<modo>_prompt.md` ao lado do `.synp`;
    `--output` escolhe outro caminho (criando diretórios intermediários), e no
    `abstract` o `--output-dir` é respeitado. O caminho é reportado em
    **stderr por escrita direta**, não pelo logger: `-q` eleva o nível para
    WARNING e engoliria um `log(22)`/DEST — e `-q` é justamente o que se usa
    para calar o banner neste modo. Com `-qq` a saída é uma linha: o arquivo
    gerado.
  - Arquivo, não stdout, porque o artefato é para leitura e revisão — o dump
    do face85 passa de 460 linhas, tamanho em que rolar o terminal não serve.
  - Motivação: a qualidade da extração é governada pelas GUIDELINES do
    template, e não havia como lê-las na forma renderizada sem executar o
    pipeline. O `--debug` (`debug_log.py`) grava os prompts, mas só **depois**
    de rodar e pagar.
  - Reusa as funções de `prompt_builder` que rodam em produção — o dump não
    pode divergir do prompt real. O caminho (JSON vs texto livre) é resolvido
    como os modos resolvem: `resolve_path()` espelha
    `supports_json_schema()` **sem instanciar `LLMClient`**, para que a
    inspeção não exija credencial de API.
  - Saída autossuficiente: seções `## SYSTEM` / `## USER` em blocos de código,
    com cerca dimensionada ao conteúdo (GUIDELINES podem conter crases).
    Serve para colar num chat, alimentar um harness de teste de prompt, ou
    versionar junto a uma revisão do `.synt`.
  - `--output`/`--output-dir` deixam de ser obrigatórios nesses quatro
    comandos, com guarda explícita que preserva a exigência fora do
    `--prompt-only` (`Missing option '--output' (required unless
    --prompt-only)`).
  - Anotações desatualizadas em relação ao template **não** bloqueiam a
    inspeção (`tolerate_annotation_errors=True`): revisar GUIDELINES é o que se
    faz enquanto o template muda e o corpus antigo ainda não migrou. No modo
    `ontology` o `.syno` é dispensado pelo mesmo motivo.
  - Encontrou dois defeitos reais no primeiro uso: a ausência dos VALUES em
    SOURCE (corrigida acima) e, no `face85.synt`, a contradição entre
    `EXISTING PROJECT CONCEPTS` em snake_case e a regra PascalCase do campo
    `code` — esta última é conteúdo de template, não do coder.
- **Aviso de degradação silenciosa do schema** (`runtime_info.py`, quatro modos
  geradores) — novo `warn_schema_fallbacks()`, emitido em **WARNING** ao fim da
  execução quando algum registro caiu para texto livre.
  - O contador `schema_fallbacks` já existia, mas só aparecia em
    `usage.summary_line()` — emitido **apenas** com `--format verbose`. No
    formato padrão o pesquisador via `OK: 3 (100%)` sem sinal algum de que
    parte do corpus rodou sem `enum`, `minimum/maximum` e
    `additionalProperties`.
  - WARNING (não INFO) porque a condição é corrigível — aumentar
    `SYNESIS_CODER_MAX_TOKENS` cobre o caso dominante — e porque o silêncio
    aqui compromete a validade do dado, não apenas o custo. Sobrevive a `-q`.
  - **Limitação conhecida:** o aviso é agregado por execução ("N registros"),
    não identifica quais — exceto no caso do envelope inválido, que nomeia o
    bibref. Ver *Registrado para implementação futura* abaixo.
- **Testes**: 30 novos casos, todos verificados falhando contra a versão
  anterior — `tests/test_source_field_values_and_fallback_warning.py` (11:
  ENUMERATED/ORDERED/SCALE em SOURCE, descrições dos valores, precedência da
  GUIDELINE, preservação do fallback por nome, ausência de bloco espúrio em
  campo sem VALUES, e o aviso de fallback), `tests/test_malformed_json_retry.py`
  (10: retry sync e async, temperatura elevada, retry bem-sucedido não conta
  fallback, desistência conta, caminho feliz sem chamada extra), 3 em
  `tests/test_abstract_mode.py` (envelope inválido) e 6 em `tests/test_cli.py`
  (contrato do `--prompt-only`: presença no help dos quatro modos, nome padrão
  derivado de projeto+modo, `--output` explícito com criação de diretório, e o
  destino em stderr — regressão do caso em que `-q` engolia a linha).
  Suíte: 505 → 535 passed.

### Registrado para implementação futura

- **Marcar no próprio `.syn` os registros gerados sem as garantias do schema.**
  O aviso de `warn_schema_fallbacks()` é agregado por execução: informa que N
  registros degradaram, não **quais**. Num corpus de 3 abstracts isso basta;
  nos ~2.500 do face85, não — o pesquisador saberia que há um problema sem
  saber onde auditar, e o `.syn` gravado não carrega vestígio algum da
  degradação (o bloco é sintaticamente válido e conta como OK).
  - Forma provável: um comentário no bloco do registro afetado (ex.
    `# schema-fallback: gerado em texto livre`), preservado pelo compilador
    como comentário e legível por `grep` ou pelo LSP.
  - **Não implementado agora porque altera o formato de saída** — o `.syn` é
    consumido pelo compilador, pelo `synesis-lsp`, pelo `synesis-graph` e pelo
    pipeline ACT (`critique` → `normalize` → `incorporate`). A marcação precisa
    sobreviver ao round-trip dessas etapas sem virar ruído nem ser descartada,
    o que é decisão de design de formato, não detalhe de implementação.
  - Estudo: `Planning/Marcacao_Registros_Degradados.md`.

- **Instrumentação de prompt caching** (`token_usage.py`, `llm_client.py`) — o
  coder passa a ler e reportar as métricas de cache que os dois backends já
  devolviam e que eram silenciosamente descartadas. Sem elas não havia como
  saber se o `cache_control` (marcado em todos os prompts desde sempre) estava
  funcionando, nem medir o que ele economiza.
  - `TokenUsage` ganha `cache_write_tokens` e `cache_read_tokens`; os kwargs
    de `record()` têm default `0`, então todos os chamadores existentes seguem
    funcionando sem alteração.
  - Backend **Anthropic**: lê `cache_creation_input_tokens` /
    `cache_read_input_tokens`. O rate limiting proativo passa a contar o prompt
    inteiro (cache também consome cota), não apenas o resto não-cacheado.
  - Backend **OpenAI-compat**: lê `prompt_tokens_details.cached_tokens` /
    `.cache_write_tokens`. Prompt caching é **automático** na maioria desses
    provedores (OpenAI, DeepSeek, Grok, Moonshot, Z.AI) — não exige
    `cache_control` — e o OpenRouter sempre inclui usage accounting.
  - **Semântica divergente tratada:** na Anthropic `input_tokens` é apenas o
    resto não-cacheado (total = input + write + read); no OpenAI-compat
    `prompt_tokens` já é o total e `cached_tokens` é subconjunto. Somar
    ingenuamente causaria dupla contagem no caminho OpenAI. Nova propriedade
    `total_prompt_tokens` trata a diferença via o flag `input_excludes_cache`;
    `total_tokens` passa a usá-la.
  - `summary_line()` ganha o segmento `cache w N/r N`, exibido **apenas** quando
    há atividade de cache — a linha permanece idêntica em provedores sem cache,
    preservando os testes e a saída existentes.
  - Helper `_int_attr()` coage atributos ausentes/não-inteiros para `0`
    (necessário porque `getattr` num `MagicMock` devolve outro `MagicMock`, não
    o default — os testes existentes usam mocks assim).
  - **Testes**: 11 novos casos (`tests/test_token_usage.py`) cobrindo default-zero,
    acumulação, as duas semânticas de total, exibição condicional, e captura real
    nos dois branches do client. Cada um foi verificado falhando contra a versão
    anterior. Suíte: 435 → 446 passed.
  - Diagnóstico habilitado: `cache_write > 0` com `cache_read == 0` indica cache
    escrito e nunca reusado; `cache_read == 0` em execuções repetidas indica
    invalidador silencioso no prefixo. Ver
    `Planning/Estudo_Fix_Perde_System_Prompt.md` §8.

### Licença — sem licença → AGPL-3.0-only + Synesis Data-Output Exception

- Migração aplicada em 2026-08-02, junto com o restante do ecossistema.
  Estudo completo: `synesis-planning/synesis/new_licence_policy.md`.
  - `synesis-coder` **não tinha `LICENSE` nem campo `license` algum** ("todos
    os direitos reservados" de facto) — é o único pacote sem histórico
    prévio no PyPI, por isso estreia direto em AGPL, sem MIT anterior a
    conciliar.
  - Novo `LICENSE` (AGPL-3.0 integral, obtido de gnu.org) + `LICENSE.exception`
    (idêntico ao do core).
  - `pyproject.toml`: `license = "AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception"`
    + `license-files = ["LICENSE", "LICENSE.exception"]`; `setuptools>=77`.
  - `README.md`: seção "License" (antes só "MIT") substituída pelo bloco de
    aviso que ativa a exceção — sem esse aviso a exceção não se aplica a
    nenhum arquivo.
  - `CITATION.cff`: `license: AGPL-3.0-only`, exceção referenciada no
    `abstract` (o schema 1.2.0 rejeita `LicenseRef-` no campo `license`).

### Added

- **Novo subcomando `dataset`** — processa um corpus TOML declarado por
  `INCLUDE DATASET` no `.synp`, gerando SOURCE + ITEMs por registro (espelha
  `abstract`, que itera um corpus `.bib`). Requer `synesis >= 0.10.0`.
  - `synesis-coder dataset --project lattes.synp --output-dir annotations/`
    processa o corpus inteiro (o caminho vem do `INCLUDE DATASET` do `.synp`);
    `--dataset <glob>` sobrescreve pontualmente sem editar o projeto.
  - `modes/dataset_mode.py` (novo): `parse_dataset_records` +
    `_serialize_record` (contexto por `CONTEXT FROM DATASET`, com pré-filtro —
    determinístico, testável sem LLM) + `process_dataset` (reusa
    `_generate_abstract_syn` para a geração SOURCE+ITEMs).
  - `project_loader`: `_dataset_key_path` descobre a chave de indexação do
    campo `IDENTIFIES` + `ON DATASET` do template (agnóstico de domínio — não
    presume schema de currículo); `ctx["dataset_index"]` populado e propagado a
    `synesis.load()`.
- Menu de comandos e `_EPILOG_DATASET` com exemplos de uso.

### Added (correção pré-commit)

- **Feedback de progresso no `dataset` (`dataset_mode.py`)** — o comando
  processava o corpus inteiro em silêncio (nenhuma linha entre o início e o
  resumo final), dando a impressão de travamento em corpora grandes ou com
  `--concurrent` baixo. Três pontos de `logger.info` adicionados (nível padrão
  do CLI já é INFO — sem flag nova):
  - início do lote: `Iniciando geração (concurrent=N)`;
  - por registro concluído, na ordem real de conclusão (não de submissão —
    `asyncio.gather` não preserva ordem): `[i/N] <bibref> — OK` /
    `ERRO: <motivo>` / `FALHA NA VALIDAÇÃO`;
  - resumo final ganhou tempo decorrido: `Processados N registro(s): X OK, Y
    com falha (Zs)`.
  - Granularidade deliberadamente por registro, não por chamada LLM interna
    (um registro pode disparar até 4 chamadas via `validate_and_fix_async`) —
    evita ruído sem esconder progresso real.

### Fixed

- **Prompt e schema pediam ao LLM campos com origem-de-valor externa**
  (`ON BIBLIOGRAPHY`/`ON DATASET`) — o modelo era instruído a gerar valores que
  o compilador já resolve da fonte externa, desperdiçando tokens e, em pelo
  menos um caso real, induzindo um valor fabricado (`false`) para um campo TOML
  vazio. `_build_source_fields_section` (prompt_builder) e
  `_scope_object_schema` (schema_builder) agora excluem esses campos.
- **`block_assembler._assemble_block` não pulava campos de origem externa** —
  gravava `NA` para um campo `REQUIRED` ausente no JSON do LLM mesmo quando o
  campo é `ON BIBLIOGRAPHY`/`ON DATASET` (cujo valor nunca deveria vir do LLM).
  Agora esses campos são pulados incondicionalmente na montagem do bloco.
- **Validador do coder (`validator.py`) não repassava o dataset ao compilador**
  — as 4 chamadas a `synesis.load()` agora passam `dataset_index=
  ctx.get("dataset_index")`, sem o que a validação de projetos com dataset
  falhava por não enxergar os valores resolvidos.
- **Rede de segurança contra alucinação no caminho texto-livre** —
  `_strip_external_fields` (dataset_mode) remove linhas de campo externo que o
  fallback texto-livre eventualmente escreva, aplicada antes e depois do loop
  de validação/correção.

### Changed

- **`CONTEXT FROM DATASET` mudou de ancoragem no `synesis` 0.10.0** (de cláusula
  do bloco `SOURCE/ITEM FIELDS` para propriedade do bloco `FIELD`). **Nenhuma
  alteração foi necessária neste pacote:** `_declared_context_sections`
  (dataset_mode) lê `spec.context_from_dataset` — o atributo do `FieldSpec` —,
  não a estrutura do bloco do template. Registrado aqui porque o requisito
  `synesis >= 0.10.0` agora implica a sintaxe nova: um `.synt` com a forma
  antiga passa a ser erro de sintaxe.

### Fixed (correção pré-commit)

- **`INCLUDE ANNOTATIONS`/`ONTOLOGY` com padrão glob era descartado em
  silêncio** (`project_loader._collect_includes`) — a resolução testava cada
  literal do `.synp` com `Path.is_file()`, que é sempre `False` para um
  padrão como `"annotations/*.syn"`; o `continue` subsequente descartava o
  include sem warning. `code_index`/`ontology_index` ficavam vazios mesmo com
  anotações reais no disco, e como `code_index` alimenta o prompt de extração
  ("conceitos existentes"), a própria geração rodava sem o vocabulário
  acumulado do corpus. Corrigido delegando a `synesis.parser.paths.has_glob`/
  `resolve_glob` (mesmos utilitários do compilador principal, com a mesma
  contenção de diretório — `../*.syn` não escapa do projeto).
- **`INCLUDE SHARED ONTOLOGY` nunca era reconhecido** (mesma função) — a
  regex de includes não cobria a palavra `SHARED`, então uma ontologia
  compartilhada fora do diretório do projeto (`INCLUDE SHARED ONTOLOGY
  "../ontologia.syno"`) nunca era lida. Regex ajustada para aceitar o
  qualificador opcional e repassar `shared=True` a
  `synesis.parser.paths.resolve_include` (que autoriza o alvo externo).
  **Combinado com o bug do glob acima**, o efeito em `ontology --update` era
  duplo: nem os códigos pendentes nem a ontologia já definida eram vistos.
- **`synesis-coder ontology --update` apagava entradas curadas do `.syno`**
  (`ontology_mode.process_ontology`) — `_get_pending_codes` exclui de
  propósito os códigos já definidos (é o próprio objetivo do `--update`), mas
  a escrita final gravava só as entradas recém-geradas com `overwrite=True`,
  descartando todo o conteúdo pulado. Observado em caso real: uma ontologia
  compartilhada com 74 entradas curadas virou 59 após um `--update`. Corrigido
  lendo o `.syno` existente antes de escrever e anexando as novas entradas ao
  final (separadas por um cabeçalho de seção), em vez de substituir o arquivo.
- **Schema JSON rejeitado (HTTP 400) por provedores `strict` (OpenAI/Azure via
  OpenRouter)** (`schema_builder._scope_object_schema`) — o schema declarava
  `required` só com os campos `REQUIRED` do template, mas `llm_client` envia
  `strict: True` fixo para o backend `openai`-compatível; a spec de
  *structured outputs* da OpenAI exige que **todo** campo de `properties`
  conste em `required` sob `strict`, expressando opcionalidade por tipo
  nullable, não por ausência. O erro 400 (`'required' is required to be an
  array including every key in properties`) fazia o caminho JSON cair
  silenciosamente para texto livre — descartando por baixo dos panos as
  garantias que o schema existe para dar: `enum` de ENUMERATED/ORDERED,
  `minimum`/`maximum` de SCALE, `enum` de relação de CHAIN e
  `additionalProperties: false`. Corrigido: todo campo agora entra em
  `required`; os que o template declara `OPTIONAL` recebem tipo nullable
  (novo helper `_nullable`, que trata os três formatos de fragmento — `type`,
  `enum`, `const`). `block_assembler._has_value` já descarta `None`, então um
  campo opcional omitido pelo modelo continua ausente no `.syn` gerado.

### Testing (correção pré-commit)

- `tests/test_project_loader_includes.py` (novo, 9 testes) — cobre expansão
  de glob, contenção de diretório, `INCLUDE SHARED ONTOLOGY` com e sem alvo
  externo, e o cenário combinado (glob + shared + bibliography) do case study
  Quinto Andar. Cada teste foi verificado falhando contra a versão anterior à
  correção antes de ser aceito.
- `tests/test_schema_builder.py` — nova classe `TestStrictModeConformance` (6
  testes): `required == properties` para ITEM e SOURCE, campos `OPTIONAL`
  aceitam `null`, campos `REQUIRED` continuam não-nulos, `minimum`/`maximum`
  de SCALE sobrevivem à conversão nullable, e tratamento de `enum`/`const`
  pelo novo `_nullable`.
- `tests/test_ontology_mode.py` — nova classe `TestUpdatePreservesExistingEntries`:
  simula `--update` com LLM mockado e confirma que entradas preexistentes no
  `.syno` sobrevivem à escrita, com as novas anexadas.
- Suíte completa antes/depois das quatro correções: 420 → 435 passed, 0
  regressões (`_scope_object_schema` tem blast radius CRITICAL — 13 símbolos,
  20 fluxos de execução, todos os modos de extração — confirmado sem
  regressão pela suíte completa, não só pelos testes novos).
- Validação ponta a ponta no projeto real (Quinto Andar / Dados_Lattes):
  `synesis compile` foi de 9 erros + ~180 warnings para 0/0;
  `ontology --update` (antes falhava com "nenhum código encontrado") gerou
  59/59 códigos pendentes preservando as 74 entradas curadas (133 no total);
  `dataset` (3 currículos, backend `openai`/OpenRouter) não apresentou mais
  fallback para texto livre.

### Testing

- Codificação real de 3 currículos TOML do corpus Quinto Andar (backend
  Anthropic, claude-sonnet-5, pt-BR): 3/3 `.syn` gerados corretamente.
- `tests/test_dataset_mode.py` (offline — sem chamada de LLM/API; 7 testes ao
  final desta versão, ver detalhamento abaixo).
- Após a mudança de ancoragem: contexto serializado do `lattes.synt` verificado
  idêntico ao anterior (105176 / 71343 / 175203 chars nos 3 currículos); suíte
  offline relevante 97 passed.
- Feedback de progresso: smoke test com `_generate_abstract_syn`/
  `validate_and_fix_async` mockados (offline, sem chamada de LLM) exercitando
  `_process_dataset_async` ponta a ponta — confirma ordem `[i/N]` correta e
  ausência de duplicação com o log pré-existente de `parse_dataset_records`.
  `tests/test_dataset_mode.py` 7 passed (2 novos, override de dataset).

---

## [0.7.7] — 2026-07-06

### Fixed

- **Bibref rejeitado em projetos sem bibliografia (`INCLUDE BIBLIOGRAPHY` ausente)** (`project_loader.py`)
  - `assert_bibref_known()` tratava `bib_keys` vazio como erro de configuração incondicional ("verifique a diretiva INCLUDE BIBLIOGRAPHY"), mesmo quando o `.synp` legitimamente não declara bibliografia — caso do compilador `synesis` >= 0.6.0, que permite SOURCEs definidos exclusivamente pelo template (ex.: `lattes.synp`, cujo bibref é o ID Lattes, não uma chave `.bib`). Todo `document`/`item` contra esses projetos abortava antes de chamar o LLM.
  - A validação agora distingue os dois casos: se o `.synp` não declara `INCLUDE BIBLIOGRAPHY`, o bibref é aceito sem checagem contra `.bib` (o compilador já teria abortado o `load_project()` se o projeto fosse inválido); se a diretiva está presente mas o `.bib` carregou zero chaves, o erro é mantido, com mensagem revisada apontando o arquivo `.bib` como a causa (antes apontava a diretiva ausente, que não é o caso).
  - `synesis>=0.5.5` → `synesis>=0.6.0` em `pyproject.toml`, refletindo a dependência real desta capacidade.
  - Testes: `TestAssertBibrefKnown` em `tests/test_item_mode.py` cobre os dois ramos nas condições de `bib_keys` vazio.

---

## [0.7.6] — 2026-07-06

### Added

- **Caminho JSON (Opção 3) no modo `ontology`** (`schema_builder.py`, `block_assembler.py`, `prompt_builder.py`, `modes/ontology_mode.py`)
  - Até então o modo `ontology` só tinha o caminho de texto livre: o LLM escrevia o bloco `ONTOLOGY ... END ONTOLOGY` inteiro, incluindo a moldura estrutural (keyword, nomes de campo, `END`). Isso permitia alucinações de sintaxe — por exemplo, uma linha `ITEM <code> TYPE variable` dentro do bloco, que o compilador rejeita com "Token inesperado" e que corrompe o `.syno` inteiro no processamento subsequente.
  - `build_ontology_schema(ctx, topics=...)`: gera o JSON Schema dos campos ONTOLOGY do template (`additionalProperties: false` elimina por construção qualquer chave fora de `ONTOLOGY FIELDS`); quando há tópicos existentes no projeto, o campo TOPIC vira `enum` restrito a eles.
  - `assemble_ontology(ctx, code, data)`: monta o bloco `ONTOLOGY <code> ... END ONTOLOGY` a partir de valores JSON, reaproveitando `_assemble_block` (agora com o parâmetro `with_at`, default `True`, para preservar 100% o comportamento existente de SOURCE/ITEM que usam `@bibref`).
  - `build_ontology_values_prompt`: prompt análogo ao do modo `abstract`, pedindo apenas os VALORES dos campos — o LLM nunca digita `ONTOLOGY`, `END ONTOLOGY` ou nomes de campo.
  - `_generate_ontology_syno` (novo, em `ontology_mode.py`): espelha `_generate_abstract_syn` — usa o caminho JSON quando `supports_json_schema()` é `True`; cai para o texto livre existente em caso de falha ou SDK sem suporte (degradação graciosa idêntica à do modo `abstract`, ver [0.7.3]).
  - **Testado sem custo de API**: montagem determinística do bloco a partir de valores simulados, incluindo chaves alucinadas (`item`, `type`) que o schema/assembler descartam por construção; bloco resultante validado com `synesis.load()` real sem erros.

### Fixed

- **Blocos ONTOLOGY que falham a validação eram gravados no `.syno`, corrompendo-o** (`modes/ontology_mode.py`)
  - `_process_ontology_async` concatenava TODOS os resultados no `.syno` final, inclusive os que retornaram `success=False` (contendo o comentário `# ERRO: validação falhou após N tentativa(s)` seguido do texto malformado). Um único código mal-extraído corrompia o arquivo inteiro para qualquer `compile`/`load` posterior.
  - Blocos com `success=False` agora são desviados para `<output>.syno.rejeitados` (mesmo diretório, sobrescrito a cada execução) em vez de entrar no `.syno`; o resumo da execução já reportava a contagem de falhas via "Falhas: N", agora complementado por um aviso de log apontando o arquivo de rejeitados.

### Changed

- **Testes de integração (API real) não são mais executados por padrão** (`pyproject.toml`)
  - `pytest` sem flags rodava toda a suíte incluindo `@pytest.mark.integration` (`process_abstract`/`process_ontology` reais, consumindo tokens) sem aviso. Adicionado `addopts = "-m 'not integration'"`: agora esses testes só rodam com `pytest -m integration` explícito (e `ANTHROPIC_API_KEY` configurada). Suíte padrão: 423 testes coletados, 3 deselected.

---

## [0.7.5] — 2026-07-06

### Fixed

- **`load_project` falhava com `PermissionError` ao encontrar um diretório no lugar de um `.syn`/`.syno`/`.bib` referenciado por `INCLUDE`** (`project_loader.py`)
  - `_collect_includes` checava apenas `file_path.exists()` antes de `read_text()`; um diretório homônimo (por exemplo, criado por engano ao confundir `--output` do comando `abstract` com nome de arquivo — ver [0.7.4]) também satisfaz `exists()`, levando a `read_text()` tentar abrir um diretório como arquivo. No Windows isso aparece como `PermissionError: [Errno 13] Permission denied` em vez de um erro claro.
  - Troca para `file_path.is_file()`, que exclui diretórios: um `INCLUDE` cujo caminho aponta para uma pasta agora é silenciosamente ignorado (mesmo comportamento já existente para arquivo ausente), sem quebrar `load_project`.

---

## [0.7.4] — 2026-07-06

### Fixed

- **`abstract --output` confundido com nome de arquivo em vez de diretório** (`cli.py`, `modes/abstract_mode.py`)
  - O comando `abstract` sempre tratou `--output` como diretório de saída (escreve `annotations.syn` ou `<bibref>.syn` dentro dele), mas o nome da flag e os exemplos do `--help` não deixavam essa distinção clara. Um usuário passando `--output abstracts.syn` (esperando um arquivo) fazia o comando criar uma **pasta** com esse nome; uma tentativa subsequente de abrir o mesmo caminho como arquivo falhava com `PermissionError: [Errno 13] Permission denied`, sem indicar a causa real.
  - Flag renomeada para `--output-dir`, alinhando com o comando `normalize` que já usa esse nome para o mesmo conceito; `--output` mantido como alias retrocompatível. Help text agora diz explicitamente "a folder, not a file path". Exemplos do epílogo atualizados.
  - `process_abstract` passa a validar o caminho antes de criar o diretório: se já existir como arquivo, falha cedo com `ValueError` explicando o conflito e sugerindo `--output-dir annotations`, em vez de deixar o erro estourar depois como `PermissionError` genérico.

---

## [0.7.3] — 2026-07-06

### Added

- **Caminho JSON (Opção 3) no backend Anthropic via structured outputs nativo** (`llm_client.py`, `runtime_info.py`, `pyproject.toml`, `.env.example`)
  - Até então o "caminho JSON" (LLM devolve só VALORES conforme JSON Schema → `block_assembler` monta o bloco) existia apenas no backend `openai`-compatível; o backend `anthropic` sempre caía no caminho de texto-livre (regex). A API Anthropic lançou **structured outputs** (`output_config.format`), o equivalente nativo do `response_format` da OpenAI com constrained decoding.
  - `supports_json_schema()` passa a retornar `True` para o backend `anthropic` **quando o SDK instalado suporta `output_config`** (via `_anthropic_sdk_supports_output_config()`, introspecção cacheada da assinatura). Em SDK anterior, retorna `False` → texto-livre → **comportamento idêntico ao anterior** (degradação graciosa).
  - Novo `_sanitize_schema_for_anthropic()`: remove do schema enviado ao wire os keywords que o constrained decoding da Anthropic não aceita (`minimum`/`maximum`/`multipleOf`/`minLength`/`maxLength`/`pattern`/`exclusive*`). A garantia desses limites permanece no `validate_and_fix` (compilador Synesis), que roda sempre depois — nada é enfraquecido. O schema original (usado pelo backend OpenAI, que aceita esses keywords) é preservado intacto.
  - Ramo `anthropic` de `_call_sync_inner`: quando `schema` é fornecido, monta `output_config={"format":{"type":"json_schema","schema": <saneado>}}`. Falha/refusal/truncamento → `call_json` cai no texto-livre (piso de segurança preservado em todos os cenários).
  - **Sem mudança nos modos nem no assembler**: os 4 modos que usam o caminho JSON (`item`, `abstract`, `document`, `refine`) e o `block_assembler` são agnósticos ao backend — o contrato de dados (`dict` → assembler) é idêntico. A alteração se concentra em `llm_client.py`.
  - **Floor do SDK**: `anthropic>=0.40.0` → `anthropic>=0.77.1` (versão que introduziu structured outputs; 0.77.1 corrigiu o beta header).
  - `runtime_info`: banner mostra "JSON assembler" para anthropic com structured outputs; a dica de fallback passou a orientar a atualização do SDK (`anthropic>=0.77.1`) em vez de trocar de backend.
  - **Testes**: novos casos para `_sanitize_schema_for_anthropic` (remoção recursiva, preservação de `enum`/`const`/`additionalProperties`, não-mutação do original), `supports_json_schema()` por backend (gate no SDK), e construção do `output_config` no ramo anthropic (mock da API). Corrigidos 3 testes pré-existentes de `test_runtime_info.py` que asseriam strings que a implementação não emitia.

---

## [0.7.2] — 2026-07-06

### Fixed

- **Chamada de crítica redundante no loop do modo `refine`** (`modes/refine_mode.py`)
  - Cada iteração de `_refine_single_item` re-executava `_critique_tags` sobre o bloco `current` no início do loop, recomputando um score já obtido (o `initial_score` na 1ª iteração, ou o `cand_score` da iteração anterior nas seguintes) — uma chamada de LLM inteira desperdiçada por iteração.
  - Agora as tags do critique mais recente (`current_tags`) são reaproveitadas como feedback de entrada da iteração seguinte. Reduz de até 7 para até 5 chamadas de LLM por ITEM com `max_iter=2`, sem alterar o resultado do loop (mesma lógica de não-regressão, ponto-fixo e trace).
  - Testes de `test_refine_mode.py` ajustados: mocks de `critique_scores` que assumiam a chamada duplicada foram corrigidos para a sequência real de chamadas.

---

## [0.7.1] — 2026-07-05

### Added

- **Conexão de crítica (2ª API) para `critique` e `refine`** (`llm_client.py`, `modes/critique_mode.py`, `modes/refine_mode.py`, `.env.example`)
  - `LLMClient` ganha os parâmetros opcionais `api_url` e `api_key` (além do já existente `backend`), com fallback ao ambiente — permite instanciar clients com conexões explícitas, mantendo comportamento idêntico quando omitidos (retrocompatível bit-a-bit).
  - `get_critique_connection()` resolve a família `SYNESIS_CODER_CRITIQUE_{BACKEND,API_URL,API_KEY}`; cada eixo omitido herda a conexão primária. A fase `critique` e o **crítico** do `refine` passam a usar essa conexão; o **gerador** do refine e as demais fases seguem na conexão primária.
  - Habilita independência epistêmica real (ex.: gerador no OpenRouter + crítico na Anthropic nativa) sem afetar as outras fases. Ver Planning/Estudo_API_por_Fase.md.
  - `_validate_phase_env` **não** foi alterado (contenção de blast-radius); a conexão de crítica é resolvida na instanciação do client.

### Changed

- **`.env` simplificado** — seção "ATIVO AGORA" consolida conexão primária, conexão de crítica opcional, modelos/thresholds por fase e parâmetros globais; seção "Pipeline ACT" separada removida. Sem mudança nas variáveis efetivamente lidas.

---

## [0.7.0] — 2026-07-05

### Added

- **Modo `refine` — re-extração com feedback (Fase R do pipeline ACT)** (`modes/refine_mode.py` *(novo)*, `prompt_builder.py`, `modes/critique_mode.py`, `cli.py`, `.env.example`)
  - Loop opt-in Self-Refine/Reflexion: para cada ITEM suspeito, o crítico aponta o erro e o **gerador** reescreve a anotação raciocinando de novo sobre o texto-fonte — em vez de aplicar mecanicamente o palpite do crítico (como o `incorporate`).
  - **Cláusulas de segurança** embutidas no loop: não-regressão estrita (só aceita versão com `suspicion_score` menor), `MAX_ITER` rígido, detecção de ponto-fixo/oscilação (histórico normalizado), validação estrutural obrigatória via `validate_and_fix_async`, e crítico ≠ gerador (clients/modelos distintos) contra viés de auto-validação.
  - **Rastreabilidade**: o `.syn` final traz cabeçalho `# $metrics.refine.*` com métricas agregadas (com fórmulas) e o trace de score por iteração por ITEM (`# $refine.@bibref.trace: 0.62 -> 0.18`).
  - **Aditivo**: reaproveita critique, validação, obtenção de source-text e assembler como biblioteca. Extraído `_critique_tags`/`_score_of` de `critique_mode` (sem mudança de comportamento do modo `critique`).
  - **CLI**: subcomando `synesis-coder refine` com `--max-iter`, `--threshold`, `--critique-model`, `--refine-model`, `--thinking-budget`, `--overwrite`/`--backup`, e guarda de I/O que impede sobrescrever a fonte por acidente.
  - **Config**: `SYNESIS_CODER_REFINE_MODEL` (gerador) e `SYNESIS_CODER_REFINE_MAX_ITER`; crítico reusa `SYNESIS_CODER_CRITIQUE_MODEL`; limiar reusa `SYNESIS_CODER_SUSPICION_THRESHOLD`.
  - **Novos prompts** puros: `build_item_refinement_prompt` (texto-livre) e `build_item_refinement_values_prompt` (caminho JSON), reusando o system prompt de valores para preservar o `cache` das GUIDELINES.

---

## [0.6.2] — 2026-06-16

### Added

- **Flags `--overwrite` e `--backup` em todos os modos geradores** (`cli.py`, `synr_io.py` *(novo: `safe_write_output`)*, `modes/document_mode.py`, `modes/ontology_mode.py`, `modes/incorporate_mode.py`, `modes/finetune_mode.py`)
  - Antes, qualquer modo sobrescrevia o arquivo de saída silenciosamente.
  - `--overwrite`: sobrescreve sem confirmação (útil em scripts/CI).
  - `--backup`: cria cópia `.bak` do arquivo existente antes de gravar.
  - Sem `--overwrite`: em TTY pergunta ao usuário; em não-TTY (CI/pipe) aborta com mensagem clara.
  - `safe_write_output(output_path, content, overwrite, backup)` em `synr_io.py`: escrita atômica via `tempfile.mkstemp` + `os.replace()` — arquivo nunca fica truncado em caso de Ctrl-C ou crash. Centraliza R2+R3+R4 para todos os modos.
  - Modo `ontology`: `--update` continua implicando sobrescrita intencional (`overwrite=update or overwrite`).

- **Tolerância a erros de anotação pré-existente no modo `document`** (`project_loader.py`, `modes/document_mode.py`)
  - Resolução do "deadlock de regeneração": o arquivo `.syn` de saída é referenciado em `INCLUDE ANNOTATIONS` do `.synp`, portanto `load_project` o compilava antes que a extração pudesse começar. Se o `.syn` usava um template antigo, o compilador abortava antes de qualquer chamada LLM — impossível regenerar o arquivo que o comando deveria substituir.
  - `load_project(tolerate_annotation_errors=True)`: erros cujo `location.file` aponta para um arquivo `.syn` são emitidos como warnings em vez de abortar. Erros de template, `.synp` ou bib continuam sendo fatais.
  - `_split_and_tolerate_errors()`: classifica cada erro por origem, agrega os tolerados com `Counter` e emite um `[WARN]` com bullet por categoria (`Nx mensagem`). Erros fatais reconstroem um `ValidationResult` parcial e abortam normalmente.

### Changed

- **Apresentação de console completamente reformulada** (`cli.py`, `runtime_info.py`, `modes/document_mode.py`, `project_loader.py`, `synr_io.py`)
  - Cabeçalho do produto impresso uma vez por invocação em stderr antes de qualquer log:
    ```
    SYNESIS CODER (v0.6.2) | Core (v0.5.7)
    Extraction engine for generating valid annotations in the Synesis ecosystem.
    The template defines all fields, relations, and constraints — nothing is hardcoded.
    ```
  - Rótulos de log padronizados: `[INFO]`, `[WARN]` (era `[WARNING]`), `[ERROR]`, `[OK]` *(novo — nível 21)*, `[DEST]` *(novo — nível 22)*.
  - `_BracketFormatter` em `_configure_logging`: formata `[LABEL] mensagem` sem nome do módulo; suprime `WARNING` em favor de `WARN`.
  - `[INFO] Motor: backend/model | JSON assembler` — banner de LLM compacto (era uma linha longa com versões e dica).
  - `[INFO] Origem: arquivo.md (94k chars, 12 chunks, −1% após limpeza)` — novo label de progresso.
  - `[WARN] Ignorando anotações anteriores (N erros):\n       - Nx mensagem` — erros tolerados agregados por tipo com bullet indentado.
  - `[INFO] Processando: [████████████] 12/12 chunks (0 falhas)` — barra de progresso preenchida in-place (era grade `[1][2][3]...`). Suprimida em não-TTY e com `-v`.
  - `[PROMPT] arquivo.syn já existe. Sobrescrever? [y/N]:` — prompt de confirmação com label explícito via stderr.
  - `[OK] Validação concluída. N itens únicos extraídos (de M totais) em Xs.` — resultado final.
  - `[DEST] D:\caminho\para\lattes.syn` — destino do arquivo gravado.
  - Modo `plain`: stdout silencioso (return `""`). Modo `verbose`: retorna header com metadados.

### Added (banner)

- **Banner de runtime no início de cada execução com LLM** (`runtime_info.py` *(novo)*, todos os modos com LLM)
  - `runtime_banner(llm_client, format)` emite uma linha única e legível por
    pesquisador não-técnico informando: versão do `synesis-coder`, versão do
    compilador `synesis`, backend/modelo LLM em uso e — crucialmente — se o
    caminho ativo é **JSON assembler (determinístico)** ou **texto-livre (regex)**.
    O caminho determinístico (Opção 3) só ativa quando o backend suporta
    `response_format json_schema` (`supports_json_schema()` → backend `openai`);
    no backend padrão `anthropic` o coder cai em texto-livre, antes sem nenhum
    sinal visível. Quando aplicável, a linha sugere `SYNESIS_CODER_BACKEND=openai`.
  - Emitido via `logger.info` (stderr na CLI), nunca em stdout — preserva o
    `.syn` cru do formato `plain` e respeita `-q`/`-qq`.
  - Chamado em `item`, `document`, `abstract`, `suggest`, `ontology`, `critique`
    e `finetune`. `incorporate` é determinístico (sem LLM) e não emite banner.
  - `tests/test_runtime_info.py` *(novo)*: 6 casos (4 infos presentes, rótulo de
    caminho conforme `supports_json_schema()`, dica só no anthropic, emissão via logger).

### Changed

- **Logging centralizado na CLI agora cobre TODOS os modos** (`modes/critique_mode.py`, `modes/finetune_mode.py`, `modes/normalize_mode.py`, `modes/ontology_mode.py`)
  - A migração anterior (ver abaixo) removeu `basicConfig(force=True)` apenas de
    `document` e `abstract`. Os modos `critique`, `finetune`, `normalize` e
    `ontology` ainda o chamavam com `level=INFO` fixo e `force=True`, **derrubando**
    a configuração da CLI: `-v`/`-q` eram ignorados e o ruído de `httpx`/`openai`/
    `anthropic` voltava. Agora todos delegam o logging a `_configure_logging`.
  - `critique --debug` preserva a elevação para `DEBUG` via
    `logging.getLogger().setLevel(DEBUG)`, sem reinstalar handlers.
  - `finetune` perdeu também o loop redundante de silenciamento de loggers de
    terceiros (a CLI já o faz, e o loop ignorava `-v`).
  - `tests/test_logging_centralized.py` *(novo)*: teste-guarda que falha se
    qualquer modo reintroduzir `basicConfig`.

- **Saída de console minimalista nos modos `document` e `abstract`** (`modes/document_mode.py`, `modes/abstract_mode.py`, `cli.py`)
  - Logs de bibliotecas de terceiros silenciados na saída padrão — `httpx`/`httpcore`/`openai`/`anthropic`/`urllib3` ficam em `WARNING` (eliminam a enxurrada de `HTTP Request: POST ...`, uma por chamada LLM). Voltam com `-v`.
  - Logs de progresso individual (chunk por chunk, referência por referência, batch headers, "Escrito:", cooldown) rebaixados de `INFO` para `DEBUG` — só aparecem com `-v`.
  - Modo `document`: barra de progresso compacta `[1][2][3]…` que se preenche conforme os chunks concluem (chunks com falha marcados com `✗`). Renderizada in-place só em TTY; suprimida em pipes/redireções e quando `-v` está ativo.
  - Mensagem de inicialização consolidada (`Inicializando… arquivo.md (94k chars, 12 chunks, −1% após limpeza)`) e linha `Concluído! N itens extraídos → M únicos em Xs`.
  - Sumário final reformatado com separadores e alinhamento de colunas.
  - `basicConfig(force=True)` removido dos modos — configuração de logging centralizada na CLI. Com `-v`, o formato inclui timestamp e nome do módulo; sem `-v`, apenas `[LEVEL] mensagem`.

- **Mensagens de diagnóstico compactas para o usuário pesquisador** (`project_loader.py`, `modes/document_mode.py`)
  - Erros de compilação exibidos ao usuário agora usam `get_diagnostics(verbose=False)`: uma linha por erro, avisos `UndefinedCode` agrupados por código com contagem de ocorrências e dica `synesis-coder ontology`.
  - O caminho do LLM (auto-correção em `validator.py`) continua usando `verbose=True` (mensagens pedagógicas completas), preservando a qualidade da auto-correção.
  - Caso típico: saída reduzida de ~500 linhas para ~10 linhas.

- **Bloco SOURCE do modo `document` migrado para JSON + assembler** (`modes/document_mode.py`, `prompt_builder.py`)
  - `_generate_source_block` agora usa o mesmo caminho determinístico já adotado para ITEMs e pelo modo `abstract`: `build_source_schema` → `call_json_async` → `assemble_source`. A moldura é montada em Python (indentação canônica, separadores, `NA` por construção), eliminando de raiz os erros de extração por regex, indentação inconsistente e campo REQUIRED ausente no SOURCE.
  - Novo `build_document_source_values_prompt` e `_build_values_system_prompt(scope="source")` no `prompt_builder`.
  - Caminho de texto livre preservado como fallback (extração por regex agora tolerante a indentação, texto explicativo antes/depois e whitespace em `END SOURCE`; com dedent automático via `_dedent_block`).

### Fixed

- **Extração frágil do bloco SOURCE no fallback de texto livre** (`modes/document_mode.py`)
  - A regex exigia `SOURCE`/`END SOURCE` no início absoluto da linha; quando o LLM indentava o bloco ou o precedia de explicação, a extração falhava e caía para o SOURCE mínimo. Agora tolera indentação (com dedent), caixa, whitespace variável em `END SOURCE` e texto ao redor. O warning de fallback passou a registrar os primeiros 200 chars da resposta para depuração.

- **Indentação inconsistente no SOURCE corrigido pelo modo `document`** (`modes/document_mode.py`)
  - `_patch_required_source_fields` agora detecta a indentação dos campos já presentes no bloco (via `_detect_block_indent`) e a replica ao inserir `campo: NA`. Antes inseria 4 espaços fixos; quando o LLM usava 2 espaços, o Indenter da gramática aninhava o campo (`_INDENT` extra) e ele sumia do SOURCE, reaparecendo como `Campo obrigatorio ausente`.

- **Campos REQUIRED ausentes no SOURCE gerado pelo modo `document`** (`modes/document_mode.py`)
  - `_generate_source_block` agora chama `_patch_required_source_fields` tanto no caminho do LLM quanto no fallback mínimo: campos REQUIRED omitidos pelo LLM recebem `campo: NA`, evitando erro de compilação `Campo obrigatorio ausente no bloco SOURCE`.
  - Espelha o comportamento já existente no `block_assembler` para blocos ITEM.

- **Normalização de case em códigos gerados pelo LLM** (`block_assembler.py`)
  - `_render_code` agora converte tokens CODE para lowercase antes de emitir, alinhando com `normalize_code()` do compilador Synesis.
  - `_normalize_concept` (CHAIN) também aplica lowercase, evitando que variantes como `Graduacao_Curso` e `graduacao_curso` apareçam como dois códigos distintos no relatório e no `code_index`.
  - 2 novos testes em `test_block_assembler.py` cobrem os casos de normalização.

---

## [0.6.1] — 2026-06-15


### Added

- **Caminho JSON + assembler no modo `abstract`** — os três modos de anotação
  (`item`, `document`, `abstract`) usam agora o caminho JSON por padrão.
  - `schema_builder.py`: `build_abstract_schema(ctx)` — envelope combinado
    `{"source": {...}, "items": [...]}` gerado a partir dos campos SOURCE e ITEM
    do template. `additionalProperties: false` em todos os níveis.
  - `prompt_builder.py`: `build_abstract_values_prompt(ctx, bibref, abstract)` /
    `_build_abstract_values_system_prompt(ctx)` — contrato JSON com `"source"` e
    `"items"` como chaves obrigatórias; reutiliza seções de GUIDELINES e índices;
    omite seção de formato de bloco.
  - `abstract_mode.py`: `_generate_abstract_syn(ctx, bibref, abstract, llm_client,
    context)` — caminho JSON com `call_json_async → assemble_source + assemble_items`;
    fallback automático para `build_abstract_prompt + call_async`.
  - `tests/test_abstract_mode.py`: `TestAbstractSchema` (4 casos), `TestAbstractValuesPrompt`
    (5 casos), `TestAssembleAbstractFromData` (2 casos, incluindo compilação real).

---

## [0.6.0] — 2026-06-15

### Added

- **Pré-validação de bibref com abort precoce** — elimina o erro dominante E001
  (bibref inexistente no `.bib`) antes de gastar qualquer chamada LLM.
  - `ctx["bib_keys"]` em `project_loader.py`: lista ordenada das chaves do `.bib`
    parseado pelo compilador, sem reparse adicional.
  - `assert_bibref_known(ctx, bibref)` *(novo)*: valida que o bibref (com ou sem `@`)
    existe em `bib_keys`; levanta `ValueError` com amostra das chaves disponíveis e,
    quando o `.synp` traz `DESCRIPTION`, cita a convenção do projeto.
  - `item_mode.py` e `document_mode.py`: guard chamado logo após `load_project`, antes
    de qualquer chamada LLM.
  - `tests/test_item_mode.py`: `TestAssertBibrefKnown` (4 casos) + `test_bib_keys_populated`.

- **Opção 3 — geração via JSON + assembler determinístico** — o LLM devolve apenas
  valores em JSON; o Python monta a moldura estrutural inteira (palavras-chave, nomes de
  campo, indentação, `@{bibref}`, setas `->` de chains). Elimina por construção E022
  (campo desconhecido), E033/E015 (separador de CODE), E008/E010/E011 (sintaxe de chain)
  e fences Markdown. Ativo por padrão no backend openai-compat, com fallback automático.
  - `schema_builder.py` *(novo)*: `FieldSpec` → JSON Schema. CODE→array, ENUMERATED/
    ORDERED→`enum`, SCALE→integer com min/max de `[lo..hi]`, CHAIN→array de hops
    `{source, relation, target}` com `relation` por enum. `additionalProperties:false`.
  - `block_assembler.py` *(novo)*: dict de valores → texto Synesis. CODE→`", "`,
    CHAIN hops→`A -> rel -> B` com interleave de hops contíguos e snake_case;
    campos REQUIRED ausentes → `NA`; OPTIONAL ausentes omitidos; chaves extras ignoradas.
  - `llm_client.py`: `call_json` / `call_json_async` (retornam `None` para acionar
    fallback); `supports_json_schema()`; `response_format: json_schema` em `create_kwargs`;
    `_parse_json_response()` tolera fences de markdown.
  - `prompt_builder.py`: `build_item_values_prompt` / `build_document_values_prompt` —
    prompts sem seção de formato de bloco. Fallback de CODE menciona vírgula.
  - `item_mode.py` / `document_mode.py`: caminho `call_json → assembler → validate_and_fix`,
    com fallback para texto livre.
  - Testes: `test_schema_builder.py`, `test_block_assembler.py`, `test_call_json.py` *(novos)*.

- **NA fallback para campos REQUIRED ausentes** — quando o LLM omite ou deixa vazio um
  campo obrigatório, o assembler emite `campo: NA` em vez de omiti-lo, garantindo
  conformidade estrutural sem retry adicional.
  - `block_assembler.py`: `_assemble_block` recebe `required: set`; `assemble_items` /
    `assemble_source` passam `set(ctx["required_item"])` / `set(ctx["required_source"])`.
  - `tests/test_block_assembler.py`: 3 novos casos (`test_required_absent_field_gets_na`,
    `test_required_empty_string_gets_na`, `test_optional_absent_never_gets_na`).

- **Filtragem de ruído pré-chunking** (`text_cleaner.py`) — saneamento de documentos
  longos antes do envio ao LLM. Quatro camadas em ordem:
  1. Seções ATX com marcador de ausência (`Não informado.`, `Nenhum item cadastrado.`,
     `N/A.`) — cabeçalho + marcador removidos.
  2. Boilerplate Lattes/CNPq: rodapé de geração, endereço de CV, data de atualização.
  3. Paginação (`Página X de Y`) e separadores visuais (`----`, `____`).
  4. Espaços/tabs múltiplos → espaço único; `\n{3+}` → `\n\n`.
  - `text_cleaner.py` *(novo)*: `clean_document(text) → str` (stateless, idempotente).
  - `document_mode.py`: `clean_document` chamado após `read_document`, antes de
    `split_into_chunks`; log reporta redução percentual; `input_chars` no debug recorder
    reflete o tamanho pós-limpeza.
  - `tests/test_text_cleaner.py` *(novo)*: 18 testes (cada camada, idempotência,
    preservação de conteúdo real).

### Notes

- O modo `abstract` permanece no caminho de texto livre (envelope JSON combinado
  SOURCE+ITEM fica como follow-up). Os modos `item` e `document` usam o caminho JSON.
- Reinjeção/revalidação do SOURCE no loop de correção por chunk (E020/E022): fora de
  escopo, registrado para follow-up.
- Impacto do `text_cleaner` depende da origem do documento. O conversor Lattes utilizado
  neste projeto já entrega Markdown limpo (~1% de redução medida em documento real de
  95 k chars). Efeito maior em exportações HTML→Markdown genéricas.

---

## [0.5.0] — 2026-06-14

### Added

- **Flag `--debug` no modo `document`** — gera um relatório Markdown de auditoria
  do pipeline LLM ao lado do `.syn` de saída (`<projeto>_<bibref>_debug.md`),
  legível por pesquisadores não-técnicos.
  - **`synesis_coder/debug_log.py`** *(novo)*:
    - `DebugRecorder` — acumulador thread-safe de eventos (chamadas LLM, ciclos
      de validação, correções). Contexto por chunk propagado via `threading.local`
      (mesmo padrão de `_correction_local`), pois as chamadas LLM rodam em threads
      worker via `asyncio.to_thread`. Quando ausente (sem `--debug`), overhead zero.
    - `render_markdown()` / `write(path)` — relatório cronológico: cabeçalho da
      sessão → bloco SOURCE → por trecho (system prompt com GUIDELINES, user
      message, resposta bruta, latência, tokens) → verificação do compilador
      tentativa a tentativa (✅/🔴) com correções → resumo final.
    - `classify_error(err)` → `"structural" | "value" | "other"` (mesma taxonomia
      da Opção 0 do `Estudo_Reducao_Tokens`).
    - `translate_diagnostics(result)` — converte erros do compilador em frases
      amigáveis reaproveitando `to_cli_line()`, com o código técnico
      (`SYNESIS_E0xx`) anexado como nota secundária. Ignora `OrphanItem`.
  - **`synesis_coder/llm_client.py`**: `LLMClient.__init__` aceita `recorder=None`.
    `_call_sync_inner` mede latência e emite evento bruto (system/user/resposta/
    tokens/params resolvidos) apenas quando há recorder. `call_async`/`fix_async`
    aceitam `context` e o setam dentro da thread worker.
  - **`synesis_coder/validator.py`**: `validate_and_fix_async` aceita
    `recorder`/`context` e registra cada tentativa de validação e correção.
  - **`synesis_coder/modes/document_mode.py`**: cria o recorder quando `debug=True`,
    propaga contexto por chunk, registra header/footer e grava o relatório.
  - **`synesis_coder/cli.py`**: opção `--debug` no comando `document`.

- **Flag `--debug` no modo `abstract`** — gera `<projeto>_abstract_debug.md` no
  diretório de saída, reaproveitando o `DebugRecorder`. O recorder foi
  generalizado para unidades arbitrárias de processamento: `DebugRecorder(
  unit_type, unit_label, coding_step_title)` permite que a mesma estrutura
  renderize "Trecho N de M" (document) ou "Referência N de M — @bibref"
  (abstract). O contexto de cada entrada é `("entry", índice, total, bibref)`;
  o bibref aparece no título da seção.
  - **`synesis_coder/modes/abstract_mode.py`**: `process_abstract`/
    `_process_one_abstract`/`_process_batch` aceitam `debug`/contexto; criam o
    recorder, propagam o índice global da entrada entre batches e gravam o relatório.
  - **`synesis_coder/cli.py`**: opção `--debug` no comando `abstract`.

### Changed

- **Relatório de debug não trunca mais o conteúdo** — instruções de sistema
  (com GUIDELINES), mensagens do usuário (documento/abstract) e respostas da IA
  são exibidas na íntegra. O truncamento anterior (`… (truncado para
  legibilidade)`, limites de 1200/1500/2000 chars) impedia o pesquisador de
  auditar exatamente como o prompt foi montado e o que o documento entregou —
  justamente o propósito da flag. Removidos `_truncate()` e `SOURCE_PREVIEW_CHARS`.

### Fixed

- **Bloco SOURCE ausente no relatório de debug do modo `document`** — o evento
  da chamada SOURCE recebia `phase="chunk"` (em vez de `"source"`) porque o
  contexto era setado via `set_context()` na thread do event-loop, invisível à
  thread worker do `asyncio.to_thread`. Agora `_generate_source_block` passa
  `context=("source",)` diretamente a `call_async`, e a "Etapa 1 — Geração do
  bloco SOURCE" volta a aparecer.

- **`tests/test_debug_log.py`** — além dos testes do modo document, cobre a
  unidade `entry` (rótulos "Referência", exibição do bibref, cabeçalho/rodapé
  adaptados) e a renderização ordenada das entradas.

## [0.4.2] — 2026-06-12

### Added

- **Verbosity flags `-v`/`-q` on `synesis-coder` CLI** (`synesis_coder/cli.py`)
  - `-v` / `--verbose` (count): raises log level to DEBUG. Repeatable.
  - `-q` / `--quiet` (count): lowers to WARNING (`-q`) or ERROR (`-qq`). Repeatable.
  - Both options added to `main` group; wired through `_configure_logging(verbose, quiet)`.
  - Distinct from `--format` (which controls output *style* — plain vs. verbose token usage); `-v/-q` controls Python logging only.
  - `-v, --verbose` and `-q, --quiet` added to `Global Options:` block in `_build_main_help()`.

## [0.4.1] — 2026-06-11

### Added

- **Quality toolchain and CI** (`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`)
  - `ruff==0.15.17` and `mypy==1.15.0` added to `dev` extras (pinned, in sync with ecosystem).
  - `[tool.ruff]`: `line-length=100`, `target-version="py310"`; rules `["E","F","I","UP","B","SIM","C4"]`.
  - `[tool.mypy]`: `ignore_missing_imports=true`, `disallow_untyped_defs=false`.
  - `.pre-commit-config.yaml`: `ruff` (lint + `--fix`), `ruff-format`, `mypy`, standard file-hygiene hooks.
  - CI workflow (3 OS × 3 Python): `test` (pytest, skips `integration` marker), `lint`, `build`, `integration` (`synesis-coder --help/--version`).

- **`synesis>=0.5.5` constraint** (`pyproject.toml`)
  - Updated from `>=0.3.0`; aligns with the compatibility matrix documented in the README.

- **CLI snapshot tests** (`tests/test_cli.py`)
  - Structural anchor assertions on `--help` output and `--version` — regression guard for CLI changes.

### Changed

**`synesis_coder/cli.py`**
- **CLI fully translated to English and aligned with the synesis compiler pattern.**
  - `_build_main_help()`: all user-facing strings translated to English — title, description, group labels, command summaries, option descriptions, and footer hint.
  - Group labels renamed: "Ingestão & Extração" → "Ingestion & Extraction", "Estruturação & LLM" → "Structuring & LLM", "Pipeline ACT (Revisão e Consolidação)" → "ACT Pipeline (Review & Consolidation)".
  - Phase tags translated: "[Fase N]" → "[Phase N]" in critique / normalize / incorporate summaries.
  - Section header renamed: "Opções Globais:" → "Global Options:", "Comandos:" → "Commands:", "Uso:" → "Usage:".
  - Footer hint: "Execute 'synesis-coder COMANDO --help'…" → "Run 'synesis-coder COMMAND --help'…".
  - `_ex()`: "Exemplos:" → "Examples:"; all inline comments and example paths translated.
  - All nine epilogs (`_EPILOG_ITEM` … `_EPILOG_INCORPORATE`) rewritten in English with paths matching the English terminology (e.g. `annotations/`, `revisions/`).
  - All subcommand `help=` strings and docstrings translated to English.
  - `_SynesisGroup.get_help()` now writes via `sys.stdout.buffer` with explicit UTF-8 encoding (matching the synesis compiler fix), preventing character corruption on Windows terminals when `--help` is passed.

---

## [0.4.0] — 2026-06-11

### Added

**`synesis_coder/modes/document_mode.py`**
- **Semantic Chunking (structure-aware)**: `split_into_chunks` agora detecta automaticamente documentos com estrutura Markdown (≥2 cabeçalhos ATX `#`…`######`) e usa o novo modo semântico antes de cair no algoritmo size-based existente.
  - `_ATX_HEADER`: regex compilada de escopo de módulo para detecção de cabeçalhos.
  - `_has_markdown_structure(text, min_headers=2)`: retorna `True` se o texto tem pelo menos `min_headers` cabeçalhos ATX. Threshold de 2 evita tratar documentos com título único (sem subdivisão real) como estruturados.
  - `_parse_markdown_sections(text)`: divide o texto em `(header_line, section_text)` por cabeçalho ATX; preâmbulo antes do primeiro cabeçalho vira seção com `header_line` vazia.
  - `_split_by_headers(text, chunk_size, overlap)`: empacota seções consecutivas até `chunk_size`; seções maiores que o teto são subdivididas via `_split_by_sentences` com o cabeçalho da seção replicado como prefixo de contexto em cada subchunk.
- **Degradação graciosa**: documentos sem cabeçalhos (entrevistas `.txt`, texto corrido) continuam usando o algoritmo size-based (parágrafo → sentença) sem nenhuma alteração no comportamento.
- Interface `split_into_chunks(text, chunk_size, overlap)` **inalterada** — nenhuma quebra de CLI, API ou integração.

**`tests/test_document_mode.py`**
- `TestHasMarkdownStructure` (5 casos): cobertura de `_has_markdown_structure`.
- `TestParseMarkdownSections` (4 casos): parse em seções, preâmbulo, ausência de cabeçalhos.
- `TestSplitByHeaders` (5 casos): agrupamento de seções pequenas, subdivisão de seção gigante, dispatch semântico, preservação de conteúdo, fallback size-based.
- `test_fallback_for_text_without_headers`: regressão — texto corrido produz resultado coerente via fallback.

### Changed

- Docstring do módulo `document_mode.py` atualizada para descrever modo semântico + fallback.

---

## [0.3.3] — 2026-06-11

### Fixed

**`synesis_coder/llm_client.py`**
- **P1 — Detecção de truncamento**: `_call_sync_inner` agora inspeciona `finish_reason` (branch OpenAI) e `stop_reason` (branch Anthropic) após cada chamada LLM. Quando o modelo trunca a resposta por limite de tokens, emite `WARNING` com o valor de `max_tokens` usado. Antes, truncamentos eram silenciosos — o chunk retornava um bloco ITEM cortado no meio sem nenhuma indicação.
- **P1-bis — `max_tokens` dinâmico**: introduzida precedência de três camadas para `max_tokens`:
  1. `SYNESIS_CODER_MAX_TOKENS` (env) — vence tudo
  2. `min(teto_via_API, estimativa_por_chunk)` — dinâmico: `_estimate_max_tokens()` calcula `len(chars) / 4 × 1.2` com piso `_DEFAULT_MAX_TOKENS`; `_discover_model_output_cap()` consulta o teto do modelo via API (lazy, cacheado)
  3. Valor explícito do chamador (ex.: `suggest_mode` com 512)
  Corrige também um bug em que `SYNESIS_CODER_MAX_TOKENS` era ignorado quando `thinking=False` (agora o override de env se aplica a todos os modos).
- **`_DEFAULT_MAX_TOKENS = 4096`**: literal promovido a constante nomeada nas quatro assinaturas públicas (`call`, `fix`, `call_async`, `fix_async`) — sem quebra de interface.

**`synesis_coder/modes/document_mode.py`**
- **P3 — `_item_signature` sem hardcode**: o campo de quotation buscado para a assinatura de deduplicação era hardcoded como `text:`, que não existe no template lattes (usa `trecho:`). A assinatura retornava só chains → deduplicação excessiva (51→26 ITEMs com 60% overlap). Corrigido: `_item_signature(item_text, quotation_field=None)` recebe o nome real do campo derivado do `ctx["item_fields"]`.
- **P3 — deduplicação exata**: `merge_and_dedup` substituiu o threshold de 60% de overlap por igualdade exata de `frozenset`. ITEMs com chains distintas mas com alguma sobreposição não são mais descartados. Resultado imediato: 53 ITEMs extraídos → 53 ITEMs após deduplicação (zero perdas), vs. 26 ITEMs na versão anterior para o mesmo documento.

---

## [0.3.2] — 2026-06-10

### Changed

**`synesis_coder/llm_client.py`**
- `_wait_honoring_retry_after()` — nova função de espera usada pelos dois decoradores `@retry` (backends Anthropic e OpenAI-compat). Quando a API devolve um erro 429 com header `Retry-After`, aguarda exatamente o tempo indicado em vez de calcular backoff exponencial cego. Fallback: mantém o `wait_exponential(multiplier=2, min=4, max=60)` original quando o header está ausente. Comportamento sem 429 é byte-a-byte idêntico ao anterior.
- Os dois decoradores `@retry` em `_call_sync_inner` (branch OpenAI linha ~397 e branch Anthropic linha ~452) substituem `wait=wait_exponential(...)` por `wait=_wait_honoring_retry_after`. O controle reativo de `Retry-After` agora cobre ambos os backends; o sleep proativo por janela de tokens continua exclusivo do backend Anthropic.

---

## [0.3.1] — 2026-04-25

### Fixed

**`synesis_coder/modes/incorporate_mode.py`**
- `_META_TAGS` ampliado: adicionados `"note"`, `"reason_detail"` e `"phase"`. O LLM de critique usava `# $note:` como raciocínio livre; anteriormente `incorporate` tentava substituir o campo `note:` do ITEM com esse texto.
- `_replace_field_value`: quando um ITEM tem múltiplos campos de mesmo nome (ex: vários `chain:` num ITEM complexo), a substituição agora faz match por **nó-fonte** — extrai o primeiro token antes de `->` da sugestão e seleciona a ocorrência cujo valor atual começa com o mesmo nó. Antes, sempre substituía a primeira ocorrência, independente de qual chain o LLM endereçava.
- `_apply_revision_tags`: chaves numeradas geradas pelo parser de critique (`chain.1`, `chain.2`) são normalizadas para o nome de campo base antes da substituição.

**`synesis_coder/modes/critique_mode.py`**
- `_parse_critique_response`: quando o LLM emite múltiplos `# $chain:` (um por chain a corrigir num ITEM com vários campos chain), as ocorrências adicionais são armazenadas com sufixo numérico (`chain.1`, `chain.2`) em vez de sobrescrever a chave anterior.

**`synesis_coder/prompt_builder.py`**
- `_build_critique_output_format`: introduzido `# $reason_detail:` como tag explícita para explicações livres do LLM (substitui o uso incorreto de `# $note:`). Adicionadas instruções explícitas: (a) nunca emitir `# $note:`, (b) ao corrigir múltiplos chains num ITEM, manter o mesmo nó-fonte em cada `# $chain:` para identificação unívoca da ocorrência.

---

## [0.3.0] — 2026-04-25

### Added

**Pipeline ACT (Annotation with Critical Thinking) — 4 fases**

Implementação das Fases 2, 3 e 4 do pipeline ACT descrito em `Estudo_Phases_Coder.md`.
A Fase 1 (Extração) era o comportamento pré-existente de `document` / `item`.

**`synesis_coder/synr_io.py`** *(novo)*
- Formato `.synr` — superconjunto sintático de `.syn`: comentários `# $key: value` e blocos `# REVISION` são ignorados pelo compilador Synesis (gramática inalterada)
- `SynrDocument` — dataclass com `header`, `content` e `item_revisions`
- `parse_synr(path)` — lê `.synr` ou `.syn`; extrai cabeçalho e blocos `# REVISION` por ITEM
- `write_synr(path, doc)` — persiste `SynrDocument` em disco
- `create_synr(syn_content, header, item_revisions)` — injeta blocos `# REVISION` no conteúdo `.syn` original sem alterar nenhum byte fora dos ITEMs afetados
- `extract_revision_tags(item_block)` — extrai tags `# $key: value` de um bloco ITEM
- `serialize_revision_block(tags)` — serializa dict de tags para formato `# REVISION`

**`synesis_coder/modes/critique_mode.py`** *(novo)*
- Subcomando `critique` — Fase 2 do pipeline ACT
- Por ITEM: invoca LLM configurado para critique (`SYNESIS_CODER_CRITIQUE_MODEL`) e avalia fidelidade textual dos campos ao abstract/fonte
- Items com `suspicion_score >= threshold` (padrão: 0.20) recebem bloco `# REVISION` no `.synr` gerado
- Concorrência via `asyncio.gather` com `Semaphore` configurável
- Prioridade de texto-fonte: abstract completo do `.bib` > campo `text` do ITEM > sentinel
- `_parse_critique_response()` aceita formato `# $key: value` (preferido) e `key: value` (fallback)

**`synesis_coder/modes/normalize_mode.py`** *(novo)*
- Subcomando `normalize` — Fase 3 do pipeline ACT
- Constrói inventário global de códigos cross-file: conceitos de campos `chain` e valores de campos `code`
- Normalização determinística: agrupa variantes pela chave `lowercase+underscore`; canonical = variante mais frequente (desempate: forma com underscore > ordem alfabética)
- LLM em chunks (`SYNESIS_CODER_NORMALIZATION_MODEL`) para grupos residuais com ≥2 variantes após normalização determinística
- Aceita sugestões LLM com `merge_confidence >= confidence_threshold` (padrão: 0.65)
- `_substitute_code_in_chain()` — substituição token-a-token preservando relações (`ENABLES`, `INFLUENCES`, etc.)
- `_write_inventory_txt()` — inventário de códigos em TXT com contagens e canonical por grupo
- Emite um `.synr` por arquivo de entrada com blocos `# REVISION` contendo `# $chain:` e/ou `# $code:`

**`synesis_coder/modes/incorporate_mode.py`** *(novo)*
- Subcomando `incorporate` — Fase 4 do pipeline ACT (determinístico, sem LLM)
- Aplica tags `# $<field>:` por ITEM com validação sintática via `synesis.load()` antes de cada substituição; rejeita com rollback e warning se a substituição quebrar a compilação
- Remove todos os blocos `# REVISION` e linhas de metadados `# $key: value`
- Grava métricas no cabeçalho do `.syn` final: `fields_changed`, `fields_rejected`, `items_revised`, `ACS` (Annotation Change Score = changed / (changed + rejected)), `timestamp`, `source`
- `_validate_phase_env()` em `cli.py` — valida variáveis de ambiente por fase com fallback para `SYNESIS_CODER_MODEL` e mensagem instrucional quando ausentes

**`synesis_coder/prompt_builder.py`**
- `build_critique_prompt(ctx, item_block, source_text)` — prompt parametrizado pelos `FIELD` + `GUIDELINES` do template; inclui critérios por campo e guia de pontuação de suspeição (0.00–1.00)
- `build_normalization_prompt(ctx, code_groups)` — prompt para canonicalização semântica de grupos de códigos residuais; output estruturado com `# $group:`, `# $suggested_canonical:`, `# $merge_confidence:`

**`synesis_coder/cli.py`**
- `_validate_phase_env(phase_name)` — valida `SYNESIS_CODER_<PHASE>_MODEL` com fallback para `SYNESIS_CODER_MODEL`; verifica `ANTHROPIC_API_KEY` quando backend = anthropic
- Subcomando `critique` — `SYN_FILE`, `--project`, `--output`, `--concurrent`, `--threshold`, `--format`, `--model`
- Subcomando `normalize` — `SYNR_FILES...` (múltiplos), `--project`, `--output-dir`, `--concurrent`, `--confidence`, `--inventory`, `--format`, `--model`
- Subcomando `incorporate` — `SYNR_FILE`, `--project`, `--output`, `--format`
- Help principal atualizado com seção "PIPELINE ACT" e exemplos para cada novo subcomando
- Novos subcomandos exibidos em ciano no bloco `Commands:` para distinguir dos modos de extração (verdes)

**Testes**
- `tests/test_synr_io.py` — 30 testes: round-trip, parsing de tags, namespaces, integração com `synesis.load()`
- `tests/test_phase_env_validator.py` — 17 testes: precedência de variáveis, fallbacks, backends, mensagens de erro
- `tests/test_incorporate_mode.py` — 36 testes: substituição de campos, rejeição sintática, métricas ACS, integração com `synesis.load()`
- `tests/test_critique_mode.py` — 31 testes: extração de texto-fonte, parse de resposta LLM, fluxo com mock LLM, integração com compilador
- `tests/test_normalize_mode.py` — 44 testes: normalização de chave, extração de conceitos de chain, inventário cross-file, normalização determinística, parse LLM, geração de revisões

### Changed

- `synesis_coder/cli.py` — `_cmd_line()` reconhece `critique`, `normalize`, `incorporate` como nomes de subcomandos para colorização
- `synesis_coder/prompt_builder.py` — prompt de critique parametrizado pelos GUIDELINES do template (sem campos hardcoded)

---

## [0.2.0] — 2026-04-16

### Added

**`synesis_coder/llm_client.py`**
- `SYNESIS_CODER_THINKING_BUDGET` — ativa extended thinking (Anthropic Claude 4.x); 0 = desabilitado (padrão). Valores recomendados: 4000 leve / 8000 médio (face85) / 16000 pesado (ontology)
- `_get_thinking_budget()`, `_model_supports_thinking()`, `_THINKING_CAPABLE_MODELS` — detecta suporte ao thinking por modelo; emite aviso claro quando modelo incompatível, continua sem thinking
- `_get_env_temperature()` — `SYNESIS_CODER_TEMPERATURE` agora é lida e aplicada a todos os modos analíticos (`thinking=True`); antes era variável inerte
- `_get_max_tokens_override()` — `SYNESIS_CODER_MAX_TOKENS` agora é lida e aplicada a todos os modos analíticos; antes era variável inerte
- `call()` e `call_async()` aceitam `thinking_budget: int | None` — permite override pontual sem alterar o `.env`
- Bloco 1b no `.env` e `.env.example` com `claude-opus-4-7` (recomendado com `THINKING_BUDGET=8000`)

**`synesis_coder/prompt_builder.py`**
- Injeção de instrução `OUTPUT LANGUAGE` nos system prompts de item, abstract e ontology quando `output_language` está definido no contexto

**`synesis_coder/project_loader.py`**
- Lê `SYNESIS_CODER_LANGUAGE` do ambiente e expõe como `ctx["output_language"]`; `None` quando não definida (preserva comportamento v0.1.x)

**`synesis_coder/cli.py`**
- Flags `--thinking-budget INT`, `--language TEXT`, `--max-tokens INT`, `--temperature FLOAT` adicionadas aos comandos `item`, `abstract`, `document` e `ontology`
- Precedência: flag CLI > variável `.env` > default do modo
- Exemplos de extended thinking e `--language` adicionados ao help de `item`

### Changed

- `fix()` e `fix_async()` passam `thinking=False` — chamadas de correção não ativam extended thinking (economia de custo)
- Branch Anthropic de `_call_sync_inner` itera `response.content` por `block.type == "text"` em vez de acessar `content[0].text` diretamente — necessário para receber resposta correta quando `ThinkingBlock` precede o `TextBlock`
- `SYNESIS_CODER_TEMPERATURE` e `SYNESIS_CODER_MAX_TOKENS` passam a ter efeito real (antes documentadas mas inertes); `SYNESIS_CODER_MAX_RETRIES`, `MAX_RPM`, `MAX_INPUT_TPM` e `MAX_OUTPUT_TPM` já eram funcionais

### Fixed

- Documentação enganosa no `.env.example`: seção OPCIONAIS agora reflete corretamente quais variáveis são funcionais
- Mock de testes `_make_mock_anthropic_response` atualizado para `block.type = "text"` (compatível com nova iteração de content blocks)

---

## [0.1.5] — 2026-04-09

### Added

**`synesis_coder/modes/finetune_mode.py`** *(novo)*
- `process_finetune(output_path, project_path, input_path, enrich, concurrent, model, format)` — enriquece dataset Alpaca via LLM (Camada 2)
- Duas fontes de entrada mutuamente exclusivas: `--project` (compila e gera Camada 1 internamente via `build_alpaca_pairs()`) ou `--input` (carrega JSONL pré-gerado)
- `_quality_filter()` — descarta pares com instruction < 15 chars ou output < 10 chars; sempre aplicado antes do enriquecimento
- `_enrich_one()` — enriquece um par via LLM com concorrência controlada por `asyncio.Semaphore`
- Três tipos de enriquecimento (flag `--enrich`, repetível):
  - `vary` (padrão): paráfrase da instruction via LLM; aplicado a todos os pares; duplica aproximadamente o dataset
  - `didactic`: reformula chains como explicação pedagógica; apenas pares chain/causal
  - `counterfactual`: gera par "e se X fosse diferente?"; apenas pares chain/causal
- `_is_chain_pair()` — detecta pares chain/causal por palavras-chave na instruction
- `_parse_qa_response()` — extrai campos QUESTION/ANSWER de resposta estruturada do LLM
- `_deduplicate()` — remove pares com (instruction, input) idênticos após mescla
- Formato `verbose`: exibe fonte, tipos de enriquecimento, tokens e estatísticas

**`synesis_coder/cli.py`**
- Comando `finetune` adicionado ao grupo principal
- `--project PATH` / `--input PATH` (mutuamente exclusivos): fonte dos pares Alpaca
- `--output PATH` (obrigatório): destino do JSONL enriquecido
- `--enrich [vary|didactic|counterfactual]` (múltiplo, padrão `vary`)
- `--concurrent INTEGER` (padrão 5), `--format [plain|verbose]`, `--model TEXT`
- Help (`--help`) e seção "MODO finetune" no help global explicam as duas formas de uso e os três tipos de enriquecimento com exemplos comentados

---

## [0.1.4] — 2026-04-08

### Added

**`synesis_coder/token_usage.py`** *(novo)*
- `TokenUsage` — dataclass thread-safe que acumula `input_tokens`, `output_tokens`, `api_calls` e `corrections` ao longo de uma execução
- `record(input_tok, output_tok, is_correction)` — registra tokens de uma chamada com lock; `is_correction=True` incrementa `corrections`
- `summary_line()` — formata linha para o terminal: `tokens: in X,XXX | out X,XXX | total X,XXX | calls N [| corrections N]`
- `reset()` — reinicia todos os contadores

**`synesis_coder/llm_client.py`**
- `self.usage: TokenUsage` — acumulador de sessão, exposto publicamente; reflete todas as chamadas do cliente desde sua instanciação
- `self._correction_local: threading.local` — flag por thread para marcação de correções; garante segurança em modos concorrentes (`abstract`, `document`, `ontology`) onde `fix_async()` corre em threads separadas via `asyncio.to_thread()`
- `_record_usage()` — agora acumula em `self.usage` além das deques de rate-limiting existentes; lê e reseta o flag `_correction_local.is_correction`
- `fix()` — seta `_correction_local.is_correction = True` antes de delegar para `call()`
- `fix_async()` — usa wrapper `_fix_in_thread()` para setar o flag *dentro* da thread worker, evitando que o flag do event loop seja lido pela thread errada; o rate-limiting proativo permanece no event loop
- Branch OpenAI de `_call_sync_inner` — registra tokens em `self.usage` (anteriormente ignorava `response.usage`)

**`synesis_coder/modes/`** — todos os 5 modos
- Formato `verbose` exibe `# tokens: in X | out X | total X | calls N` no cabeçalho de saída
- Modos afetados: `item_mode.py`, `suggest_mode.py`, `abstract_mode.py`, `document_mode.py`, `ontology_mode.py`
- Formato `plain` preservado inalterado (compatibilidade com pipes e extensão VSCode)

**`tests/test_token_usage.py`** *(novo)*
- `TestTokenUsageRecord` (3): acumulação, `total_tokens`, flag `is_correction`
- `TestTokenUsageSummaryLine` (3): formatação com/sem correções, estado zerado
- `TestTokenUsageReset` (1): `reset()` zera todos os campos
- `TestTokenUsageThreadSafety` (1): 10 threads concorrentes sem race condition
- `TestLLMClientCorrectionFlag` (6): `call()` não marca correção; `fix()` marca; reset após uso; `fix_async()` marca; concorrência de `fix_async()` sem colisão de flags

### Changed

**`tests/test_item_mode.py`**, **`tests/test_document_mode.py`**
- Testes de formato verbose (`test_item_verbose_format`, `test_process_document_verbose_format`) verificam presença de `"tokens:"` no output

---

## [0.1.3] — 2026-04-06

### Added

**`synesis_coder/modes/suggest_mode.py`** *(novo)*
- `process_suggest(project_path, text, format, model)` — sugere códigos relevantes para um trecho de texto
- Fluxo adaptativo: dois passos (tópico → código) para projetos com > 100 códigos; passo único para projetos menores
- `_select_topics()` — passo 1: LLM identifica 2-4 tópicos relevantes dentre os disponíveis no projeto; fallback por frequência se resposta inválida
- `_build_enriched_code_list()` — filtra e enriquece a lista de códigos com frequência (`code_index["stats"]`) e descrição semântica (`ontology_index["ontology_description"]`); limita a 60 códigos por chamada
- `_postprocess()` — verifica sugestões e marca automaticamente `[NEW]` em códigos que não existem no projeto

**`synesis_coder/prompt_builder.py`**
- `build_topic_filter_prompt(available_topics, text)` — prompt mínimo para passo 1 (identificação de tópicos); ~130 tokens, temperatura 0.0
- `build_suggest_prompt(ctx, text, enriched_codes)` — prompt para sugestão de códigos; inclui contexto do projeto (truncado a 200 chars), lista enriquecida e formato de resposta bullet

**`synesis_coder/cli.py`**
- Subcomando `suggest`: `--project`, `--text`, `--format` (plain/verbose), `--model`
- Seção de exemplos do `suggest` adicionada ao help do CLI

### Changed

**`synesis_coder/cli.py`**
- Help do CLI atualizado com seção "MODO suggest" e exemplos

---

## [0.1.2] — 2026-04-04

### Added

**`synesis_coder/llm_client.py`**
- Suporte a backends OpenAI-compatíveis via `SYNESIS_CODER_BACKEND=openai`
- `SYNESIS_CODER_API_URL` — base URL do endpoint (Ollama local, RunPod, Together AI, etc.)
- `SYNESIS_CODER_API_KEY` — chave para APIs que exigem autenticação (Ollama: ignorada)
- Rate limiting desabilitado automaticamente no backend OpenAI (sem cotas externas)
- Retry adaptado por backend: `openai.APIStatusError` / `openai.APIConnectionError`
- `_translate_messages_openai()` — converte formato interno para OpenAI Chat Completions (campo `cache` ignorado silenciosamente)
- `_translate_messages_anthropic()` — código anterior renomeado, comportamento inalterado
- Fix messages traduzidos para inglês (melhor instruction-following em modelos menores)
- `openai>=1.0` adicionado como dependência core

### Fixed

**`synesis_coder/cli.py`**
- Modelo padrão exibido no help agora reflete corretamente o valor de `SYNESIS_CODER_MODEL` no `.env` — `load_dotenv()` chamado dentro de `_default_model()` antes de ler a variável de ambiente

### Changed

**`synesis_coder/prompt_builder.py`**
- Todos os prompts traduzidos de português para inglês para melhor instruction-following com modelos open-source menores (Qwen3, Gemma)

**`tests/test_abstract_mode.py`**, **`tests/test_document_mode.py`**
- Assertions atualizadas para refletir strings em inglês nos prompts

---

## [0.1.1] — 2026-04-01

### Fixed

**`synesis_coder/cli.py`**
- Forçar encoding UTF-8 em `sys.stdout`, `sys.stderr` e `sys.stdin` no topo do
  módulo, antes de qualquer `click.echo` — corrige corrupção de acentos e
  caracteres especiais quando invocado como processo filho pelo VSCode (Windows
  usa cp1252 por padrão)

**`synesis_coder/__main__.py`**
- Mesma correção de encoding UTF-8 para invocação via `python -m synesis_coder`

### Changed

**`synesis_coder/cli.py`**
- Help CLI completamente reformulado: exibe versão do `synesis-coder`, versão
  do compilador `synesis` e modelo LLM padrão em uso (lidos em runtime)
- Exemplos de uso para todos os 4 modos (`item`, `abstract`, `document`,
  `ontology`) com todas as opções relevantes documentadas
- Cores ANSI aplicadas ao help para facilitar leitura: títulos de seção em
  amarelo, comandos em verde, flags em ciano, comentários em cinza —
  automaticamente suprimidas em pipes e redirecionamentos (detecção de TTY)
- Subclasse `_SynesisGroup` bypassa o formatter do Click, preservando
  indentação e quebras de linha exatas nos exemplos de código

---

## [0.1.0] — 2026-03-23

### Added — Phase 2: `abstract` mode

**`synesis_coder/modes/abstract_mode.py`**
- `process_abstract(project_path, bibref, format, model)` — generates structured
  academic abstracts from the project's corpus of ITEM blocks for a given bibref
- Loads all ITEM blocks, excerpts QUOTATION/MEMO/NOTE fields (template-driven),
  and injects them as context into the LLM prompt
- Validates output via compiler; correction loop with temperature escalation

**`synesis_coder/prompt_builder.py`**
- `build_abstract_prompt(ctx, bibref, excerpts)` — assembles Anthropic API message
  list for abstract generation; system prompt cached, user message dynamic
- Excerpt injection: bibref metadata (author/year via BibTeX), field content per
  ITEM block, ordered by document position

---

### Added — Phase 3: `document` mode

**`synesis_coder/modes/document_mode.py`**
- `process_document(project_path, output, format, model)` — batch-generates ITEM
  blocks for all SOURCEs in a project that have no ITEM annotations yet
- Concurrent processing via `asyncio.Semaphore` (default 5 simultaneous calls)
- Progress reporting per source; appends results to a `.syn` output file

**`synesis_coder/llm_client.py`**
- `AsyncLLMClient` — async counterpart of `LLMClient` using `anthropic.AsyncAnthropic`
- Shared rate-limiting logic (RPM + TPM semaphores) across concurrent requests
- `call_async(messages, temperature)` / `fix_async(previous_output, errors, temperature)`

---

### Added — Phase 4: `ontology` mode

**`synesis_coder/modes/ontology_mode.py`**
- `process_ontology(project_path, output_path, update, concurrent, model, format)` —
  batch-generates ONTOLOGY entries for all codes found in the project corpus
- `_build_semantic_ctx(code, ctx)` — assembles rich per-code context:
  frequency (# items using the code), sources (# distinct SOURCEs), relations
  from CHAIN fields (up to 15), co-occurrences with other codes (up to 20),
  representative examples (up to 3 excerpts from QUOTATION/NOTE fields)
- `_get_pending_codes(ctx, update)` — with `--update`: skips codes already
  defined in `ontology_index`; without: generates all codes
- Concurrent processing via `asyncio.Semaphore`
- Raises `ValueError` if the project template has no ONTOLOGY scope fields

**`synesis_coder/prompt_builder.py`**
- `build_ontology_prompt(ctx, code, semantic_ctx)` — ontology prompt with cached
  system message (template fields, project description, available TOPIC codes)
  and dynamic user message (code name, semantic stats, relations, co-occurrences,
  examples)

**`synesis_coder/validator.py`**
- `validate_ontology_entry(output, ctx, llm_client, ontology_key, max_tries)` —
  validates ONTOLOGY blocks via `synesis.load(..., ontology_contents={key: output})`
- `validate_ontology_entry_async(...)` — async counterpart for concurrent use
- `_extract_ontology_blocks(text)` — extracts only `ONTOLOGY...END ONTOLOGY`
  blocks from LLM output (discards ITEM/SOURCE noise)

**`synesis_coder/project_loader.py`**
- `load_project()` now returns `required_ontology: List[str]` — required fields
  in ONTOLOGY scope, derived from `result.template.required_fields[Scope.ONTOLOGY]`
- `has_ontology_scope: bool` — True when template defines at least one ONTOLOGY field

**`synesis_coder/cli.py`**
- `ontology` subcommand fully implemented: `--project`, `--output`, `--update`,
  `--concurrent` (default 5), `--format`, `--model`

---

### Added — Backup feature

- When running `ontology` mode **without** `--update` and the output `.syno` already
  exists, a backup is automatically created as `{stem}_bkp.syno` before overwriting
- Prevents accidental loss of hand-curated ontology entries

---

### Added — Tests

**`tests/test_ontology_mode.py`** — 15 unit tests + 3 integration tests:
- `TestGetPendingCodes` (3): all codes returned without `--update`; defined codes
  excluded with `--update`; empty result when all codes already defined
- `TestBuildSemanticCtx` (4): frequency/source counts; relation extraction from
  CHAIN triples; examples from QUOTATION fields; graceful empty ctx for codes with
  no linked data
- `TestOntologyPromptBuilder` (5): system + user structure; system prompt cached;
  code name in user message; frequency/source stats in user message; relations in
  user message
- `TestValidateOntologyEntry` (3): single ONTOLOGY block extracted; ITEM/SOURCE
  blocks discarded; empty string on no blocks
- `TestOntologyModeIntegration` (3, require `ANTHROPIC_API_KEY`): social_acceptance
  generates valid entry; `--update` skips existing codes; thompson project raises
  `ValueError` (no ONTOLOGY scope)

---

### Changed

- `--version` flag now reports `0.1.0`

---

## [0.0.1] — 2026-03-10

### Added — Phase 1: `item` mode

This release implements the MVP of `synesis-coder`: generating Synesis ITEM blocks
from text and a bibliographic reference, with compiler-based validation and an
automatic LLM correction loop.

#### New modules

**`synesis_coder/project_loader.py`**
- `load_project(project_path, load_annotations, load_ontology)` — the single
  function that invokes `synesis.load()` to load project context
- Separates fields by scope (`SOURCE`, `ITEM`, `ONTOLOGY`) from
  `result.template.field_specs`
- Detects the `CHAIN` field in `ITEM` scope and extracts its relations
- Builds `code_index` by combining `code_usage` (from `CODE` fields) with nodes
  from `all_triples` — so CHAIN-only projects (no `CODE` field) still get a
  populated index
- Builds `topic_index` from `linked_project.topic_index`
- Reads project description via `result.linked_project.project.description`
  (the compiler already processes the `DESCRIPTION...END DESCRIPTION` block)
- `load_ontology=False` by default — prevents errors when loading projects
  whose `.syno` references fields absent from the current template
- Bibliography (`.bib`) always loaded regardless of `load_annotations` flag,
  since it is required for compiler validation

**`synesis_coder/prompt_builder.py`**
- `build_item_prompt(ctx, bibref, text)` — assembles the Anthropic API message
  list with prompt caching on the system message
- Cached system prompt contains: absolute Synesis format rules, project
  description, per-field instructions derived from the template, existing
  concept index (`code_index`), and existing topic index (`topic_index`)
- `_field_instruction(name, spec, ctx)` — generates per-field instruction using
  `guidelines` > `description` > generic instruction by `FieldType`
- `CHAIN` fields: injects available relations and list of existing concepts
- `ORDERED`/`ENUMERATED` fields: injects allowed values with labels
- `SCALE` fields: injects range from format string
- Dynamic user message: `BIBREF: @{bibref}` + `<text>{text}</text>`
- Prompt caching active from `item` mode (reduces latency and cost per session)

**`synesis_coder/llm_client.py`**
- `LLMClient` class — the only module that imports `anthropic`
- Loads `ANTHROPIC_API_KEY` via `python-dotenv` (`.env` in project root)
- Supports alternative model via `model` parameter or `SYNESIS_CODER_MODEL` env var
- Default model: `claude-opus-4-6`
- Rate limiting: RPM semaphore + 60-second sliding window for TPM
  (input and output tokens tracked separately)
- `call(messages, temperature)` — translates internal format to Anthropic API
- `fix(previous_output, errors, temperature)` — correction call with previous
  output and compiler diagnostics
- `_translate_messages()` — converts `[{"role", "content", "cache"}]` to
  `system` blocks with `cache_control` and the API `messages` list

**`synesis_coder/validator.py`**
- `validate_and_fix(output, ctx, llm_client, annotation_key, max_tries)` —
  validates output via `synesis.load()` and requests LLM corrections if invalid
- `_has_structural_errors(result)` — filters `OrphanItem` from the error list;
  `OrphanItem` (ITEM without a corresponding SOURCE) is expected when validating
  an isolated ITEM — the SOURCE exists in the project's `.syn` but is not loaded
  to avoid exceeding API token limits
- `_extract_item_blocks(text)` — extracts only `ITEM...END ITEM` blocks from
  the output, discarding `SOURCE`, `ONTOLOGY`, or markdown blocks the LLM adds
  even when instructed not to
- `_strip_markdown_fences(text)` — removes ` ``` ` delimiters from LLM output
- Temperature escalation across correction attempts:
  `CORRECTION_TEMPERATURES = [0.0, 0.2, 0.5]` — avoids deterministic loops
- Error fallback: commented error header prepended to last output when all
  correction attempts are exhausted

**`synesis_coder/modes/item_mode.py`**
- `process_item(project_path, bibref, text, format, model)` — orchestrates the
  full pipeline: load project → build prompt → call LLM → validate
- `plain` format: returns only the Synesis ITEM blocks (for piping to `.syn`
  files or editor use)
- `verbose` format: prepends a header with validation status, model, bibref,
  and timestamp (for interactive terminal use)

**`synesis_coder/cli.py`**
- Click CLI with four subcommands: `item`, `abstract`, `document`, `ontology`
- `--version` flag shows `0.0.1` (read from `pyproject.toml` via
  `importlib.metadata`)
- Usage examples included in the root command `--help`
- `abstract`, `document`, `ontology` subcommands print an informative message
  and exit with code 1 (pending implementation in future phases)

**`synesis_coder/__main__.py`**
- Enables invocation via `python -m synesis_coder`

#### Support files

**`pyproject.toml`**
- Dependencies: `synesis>=0.3.0`, `anthropic>=0.40.0`, `click>=8.0`,
  `tenacity>=8.0`, `bibtexparser>=1.4`, `python-dotenv>=1.0`
- Entry point: `synesis-coder = "synesis_coder.cli:main"`
- Build backend: `setuptools.build_meta`

**`.env.example`**
- Configuration template with required `ANTHROPIC_API_KEY` and optional vars:
  `SYNESIS_CODER_MODEL`, `SYNESIS_CODER_MAX_RETRIES`, `SYNESIS_CODER_TEMPERATURE`,
  and rate limiting limits

**`.gitignore`**
- `.env` and variants excluded (except `.env.example`)
- Python build artifacts: `__pycache__`, `*.pyc`, `.eggs`, `dist`, `build`, `.venv`

#### Tests

**`tests/test_item_mode.py`** — 17 tests using real projects from `d:/GitHub/case-studies/`:

*`TestProjectLoader` (6 tests — no LLM required):*
- `test_load_social_acceptance` — full template with GUIDELINES, ORDERED,
  ENUMERATED, SCALE, CHAIN
- `test_load_thompson_no_ontology_scope` — template without ONTOLOGY scope
- `test_load_nave` — template without CHAIN field
- `test_load_aids_corpus` — template with CHAIN and Portuguese relations, no GUIDELINES
- `test_code_index_populated` — projects with existing `.syn` populate `code_index`
- `test_project_not_found_raises` — `FileNotFoundError` for invalid path

*`TestPromptBuilder` (6 tests — no LLM required):*
- `test_prompt_structure` — system (cacheable) + user (dynamic) messages
- `test_system_prompt_contains_project_description` — DESCRIPTION block injected
- `test_system_prompt_contains_field_instructions` — ITEM fields listed
- `test_system_prompt_contains_chain_relations` — CHAIN relations included
- `test_user_message_contains_bibref_and_text` — bibref and text in user message
- `test_prompt_no_ontology_scope` — works correctly without ONTOLOGY scope

*`TestItemModeIntegration` (5 tests — require `ANTHROPIC_API_KEY`):*
- `test_item_social_acceptance_compiles` — output compiles for complex template
- `test_item_thompson_no_ontology_scope` — item mode works without ONTOLOGY scope
- `test_item_aids_corpus_compiles` — template with Portuguese relations
- `test_item_verbose_format` — status header present in verbose format
- `test_item_synesis_init_project` — compatibility with `synesis init` projects

#### Architectural decisions

- **Total compiler coupling**: all template, project, bibliography, and annotation
  reads go through `synesis.load()` — compiler updates are absorbed automatically
- **Dynamic templates**: no field name, scope, or relation is hardcoded — everything
  derived from `result.template.field_specs` at runtime
- **GUIDELINES as primary instruction**: `guidelines` > `description` > generic
  instruction by `FieldType`
- **DESCRIPTION via compiler**: `result.linked_project.project.description`
  instead of regex over `project_content`
- **`OrphanItem` ignored in item mode validation**: isolated ITEM has no SOURCE
  in the same file — filtered in `_has_structural_errors()`
- **`code_index` for CHAIN-only projects**: combines `code_usage` (CODE fields)
  with nodes from `all_triples` (CHAIN fields)
- **`load_ontology=False` default**: prevents errors in projects whose `.syno`
  references fields absent from the template (thompson_bible case)
- **`.bib` always loaded**: required for compiler validation regardless of
  `load_annotations` flag
- **Output cleaning pipeline**: `_strip_markdown_fences` → `_extract_item_blocks`
  → validation → correction loop
- **Prompt caching from item mode**: system prompt built once per session,
  marked with `cache_control: ephemeral`

---

[0.6.2]: https://github.com/usuario/synesis-coder/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/usuario/synesis-coder/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/usuario/synesis-coder/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/usuario/synesis-coder/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/usuario/synesis-coder/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/usuario/synesis-coder/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/usuario/synesis-coder/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/usuario/synesis-coder/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/usuario/synesis-coder/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/usuario/synesis-coder/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/usuario/synesis-coder/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/usuario/synesis-coder/compare/v0.1.5...v0.2.0
[0.1.5]: https://github.com/usuario/synesis-coder/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/usuario/synesis-coder/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/usuario/synesis-coder/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/usuario/synesis-coder/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/usuario/synesis-coder/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/usuario/synesis-coder/releases/tag/v0.1.0
[0.0.1]: https://github.com/usuario/synesis-coder/releases/tag/v0.0.1
