"""`finances selfcheck`, which `make smoke` runs in every job after a deploy.

It has to fail on exactly what would break a real run, and do nothing else: a
false failure rolls back a good release, and a side effect would fire on every
deploy.
"""

from __future__ import annotations

import logging

import pytest

from finances import selfcheck, store
from finances.cli import main


@pytest.fixture
def storage_ok(monkeypatch):
    monkeypatch.setattr(store, "load", lambda: (None, None))
    monkeypatch.setenv("RESEND_API_KEY", "value-of-secret")


def test_passes_when_the_secrets_and_the_storage_resolve(storage_ok):
    assert selfcheck.run() == 0


def test_fails_on_a_missing_secret_and_names_it(storage_ok, monkeypatch, caplog):
    monkeypatch.delenv("RESEND_API_KEY")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret RESEND-API-KEY" in caplog.text


def test_fails_when_storage_cannot_be_read(storage_ok, monkeypatch, caplog):
    def broken():
        raise RuntimeError("blob down")

    monkeypatch.setattr(store, "load", broken)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL storage" in caplog.text


def test_never_logs_a_secret_value(storage_ok, monkeypatch, caplog):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "value-of-secret")
    with caplog.at_level(logging.DEBUG):
        selfcheck.run()
    assert "value-of-secret" not in caplog.text


def test_is_dispatched_from_the_cli(monkeypatch):
    monkeypatch.setattr(selfcheck, "run", lambda: 0)
    assert main(["selfcheck"]) == 0


@pytest.fixture
def backup_job(monkeypatch):
    """What the backup job is given: a bucket, and its three secrets."""
    from pyrage import x25519

    from finances import backup

    monkeypatch.setenv("BACKUP_BUCKET", "test-backups")
    monkeypatch.setenv("BACKUP_R2_ENDPOINT", "https://acc.eu.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", str(x25519.Identity.generate().to_public()))
    for name in selfcheck.BACKUP_SECRETS:
        monkeypatch.setenv(name.replace("-", "_"), "value-of-secret")
    monkeypatch.setenv("BACKUP_AGE_DRILL_KEY", str(x25519.Identity.generate()))
    calls: list[str] = []
    monkeypatch.setattr(backup.JOB, "check_bucket", lambda: calls.append("listed"))
    return calls


def test_the_backup_job_also_checks_its_keys_and_bucket(storage_ok, backup_job, caplog):
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert backup_job == ["listed"]
    assert "ok secret BACKUP-AGE-DRILL-KEY" in caplog.text
    assert "ok backup bucket" in caplog.text
    assert "ok backup settings and keys" in caplog.text


def test_the_backup_job_fails_on_an_unset_endpoint(storage_ok, backup_job, monkeypatch, caplog):
    monkeypatch.delenv("BACKUP_R2_ENDPOINT")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL backup settings and keys: ValueError: not configured, BACKUP_R2_ENDPOINT" in (
        caplog.text
    )


def test_settings_that_dont_load_are_reported_not_raised(storage_ok, monkeypatch, caplog):
    from functools import cache

    from finances import settings as settings_module

    # Cached like the real one, so the suite's teardown can clear it.
    @cache
    def broken() -> None:
        raise ValueError("bad setting")

    monkeypatch.setattr(settings_module, "settings", broken)
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL settings: ValueError: bad setting" in caplog.text


def test_the_backup_job_fails_without_its_r2_keys(storage_ok, backup_job, monkeypatch, caplog):
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "FAIL secret R2-SECRET-ACCESS-KEY" in caplog.text


def test_the_digest_needs_neither_the_bucket_nor_the_backup_keys(storage_ok, caplog):
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "backup bucket" not in caplog.text
    assert "R2-" not in caplog.text


# --- integrity -----------------------------------------------------------------


def test_a_stored_rule_violation_does_not_fail_the_deploy(storage_ok, doc, caplog):
    """A deploy can't change the data; one old bad entry mustn't block every
    release. The nightly backup reports it instead."""
    doc.pot_entries[2].amount_pence = 30_000  # a spend stored as money in
    store.local_path().write_text(doc.to_json(), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "selfcheck: ok integrity finances.json" in caplog.text


def test_a_stored_field_this_release_would_drop_fails(storage_ok, doc, caplog):
    """A model that no longer knows a stored key would lose it on the next write."""
    import json

    raw = json.loads(doc.to_json())
    raw["pots"][0]["colour"] = "teal"  # a field from some other release
    store.local_path().write_text(json.dumps(raw), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 1
    assert "$.pots[0].colour: dropped" in caplog.text
    assert "teal" not in caplog.text


def test_a_good_stored_document_passes(storage_ok, stored, caplog):
    with caplog.at_level(logging.INFO):
        assert selfcheck.run() == 0
    assert "selfcheck: ok integrity finances.json" in caplog.text
