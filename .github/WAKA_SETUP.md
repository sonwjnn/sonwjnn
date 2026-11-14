# Coding activity setup

The profile uses [anmol098/waka-readme-stats](https://github.com/anmol098/waka-readme-stats) only as a README generator. It reads coding metrics from your [Wakapi.dev](https://wakapi.dev) account, not WakaTime.com, and updates the `waka` section daily at 00:17 UTC or on a manual **Actions → Update coding activity → Run workflow** dispatch.

## Enable updates

1. In your IDE, install the [WakaTime editor plugin](https://wakatime.com/plugins). It acts as the coding-activity collector; the `api_url` below sends its data to Wakapi.dev, not WakaTime.com. Configure `~/.wakatime.cfg` with your Wakapi account's API key:
   ```ini
   [settings]
   api_url = https://wakapi.dev/api
   api_key = <your Wakapi API key>
   ```
   Keep the key private; do not put it in this repository. Start coding and wait until Wakapi shows activity before running the README workflow.
2. In **Settings → Secrets and variables → Actions**, set the repository variable `WAKAPI_API_URL` to `https://wakapi.dev/api/compat/wakatime/v1/`. This is the WakaTime-compatible stats API used by the README action; it differs from the editor heartbeat URL (`https://wakapi.dev/api`). Keep the trailing slash because the action appends `users/current/stats/...` to this URL.
3. Add these repository secrets:
   - `WAKAPI_API_KEY`: an API key created in your Wakapi account. The workflow maps it to the input name expected by the stats generator.
   - `GH_TOKEN`: a GitHub personal access token. Upstream documents a classic PAT with `repo` and `user` scopes. These scopes are broad: use a dedicated token, set an expiration, and never commit it.
4. Enable GitHub Actions for the repository, then run **Update coding activity** manually to populate the section for the first time.

Until the URL, Wakapi key, and GitHub token are configured, the workflow skips the update with a notice rather than failing. The README contains no fabricated activity while waiting for real data. Wakapi.dev currently retains up to 12 months of coding activity on its free tier; the README generator only displays weekly stats.

The action reads profile data with `GH_TOKEN` and pushes README changes with the workflow's built-in `GITHUB_TOKEN` (`PUSH_TOKEN`), granted `contents: write`. Branch protection or repository policies may still prevent the push; check the Actions log if that happens.

Enabled stats: commit activity by time of day and weekday, weekly languages and editors, operating system, timezone, languages by repository, and the last-updated date. Commit charts scan all repositories (`MAX_REPOS: "0"`). The job timeout is set to 360 minutes, GitHub-hosted runners' six-hour maximum; full scans can use substantial Actions time. Project names, GitHub short-info, profile-view badges, all-time totals, and charts stay hidden. AI activity is generated separately by `tools/wakapi_ai_stats.py` from the last seven days of Wakapi heartbeats; this avoids WakaReadme's unsupported AI-aggregate fallback. It can show AI time, reported AI/human line changes, tokens, sessions, prompt-bearing events and models. Wakapi does not provide model cost in these heartbeats, so cost is omitted. Detailed fields appear only when the client sends AI metadata. The section publicly shares the displayed AI usage and model information. See [the compatibility research note](WAKAPI_AI_STATS_RESEARCH.md). Change the `SHOW_*` inputs in `.github/workflows/waka-readme.yml` if needed.

The generator calls the configured stats API base URL directly; keep `/api/compat/wakatime/v1/` and the trailing slash in `WAKAPI_API_URL`. For another Wakapi host, replace `wakapi.dev` with that hostname. The editor's heartbeat URL (`/api`) and the README action's stats URL are different.

The action is pinned to a full upstream commit SHA for reproducibility. Review upstream changes before updating that SHA.

## References

- [WakaReadme configuration](https://github.com/anmol098/waka-readme-stats#new-to-this)
- [GitHub: creating repository secrets](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions)
