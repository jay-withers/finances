"""The reminder email, via Resend.

Copied from jay-withers/market-agent apps/marketagent/src/marketagent/mailer.py
and kept deliberately thin. Fix bugs in both.

**Sending never raises.** The digest job's real output is the log line it writes
on every run — the platform's alerting cannot see a schedule that silently never
fires, so a run that reports nothing is indistinguishable from a run that never
happened. Failing the job because a mail provider had a bad minute would trade a
useful signal for a noisy one.

Resend rather than Azure Communication Services: ACS Email needs a verified
domain or a provisioned Azure-managed one, which is Terraform, a subscription
resource and a monthly line item, against an HTTP POST to a service the estate
already uses for market-agent's daily summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .settings import optional_secret, secret, settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class MailResult:
    """The outcome of one send."""

    # sent | skipped | failed
    status: str
    provider_id: str | None = None
    error: str | None = None


def send(subject: str, html: str, text: str, client: Any = None) -> MailResult:
    """Send the digest, returning what happened rather than raising.

    `skipped` when no recipient is configured, which is the default. A job that
    emails on every run during development is worse than one that has to be
    switched on deliberately — so an absent `DIGEST-EMAIL-TO` is a normal
    outcome, not an error.
    """
    # From Key Vault rather than the environment Terraform injects: a personal
    # address in a public repository is permanent, and a runtime lookup means
    # changing the recipient needs no redeploy.
    recipient = optional_secret("DIGEST-EMAIL-TO")
    if not recipient:
        logger.info("no DIGEST-EMAIL-TO configured, not sending")
        return MailResult(status="skipped")

    recipients = [address.strip() for address in recipient.split(",") if address.strip()]

    try:
        import httpx

        payload = {
            "from": settings().digest_email_from,
            # Resend takes a list even for one recipient.
            "to": recipients,
            "subject": subject,
            "html": html,
            "text": text,
        }
        headers = {
            "Authorization": f"Bearer {secret('RESEND-API-KEY')}",
            "Content-Type": "application/json",
        }
        if client is None:
            response = httpx.post(
                RESEND_ENDPOINT, json=payload, headers=headers, timeout=TIMEOUT_SECONDS
            )
        else:
            response = client.post(RESEND_ENDPOINT, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        # Not raised: see the module docstring. The digest has already been
        # composed and logged by the time this runs, so the information is not
        # lost — only its delivery.
        logger.warning("digest email failed: %s", exc)
        return MailResult(status="failed", error=str(exc))

    provider_id = body.get("id") if isinstance(body, dict) else None
    logger.info("digest emailed to %d recipient(s), id %s", len(recipients), provider_id)
    return MailResult(status="sent", provider_id=provider_id)
