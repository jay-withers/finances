"""Where the finances document lives: one JSON blob, read whole and written whole.

Adapted from jay-withers/gym-log src/gymlog/store.py, which took it from
repo-agent. The blob client and the lazy-import discipline are the same, and so
is the error handling: **reads raise, writes raise, and the only tolerated
absence is a blob that has never existed.**

That inversion of repo-agent's degrade-to-empty behaviour is the point. Its
state is commentary — it decides which findings are "new", so losing it costs a
week of deltas. Here the document *is* the product: a payday run that fails to
write is money that did not move as far as this app is concerned, and silently
returning an empty document would present the household's savings as zero and
then overwrite them with it.

**Concurrency.** Two people share one passcode and `max_replicas = 1`, so a lost
update needs two browsers open at once — unlikely, but not impossible the way
gym-log's single user was. Every write carries an `If-Match` on the ETag read at
the start, and `update()` retries once against the newer document, which turns
the race into a retry rather than a silently discarded payday.

With no container configured the document falls back to a local file, which is
what makes `make run` work against nothing but a checkout.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Callable
from typing import Any

from .model import Document
from .settings import credential, settings

logger = logging.getLogger(__name__)

# The document's own name inside the container.
BLOB_NAME = "finances.json"


class ConflictError(RuntimeError):
    """The document changed between being read and being written."""


def load() -> tuple[Document, str | None]:
    """Read the document and the ETag to write it back against.

    A blob that has never existed yields an empty document and no ETag — that is a
    first run, not a failure. Every other error propagates.
    """
    blob = _blob()
    if blob is None:
        return _load_local()

    from azure.core.exceptions import ResourceNotFoundError

    try:
        stream = blob.download_blob()
        raw = stream.readall()
    except ResourceNotFoundError:
        logger.info("no document yet; starting an empty one")
        return Document(), None

    etag = stream.properties.etag
    return Document.from_json(raw.decode("utf-8")), etag


def save(document: Document, etag: str | None = None) -> str | None:
    """Write the document, refusing to clobber a document that moved underneath us.

    `etag` is the value from the `load()` that produced this document. None means
    "this must be a create", which is what stops two first-runs racing and one
    of them winning silently.
    """
    blob = _blob()
    if blob is None:
        return _save_local(document)

    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

    body = document.to_json().encode("utf-8")
    try:
        if etag is None:
            result = blob.upload_blob(body, overwrite=False)
        else:
            result = blob.upload_blob(
                body,
                overwrite=True,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
    except (ResourceModifiedError, ResourceExistsError) as exc:
        raise ConflictError("the document changed while this change was being written") from exc

    return result.get("etag") if isinstance(result, dict) else None


def update(change: Callable[[Document], Document]) -> Document:
    """Read, apply `change`, write — retrying once if the document moved.

    `change` must be pure and cheap: on a conflict it is called a second time
    against the newer document, so anything with a side effect would happen
    twice. In particular it must not call `new_id()` outside the document it is
    handed, or a retry would write different ids than the first attempt logged.
    """
    for attempt in (1, 2):
        document, etag = load()
        updated = change(document)
        try:
            save(updated, etag)
        except ConflictError:
            if attempt == 2:
                raise
            logger.warning("document changed under us, retrying against the newer document")
            continue
        return updated
    raise AssertionError("unreachable")


def _blob() -> Any:
    """A client for the document, or None when no container is configured.

    Imported lazily so the model, the derived calculations and their tests never
    need the Azure SDK present — the same reason `settings.py` defers its
    imports.
    """
    url = settings().state_container_url
    if not url:
        return None

    from azure.storage.blob import BlobClient

    return BlobClient.from_blob_url(f"{url.rstrip('/')}/{BLOB_NAME}", credential=credential())


def local_path() -> pathlib.Path:
    return pathlib.Path(settings().local_state_path)


def _load_local() -> tuple[Document, str | None]:
    path = local_path()
    if not path.exists():
        logger.info("no local document at %s; starting an empty one", path)
        return Document(), None
    return Document.from_json(path.read_text(encoding="utf-8")), None


def _save_local(document: Document) -> None:
    """Write via a temporary file and rename.

    An interrupted write that truncates the file in place would lose the whole
    history; a rename is atomic on every filesystem this runs on.

    There is no local equivalent of the ETag check: a local file is one
    developer on one machine, and the blob is the only place two writers can
    meet.
    """
    path = local_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(document.to_json(), encoding="utf-8")
    tmp.replace(path)
    return None
