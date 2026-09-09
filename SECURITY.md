# Security operations

## Reporting

Do not include credentials, signed URLs, customer images, or full prompts in an issue. Send the
request ID, UTC time, affected account ID, and a redacted description through the private incident
channel configured by the operator. Rotate any credential that was pasted into a public system.

## Automated checks

- Pull requests, pushes to `main`, and the weekly schedule run Python and Node vulnerability scans
  plus repository secret scanning in `.github/workflows/security.yml`.
- Production Python installs use `backend/constraints.lock`; frontend installs use `npm ci` and
  `frontend/package-lock.json`. Dependency changes must update the matching lock and pass the scan.
- Run `./scripts/security-check.ps1` locally. Release builds use `-Strict`, which requires
  `pip-audit` and `gitleaks` to be installed.
- High or critical findings block release unless an owner records scope, compensating controls, and
  an expiry date for the exception.

## Audit and retention

- `audit_logs` is append-only and remains online for at least 180 days before encrypted archival.
- Login attempts and rate-limit details are retained for 90 days, then deleted by an approved
  maintenance job. Security events remain linked to pseudonymous user and IP fingerprints.
- Exports require `audit.export`, abbreviate IP fingerprints, exclude images and full prompts, and
  create their own audit event. Application logs use a field allowlist and redact common secrets.
- Account deletion anonymizes profile and device fields while preserving the minimum ledger, audit,
  and security identifiers required by policy.

## Encrypted backups

Run `scripts/backup-production.ps1` from a controlled host with PostgreSQL client tools and `age`.
The script creates a PostgreSQL custom-format dump, encrypts it to the recovery public key, removes
the plaintext temporary file, and emits a SHA-256 manifest. Keep the `age` private key offline in two
separate controlled locations; do not store it beside the database or backup object.

Upload the `.age` file and manifest to versioned restricted storage on a different failure domain.
Test a full restore in an isolated environment every month, verify database row counts, sample R2
object hashes, and record the recovery point and recovery time. A backup job that merely reports
success does not count as a restore test.

## Incident response

1. Triage the security event, preserve request IDs and audit exports, and assign severity and owner.
2. Contain with a user or IP-fingerprint block, session revocation, affected operation disablement,
   or Sub2API circuit breaker. Do not delete or edit audit records.
3. Rotate exposed credentials, invalidate signed URLs and sessions, patch the cause, then verify the
   fix with cross-user and replay tests.
4. Recover from a known-good encrypted backup when integrity is uncertain. Reconcile point ledgers,
   jobs, and R2 hashes before reopening traffic.
5. Resolve the event with a reason, document the UTC timeline and affected data, complete required
   notifications, and add a regression test and follow-up owner.

Critical events require immediate containment and escalation. High events require same-day review;
medium and low events enter the normal security queue. Recovery must explicitly re-enable temporary
blocks, rate policies, operations, and circuit breakers so degraded controls do not become permanent.
