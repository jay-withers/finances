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
