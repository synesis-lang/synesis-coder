"""Modo document: codificação de documento longo com chunking e deduplicação.

Processa arquivos .txt ou .md que representam documentos longos (entrevistas,
artigos, capítulos). Divide o texto em chunks com overlap, processa cada chunk
de forma assíncrona e combina os resultados deduplicando ITEMs repetidos.

Fluxo:
    1. load_project() → ctx
    2. read_document() → texto completo
    3. split_into_chunks(texto, chunk_size, overlap) → chunks
    4. Para cada chunk (assíncrono, concorrente):
        a. build_document_prompt(ctx, bibref, chunk, i, n) → messages
        b. LLMClient.call_async(messages) → raw_syn
        c. validate_and_fix_async(raw_syn, ctx, llm_client) → (items_syn, ok)
    5. build_source_block(ctx, bibref) → source_syn  [chamada LLM separada]
    6. merge_and_dedup(all_items) → combined_items
    7. gravar source_syn + combined_items em output

Deduplicação (R1 do plano):
    - Por normalização de conceitos (lowercase, strip), nunca por similaridade
    - CHAINs são duplicatas somente se (A, REL, B) idêntico após normalização
    - ITEM inteiro: duplicata se todos os campos relevantes coincidem
    - Em caso de dúvida, preservar (melhor duplicata que perda)

Chunking:
    - Modo semântico (padrão para docs com ≥2 cabeçalhos Markdown ATX):
        agrupa seções por cabeçalho respeitando chunk_size como teto;
        seções grandes subdividem-se com o cabeçalho replicado como prefixo.
    - Modo size-based (fallback para texto corrido sem estrutura):
        divisão por parágrafos (\\n\\n), com fallback por frases.
    - chunk_size em caracteres (padrão: ~12000 chars ≈ 3000 tokens)
    - overlap em caracteres (padrão: ~2400 chars ≈ 600 tokens)
    - Chunks nunca cortam no meio de uma sentença (ambos os modos)
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import synesis

from synesis_coder.block_assembler import assemble_items, assemble_source
from synesis_coder.debug_log import DebugRecorder, now_human
from synesis_coder.llm_client import LLMClient
from synesis_coder.project_loader import assert_bibref_known, load_project
from synesis_coder.synr_io import safe_write_output
from synesis_coder.prompt_builder import (
    build_document_prompt,
    build_document_source_values_prompt,
    build_document_values_prompt,
)
from synesis_coder.runtime_info import runtime_banner
from synesis_coder.schema_builder import build_item_schema, build_source_schema
from synesis_coder.text_cleaner import clean_document
from synesis_coder.validator import (
    _extract_item_blocks,
    _has_structural_errors,
    _strip_markdown_fences,
    validate_and_fix_async,
)

logger = logging.getLogger(__name__)

# Tamanho padrão do chunk e overlap em caracteres
# ~12000 chars ≈ 3000 tokens (margem conservadora para documentos longos)
DEFAULT_CHUNK_SIZE = 12_000
DEFAULT_OVERLAP = 2_400


def _human_chars(n: int) -> str:
    """Formata contagem de caracteres de forma compacta: 94298 → '94k'."""
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


class _ChunkProgress:
    """Indicador de progresso para chunks processados em paralelo.

    Renderiza `[INFO] Processando: [████████····] 8/12 chunks (N falhas)`,
    reescrevendo a linha in-place quando a saída é um TTY. Em pipes/redireções
    ou com logging em nível DEBUG o indicador é suprimido.
    """

    _FILL = "█"
    _EMPTY = "·"
    _BAR_WIDTH = 12

    def __init__(self, total: int, stream=None) -> None:
        self.total = total
        self.stream = stream or sys.stderr
        self.done: dict[int, bool] = {}
        self.enabled = (
            self.total > 0
            and hasattr(self.stream, "isatty")
            and self.stream.isatty()
            and logger.isEnabledFor(logging.INFO)
            and not logger.isEnabledFor(logging.DEBUG)
        )

    def start(self) -> None:
        if not self.enabled:
            return
        self._render()

    def mark(self, idx: int, success: bool) -> None:
        if not self.enabled:
            return
        self.done[idx] = success
        self._render()

    def _render(self) -> None:
        n_done = len(self.done)
        n_fail = sum(1 for ok in self.done.values() if not ok)
        filled = round(n_done / self.total * self._BAR_WIDTH) if self.total else 0
        bar = self._FILL * filled + self._EMPTY * (self._BAR_WIDTH - filled)
        fail_str = f" ({n_fail} falhas)" if n_fail else ""
        line = f"[INFO] Processando: [{bar}] {n_done}/{self.total} chunks{fail_str}"
        self.stream.write(f"\r{line}")
        self.stream.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        self.stream.write("\n")
        self.stream.flush()

# Regex para cabeçalhos ATX Markdown (# … ######)
_ATX_HEADER = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)


# ---------------------------------------------------------------------------
# Leitura do documento
# ---------------------------------------------------------------------------


def read_document(input_path: Path) -> str:
    """Lê o documento de entrada (.txt ou .md).

    Args:
        input_path: Caminho para o arquivo.

    Returns:
        Conteúdo do arquivo como string.

    Raises:
        FileNotFoundError: Se o arquivo não existir.
        ValueError: Se o arquivo estiver vazio.
    """
    input_path = Path(input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Documento não encontrado: {input_path}")

    text = input_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Documento está vazio: {input_path}")

    return text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def split_into_chunks(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """Divide o texto em chunks com overlap, preservando fronteiras de parágrafo.

    Estratégia:
    1. Tenta dividir por parágrafos (\\n\\n)
    2. Se um parágrafo exceder chunk_size, divide por sentenças
    3. Overlap é adicionado no início de cada chunk (exceto o primeiro)

    Args:
        text: Texto completo do documento.
        chunk_size: Tamanho máximo de cada chunk em caracteres.
        overlap: Tamanho do overlap entre chunks consecutivos em caracteres.

    Returns:
        Lista de strings — cada string é um chunk do documento.
    """
    if len(text) <= chunk_size:
        return [text]

    # Modo semântico: respeita hierarquia de cabeçalhos Markdown
    if _has_markdown_structure(text):
        return _split_by_headers(text, chunk_size, overlap)

    # Modo size-based (fallback): dividir por parágrafos duplos
    paragraphs = re.split(r"\n\n+", text)

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)

        # Parágrafo maior que chunk_size: subdividir por sentenças
        if para_len > chunk_size:
            # Fechar chunk atual se houver conteúdo
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            # Subdividir por sentença
            sub_chunks = _split_by_sentences(para, chunk_size)
            chunks.extend(sub_chunks)
            continue

        # Adicionar parágrafo ao chunk atual ou iniciar novo
        if current_len + para_len + 2 > chunk_size and current:
            chunks.append("\n\n".join(current))
            # Carregar overlap: últimos parágrafos que cabem no overlap
            current, current_len = _build_overlap_prefix(current, overlap)

        current.append(para)
        current_len += para_len + 2  # +2 para "\n\n"

    if current:
        chunks.append("\n\n".join(current))

    return chunks if chunks else [text]


def _split_by_sentences(text: str, chunk_size: int) -> List[str]:
    """Subdivide um parágrafo longo em chunks por sentenças."""
    # Separador de sentenças: ., !, ? seguidos de espaço ou fim
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: List[str] = []
    current_parts: List[str] = []
    current_len = 0

    for sent in sentences:
        sent_len = len(sent)
        if current_len + sent_len + 1 > chunk_size and current_parts:
            chunks.append(" ".join(current_parts))
            current_parts = []
            current_len = 0
        current_parts.append(sent)
        current_len += sent_len + 1

    if current_parts:
        chunks.append(" ".join(current_parts))

    return chunks if chunks else [text]


def _build_overlap_prefix(
    current: List[str], overlap: int
) -> Tuple[List[str], int]:
    """Seleciona parágrafos do final do chunk atual para o overlap do próximo."""
    overlap_parts: List[str] = []
    overlap_len = 0

    for para in reversed(current):
        para_len = len(para)
        if overlap_len + para_len + 2 > overlap:
            break
        overlap_parts.insert(0, para)
        overlap_len += para_len + 2

    return overlap_parts, overlap_len


def _has_markdown_structure(text: str, min_headers: int = 2) -> bool:
    """True se o texto tem ≥ min_headers cabeçalhos ATX Markdown.

    Limiar de 2 evita tratar documento com título único (sem subdivisão real)
    como estruturado — esse caso é servido melhor pelo split por parágrafo.
    """
    return len(_ATX_HEADER.findall(text)) >= min_headers


def _parse_markdown_sections(text: str) -> List[Tuple[str, str]]:
    """Divide texto em (header_line, section_text) por cabeçalho ATX.

    O preâmbulo antes do primeiro cabeçalho vira seção com header_line vazia.
    Cada section_text inclui o próprio cabeçalho como primeira linha para
    preservar contexto quando a seção é usada como chunk isolado.

    Returns:
        Lista de (header_line, section_text).
    """
    sections: List[Tuple[str, str]] = []
    # Encontrar todas as posições de cabeçalhos ATX
    matches = list(_ATX_HEADER.finditer(text))

    # Preâmbulo antes do primeiro cabeçalho
    if matches and matches[0].start() > 0:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))
    elif not matches:
        # Sem cabeçalhos: texto inteiro como seção única com header vazio
        sections.append(("", text.strip()))
        return sections

    for i, m in enumerate(matches):
        header_line = m.group(0).rstrip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_body = text[start:end].strip()
        sections.append((header_line, section_body))

    return sections


def _split_by_headers(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Agrupa seções Markdown em chunks respeitando chunk_size como teto.

    - Seções pequenas consecutivas → empacotadas no mesmo chunk.
    - Seção isolada > chunk_size → corpo subdividido via _split_by_sentences;
      cada subchunk recebe o cabeçalho da seção como prefixo de contexto.
    - Overlap entre chunks consecutivos via _build_overlap_prefix (em parágrafos).
    """
    sections = _parse_markdown_sections(text)
    chunks: List[str] = []
    current_parts: List[str] = []  # partes do chunk em construção (parágrafos)
    current_len = 0

    def _flush(parts: List[str]) -> Tuple[List[str], int]:
        """Emite um chunk e retorna o prefixo de overlap para o próximo."""
        chunks.append("\n\n".join(parts))
        return _build_overlap_prefix(parts, overlap)

    for _header_line, section_text in sections:
        section_len = len(section_text)

        if section_len > chunk_size:
            # Seção gigante: fechar chunk atual, depois subdividir o corpo
            if current_parts:
                current_parts, current_len = _flush(current_parts)

            # Separar cabeçalho do corpo para subdivisão
            lines = section_text.split("\n", 1)
            header_prefix = lines[0].strip() if len(lines) > 1 else ""
            body = lines[1].strip() if len(lines) > 1 else section_text

            sub_chunks = _split_by_sentences(body, chunk_size)
            for sub in sub_chunks:
                if header_prefix:
                    chunk_text = header_prefix + "\n\n" + sub
                else:
                    chunk_text = sub
                chunks.append(chunk_text)
            # Após seção gigante não há overlap estrutural — próximo chunk começa limpo
            current_parts = []
            current_len = 0
            continue

        # Seção cabe: tentar adicionar ao chunk atual
        if current_len + section_len + 2 > chunk_size and current_parts:
            current_parts, current_len = _flush(current_parts)

        current_parts.append(section_text)
        current_len += section_len + 2

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks if chunks else [text]


