# LLM integration

Filearr can act as a **tool backend for LLMs** — ask a local model *"where
are my 2023 tax PDFs?"* or *"summarize what's in the training library"* and
it answers from your catalog with citations. The integration is a compact,
role-gated tool facade at `/api/llm/v1` that works with OpenWebUI, Ollama,
and any OpenAI-compatible or MCP tool-calling client.

## Quick start

1. **Mint a key**: Admin page → *LLM access keys* → Mint. Pick a role:
   `librarian` (search/where, no content), `analyst` (adds document-text
   reading for RAG), `guest` (filenames only, no paths), `auditor`
   (reports only). Optional expiry, path scope, library allow-list, rate
   limit. The key is shown once.
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

`search_files` (keyword/semantic/hybrid), `get_file`, `where_is` (library,
host, share URL), `read_content` (extracted document text, analyst only),
`filter_files` (the same query grammar the console uses), `run_report`,
`aggregate`, `catalog_overview`.

!!! note "Enforcement is server-side"
    Roles, path/library scope, result caps, and rate limits are enforced in
    the API handlers — the system prompt describes the boundary but never
    IS the boundary. Every tool call lands in the audit log. `read_content`
    only ever serves sanitized extracted text, never raw file bytes.

See `docs/ops/llm.md` in the repository for the full runbook and
`docs/research/llm-rag-integration.md` for the design (including the M2
passage-retrieval roadmap).
