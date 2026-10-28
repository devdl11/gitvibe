# vgit client

Install from the project root:

```bash
python -m pip install -e ./client
vgit --help
vgit --skill-issue init /path/to/project
```

Without installation:

```bash
PYTHONPATH=client/src python -m vgit --help
```

The client supports filesystem repositories plus HTTP server login and SSH key management. Its AI adapter protocol and push/pull examples are documented in the [project README](../README.md).

```bash
export GITVIBE_AI_PROVIDER=ollama
export GITVIBE_AI_MODEL=YOUR_MODEL
vgit --skill-issue commit --project /path/to/project
vgit --skill-issue status --project /path/to/project
vgit --skill-issue export /tmp/my-vibes --project /path/to/project
vgit --skill-issue pull /tmp/new-checkout --repo /tmp/my-vibes
```

Initialization creates `.vgit` without requiring the project directory to be empty. From a project or its subdirectory, `init`, `commit`, `status`, and `export DEST` use the current project by default; `init` initializes the directory specified or the current directory. `commit` stores a complete knowledge revision and source file hashes; `status` reports added, changed, and deleted supported files. Export copies committed history to a separate new, empty, or matching standalone repository. It refuses conflicting histories; the export itself does not use a model, but its outcome message can. The legacy direct workflow remains available with `init --bare DEST` and `push SOURCE --repo DEST`.

`vgit --skill-issue push DEST` is an alias for exporting the current project's commits. The natural-language prompt `vgit 'push to the local path ../test2'` can select this workflow; prompt interpretation itself uses the configured model.

`vgit 'do something'` can select the automatic action, also available explicitly as `vgit --skill-issue auto`. It initializes the current directory if no local repository exists; otherwise it commits supported changes, or reports that there is nothing to commit. It never chooses an export destination or rebuilds code. Prompt selection and a needed commit call the configured model; outcome messages also use the model when available.

## Prompt interface

Use `vgit 'initialize /path/to/project'`, `vgit 'commit /path/to/project'`, `vgit 'export knowledge from /path/to/project to /tmp/my-vibes'`, or `vgit 'pull /tmp/my-vibes to /tmp/new-checkout'`. The configured AI model selects the action; include paths and other details in the request. Prompt selection itself calls the model, even for export or status; use the explicit interface to avoid that call. The model is instructed to ask for missing details, and selected arguments are validated, but model interpretations can be wrong. The prompt is sent to the configured provider, which may be remote. Set `GITVIBE_PROMPT_SYSTEM_PROMPT` to change the default medieval reply style. The command schema remains fixed. For authentication examples and the full command reference run `vgit --help` and `vgit --skill-issue --help`.

## Rebuild existing code

`vgit --skill-issue rebuild /path/to/project --repo /tmp/my-vibes` reconstructs the latest revision and replaces the entire existing target directory, including hidden and untracked files. Add `--revision REVISION_ID` to select an older revision or `--ai-command` to override the adapter. You can also ask `vgit 'rebuild /path/to/project from /tmp/my-vibes'`. Generation and validation complete before replacement; on swap failure the client attempts to restore the original. A crash or cleanup failure may leave the original in a sibling `.vgit-backup-*` directory. The target must be separate from the knowledge repository. Back up anything you need to keep and review generated code before running it.

## Server login

```bash
# Prompt for the account password; print session JSON.
vgit --skill-issue login --server http://127.0.0.1:7848 --username alice

# Register a public key using the token returned by login.
export GITVIBE_TOKEN='YOUR_SESSION_TOKEN'
vgit --skill-issue ssh-key add --server http://127.0.0.1:7848 \
  --title laptop --public-key ~/.ssh/id_ed25519.pub

# Authenticate by signing a challenge locally.
vgit --skill-issue login --server http://127.0.0.1:7848 --username alice \
  --ssh-key ~/.ssh/id_ed25519

vgit --skill-issue ssh-key list --server http://127.0.0.1:7848
vgit --skill-issue ssh-key remove --server http://127.0.0.1:7848 KEY_ID
```

