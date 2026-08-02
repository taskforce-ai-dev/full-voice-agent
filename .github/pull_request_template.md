<!--
  Merging this PR to `main` DEPLOYS to production (67.207.90.109) automatically.
  See CONTRIBUTING.md §6. Fill this in honestly — it is the only gate we have.
-->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Agents touched

<!-- Tick every agent whose folder this PR changes. These WILL deploy on merge. -->

- [ ] BSL Agent
- [ ] Flico Agent
- [ ] HattonHills
- [ ] Kavya
- [ ] Kitchened
- [ ] SLIC Agent
- [ ] Sofia Agent
- [ ] WorldOfRefrigerators
- [ ] None — tooling / docs / CI only (nothing deploys)

## Deploy impact

- **Mode this will trigger:** <!-- fast (code/knowledge_docs only) | build (requirements/Dockerfile/compose changed) | none -->
- **Calls interrupted on merge?** <!-- yes, brief restart | yes, longer rebuild | no -->
- **Safe merge window:** <!-- e.g. after 8pm LK / anytime, agent is not live yet -->

## How it was tested

<!-- Local run, unit tests, live call, WebSocket test... Be specific. "It should work" is not testing. -->

## Rollback

<!-- The ref to roll back to, e.g.:
     gh workflow run deploy.yml -f agent=kavya -f ref=<last-good-sha> -f mode=fast -->

## Checklist

- [ ] Branched off `main` — did **not** commit directly to `main`
- [ ] Pre-commit hooks ran (no `--no-verify`); gitleaks clean
- [ ] No secrets, keys, or real customer data in the diff or in test fixtures
- [ ] Edited a `CLAUDE.md`? Ran `bash ops/sync-agent-docs.sh`
- [ ] CI is green
