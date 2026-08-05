"""Password reset demo service (partially implemented).

Ships with user creation + a STUB password reset: request_reset() generates
a token but reset_password() accepts ANY token and never invalidates it.
The eval task asks the agent to implement a real reset flow; hidden
acceptance checks the full chain (see checks/h005_reset_check.py).
"""

import secrets
import time


class UserStore:
    def __init__(self):
        self.users = {}   # email -> {"password": str}
        self.tokens = {}  # token -> {"email": str, "expires": float}

    def create_user(self, email, password):
        if email in self.users:
            raise ValueError(f"user already exists: {email}")
        self.users[email] = {"password": password}
        return {"email": email}

    def verify_password(self, email, password):
        user = self.users.get(email)
        return bool(user) and user["password"] == password

    # --- password reset (to be implemented properly by the agent) ---

    def request_reset(self, email):
        """Generate a reset token for ``email`` (raises if user unknown)."""
        if email not in self.users:
            raise ValueError(f"unknown user: {email}")
        token = secrets.token_urlsafe(16)
        # BUG: token never expires (expires = forever), and it is stored
        # but reset_password() below ignores the store entirely.
        self.tokens[token] = {"email": email, "expires": time.time() + 3600}
        from mailer import send_reset_email

        send_reset_email(email, token)
        return token

    def reset_password(self, token, new_password):
        """Set a new password for the token's user; token must be single-use."""
        # BUG: accepts any token, does not check store, does not invalidate.
        email = next(
            (info["email"] for info in self.tokens.values()), None
        )
        if email is None:
            raise ValueError("invalid or expired token")
        self.users[email]["password"] = new_password
        return {"email": email}
