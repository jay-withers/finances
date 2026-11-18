"""`finances backup`: copy the document off Azure, encrypted, and prove the copy restores.

The job itself is jay-withers/appkit's `appkit.backup`, shared with gym-log
and career. This module declares what finances backs up and how it is checked
to restore; the reasoning below is why the job is shaped as it is.

The document lives in one LRS storage account in one subscription. Versioning,
soft delete and the delete lock cover a bad write or a slip of the hand; they
do nothing if the subscription itself is disabled or lost. So once a day this
job reads the document *exactly as stored*, encrypts it with age, and puts it
in Cloudflare R2 (the `cloudflare` repo's `jw-app-backups` bucket) under
`daily/finances/<UTC timestamp>/`. The first run in a month whose daily copy
verifies, and that finds no `monthly/finances/<yyyy-mm>/` copy, writes one
there too: a run that fails on the 1st doesn't leave the month without one,
and a bad copy never takes the month's slot. R2's lifecycle rules are the
only retention; nothing here deletes.

**Nothing is ever overwritten.** Every key carries the moment it was taken
(and a monthly copy is only written where none exists), so a rerun or a
retry, even one reading a document that went bad since, adds a copy instead
of replacing a good one.

**Two age recipients.** Every copy is encrypted to the owner's public key
(`BACKUP_AGE_RECIPIENT`), whose private half stays offline, and to a separate
*drill* key whose private half is the Key Vault secret
`BACKUP-AGE-DRILL-KEY`. Either can decrypt. The drill key is what lets this job
prove, on every run, that what it uploaded comes back: it downloads each object
again, decrypts it, checks it is byte-for-byte what it read, and parses it the
way the app does. Whoever can read the drill key can already read the blob
itself, so it widens nothing.

**Copy first, judge second.** A document that fails to parse is still uploaded
and then fails the run, so the job-failure alert fires while the last good
copies are still in R2. A failure with one key is logged and counted, and the
rest are still attempted.
"""

from __future__ import annotations

from collections.abc import Callable

from appkit.backup import Backup, Document

from . import store
from .model import Document as FinancesDocument
from .settings import secret, settings

# The key prefix under daily/ and monthly/, shared by every app writing to the
# bucket. The repository's name, so a restore knows where to look.
APP = "finances"


def _document(raw: bytes) -> None:
    FinancesDocument.from_json(raw.decode("utf-8"))


def _load() -> Callable[[], bytes | None]:
    # Looked up on every call, so a test that patches store.load_raw sees it.
    return lambda: store.load_raw()


JOB = Backup(
    APP,
    [Document(store.BLOB_NAME, _load(), _document, required=True)],
    settings=settings,
    secret=secret,
)

run = JOB.run
keys = JOB.keys
check_bucket = JOB.check_bucket
r2_client = JOB.r2_client
daily_key = JOB.daily_key
monthly_key = JOB.monthly_key
