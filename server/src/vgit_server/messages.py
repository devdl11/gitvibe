"""Generate optional human-facing status messages without altering API errors."""
from .digest import DigestError

OUTCOMES = {
    400: ('The request was invalid.', 'Alas, my liege, this request needs a little mending.'),
    401: ('Authentication is required or failed.', 'Halt! Who goes there? Please authenticate, my liege.'),
    403: ('Permission was denied.', 'Alas, my liege, this gate is beyond your authority.'),
    404: ('The requested resource was not found.', 'Alas, my liege, no such treasure can be found.'),
    409: ('The request conflicts with existing data.', 'A conflict bars our path, my liege.'),
    413: ('The request is too large.', 'This cargo is too mighty for the gates, my liege.'),
    415: ('The request content type is unsupported.', 'This scroll is in an unfamiliar format, my liege.'),
    429: ('Too many requests were made.', 'Patience, my liege; the gatekeeper needs a moment.'),
    503: ('The service is unavailable.', 'The castle is temporarily unavailable, my liege.'),
}


def render(config, status):
    outcome, fallback = OUTCOMES.get(status, ('The operation succeeded.' if status < 400 else 'The operation failed.', 'It is done, my liege.' if status < 400 else 'Alas, my liege, the operation failed.'))
    if callable(getattr(config, "message", None)):
        try:
            return config.message(status, outcome)
        except (DigestError, OSError, ValueError):
            pass
    return fallback
