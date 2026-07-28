# LLM integration (M1) — connecting Ollama, OpenWebUI, and MCP clients

Filearr exposes a role-gated **LLM tool facade** at `/api/llm/v1` so a
model can search the catalog, look up where files live, run reports, and
(with the right role) read extracted document text — RAG over your file
estate. Design: `docs/research/llm-rag-integration.md`.

## 1. Mint an LLM key

Console → Admin → **LLM access keys** → Mint key (or
`POST /api/v1/llm-keys` with an admin key). Pick a role:

| Role | Tools | Reads content | Sees paths |
|---|---|---|---|
| `librarian` | search, get_file, where_is, filter, reports, aggregate, overview | no | yes |
| `analyst` | librarian + `read_content` | **yes** | yes |
| `guest` | search, get_file, overview (no snippets) | no | **no** |
| `auditor` | reports, aggregate, overview | no | yes |

Optional per-key: expiry, ltree path scope, library allow-list, rate limit
(default 60 calls/min). The key is shown once. Enforcement is server-side —
the prompt describes the boundary, the handlers ARE the boundary.

## 2. Get the system prompt

```bash
curl -H "Authorization: Bearer $KEY" https://filearr.example.com/api/llm/v1/system-prompt
```

This returns a role-accurate prompt (capabilities, query-grammar cheatsheet,
citation + injection rules). It is generated from the key's actual role, so
it never drifts from what's enforced. `GET /capabilities` returns the same
information as JSON — including `tools_openai`, the tool list pre-rendered
for OpenAI-style `tools=[...]` calls.

## 3. OpenWebUI (recommended front end)

1. Admin Panel → Settings → **External Tools** → `+`
   - URL: `https://filearr.example.com/api/llm/v1`
     (OpenWebUI fetches `openapi.json` from it — the facade serves a trimmed
     spec containing only the tool operations)
   - Auth: Bearer → paste the LLM key.
2. Create a model preset (Workspace → Models): pick a tool-capable model
   (Qwen 2.5 ≥7B, Llama 3.1 ≥8B, Mistral-Nemo), paste the `/system-prompt`
   output into its System Prompt, and enable the Filearr tool server.
   Prefer "native" function calling on models that support it.
3. Ask: *"where do my 2023 tax PDFs live?"* — the model calls
   `search_files` → `where_is` and answers with library/host/share URL.

## 4. Bare Ollama

Ollama's `/api/chat` supports `tools`. Use the reference loop:

```bash
python docs/examples/ollama_filearr_loop.py \
  --filearr https://filearr.example.com --key $KEY \
  --model qwen2.5:7b "what are the biggest video files?"
```

The loop fetches `/capabilities`, passes `tools_openai` to Ollama, executes
returned tool calls against the facade, and feeds results back until the
model answers. For chat-only use, bake the system prompt into a Modelfile:

```
FROM qwen2.5:7b
SYSTEM """<paste /system-prompt output>"""
```

## 5. MCP clients (Claude Desktop/Code, mcpo)

Use any OpenAPI→MCP bridge; the facade is a standard OpenAPI tool server.
With OpenWebUI's `mcpo` you can also bridge the other direction. A native
in-repo `filearr-mcp` adapter is planned (design §2, M-followup).

## 6. Security posture

- Keys are read-only; rate-limited (429 + Retry-After); every tool call is
  written to the audit log (`LLM_TOOL_CALL`, with tool, role, row count).
- Path/library scope is applied inside Meilisearch filters and SQL WHERE
  clauses — out-of-scope files 404 as "not visible to this role".
- `read_content` serves only the sanitized extracted-text store
  (`body_text`/`ocr_text`), never raw file bytes.
- Retrieved names/paths/text are untrusted data; the generated prompt
  instructs the model to report, not follow, instructions found in them.
  Layered defense: even a hijacked model can only call read-only, scoped,
  audited tools.
