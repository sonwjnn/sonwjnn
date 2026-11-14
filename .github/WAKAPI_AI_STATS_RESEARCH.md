# WakaReadme AI stats compatibility

Checked 2026-11-14 against the pinned WakaReadme action and current upstream Wakapi/Orca sources.

## Findings

- The pinned `waka-readme-stats` action does not infer AI activity from editor time. Its AI block requires an `AI Coding` category **and** a non-zero `ai_sessions` value; otherwise it deliberately emits `No AI Coding Activity Tracked This Week`. When those are present, it reads AI/human additions and deletions, input/output tokens, estimated cost, prompt counts, session averages and model breakdown. See [the pinned formatter](https://github.com/anmol098/waka-readme-stats/blob/4db28e08bfd8bac570125799fe1cfdcb23a8c871/sources/main.py#L72-L120) and [action README](https://github.com/anmol098/waka-readme-stats/blob/4db28e08bfd8bac570125799fe1cfdcb23a8c871/README.md).

- The Orca WakaTime plugin can produce more than ordinary editor heartbeats: its [README](https://github.com/nknwns/orca-wakatime/blob/3913f84dc24a61b1f42f14686c9ab4372dccc14b/README.md#what-gets-tracked) says it sends `ai coding` heartbeats while agents work and uses `wakatime-cli --sync-ai-activity` after Claude/Codex agent turns. This suggests AI events can reach the configured WakaTime-compatible heartbeat endpoint, but it does not make Wakapi's aggregate stats response compatible with WakaReadme's AI block.

- Current [Wakapi heartbeat model](https://github.com/muety/wakapi/blob/e511f6815da4ef879c425219d298d6ece48df673/models/heartbeat.go#L44-L75) accepts raw fields for AI model, session, line changes, human line changes, input/output tokens and prompt length. The [WakaTime-compatible heartbeat response](https://github.com/muety/wakapi/blob/e511f6815da4ef879c425219d298d6ece48df673/models/compat/wakatime/v1/heartbeat.go#L62-L108) preserves these, and the [compat heartbeat route](https://github.com/muety/wakapi/blob/e511f6815da4ef879c425219d298d6ece48df673/routes/compat/wakatime/v1/heartbeat.go#L36-L80) can return them.

- However, Wakapi's current [compat stats model](https://github.com/muety/wakapi/blob/e511f6815da4ef879c425219d298d6ece48df673/models/compat/wakatime/v1/stats.go) and [stats route](https://github.com/muety/wakapi/blob/e511f6815da4ef879c425219d298d6ece48df673/routes/compat/wakatime/v1/stats.go) do not aggregate/expose the AI fields WakaReadme expects (such as `ai_sessions`, `ai_model_total_cost`, and `ai_model_breakdown`). Wakapi's heartbeat model also has no cost field. So setting `SHOW_AI_CODING=True` alone cannot produce the screenshot's detailed metrics from the current `/stats` response.

- WakaTime's [pricing page](https://wakatime.com/pricing) lists AI adoption, model usage and spend under Premium. Its [AI dashboard](https://wakatime.com/ai) describes per-model lines, tokens and estimated cost across supported tools including Claude Code, Codex and Pi. This is the turnkey path WakaReadme targets, provided AI tracking is enabled and the action uses a WakaTime API key/URL. WakaTime describes cost as an estimate, not an invoice, in its [FAQ](https://wakatime.com/faq).

## Recommendation

1. **Turnkey:** use WakaTime's own AI tracking/API for the WakaReadme job; this likely requires Premium for the AI insights. The Orca plugin already uses the official `wakatime-cli` AI sync path, but the tracker must send data to WakaTime rather than only Wakapi. WakaReadme reads one stats API source, so it will not automatically combine Wakapi's language stats with WakaTime's AI stats.
2. **Keep Wakapi:** use a small custom weekly-heartbeat aggregator and write the AI card in a separate README marker. The raw Wakapi fields support time, model, sessions, tokens and line-change reporting if the client sends them; prompt-bearing events are derived. Cost is omitted per the user's choice because Wakapi does not provide it. This is DIY, not a drop-in WakaReadme setting. The updater should leave unavailable fields clearly marked rather than inventing values.
3. **No new integration:** leave the current fallback visible or set `SHOW_AI_CODING=False` to hide it.
