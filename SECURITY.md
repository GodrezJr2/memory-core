# Security

memory-core holds what your agents know about your projects and machines, so treat a running instance as sensitive.

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repository). You'll get a reply within a week.

## Design notes for deployers

- Every port binds to `127.0.0.1`; expose the app only through something that authenticates (Tailscale serve, or a
  reverse proxy with TLS). Bearer tokens are the only auth on `/mcp` and `/api/v1`.
- `tokens.json` stores SHA-256 hashes, never tokens. Mint one token per agent and rotate them individually.
- Each token carries scope patterns; give agents only the scopes they need.
- Secrets in incoming text (API keys, tokens, bearer headers and similar) are redacted before storage, but redaction
  is pattern-based. Don't rely on it as your only control.
- The live view and phone capture trust the `Tailscale-User-Login` header only on requests whose Host is a `*.ts.net`
  name. Don't put a proxy in front that forwards client-supplied headers on such a host.
