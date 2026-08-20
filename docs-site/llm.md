# LLM integration

Filearr can act as a **tool backend for LLMs** — ask a local model *"where
are my 2023 tax PDFs?"* or *"summarize what's in the training library"* and
it answers from your catalog with citations. The integration is a compact,
role-gated tool facade at `/api/llm/v1` that works with OpenWebUI, Ollama,
and any OpenAI-compatible or MCP tool-calling client.

## Quick start

1. **Mint a key**: Admin page → *LLM access keys* → Mint. Pick the owning
   **service account** (like plain API keys — disable the account to cut all
   its keys at once, delete it to revoke them) and a role:
   `librarian` (search/where, no content), `analyst` (adds document-text
   reading + passage retrieval for RAG), `guest` (filenames only, no
   paths), `auditor` (reports only), `curator` (analyst plus bounded
   writes: tags and notes — PATCH-only, never deletes, always audited).
   Optional expiry, path scope, library allow-list, rate limit. The key is
   shown once; the key list shows each key's audited tool-call count.
2. **Fetch the system prompt** (it is generated from the key's actual role,
   so the model's stated capabilities always match what the server
   enforces):
   ```bash
   curl -H "Authorization: Bearer $KEY" https://your-host/api/llm/v1/system-prompt
   ```
3. **Connect a client**:
   - **OpenWebUI**: Settings → External Tools → add
     `https://your-host/api/llm/v1` with the Bearer key (it imports the
     facade's trimmed OpenAPI spec automatically), then paste the system
     prompt into your model preset. Use a tool-capable model (Qwen 2.5
     ≥7B, Llama 3.1 ≥8B).
   - **Ollama (bare)**: use the stdlib-only reference loop at
     `docs/examples/ollama_filearr_loop.py` — it fetches the tool specs
     from `/capabilities` and runs the call-execute-answer loop.
   - **MCP clients**: bridge the OpenAPI spec with `mcpo` or any
     OpenAPI-to-MCP adapter.

## The tools

`search_files` (keyword/semantic/hybrid; `search_in=content|names` restricts
what the query text matches), `find_similar` (content-similar files by local
text embedding, each with a 0..1 `similarity`), `get_file`, `where_is` (library,
host, share URL), `read_content` (extracted document text, content roles
only), `retrieve_passages` (ranked in-document passages — see below),
`filter_files` (the same query grammar the console uses), `run_report`,
`aggregate`, `catalog_overview`, and — `curator` role only — `tag_files`
and `annotate` (add/remove tags, set a note; PATCH of user metadata only).

## Content-RAG: retrieve_passages

For "answer from *inside* my documents" questions, Filearr chunks extracted
text (PDF/DOCX/TXT/MD bodies, OCR results) into ~1,000-character passages,
stores them in Postgres, and projects them into a second search index —
semantic when the local embedder is enabled, keyword otherwise. It is a
**per-library opt-in**:

1. Edit the library → Content processing → enable **RAG chunking**.
2. Jobs page → **Chunk documents for RAG** → Run now (repeat until it
   defers 0; new scans keep chunks current automatically).
3. `analyst`/`curator` keys can then call `retrieve_passages` — each hit
   carries the file citation, so answers stay grounded.

The chunk store rides the same disposable-projection rule as the main
index: **Rebuild the passages search index** (Jobs page) re-projects it
from Postgres without re-embedding.

!!! note "Enforcement is server-side"
    Roles, path/library scope, result caps, and rate limits are enforced in
    the API handlers — the system prompt describes the boundary but never
    IS the boundary. Every tool call lands in the audit log (the Admin key
    list shows per-key call counts), and every curator write additionally
    records an item-version row attributed to the key. `read_content` and
    `retrieve_passages` only ever serve sanitized extracted text, never raw
    file bytes.

See `docs/ops/llm.md` in the repository for the full runbook and
`docs/research/llm-rag-integration.md` for the design.