SSH login requires OpenSSH `ssh-keygen` with SSHSIG support. Use a private key with its matching `.pub` beside it, or supply a `.pub` file whose private half is available in `ssh-agent`. Private keys stay local. Sessions are printed as JSON, not saved automatically; `GITVIBE_TOKEN` supplies a session for key management. Remote authentication requires HTTPS; loopback HTTP works for local development. Remote pull can fetch hosted knowledge for local reconstruction; remote source push is not implemented. See the [server documentation](../server/README.md#ssh-key-login) for the protocol and revocation behavior.

Run tests from the project root:

```bash
PYTHONPATH=client/src python -m unittest discover -s client/tests -v
```

## Built-in model configuration

Set `GITVIBE_AI_PROVIDER` to `codex`, `ollama`, `openai`, or `lmstudio`, and set `GITVIBE_AI_MODEL` to a model available at that provider. These settings apply to both push and pull. Start local model providers and load the model yourself.

### Codex CLI provider

Install Codex CLI and sign in separately (`codex login`). GitVibe can use that local login without an API key configured in GitVibe:

```bash
export GITVIBE_AI_PROVIDER=codex
export GITVIBE_AI_MODEL=gpt-6.1-sol  # Choose a model available to your Codex account.
vgit --codex-dry-run
vgit --codex-debug --skill-issue push /path/to/project --repo /tmp/my-vibes
```

The dry run prints the planned launch and does not start Codex, read project files, check login or model access, or call a model. The debug flag must precede the command; it prints the launch and operation to stderr before each Codex request. If Codex fails, debug mode also prints up to the last 16,000 characters of Codex's stderr, which can contain sensitive information; review it before sharing. Debug mode does perform the operation. An explicit `--ai-command` or `GITVIBE_AI_COMMAND` takes precedence over this provider; the dry run warns about the environment override.

Codex is launched in a temporary working directory with a read-only sandbox and an ephemeral session. It inherits local Codex configuration and authentication. GitVibe sends source or knowledge to the model via stdin; the model service is remote. The adapter requests JSON and validates it, but does not guarantee reconstruction fidelity. Calls have a 280-second timeout. Outcome messages and natural-language action selection also invoke Codex. Server-mode generation uses the server's independent configuration and does not use this client provider.

The default endpoints are `http://127.0.0.1:11434/api/chat` for Ollama, `http://127.0.0.1:1234/v1/chat/completions` for LM Studio, and `https://api.openai.com/v1/chat/completions` for OpenAI. Set `GITVIBE_AI_URL` to the complete endpoint URL to override it. Remote endpoints require HTTPS. OpenAI uses `OPENAI_API_KEY`; local providers may use `GITVIBE_AI_API_KEY` if authentication is configured. Credentials are read from the environment.

To diagnose an HTTP error, prefix the operation with `vgit --provider-debug`; this prints up to 8 KB of the provider's error response to stderr. The response may contain sensitive information, so inspect it before sharing. Debug mode performs the operation.

An explicit `--ai-command` takes precedence over `GITVIBE_AI_COMMAND`, which takes precedence over these built-in provider settings. The original `python -m vgit.ollama MODEL` adapter remains available. Provider settings are client-side and independent of the server's digest settings.

Custom adapters also receive `operation: "select_action"`, `instructions`, and `prompt`. They must return a JSON object containing `action`, `arguments`, and `reply`; the instructions describe the supported actions and fields. Use a null action and empty arguments for clarification.

For a hosted revision, use `vgit --skill-issue pull OUTPUT --server ORIGIN --repo OWNER/NAME [--revision ID]`. The client fetches knowledge and reconstructs it with its configured model; `GITVIBE_TOKEN` supplies credentials for private repositories. Add `--mode server` to have the server generate and stream files; the server must have a model configured. The default `--mode local` fetches knowledge for the client model.

## Outcome messages

The CLI asks its configured model or adapter for a short in-character message after an action succeeds or fails. It prints this message to stderr; normal command output stays on stdout, and errors retain their original details and exit status. `GITVIBE_MESSAGE_SYSTEM_PROMPT` changes the style independently of the action selector's `GITVIBE_PROMPT_SYSTEM_PROMPT`. Only the command name and a generic outcome category are sent for message generation, not source, paths, credentials, or the raw error. If the model is unavailable or returns an invalid message, a built-in fallback is used.

Custom adapters receive `operation: "message"`, `instructions`, `action`, and `outcome`, and must return `{"message": "..."}`. Message requests have a 30-second timeout and may add latency.
