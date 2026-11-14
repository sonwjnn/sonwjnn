#!/usr/bin/env python3
"""Export aggregate AI usage and GitHub commit counts; never export raw records."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import subprocess


LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}")


def safe_label(value: object) -> str:
    if (isinstance(value, str) and LABEL.fullmatch(value)
            and not value.lower().startswith(("sk-", "waka_", "ghp_", "http:", "https:"))):
        return value
    return "unknown"


def timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value: object) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) and value >= 0 else 0


def recorded_cost(usage: dict) -> float | None:
    """Pi's recorded USD estimate, not a bill or an inferred token price."""
    cost = usage.get("cost")
    value = cost.get("total") if isinstance(cost, dict) else None
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        return float(value)
    return None


def records(path: Path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict):
                yield item


def events(path: Path, agent: str):
    """Yield an allowlisted internal usage record; IDs are only used for dedup."""
    session = str(path)  # Fallback stays internal; never serialized.
    provider = "unknown"
    model = "unknown"
    turns = {}
    for item in records(path):
        kind = item.get("type")
        if agent == "pi":
            if kind == "session":
                session = str(item.get("id") or session)
            message = item.get("message")
            if kind != "message" or not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            # Pi reports cache tokens separately from non-cached input.
            yield {
                "session": session, "id": message.get("responseId") or item.get("id"),
                "timestamp": item.get("timestamp"),
                "provider": safe_label(message.get("provider")),
                "model": safe_label(message.get("model")),
                "input": number(usage.get("input")), "output": number(usage.get("output")),
                "cache_read": number(usage.get("cacheRead")),
                "cache_write": number(usage.get("cacheWrite")),
                "cost_usd": recorded_cost(usage),
            }
        elif agent == "codex":
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                session = str(payload.get("id") or payload.get("session_id") or session)
                provider = safe_label(payload.get("model_provider"))
            elif kind == "turn_context":
                model = safe_label(payload.get("model"))
                turns[payload.get("turn_id")] = model
            elif kind == "token_usage_record":
                # `usage` is this response's delta; turn/thread usage are cumulative.
                usage = payload.get("usage")
                if not isinstance(usage, dict):
                    continue
                cached = number(usage.get("cached_input_tokens"))
                cache_write = number(usage.get("cache_write_input_tokens"))
                yield {
                    "session": session, "id": payload.get("response_id") or item.get("ordinal"),
                    "timestamp": item.get("timestamp"), "provider": provider,
                    "model": turns.get(payload.get("turn_id"), model),
                    # Codex input already includes cached tokens; do not count twice.
                    "input": max(0, number(usage.get("input_tokens")) - cached - cache_write),
                    "output": number(usage.get("output_tokens")),
                    "cache_read": cached, "cache_write": cache_write,
                }
        elif agent == "claude" and kind == "assistant":
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            model = safe_label(message.get("model"))
            if not isinstance(usage, dict) or model == "unknown":
                continue  # Error/synthetic messages are not model usage.
            yield {
                "session": str(item.get("sessionId") or session), "id": message.get("id"),
                "timestamp": item.get("timestamp"),
                # Do not infer Anthropic: Claude can route via Bedrock/Vertex/proxies.
                "provider": safe_label(message.get("provider")), "model": model,
                "input": number(usage.get("input_tokens")),
                "output": number(usage.get("output_tokens")),
                "cache_read": number(usage.get("cache_read_input_tokens")),
                "cache_write": number(usage.get("cache_creation_input_tokens")),
            }


