# Sentry → Claude Auto-Triage

When a production error hits Sentry, Claude automatically diagnoses it and opens a
**draft pull request** with a proposed fix — you review, merge, and deploy. Nothing
reaches production without you.

```
error in agent → Sentry → (GitHub integration opens a `sentry`-labeled issue)
   → .github/workflows/sentry-autofix.yml runs Claude
   → Claude reads stack trace + agent tag → draft PR with the fix (or a diagnosis comment)
   → you review → merge → deploy via deploy.yml
```

The workflow is `.github/workflows/sentry-autofix.yml`. It is **human-in-the-loop by
design**: Claude only opens *draft* PRs and never deploys.

---

## One-time setup

### 1. Add the Anthropic API key (repo secret)
The Action authenticates to Claude with an API key (separate from your Claude Code
subscription).
1. Create a key at https://console.anthropic.com → API Keys.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ANTHROPIC_API_KEY`
   - Value: the key
3. *(Optional alternative)* install the **Claude GitHub App** instead via `/install-github-app`; then the App provides auth and you can drop `anthropic_api_key` from the workflow.

### 2. Connect Sentry to GitHub
In Sentry → **Settings → Integrations → GitHub** → Install → grant access to
`thiva2k/full-voice-agent`.

### 3. Make Sentry open a labeled issue on new errors
In Sentry → **Alerts → Create Alert → Issues** (an "issue alert"):
- **When:** *A new issue is created* (fires once per unique error group, not per occurrence).
- **Then:** *Create a new GitHub issue* → repository `thiva2k/full-voice-agent`.
- Set the issue **label to `sentry`** (the workflow only acts on that label).

> If your Sentry plan's "Create GitHub issue" action can't set a label, tell me — I'll
> add a tiny auto-label step (label any issue authored by the Sentry bot as `sentry`),
> or switch the workflow's filter to the Sentry author instead of the label.

That's it. The next new error opens a `sentry` issue → the workflow runs → a draft PR appears.

---

## How to test (no real error needed)
1. Open a GitHub issue, add the label **`sentry`**, and paste a sample body, e.g.:
   ```
   KeyError: 'price'
   agent: flico
   Traceback (most recent call last):
     File "/app/server.py", line 512, in retrieve_context
       price = listing['price']
   ```
2. Watch **Actions → Sentry Auto-Triage**. Claude will open a draft PR or comment a diagnosis.
3. Delete the test issue/PR afterwards.

---

## Guardrails & cost
- **Draft PRs only** — Claude never merges or deploys. You decide.
- **Scoped** — the prompt restricts Claude to the one affected agent (from the `agent` tag) and a minimal fix; no refactors, no other agents, no secret/`.env` changes.
- **Conservative** — if it isn't confident (external outage, data issue, ambiguity), it comments a diagnosis instead of opening a PR.
- **Cost** — one Claude run per *new* Sentry issue group (Sentry dedupes occurrences), capped by `--max-turns`. Model is `claude-sonnet-4-6` (cost-effective); bump to a stronger model in `claude_args` if you want deeper fixes.
- **Bounded blast radius** — runs on the GitHub runner against a checkout; it cannot touch the VPS.

## Tuning
- Stronger fixes: change `--model` in `claude_args` (e.g. to an Opus model).
- Quieter: in Sentry, scope the alert (only `level:error`, specific projects/agents, or a rate threshold) so noisy/low-severity issues don't trigger runs.

## Disable
Delete `.github/workflows/sentry-autofix.yml`, or in Sentry disable the alert rule.
