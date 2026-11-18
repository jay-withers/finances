"""Secret resolution as the suite sees it: never from a developer's `.env`."""

from __future__ import annotations

import pytest

from finances import settings


def test_a_dotenv_in_the_working_directory_supplies_no_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression: conftest's stub once patched `settings.dotenv`, a re-export
    appkit.keyvault never calls, so a developer's `.env` reached the tests."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RESEND_API_KEY=from-a-developers-dotenv\n")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    assert settings.optional_secret("RESEND-API-KEY") is None
