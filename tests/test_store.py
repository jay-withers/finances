"""Persistence, including the inversion that matters.

The document *is* the product: a read that degraded to empty would show the
household's savings as zero and the next save would write that back. So the
tests here are largely about the store refusing to lose anything.

Ported from gym-log's tests/test_store.py, which covers the same store logic.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import azure.storage.blob
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from finances import settings as settings_module
from finances import store
from finances.model import Document, Line


def _with_income(name: str) -> Callable[[Document], Document]:
    """A pure change, as `update` requires: a fixed id, so a retry writes the same line."""

    def change(document: Document) -> Document:
        return document.model_copy(
            update={"income": [*document.income, Line(id=name, name=name, amount_pence=100)]}
        )

    return change


# --- the local file -----------------------------------------------------------


def test_a_missing_file_is_a_first_run_not_a_failure():
    document, etag = store.load()
    assert document == Document()
    assert etag is None


def test_a_saved_document_reads_back(doc):
    store.save(doc)
    assert store.load()[0] == doc


def test_update_applies_and_persists(doc):
    store.save(doc)
    store.update(_with_income("Bonus"))
    assert [line.name for line in store.load()[0].income][-1] == "Bonus"


def test_a_corrupt_document_raises_rather_than_starting_fresh():
    """The failure this exists to prevent: an empty document written over real savings."""
    store.local_path().write_text("{ this is not json", encoding="utf-8")
    with pytest.raises((json.JSONDecodeError, ValueError)):
        store.load()


def test_a_local_write_is_atomic(doc):
    """Written via a temp file and renamed, so no stray temp file survives a write."""
    store.save(doc)
    path = store.local_path()
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


# --- the blob ----------------------------------------------------------------
#
# Everything above runs against the local file, which is what `make run` uses.
# What follows is the path that runs in Azure, faked at the one seam the module
# has: `BlobClient.from_blob_url`. Worth faking rather than leaving uncovered
# because this is where the inversion lives (a failed read must raise, and a
# write that lost its race must be reported), and the local path exercises
# neither.


class _Stream:
    def __init__(self, body: bytes, etag: str) -> None:
        self._body = body
        self.properties = types.SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self._body


class _FakeBlob:
    """One blob in a fake container: the `BlobClient` calls `store` actually makes.

    Stateful, so a `save` followed by a `load` round trips through it the way
    the real one does. `store` builds a client per call.
    """

    def __init__(self) -> None:
        self.url = ""
        self.credential: object = None
        self.content: bytes | None = None
        self.etag = '"read"'
        self.download_raises: Exception | None = None
        # A list, because the retry in `update` is only visible if the first
        # attempt fails and the second does not.
        self.upload_raises: list[Exception] = []
        # Called when an upload is refused: the other writer landing its own
        # document, which is what makes the retry read something different.
        self.on_conflict: Callable[[], None] | None = None
        self.uploads: list[dict] = []

    def download_blob(self) -> _Stream:
        if self.download_raises is not None:
            raise self.download_raises
        if self.content is None:
            raise ResourceNotFoundError("no such blob")
        return _Stream(self.content, self.etag)

    def upload_blob(self, body: bytes, **kwargs: object) -> dict[str, str]:
        self.uploads.append({"body": body, **kwargs})
        if self.upload_raises:
            if self.on_conflict is not None:
                self.on_conflict()
            raise self.upload_raises.pop(0)
        self.content = body
        return {"etag": '"written"'}


@pytest.fixture
def blob(monkeypatch):
    """Point the store at a fake container and hand back the one blob in it."""
    monkeypatch.setenv("STATE_CONTAINER_URL", "https://acct.blob.core.windows.net/state/")
    settings_module.settings.cache_clear()
    # A sentinel credential: resolving a real one would make the suite pass or
    # fail on whether the machine running it happens to be logged in to Azure.
    monkeypatch.setattr(store, "credential", lambda: "the-credential")

    fake = _FakeBlob()

    class _Client:
        @staticmethod
        def from_blob_url(url: str, credential: object) -> _FakeBlob:
            fake.url = url
            fake.credential = credential
            return fake

    monkeypatch.setattr(azure.storage.blob, "BlobClient", _Client)
    return fake


def test_the_document_is_named_under_the_container_url(blob):
    """A container URL with a trailing slash would otherwise give `state//finances.json`."""
    store.load()
    assert blob.url == "https://acct.blob.core.windows.net/state/finances.json"
    assert blob.credential == "the-credential"


def test_a_blob_that_has_never_existed_is_a_first_run(blob):
    """The one tolerated absence: no document yet is not a lost document."""
    document, etag = store.load()
    assert document == Document()
    assert etag is None


def test_an_unreadable_blob_raises_rather_than_reading_as_empty(blob):
    """Swallowed, this would hand back an empty document for the next save to write."""
    blob.download_raises = HttpResponseError("storage is having a day")
    with pytest.raises(HttpResponseError):
        store.load()


def test_a_read_hands_back_the_etag_to_write_against(blob, doc):
    blob.content = doc.to_json().encode("utf-8")

    document, etag = store.load()
    assert document == doc
    assert etag == '"read"'


def test_a_first_write_refuses_to_overwrite(blob):
    """No ETag means "this must be a create", which is what stops two first runs racing."""
    store.save(Document())
    assert blob.uploads[0]["overwrite"] is False
    assert "etag" not in blob.uploads[0]


def test_a_later_write_carries_the_etag_it_read(blob):
    assert store.save(Document(), etag='"read"') == '"written"'
    upload = blob.uploads[0]
    assert upload["overwrite"] is True
    assert upload["etag"] == '"read"'
    assert upload["match_condition"] is MatchConditions.IfNotModified


@pytest.mark.parametrize(
    ("etag", "error"),
    [
        # Two first runs racing: the create lost.
        (None, ResourceExistsError("already there")),
        # The ordinary case: the document moved between the read and the write.
        ('"read"', ResourceModifiedError("moved")),
    ],
)
def test_a_write_that_lost_its_race_is_a_conflict(blob, etag, error):
    """Both SDK errors mean the same thing here, so both surface as one type."""
    blob.upload_raises = [error]
    with pytest.raises(store.ConflictError):
        store.save(Document(), etag=etag)


def test_update_retries_once_against_the_newer_document(blob, doc):
    """Two browsers at once costs a retry, not a payday."""
    blob.content = doc.to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved")]

    updated = store.update(_with_income("Bonus"))

    assert updated.income[-1].name == "Bonus"
    assert len(blob.uploads) == 2
    # The second attempt read again first, so it wrote against the fresh ETag.
    assert blob.uploads[1]["etag"] == '"read"'


def test_update_gives_up_after_the_second_conflict(blob, doc):
    """Two in a row is not contention any more, and retrying forever would hide it."""
    blob.content = doc.to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved"), ResourceModifiedError("again")]

    with pytest.raises(store.ConflictError):
        store.update(_with_income("Bonus"))
    assert len(blob.uploads) == 2


def test_the_retry_applies_the_change_to_what_the_other_writer_left(blob, doc):
    """`update` re-runs `change` against the newer document, which is why it must be pure.

    Asserted on the body actually uploaded: a retry that re-sent the first
    attempt's document would silently drop the other person's change, which is
    the whole failure the ETag exists to catch.
    """
    blob.content = doc.to_json().encode("utf-8")
    blob.upload_raises = [ResourceModifiedError("moved")]
    blob.on_conflict = lambda: setattr(
        blob, "content", _with_income("Theirs")(doc).to_json().encode("utf-8")
    )

    store.update(_with_income("Mine"))

    written = Document.from_json(blob.uploads[1]["body"].decode("utf-8"))
    assert [line.name for line in written.income][-2:] == ["Theirs", "Mine"]
