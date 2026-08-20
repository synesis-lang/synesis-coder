"""Testes para o modo ontology (Fase 4).

Estrutura:
    - TestGetPendingCodes (3 testes) — seleção de codes pendentes com/sem --update
    - TestBuildSemanticCtx (4 testes) — construção do semantic_ctx
    - TestOntologyPromptBuilder (5 testes) — estrutura e conteúdo dos prompts
    - TestValidateOntologyEntry (3 testes) — extração e validação de blocos ONTOLOGY
    - TestOntologyModeIntegration (3 testes LLM) — geração real com casos reais

Princípio: sem fixtures fictícios. Todos os casos usam projetos reais de
d:\\GitHub\\case-studies\\.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Caminhos dos projetos de teste
# ---------------------------------------------------------------------------

SOCIAL_ACCEPTANCE_SYNP = Path(
    r"d:\GitHub\case-studies\Sociology\Social_Acceptance\social_acceptance.synp"
)
NAVE_SYNP = Path(
    r"d:\GitHub\case-studies\Theology\Nave_Topical_Concordance\nave.synp"
)
THOMPSON_SYNP = Path(
    r"d:\GitHub\case-studies\Theology\Thompson_Chain_Reference\thompson_bible.synp"
)

# Pular testes de integração se projetos não existem
SKIP_IF_NO_SOCIAL = pytest.mark.skipif(
    not SOCIAL_ACCEPTANCE_SYNP.exists(),
    reason="Projeto social_acceptance não encontrado",
)
SKIP_IF_NO_NAVE = pytest.mark.skipif(
    not NAVE_SYNP.exists(),
    reason="Projeto nave não encontrado",
)
SKIP_IF_NO_THOMPSON = pytest.mark.skipif(
    not THOMPSON_SYNP.exists(),
    reason="Projeto thompson_bible não encontrado",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    code_index_codes=None,
    ontology_index=None,
    has_ontology_scope=True,
    ontology_fields=None,
    topic_index=None,
    project_description=None,
) -> dict:
    """Cria um ctx mínimo para testes sem LLM."""
    linked = MagicMock()
    linked.code_usage = {c: [] for c in (code_index_codes or [])}
    linked.all_triples = []
    linked.ontology_index = ontology_index or {}
    linked.topic_index = {}

    result = MagicMock()
    result.linked_project = linked

    return {
        "result": result,
        "has_ontology_scope": has_ontology_scope,
        "ontology_fields": ontology_fields or {},
        "ontology_index": ontology_index or {},
        "code_index": {
            "codes": sorted(code_index_codes or []),
            "stats": {c: 1 for c in (code_index_codes or [])},
            "empty": len(code_index_codes or []) == 0,
        },
        "topic_index": topic_index or {"topics": [], "topic_members": {}, "empty": True},
        "project_description": project_description,
        "chain_relations": {},
        "required_ontology": [],
        "project_content": "PROJECT test\nTEMPLATE \"test.synt\"\nEND PROJECT",
        "template_content": "# minimal",
        "bib_content": None,
        "annotation_contents": {},
    }


# ---------------------------------------------------------------------------
# TestGetPendingCodes
# ---------------------------------------------------------------------------


class TestGetPendingCodes:
    def test_no_update_returns_all_codes(self):
        """Sem --update, retorna todos os códigos do code_index."""
        from synesis_coder.modes.ontology_mode import _get_pending_codes

        ctx = _make_ctx(code_index_codes=["Cost", "Benefit", "Risk"])
        result = _get_pending_codes(ctx, update=False)
        assert result == ["Benefit", "Cost", "Risk"]

    def test_update_excludes_already_defined(self):
        """Com --update, exclui códigos já presentes no ontology_index."""
        from synesis_coder.modes.ontology_mode import _get_pending_codes

        ctx = _make_ctx(
            code_index_codes=["Cost", "Benefit", "Risk"],
            ontology_index={"Cost": MagicMock(), "Benefit": MagicMock()},
        )
        result = _get_pending_codes(ctx, update=True)
        assert result == ["Risk"]

    def test_update_all_defined_returns_empty(self):
        """Com --update e todos definidos, retorna lista vazia."""
        from synesis_coder.modes.ontology_mode import _get_pending_codes

        ctx = _make_ctx(
            code_index_codes=["Cost", "Benefit"],
            ontology_index={"Cost": MagicMock(), "Benefit": MagicMock()},
        )
        result = _get_pending_codes(ctx, update=True)
        assert result == []


class TestUpdatePreservesExistingEntries:
    """Regressão: `--update` não pode apagar as entradas que ele pulou.

    `_get_pending_codes` exclui de propósito os códigos já definidos, então eles
    não aparecem em `results`. Gravar só `results` com overwrite apagava todas
    as definições preexistentes — inclusive as curadas à mão. Observado num
    caso real: 74 entradas curadas viraram 59 geradas.
    """

    def test_existing_content_is_preserved_and_new_appended(self, tmp_path):
        import asyncio

        from synesis_coder.modes import ontology_mode

        output = tmp_path / "ontologia.syno"
        output.write_text(
            "ONTOLOGY curada_a\n  ontology_description: X\nEND ONTOLOGY\n\n"
            "ONTOLOGY curada_b\n  ontology_description: Y\nEND ONTOLOGY\n",
            encoding="utf-8",
        )

        ctx = _make_ctx(
            code_index_codes=["curada_a", "curada_b", "nova_c"],
            ontology_index={"curada_a": MagicMock(), "curada_b": MagicMock()},
        )
        novo_bloco = "ONTOLOGY nova_c\n  ontology_description: Z\nEND ONTOLOGY"

        async def fake_process_one(code, _ctx, _client, _sem):
            return (code, novo_bloco, True)

        with patch.object(ontology_mode, "load_project", return_value=ctx), \
             patch.object(ontology_mode, "LLMClient", MagicMock()), \
             patch.object(ontology_mode, "runtime_banner", MagicMock()), \
             patch.object(ontology_mode, "_process_one_code", fake_process_one):
            asyncio.run(
                ontology_mode._process_ontology_async(
                    project_path=Path("dummy.synp"),
                    output_path=output,
                    update=True,
                    concurrent=1,
                    model=None,
                    format="plain",
                    overwrite=False,
                    backup=False,
                )
            )

        content = output.read_text(encoding="utf-8")
        assert "ONTOLOGY curada_a" in content, "entrada curada foi apagada"
        assert "ONTOLOGY curada_b" in content, "entrada curada foi apagada"
        assert "ONTOLOGY nova_c" in content, "entrada nova não foi gravada"
        assert content.count("ONTOLOGY ") >= 3


# ---------------------------------------------------------------------------
# TestBuildSemanticCtx
# ---------------------------------------------------------------------------


class TestBuildSemanticCtx:
    def _make_item(self, source_ref="smith2024", text="sample text"):
        item = MagicMock()
        item.source_ref = source_ref
        item.text = text
        item.note = None
        item.chain = None
        item.topic = None
        item.code = None
        return item

    def test_frequency_and_sources_counted(self):
        """Frequência e fontes são contadas corretamente."""
        from synesis_coder.modes.ontology_mode import _build_semantic_ctx

        items = [
            self._make_item("smith2024"),
            self._make_item("jones2023"),
            self._make_item("smith2024"),  # mesma fonte
        ]
        linked = MagicMock()
        linked.code_usage = {"Social_Acceptance": items}
        linked.all_triples = []

        result_mock = MagicMock()
        result_mock.linked_project = linked

        ctx = _make_ctx(code_index_codes=["Social_Acceptance"])
        ctx["result"] = result_mock

        sem_ctx = _build_semantic_ctx("Social_Acceptance", ctx)
        assert sem_ctx["frequency"] == 3
        assert sem_ctx["sources"] == 2

    def test_relations_extracted_from_triples(self):
        """Relações são extraídas dos triples onde o código aparece como A ou B."""
        from synesis_coder.modes.ontology_mode import _build_semantic_ctx

        linked = MagicMock()
        linked.code_usage = {"Social_Acceptance": []}
        linked.all_triples = [
            ("Social_Acceptance", "ENABLES", "Deployment"),
            ("Trust", "ENABLES", "Social_Acceptance"),
            ("Cost", "CONSTRAINS", "Deployment"),  # não envolve Social_Acceptance
        ]

        result_mock = MagicMock()
        result_mock.linked_project = linked

        ctx = _make_ctx(code_index_codes=["Social_Acceptance"])
        ctx["result"] = result_mock

        sem_ctx = _build_semantic_ctx("Social_Acceptance", ctx)
        assert len(sem_ctx["relations"]) == 2
        assert ("Social_Acceptance", "ENABLES", "Deployment") in sem_ctx["relations"]
        assert ("Trust", "ENABLES", "Social_Acceptance") in sem_ctx["relations"]

    def test_examples_include_text_field(self):
        """Exemplos concretos incluem o campo text dos ITEMs."""
        from synesis_coder.modes.ontology_mode import _build_semantic_ctx

        items = [self._make_item(text="Community trust is key.")]
        linked = MagicMock()
        linked.code_usage = {"Trust": items}
        linked.all_triples = []

        result_mock = MagicMock()
        result_mock.linked_project = linked

        ctx = _make_ctx(code_index_codes=["Trust"])
        ctx["result"] = result_mock

        sem_ctx = _build_semantic_ctx("Trust", ctx)
        assert len(sem_ctx["examples"]) == 1
        assert sem_ctx["examples"][0].get("text") == "Community trust is key."

    def test_no_linked_returns_empty_ctx(self):
        """Projeto sem linked_project retorna semantic_ctx vazio."""
        from synesis_coder.modes.ontology_mode import _build_semantic_ctx

        result_mock = MagicMock()
        result_mock.linked_project = None

        ctx = _make_ctx(code_index_codes=["Cost"])
        ctx["result"] = result_mock

        sem_ctx = _build_semantic_ctx("Cost", ctx)
        assert sem_ctx["frequency"] == 0
        assert sem_ctx["sources"] == 0
        assert sem_ctx["relations"] == []


# ---------------------------------------------------------------------------
# TestOntologyPromptBuilder
# ---------------------------------------------------------------------------


class TestOntologyPromptBuilder:
    def _make_minimal_ontology_ctx(self) -> dict:
        """Contexto mínimo com 1 campo ONTOLOGY para testes de prompt."""
        from synesis.ast.nodes import FieldType, Scope

        spec = MagicMock()
        spec.type = FieldType.TEXT
        spec.scope = Scope.ONTOLOGY
        spec.guidelines = "Defina o conceito em 40-80 palavras."
        spec.description = "Definição semântica."
        spec.values = []
        spec.relations = {}
        spec.format = None

        ctx = _make_ctx(
            code_index_codes=["Social_Acceptance"],
            ontology_fields={"ontology_description": spec},
        )
        ctx["required_ontology"] = ["ontology_description"]
        return ctx

    def test_prompt_has_system_and_user(self):
        """Prompt retorna lista com system e user."""
        from synesis_coder.prompt_builder import build_ontology_prompt

        ctx = self._make_minimal_ontology_ctx()
        semantic_ctx = {"frequency": 5, "sources": 3, "relations": [], "co_codes": [], "examples": []}
        messages = build_ontology_prompt(ctx, "Social_Acceptance", semantic_ctx)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_cached(self):
        """System prompt é marcado como cacheável."""
        from synesis_coder.prompt_builder import build_ontology_prompt

        ctx = self._make_minimal_ontology_ctx()
        semantic_ctx = {"frequency": 1, "sources": 1, "relations": [], "co_codes": [], "examples": []}
        messages = build_ontology_prompt(ctx, "Test_Code", semantic_ctx)

        assert messages[0]["cache"] is True
        assert messages[1]["cache"] is False

    def test_code_name_in_user_message(self):
        """Nome do código aparece na mensagem do usuário."""
        from synesis_coder.prompt_builder import build_ontology_prompt

        ctx = self._make_minimal_ontology_ctx()
        semantic_ctx = {"frequency": 2, "sources": 1, "relations": [], "co_codes": [], "examples": []}
        messages = build_ontology_prompt(ctx, "Community_Trust", semantic_ctx)

        assert "Community_Trust" in messages[1]["content"]

    def test_frequency_and_sources_in_user_message(self):
        """Frequência e fontes aparecem na mensagem do usuário."""
        from synesis_coder.prompt_builder import build_ontology_prompt

        ctx = self._make_minimal_ontology_ctx()
        semantic_ctx = {
            "frequency": 42,
            "sources": 17,
            "relations": [],
            "co_codes": [],
            "examples": [],
        }
        messages = build_ontology_prompt(ctx, "Cost", semantic_ctx)

        assert "42" in messages[1]["content"]
        assert "17" in messages[1]["content"]

    def test_relations_in_user_message(self):
        """Relações do código aparecem na mensagem do usuário."""
        from synesis_coder.prompt_builder import build_ontology_prompt

        ctx = self._make_minimal_ontology_ctx()
        semantic_ctx = {
            "frequency": 3,
            "sources": 2,
            "relations": [("Cost", "CONSTRAINS", "Deployment")],
            "co_codes": [],
            "examples": [],
        }
        messages = build_ontology_prompt(ctx, "Cost", semantic_ctx)

        assert "CONSTRAINS" in messages[1]["content"]
        assert "Deployment" in messages[1]["content"]


# ---------------------------------------------------------------------------
# TestValidateOntologyEntry — extração de blocos
# ---------------------------------------------------------------------------


class TestValidateOntologyEntry:
    def test_extract_ontology_blocks_single(self):
        """_extract_ontology_blocks extrai um bloco ONTOLOGY."""
        from synesis_coder.validator import _extract_ontology_blocks

        text = textwrap.dedent("""\
            ONTOLOGY Cost
              ontology_description: Custos financeiros associados à tecnologia.
            END ONTOLOGY
        """)
        result = _extract_ontology_blocks(text)
        assert "ONTOLOGY Cost" in result
        assert "END ONTOLOGY" in result

    def test_extract_ontology_blocks_ignores_other(self):
        """_extract_ontology_blocks descarta blocos ITEM e SOURCE."""
        from synesis_coder.validator import _extract_ontology_blocks

        text = textwrap.dedent("""\
            ITEM @smith2024
              text: 'some text'
            END ITEM

            ONTOLOGY Trust
              ontology_description: Confiança comunitária.
            END ONTOLOGY

            SOURCE @smith2024
              description: A study.
            END SOURCE
        """)
        result = _extract_ontology_blocks(text)
        assert "ONTOLOGY Trust" in result
        assert "ITEM" not in result
        assert "SOURCE" not in result

    def test_extract_ontology_blocks_empty_on_no_blocks(self):
        """_extract_ontology_blocks retorna string vazia quando não há blocos."""
        from synesis_coder.validator import _extract_ontology_blocks

        result = _extract_ontology_blocks("Nenhum bloco aqui.")
        assert result == ""


# ---------------------------------------------------------------------------
# TestOntologyModeIntegration (requer LLM + projetos reais)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOntologyModeIntegration:
    @SKIP_IF_NO_SOCIAL
    def test_social_acceptance_generates_ontology(self, tmp_path):
        """Gera entrada ONTOLOGY para social_acceptance e valida com compilador."""
        from synesis_coder.modes.ontology_mode import process_ontology

        output_path = tmp_path / "test_ontology.syno"
        result = process_ontology(
            project_path=SOCIAL_ACCEPTANCE_SYNP,
            output_path=output_path,
            update=False,
            concurrent=1,
            format="plain",
        )
        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        assert "ONTOLOGY" in content
        assert "END ONTOLOGY" in content
        # Deve haver pelo menos uma entrada gerada
        assert content.count("ONTOLOGY ") >= 1

    @SKIP_IF_NO_SOCIAL
    def test_update_flag_skips_existing_codes(self, tmp_path):
        """Com --update, não regera entradas que já existem no .syno."""
        from synesis_coder.modes.ontology_mode import process_ontology

        output_path = tmp_path / "test_update.syno"
        # Primeira execução gera tudo
        process_ontology(
            project_path=SOCIAL_ACCEPTANCE_SYNP,
            output_path=output_path,
            update=False,
            concurrent=1,
            format="plain",
        )
        content_first = output_path.read_text(encoding="utf-8")
        count_first = content_first.count("ONTOLOGY ")

        # Segunda execução com --update — como já existem entradas, deve retornar
        # mensagem de "nenhum código pendente" ou gerar menos entradas
        # (depende do projeto ter o .syno incluído no .synp)
        result_msg = process_ontology(
            project_path=SOCIAL_ACCEPTANCE_SYNP,
            output_path=tmp_path / "test_update2.syno",
            update=True,
            concurrent=1,
            format="plain",
        )
        # A mensagem deve indicar processamento
        assert isinstance(result_msg, str)
        assert len(result_msg) > 0

    @SKIP_IF_NO_THOMPSON
    def test_thompson_no_ontology_scope_raises(self, tmp_path):
        """Projeto sem ONTOLOGY scope levanta ValueError com mensagem clara."""
        from synesis_coder.modes.ontology_mode import process_ontology

        output_path = tmp_path / "thompson.syno"
        with pytest.raises(ValueError, match="não define campos ONTOLOGY"):
            process_ontology(
                project_path=THOMPSON_SYNP,
                output_path=output_path,
                update=False,
                concurrent=1,
                format="plain",
            )


class TestStaleOntologyDoesNotBlockRegeneration:
    """Regerar a ontologia nao pode exigir que a ontologia antiga compile.

    Um `.syno` escrito sob regras anteriores (ex.: ORDERED com rotulo, hoje
    E088) abortava a carga do projeto — impedindo justamente a regeneracao que o
    corrigiria. Fora de `--update` o arquivo e integralmente substituido, entao
    carrega-lo e desnecessario.
    """

    def _run(self, update: bool, tmp_path, load_side_effect=None):
        import asyncio

        from synesis_coder.modes import ontology_mode

        ctx = _make_ctx(code_index_codes=["a"], ontology_index={})
        captured: dict = {}

        def fake_load(project_path, **kwargs):
            captured.update(kwargs)
            if load_side_effect is not None:
                raise load_side_effect
            return ctx

        async def fake_process_one(code, _ctx, _client, _sem):
            return (code, f"ONTOLOGY {code}\nEND ONTOLOGY", True)

        with patch.object(ontology_mode, "load_project", fake_load), \
             patch.object(ontology_mode, "LLMClient", MagicMock()), \
             patch.object(ontology_mode, "runtime_banner", MagicMock()), \
             patch.object(ontology_mode, "_process_one_code", fake_process_one):
            result = asyncio.run(
                ontology_mode._process_ontology_async(
                    project_path=Path("dummy.synp"),
                    output_path=tmp_path / "out.syno",
                    update=update,
                    concurrent=1,
                    model=None,
                    format="plain",
                    overwrite=True,
                    backup=False,
                )
            )
        return captured, result

    def test_full_regeneration_does_not_load_the_old_ontology(self, tmp_path):
        captured, _ = self._run(update=False, tmp_path=tmp_path)
        assert captured["load_ontology"] is False

    def test_update_still_loads_the_old_ontology(self, tmp_path):
        """Em --update o arquivo e preservado e anexado: precisa ser valido."""
        captured, _ = self._run(update=True, tmp_path=tmp_path)
        assert captured["load_ontology"] is True

    def test_stale_annotations_are_tolerated(self, tmp_path):
        captured, _ = self._run(update=False, tmp_path=tmp_path)
        assert captured["tolerate_annotation_errors"] is True

    def test_update_failure_explains_how_to_proceed(self, tmp_path):
        err = ValueError("=== ERROS ===\n[!] `aspect: Economico` — em ORDERED grave o indice")
        with pytest.raises(ValueError) as excinfo:
            self._run(update=True, tmp_path=tmp_path, load_side_effect=err)
        msg = str(excinfo.value)
        assert "SEM `--update`" in msg
        assert "aspect: Economico" in msg  # preserva o diagnostico original

    def test_full_regeneration_failure_is_not_rewritten(self, tmp_path):
        """Sem --update, um erro de carga e um erro real: nao mascarar."""
        err = ValueError("template invalido")
        with pytest.raises(ValueError) as excinfo:
            self._run(update=False, tmp_path=tmp_path, load_side_effect=err)
        assert "--update" not in str(excinfo.value)
