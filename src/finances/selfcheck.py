"""`finances selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (scripts/smoke.sh) starts one run of each job
with its args swapped for `selfcheck`. That run gets everything a real one does
— the image, the managed identity, the env and the Key Vault references — and
checks what a real run needs before it does anything: the settings load, every
secret the digest job reads resolves, and the document in storage reads. It
sends no email and writes nothing.

Exits 1 if any check fails, naming each failure but never a secret's value. A
failure fails the smoke test, which rolls the deploy back.

The required secrets are the ones the jobs read with `secret()`, which raises
on a missing value, so a run without one would fail anyway; an optional one
only has to resolve. Add a secret here when a job starts reading one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial

logger = logging.getLogger(__name__)

# mailer.py (the digest job).
SECRETS = ("RESEND-API-KEY",)
OPTIONAL_SECRETS = ("DIGEST-EMAIL-TO",)

# backup.py's. Checked only in the job given a bucket, so a missing R2 key
# fails that job's selfcheck without it reading as a fault in the digest.
BACKUP_SECRETS = (
    "R2-ACCESS-KEY-ID",
    "R2-SECRET-ACCESS-KEY",
    "BACKUP-AGE-DRILL-KEY",
)


def checks() -> list[tuple[str, Callable[[], object]]]:
    from . import store
    from .settings import optional_secret, secret, settings

    def present(name: str) -> None:
        if not secret(name):
            raise ValueError("resolved to an empty value")

    found: list[tuple[str, Callable[[], object]]] = [
        ("settings", settings),
        *[(f"secret {name}", partial(present, name)) for name in SECRETS],
        *[
            (f"secret {name} (optional)", partial(optional_secret, name))
            for name in OPTIONAL_SECRETS
        ],
        ("storage", store.load),
    ]
    # Only the backup job is given a bucket, so only its selfcheck lists these.
    # Settings that don't load are the "settings" check's failure to report, not
    # a reason for this list to raise.
    try:
        backup_job = bool(settings().backup_bucket)
    except Exception:
        backup_job = False
    if backup_job:
        from . import backup

        found += [(f"secret {name}", partial(present, name)) for name in BACKUP_SECRETS]
        found.append(("backup settings and keys", backup.keys))
        found.append(("backup bucket", backup.check_bucket))
    return found


def run() -> int:
    failed = 0
    for label, check in checks():
        try:
            check()
        except Exception as exc:
            failed += 1
            logger.error("selfcheck: FAIL %s: %s: %s", label, type(exc).__name__, str(exc)[:300])
        else:
            logger.info("selfcheck: ok %s", label)
    if failed:
        logger.error("selfcheck: %d check(s) failed", failed)
        return 1
    logger.info("selfcheck: all checks passed")
    return 0
