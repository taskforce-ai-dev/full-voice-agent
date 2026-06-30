# Security Policy

This is a **private** monorepo (`github.com/thiva2k/full-voice-agent`) of Sri
Lankan voice agents built on Twilio + FastAPI. Access is restricted to the
maintainer and trusted collaborators. This policy covers how to report issues
and how secrets are handled.

## Reporting a Vulnerability

**Do not open a public GitHub issue, PR, or discussion for security problems.**

Report privately by email to the maintainer:

- **chrys@taskforceai.tech**

Please include:

- A description of the issue and its impact.
- Steps to reproduce (or a proof of concept).
- Affected agent(s) / endpoint(s) / file(s) if known.

We aim to acknowledge reports within a few business days. Please give us
reasonable time to investigate and ship a fix before disclosing anything
publicly.

## Supported Scope

- This is an **internal / private** project; there is no public bug-bounty
  program and no formal SLA.
- Production runs on a **single VPS**. Only the current `main` branch is
  supported — older commits are not patched.
- The marketing website (`Taskforce_AI_Website/`) is a separate, outward-facing
  repo with its own deploy pipeline; report site issues the same way.

## Secret Handling

- **Real secrets live only in per-agent `.env` files**, which are **gitignored**
  and never committed. Only `.env.example` (placeholder keys, no real values) is
  committed.
- Secrets include: Twilio Account SID / auth token / API keys, `KB_RELOAD_SECRET`,
  `ANTHROPIC_API_KEY`, SSH/VPS credentials, and any provider API keys.
- **`gitleaks` runs in CI and as a pre-commit hook** to block secrets from being
  committed. Keep the hooks installed locally (`pre-commit install`) and do not
  bypass them with `--no-verify`.
- Never paste live credentials into code, logs, commit messages, issues, PRs, or
  chat. Rotate any secret that has been shared in plaintext.

## If You Find a Leaked Credential — Runbook

If a secret ever lands in the repo, git history, logs, or anywhere public, treat
it as compromised. Speed matters more than tidiness: **rotate first.**

1. **Rotate immediately.** Revoke/regenerate the credential in the provider
   console (e.g. Twilio Console → API keys / auth token, Anthropic console,
   DigitalOcean) and update the live `.env` on the VPS. A scrubbed-but-unrotated
   secret is still a live secret.
2. **Scrub history.** Remove the secret from git history with
   `git filter-repo` (preferred) or the BFG Repo-Cleaner. Deleting the file in a
   new commit is **not** enough — the value remains in earlier commits.
3. **Force-push** the rewritten history and have all collaborators re-clone (or
   hard-reset) so the leaked value does not get reintroduced from local copies.
4. **Audit.** Check provider access/usage logs for unauthorized activity during
   the exposure window, confirm `gitleaks` now passes clean, and verify the new
   credential is only present in the gitignored `.env`.

> Precedent: a leaked Twilio token was previously scrubbed from this repo using
> exactly this rotate → scrub → force-push → audit sequence.
