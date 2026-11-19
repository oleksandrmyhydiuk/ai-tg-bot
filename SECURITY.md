# Security Policy

## Supported Versions

Security updates and patches are applied to the latest state of the `main` branch.

| Version / Branch | Supported          |
| ---------------- | ------------------ |
| `main`           | :white_check_mark: |
| Older commits    | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please **do not** open a public GitHub issue.

Instead, report it responsibly using one of the following methods:
1. Open a private security advisory via GitHub's **Security -> Advisories -> Report a vulnerability** tab in this repository.
2. Contact the maintainer directly via GitHub (`@oleksandrmyhydiuk`).

Please include:
- A clear description of the vulnerability and its potential impact.
- Steps to reproduce or a minimal proof-of-concept.
- Affected files or configuration details (without exposing real API keys or tokens).

You can expect an initial acknowledgment within **48 hours** and a remediation plan once the issue is confirmed.

## Security Practices in This Repository

- **Secrets Management**: API keys (`BOT_TOKEN`, `GEMINI_API_KEY`) are loaded strictly from environment variables / `.env` and excluded from version control (`.gitignore`) and Docker build contexts (`.dockerignore`).
- **Least-Privilege Container**: The production `Dockerfile` runs the application as a non-root user (`botuser`, UID `10001`).
- **SQL Injection Protection**: All database operations use parameterized SQLAlchemy ORM/Core queries.
- **Rate Limiting & Input Caps**: Per-user request cooldowns protect API quotas, and image uploads are capped at 10 MB.
- **Access Control**: Destructive chat history commands (`/clear`) are restricted to group administrators.
- **Automated Checks**: CI runs `bandit` static security analysis, `ruff` linting, and `pip-audit` dependency vulnerability scanning on every push and pull request.
