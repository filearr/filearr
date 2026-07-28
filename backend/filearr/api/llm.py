"""LLM tool facade (M1) — a compact, role-gated surface for LLM tool calling.

Mounted as its OWN FastAPI sub-application at ``/api/llm/v1`` so it serves a
trimmed ``openapi.json`` containing ONLY these operations — OpenWebUI's
external-tool import and MCP bridges (mcpo) consume that spec directly,
and ``/capabilities`` renders the same tools as OpenAI function specs for
bare Ollama/openai-client loops. Design: docs/research/llm-rag-integration.md.

Enforcement lives HERE (role tool-gating, path/library scope, caps, rate
limit) — the system prompt only describes it. Every tool call is audited.
"""

from __future__ import annotations

import hashlib
import uuid as uuidlib
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from meilisearch_python_sdk.models.search import Hybrid
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, querydsl
from filearr.config import get_settings
from filearr.db import get_session
from filearr.embed import embed_query
from filearr.llm import (
    RATE_LIMITER,
    TOOL_SPECS,
    LlmPrincipal,
    principal_for,
    render_system_prompt,
    tools_openai,
)
from filearr.meili_ops import DEFAULT_EMBEDDER_NAME
from filearr.models import Agent, ApiKey, Item, Library
from filearr.query_sql import QueryTranslationError, ast_to_where
from filearr.rbac import PathGrant
from filearr.rbac_sql import _covers, path_scope_uses_ltree
from filearr.reports import ReportParams, get_report, list_reports
from filearr.search import client as meili_client
from filearr.tenant_tokens import compile_scope_filter

llm_app = FastAPI(
    title="Filearr LLM tools",
    version="1",
    description=(
        "Role-gated tool facade for LLM clients. Authenticate with an LLM "
        "API key: 'Authorization: Bearer <key>'."
    ),
)

_SEARCH_ACTION = "search_metadata"


# --------------------------------------------------------------------------- #
# Auth + scope plumbing                                                        #
# --------------------------------------------------------------------------- #


async def llm_principal(
    request: Request, session: AsyncSession = Depends(get_session)
) -> LlmPrincipal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing Bearer key")
    token = auth[7:].strip()
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(401, "unknown key")
    if row.expires_at is not None and row.expires_at < datetime.now(UTC):
        raise HTTPException(401, "key expired")
    principal = principal_for(row)
    if principal is None:
        raise HTTPException(
            403, "not an LLM key (mint one with a role via /api/v1/llm-keys)"
        )
    if not RATE_LIMITER.allow(principal.key_id, principal.rate_limit):
        raise HTTPException(
            429, "rate limit exceeded", headers={"Retry-After": "5"}
        )
    return principal


def _require(principal: LlmPrincipal, tool: str) -> None:
    if not principal.allows(tool):
        raise HTTPException(
            403,
            f"role {principal.role.name!r} does not include the {tool} tool",
        )


async def _audit_tool(
    request: Request, principal: LlmPrincipal, tool: str, rows: int
) -> None:
    await audit.emit(
        "LLM_TOOL_CALL",
        request=request,
        principal_id=principal.key_id,
        details={"tool": tool, "role": principal.role.name, "rows": rows},
    )


async def _sql_scope(principal: LlmPrincipal, session: AsyncSession) -> list:
    """SQL predicates enforcing the key's library allow-list + path scope."""
    preds = []
    if principal.libraries:
        preds.append(Item.library_id.in_([uuidlib.UUID(x) for x in principal.libraries]))
    if principal.path_scope:
        use_ltree = await path_scope_uses_ltree(session)
        preds.append(_covers(Item.path_scope, principal.path_scope, use_ltree=use_ltree))
    return preds


def _meili_scope(principal: LlmPrincipal) -> list[str]:
    """Meili filter fragments for the same scope (deny-aware ancestor filter)."""
    out = []
    if principal.libraries:
        joined = ", ".join(f'"{x}"' for x in principal.libraries)
        out.append(f"library_id IN [{joined}]")
    if principal.path_scope:
        compiled = compile_scope_filter(
            [PathGrant(path=principal.path_scope, action=_SEARCH_ACTION)],
            action=_SEARCH_ACTION,
        )
        out.append(compiled.expression)
    return out


