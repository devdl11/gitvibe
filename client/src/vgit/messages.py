"""Human-facing model messages that never replace machine-readable output."""
import os

DEFAULT_PERSONA = 'Speak briefly like a witty medieval knight addressing their liege.'


def render(invoke, action, error=None):
    # Send a bounded, generic category, not exception text, paths, tokens or source.
    if error is None:
        outcome = 'The action completed successfully.'
        fallback = 'It is done, my liege.'
    else:
        text = str(error).lower()
        if 'authentication' in text or '401' in text or 'login' in text:
            outcome = 'Authentication is required or failed.'
        elif '404' in text or 'not found' in text:
            outcome = 'The requested resource was not found.'
        elif 'permission' in text or '403' in text:
            outcome = 'Permission was denied.'
        elif 'exist' in text or 'overwrite' in text:
            outcome = 'The target already exists or cannot be used.'
        elif 'model' in text or 'provider' in text or 'ai ' in text:
            outcome = 'The AI provider or its response failed.'
        else:
            outcome = 'The action failed.'
        fallback = 'Alas, my liege, the task has failed.'
    persona = os.environ.get('GITVIBE_MESSAGE_SYSTEM_PROMPT', DEFAULT_PERSONA)
    try:
        result = invoke(None, {'operation': 'message', 'instructions': 'Write one brief, friendly message about the outcome. Do not invent facts. Return a JSON object with a message string. Style: ' + persona, 'action': action, 'outcome': outcome})
        message = result.get('message')
        if isinstance(message, str) and message.strip() and len(message) <= 500 and not any(ord(c) < 32 for c in message):
            return message.strip()
    except Exception:
        pass
    return fallback
