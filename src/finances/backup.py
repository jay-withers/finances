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

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from appkit import integrity as kit
from appkit.backup import Backup, Document

from . import integrity, store
from .settings import secret, settings

# The key prefix under daily/ and monthly/, shared by every app writing to the
# bucket. The repository's name, so a restore knows where to look.
APP = "finances"


logger = logging.getLogger(__name__)


# The bytes the last copy read, for `run` to judge: the document that is in R2,
# not whatever a write has made of the blob since.
_copied: list[bytes] = []


def _document(raw: bytes) -> None:
    """What "restores" means: the copy parses. Only that: a document that breaks
    one of its rules is still a good backup of it, and failing here would cost
    the month its monthly copy. `run` judges the rules after every copy."""
    _copied.append(raw)
    integrity.parse(raw)


def _load() -> Callable[[], bytes | None]:
    # Looked up on every call, so a test that patches store.load_raw sees it.
    return lambda: store.load_raw()


JOB = Backup(
    APP,
    [Document(store.BLOB_NAME, _load(), _document, required=True)],
    settings=settings,
    secret=secret,
)


def run(client: Any | None = None, now: datetime | None = None) -> int:
    """Back up, then check the document's rules and round trip: 1 if either fails.

    Judged after both the daily and any monthly copy are in R2 (copy first, judge
    second), so a document that went bad fires the job-failure alert with every
    copy safe. This is where a stored rule violation is reported; the deploy
    selfcheck runs the round trip only.
    """
    _copied.clear()
    failed = JOB.run(client=client, now=now)
    if not _copied:
        return failed  # nothing was read (not configured, or the read failed)
    raw = _copied[0]
    found = kit.problems(raw, integrity.parse, integrity.dump, integrity.check)
    if found:
        logger.error("integrity: FAIL %s", kit.IntegrityError(found))
        return 1
    logger.info("integrity: ok %s", store.BLOB_NAME)
    return failed


keys = JOB.keys
check_bucket = JOB.check_bucket
r2_client = JOB.r2_client
daily_key = JOB.daily_key
monthly_key = JOB.monthly_key
