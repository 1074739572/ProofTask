"""Outbox for reset emails. Tests assert on SENT instead of real SMTP."""

SENT: list[tuple[str, str]] = []


def send_reset_email(email, token):
    SENT.append((email, token))
