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

import pytest

try:  # pragma: no cover - ambiente sem python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass


def pytest_collection_modifyitems(config, items):
    """Pula testes `integration` quando não há credencial de API.

    Só age sobre itens marcados; a suíte offline (a esmagadora maioria) não é
    tocada. Não substitui o filtro por marker — complementa-o para o caso em
    que os testes de integração são pedidos explicitamente.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return

    skip_no_key = pytest.mark.skip(
        reason="ANTHROPIC_API_KEY não disponível (teste de integração exige API real)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_no_key)