# ---------------------------------------------------------------------------
# Geração do bloco SOURCE
# ---------------------------------------------------------------------------


async def _generate_source_block(
    ctx: dict,
    bibref: str,
    document_excerpt: str,
    llm_client: LLMClient,
) -> str:
    """Gera um bloco SOURCE para o documento usando o primeiro trecho como contexto.

    Caminho preferencial: JSON + assembler (build_source_schema → call_json_async
    → assemble_source). A moldura determinística do assembler emite indentação
    canônica, separadores e `NA` por construção — eliminando os erros de extração
    por regex, indentação inconsistente e campo REQUIRED ausente. Cai para o
    caminho de texto livre (extração por regex tolerante) se o JSON falhar.

    Args:
        ctx: Contexto do projeto.
        bibref: Referência bibliográfica.
        document_excerpt: Primeiros parágrafos do documento (contexto).
        llm_client: Cliente LLM.

    Returns:
        String com o bloco SOURCE...END SOURCE.
    """
    # Caminho JSON + assembler (preferencial)
    try:
        schema = build_source_schema(ctx)
        messages = build_document_source_values_prompt(ctx, bibref, document_excerpt)
        data = await llm_client.call_json_async(
            messages, schema, temperature=0.0, context=("source",)
        )
        if data is not None:
            return assemble_source(ctx, bibref, data)
    except Exception as exc:
        logger.debug("Caminho JSON do SOURCE falhou (%s) — usando texto livre", exc)

    # Limite do excerpt a ~500 chars para não desperdiçar tokens
    excerpt = document_excerpt[:500].strip()

    # Instrução especializada para gerar apenas SOURCE
    source_messages = [
        {
            "role": "system",
            "content": (
                "Você é um codificador de pesquisa qualitativa.\n"
                "Gere APENAS um bloco SOURCE Synesis para o documento abaixo.\n"
                "NÃO gere blocos ITEM, ONTOLOGY ou qualquer outro tipo.\n"
                "NÃO use markdown ou formatação extra.\n\n"
                + _build_source_fields_instruction(ctx)
            ),
            "cache": True,
        },
        {
            "role": "user",
            "content": (
                f"BIBREF: @{bibref}\n"
                f"<excerpt>{excerpt}</excerpt>\n\n"
                "Gere o bloco SOURCE para este documento."
            ),
            "cache": False,
        },
    ]

    # O contexto é passado como parâmetro (não via set_context na thread atual):
    # call_async roda em thread worker, onde o threading.local desta thread não
    # é visível. Sem isto o evento SOURCE recebe phase="chunk" e some da Etapa 1.
    raw = await llm_client.call_async(
        source_messages, temperature=0.0, context=("source",)
    )
    raw = _strip_markdown_fences(raw)

    # Extrair o bloco SOURCE da resposta. Tolerante a:
    #  - indentação à esquerda de SOURCE/END SOURCE (LLM aninha em explicação)
    #  - whitespace variável entre END e SOURCE; caixa diferente
    #  - texto explicativo antes/depois do bloco
    source_match = re.search(
        r"^[ \t]*SOURCE[ \t]+@\S+.*?^[ \t]*END[ \t]+SOURCE",
        raw,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if source_match:
        extracted = _dedent_block(source_match.group(0))
        return _patch_required_source_fields(extracted, ctx)

    # Fallback: tentar construir um SOURCE mínimo
    logger.warning(
        "Não foi possível extrair bloco SOURCE da resposta para %s — "
        "usando SOURCE mínimo. Resposta (primeiros 200 chars): %r",
        bibref, raw[:200],
    )
    return _patch_required_source_fields(f"SOURCE @{bibref}\nEND SOURCE", ctx)


def _dedent_block(block: str) -> str:
    """Remove indentação comum à esquerda do bloco inteiro, preservando a
    indentação relativa dos campos.

    Necessário quando o LLM aninha o bloco SOURCE inteiro sob uma explicação,
    deixando todas as linhas com um prefixo de espaços que quebraria o parser.
    """
    lines = block.splitlines()
    if not lines:
        return block.strip()
    # Indentação da linha de abertura (SOURCE @...) define o offset a remover
    opening_indent = len(lines[0]) - len(lines[0].lstrip(" \t"))
    if opening_indent == 0:
        return block.strip()
    dedented = []
    for line in lines:
        # Remove até `opening_indent` chars de whitespace do início
        stripped = line
        for _ in range(opening_indent):
            if stripped[:1] in (" ", "\t"):
                stripped = stripped[1:]
            else:
                break
        dedented.append(stripped)
    return "\n".join(dedented).strip()


def _patch_required_source_fields(source_block: str, ctx: dict) -> str:
    """Insere `campo: NA` para campos REQUIRED ausentes no bloco SOURCE.

    Garante conformidade estrutural mesmo quando o LLM omite um campo obrigatório,
    espelhando o comportamento do block_assembler para blocos ITEM.
    """
    required = ctx.get("required_source", [])
    if not required:
        return source_block

    # Encontrar linha END SOURCE para inserir antes dela
    end_match = re.search(r"^END SOURCE", source_block, re.MULTILINE)
    if not end_match:
        return source_block

    missing = [
        name for name in required
        if not re.search(rf"^[ \t]+{re.escape(name)}\s*:", source_block, re.MULTILINE)
    ]
    if not missing:
        return source_block

    # Detectar a indentação dos campos já presentes no bloco. A gramática usa um
    # Indenter (estilo Python): linhas de campo precisam ter EXATAMENTE a mesma
    # indentação, senão um _INDENT extra aninha o campo e ele some do SOURCE.
    indent = _detect_block_indent(source_block)

    insert_pos = end_match.start()
    patch_lines = "".join(f"{indent}{name}: NA\n" for name in missing)
    return source_block[:insert_pos] + patch_lines + source_block[insert_pos:]


def _detect_block_indent(block: str) -> str:
    """Retorna a indentação (espaços/tabs) do primeiro campo do bloco.

    Inspeciona a primeira linha indentada após a linha de abertura. Default
    de 4 espaços caso o bloco não tenha nenhum campo (ex: SOURCE vazio).
    """
    for line in block.splitlines()[1:]:  # pula a linha de abertura (SOURCE @...)
        m = re.match(r"^([ \t]+)\S", line)
        if m:
            return m.group(1)
    return "    "


def _build_source_fields_instruction(ctx: dict) -> str:
    """Instrução sobre os campos SOURCE disponíveis no template."""
    source_fields = ctx.get("source_fields", {})
    required_source = ctx.get("required_source", [])

    if not source_fields:
        return "CAMPOS DO SOURCE: nenhum campo definido no template."

    lines = ["CAMPOS DO SOURCE:"]
    for name, spec in source_fields.items():
        req = "REQUIRED" if name in required_source else "OPTIONAL"
        instruction = spec.guidelines or spec.description or ""
        lines.append(f"  {name} ({spec.type.name}) [{req}]: {instruction}")

    lines.append(
        "\nFORMATO:\n"
        "  SOURCE @{bibref}\n"
        "    {campo}: {valor}\n"
        "  END SOURCE"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deduplicação de ITEMs
# ---------------------------------------------------------------------------


def _normalize_concept(s: str) -> str:
    """Normaliza nome de conceito para comparação (lowercase, strip)."""
    return s.strip().lower().replace(" ", "_")


def _extract_chain_tuples(item_text: str) -> List[Tuple[str, str, str]]:
    """Extrai tuplas (A, RELACAO, B) de um bloco ITEM para deduplicação.

    Considera apenas o primeiro e último conceito de chains longas (A->R->B->R->C)
    para capturar o par de início/fim da chain.
    """
    tuples: List[Tuple[str, str, str]] = []
    # Linha de chain: conceito -> RELACAO -> conceito [-> RELACAO -> ...]
    for line in item_text.splitlines():
        line = line.strip()
        # Procurar padrão "campo: conceito -> REL -> conceito"
        m = re.match(r"[a-zA-Z_]+\s*:\s*(.+)", line)
        if m:
            chain_str = m.group(1)
            parts = [p.strip() for p in re.split(r"\s*->\s*", chain_str)]
            if len(parts) >= 3:
                # Pegar primeira tripla (A, REL, B)
                a = _normalize_concept(parts[0])
                rel = parts[1].strip().upper()
                b = _normalize_concept(parts[2])
                tuples.append((a, rel, b))
    return tuples


def _item_signature(item_text: str, quotation_field: Optional[str] = None) -> frozenset:
    """Gera uma assinatura para um bloco ITEM para deduplicação.

    A assinatura inclui:
    - Normalização do campo quotation (nome derivado do template, nunca hardcoded)
      — primeiros 100 chars do valor
    - Todas as tuplas (A, REL, B) de chains

    Retorna um frozenset de strings que representa o conteúdo do item.
    Retorna frozenset vazio se nenhum dos campos identificadores for encontrado
    (item será sempre preservado pelo merge_and_dedup).
    """
    sig = set()

    if quotation_field:
        # Busca dinâmica pelo nome real do campo no template
        q_match = re.search(
            rf"^\s*{re.escape(quotation_field)}\s*:\s*(.+?)(?=\n\s*\w|\Z)",
            item_text,
            re.MULTILINE | re.DOTALL,
        )
        if q_match:
            raw_text = q_match.group(1).strip()
            norm = re.sub(r"['\"\s]+", " ", raw_text.lower()).strip()[:100]
            sig.add(f"quotation:{norm}")

    # Extrair chains
    for a, rel, b in _extract_chain_tuples(item_text):
        sig.add(f"chain:{a}:{rel}:{b}")

    return frozenset(sig)


def merge_and_dedup(item_blocks: List[str], quotation_field: Optional[str] = None) -> str:
    """Combina e deduplica blocos ITEM de múltiplos chunks.

    Deduplicação por igualdade exata de assinatura normalizada:
    - Mesmo campo quotation (primeiros 100 chars) → duplicata
    - Mesmas tuplas (A, REL, B) de chains → duplicata

    Deduplicação é por igualdade exata (frozenset), nunca por proximidade,
    para evitar remoção de ITEMs distintos que compartilham alguns campos.
    Em caso de dúvida (assinatura vazia), preservar.

    Args:
        item_blocks: Lista de strings, cada uma sendo um bloco ITEM.
        quotation_field: Nome do campo QUOTATION no template (ex: "trecho").
            None desativa a parte da assinatura baseada em quotation.

    Returns:
        String com todos os blocos ITEM únicos concatenados.
    """
    seen_sigs: set = set()
    unique_items: List[str] = []

    for item in item_blocks:
        item = item.strip()
        if not item:
            continue

        sig = _item_signature(item, quotation_field=quotation_field)

        # Item sem assinatura identificável → sempre preservar
        if not sig:
            unique_items.append(item)
            continue

        if sig not in seen_sigs:
            unique_items.append(item)
            seen_sigs.add(sig)

    return "\n\n".join(unique_items)


# ---------------------------------------------------------------------------
# Processamento assíncrono de um chunk
# ---------------------------------------------------------------------------


async def _generate_chunk_syn(
    ctx: dict,
    bibref: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    llm_client: LLMClient,
    context: tuple,
) -> str:
    """Gera o texto Synesis de um chunk, preferindo o caminho JSON (Opção 3).

    Caminho JSON: prompt de valores → call_json_async(schema) → assembler. Cai
    para texto livre quando o backend não suporta json_schema ou a resposta não
    é JSON válido (call_json_async retorna None).
    """
    if llm_client.supports_json_schema():
        schema = build_item_schema(ctx)
        messages = build_document_values_prompt(
            ctx, bibref, chunk, chunk_index, total_chunks
        )
        data = await llm_client.call_json_async(
            messages, schema, temperature=0.0, context=context
        )
        if data is not None:
            return assemble_items(ctx, bibref, data)

    messages = build_document_prompt(ctx, bibref, chunk, chunk_index, total_chunks)
    return await llm_client.call_async(messages, temperature=0.0, context=context)


async def _process_chunk(
    bibref: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    ctx: dict,
    llm_client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> Tuple[int, List[str], bool]:
    """Processa um chunk e retorna os blocos ITEM extraídos.

    Args:
        bibref: Referência bibliográfica.
        chunk: Texto do chunk.
        chunk_index: Índice do chunk (0-based).
        total_chunks: Total de chunks.
        ctx: Contexto do projeto.
        llm_client: Cliente LLM compartilhado.
        semaphore: Semáforo de concorrência.

    Returns:
        (chunk_index, item_blocks, success)
    """
    async with semaphore:
        logger.debug(
            "Processando chunk %d/%d de @%s", chunk_index + 1, total_chunks, bibref
        )
        context = ("chunk", chunk_index, total_chunks)

        try:
            raw = await _generate_chunk_syn(
                ctx, bibref, chunk, chunk_index, total_chunks, llm_client, context
            )
        except Exception as exc:
            logger.error(
                "Falha na chamada LLM para chunk %d de @%s: %s",
                chunk_index + 1, bibref, exc,
            )
            return chunk_index, [], False

        # Validar e corrigir
        annotation_key = f"{bibref}_chunk{chunk_index}.syn"
        final_syn, success = await validate_and_fix_async(
            raw, ctx, llm_client, annotation_key=annotation_key,
            recorder=llm_client.recorder, context=context,
        )

        # Extrair apenas blocos ITEM
        extracted = _extract_item_blocks(final_syn)
        if not extracted:
            # Tentar extrair do raw se validate falhou mas há ITEMs
            extracted = _extract_item_blocks(_strip_markdown_fences(raw))

        item_blocks = [
            b.strip()
            for b in re.split(r"\n\n(?=ITEM\s+@)", extracted)
            if b.strip().startswith("ITEM")
        ]

        if llm_client.recorder is not None:
            # Nº de correções = chamadas fix registradas para este chunk.
            corrections = sum(
                1
                for c in llm_client.recorder._llm_calls
                if c.phase == "fix" and c.context and c.context[1] == chunk_index
            )
            llm_client.recorder.record_chunk_summary(
                context=context,
                items_generated=len(item_blocks),
                corrections=corrections,
                success=success,
            )

        logger.debug(
            "Chunk %d/%d: %d ITEMs extraídos (ok=%s)",
            chunk_index + 1, total_chunks, len(item_blocks), success,
        )
        return chunk_index, item_blocks, success


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------


def process_document(
    project_path: Path,
    bibref: str,
    input_path: Path,
    output_path: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    concurrent: int = 3,
    model: str | None = None,
    format: str = "plain",
    debug: bool = False,
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Processa um documento longo, gerando anotações Synesis (.syn).

    Args:
        project_path: Caminho para o arquivo .synp.
        bibref: Referência bibliográfica (chave BibTeX).
        input_path: Caminho para o arquivo .txt ou .md.
        output_path: Caminho de saída para o arquivo .syn.
        chunk_size: Tamanho máximo de cada chunk em caracteres.
        overlap: Tamanho do overlap entre chunks em caracteres.
        concurrent: Número máximo de chamadas LLM simultâneas.
        model: ID do modelo LLM (sobrescreve env SYNESIS_CODER_MODEL).
        format: "plain" (resumo simples) ou "verbose" (com header).
        debug: Se True, gera um relatório Markdown de auditoria do pipeline LLM
            ao lado do arquivo de saída (<projeto>_<bibref>_debug.md).
        overwrite: Se True, sobrescreve output existente sem confirmação.
        backup: Se True, cria backup (.syn.bak) do output existente antes de gravar.

    Returns:
        String com resumo da execução.
    """
    return asyncio.run(
        _process_document_async(
            project_path, bibref, input_path, output_path,
            chunk_size, overlap, concurrent, model, format, debug,
            overwrite, backup,
        )
    )


async def _process_document_async(
    project_path: Path,
    bibref: str,
    input_path: Path,
    output_path: Path,
    chunk_size: int,
    overlap: int,
    concurrent: int,
    model: str | None,
    format: str,
    debug: bool = False,
    overwrite: bool = False,
    backup: bool = False,
) -> str:
    """Implementação assíncrona do processamento de documento."""

    start_time = time.monotonic()

    # 1. Carregar documento e filtrar ruído antes do chunking
    text = read_document(input_path)
    original_len = len(text)
    text = clean_document(text)
    cleaned_len = len(text)
    reduction_pct = 100 * (original_len - cleaned_len) / original_len if original_len else 0

    # 2. Dividir em chunks
    chunks = split_into_chunks(text, chunk_size=chunk_size, overlap=overlap)
    total_chunks = len(chunks)
    logger.info(
        "Origem: %s (%s chars, %d chunks, −%.0f%% após limpeza)",
        input_path.name, _human_chars(cleaned_len), total_chunks, reduction_pct,
    )

    # 3. Carregar contexto do projeto — anotações pré-existentes inválidas são toleradas
    # porque este modo irá substituí-las; erros de template/bibref ainda abortam.
    ctx = load_project(project_path, load_annotations=True, tolerate_annotation_errors=True)

    # 3b. Pré-validar bibref (abort precoce — elimina E001 antes de gastar LLM)
    assert_bibref_known(ctx, bibref)

    # 4. Inicializar LLM client (com recorder de debug se solicitado)
    recorder = DebugRecorder() if debug else None
    llm_client = LLMClient(model=model, recorder=recorder)
    runtime_banner(llm_client, format=format)

    if recorder is not None:
        recorder.record_session_header(
            project=project_path.stem,
            input_name=input_path.name,
            input_chars=cleaned_len,
            bibref=bibref,
            model=llm_client.model,
            backend=llm_client.backend,
            start=now_human(),
            total_chunks=total_chunks,
            chunk_size=chunk_size,
            overlap=overlap,
            temperature=0.0,
        )

    # 5. Gerar SOURCE block (primeiro trecho como contexto)
    logger.debug("Gerando bloco SOURCE para @%s...", bibref)
    source_block = await _generate_source_block(
        ctx, bibref, chunks[0], llm_client
    )

    # 6. Processar chunks em paralelo (com semáforo)
    semaphore = asyncio.Semaphore(concurrent)

    tasks = [
        _process_chunk(
            bibref, chunk, i, total_chunks, ctx, llm_client, semaphore
        )
        for i, chunk in enumerate(chunks)
    ]

    # Coletar resultados mantendo ordem
    results_by_index: dict = {}
    total_ok = 0
    total_fail = 0

    progress = _ChunkProgress(total_chunks)
    progress.start()
    for coro in asyncio.as_completed(tasks):
        idx, item_blocks, success = await coro
        results_by_index[idx] = item_blocks
        if success:
            total_ok += 1
        else:
            total_fail += 1
        progress.mark(idx, success)
    progress.finish()

    # 7. Combinar ITEMs em ordem de chunk
    all_item_blocks: List[str] = []
    for i in range(total_chunks):
        all_item_blocks.extend(results_by_index.get(i, []))

    logger.debug(
        "Total de ITEMs antes da deduplicação: %d", len(all_item_blocks)
    )

    # 8. Deduplicar (nome do campo QUOTATION derivado do template)
    quotation_field = next(
        (
            name
            for name, spec in ctx.get("item_fields", {}).items()
            if spec.type.name == "QUOTATION"
        ),
        None,
    )
    combined_items = merge_and_dedup(all_item_blocks, quotation_field=quotation_field)
    final_item_blocks = [
        b.strip()
        for b in re.split(r"\n\n(?=ITEM\s+@)", combined_items)
        if b.strip().startswith("ITEM")
    ]
    logger.debug(
        "ITEMs após deduplicação: %d", len(final_item_blocks)
    )

    # 9. Montar output final: SOURCE + ITEMs
    final_output = source_block + "\n\n" + combined_items

    # 10. Validação final
    has_errors = False
    try:
        validation = synesis.load(
            project_content=ctx["project_content"],
            template_content=ctx["template_content"],
            annotation_contents={output_path.name: final_output},
            bibliography_content=ctx.get("bib_content"),
        )
        has_errors = _has_structural_errors(validation)
        if has_errors:
            diag = validation.get_diagnostics(verbose=False)
            logger.warning("Validação final detectou erros estruturais:\n%s", diag)
    except Exception as exc:
        has_errors = True
        logger.warning("Validação final: erro de parse no output combinado: %s", exc)

    # 11. Gravar output (escrita atômica, com proteção de sobrescrita e backup)
    output_path = Path(output_path).resolve()
    safe_write_output(output_path, final_output + "\n", overwrite=overwrite, backup=backup)
    logger.debug("Escrito: %s", output_path)

    elapsed = time.monotonic() - start_time
    items_count = len(final_item_blocks)
    status = "OK" if not has_errors else "COM AVISOS"

    # Gravar relatório de debug (--debug)
    if recorder is not None:
        recorder.record_session_footer(
            total_chunks=total_chunks,
            total_ok=total_ok,
            total_fail=total_fail,
            items_generated=len(all_item_blocks),
            items_dedup=items_count,
            tokens_line=llm_client.usage.summary_line(),
            elapsed=elapsed,
            validation="✅ OK" if not has_errors else "⚠️ COM AVISOS",
            output_file=output_path.name,
        )
        debug_path = output_path.parent / f"{project_path.stem}_{bibref}_debug.md"
        recorder.write(debug_path)
        logger.debug("Relatório de debug escrito: %s", debug_path)

    logger.log(21,  # OK
        "Validação concluída. %d itens únicos extraídos (de %d totais) em %.1fs.",
        items_count, len(all_item_blocks), elapsed,
    )
    logger.log(22, "%s", output_path)  # DEST

    if format == "verbose":
        return (
            f"# synesis-coder document\n"
            f"# projeto: {project_path.stem}\n"
            f"# bibref: @{bibref}\n"
            f"# input: {input_path.name}\n"
            f"# chunks: {total_chunks} | ITEMs: {items_count} | validação: {status}\n"
            f"# {llm_client.usage.summary_line()}\n"
            f"# tempo: {elapsed:.1f}s\n"
        )

    return ""
