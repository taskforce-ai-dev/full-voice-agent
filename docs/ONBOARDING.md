# Onboarding — read this before your first commit

Welcome. This repo builds **phone agents that answer real customer calls**. That
single fact shapes every rule below. Please read this end to end once — it takes
five minutes and it will stop you from having a bad first week.

> **Current status (Aug 2026): no agent is live for a client yet.** The fleet is
> deployed and healthy, but it is not taking customer traffic. So today a bad
> merge costs you a rebuild, not a dropped call.
>
> The rules below are written for the day that changes, which is soon and may
> not come with an announcement. Build the habits now, while mistakes are cheap
> — nobody develops good deploy discipline during their first real incident.
> **If you are ever unsure whether an agent is live, assume it is.**

`CONTRIBUTING.md` is the full reference. This page is the part you need on day one.

---

## The one rule

> **Merging to `main` deploys to production immediately. There is no staging
> environment and no approval step.**

Merge a PR that touches an agent's folder and, within seconds, that agent's
container is restarted on the production server — dropping any call in progress.
There is no "let's see how it looks on staging first". `main` **is** production.

So: **never commit or push directly to `main`.** Branch, open a PR, get a review,
and let the reviewer tell you when to merge.

Nothing on GitHub currently *enforces* this — we're on a free plan where protected
branches aren't available on private repos. The rule is the only safeguard there
is. Please treat it as load-bearing.

---

## Day one setup

```bash
git clone https://github.com/taskforce-ai-dev/full-voice-agent.git
cd full-voice-agent

pipx install pre-commit     # or: pip install pre-commit
pre-commit install          # <- REQUIRED, do not skip
```

`pre-commit install` is not optional. It activates **gitleaks**, which scans your
staged changes for secrets before they can ever reach GitHub. Without it you are
one careless `git add .` away from committing an API key. It also keeps the
generated `AGENTS.md` doc mirrors in sync.

Never use `git commit --no-verify`. That flag skips secret scanning.

To run an agent locally:

```bash
cd "Kavya"                # folder names contain spaces — always quote them
cp .env.example .env      # then fill in your own keys
pip install -r requirements.txt
python server.py
```

---

## How to ship a change

1. **Branch** — `feat/<scope>-<desc>`, `fix/<scope>-<desc>`, `chore/<desc>`.
   Scope is the agent id (`kavya`, `flico`, `bsl`, `hatton`, `slic`, `sofia`,
   `kitchened`, `wor`) or an area (`ci`, `docs`, `ops`).
2. **Commit** — conventional style: `fix(kavya): stop the failsafe leaking state`.
3. **Open a PR.** The template asks which agents deploy and what the call impact
   is. Fill it in honestly — the reviewer is relying on it.
4. **Read the automatic "Deploy impact" comment.** A bot posts on every PR listing
   exactly which agents will deploy on merge and whether calls get interrupted.
   Trust it over your own reading of the diff.
5. **Wait for the go-ahead**, then merge. If the comment says agents will deploy,
   merge during a quiet window — not during business hours in Sri Lanka.

### What "deploy impact" actually means

| Mode | Triggered by | What happens |
|---|---|---|
| **fast** | code or `knowledge_docs` changes | Files hot-swapped into the running container + restart. Seconds. |
| **build** | `requirements*.txt`, `Dockerfile`, `docker-compose.yml` | Full image rebuild + restart. Minutes. |

Anything inside an agent folder counts — **including a `.md` file**. The deploy
detection matches on the folder path, not on file type. A docs-only change inside
`Kavya/` still restarts Kavya.

---

## Things that will surprise you

**Dependabot PRs are not free wins.** There are around two dozen open, and most of
them bump a `requirements*.txt` inside an agent folder — which means `build` mode:
a full rebuild and restart of a live agent. They look like ideal warm-up tasks.
They are not. **Do not merge a Dependabot PR without asking first.**

**Folder names contain spaces.** `"BSL Agent"`, `"Flico Agent"`, `"SLIC Agent"`,
`"Sofia Agent"`. Quote them in every shell command and script.

**CI red does not always mean you broke something.** The `lint` job is advisory —
this codebase predates ruff and has pre-existing violations we are paying down
gradually. It reports but never blocks. What *does* block: `secret-scan`. If that
fails, stop and talk to someone; don't try to work around it.

**Don't mass-reformat.** Running a formatter across the repo touches `.py` files in
every agent folder, which would restart all eight production agents at once.

**`CLAUDE.md` is the source of truth for each agent.** Each agent folder has one,
with architecture, history and gotchas. Read it before deep work. The matching
per-agent `AGENTS.md` is generated — never hand-edit it.

**Explore via the knowledge graph, not grep.** Start with
`graphify-out/GRAPH_REPORT.md`, then `graphify query "<question>"`. It's far faster
than reading files.

---

## Secrets and access

- Real credentials live in a git-ignored `.env` per agent. Only `.env.example`
  (keyless template) is committed. **Never commit a real key.**
- If you ever find a committed secret, treat it as compromised: tell the team
  immediately and rotate it. Don't just delete the line.
- **Developers do not get production SSH access.** The production server handles
  live calls and holds customer data. If you think you need shell access there,
  ask — the answer is usually a log stream or a staging reproduction instead.
- Repo write access is effectively production access, because deploy credentials
  live in GitHub Actions. Treat your GitHub account accordingly: 2FA is required
  org-wide.

---

## When something breaks in production

Don't fix it by pushing straight to `main`. That's how a small problem becomes two
problems. Tell the team, then either roll back or open a normal PR:

```bash
# roll an agent back to a known-good commit
gh workflow run deploy.yml -f agent=<id> -f ref=<last-good-sha> -f mode=fast
```

---

## Where to go next

| I want to… | Read |
|---|---|
| Understand the rules properly | [`CONTRIBUTING.md`](../CONTRIBUTING.md) |
| Understand the repo and agents | [`README.md`](../README.md) |
| Work on a specific agent | that agent's `CLAUDE.md` |
| Understand deploys and rollback | [`CONTRIBUTING.md`](../CONTRIBUTING.md) §6, [`docs/DEPLOYMENT.md`](./DEPLOYMENT.md) |
| Understand the pre-commit hooks | [`docs/PRECOMMIT.md`](./PRECOMMIT.md) |

**If you are unsure whether something will deploy, ask before merging.** Nobody
will mind the question. Everyone will mind a dropped customer call.
