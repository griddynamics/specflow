#!/usr/bin/env python3
"""Check if an OpenRouter model is accessible (not blocked)."""

import os
import sys
import json
import urllib.request
import urllib.error


def check_model(model: str) -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: OPENROUTER_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with one word only: yes"}],
        "max_tokens": 10,
    }).encode()

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"]
        print(f"OK — model is accessible. Response: {content!r}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body)
            error_msg = detail.get("error", {}).get("message") or body
        except json.JSONDecodeError:
            error_msg = body
        print(f"BLOCKED/ERROR — HTTP {e.code}: {error_msg}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <model>", file=sys.stderr)
        print(f"Example: {sys.argv[0]} openai/gpt-4o", file=sys.stderr)
        sys.exit(1)
    check_model(sys.argv[1])
