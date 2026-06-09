# Security Policy

Vault Capture is designed to run locally and connect to third-party services with user-provided credentials. Treat local configuration and runtime data as private.

## Do Not Commit

Never commit these files or values:

- `config.ps1`
- `.env` or other local secret files
- `runtime/`
- Zhipu API keys
- Notion integration tokens
- GitHub personal access tokens
- Local Obsidian vault paths that reveal private machine structure
- Captured page history or personal note content

The repository `.gitignore` excludes known local-only files, but review `git status --short --ignored` before every push.

## If a Secret Is Exposed

1. Revoke or rotate the leaked token in the provider dashboard.
2. Remove the secret from the repository history before sharing the repository further.
3. Regenerate local `config.ps1` from `config.example.ps1`.

## Supported Use

This project is a local personal workflow tool. It does not provide hosted authentication, multi-user access control, or a hardened public web service surface.

Only bind the local server to `127.0.0.1` unless you have reviewed and hardened the code for network exposure.
