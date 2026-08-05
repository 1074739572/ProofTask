"""Email validation demo service.

The shipped `validate_email` is intentionally buggy: it only checks for an
'@' sign, so 'user@' (no domain) and 'a b@c.com' (space) pass validation.
The eval task asks the agent to implement proper email format validation.
"""


def validate_email(email):
    """Return True if ``email`` looks like a valid email address.

    Contract (hidden acceptance, see checks/):
    - must contain exactly one '@' with non-empty local part
    - domain must contain a dot (e.g. example.com)
    - no whitespace anywhere
    """
    # BUG: this naive check lets 'user@' and 'a b@x.com' through.
    return isinstance(email, str) and "@" in email


def register_user(email, name):
    """Register a user; raises ValueError on invalid email."""
    if not validate_email(email):
        raise ValueError(f"invalid email: {email!r}")
    return {"email": email, "name": name, "active": True}
