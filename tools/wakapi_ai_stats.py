#!/usr/bin/env python3
"""Update a separate README AI-activity block from Wakapi heartbeats."""

from __future__ import annotations

import base64
import json
import math
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
LOCAL_USAGE_DIVIDER = "Total: "
LEGACY_USAGE_DIVIDER = "── Local provider / model usage · token share ──"
COMMIT_PREFIX = "Commits: "


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


def _fetch_month(api_base: str, api_key: str) -> tuple[dict, list[dict], datetime, datetime]:
    # Wakapi's `month` interval starts on the first day in the account timezone.
    stats_payload = _get_json(f"{api_base}/users/current/stats/month", api_key)
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


def reported_model(heartbeat: dict) -> str:
    """Recover the version Wakapi drops when parsing a model/version UA token.

    Wakapi's compatibility API puts the raw user-agent string in user_agent_id.
    Only match the reported model family; never mistake harness/runtime versions
    for model versions or infer an API provider from a model brand.
    """
    model = str(heartbeat.get("ai_model") or "").strip()
    if not model:
        return ""
    user_agent = str(heartbeat.get("user_agent") or heartbeat.get("user_agent_id") or "")
    for token in user_agent.split():
        family, separator, version = token.partition("/")
        if (separator and family.casefold() == model.casefold()
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", version)
                and version.casefold() not in {"unknown", "none", "null"}):
            return f"{family}/{version}"
    return model


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

        model = reported_model(heartbeat)
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
    lines = ["🤖 **AI Coding This Month**", "", "```text", "Wakapi telemetry", ""]
    if summary["ai_event_count"] == 0 and not (summary["ai_seconds"] or 0):
        lines.append("No AI coding telemetry was returned by Wakapi for this month.")
    else:
        ai_time = _duration(summary["ai_seconds"])
        if summary["ai_percent"] is not None and summary["ai_seconds"] is not None:
            lines.append(f"⏱ AI Coding Time: {ai_time} ({summary['ai_percent']:.2f}%)")
        else:
            lines.append(f"⏱ AI Coding Time: {ai_time}")

        if summary["ai_event_count"]:
            lines.append(
                "\nLines changed: "
                f"{summary['ai_lines'] + summary['human_lines']:,}"
            )
            lines.append(
                "\n🔤 "
                f"{summary['input_tokens']:,} Input Tokens, {summary['output_tokens']:,} Output Tokens"
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
            lines.append(f"\n🧠 {sessions} AI Sessions · {prompts}")

            model_rows = sorted(
                ((name, values) for name, values in summary["models"].items()
                 if values["ai_lines"] or values["input_tokens"] or values["output_tokens"]),
                key=lambda item: item[1]["ai_lines"],
                reverse=True,
            )
            if model_rows:
                total_lines = sum(max(0, row["ai_lines"]) for _, row in model_rows)
                lines.append("\nWakapi models · share of AI lines")
                for model, values in model_rows[:5]:
                    percent = max(0, values["ai_lines"]) / total_lines * 100 if total_lines else 0
                    filled = round(percent / 100 * 25)
                    bar = "█" * filled + "░" * (25 - filled)
                    lines.append(
                        f"{_model_label(model):<24} "
                        f"{values['ai_lines']:>9,} lines  {bar}  {percent:6.2f} %"
                    )
                if len(model_rows) > 5:
                    lines.append(f"... and {len(model_rows) - 5} more models")
        else:
            lines.append("Wakapi reported AI coding time but no AI session/token heartbeat fields.")

    lines.extend(["```", ""])
    return "\n".join(lines)


def render_coding_month(stats: dict) -> str:
    """Render coding durations from the same calendar-month stats as AI activity."""
    lines = ['📊 **This Month I Spent My Time On**', '', '```text',
             f"Time Zone: {_model_label(str(stats.get('timezone') or 'UTC'))}"]
    for key, label in [('languages', 'Programming Languages'), ('editors', 'Editors'),
                       ('operating_systems', 'Operating System')]:
        lines.extend(['', label + ':'])
        entries = stats.get(key, [])
        if not isinstance(entries, list):
            raise ValueError('Wakapi monthly coding entries must be a list')
        entries = [entry for entry in entries if isinstance(entry, dict)]
        entries.sort(key=lambda entry: _as_float(entry.get('total_seconds')), reverse=True)
        if not entries:
            lines.append('No activity this month.')
        for entry in entries[:5]:
            seconds = _as_float(entry.get('total_seconds'))
            percent = _as_float(entry.get('percent'))
            if not math.isfinite(seconds) or not math.isfinite(percent):
                raise ValueError('Wakapi monthly coding values must be finite')
            percent = min(100, max(0, percent))
            filled = round(percent / 100 * 25)
            bar = '█' * filled + '░' * (25 - filled)
            name = _model_label(str(entry.get('name', 'unknown')))
            lines.append(f'{name:<24} {_duration(max(0, seconds)):<20} {bar} {percent:6.2f} %')
    lines.extend(['```', ''])
    return '\n'.join(lines)


def replace_coding_month(readme: str, content: str) -> str:
    """Replace only coding-time stats, never the repository language breakdown."""
    start_marker, end_marker = '<!--START_SECTION:waka-->', '<!--END_SECTION:waka-->'
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise ValueError('README must contain exactly one waka marker pair')
    start = readme.index(start_marker) + len(start_marker)
    end = readme.index(end_marker)
    if end < start:
        raise ValueError('README waka markers are out of order')
    section = readme[start:end]
    pattern = r'[^\n]*\*\*This (?:Week|Month) I Spen[dt] My Time On\*\*[^\n]*\n\s*```text\n.*?\n```'
    section, count = re.subn(pattern, lambda _: content.rstrip(), section, flags=re.DOTALL)
    if count != 1:
        raise ValueError('README must contain exactly one coding-time stats block')
    return readme[:start] + section + readme[end:]


def replace_section(readme: str, content: str) -> str:
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise ValueError("README must contain exactly one AI coding section marker pair")
    start = readme.index(START_MARKER) + len(START_MARKER)
    end = readme.index(END_MARKER)
    if end < start:
        raise ValueError("README AI coding section markers are out of order")
    return readme[:start] + "\n" + content.rstrip() + "\n" + readme[end:]


def render_providers(snapshot: dict) -> str:
    """Render plain text for the shared AI block; keep local and Wakapi metrics distinct."""
    rows = snapshot.get("rows", [])
    lines = []
    commits = snapshot.get('github_commits')
    if type(commits) is int and commits >= 0:
        lines.append(f'{COMMIT_PREFIX}{commits:,}')
    def total(row: dict) -> int:
        return sum(max(0, _as_int(row.get(key))) for key in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    ordered = sorted((row for row in rows if isinstance(row, dict)), key=total, reverse=True)
    all_tokens = sum(total(row) for row in ordered)
    responses = sum(max(0, _as_int(row.get("responses"))) for row in ordered)

    def cost_info(row: dict) -> tuple[float | None, int]:
        value = row.get("cost_usd")
        covered = max(0, _as_int(row.get("cost_responses")))
        count = max(0, _as_int(row.get("responses")))
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0 or not 0 < covered <= count):
            return None, 0
        return float(value), covered

    costs = [cost_info(row) for row in ordered]
    covered = sum(count for _, count in costs)
    cost = sum(value for value, _ in costs if value is not None) if covered else None
    def compact_number(value: int) -> str:
        for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
            if value >= scale:
                return f"{value / scale:.2f}{suffix}"
        return str(value)

    def compact_cost(value: float | None, covered: int, count: int) -> str:
        if value is None:
            return "N/A"
        amount = f"${value:,.2f}" if value == 0 or value >= 0.01 else "<$0.01"
        return amount

    lines.extend([
        f"{LOCAL_USAGE_DIVIDER}{compact_number(all_tokens)} tokens · "
        f"{compact_cost(cost, covered, responses)}", "",
    ])
    for row in ordered[:12]:
        label = _model_label(str(row.get('model', 'unknown')))
        agent = _model_label(str(row.get('agent', 'unknown')))
        name = f"{label} [{agent}]"
        if len(name) > 30:
            name = name[:29] + "…"
        percent = total(row) / all_tokens * 100 if all_tokens else 0
        filled = round(percent / 100 * 15)
        bar = "█" * filled + "░" * (15 - filled)
        amount = compact_cost(*cost_info(row), max(0, _as_int(row.get('responses'))))
        lines.append(
            f"{name:<30} {compact_number(total(row)):>7}  {bar} {percent:5.1f}% {amount:>9}"
        )
    if not ordered:
        lines.append("No local session usage exported for this period.")
    if len(ordered) > 12:
        lines.append(f"Showing 12 of {len(ordered)} provider/model rows.")
    lines.append("```")
    return "\n".join(lines)


def combine_ai_usage(content: str, local_usage: str) -> str:
    """Keep both sources inside one text fence, replacing an earlier local block."""
    base = content
    for divider in (LOCAL_USAGE_DIVIDER, LEGACY_USAGE_DIVIDER):
        base = base.split(divider, 1)[0]
    base = base.strip()
    if base.endswith("```"):
        base = base[:-3].rstrip()
    if "```text" not in base:
        raise ValueError("AI coding content must contain a text code block")
    heading, body = base.split("```text", 1)
    if "AI Coding This Month" not in heading:
        # Never relabel a previously exported weekly Wakapi total as monthly.
        # The next trusted Wakapi refresh supplies the month-to-date total.
        heading, body = "🤖 **AI Coding This Month**", ""
    # With local usage present, keep only Wakapi's distinct line/time metrics.
    # Avoid two token totals, two model charts, and unavailable-time placeholders.
    summary = [line for line in body.splitlines()
               if line.startswith(("✍", "Lines changed:"))
               or (line.startswith("⏱") and "not reported" not in line)]
    summary = [re.sub(r'^✍\ufe0f?\s*', '', line) for line in summary]
    # Migrate already-published line counts without fetching fresh telemetry.
    summary = [re.sub(
        r"AI ([\d,]+) · human ([\d,]+)",
        lambda match: f"{int(match[1].replace(',', '')) + int(match[2].replace(',', '')):,}",
        line,
    ) for line in summary]
    base = heading.rstrip() + "\n\n```text\n" + "\n".join(summary)
    usage_lines = local_usage.rstrip().splitlines()
    commit_lines = [line for line in usage_lines if line.startswith(COMMIT_PREFIX)]
    if commit_lines:
        base = base.rstrip() + "\n" + commit_lines[0]
    usage = "\n".join(line for line in usage_lines if not line.startswith(COMMIT_PREFIX))
    if "```" not in usage:  # Compatibility with previously exported plain-text blocks.
        usage += "\n```"
    return base.rstrip() + "\n\n" + usage + "\n"


def normalize_layout(readme: str, updated_date: str) -> str:
    """Show AI before weekly stats and keep the date below all generated blocks."""
    readme = readme.replace("### AI coding activity\n\n", "")
    readme = re.sub(r'(?m)^\*\*(I Mostly Code in [^\n]+)\*\*', r'💻 **\1**', readme)
    for label in ('Time Zone', 'Programming Languages', 'Editors', 'Operating System'):
        readme = re.sub(r'(?m)^[^\w\n]*(' + re.escape(label) + r':)', r'\1', readme)
    # Upstream may regenerate these sections; strip them in the existing postprocessor.
    readme = re.sub(
        r"^[^\n]*\*\*I'm (?:an Early[^\n]*|a Night[^\n]*|Most Productive on [^\n]*)"
        r"\*\*[ \t]*\n\s*```text\n.*?\n```[ \t]*\n*",
        "", readme, flags=re.MULTILINE | re.DOTALL,
    )
    readme = re.sub(r"\n+[ \t]*Last Updated on [^\n]+\n*", "\n\n", readme)
    # Relocate the complete AI section (including its compatibility markers),
    # rather than copying it, so both generators continue to find one marker pair.
    waka_start = "<!--START_SECTION:waka-->"
    ai_block = re.search(
        r"<!--START_SECTION:ai-coding-->.*?<!--END_SECTION:ai-coding-->"
        r"(?:\s*<!--START_SECTION:ai-providers-->.*?<!--END_SECTION:ai-providers-->)?",
        readme, flags=re.DOTALL,
    )
    if ai_block and waka_start in readme and readme.index(waka_start) < ai_block.start():
        block = ai_block.group()
        readme = readme[:ai_block.start()].rstrip() + "\n\n" + readme[ai_block.end():].lstrip()
        position = readme.index(waka_start)
        readme = readme[:position] + block + "\n\n" + readme[position:]
    footer_marker = max(
        (END_MARKER, "<!--END_SECTION:ai-providers-->", "<!--END_SECTION:waka-->"),
        key=readme.rfind,
    )
    return readme.replace(
        footer_marker,
        f"{footer_marker}\n\n Last Updated on {updated_date} UTC",
        1,
    ).rstrip() + "\n"


def update_readme(path: str, content: str, coding_content: str | None = None) -> bool:
    with open(path, encoding="utf-8") as readme_file:
        original = readme_file.read()
    # A Wakapi refresh must retain the already-exported local aggregates.
    if original.count(START_MARKER) == 1 and original.count(END_MARKER) == 1:
        current = original.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
        divider = next((marker for marker in (LOCAL_USAGE_DIVIDER, LEGACY_USAGE_DIVIDER)
                        if marker in current), None)
        if divider is not None:
            local = current.split(divider, 1)[1].rstrip()
            if local.endswith("```"):
                local = local[:-3].rstrip()
            # The count comes from the trusted local export, not Wakapi heartbeats.
            commit_line = next((line for line in current.splitlines()
                                if line.startswith(COMMIT_PREFIX)), None)
            usage = divider + local
            if commit_line:
                usage = commit_line + '\n' + usage
            content = combine_ai_usage(content, usage)
    updated = replace_section(original, content)
    if coding_content is not None:
        updated = replace_coding_month(updated, coding_content)
    updated = normalize_layout(
        updated,
        datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S"),
    )
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
        stats, heartbeats, start, end = _fetch_month(api_base, api_key)
        summary = aggregate(stats, heartbeats, start, end)
        content = render(summary)
        changed = update_readme("README.md", content, render_coding_month(stats))
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
