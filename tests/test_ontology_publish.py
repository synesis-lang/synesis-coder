"""Etapa A0a: mesclagem por bloco num .syno local, validação do conjunto e
conferência das entradas antes de gravar (Spec_ROLE_EXPORT_SKOS v7, §11 e §14).

O modelo é simulado: o que se testa é o caminho de gravação real, com o
compilador real, sobre um projeto em disco.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from synesis_coder.modes import ontology_mode
from synesis_coder.ontology_publish import (
    Destination,
    DestinationError,
    InputSnapshot,
    resolve_destination,
    validate_changes,
)

TEMPLATE = """\
ITEM FIELDS
    REQUIRED trecho, codigo
END ITEM FIELDS

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD

FIELD codigo TYPE CODE
    SCOPE ITEM
END FIELD

ONTOLOGY FIELDS
    REQUIRED definicao
    OPTIONAL tipo_de, exemplos
END ONTOLOGY FIELDS

FIELD definicao TYPE TEXT
    SCOPE ONTOLOGY
END FIELD

FIELD tipo_de TYPE CODE
    SCOPE ONTOLOGY
END FIELD

FIELD exemplos TYPE TEXT
    SCOPE ONTOLOGY
END FIELD
"""

BIB = "@article{a1, title={T}, author={A}, year={2024}}\n"

ANNOTATIONS = """\
SOURCE @a1
END SOURCE

ITEM @a1
    trecho: Mensagens com link falso.
    codigo: smishing, vishing, phishing
END ITEM
"""

FILHOS = """\
# Tipos de golpe — curado à mão
ONTOLOGY smishing
    # nota: revisado pela equipe
    definicao: Antiga.
    tipo_de: phishing
    exemplos:
        Mensagem com link.

        Mensagem com cobrança.
END ONTOLOGY

ONTOLOGY vishing
    exemplos: Ligação falsa.
END ONTOLOGY
"""

PAIS = """\
ONTOLOGY phishing
    definicao: Fraude por comunicação enganosa.
