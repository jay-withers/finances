# finances

Household finance tracker: savings pots, recurring commitments, renewal dates
and wealth. A small private web app on Azure Container Apps, replacing the
spreadsheet it was migrated from.

It exists to make three recurring jobs quicker and harder to forget:

- **Payday** — top up every savings pot in one click, then work through the
  transfer checklist.
- **Renewals** — see what is coming up for renewal without opening anything.
- **Wealth** — record a valuation each quarter, and be told when the figures
  have gone stale.

The dashboard shows what needs attention right now; a scheduled job emails the
same list every Sunday evening.

## Running it locally

No Azure, no credentials, no spreadsheet:

```bash
make install                        # hooks and the Python environment
make import FILE=Finances_3.xlsx    # or: make seed, for invented sample data
make run                            # http://localhost:8000, passcode "local"
```

With `STATE_CONTAINER_URL` unset the document is a local `.finances.json` and
secrets come from the environment, so nothing reaches or needs Azure. `make
import` defaults to that local file — pass `REMOTE=1` to import into the
deployed blob instead.

```
make                # every target, with a description
make test           # the suite
make typecheck      # mypy; CI runs it too
make lint           # every pre-commit hook against every file
make digest         # print the reminder email without sending it
```

## How it is put together

Python 3.14, FastAPI and Jinja2, server-rendered. One image, one process, no
build step and no JavaScript framework — the same shape as
[gym-log][gym-log], and for the same reason: there is nothing here a single
rendered page cannot do.

**There is no database.** The whole document is one JSON blob in Azure Storage,
read whole on each request and written whole on each change. A payday run adds
nine ledger entries and a renewal adds a row; a year is measured in tens of
kilobytes, and there is no query to serve. A PostgreSQL Flexible Server would
bill about £13/month whether or not anything used it.

**Money is integer pence, everywhere.** A budget is a long chain of additions
that has to reconcile to the penny, and the spreadsheet's own total cell reads
`2722.5299999999997` because Excel summed 38 floats. `money.py` is the only
place a decimal string becomes a number.

**Nothing derived is stored.** Pot balances, monthly totals and the attention
list are all computed in `calc.py`. That is a direct response to how the
spreadsheet failed: it kept the pot contributions in two places, and by
migration time they had drifted £28 apart.

### Where things live

| | |
|---|---|
| `src/finances/model.py` | The document — everything, as held in the blob |
| `src/finances/calc.py` | Balances, totals and what needs attention |
| `src/finances/money.py` | Pence in, formatted strings out |
| `src/finances/store.py` | Blob load/save, with an ETag precondition |
| `src/finances/importer.py` | The one-off spreadsheet migration |
| `src/finances/digest.py` | The weekly reminder email |
| `src/finances/api/` | Routes, the passcode gate, the app |
| `terraform/` | The infrastructure — see [its README](terraform/README.md) |

### Getting in

A single shared passcode, held in Key Vault as `APP-PASSCODE`, with an
HMAC-signed session cookie lasting thirty days. Not Entra ID: Container Apps'
built-in auth is not exposed by the `azurerm` provider and would need `azapi`,
which nothing in this estate uses. Shared also suits the data — it is a
household's, not a person's, so per-user identity would add a sign-in without
adding a boundary.

The gate defaults **on**, so a missing passcode fails closed.

## Deploying

**Releases deploy themselves.** When a merge publishes a new image,
`cd-publish`'s `deploy` job runs this same `make deploy IMAGE_TAG=<release>`
against dev, through the shared `container-apps-deploy.yml` (OIDC in the `dev`
GitHub environment). To redeploy an existing tag — after a failed deploy, or an
apply that recreated a workload — run `cd-publish` from the Actions tab with
that tag. Running `make deploy` by hand is for anything else, such as a
rollback.

**Every deploy is smoke-tested, and a failed one rolls back.** Before deploying,
the workflow records `make deployed-tag` (the tag on the app now). After it,
`make smoke` (`scripts/smoke.sh`) checks that the app and every job are configured with
the new tag, that the app's `/healthz` reports it as `version` on its default
FQDN (allowing five minutes for a cold start), and that `/readyz` answers 200.
It then starts one run of each job with its args swapped for `finances selfcheck`,
which checks the settings, secrets and storage a real run needs and does
nothing else, and waits for each to succeed.
If the deploy or the smoke test fails, the workflow redeploys the recorded tag,
smoke-tests that, and fails the run, so a `deploy-failed` issue says whether
dev is back on the old version. Rolling back moves the image only: a release
must not change stored data in a way the previous version can't read.

```bash
make deploy IMAGE_TAG=v0.2.0
```

Images are published to `ghcr.io/jay-withers/finances` by `cd-tag` on every
merge that touches the application, tagged with both the release version and
the commit SHA and never with a moving tag, matching [gym-log][gym-log] and
[market-agent][ma].

See [terraform/README.md](terraform/README.md) for the first apply, the
secrets, and the custom domain's manual binding step.

## Backups

The document is one blob in one LRS storage account. Versioning, soft delete
and the delete lock cover a bad write or a slip of the hand, but not losing
the subscription. So the **backup job** (`finances backup`, 02:45 UTC daily)
copies `finances.json` to the cloudflare repo's R2 bucket `jw-app-backups`,
encrypted with [age](https://age-encryption.org). It works the same way as
gym-log's:

- `daily/finances/<UTC timestamp>/finances.json.age`, kept 30 days.
- `monthly/finances/<yyyy-mm>/finances.json.age`, written by the first
  successful run in the month, kept a year.

Nothing is overwritten: a rerun adds a copy beside the earlier ones.

Each copy is encrypted to two keys. One is **the owner's**, whose public half
is `backup_age_recipient` in `environments/dev.tfvars` (the same key as
gym-log's) and whose private half lives only in a password manager. The other
is a **drill key** in Key Vault (`BACKUP-AGE-DRILL-KEY`). After every upload
the job downloads the copy, decrypts it with the drill key, checks it matches
byte for byte and parses it as a `Document`. Any failure fails the job, and
the platform's job-failure alert fires.

**Setup (once):** put the R2 token's two halves (an Account API token with
**Object Read & Write** on `jw-app-backups` only; gym-log's works) in Key
Vault as `R2-ACCESS-KEY-ID` and `R2-SECRET-ACCESS-KEY`, and generate the drill
key straight into Key Vault. `make secrets` prints all three commands. Do it
**before** a release with the backup job deploys: the job's selfcheck checks
them, so a missing one fails the smoke test and rolls the release back.

**Restoring.** Download a copy from the R2 dashboard. Put the private key from
the password manager in a file outside any repository (`cat > ~/backup.key`,
paste, Ctrl+D; never `echo` it, where shell history keeps it), then:

```bash
mise exec age@latest -- age -d -i ~/backup.key -o finances.json finances.json.age
LOCAL_STATE_PATH=finances.json uv run finances show | head
rm ~/backup.key
```

Put it back with `az storage blob upload --auth-mode login --overwrite` into
the container `make show` reads, or into a new storage account after a
rebuild.

[gym-log]: https://github.com/jay-withers/gym-log
[ma]: https://github.com/jay-withers/market-agent