def _cap(limit: int | None, default: int, ceiling: int) -> int:
    if not limit or limit < 1:
        return default
    return min(limit, ceiling)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _row(principal: LlmPrincipal, it: Item, library_name: str | None) -> dict:
    d = {
        "citation": str(it.id),
        "filename": it.filename,
        "kind": it.file_category,
        "library": library_name,
        "size": it.size,
        "mtime": _iso(it.mtime),
    }
    if principal.reveal_paths:
        d["rel_path"] = it.rel_path
    return d


async def _get_item(
    citation: str, principal: LlmPrincipal, session: AsyncSession
) -> Item:
    try:
        item_id = uuidlib.UUID(citation)
    except ValueError:
        raise HTTPException(
            422, "citation must be a file citation id from a prior tool result"
        ) from None
    stmt = select(Item).where(Item.id == item_id)
    for p in await _sql_scope(principal, session):
        stmt = stmt.where(p)
    it = (await session.execute(stmt)).scalar_one_or_none()
    if it is None:
        raise HTTPException(404, "no such file visible to this role")
    return it


# --------------------------------------------------------------------------- #
# Tool request models                                                          #
# --------------------------------------------------------------------------- #


class SearchArgs(BaseModel):
    query: str
    mode: str = Field(default="hybrid", pattern="^(keyword|semantic|hybrid)$")
    kind: str | None = None
    library: str | None = None
    limit: int | None = None


class CitationArgs(BaseModel):
    citation: str


class ReadContentArgs(BaseModel):
    citation: str
    max_chars: int | None = None
    page: int = 0


class FilterArgs(BaseModel):
    dsl: str
    limit: int | None = None


class ReportArgs(BaseModel):
    report: str
    limit: int | None = None
    offset: int = 0


class AggregateArgs(BaseModel):
    group_by: str = Field(pattern="^(kind|group|library|extension|year)$")
    metric: str = Field(default="count", pattern="^(count|bytes)$")
    dsl: str | None = None


# --------------------------------------------------------------------------- #
# Tools                                                                        #
# --------------------------------------------------------------------------- #


@llm_app.post("/search_files", operation_id="search_files")
async def search_files(
    args: SearchArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "search_files")
    s = get_settings()
    limit = _cap(args.limit, 15, 50)

    filters = list(_meili_scope(principal)) + ['status = "active"']
    if args.kind:
        filters.append(f'file_category = "{args.kind.strip().lower()}"')
    if args.library:
        lib = (
            await session.execute(
                select(Library).where(Library.name == args.library)
            )
        ).scalar_one_or_none()
        if lib is not None:
            filters.append(f'library_id = "{lib.id}"')

    hybrid = None
    vector = None
    if args.mode in ("semantic", "hybrid") and s.semantic_enabled:
        vector = embed_query(args.query)
        ratio = 1.0 if args.mode == "semantic" else 0.5
        hybrid = Hybrid(semantic_ratio=ratio, embedder=DEFAULT_EMBEDDER_NAME)

    kwargs: dict = dict(
        filter=" AND ".join(f"({f})" for f in filters),
        limit=limit,
        hybrid=hybrid,
        vector=vector,
        attributes_to_retrieve=[
            "id", "filename", "title", "rel_path", "library_id",
            "file_category", "size", "mtime",
        ],
    )
    if principal.role.snippets:
        kwargs["attributes_to_crop"] = ["body_text"]
        kwargs["crop_length"] = 30

    async with meili_client() as c:
        result = await c.index(s.meili_index).search(args.query, **kwargs)

    lib_names = {
        str(lid): name
        for lid, name in (await session.execute(select(Library.id, Library.name))).all()
    }
    rows = []
    for hit in result.hits:
        row = {
            "citation": hit.get("id"),
            "filename": hit.get("filename") or hit.get("title"),
            "kind": hit.get("file_category"),
            "library": lib_names.get(str(hit.get("library_id"))),
            "size": hit.get("size"),
            "mtime": hit.get("mtime"),
        }
        if principal.reveal_paths:
            row["rel_path"] = hit.get("rel_path")
        if principal.role.snippets:
            fmt = hit.get("_formatted") or {}
            snippet = (fmt.get("body_text") or "").strip()
            if snippet and snippet != "…":
                row["snippet"] = snippet[:400]
        rows.append(row)
    await _audit_tool(request, principal, "search_files", len(rows))
    return {"rows": rows, "role": principal.role.name}


