# Pre-commit Hooks

This repo uses [pre-commit](https://pre-commit.com/) to run hygiene checks,
secret scanning, and a CLAUDE.md -> AGENTS.md drift guard before each commit.
Config lives in `.pre-commit-config.yaml`. (Python lint via ruff runs in CI, not
here — see below.)

## Install

Install the `pre-commit` tool (one of):

```bash
pipx install pre-commit      # recommended
# or
pip install pre-commit
```

Then install the git hook into this clone (run once per clone):

```bash
pre-commit install
```

After this, the hooks run automatically on every `git commit`.

## Run on all files

To run every hook against the whole repo (useful on first setup or in CI):

```bash
pre-commit run --all-files
```

To run a single hook:

```bash
pre-commit run gitleaks --all-files
```

## What the hooks enforce

- **pre-commit-hooks** — trailing whitespace, end-of-file newline,
  blocks files > 5 MB, merge-conflict markers, private keys, valid YAML/JSON.
- **gitleaks** — scans staged changes for secrets before they are committed
  (we had a leak; this is the local guardrail).
- **ruff lint runs in CI, not here** — the codebase predates ruff and has
  pre-existing violations, so ruff is kept out of the blocking pre-commit path
  to avoid reformatting production code on commit. See `.github/workflows/ci.yml`
  (advisory `lint` job); re-add it here after a cleanup pass.
- **agents-md-sync** — runs `ops/sync-agent-docs.sh` (CLAUDE.md is the source
  of truth) and fails if any `*/AGENTS.md` is out of date, preventing
  CLAUDE.md / AGENTS.md drift.

## Skip in an emergency (discouraged)

If you must commit without running hooks (for example a hotfix while a hook is
misbehaving):

```bash
git commit --no-verify
```

This is discouraged — it bypasses secret scanning and the docs-sync guard.
Re-run `pre-commit run --all-files` and fix any issues as soon as possible.
