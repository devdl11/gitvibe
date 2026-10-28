# vgit server

A custom Python HTTP server package, independent of the client and using only the standard library at runtime.

Install and start from the project root:

```bash
python -m pip install -e ./server
vgit-server --host 127.0.0.1 --port 7848 --data-dir ./vgit-data
```

Without installation:

```bash
PYTHONPATH=server/src python -m vgit_server --port 7848
```

Accounts, sessions, SSH public keys, repository metadata, and collaborator grants persist in `DATA_DIR/accounts.sqlite3`. The server creates its data directory with owner-only permissions. Keep this directory outside hosted repositories and back it up separately. Restarting the server preserves accounts and unexpired sessions. No CLI package or third-party Python runtime dependency is required. SSH authentication additionally requires OpenSSH `ssh-keygen` with `-Y sign`/`-Y verify` support on the client and server.

## Accounts and authentication

Register with `POST /api/accounts` and log in with `POST /api/auth/login`. Both take `username` and `password`. Names are case insensitive and normalized to lowercase; names must contain 1–39 letters, digits, hyphens or underscores, with a letter or digit at either end. Passwords must contain 12–256 characters. No email address is collected.

```bash
curl -X POST http://127.0.0.1:7848/api/accounts \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"replace with your own password"}'

curl -X POST http://127.0.0.1:7848/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"replace with your own password"}'
```

Login returns `token`, `token_type`, `expires_at` (Unix seconds), and a public `user` object. Send the token as `Authorization: Bearer TOKEN`. Tokens expire after seven days. Logout revokes the current token immediately. Changing a password requires the current password and revokes **all** of the user's sessions, including the current one; log in again afterward.

