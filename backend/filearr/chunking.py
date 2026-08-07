"""LLM/RAG M2 — passage chunking + the disposable Meili chunks projection.

Design (docs/research/llm-rag-integration.md §4 tier 2): items whose library
opted in (``Library.chunking_enabled``) get their extracted text (``body_text``
/ ``ocr_text``) split into ~1,000-char chunks with overlap, stored in Postgres
(``doc_chunks``, source of truth) with a persisted local embedding, and
projected into a SECOND Meili index (``<meili_index>_chunks``) that
``retrieve_passages`` searches. Invariants preserved:

* **Postgres truth / Meili disposable** — the chunks index is rebuilt from
  ``doc_chunks`` (``rebuild_chunks_index`` maintenance action); vectors are
  persisted per-chunk so a rebuild never re-embeds.
* **Change detection via fingerprint stamp** — the item's ``metadata_``
  carries ``_chunks_fp`` (sha256 of source text + chunk config + embedder
  fingerprint); the chunk pass no-ops when current, replaces wholesale when
  stale. Extracted-metadata column only (invariant 2).
* **Same embedder as item vectors** (bge-small ONNX, local): the chunks index
  registers the identical userProvided embedder, so semantic passage search
  needs nothing new; without ``semantic_enabled`` chunks are keyword-searched.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from filearr.config import get_settings
from filearr.meili_ops import DEFAULT_EMBEDDER_NAME, build_embedders, embedder_matches
from filearr.models import DocChunk, Item

log = logging.getLogger("filearr.chunking")

#: metadata_ stamp marking the chunk pass current for an item.
CHUNKS_FP_KEY = "_chunks_fp"


def chunks_index_uid(settings=None) -> str:
    s = settings or get_settings()
    return f"{s.meili_index}_chunks"


def chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    """Split ``text`` into ~``size``-char chunks with ``overlap`` chars of
    lookback, preferring to break at whitespace near the boundary. Pure."""
    text = text.strip()
    if not text:
        return []
    if size <= 0:
        raise ValueError("chunk size must be positive")
    overlap = max(0, min(overlap, size // 2))
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + size)
        if end < n:
            # Prefer a whitespace break within the last 15% of the window so
            # chunks don't split words mid-token.
            window_floor = start + int(size * 0.85)
            space = text.rfind(" ", window_floor, end)
            if space > start:
                end = space
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(start + 1, end - overlap)
    return out


def source_text(meta: dict) -> str:
    """The chunkable text of an item — same precedence as read_content."""
    return (meta.get("body_text") or meta.get("ocr_text") or "").strip()


def chunks_fingerprint(text: str, settings=None) -> str:
    """Fingerprint binding source text + chunk config + embedder identity, so a
    change to ANY of them re-chunks on the next pass."""
    s = settings or get_settings()
    basis = "|".join(
        (
            hashlib.sha256(text.encode()).hexdigest(),
            str(s.chunk_size_chars),
            str(s.chunk_overlap_chars),
            str(s.chunk_max_per_item),
            s.embed_model if s.semantic_enabled else "nosem",
            s.embed_version if s.semantic_enabled else "-",
        )
    )
    return hashlib.sha256(basis.encode()).hexdigest()


def build_chunk_doc(chunk: DocChunk, item: Item) -> dict[str, Any]:
    """One Meili document for the chunks index. ``id`` must be Meili-safe
    (alphanumeric/hyphen/underscore): ``<item uuid>_<chunk_no>``."""
    doc: dict[str, Any] = {
        "id": f"{chunk.item_id}_{chunk.chunk_no}",
        "item_id": str(chunk.item_id),
        "library_id": str(item.library_id),
        "chunk_no": chunk.chunk_no,
        "text": chunk.text_,
        "filename": item.filename,
        "rel_path": item.rel_path,
    }
    if item.path_scope is not None:
        doc["path_scope"] = str(item.path_scope)
    if chunk.embedding:
        doc["_vectors"] = {DEFAULT_EMBEDDER_NAME: chunk.embedding}
    return doc


# --------------------------------------------------------------------------- #
# Meili index plumbing (mirrors search.py's ensure/upsert/delete shape)        #
# --------------------------------------------------------------------------- #

#: The chunks index is tiny-spec: text search over ``text``/``filename``,
#: filters for scoping, no facets/sortables (retrieval is relevance-only).
CHUNK_SEARCHABLE = ("text", "filename")
CHUNK_FILTERABLE = ("item_id", "library_id", "path_scope")


async def ensure_chunks_index() -> None:
    """Create/settings-converge the chunks index (idempotent, boot + rebuild)."""
    from filearr.search import client

    s = get_settings()
    async with client() as c:
        index = await c.get_or_create_index(chunks_index_uid(s), primary_key="id")
        if index.primary_key is None:
            await index.update(primary_key="id")
        current = await index.get_settings()
        if set(current.searchable_attributes or ["*"]) != set(CHUNK_SEARCHABLE):
            await index.update_searchable_attributes(list(CHUNK_SEARCHABLE))
        if set(current.filterable_attributes or []) != set(CHUNK_FILTERABLE):
            await index.update_filterable_attributes(list(CHUNK_FILTERABLE))
        if s.semantic_enabled:
            embedders = await index.get_embedders()
            if not embedder_matches(embedders, s.embed_dim):
                await index.update_embedders(build_embedders(s.embed_dim))


async def upsert_chunk_docs(docs: list[dict[str, Any]]) -> None:
    from filearr.search import client

    if not docs:
        return
    async with client() as c:
        await c.index(chunks_index_uid()).update_documents(docs, primary_key="id")


async def delete_item_chunk_docs(item_id: str) -> None:
    """Drop every chunk doc of an item from the projection (filter delete)."""
    from filearr.search import client

    async with client() as c:
        await c.index(chunks_index_uid()).delete_documents_by_filter(
            f'item_id = "{item_id}"'
        )
