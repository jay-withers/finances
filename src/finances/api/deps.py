"""The passcode gate, from jay-withers/appkit (`appkit.passcode`).

This is reachable from the public internet the moment ingress is external, so the
gate defaults **on** and a missing passcode fails closed rather than open. It is
a shared passcode rather than Entra sign-in, which suits the data: it is a
household's rather than a person's, so per-user identity would add a sign-in
without adding a boundary. appkit's module docstring has the rest of the
reasoning.
"""

from __future__ import annotations

from appkit.passcode import PasscodeGate

from ..settings import secret, settings

COOKIE_NAME = "finances_session"

gate = PasscodeGate(COOKIE_NAME, settings=settings, secret=secret)

issue = gate.issue
valid = gate.valid
check_passcode = gate.check_passcode
require_session = gate.require_session
