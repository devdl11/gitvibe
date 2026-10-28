"""Read server reconstruction events without buffering the entire checkout."""
import json
import os
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
from .auth import NoRedirect


def files(server, path):
    parsed = urlsplit(server)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ValueError('Invalid server origin')
    if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Remote servers require HTTPS')
    headers = {}
    token = os.environ.get('GITVIBE_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(server.rstrip('/') + '/api/' + path + '/stream', headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=300) as response:
            if response.headers.get_content_type() != 'application/x-ndjson':
                raise ValueError('Invalid server stream content type')
            count = 0
            finished = False
            for raw in response:
                if len(raw) > 4_000_000:
                    raise ValueError('Server stream event too large')
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError('Invalid server stream event')
                if count == 0:
                    if set(event) != {'type', 'id'} or event['type'] != 'revision' or not isinstance(event['id'], str):
                        raise ValueError('Missing server revision')
                    count += 1
                    yield event
                elif event.get('type') == 'file' and set(event) == {'type', 'path', 'content'} and not finished:
                    yield event
                elif event.get('type') == 'done' and set(event) <= {'type', 'vibe'} and not finished:
                    vibe = event.get('vibe')
                    if isinstance(vibe, str) and len(vibe) <= 500 and not any(ord(c) < 32 for c in vibe):
                        print(vibe, file=sys.stderr)
                    finished = True
                elif event.get('type') == 'error':
                    vibe = event.get('vibe')
                    if isinstance(vibe, str) and len(vibe) <= 500 and not any(ord(c) < 32 for c in vibe):
                        print(vibe, file=sys.stderr)
                    raise ValueError('Server reconstruction failed')
                else:
                    raise ValueError('Invalid server stream event')
            if not finished:
                raise ValueError('Server reconstruction stream was interrupted')
    except HTTPError as error:
        try:
            payload = json.loads(error.read(16384))
            vibe = payload.get('vibe')
            if isinstance(vibe, str) and len(vibe) <= 500 and not any(ord(c) < 32 for c in vibe):
                print(vibe, file=sys.stderr)
        except (ValueError, UnicodeError, AttributeError):
            pass
        raise ValueError(f'Server returned HTTP {error.code}') from None
    except (URLError, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Server reconstruction stream failed') from None
