#!/usr/bin/env python3
"""Reference agent loop: Ollama tool-calling against the Filearr LLM facade.

Usage:
    python ollama_filearr_loop.py --filearr https://filearr.example.com \
        --key <llm-api-key> [--model qwen2.5:7b] [--ollama http://localhost:11434] \
        "where are my biggest video files?"

Flow (design docs/research/llm-rag-integration.md §2 transport C):
  1. GET /api/llm/v1/capabilities  -> tools_openai (pre-rendered tool specs)
  2. GET /api/llm/v1/system-prompt -> role-accurate system prompt
  3. chat loop: send messages+tools to Ollama; execute any tool_calls against
     the facade; append results; repeat until the model answers in prose.

Stdlib only — no pip installs needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def http_json(url: str, *, key: str | None = None, body: dict | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def http_text(url: str, *, key: str) -> str:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filearr", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("question")
    args = ap.parse_args()

    base = args.filearr.rstrip("/") + "/api/llm/v1"
    caps = http_json(f"{base}/capabilities", key=args.key)
    tools = caps["tools_openai"]
    system = http_text(f"{base}/system-prompt", key=args.key)
    print(f"[role: {caps['role']['name']}; {len(tools)} tools]", file=sys.stderr)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": args.question},
    ]
    for _ in range(args.max_steps):
        resp = http_json(
            f"{args.ollama}/api/chat",
            body={"model": args.model, "messages": messages, "tools": tools, "stream": False},
        )
        msg = resp["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            print(msg.get("content", ""))
            return 0
        for call in calls:
            fn = call["function"]["name"]
            raw_args = call["function"].get("arguments") or {}
            if isinstance(raw_args, str):
                raw_args = json.loads(raw_args or "{}")
            print(f"[tool: {fn}({json.dumps(raw_args)[:120]})]", file=sys.stderr)
            try:
                result = http_json(f"{base}/{fn}", key=args.key, body=raw_args)
            except Exception as exc:  # surface tool errors to the model
                result = {"error": str(exc)}
            messages.append(
                {"role": "tool", "content": json.dumps(result)[:12000], "tool_name": fn}
            )
    print("(gave up after max steps)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
