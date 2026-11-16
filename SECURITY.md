# Security

## Reporting a vulnerability

Use GitHub's [private vulnerability report](https://github.com/YourNoob-web/QueueOps/security/advisories/new)
when available. Include the affected commit, reproduction steps and impact.
Do not include real credentials or personal data. If private reporting is
unavailable, open an issue requesting a private contact channel without posting
exploit details.

## Local security model

QueueOps is intended for trusted local use. It has no authentication,
authorization, rate limiting, tenant isolation or TLS. Keep published ports on
`127.0.0.1`. Other local processes can still access them.

- Store credentials in ignored `.env` with mode `0600`. Never commit keys, dumps,
  logs or private backups. Resolved Compose output contains credentials; validate
  with `docker compose config --quiet`.
- Preserve existing database/Grafana credentials. Changing environment variables
  does not reset accounts in initialized volumes. Do not delete volumes to fix a
  credential mismatch.
- RQ uses JSON serialization, but Redis must remain private and trusted.
- Leave the optional provider key unset. Incident redaction is best effort;
  never submit real secrets or sensitive logs. Enabling that endpoint can incur
  API costs.

Before sharing changes, review dependencies and scan committed/staged content:

```sh
python -m pip_audit -r requirements-dev.txt
gitleaks git --redact=100 .
git diff --cached --no-ext-diff --binary | gitleaks stdin --redact=100
```

These commands require `pip-audit` and Gitleaks installed separately. A clean
scan does not guarantee that every secret or vulnerability has been detected.
See [failure semantics](docs/architecture.md#failure-semantics) for delivery and
concurrency limitations.
