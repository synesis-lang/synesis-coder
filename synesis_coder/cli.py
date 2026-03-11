"""Interface de linha de comando do synesis-coder.

Comandos disponíveis:
    item      — gera um bloco ITEM a partir de texto e bibref (Fase 1)
    abstract  — processa corpus .bib em lote (Fase 2, não implementado)
    document  — processa documento longo com chunking (Fase 3, não implementado)
    ontology  — gera definições de ontologia (Fase 4, não implementado)
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from importlib.metadata import version as _pkg_version


def _version_string() -> str:
    try:
        return _pkg_version("synesis-coder")
    except Exception:
        return "0.0.1"


@click.group()
@click.version_option(version=_version_string(), prog_name="synesis-coder")
def main() -> None:
    """synesis-coder — Codificação qualitativa guiada por template Synesis.

    Gera anotações Synesis válidas usando Claude como motor de inferência.
    O template do projeto define todos os campos, relações e restrições —
    nada é hardcoded nesta ferramenta.

    \b
    Exemplos de uso:

    \b
      # Codificar um trecho de texto (projeto social_acceptance):
      synesis-coder item \\
        --project projeto.synp \\
        --bibref smith2024 \\
        --text "Community trust is the most important factor."

    \b
      # Saída verbose com status de validação:
      synesis-coder item \\
        --project projeto.synp \\
        --bibref smith2024 \\
        --text "Local ownership reduces opposition." \\
        --format verbose

    \b
      # Redirecionar saída para arquivo .syn:
      synesis-coder item \\
        --project projeto.synp \\
        --bibref smith2024 \\
        --text "..." >> anotacoes/smith2024.syn

    \b
      # Usar modelo alternativo:
      synesis-coder item \\
        --project projeto.synp \\
        --bibref smith2024 \\
        --text "..." \\
        --model claude-sonnet-4-6
    """


@main.command()
@click.option(
    "--project",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Caminho para o arquivo .synp do projeto.",
)
@click.option(
    "--bibref",
    required=True,
    help="Referência bibliográfica do item (ex: smith2024).",
)
@click.option(
    "--text",
    required=True,
    help="Texto a ser codificado.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["plain", "verbose"]),
    default="plain",
    show_default=True,
    help="Formato de saída: plain (apenas Synesis) ou verbose (com log).",
)
@click.option(
    "--model",
    default=None,
    help="ID do modelo LLM (sobrescreve SYNESIS_CODER_MODEL do .env).",
)
def item(
    project: Path,
    bibref: str,
    text: str,
    output_format: str,
    model: str | None,
) -> None:
    """Gera um bloco ITEM Synesis a partir de texto e referência bibliográfica."""
    from synesis_coder.modes.item_mode import process_item

    try:
        result = process_item(
            project_path=project,
            bibref=bibref,
            text=text,
            format=output_format,
            model=model,
        )
        click.echo(result)
    except FileNotFoundError as exc:
        click.echo(f"Erro: {exc}", err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"Erro de compilação: {exc}", err=True)
        sys.exit(1)
    except EnvironmentError as exc:
        click.echo(f"Erro de configuração: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Erro inesperado: {exc}", err=True)
        sys.exit(1)


@main.command()
def abstract() -> None:
    """[Fase 2] Processa corpus .bib em lote. Não implementado ainda."""
    click.echo("Modo 'abstract' será implementado na Fase 2.", err=True)
    sys.exit(1)


@main.command()
def document() -> None:
    """[Fase 3] Processa documento longo com chunking. Não implementado ainda."""
    click.echo("Modo 'document' será implementado na Fase 3.", err=True)
    sys.exit(1)


@main.command()
def ontology() -> None:
    """[Fase 4] Gera definições de ontologia. Não implementado ainda."""
    click.echo("Modo 'ontology' será implementado na Fase 4.", err=True)
    sys.exit(1)
