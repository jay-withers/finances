"""`finances selfcheck`: prove a job's container could do a real run, and do nothing.

After every deploy, `make smoke` (workflows' smoke.sh) starts one run of each job
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

from appkit import selfcheck as kit

# backup.py's (appkit's R2 keys and drill key). Checked only in the job given a
# bucket, so a missing R2 key fails that job's selfcheck without it reading as a
# fault in the digest.
from appkit.backup import SECRETS as BACKUP_SECRETS

# mailer.py (the digest job).
SECRETS = ("RESEND-API-KEY",)
OPTIONAL_SECRETS = ("DIGEST-EMAIL-TO",)

__all__ = ["BACKUP_SECRETS", "OPTIONAL_SECRETS", "SECRETS", "checks", "run"]


def checks() -> list[kit.Check]:
    from . import backup, store
    from .settings import optional_secret, secret, settings

    return [
        ("settings", settings),
        *kit.secret_checks(SECRETS, secret),
        *kit.optional_secret_checks(OPTIONAL_SECRETS, optional_secret),
        ("storage", store.load),
        *kit.backup_checks(backup.JOB),
    ]


def run() -> int:
    return kit.run(checks())
