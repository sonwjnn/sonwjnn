#!/usr/bin/env python3
"""Update a separate README AI-activity block from Wakapi heartbeats."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

START_MARKER = "<!--START_SECTION:ai-coding-->"
END_MARKER = "<!--END_SECTION:ai-coding-->"
TIMEOUT_SECONDS = 20


def _parse_time(value: str) -> datetime:
    """Parse an RFC3339 timestamp returned by Wakapi."""
    if not value:
        raise ValueError("Wakapi stats response is missing a time range")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _api_base(configured_url: str) -> str:
    parsed = urlparse(configured_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WAKAPI_API_URL must be an HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("WAKAPI_API_URL must not include credentials, a query, or a fragment")

    path = parsed.path.rstrip("/")
    if path.endswith("/compat/wakatime/v1"):
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    if path.endswith("/api"):
        return f"{parsed.scheme}://{parsed.netloc}{path}/compat/wakatime/v1"
    raise ValueError("WAKAPI_API_URL must end in /api/compat/wakatime/v1/ or /api/")


def _get_json(url: str, api_key: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Basic "
            + base64.b64encode(api_key.encode("utf-8")).decode("ascii"),
            "User-Agent": "sonwjnn-wakapi-ai-readme/1.0",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"Wakapi returned HTTP {error.code}; response body omitted") from None
    except URLError:
        raise RuntimeError("Could not connect to the configured Wakapi API") from None
    except (TimeoutError, OSError):
        raise RuntimeError("The Wakapi API request timed out or failed") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Wakapi returned invalid JSON") from None

    if not isinstance(payload, dict):
        raise RuntimeError("Wakapi returned an unexpected JSON shape")
    return payload


def _dates_in_range(start: datetime, end: datetime, timezone_name: str) -> list[date]:
    try:
        local_tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError):
        local_tz = timezone.utc
    first = start.astimezone(local_tz).date()
    last = end.astimezone(local_tz).date()
    days: list[date] = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days


def _fetch_week(api_base: str, api_key: str) -> tuple[dict, list[dict], datetime, datetime]:
    stats_payload = _get_json(f"{api_base}/users/current/stats/last_7_days", api_key)
    stats = stats_payload.get("data")
    if not isinstance(stats, dict):
        raise RuntimeError("Wakapi stats response is missing its data object")

    start = _parse_time(stats.get("start", ""))
    end = _parse_time(stats.get("end", ""))
    timezone_name = stats.get("timezone") or "UTC"
    heartbeats: list[dict] = []
    for day in _dates_in_range(start, end, timezone_name):
        query = urlencode({"date": day.isoformat()})
        payload = _get_json(
            f"{api_base}/users/current/heartbeats?{query}",
            api_key,
        )
        entries = payload.get("data", [])
        if not isinstance(entries, list):
            raise RuntimeError("Wakapi heartbeat response has an unexpected data shape")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            timestamp = _as_float(entry.get("time"))
            if start.timestamp() <= timestamp < end.timestamp():
                heartbeats.append(entry)
    return stats, heartbeats, start, end


def _category_ai_seconds(stats: dict) -> tuple[float | None, float | None]:
    categories = stats.get("categories", [])
    if not isinstance(categories, list):
        return None, None
    for category in categories:
        if not isinstance(category, dict):
            continue
        label = str(category.get("text") or category.get("name") or "").strip().casefold()
        if label == "ai coding":
            percent = category.get("percent")
            return _as_float(category.get("total_seconds")), (
                _as_float(percent) if percent is not None else None
            )
    return None, None


def aggregate(stats: dict, heartbeats: list[dict], start: datetime, end: datetime) -> dict:
    """Aggregate raw AI heartbeat fields without inventing missing values."""
    ai_seconds, ai_percent = _category_ai_seconds(stats)
    sessions: set[str] = set()
    models: dict[str, dict[str, int]] = defaultdict(
        lambda: {"ai_lines": 0, "input_tokens": 0, "output_tokens": 0, "events": 0}
    )
    ai_lines = 0
    human_lines = 0
    input_tokens = 0
    output_tokens = 0
    prompt_events = 0
    ai_event_count = 0

    for heartbeat in heartbeats:
        timestamp = _as_float(heartbeat.get("time"))
        if not start.timestamp() <= timestamp < end.timestamp():
            continue

        model = str(heartbeat.get("ai_model") or "").strip()
        session = str(heartbeat.get("ai_session") or "").strip()
        event_ai_lines = _as_int(heartbeat.get("ai_line_changes"))
        event_input = _as_int(heartbeat.get("ai_input_tokens"))
        event_output = _as_int(heartbeat.get("ai_output_tokens"))
        prompt_length = _as_int(heartbeat.get("ai_prompt_length"))
        is_ai_event = bool(model or session or event_ai_lines or event_input or event_output or prompt_length)

        human_lines += _as_int(heartbeat.get("human_line_changes"))
        if not is_ai_event:
            continue

        ai_event_count += 1
        if session:
            sessions.add(session)
        ai_lines += event_ai_lines
        input_tokens += event_input
        output_tokens += event_output
        if prompt_length > 0:
            prompt_events += 1

        model_key = model or "Model not reported"
        models[model_key]["ai_lines"] += event_ai_lines
        models[model_key]["input_tokens"] += event_input
        models[model_key]["output_tokens"] += event_output
        models[model_key]["events"] += 1

    return {
        "ai_seconds": ai_seconds,
        "ai_percent": ai_percent,
        "ai_lines": ai_lines,
        "human_lines": human_lines,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "sessions": len(sessions),
        "sessions_reported": bool(sessions),
        "prompt_events": prompt_events,
        "prompt_events_reported": prompt_events > 0,
        "ai_event_count": ai_event_count,
        "models": dict(models),
    }


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "not reported"
    minutes = max(0, round(seconds / 60))
    hours, remaining = divmod(minutes, 60)
    return f"{hours} hrs {remaining} mins"


def _model_label(value: str) -> str:
    # Heartbeat metadata is user-controlled; keep it safe inside a Markdown code block.
    return re.sub(r"[^A-Za-z0-9._:/+ -]", "", value).strip()[:48] or "Unknown model"


def render(summary: dict) -> str:
    lines = ["🤖 **AI Coding This Week**", "", "```text"]
    if summary["ai_event_count"] == 0 and not (summary["ai_seconds"] or 0):
        lines.append("No AI coding telemetry was returned by Wakapi for this week.")
    else:
        ai_time = _duration(summary["ai_seconds"])
        if summary["ai_percent"] is not None and summary["ai_seconds"] is not None:
            lines.append(f"AI coding time: {ai_time} ({summary['ai_percent']:.2f}%)")
        else:
            lines.append(f"AI coding time: {ai_time}")

        if summary["ai_event_count"]:
            lines.append(
                "Lines changed: "
                f"AI {summary['ai_lines']:,} · human {summary['human_lines']:,}"
            )
            lines.append(
                "Token usage: "
                f"{summary['input_tokens']:,} input · {summary['output_tokens']:,} output"
            )
            sessions = (
                f"{summary['sessions']:,}"
                if summary["sessions_reported"]
                else "not reported"
            )
            prompts = (
                f"{summary['prompt_events']:,} prompt-bearing events"
                if summary["prompt_events_reported"]
                else "not reported"
            )
            lines.append(f"AI sessions: {sessions} · Prompts: {prompts}")

            model_rows = sorted(
                summary["models"].items(),
                key=lambda item: (
                    item[1]["input_tokens"] + item[1]["output_tokens"],
                    item[1]["ai_lines"],
                ),
                reverse=True,
            )
            if model_rows:
                lines.extend(["", "Models:"])
                for model, values in model_rows[:5]:
                    tokens = values["input_tokens"] + values["output_tokens"]
                    lines.append(
                        f"{_model_label(model):<30} "
                        f"{values['ai_lines']:,} AI lines · {tokens:,} tokens"
                    )
                if len(model_rows) > 5:
                    lines.append(f"... and {len(model_rows) - 5} more models")
        else:
            lines.append("Wakapi reported AI coding time but no AI session/token heartbeat fields.")

    lines.extend(["```", ""])
    return "\n".join(lines)


def replace_section(readme: str, content: str) -> str:
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise ValueError("README must contain exactly one AI coding section marker pair")
    start = readme.index(START_MARKER) + len(START_MARKER)
    end = readme.index(END_MARKER)
    if end < start:
        raise ValueError("README AI coding section markers are out of order")
    return readme[:start] + "\n" + content.rstrip() + "\n" + readme[end:]


def update_readme(path: str, content: str) -> bool:
    with open(path, encoding="utf-8") as readme_file:
        original = readme_file.read()
    updated = replace_section(original, content)
    if updated == original:
        return False
    with open(path, "w", encoding="utf-8") as readme_file:
        readme_file.write(updated)
    return True


def main() -> int:
    api_key = os.environ.get("WAKAPI_API_KEY", "").strip()
    configured_url = os.environ.get("WAKAPI_API_URL", "").strip()
    if not api_key or not configured_url:
        print("WAKAPI_API_KEY and WAKAPI_API_URL are required", file=sys.stderr)
        return 1

    try:
        api_base = _api_base(configured_url)
        stats, heartbeats, start, end = _fetch_week(api_base, api_key)
        summary = aggregate(stats, heartbeats, start, end)
        content = render(summary)
        changed = update_readme("README.md", content)
    except (ValueError, RuntimeError) as error:
        # Deliberately omit response bodies, request headers, and credentials.
        print(f"AI stats update failed: {error}", file=sys.stderr)
        return 1

    print(
        "AI stats update: "
        f"{summary['ai_event_count']} AI heartbeat events; "
        f"README {'updated' if changed else 'unchanged'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
