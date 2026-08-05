"""User preferences demo — multi-module service.

Layers: app.py (domain model) / store.py (in-memory persistence) /
api.py (request handling + auth). The API currently supports listing and
fetching users but NOT preferences — the eval task asks the agent to add
GET/PUT /users/{id}/preferences across these modules.
"""


class User:
    def __init__(self, user_id, name):
        self.id = user_id
        self.name = name
        self.preferences = {}


def new_user(user_id, name):
    return User(user_id, name)
