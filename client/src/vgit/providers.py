"""Built-in model providers for digest and reconstruction."""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import codex


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run(request, env=None):
    env = os.environ if env is None else env
    provider = env.get("GITVIBE_AI_PROVIDER", "").strip().lower()
    if provider == "codex":
        return codex.run(request, env)
    defaults = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "ollama": "http://127.0.0.1:11434/api/chat",
        "lmstudio": "http://127.0.0.1:1234/v1/chat/completions",
    }
    if provider not in defaults:
        raise ValueError("Set GITVIBE_AI_PROVIDER to codex, openai, ollama, or lmstudio, or configure an AI command")
    model = env.get("GITVIBE_AI_MODEL", "").strip()
    if not model:
        raise ValueError("Set GITVIBE_AI_MODEL to the provider's model name")
    url = env.get("GITVIBE_AI_URL", defaults[provider]).strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Invalid AI endpoint URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote AI endpoints require HTTPS")
    except ValueError:
        raise ValueError("Invalid AI endpoint URL, or remote endpoint does not use HTTPS") from None
    key = env.get("OPENAI_API_KEY", "") if provider == "openai" else env.get("GITVIBE_AI_API_KEY", "")
    if provider == "openai" and not key:
        raise ValueError("Set OPENAI_API_KEY for OpenAI")
    instructions = request.get("instructions")
    data = {k: v for k, v in request.items() if k != "instructions"}
    payload = {"model": model, "stream": False, "messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(data)},
    ]}
    if provider == "ollama":
        payload["format"] = "json"
    elif provider == "lmstudio":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "gitvibe_response", "schema": {"type": "object"}
        }}
    else:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with build_opener(NoRedirect).open(Request(url, data=json.dumps(payload).encode(), headers=headers), timeout=30 if request.get("operation") == "message" else 280) as response:
            raw = response.read(4_000_001)
    except HTTPError as error:
        if env.get('GITVIBE_PROVIDER_DEBUG') == '1' and request.get('operation') != 'message':
            try:
                detail = error.read(8192).decode('utf-8', errors='replace')
            except OSError:
                detail = ''
            if detail:
                print(f'{provider} error response (may contain sensitive details):\n{detail}', file=sys.stderr)
        raise ValueError(f'{provider} returned HTTP {error.code}; use --provider-debug for the provider error response') from None
    except TimeoutError:
        raise ValueError(f'{provider} request timed out; the model may need more time for this project') from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ValueError(f'{provider} request timed out; the model may need more time for this project') from None
        raise ValueError(f'Cannot connect to {provider}; check that its local server is running and the endpoint is correct') from None
    except OSError:
        raise ValueError(f'{provider} request failed; check the server and its logs') from None
    if len(raw) > 4_000_000:
        raise ValueError("AI response exceeds size limit")
    try:
        result = json.loads(raw)
        content = result["message"]["content"] if provider == "ollama" else result["choices"][0]["message"]["content"]
        value = json.loads(content)
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError(f'{provider} returned an invalid response or invalid JSON; check the model output and server logs') from None
    if not isinstance(value, dict):
        raise ValueError("AI response must be an object")
    return value
