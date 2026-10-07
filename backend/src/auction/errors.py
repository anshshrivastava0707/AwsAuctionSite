"""Domain errors that the HTTP handler maps to status codes (ValidationError -> 400 lives in models)."""


class NotFound(Exception):
    """-> 404"""


class Forbidden(Exception):
    """-> 403"""


class Conflict(Exception):
    """-> 409: the request was valid but the current state doesn't allow it."""