@llm_app.post("/get_file", operation_id="get_file")
async def get_file(
    args: CitationArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "get_file")
    it = await _get_item(args.citation, principal, session)
    lib = await session.get(Library, it.library_id)

    dup_count = 0
    if it.content_hash:
        dup_count = (
            await session.execute(
                select(func.count()).select_from(Item).where(
                    Item.content_hash == it.content_hash,
                    Item.status == "active",
                    Item.id != it.id,
                )
            )
        ).scalar_one()

    meta = dict(it.effective_metadata)
    # Content stays behind read_content: strip bulk text, keep a hint.
    for key in ("body_text", "ocr_text"):
        if key in meta:
            meta[key] = f"<{len(meta[key])} chars — use read_content>"

    out = {
        "citation": str(it.id),
        "filename": it.filename,
        "title": it.title,
        "kind": it.file_category,
        "group": it.file_group,
        "extension": it.extension,
        "library": lib.name if lib else None,
        "size": it.size,
        "mtime": _iso(it.mtime),
        "year": it.year,
        "tags": list(it.tags or []),
        "status": it.status,
        "duplicate_copies": dup_count,
        "metadata": meta,
    }
    if principal.reveal_paths:
        out["rel_path"] = it.rel_path
        out["path"] = it.path
    await _audit_tool(request, principal, "get_file", 1)
    return out


@llm_app.post("/where_is", operation_id="where_is")
async def where_is(
    args: CitationArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "where_is")
    if not principal.reveal_paths:
        raise HTTPException(403, "this role cannot reveal file locations")
    it = await _get_item(args.citation, principal, session)

    # The item itself + every active byte-identical copy (content hash).
    ids = [it.id]
    copies: list[Item] = [it]
    if it.content_hash:
        others = (
            (
                await session.execute(
                    select(Item).where(
                        Item.content_hash == it.content_hash,
                        Item.status == "active",
                        Item.id != it.id,
                    ).limit(20)
                )
            )
            .scalars()
            .all()
        )
        copies += list(others)
        ids += [o.id for o in others]

    lib_rows = (
        await session.execute(
            select(Library).where(Library.id.in_({c.library_id for c in copies}))
        )
    ).scalars().all()
    libs = {lr.id: lr for lr in lib_rows}
    agent_rows = (
        await session.execute(
            select(Agent).where(
                Agent.id.in_({c.source_agent_id for c in copies if c.source_agent_id})
            )
        )
    ).scalars().all()
    agents = {a.id: a for a in agent_rows}

    locations = []
    for c in copies:
        lib = libs.get(c.library_id)
        agent = agents.get(c.source_agent_id) if c.source_agent_id else None
        loc = {
            "citation": str(c.id),
            "library": lib.name if lib else None,
            "host": (agent.name if agent else "central"),
            "path": c.path,
            "status": c.status,
        }
        hint = c.share_hint or {}
        if hint.get("share_url"):
            loc["share_url"] = hint["share_url"]
        if hint.get("unc"):
            loc["unc"] = hint["unc"]
        locations.append(loc)
    await _audit_tool(request, principal, "where_is", len(locations))
    return {"filename": it.filename, "locations": locations}


