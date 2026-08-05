"""In-memory user store."""

from app import new_user


class UserStore:
    def __init__(self):
        self._users = {}

    def add_user(self, user_id, name):
        if user_id in self._users:
            raise ValueError(f"user exists: {user_id}")
        self._users[user_id] = new_user(user_id, name)
        return self._users[user_id]

    def get_user(self, user_id):
        return self._users.get(user_id)

    def all_users(self):
        return list(self._users.values())
