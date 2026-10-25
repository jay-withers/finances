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
`make smoke` (`scripts/smoke.sh`) checks that the app and the digest job are configured with
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
the commit SHA and never with a moving tag. Rolling one onto Azure is a
deliberate manual step, matching [gym-log][gym-log] and [market-agent][ma].

See [terraform/README.md](terraform/README.md) for the first apply, the
secrets, and the custom domain's manual binding step.

[gym-log]: https://github.com/jay-withers/gym-log
[ma]: https://github.com/jay-withers/market-agent