@llm_app.post("/read_content", operation_id="read_content")
async def read_content(
    args: ReadContentArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "read_content")
    it = await _get_item(args.citation, principal, session)
    meta = it.effective_metadata
    text = meta.get("body_text") or meta.get("ocr_text") or ""
    if not text:
        return {
            "citation": str(it.id),
            "text": "",
            "note": (
                "no extracted text for this file "
                "(not a text-bearing type, or not extracted yet)"
            ),
        }
    max_chars = _cap(args.max_chars, 4000, 8000)
    page = max(0, args.page)
    start = page * max_chars
    chunk = text[start : start + max_chars]
    out = {
        "citation": str(it.id),
        "filename": it.filename,
        "page": page,
        "pages_total": (len(text) + max_chars - 1) // max_chars,
        "text": chunk,
        "truncated_at_extract": bool(meta.get("body_text_truncated")),
    }
    await _audit_tool(request, principal, "read_content", 1)
    return out


@llm_app.post("/filter_files", operation_id="filter_files")
async def filter_files(
    args: FilterArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "filter_files")
    limit = _cap(args.limit, 25, 100)
    try:
        where = ast_to_where(querydsl.parse(args.dsl))
    except querydsl.ParseError as exc:
        raise HTTPException(422, f"filter parse error: {exc}") from exc
    except QueryTranslationError as exc:
        raise HTTPException(422, f"filter not supported here: {exc}") from exc

    stmt: Select = (
        select(Item, Library.name)
        .join(Library, Library.id == Item.library_id)
        .where(where, Item.status == "active", Item.sidecar_of.is_(None))
        .order_by(Item.mtime.desc().nulls_last())
        .limit(limit)
    )
    for p in await _sql_scope(principal, session):
        stmt = stmt.where(p)
    rows = [
        _row(principal, it, lib_name)
        for it, lib_name in (await session.execute(stmt)).all()
    ]
    await _audit_tool(request, principal, "filter_files", len(rows))
    return {"rows": rows, "role": principal.role.name}


@llm_app.post("/run_report", operation_id="run_report")
async def run_report(
    args: ReportArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "run_report")
    report = get_report(args.report)
    if report is None:
        raise HTTPException(
            404,
            f"unknown report {args.report!r}; valid ids: "
            + ", ".join(r["id"] for r in list_reports()),
        )
    limit = _cap(args.limit, 100, 200)
    offset = max(0, args.offset)
    stmt = report.build(ReportParams(limit=limit + offset + 1))
    result = await session.execute(stmt)
    fetched = [report.row(r) for r in result.all()]
    page = fetched[offset : offset + limit]
    await _audit_tool(request, principal, "run_report", len(page))
    return {
        "report": report.id,
        "columns": list(report.columns),
        "rows": page,
        "has_more": len(fetched) > offset + limit,
    }


@llm_app.post("/aggregate", operation_id="aggregate")
async def aggregate(
    args: AggregateArgs,
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "aggregate")
    col = {
        "kind": Item.file_category,
        "group": Item.file_group,
        "library": Library.name,
        "extension": Item.extension,
        "year": Item.year,
    }[args.group_by]
    metric = func.count() if args.metric == "count" else func.coalesce(func.sum(Item.size), 0)

    stmt = (
        select(col, metric)
        .select_from(Item)
        .join(Library, Library.id == Item.library_id)
        .where(Item.status == "active")
        .group_by(col)
        .order_by(metric.desc())
        .limit(100)
    )
    if args.dsl:
        try:
            stmt = stmt.where(ast_to_where(querydsl.parse(args.dsl)))
        except (querydsl.ParseError, QueryTranslationError) as exc:
            raise HTTPException(422, f"filter error: {exc}") from exc
    for p in await _sql_scope(principal, session):
        stmt = stmt.where(p)
    rows = [
        {"key": (k if k not in (None, "") else "(none)"), args.metric: v}
        for k, v in (await session.execute(stmt)).all()
    ]
    await _audit_tool(request, principal, "aggregate", len(rows))
    return {"group_by": args.group_by, "metric": args.metric, "rows": rows}