def collect(home: Path, now: datetime) -> dict:
    # Calendar month-to-date in UTC, not a trailing 30-day window.
    now = now.astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    groups = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "responses": 0, "session_ids": set(),
        "cost_usd": Decimal(0), "cost_responses": 0,
    })
    seen = set()
    sources = {"pi": ".pi/agent/sessions", "codex": ".codex/sessions", "claude": ".claude/projects"}
    for agent, directory in sources.items():
        root = home / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            for event in events(path, agent):
                when = timestamp(event["timestamp"])
                if when is None or not start <= when < now:
                    continue
                if not any(event[key] for key in ("input", "output", "cache_read", "cache_write")):
                    continue
                identity = (agent, event["id"] or (event["session"], str(when)),
                            event["provider"], event["model"])
                if identity in seen:
                    continue
                seen.add(identity)
                group = groups[(agent, event["provider"], event["model"])]
                for source, target in [("input", "input_tokens"), ("output", "output_tokens"),
                                       ("cache_read", "cache_read_tokens"), ("cache_write", "cache_write_tokens")]:
                    group[target] += event[source]
                if event.get("cost_usd") is not None:
                    group["cost_usd"] += Decimal(str(event["cost_usd"]))
                    group["cost_responses"] += 1
                group["responses"] += 1
                group["session_ids"].add(event["session"])
    rows = []
    for (agent, provider, model), group in sorted(groups.items()):
        rows.append({"agent": agent, "provider": provider, "model": model,
                     **{k: v for k, v in group.items() if k not in ("session_ids", "cost_usd")},
                     "cost_usd": float(group["cost_usd"]) if group["cost_responses"] else None,
                     "sessions": len(group["session_ids"])})
    return {"version": 2, "source": "local-session-logs", "generated_at": now.isoformat(),
            "start": start.isoformat(), "end": now.isoformat(), "rows": rows}


def fetch_month_commits(start: datetime, end: datetime) -> int:
    """Fetch only GitHub's aggregate profile commit contributions for this range."""
    query = (
        'query($from: DateTime!, $to: DateTime!) {'
        ' user(login: "sonwjnn") {'
        ' contributionsCollection(from: $from, to: $to) { totalCommitContributions }'
        ' } }'
    )
    try:
        result = subprocess.run(
            ['gh', 'api', 'graphql', '-f', f'query={query}',
             '-f', f'from={start.isoformat()}', '-f', f'to={end.isoformat()}',
             '--jq', '.data.user.contributionsCollection.totalCommitContributions'],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('GitHub aggregate commit query failed; details omitted') from None
    if result.returncode:
        raise RuntimeError('GitHub aggregate commit query failed; details omitted')
    try:
        count = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError('GitHub returned an invalid aggregate commit count') from None
    if type(count) is not int or count < 0:
        raise RuntimeError('GitHub returned an invalid aggregate commit count')
    return count


def update_provider_section(readme: str, snapshot: dict) -> str:
    # Share one visual block without mixing the two sources' metrics.
    from wakapi_ai_stats import (
        START_MARKER, END_MARKER, combine_ai_usage, render_providers, replace_section,
    )
    start_marker = "<!--START_SECTION:ai-providers-->"
    end_marker = "<!--END_SECTION:ai-providers-->"
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise ValueError("README must contain exactly one ai-providers marker pair")
    start = readme.index(start_marker) + len(start_marker)
    end = readme.index(end_marker)
    if end < start:
        raise ValueError("Provider section markers are out of order")
    # Retain the legacy marker pair, empty, so existing automation stays compatible.
    readme = readme[:start] + "\n" + readme[end:]
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise ValueError("README must contain exactly one AI coding marker pair")
    content = readme.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
    return replace_section(readme, combine_ai_usage(content, render_providers(snapshot)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="assets/ai-providers.json")
    parser.add_argument("--readme", default="README.md")
    args = parser.parse_args()
    snapshot = collect(Path.home(), datetime.now(timezone.utc))
    snapshot['github_commits'] = fetch_month_commits(
        datetime.fromisoformat(snapshot['start']), datetime.fromisoformat(snapshot['end']),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    from wakapi_ai_stats import normalize_layout
    readme = Path(args.readme)
    updated = update_provider_section(readme.read_text(encoding="utf-8"), snapshot)
    updated = normalize_layout(updated, datetime.now(timezone.utc).strftime("%d %B %Y"))
    readme.write_text(updated, encoding="utf-8")
    print(f"Exported {len(snapshot['rows'])} aggregate provider/model rows; no raw session data exported.")


if __name__ == "__main__":
    main()
