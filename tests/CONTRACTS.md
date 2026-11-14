# Statistics contracts

Define or change a contract here before adding its regression test. Tests use synthetic
records and mocked network/process boundaries; they must not read real sessions,
credentials, environment configuration, or call live services.

## Aggregate export

- `collect(home, now)` is a local-data operation. Its JSON snapshot has version,
  source, generated_at, start, end, and rows. The production exporter additionally
  attaches an integer `github_commits` from GitHub; it never publishes raw responses.
- The window is `[first day of now's UTC month, now in UTC)`, not seven or thirty
  trailing days. Missing timestamps, empty usage, and out-of-range records do not count.
- Each row contains only agent/provider/model labels, four nonnegative token counters,
  response/session counts, nullable recorded USD cost, and cost-response coverage.
  IDs, paths, prompts, messages, credentials, and raw logs never enter the snapshot.
- Deduplication is by response identity, including across forked logs. Session counts
  are unique within each row, not additive across models.
- Codex response deltas exclude cache from noncached input; cumulative thread/turn
  counters are not added. Model identity retains reported versions. Providers are
  never guessed from a model's name.
- Cost is recorded Pi USD cost only: finite nonnegative numbers (including zero).
  Missing/invalid/unsupported cost is null, not zero; coverage counts only known-cost
  responses. Deduplication also applies to cost.

## External adapters

- GitHub receives the exact snapshot start/end through `gh api graphql` and returns
  only `totalCommitContributions`. Only nonnegative JSON integers are accepted.
  Failures/timeouts/invalid responses fail closed without exposing response bodies;
  failed collection must not write output files.
- Wakapi uses `/stats/month`, follows its returned timezone and start/end, fetches
  the corresponding local calendar dates, and accepts heartbeats only in `[start,end)`.
- HTTP authentication is sent in a header, never the URL. API-base validation rejects
  embedded credentials, fragments, queries, and unsupported paths.
- Heartbeat aggregation keeps human/AI metrics separate internally, unique sessions,
  positive prompt-bearing events, and exact reported model versions. Missing telemetry
  stays unknown rather than fabricated zero-duration or guessed identities.

## README transformations

- Each generator owns its marker-bounded region. Unrelated content and repository
  language statistics survive updates unchanged; invalid/missing/reversed ownership
  markers are rejected instead of allowing an unrestricted replacement.
- Reapplying the same input is idempotent. A Wakapi refresh preserves local aggregates
  and the exported GitHub count; a local refresh does not duplicate existing content.
- Monthly migration never relabels old weekly telemetry as monthly data.
- Coding-time statistics use the same monthly Wakapi response as AI. Replacing them
  does not replace repository-language statistics or modify snapshot inputs.
- Generated Markdown has balanced fences and one marker pair per section.

## Deliberately not locked by tests

Exact headings, wording, punctuation, emoji, padding, decimal presentation, bar glyphs,
truncation width, and the choice/order of visible cosmetic subsections are presentation
choices. Validate those during the requested README change, not with permanent tests
that duplicate implementation strings.