@llm_app.post("/catalog_overview", operation_id="catalog_overview")
async def catalog_overview(
    request: Request,
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _require(principal, "catalog_overview")
    scope = await _sql_scope(principal, session)

    lib_stmt = (
        select(Library.name, func.count(Item.id), func.coalesce(func.sum(Item.size), 0))
        .join(Item, Item.library_id == Library.id, isouter=True)
        .where(Item.status == "active")
        .group_by(Library.id, Library.name)
        .order_by(func.count(Item.id).desc())
    )
    for p in scope:
        lib_stmt = lib_stmt.where(p)
    libraries = [
        {"name": name, "items": n, "bytes": b}
        for name, n, b in (await session.execute(lib_stmt)).all()
    ]

    cat_stmt = (
        select(Item.file_category, func.count())
        .where(Item.status == "active")
        .group_by(Item.file_category)
        .order_by(func.count().desc())
        .limit(20)
    )
    for p in scope:
        cat_stmt = cat_stmt.where(p)
    categories = [
        {"kind": (k or "(unclassified)"), "items": n}
        for k, n in (await session.execute(cat_stmt)).all()
    ]

    agents = [
        {"name": a.name, "status": a.status, "last_seen": _iso(a.last_seen_at)}
        for a in (await session.execute(select(Agent).limit(50))).scalars().all()
    ]
    await _audit_tool(request, principal, "catalog_overview", len(libraries))
    return {
        "libraries": libraries,
        "categories": categories,
        "agents": agents,
        "total_items": sum(x["items"] for x in libraries),
        "total_bytes": sum(x["bytes"] for x in libraries),
        "role": principal.role.name,
    }


# --------------------------------------------------------------------------- #
# Capability handshake                                                         #
# --------------------------------------------------------------------------- #


async def _capabilities(principal: LlmPrincipal, session: AsyncSession) -> dict:
    s = get_settings()
    item_count = (
        await session.execute(
            select(func.count()).select_from(Item).where(Item.status == "active")
        )
    ).scalar_one()
    lib_names = [
        n for (n,) in (await session.execute(select(Library.name).order_by(Library.name))).all()
    ]
    agent_count = (
        await session.execute(select(func.count()).select_from(Agent))
    ).scalar_one()
    return {
        "system": {
            "name": "Filearr",
            "item_count": item_count,
            "libraries": lib_names if principal.reveal_paths else len(lib_names),
            "agents": agent_count,
            "semantic_search": s.semantic_enabled,
        },
        "role": {
            "name": principal.role.name,
            "description": principal.role.description,
            "tools": [t for t in TOOL_SPECS if principal.allows(t)],
            "content_access": principal.content_access,
            "reveal_paths": principal.reveal_paths,
            "rate_limit_per_minute": principal.rate_limit,
        },
        "reports": [r["id"] for r in list_reports()],
        "tools_openai": tools_openai(principal),
        "dsl_cheatsheet": (
            'kind:video size:>1G modified:<7d ext:mp4;mkv -tag:archived "two words" '
            "meta.height:>=1080 (AND-combined; ~term = fuzzy)"
        ),
    }


@llm_app.get("/capabilities", operation_id="capabilities")
async def capabilities(
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _capabilities(principal, session)


@llm_app.get("/system-prompt", operation_id="system_prompt")
async def system_prompt(
    principal: LlmPrincipal = Depends(llm_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    caps = await _capabilities(principal, session)
    libs = caps["system"]["libraries"]
    prompt = render_system_prompt(
        principal,
        system_name="Filearr",
        item_count=caps["system"]["item_count"],
        library_names=libs if isinstance(libs, list) else [],
        agent_count=caps["system"]["agents"],
        report_ids=caps["reports"] if principal.allows("run_report") else [],
    )
    etag = '"' + hashlib.sha256(prompt.encode()).hexdigest()[:16] + '"'
    return Response(
        content=prompt,
        media_type="text/plain; charset=utf-8",
        headers={"ETag": etag},
    )
