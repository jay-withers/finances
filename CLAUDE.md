# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

`README.md` covers *why* and how to run it. This file covers the traps.

## What this repo does

A household finance tracker (savings pots, payday transfers, renewals, wealth)
replacing `Finances_3.xlsx`. One container app (`finances serve`) and one
scheduled job (`finances digest`, Sundays 17:00 UTC) on the **shared** Container
Apps environment from
[jay-withers/azure-container-apps](https://github.com/jay-withers/azure-container-apps),
with its own identity, Key Vault and storage account. Shaped like
[gym-log](https://github.com/jay-withers/gym-log): when in doubt, follow it.

## Money and the document: decisions not to re-litigate

- **Money is integer pence, everywhere.** `money.py` is the only place a
  decimal string becomes a number. No `float`, and no `Decimal` either: it
  serialises as a string and invites mixed arithmetic.
- **Nothing derived is stored.** Balances, totals and the attention list are
  computed in `calc.py` from the document. The spreadsheet drifted £28 because
  it kept pot contributions in two places. Never add a stored total.
- **Pots own a ledger; a balance is the sum of its entries.** The spreadsheet's
  month-end balances were imported as one `opening` entry per pot.
- **Ids are short random strings** (`new_id()`), not sequential, so two tabs
  can't mint the same id.
- **There is no database.** The whole document is one JSON blob, read whole and
  written whole. A Postgres server would bill ~£13/month for tens of kilobytes.

## Storage: the inversion from repo-agent

`store.py` comes from gym-log (which adapted repo-agent's), and **the error
handling is deliberately the opposite of repo-agent's**: reads raise, writes
raise, and the only tolerated absence is a blob that has never existed. An
empty document returned on a bad read would show the savings as zero and the
next save would write that back.

- **Every write carries the ETag from its read.** No ETag means create-only.
  A lost race is `ConflictError`, and `update()` retries once against the newer
  document. Two people share the passcode, so this race is real.
- **`update(change)` may call `change` twice**, so it must be pure: no I/O, and
  no `new_id()` for anything outside the document it is handed.
- **The storage account has `prevent_destroy` and a `CanNotDelete` lock.** The
  pipeline can create the lock but not remove it; that is github-repos'
  `manual-remove-lock` workflow, run by hand. While it stands, ARM deletes of
  anything in the account fail, containers included.
- `tests/test_store.py` covers all of this against a fake `BlobClient`. A change
  to `store.py` needs a test there.

## Copied modules: re-copy, don't diverge

Each carries a header naming where it came from. Fix a bug in every copy.

- `settings.py`, `telemetry.py`: from gym-log ← repo-agent ← market-agent.
  This is the **fourth** copy; the standing decision is to extract a library
  the next time the same bug is fixed twice.
- `api/deps.py` (the passcode gate): from gym-log, cookie name aside.
- `api/headers.py`: verbatim across career, finances and gym-log.
- `mailer.py`: from market-agent. security-testing's `mailer.py` is adapted
  from this one ("fix contract bugs in both").
- `scripts/smoke.sh`: shared verbatim with gym-log, career, repo-agent,
  market-agent and security-testing. `scripts/check-tf-standards.sh`: shared
  verbatim with the Terraform repos.

## The passcode gate

One shared passcode (`APP-PASSCODE` in Key Vault) and an HMAC-signed cookie
lasting thirty days. Not Entra: Container Apps auth needs `azapi`, which the
estate doesn't use, and the data is a household's, not a person's. **The gate
defaults on, so a missing passcode fails closed.** The cookie key derives from
the passcode unless `COOKIE_SECRET` is set, so rotating the passcode signs
everyone out.

Every inline `<script>` needs `nonce="{{ request.state.csp_nonce }}"`, and
templates must have no `on*=` attributes (a nonce can't cover them). The
security-testing scan reports any missing header.

## Secrets and settings

Secrets resolve from the environment, then `.env`, then Key Vault
(`secret("RESEND-API-KEY")` reads `$RESEND_API_KEY` first). That is what makes
`make run` work with no Azure, and why Terraform creates **no**
`azurerm_key_vault_secret` and no Container Apps Key Vault reference: `make
secrets` prints the `az` commands to set them by hand.

`selfcheck.SECRETS` / `OPTIONAL_SECRETS` must list every secret a job reads.
`selfcheck` runs on every deploy, so **keep it free of side effects**: no
email, no writes.

## The CLI and its workloads

`finances serve|digest|import|show|seed|selfcheck`, one console script.

- **Terraform sets no `command`.** The Dockerfile's `ENTRYPOINT` names the
  script and `args` picks the subcommand. market-agent took a full outage in
  September 2026 from a stale `command` under `ignore_changes`.
- `import` is the one-off spreadsheet migration (`make import FILE=...`, local
  by default, `REMOTE=1` for the blob). It has run; its tests skip because the
  spreadsheet was removed once the import was verified. Don't build on it.
- `seed` writes invented sample data and refuses to run when
  `STATE_CONTAINER_URL` is set.
- **The digest logs a line on every run, including a quiet one.** The platform
  can see a crashed job but not a schedule that never fired, so silence must
  never be a normal outcome. `mailer.send` never raises, for the same reason.
- **`/healthz` answers from memory; `/readyz` reaches the document.** A liveness
  probe that touched storage would turn a blob blip into a restart loop.
  `/healthz` must keep reporting `version` from `IMAGE_TAG`: the smoke test
  checks it.

## Deploys: az cli, not terraform apply

Terraform creates the app and job on dev-containers' `placeholder:latest`, never
a guessed release tag. After that, `image` **and the whole `env` map** sit under
`ignore_changes` and `make deploy IMAGE_TAG=vX.Y.Z` owns them. So a change to
any value in `common_env` needs a `make deploy` to reach a running revision,
even though `terraform plan` shows no diff. Moving tags (`latest`, `main`) are
rejected: Container Apps makes no new revision for an unchanged template.

On merge, `cd-tag` tags a release and calls `cd-publish` when `src/`,
`Dockerfile`, `pyproject.toml` or `uv.lock` changed (keep that list in step with
`ci-container-build.yml`'s filter and the Dockerfile's `COPY` lines).
`cd-publish` pushes the image and deploys it to dev through the shared
`container-apps-deploy.yml`: `make deployed-tag`, `make deploy`, `make smoke`
(the app's `/healthz` and `/readyz`, then one `selfcheck` run of the job), and a
rollback to the recorded tag on failure, raising a `deploy-failed` issue.
Rolling back moves the image only, so **a release must not change the stored
document in a way the previous version can't read.**

## Terraform

- The environment and Application Insights are resolved **by name**
  in `data.tf`, never by remote state. The resource group is github-repos'
  (`create_resource_groups`), read by data source. Only `dev` exists, because
  a plan against an environment the platform hasn't applied fails.
- File layout is enforced by `scripts/check-tf-standards.sh`:
  `locals`/`variable`/`output`/`data` blocks in matching files or topic-scoped
  variants (`main.container-apps.tf`, `variables.optional.tf`), and
  `terraform{}`/`provider{}` in `versions.tf`. Required inputs go in
  `variables.required.tf`.
- `.terraform.lock.hcl` needs hashes for linux_amd64, linux_arm64 and
  darwin_arm64, or CI's `init` rewrites it and pre-commit fails.
- **Grants go to explicit object IDs** (`key_vault_administrator_object_ids`),
  never to "whoever applies", or every pipeline run would try to move them to
  itself. They aren't committed: CI reads
  `TF_VAR_KEY_VAULT_ADMINISTRATOR_OBJECT_IDS`, which github-repos sets on the
  `dev` environment, and a local plan needs it exported.
- The custom domain is a local `make apply` plus `make bind-domain`
  (provider bug #27362). Its DNS records live in jay-withers/cloudflare's
  `app_domains`, and must resolve before the apply that issues the certificate.
- The digest cron (`digest_cron_expression`) is UTC, and
  `schedule_trigger_config` is ForceNew: changing it replaces the job, which
  then runs the placeholder until the next deploy. Re-run `cd-publish` with
  the current tag after any apply that creates or replaces a workload.

## Testing

`make test` is pytest, flat `tests/`, one file per module. `filterwarnings =
["error"]`, with one ignore for starlette's `TestClient` import (gym-log and
market-agent carry the same one).

- `conftest.py` clears `STATE_CONTAINER_URL`, `KEY_VAULT_URI` and
  `APPLICATIONINSIGHTS_CONNECTION_STRING`, stubs `.env`, points
  `LOCAL_STATE_PATH` at a tmp file and clears the `settings()`/`secret()`
  caches around every test. A test that sets one of those must clear the cache
  itself.
- `TestClient` uses `https://testserver`. The session cookie is `Secure`, and
  over http every gated request would 303 and look like a bug in the gate.
- The `doc` fixture is a small document with round numbers, built for
  assertions. It is deliberately not `seed.py`'s sample.
- Coverage is **reported on every PR, never gated** (see `ci-python.yml`). A
  threshold would mostly measure how much of the Azure SDK is mocked.
  `pytest-cov` is not a dependency; the shared workflow injects it.

## Docker

- Both stages come from `jay-withers/dev-containers`: `python-app-builder` and
  `python-app`, which **must be the same version**. The builder is built FROM
  the runtime, so they share one interpreter; a mismatch once gave gym-log an
  image with no site-packages on `sys.path`.
- Build `--platform linux/amd64` (the Makefile does). Container Apps is
  amd64-only.

## CI

Every workflow is a thin caller of
[`jay-withers/workflows`](https://github.com/jay-withers/workflows), pinned by
SHA with the tag as a comment:

- `ci-pre-commit` (`pre-commit / Pre-commit`), `ci-python` (`test / Test`,
  Python 3.14 to match the image), `ci-codeql`.
- `ci-terraform`: validate (`terraform / Terraform`), then a dev plan commented
  on the PR, behind the always-reporting `terraform-plan` gate.
- `ci-container-build`: builds the image behind the `container-build` gate.
- `cd-tag` → `cd-publish`, as above.
- `cd-terraform`: plans dev on merge. A plan with changes opens a
  `terraform-pending` issue, and the user applies by running `cd-terraform`
  from the Actions tab. Never `make apply` except for the custom domain or a
  grant outside `rg-finances-dev`.
- `schedule-drift`, `manual-terraform-unlock`: the estate's standard pair.

Required checks are set in github-repos, not here.

## Commit messages

Conventional Commits, enforced by commitlint. `no-commit-to-branch` blocks
direct commits to `main`.