Passwords use salted [scrypt](https://docs.python.org/3/library/hashlib.html#hashlib.scrypt); only SHA-256 token hashes are stored. Tokens and password hashes are excluded from user responses. Password operations, SSH key registration, and SSH challenge/login requests share a limit of 20 requests per minute per direct peer IP, reset on server restart; HTTP 429 includes `Retry-After: 60`. Behind a proxy, configure public-edge rate limits too, because all proxied requests share the proxy peer address. The built-in server defaults to loopback HTTP; use an HTTPS reverse proxy for remote credentials. Accounts use open registration; email verification, recovery, and MFA are not implemented.

## SSH key login

SSH keys authenticate the HTTP API through a signed challenge. This does not start an SSH daemon or provide an SSH repository transport. Password login remains available for account bootstrap and recovery while you still know your password.

Supported public keys are Ed25519, ECDSA (NIST P-256/P-384/P-521), and RSA of at least 3072 bits. Upload only the single-line `.pub` file, without `authorized_keys` options; private keys and SSH certificates are rejected. Key comments are discarded, fingerprints use SHA-256, and one key can belong to only one account at a time. Each account may register up to 50 keys.

After registering your account, use the client to log in once with your password and register a public key:

```bash
# Install the updated client: python -m pip install -e ./client
# Password is requested interactively; login prints session JSON.
export GITVIBE_TOKEN="$(vgit --skill-issue login --server http://127.0.0.1:7848 --username alice |
  python -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

vgit --skill-issue ssh-key add --server http://127.0.0.1:7848 \
  --title laptop --public-key ~/.ssh/id_ed25519.pub

# Subsequent logins prove ownership of the private key locally.
vgit --skill-issue login --server http://127.0.0.1:7848 --username alice \
  --ssh-key ~/.ssh/id_ed25519

vgit --skill-issue ssh-key list --server http://127.0.0.1:7848
vgit --skill-issue ssh-key remove --server http://127.0.0.1:7848 KEY_ID
```

Encrypted keys can prompt through OpenSSH; alternatively, pass a `.pub` path with the private half loaded in `ssh-agent`. The client requires the matching `.pub` file when a private-key path is passed. Login prints a new session JSON object and does not save credentials automatically. Set `GITVIBE_TOKEN` to its `token` when you want key-management commands to use that session. Remote client authentication requires HTTPS; plain HTTP is accepted only for loopback servers.

Protocol for custom clients:

1. With an existing bearer session, `POST /api/user/ssh-keys` with `title` and `public_key`. The response includes an integer `id` and `fingerprint`.
2. Without a bearer token, `POST /api/auth/ssh/challenge` with `username` and `fingerprint`. The response includes `challenge_id`, `message`, `namespace` (`vgit-login`), and `expires_at`.
3. Sign the **exact UTF-8 message**, including its final newline, using `ssh-keygen -Y sign -f KEY -n vgit-login`. This uses the standard [OpenSSH SSHSIG format](https://man.openbsd.org/ssh-keygen#Y~5). Only the signature and challenge ID are sent back; never send the private key or its passphrase.
4. `POST /api/auth/ssh/login` with `challenge_id` and the armored `signature` string. A verified signature returns the same session format as password login.

Challenges expire in two minutes and are bound to a specific registered key. At most ten unexpired challenges may be pending per key. A verification attempt consumes its challenge even if the signature is wrong. Consumed or expired challenges cannot be replayed, including concurrent attempts. Signatures in another namespace, with another key, or over different message bytes fail.

`GET /api/user/ssh-keys` lists only your own keys. `DELETE /api/user/ssh-keys/ID` removes only a key belonging to you, immediately invalidating its pending challenges and **all sessions created using that key**. Other sessions are preserved. Password changes revoke all sessions and pending challenges but retain registered public keys; remove a compromised key explicitly. Existing databases are upgraded additively on startup without losing accounts or password sessions.

## Repository permissions

Each repository belongs to a user and is addressed as `/api/repos/OWNER/NAME`. Names are unique within an owner namespace. Creation defaults to `private`. The authenticated creator is always the owner; request bodies cannot choose another owner.

| Capability | Public visitor | Read collaborator | Write collaborator | Admin collaborator | Owner |
| --- | --- | --- | --- | --- | --- |
| Read repository metadata | Yes | Yes | Yes | Yes | Yes |
| Read private repository metadata | No | Yes | Yes | Yes | Yes |
| Pass future pull/fetch authorization | Public only | Yes | Yes | Yes | Yes |
| Pass future push authorization | No | No | Yes | Yes | Yes |
| List and manage collaborators | No | No | No | Yes | Yes |
| Change description or visibility | No | No | No | No | Yes |
| Delete repository | No | No | No | No | Yes |

Collaborator grants apply immediately to existing accounts; there is no invitation workflow yet. `PUT` adds a user or changes their role; `DELETE` revokes their grant. Admins may grant/remove other admins, but nobody can demote or remove the owner. A public repository remains readable by everyone after a collaborator grant is removed. Change visibility to private to restrict public access. There is no per-user deny rule for public repositories.

Private repositories return HTTP 404 to unauthorized users and are omitted from their lists. Publicly readable repositories still require an admin grant for collaborator management. Visibility changes and revocations take effect on subsequent requests without reissuing login tokens. Repository deletion requires `{"confirm":"OWNER/NAME"}` and removes collaborator grants in the same transaction.

```bash
# Set TOKEN to the token returned by login.
curl -X POST http://127.0.0.1:7848/api/repos \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"my-vibes","visibility":"private","description":"Knowledge, not source"}'

# Bob must already have registered an account.
curl -X PUT http://127.0.0.1:7848/api/repos/alice/my-vibes/collaborators/bob \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"role":"write"}'

curl http://127.0.0.1:7848/api/user/repos -H "Authorization: Bearer $TOKEN"
```

## HTTP API

| Method | Path | Body / behavior |
| --- | --- | --- |
| GET | `/health` | Service health; no authentication |
| POST | `/api/accounts` | `username`, `password`; creates account (201) |
| POST | `/api/auth/login` | `username`, `password`; creates session |
| POST | `/api/auth/logout` | Empty object; revokes current session |
| POST | `/api/auth/ssh/challenge` | `username`, `fingerprint`; requests a short-lived challenge |
| POST | `/api/auth/ssh/login` | `challenge_id`, `signature`; verifies SSH ownership and creates session |
| GET | `/api/user` | Authenticated account profile |
| PUT | `/api/user/password` | `current_password`, `new_password` |
| GET | `/api/user/ssh-keys` | Lists the authenticated user's SSH public keys |
| POST | `/api/user/ssh-keys` | `title`, `public_key`; registers a key (201) |
| DELETE | `/api/user/ssh-keys/ID` | Empty object; removes key and revokes associated sessions |
| POST | `/api/repos` | `name`, optional `description`, `visibility`; creates repository metadata (201) |
| GET | `/api/user/repos` | Owned repositories and explicit collaborator grants |
| GET | `/api/users/USERNAME/repos` | That user's repositories visible to the caller |
| GET | `/api/repos/OWNER/NAME` | Metadata and caller's effective `permissions` role |
| PATCH | `/api/repos/OWNER/NAME` | Optional `description`, `visibility`; owner only |
| DELETE | `/api/repos/OWNER/NAME` | `confirm` containing full name; owner only |
| GET | `/api/repos/OWNER/NAME/collaborators` | Owner plus collaborators; admin/owner only |
| PUT | `/api/repos/OWNER/NAME/collaborators/USERNAME` | `role`: `read`, `write`, or `admin` |
| DELETE | `/api/repos/OWNER/NAME/collaborators/USERNAME` | Empty object; removes grant |

Repository list responses contain a `repositories` array. Profiles contain only `id`, `username`, and `created_at`. Repository metadata includes `id`, `owner_id`, `owner`, `name`, `full_name`, `description`, `visibility`, `created_at`, and the caller's effective `permissions`.

Bodies must be JSON objects with `Content-Type: application/json`, up to 16 KB, except revision uploads, which allow up to 2,100,000 bytes. Unknown body fields are rejected. Errors are JSON objects with an `error` string and a human-facing `vibe` string: 400 invalid input, 401 missing/invalid authentication, 403 insufficient permissions, 404 absent or hidden resource, 409 duplicate name, 413 oversized request, 415 wrong content type, 429 authentication throttled, or 503 storage unavailable. Query parameters and pagination are not implemented. All API responses disable caching.

The server implements account and repository access management, knowledge revision storage, and streamed reconstruction. Remote source push and server-side digest endpoints remain to be built. The CLI supports login, key management, and remote pull. Organizations, teams, forks, pull requests, account deletion, and ownership transfer are outside this implementation.

## Tests

Run tests from the project root:

```bash
PYTHONPATH=server/src python -m unittest discover -s server/tests -v
```

Tests exercise real SQLite persistence and HTTP parsing/response serialization using in-memory connections, including role escalation attempts, visibility changes, revocation, password changes, expiry, logout, and malformed requests. SSH tests generate real keys and signatures with `ssh-keygen`, including Ed25519, RSA, ECDSA, replay/concurrency failures, key revocation, and database migration. No socket binding or model is required.

## Server digest configuration

Configure one provider for the server process using environment variables. The model must already be available at the selected provider. Restart the server to apply changes.

```bash
export GITVIBE_DIGEST_PROVIDER=ollama  # ollama, openai, or lmstudio
export GITVIBE_DIGEST_MODEL='YOUR_MODEL'
vgit-server
```

Defaults are Ollama `http://127.0.0.1:11434/api/chat`, LM Studio `http://127.0.0.1:1234/v1/chat/completions`, and OpenAI `https://api.openai.com/v1/chat/completions`. Set `GITVIBE_DIGEST_URL` to override the complete endpoint URL. Remote endpoints require HTTPS. OpenAI requires `OPENAI_API_KEY`; Ollama and LM Studio can optionally use `GITVIBE_DIGEST_API_KEY`. Keys stay in the server environment and are not stored in SQLite.

The server creates a configured model service at startup and uses it for server-side streamed reconstruction. Remote source push and server-side digest endpoints are not yet implemented. Without a provider configured, the existing server runs normally. Source sent to a provider may be retained under its policies.

## Knowledge revisions

Repository writers can publish a Markdown specification with `POST /api/repos/OWNER/NAME/revisions` and JSON body `{"knowledge":"..."}`. Each revision gets an ID and parent; revisions are stored in SQLite and deleted with their repository. Knowledge is limited to 2 MB and rejects fenced code blocks. This endpoint accepts already-created knowledge; it does not digest source code.

Readers can fetch the latest revision with `GET /api/repos/OWNER/NAME/revisions` or a specific revision by appending `/REVISION_ID`. Public repositories allow anonymous reads; private repositories require a read grant. The client can fetch knowledge and reconstruct locally with `vgit --skill-issue pull OUTPUT --server ORIGIN --repo OWNER/NAME` (set `GITVIBE_TOKEN` for private repositories). For server-side reconstruction, request `GET /api/repos/OWNER/NAME/revisions/stream` (or `/revisions/REVISION_ID/stream`). The response is newline-delimited JSON events: a `revision`, one or more `file` events with `path` and `content`, then `done`; an `error` event or missing `done` means the stream failed. The server uses its configured digest provider and model to generate file records incrementally. The client validates and stages files before publishing.

## Outcome messages

API errors retain their HTTP status and `error` field and include a human-facing `vibe`. When a server model is configured, vgit asks it to generate a short message from the status and a generic outcome category. The server does not send request bodies, credentials, usernames, repository names, or the original error to the message model. If generation fails or no model is configured, a built-in message is used. Streaming reconstruction includes a `vibe` on `done` or `error` events. Set `GITVIBE_MESSAGE_SYSTEM_PROMPT` in the server environment to change the style; the default is a medieval gatekeeper. Message generation has a 30-second request timeout and may add latency.
