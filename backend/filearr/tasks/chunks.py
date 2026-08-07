"""LLM/RAG M2 — the per-item chunk pass + backfill (design §4 tier 2).

* :func:`chunk_item` — (re)chunk one item's extracted text into ``doc_chunks``
  and refresh its slice of the Meili chunks projection. Deferred after the
  extract commit (invariant 5) for items whose library opted in; gated by the
  ``_chunks_fp`` fingerprint stamp so an unchanged document no-ops.
* :func:`chunk_missing` — the bounded backfill: defers ``chunk_item`` for
  opted-in items whose stamp is missing/stale, capped per run (mirrors
  ``embed_missing``). Exposed as the "Chunk documents for RAG" maintenance
  action.
* :func:`rebuild_chunks_index` — drop + re-project the whole chunks index
  from Postgres truth (invariant 1); persisted per-chunk embeddings mean a
  rebuild never re-embeds.

Chunking runs on the ``embed`` queue at embed priority: it embeds chunk
vectors (the expensive part) and must never starve extraction.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, or_, select

from filearr.chunking import (
    CHUNKS_FP_KEY,
    build_chunk_doc,
    chunk_text,
    chunks_fingerprint,
    chunks_index_uid,
    delete_item_chunk_docs,
    ensure_chunks_index,
    source_text,
    upsert_chunk_docs,
)
from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.embed import embed_texts
from filearr.models import DocChunk, Item, ItemStatus, Library
from filearr.worker import proc_app

logger = logging.getLogger(__name__)


@proc_app.task(queue="embed", name="filearr.tasks.chunks.chunk_item", retry=2)
async def chunk_item(item_id: str) -> int:
    """(Re)chunk one item; returns the number of chunks stored.

    No-op when the owning library is not opted in, the item is gone/inactive,
    there is no extracted text (any stale chunks are then dropped), or the
    fingerprint stamp is already current."""
    import hashlib

    settings = get_settings()
    async with SessionLocal() as session:
        item = (
            await session.execute(select(Item).where(Item.id == item_id))
        ).scalar_one_or_none()
        if item is None or item.status != ItemStatus.active:
            return 0
        library = await session.get(Library, item.library_id)
        if library is None or not library.chunking_enabled:
            return 0

        text = source_text(item.effective_metadata)
        if not text:
            # Text disappeared (re-extract) -> drop any stale chunk rows + docs.
            had = bool(
                (
                    await session.execute(
                        select(DocChunk.id).where(DocChunk.item_id == item.id).limit(1)
                    )
                ).scalar_one_or_none()
            )
            if had or CHUNKS_FP_KEY in item.metadata_:
                await session.execute(
                    delete(DocChunk).where(DocChunk.item_id == item.id)
                )
                meta = dict(item.metadata_)
                meta.pop(CHUNKS_FP_KEY, None)
                item.metadata_ = meta
                await session.commit()
                await delete_item_chunk_docs(str(item.id))
            return 0

        fp = chunks_fingerprint(text, settings)
        if item.metadata_.get(CHUNKS_FP_KEY) == fp:
            return 0  # current — nothing to do

        pieces = chunk_text(
            text, size=settings.chunk_size_chars, overlap=settings.chunk_overlap_chars
        )[: settings.chunk_max_per_item]

        vectors: list[list[float] | None]
        if settings.semantic_enabled and pieces:
            vectors = list(embed_texts(pieces, settings.embedder_config))
        else:
            vectors = [None] * len(pieces)

        # Wholesale replace: simpler and safer than diffing chunk windows.
        await session.execute(delete(DocChunk).where(DocChunk.item_id == item.id))
        rows = [
            DocChunk(
                item_id=item.id,
                chunk_no=i,
                text_=piece,
                sha256=hashlib.sha256(piece.encode()).hexdigest(),
                embedding=vec,
            )
            for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=True))
        ]
        session.add_all(rows)
        item.metadata_ = {**item.metadata_, CHUNKS_FP_KEY: fp}
        await session.commit()

        # Refresh the projection slice AFTER the commit (invariant 5).
        await ensure_chunks_index()
        await delete_item_chunk_docs(str(item.id))
        await upsert_chunk_docs([build_chunk_doc(c, item) for c in rows])
        return len(rows)


@proc_app.task(queue="embed", name="filearr.tasks.chunks.chunk_missing", retry=2)
async def chunk_missing() -> dict:
    """Bounded backfill over opted-in libraries (the on-demand maintenance
    action). Defers ``chunk_item`` for items with extractable text whose
    ``_chunks_fp`` stamp is absent — a config/embedder change is picked up
    lazily per item by ``chunk_item``'s own fingerprint check. Re-run until
    ``deferred`` is 0."""
    settings = get_settings()
    async with SessionLocal() as session:
        lib_ids = [
            lid
            for (lid,) in (
                await session.execute(
                    select(Library.id).where(Library.chunking_enabled.is_(True))
                )
            ).all()
        ]
        if not lib_ids:
            return {"deferred": 0, "skipped": "no library has chunking enabled"}
        rows = (
            (
                await session.execute(
                    select(Item.id)
                    .where(
                        Item.status == ItemStatus.active,
                        Item.library_id.in_(lib_ids),
                        or_(
                            Item.metadata_.has_key("body_text"),
                            Item.metadata_.has_key("ocr_text"),
                        ),
                        ~Item.metadata_.has_key(CHUNKS_FP_KEY),
                    )
                    .order_by(Item.id)
                    .limit(settings.chunk_backfill_batch)
                )
            )
            .scalars()
            .all()
        )
    deferrer = proc_app.configure_task(
        "filearr.tasks.chunks.chunk_item",
        queue=settings.queue_embed,
        priority=settings.embed_priority,
    )
    for iid in rows:
        await deferrer.defer_async(item_id=str(iid))
    if rows:
        logger.info("chunk_missing: deferred %d chunk_item job(s)", len(rows))
    return {"deferred": len(rows)}


@proc_app.task(
    queue="embed",
    name="filearr.tasks.chunks.rebuild_chunks_index",
    retry=1,
    queueing_lock="rebuild-chunks-index",
)
async def rebuild_chunks_index() -> dict:
    """Re-project the WHOLE chunks index from Postgres truth (invariant 1).

    Deletes every doc then streams the doc_chunks rows back in batches. The
    chunks index is small relative to items (only opted-in text-bearing docs),
    so a plain delete-all + re-add is sufficient — no shadow-swap machinery."""
    from filearr.search import client

    await ensure_chunks_index()
    async with client() as c:
        await c.index(chunks_index_uid()).delete_all_documents()

    total = 0
    batch: list[dict] = []
    async with SessionLocal() as session:
        result = await session.stream(
            select(DocChunk, Item)
            .join(Item, Item.id == DocChunk.item_id)
            .where(Item.status == ItemStatus.active)
            .execution_options(yield_per=500)
        )
        async for chunk, item in result:
            batch.append(build_chunk_doc(chunk, item))
            if len(batch) >= 500:
                await upsert_chunk_docs(batch)
                total += len(batch)
                batch = []
    if batch:
        await upsert_chunk_docs(batch)
        total += len(batch)
    logger.info("rebuild_chunks_index: projected %d chunk doc(s)", total)
    return {"projected": total}
