"""Configuração compartilhada da suíte.

Guarda central dos testes de integração — os que fazem chamada REAL à API do
LLM (custam tokens, dependem de rede e produzem saída não-determinística).

Por que a guarda vive aqui e não em cada módulo:
    Os módulos definiam `HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))`
    no nível do módulo e o usavam num `skipif`. Isso é avaliado no IMPORT, e
    `load_dotenv()` roda algumas linhas acima — então em qualquer máquina com
    `.env` (todo ambiente de dev) `HAS_API_KEY` era sempre `True` e o `skipif`
    NUNCA disparava. A proteção efetiva era só o marker `integration`,
    deselecionado por `addopts` no `pyproject.toml`; o `skipif` era inerte.

    Sem chave, esses testes também não pulavam: falhavam lá adiante com
    `OSError: ANTHROPIC_API_KEY não encontrada`, vindo de dentro do client —
    ruído que esconde falhas reais.

    `pytest_collection_modifyitems` roda DEPOIS de todos os imports, então lê o
    ambiente já com o `.env` aplicado. Uma única guarda, para os quatro módulos
    de integração, sem depender de ordem de import.

Defesa em profundidade (nenhuma camada sozinha basta):
    1. `addopts = -m 'not integration'` — deseleciona por padrão, local;
    2. `pytest -m "not integration"` explícito no CI;
    3. o CI não expõe credencial de API alguma;
    4. esta guarda — se alguém rodar `-m integration` sem chave, pula com
       mensagem clara em vez de falhar com erro de credencial.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:  # pragma: no cover - ambiente sem python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass


# Corpus de fixtures (projetos reais usados como entrada dos testes).
# Historicamente fixado em `d:/GitHub/case-studies`, um caminho absoluto da
# maquina de desenvolvimento — que nao existe em runner de CI algum. A suite
# passava localmente e falhava em 9 jobs no GitHub Actions.
#
# `SYNESIS_CASE_STUDIES` permite apontar para outro local; na ausencia dele o
# default preserva o comportamento local de sempre.
CASES_DIR = Path(
    os.environ.get("SYNESIS_CASE_STUDIES", "d:/GitHub/case-studies")
)
CASES_AVAILABLE = CASES_DIR.is_dir()


def pytest_collection_modifyitems(config, items):
    """Pula testes que dependem de recursos externos ausentes.

    Duas guardas independentes:

    1. `integration` sem `ANTHROPIC_API_KEY` — chamada real de API.
    2. Qualquer teste cujo modulo dependa do corpus de fixtures, quando o
       diretorio nao existe (o caso do CI).

    Ambas agem na coleta, DEPOIS de todos os imports — por isso leem o
    ambiente ja com o `.env` aplicado e nao dependem de ordem de import.
    """
    skip_no_key = pytest.mark.skip(
        reason="ANTHROPIC_API_KEY não disponível (teste de integração exige API real)"
    )
    skip_no_cases = pytest.mark.skip(
        reason=(
            f"corpus de fixtures ausente em {CASES_DIR} "
            "(defina SYNESIS_CASE_STUDIES para apontar a outro local)"
        )
    )

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    for item in items:
        if not has_key and "integration" in item.keywords:
            item.add_marker(skip_no_key)

        # O modulo declara CASES_DIR/_PROJECT quando depende do corpus. Ler o
        # atributo do modulo (em vez de manter uma lista de arquivos aqui)
        # mantem a guarda correta quando um teste novo passa a depender dele.
        if not CASES_AVAILABLE:
            module = getattr(item, "module", None)
            if module is not None and any(
                hasattr(module, attr) for attr in ("CASES_DIR", "_PROJECT")
            ):
                item.add_marker(skip_no_cases)
