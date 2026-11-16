# Security

QueueOps currently targets trusted local development. It provides no API
authentication, authorization, rate limiting, tenant isolation or TLS termination.
Redis is unauthenticated on an internal Compose network and a loopback host port.
Keep all published ports bound to `127.0.0.1`; do not expose this stack to the
internet. An untrusted process on your Mac can still access loopback services.

Store local credentials in the ignored `.env` with owner-only permissions.
`.env.example` contains placeholders only. Do not commit private keys, access
tokens, database dumps, local backups or resolved Compose output. The build copies
only application source and requirements; `.dockerignore` excludes secret files
and development artifacts from the build context.

Existing database and Grafana volume credentials must be preserved during the
configuration migration. Removing a credential from source code does not rotate
it or erase it from history. If a credential was ever pushed or shared, revoke or
rotate it with its issuing service, update the relevant application/database
configuration, then assess history cleanup separately. Do not delete volumes or
rewrite shared history as an automatic fix.

RQ uses JSON serialization rather than pickle. This reduces deserialization risk
but does not make an exposed Redis service safe. Keep Redis private.

Incident analysis uses best-effort redaction across nested fields, URL passwords,
common token formats, bearer tokens and private-key blocks. It is not a data-loss
prevention system. Never send real secrets or sensitive personal data to the
analyzer. Its unauthenticated endpoint can incur costs whenever an OpenAI key is
configured, so keep it local and leave the key unset when not needed.

Review dependencies and images before publishing:

```sh
python -m pip install pip-audit
python -m pip_audit -r requirements-dev.txt
# Scan published history and the proposed staged changes, excluding local data:
gitleaks git --redact=100 .
git diff --cached --no-ext-diff --binary | gitleaks stdin --redact=100
```

Use current scanner versions. Do not post raw scanner output containing matches.
A clean pattern scan is not proof that no credential exists. The current review
ran Gitleaks against publication source/staged changes and pip-audit against the
resolved Python requirements. Local ignored credentials and backups should not
be included in the publication scan. See the readiness review for results. Container
image OS vulnerabilities and a GitHub-hosted CI run remain unverified.

To report a vulnerability, use the repository's private vulnerability reporting
feature if enabled. Do not put credentials or exploit details in public issues.