END ONTOLOGY
"""


def _project(root: Path, *, shared_pais: bool = False, two_local: bool = True) -> Path:
    (root / "t.synt").write_text(TEMPLATE, encoding="utf-8")
    (root / "r.bib").write_text(BIB, encoding="utf-8")
    (root / "a.syn").write_text(ANNOTATIONS, encoding="utf-8")
    (root / "filhos.syno").write_text(FILHOS, encoding="utf-8")
    includes = ['INCLUDE ONTOLOGY "filhos.syno"']
    if two_local:
        (root / "pais.syno").write_text(PAIS, encoding="utf-8")
        includes.append('INCLUDE ONTOLOGY "pais.syno"')
    if shared_pais:
        outside = root.parent / f"{root.name}_shared"
        outside.mkdir(exist_ok=True)
        (outside / "comum.syno").write_text(PAIS, encoding="utf-8")
        includes.append(f'INCLUDE SHARED ONTOLOGY "../{root.name}_shared/comum.syno"')
    synp = root / "p.synp"
    synp.write_text(
        'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
        'INCLUDE ANNOTATIONS "a.syn"\n' + "\n".join(includes) + "\nEND PROJECT\n",
        encoding="utf-8",
    )
    return synp


def _run(synp: Path, output: Path | None, *, update: bool, generated=None, before_write=None):
    """Roda o modo com o modelo simulado. `before_write` simula edição concorrente."""
    generated = generated or (lambda code: f"ONTOLOGY {code}\n    definicao: Gerada para {code}.\nEND ONTOLOGY")

    async def fake_generate(code, ctx, semantic_ctx, llm_client):
        if before_write is not None:
            before_write()
        return generated(code)

    client = MagicMock()
    client.usage.summary_line.return_value = ""
    # Correção pelo modelo: devolve o texto como veio (a falha persiste).
    client.fix_async = AsyncMock(side_effect=lambda text, *a, **k: text)
    with patch.object(ontology_mode, "_generate_ontology_syno", fake_generate), \
         patch.object(ontology_mode, "LLMClient", return_value=client), \
         patch.object(ontology_mode, "runtime_banner", MagicMock()), \
         patch.object(ontology_mode, "warn_schema_fallbacks", MagicMock()), \
         patch.object(ontology_mode, "campaign_summary", MagicMock(return_value="ok")):
        return asyncio.run(ontology_mode._process_ontology_async(
            project_path=synp, output_path=output, update=update, concurrent=1,
            model=None, format="plain", overwrite=True, backup=False,
        ))


# ------------------------------------------------------------------ destino

class TestDestination:
    def test_shared_destination_is_refused(self, tmp_path):
        synp = _project(tmp_path, shared_pais=True, two_local=False)
        shared = (tmp_path.parent / f"{tmp_path.name}_shared" / "comum.syno").resolve()
        with pytest.raises(DestinationError, match="SHARED"):
            resolve_destination(synp, shared)

    def test_two_local_files_need_an_explicit_destination(self, tmp_path):
        synp = _project(tmp_path)
        with pytest.raises(DestinationError, match="--output"):
            resolve_destination(synp, None)

    def test_single_local_file_is_chosen(self, tmp_path):
        synp = _project(tmp_path, two_local=False)
        dest = resolve_destination(synp, None)
        assert dest.path == (tmp_path / "filhos.syno").resolve()
        assert dest.in_project

    def test_file_outside_the_project_is_standalone(self, tmp_path):
        synp = _project(tmp_path)
        dest = resolve_destination(synp, tmp_path / "nova.syno")
        assert not dest.in_project


# ---------------------------------------------------------------- mesclagem

class TestSetValidationIntegrity:
    """R1 (avaliação de 2026-11-17): erros novos do projeto inteiro, por identidade."""

    T = ("ONTOLOGY FIELDS\n    REQUIRED definicao\n    OPTIONAL tipo_de\nEND ONTOLOGY FIELDS\n"
         "FIELD definicao TYPE TEXT\n    SCOPE ONTOLOGY\n    ROLE NOTE DEFINITION\nEND FIELD\n"
         "FIELD tipo_de TYPE CODE\n    SCOPE ONTOLOGY\n    ROLE HIERARCHY KIND\nEND FIELD\n")
    P = 'PROJECT p\nTEMPLATE "t.synt"\nINCLUDE ONTOLOGY "o.syno"\nEND PROJECT\n'
    OLD = ("ONTOLOGY a\n    definicao: A\n    tipo_de: b\nEND ONTOLOGY\n"
           "ONTOLOGY b\n    definicao: B\nEND ONTOLOGY\n")

    def _run(self, tmp_path, old, new):
        import synesis
        ctx = dict(project_content=self.P, template_content=self.T)
        dest = Destination(tmp_path / "o.syno", "o.syno", True, {})
        final, rejected = validate_changes(ctx, dest, old, lambda keys: new if "b" in keys else old, {"b"})
        codes = [e.CODE for e in synesis.load(project_content=self.P, template_content=self.T,
                                              ontology_contents={"o.syno": final}).validation_result.errors]
        return final, rejected, codes

    def test_cycle_located_in_an_unchanged_block_is_rejected(self, tmp_path):
        new = self.OLD.replace("definicao: B", "definicao: B\n    tipo_de: a")
        final, rejected, codes = self._run(tmp_path, self.OLD, new)
        assert "b" in rejected and final == self.OLD and codes == []

    def test_swapping_one_error_for_another_is_rejected(self, tmp_path):
        old = self.OLD.replace("definicao: B", "definicao: B\n    tipo_de: missing")
        new = self.OLD.replace("definicao: B", "definicao: B\n    tipo_de: b")
        final, rejected, codes = self._run(tmp_path, old, new)
        assert "b" in rejected and final == old and codes == ["SYNESIS_E101"]

    def test_preexisting_error_does_not_block_a_clean_change(self, tmp_path):
        old = self.OLD.replace("definicao: A", "definicao: A\n    tipo_de: missing")
        new = old.replace("definicao: B", "definicao: B revista")
        final, rejected, codes = self._run(tmp_path, old, new)
        assert rejected == {} and final == new and codes == ["SYNESIS_E101"]


class TestMergeIntoProjectFile:
    def test_only_generated_fields_change(self, tmp_path):
        synp = _project(tmp_path)
        _run(synp, tmp_path / "filhos.syno", update=False)
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        assert text.startswith("# Tipos de golpe — curado à mão\n")
        assert "    # nota: revisado pela equipe\n    definicao: Gerada para smishing." in text
        # CODE curado e campo de várias linhas preservados
        assert "    tipo_de: phishing\n" in text
        assert "        Mensagem com link.\n\n        Mensagem com cobrança.\n" in text
        # o outro arquivo do projeto não é tocado
        assert (tmp_path / "pais.syno").read_text(encoding="utf-8") == PAIS

    def test_concept_in_another_file_is_not_duplicated(self, tmp_path):
        synp = _project(tmp_path)
        _run(synp, tmp_path / "filhos.syno", update=False)
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        assert "ONTOLOGY phishing" not in text

    def test_update_fills_only_the_missing_required_field(self, tmp_path):
        synp = _project(tmp_path)
        _run(synp, tmp_path / "filhos.syno", update=True)
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        # smishing já tinha definicao: intocado
        assert "    definicao: Antiga.\n" in text
        # vishing não tinha: recebe só a definicao, e mantém exemplos
        assert "ONTOLOGY vishing\n    exemplos: Ligação falsa.\n    definicao: Gerada para vishing.\nEND ONTOLOGY" in text

    def test_block_that_breaks_the_project_is_rejected_alone(self, tmp_path):
        synp = _project(tmp_path)

        def generated(code):
            if code == "vishing":  # campo fora do template
                return "ONTOLOGY vishing\n    definicao: x\n    inexistente: y\nEND ONTOLOGY"
            return f"ONTOLOGY {code}\n    definicao: Gerada para {code}.\nEND ONTOLOGY"

        _run(synp, tmp_path / "filhos.syno", update=False, generated=generated)
        text = (tmp_path / "filhos.syno").read_text(encoding="utf-8")
        assert "definicao: Gerada para smishing." in text
        assert "inexistente" not in text

    def test_concurrent_edit_of_destination_aborts(self, tmp_path):
        synp = _project(tmp_path)
        dest = tmp_path / "filhos.syno"

        def edit():
            dest.write_text(FILHOS + "\n# editado durante a execução\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="changed during the run"):
            _run(synp, dest, update=False, before_write=edit)
        assert dest.read_text(encoding="utf-8").endswith("# editado durante a execução\n")


class TestSnapshot:
    def test_detects_change_in_another_input(self, tmp_path):
        synp = _project(tmp_path)
        dest = resolve_destination(synp, tmp_path / "filhos.syno")
        snap = InputSnapshot.take(synp, dest)
        (tmp_path / "pais.syno").write_text(PAIS + "\n", encoding="utf-8")
        dest_changed, others = snap.changed()
        assert not dest_changed
        assert (tmp_path / "pais.syno").resolve() in others


class TestLineEndings:
    """A gravacao devolve os bytes como compostos: LF fica LF e CRLF fica CRLF."""

    def _bytes_after(self, tmp_path, crlf: bool) -> bytes:
        synp = _project(tmp_path)
        dest = tmp_path / "filhos.syno"
        # Bytes explicitos: write_text traduziria \n para CRLF no Windows.
        text = FILHOS.replace("\n", "\r\n") if crlf else FILHOS
        dest.write_bytes(text.encode("utf-8"))
        _run(synp, dest, update=True)
        return dest.read_bytes()

    def test_lf_file_stays_lf(self, tmp_path):
        data = self._bytes_after(tmp_path, crlf=False)
        assert b"\r" not in data
        assert b"definicao: Gerada para vishing." in data

    def test_crlf_file_stays_crlf_without_doubling(self, tmp_path):
        data = self._bytes_after(tmp_path, crlf=True)
        assert b"\r\r\n" not in data
        assert data.count(b"\n") == data.count(b"\r\n")
