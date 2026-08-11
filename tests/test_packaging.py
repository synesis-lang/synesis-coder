"""Contrato de empacotamento — o que sai no sdist antes de ir para o PyPI.

Por que existe:
    Uma publicacao no PyPI e IRREVERSIVEL: o nome fica reservado para sempre e
    uma versao ja enviada nunca pode ser sobrescrita. Se o artefato sair com
    metadata de licenca errada ou faltando arquivo, a correcao custa queimar o
    numero da versao. Estes testes constroem o sdist de verdade e inspecionam o
    PKG-INFO gerado, em vez de confiar no que o pyproject.toml declara.

O que `twine check` NAO pega (e por isso este arquivo existe):
    A sintaxe legada `license = {text = "..."}` compila sem erro mas emite o
    campo obsoleto `License:` em vez de `License-Expression:`. O twine passa,
    o PyPI recebe, e a licenca aparece vazia no indice. Foi o que aconteceu com
    as versoes ja publicadas de `synesis` e `synesis-lsp` (license: None no
    PyPI). O teste abaixo falha nesse cenario.

Custo: o build leva alguns segundos, entao a fixture e `session`-scoped e o
modulo inteiro pula se `build` nao estiver instalado.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from email.parser import Parser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _declared_version() -> str:
    """Versao do pyproject.toml — a fonte unica de verdade.

    `tomllib` so entrou na stdlib no 3.11, mas o pacote declara
    `requires-python = ">=3.10"` e o CI roda a matriz nos tres. Sem este
    fallback os testes de versao quebram apenas no piso.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


pytestmark = pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-c", "import build"], capture_output=True
    ).returncode
    != 0,
    reason="pacote `build` nao instalado (extra [dev])",
)


@pytest.fixture(scope="session")
def sdist_metadata(tmp_path_factory) -> tuple[dict[str, list[str]], list[str]]:
    """Constroi o sdist e devolve (campos do PKG-INFO, lista de arquivos).

    Usa `python -m build` COM isolamento: o ambiente local pode ter
    setuptools < 77, que ignora a forma string do PEP 639 e produziria um
    artefato diferente do que o CI gera.
    """
    outdir = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"build falhou:\n{proc.stdout}\n{proc.stderr}"

    tarballs = list(outdir.glob("*.tar.gz"))
    assert len(tarballs) == 1, f"esperado 1 sdist, encontrado {tarballs}"

    with tarfile.open(tarballs[0]) as tar:
        names = tar.getnames()
        pkg_info_name = next(n for n in names if n.endswith("PKG-INFO"))
        raw = tar.extractfile(pkg_info_name).read().decode("utf-8")

    # PKG-INFO e um documento RFC 822; campos podem repetir (License-File).
    message = Parser().parsestr(raw)
    fields: dict[str, list[str]] = {}
    # message.items() preserva repeticoes (License-File aparece N vezes); um
    # dict(message) colapsaria para a ultima ocorrencia e o teste de
    # empacotamento das licencas passaria por acidente.
    for key, value in message.items():
        fields.setdefault(key, []).append(value)
    return fields, names


class TestLicenseMetadata:
    def test_uses_pep639_license_expression(self, sdist_metadata):
        fields, _ = sdist_metadata
        assert fields.get("License-Expression") == [
            "AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception"
        ]

    def test_no_obsolete_license_field(self, sdist_metadata):
        """`License:` legado convive com o novo campo e faz o PyPI mostrar vazio."""
        fields, _ = sdist_metadata
        assert "License" not in fields, (
            "campo obsoleto `License:` presente — sintaxe legada no pyproject; "
            "`twine check` NAO detecta isso"
        )

    def test_both_license_files_declared(self, sdist_metadata):
        fields, _ = sdist_metadata
        declared = {Path(v).name for v in fields.get("License-File", [])}
        assert declared == {"LICENSE", "LICENSE.exception"}

    def test_both_license_files_packaged(self, sdist_metadata):
        """A excecao so vale se o arquivo dela viajar junto com o pacote."""
        _, names = sdist_metadata
        packaged = {Path(n).name for n in names if Path(n).name.startswith("LICENSE")}
        assert {"LICENSE", "LICENSE.exception"} <= packaged


class TestSdistContents:
    def test_no_secrets_or_local_artifacts(self, sdist_metadata):
        """config.toml/.env carregam segredos reais; artefatos locais nao devem viajar."""
        _, names = sdist_metadata
        offenders = [
            n
            for n in names
            if Path(n).name == "config.toml"
            or n.endswith((".db", ".env", ".vsix"))
        ]
        assert offenders == [], f"artefatos indevidos no sdist: {offenders}"

    def test_no_orphan_standalone_scripts(self, sdist_metadata):
        """Scripts avulsos na raiz (protótipos pre-empacotamento) nao devem viajar."""
        _, names = sdist_metadata
        orphans = {
            "interview_processor.py",
            "abstract_processor10.py",
            "semantic_memory_builder.py",
            "topic_processor.py",
        }
        packaged = {Path(n).name for n in names}
        leaked = orphans & packaged
        assert not leaked, f"scripts órfãos vazaram para o sdist: {leaked}"


class TestVersionConsistency:
    def test_sdist_version_matches_pyproject(self, sdist_metadata):
        fields, _ = sdist_metadata
        assert fields.get("Version") == [_declared_version()]

    def test_citation_version_matches_pyproject(self):
        """CITATION.cff defasado ja aconteceu duas vezes no ecossistema."""
        import yaml

        citation = yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"))
        assert citation["version"] == _declared_version()

    def test_changelog_documents_current_version(self):
        """Publicar uma versao ausente do CHANGELOG deixa o historico mentindo."""
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{_declared_version()}]" in changelog
