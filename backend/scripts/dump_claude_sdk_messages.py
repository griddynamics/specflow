#!/usr/bin/env python3
"""
Dump Claude Agent SDK messages and compare billing vs OpenRouter catalog.

Default mode is a **cache probe**: two ``query()`` calls in one SDK session with the
same large static ``system_prompt`` so turn 2 should show ``cache_read_input_tokens``.

Run from backend/ (where pyproject.toml lives):

  # Cache probe (default): turn 1 = cache write, turn 2 = cache read
  uv run scripts/dump_claude_sdk_messages.py --model anthropic/claude-haiku-4.5

  # Legacy single-turn smoke test
  uv run scripts/dump_claude_sdk_messages.py --single-turn --prompt "Reply: ok"

  # Pricing catalog only (no API call)
  uv run scripts/dump_claude_sdk_messages.py --pricing-only --model anthropic/claude-haiku-4.5

  # Full JSON export
  uv run scripts/dump_claude_sdk_messages.py -o /tmp/sdk-cache-probe.json

Environment (same as production OpenRouter provider):
  OPENROUTER_API_KEY, optional OPENROUTER_BASE_URL, OPENROUTER_APP_NAME
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from claude_agent_sdk import ClaudeAgentOptions, Message, query  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.openrouter_api import fetch_models  # noqa: E402
from app.services.openrouter_pricing import pricing_from_catalog_row  # noqa: E402
from app.services.providers.openrouter import OpenRouterProvider  # noqa: E402

# Bump when changing probe text so cache keys change intentionally.
_CACHE_PROBE_VERSION = "specflow-cache-probe-v1"

_COST_KEY_HINTS = (
    "cost",
    "usage",
    "price",
    "pricing",
    "token",
    "openrouter",
    "generation",
    "request_id",
    "id",
    "billing",
    "credit",
    "cache",
)


def build_cache_probe_system_prompt(*, target_chars: int = 28_000) -> str:
    """
    Large stable system prefix for prompt caching experiments.

    Anthropic-style caches need a sizable repeated prefix (often 4k+ tokens).
    ~4 chars/token → 28k chars is a safe default for Haiku/Sonnet via OpenRouter.
    """
    header = (
        f"{_CACHE_PROBE_VERSION}\n"
        "STATIC POLICY CORPUS — keep identical across turns in this session.\n"
        "Do not summarize this block unless asked; it exists to populate provider cache.\n\n"
    )
    paragraph = (
        "Engineering policy line: validate OpenRouter costs with GET /v1/models per-token "
        "rates; never bill from Claude SDK total_cost_usd on OpenRouter routes; persist "
        "input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens; "
        "compare native_tokens_cached on the OpenRouter dashboard after each probe run. "
    )
    lines = [header]
    while sum(len(x) for x in lines) < target_chars:
        lines.append(paragraph)
    footer = (
        "\n\nEnd of static policy corpus. For user turns, answer briefly (one short sentence).\n"
    )
    return "".join(lines) + footer


def _to_jsonable(obj: Any, *, depth: int = 0, max_depth: int = 12) -> Any:
    if depth > max_depth:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return {"__bytes__": obj.decode("utf-8", errors="replace")}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            "__type__": type(obj).__name__,
            **{f.name: _to_jsonable(getattr(obj, f.name), depth=depth + 1, max_depth=max_depth) for f in fields(obj)},
        }
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v, depth=depth + 1, max_depth=max_depth) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v, depth=depth + 1, max_depth=max_depth) for v in obj]
    if hasattr(obj, "__dict__"):
        return {
            "__type__": type(obj).__name__,
            **{k: _to_jsonable(v, depth=depth + 1, max_depth=max_depth) for k, v in vars(obj).items()},
        }
    return repr(obj)


def _path_has_cost_hint(path: str) -> bool:
    lower = path.lower()
    return any(h in lower for h in _COST_KEY_HINTS)


def _collect_cost_hint_paths(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if _path_has_cost_hint(p):
                out.append((p, v))
            out.extend(_collect_cost_hint_paths(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_collect_cost_hint_paths(v, f"{prefix}[{i}]"))
    return out


def _summarize_message(msg: Message, index: int) -> dict[str, Any]:
    payload = _to_jsonable(msg)
    return {
        "index": index,
        "python_type": type(msg).__name__,
        "payload": payload,
        "cost_related_paths": [
            {"path": p, "value": v} for p, v in _collect_cost_hint_paths(payload)
        ],
    }


def _build_openrouter_env(*, cache_1h: bool) -> dict[str, str]:
    api_key = (settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set. Export it or add to .env before running a live dump."
        )
    provider = OpenRouterProvider(
        api_key=api_key,
        base_url=(settings.OPENROUTER_BASE_URL or "").strip() or None,
        app_name=(settings.OPENROUTER_APP_NAME or "").strip() or None,
    )
    env = provider.get_environment_config()
    if cache_1h:
        env["ENABLE_PROMPT_CACHING_1H"] = "1"
    return env


async def _fetch_openrouter_pricing(model_id: str) -> dict[str, Any] | None:
    for row in await fetch_models(settings):
        if row.get("id") == model_id:
            return {
                "id": row.get("id"),
                "name": row.get("name"),
                "pricing": row.get("pricing"),
                "top_provider": row.get("top_provider"),
                "context_length": row.get("context_length"),
            }
    return None


def _result_from_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in reversed(summaries):
        if s["python_type"] == "ResultMessage" and isinstance(s.get("payload"), dict):
            return s["payload"]
    return None


async def _run_one_query(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_turns: int,
    workspace: Path,
    env: dict[str, str],
    resume: str | None,
    turn_label: str,
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        cwd=str(workspace),
        permission_mode="acceptEdits",
        allowed_tools=[],
        env=env,
        resume=resume,
    )
    summaries: list[dict[str, Any]] = []
    idx = 0
    print(f"\n{'#' * 72}\n# {turn_label}\n# resume={resume!r}\n# user_prompt={user_prompt!r}\n{'#' * 72}")
    async for message in query(prompt=user_prompt, options=options):
        summaries.append(_summarize_message(message, idx))
        idx += 1
    result = _result_from_summaries(summaries)
    return {
        "turn_label": turn_label,
        "resume_session_id": resume,
        "user_prompt": user_prompt,
        "message_count": len(summaries),
        "messages": summaries,
        "result": result,
        "session_id": (result or {}).get("session_id"),
    }


async def _run_cache_probe(
    *,
    model: str,
    system_prompt: str,
    max_turns: int,
    workspace: Path,
    cache_1h: bool,
    turn1_user: str,
    turn2_user: str,
) -> list[dict[str, Any]]:
    env = _build_openrouter_env(cache_1h=cache_1h)
    approx_tokens = len(system_prompt) // 4
    print(
        f"Cache probe: system_prompt_chars={len(system_prompt)} "
        f"(~{approx_tokens} tokens), cache_1h={cache_1h}"
    )
    turn1 = await _run_one_query(
        model=model,
        system_prompt=system_prompt,
        user_prompt=turn1_user,
        max_turns=max_turns,
        workspace=workspace,
        env=env,
        resume=None,
        turn_label="Turn 1 — expect cache_creation_input_tokens (write)",
    )
    session_id = turn1.get("session_id")
    if not session_id:
        print("WARNING: Turn 1 returned no session_id; Turn 2 cannot resume.", file=sys.stderr)
    turn2 = await _run_one_query(
        model=model,
        system_prompt=system_prompt,
        user_prompt=turn2_user,
        max_turns=max_turns,
        workspace=workspace,
        env=env,
        resume=session_id,
        turn_label="Turn 2 — expect cache_read_input_tokens (read)",
    )
    return [turn1, turn2]


def _usage_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    usage = result.get("usage") or {}
    return {
        "total_cost_usd_sdk": result.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "session_id": result.get("session_id"),
        "num_turns": result.get("num_turns"),
    }


def _print_turn_billing(
    turn: dict[str, Any],
    catalog: dict[str, Any] | None,
) -> None:
    result = turn.get("result")
    print(f"\n=== {turn['turn_label']} — billing summary ===")
    print(json.dumps(_usage_summary(result), indent=2, default=str))
    if not catalog or not result:
        return
    usage = result.get("usage") or {}
    row = {"id": catalog["id"], "pricing": catalog.get("pricing") or {}}
    pricing = pricing_from_catalog_row(row)
    est = pricing.estimate_usd(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )
    sdk_cost = result.get("total_cost_usd")
    print(
        json.dumps(
            {
                "openrouter_catalog_estimate_usd": round(est, 8),
                "sdk_total_cost_usd": sdk_cost,
                "ratio_sdk_over_catalog": (
                    round(float(sdk_cost) / est, 2) if sdk_cost and est > 0 else None
                ),
            },
            indent=2,
        )
    )


def _print_cache_probe_verdict(turns: list[dict[str, Any]]) -> None:
    if len(turns) < 2:
        return
    u1 = _usage_summary(turns[0].get("result"))
    u2 = _usage_summary(turns[1].get("result"))
    read2 = int(u2.get("cache_read_input_tokens") or 0)
    write1 = int(u1.get("cache_creation_input_tokens") or 0)
    print("\n=== Cache probe verdict ===")
    print(json.dumps({"turn1": u1, "turn2": u2}, indent=2))
    if write1 > 0 and read2 > 0:
        print("OK: Turn 1 wrote cache and Turn 2 read cache — compare OpenRouter dashboard usage_cache.")
    elif write1 > 0 and read2 == 0:
        print(
            "PARTIAL: Turn 1 wrote cache but Turn 2 shows no cache_read. "
            "Retry with --cache-1h, same model, or wait <5m between turns (5m TTL default)."
        )
    else:
        print(
            "MISS: Little or no cache activity. Increase --system-prompt-chars or use a model "
            "that supports Anthropic prompt caching via OpenRouter."
        )


def _print_summary_block(summary: dict[str, Any], *, filter_cost_keys: bool) -> None:
    idx = summary["index"]
    typ = summary["python_type"]
    print(f"\n{'=' * 72}")
    print(f"Message #{idx}  type={typ}")
    if summary["cost_related_paths"]:
        print("--- cost / usage / id hints ---")
        for row in summary["cost_related_paths"]:
            print(f"  {row['path']}: {json.dumps(row['value'], default=str)[:500]}")
    elif filter_cost_keys:
        return
    if not filter_cost_keys:
        print("--- full payload ---")
        text = json.dumps(summary["payload"], indent=2, default=str)
        print(text[:20000])
        if len(text) > 20000:
            print("... [truncated; use -o for full JSON] ...")


async def _async_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="anthropic/claude-haiku-4.5", help="OpenRouter model id")
    parser.add_argument(
        "--single-turn",
        action="store_true",
        help="One short query (old behavior); default is two-turn cache probe",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly one word: ok",
        help="User prompt for --single-turn only",
    )
    parser.add_argument(
        "--system-prompt-chars",
        type=int,
        default=28_000,
        help="Target size of static system prompt for cache probe (default: 28000)",
    )
    parser.add_argument(
        "--turn1-prompt",
        default=(
            "You have read the static policy corpus in your system instructions. "
            "Reply with exactly one word: ready"
        ),
        help="User message for cache probe turn 1",
    )
    parser.add_argument(
        "--turn2-prompt",
        default=(
            "Same session and same policy corpus as before. "
            "Reply with exactly one word: cached"
        ),
        help="User message for cache probe turn 2 (should hit cache read)",
    )
    parser.add_argument(
        "--cache-1h",
        action="store_true",
        help="Set ENABLE_PROMPT_CACHING_1H=1 in agent env (longer cache TTL)",
    )
    parser.add_argument("--max-turns", type=int, default=2, help="SDK max_turns cap per query()")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("/tmp/specflow-sdk-dump-ws"),
        help="cwd for the agent",
    )
    parser.add_argument("--filter-cost-keys", action="store_true")
    parser.add_argument("--pricing-only", action="store_true")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    if args.pricing_only:
        row = await _fetch_openrouter_pricing(args.model)
        if row is None:
            print(f"No model {args.model!r} in OpenRouter catalog", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    started = datetime.now(timezone.utc).isoformat()
    catalog = await _fetch_openrouter_pricing(args.model)

    if args.single_turn:
        env = _build_openrouter_env(cache_1h=args.cache_1h)
        print(f"Single-turn dump  model={args.model!r}")
        turn = await _run_one_query(
            model=args.model,
            system_prompt=args.prompt,
            user_prompt=args.prompt,
            max_turns=args.max_turns,
            workspace=args.workspace,
            env=env,
            resume=None,
            turn_label="Single turn",
        )
        turns = [turn]
    else:
        system_prompt = build_cache_probe_system_prompt(target_chars=args.system_prompt_chars)
        print(f"Cache-probe dump  model={args.model!r}")
        turns = await _run_cache_probe(
            model=args.model,
            system_prompt=system_prompt,
            max_turns=args.max_turns,
            workspace=args.workspace,
            cache_1h=args.cache_1h,
            turn1_user=args.turn1_prompt,
            turn2_user=args.turn2_prompt,
        )

    meta: dict[str, Any] = {
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "mode": "single_turn" if args.single_turn else "cache_probe",
        "cache_probe_version": _CACHE_PROBE_VERSION,
        "turns": turns,
    }

    if catalog:
        print("\n--- OpenRouter catalog pricing (GET /v1/models) ---")
        print(json.dumps(catalog, indent=2))

    for turn in turns:
        _print_turn_billing(turn, catalog)
        stream_events = [
            m for m in turn.get("messages", []) if m.get("python_type") == "StreamEvent"
        ]
        print(f"StreamEvent count ({turn['turn_label']}): {len(stream_events)}")

    if not args.single_turn:
        _print_cache_probe_verdict(turns)

    if args.filter_cost_keys or not args.single_turn:
        # In cache probe mode, only print cost-related message hints (verbose otherwise).
        show_full = args.single_turn and not args.filter_cost_keys
        for turn in turns:
            for s in turn.get("messages", []):
                _print_summary_block(s, filter_cost_keys=not show_full)

    if args.output:
        args.output.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote probe JSON to {args.output}")

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
