# Sub2Image Studio

A local-first AI image workspace for POD artwork. Start from an uploaded image
or a text prompt, then send every result directly into another image task
without downloading and uploading it again.

## Features

- Preset-driven interface with no technical restoration parameters to tune
- AI artwork generation from a prompt through Sub2API `gpt-image-2`
- Faithful redraw, photo enhancement, illustration enhancement, logo cleanup,
  and line-art cleanup presets
- Smart cutout that automatically chooses chroma-key removal or subject segmentation
- Brush masks for local repair and text correction
- Grayscale, invert, threshold, solid-color, AI recolor, and visual variants
- Chainable 2x and 4x fidelity upscaling for any generated result
- Downloadable SVG vectorization for suitable flat artwork
- Before/after comparison for checking text, logos, colors, and small details
- Reuse any recent source or result as the input to the next task
- Private Cloudflare R2 assets with PostgreSQL ownership and version lineage
- Versioned administrator configuration with AES-256-GCM encrypted service credentials
- Permission-scoped `/admin` operations console with fact-table metrics, approval workflows,
  redacted audit exports, saved views, and responsive data tables
- Authenticated `/app` production workspace with quoted asynchronous tasks, private asset
  previews, job tracking, point ledger, membership entitlements, notifications, and device security

## Quick start

Requirements: Python 3.11+, Node.js 20+.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".\backend[dev]"

Set-Location frontend
npm install
npm run build
Set-Location ..

.\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.
Administrators can open <http://127.0.0.1:8000/admin> after signing in.
Authenticated users enter the production workspace at <http://127.0.0.1:8000/app>.

This local command keeps the legacy synchronous image endpoints enabled for
development compatibility. The production Compose stack disables them so image
processing can only be introduced through the durable Worker path.

## Production foundation

Copy `.env.example` to `.env`, replace `POSTGRES_PASSWORD`, generate independent
`AUTH_TOKEN_PEPPER`, `AUTH_HASH_SALT`, and 256-bit `APP_CONFIG_MASTER_KEY` values,
and set the public HTTPS URL.
The production stack requires TLS because session cookies are always `Secure`.
Start the isolated production services, create the first administrator, then publish
Sub2API and R2 configuration through the authenticated administrator API:

```powershell
docker compose up --build -d
docker compose ps
```

Use an R2 token limited to the private asset bucket; never use a Cloudflare Global
API Key. Sub2API, R2, and email credentials do not belong in `.env`: PostgreSQL
stores only per-version AES-256-GCM ciphertext and metadata. The application stores
only bucket and object keys, and issues 5-15 minute authorized download URLs.

Open <http://127.0.0.1:8080/api/health>. Only Nginx publishes a host port;
PostgreSQL, Redis, Web, Worker, and Scheduler stay on the internal Docker
network. The `migrate` service applies Alembic migrations before Web and workers
start. Web, Worker, and Scheduler use one image with independent commands.
Terminate TLS at Nginx or an upstream load balancer before using authenticated
routes. After migrations complete, create the first administrator interactively:

```powershell
docker compose exec web python -m app.cli create-admin
```

This command accepts no password argument and refuses to create another bootstrap
administrator after the first `super_admin` exists.

For local infrastructure development, set `DATABASE_URL` and `REDIS_URL`, then
run migrations and each process separately:

```powershell
.\scripts\migrate.ps1
.\scripts\start-worker.ps1
.\scripts\start-scheduler.ps1
```

The workspace status in the top-right corner is read-only. Configuration changes
require `config.manage`; connection tests require `config.test`; masked status and
history require `config.read`. Empty secret inputs preserve the prior encrypted
value, while clearing a secret uses the separate confirmation endpoint.

You can also run `./scripts/setup.ps1` once and then `./scripts/start.ps1`.

During frontend development, run `npm run dev` from `frontend`. Vite proxies
`/api` requests to FastAPI on port 8000.

## Quality gates

Run the complete local quality gate from the repository root:

```powershell
.\scripts\quality-check.ps1
```

This runs Ruff, formatting checks, core mypy checks, backend tests and coverage
gates, frontend type checking, component tests, and a production build. Dependency
health can be checked separately with `.\.venv\Scripts\python -m pip check` and
`npm audit --omit=dev --audit-level=high --registry=https://registry.npmjs.org`.
The current automated coverage gate applies to the quality-critical module list in
`backend/pyproject.toml`; full-package coverage is tracked separately and must reach
80% before the productization acceptance gate is considered complete.

External integration tests are optional locally because they require disposable
PostgreSQL, Redis, and S3-compatible services. Configure only isolated test
resources, then add `-Integration`:

```powershell
$env:APP_ENV = "test"
$env:TEST_DATABASE_URL = "postgresql+asyncpg://sub2image:password@127.0.0.1:5432/sub2image_test"
$env:TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
$env:TEST_S3_ENDPOINT = "http://127.0.0.1:9000"
$env:TEST_S3_ACCESS_KEY = "integration-access-key"
$env:TEST_S3_SECRET_KEY = "integration-secret-key"
$env:TEST_S3_BUCKET = "sub2image-test-quality"
.\scripts\quality-check.ps1 -Integration
```

The integration suite refuses database names without a `_test` suffix, Redis
database 0, non-local S3 endpoints, and bucket names outside the
`sub2image-test-` namespace. CI provisions and destroys all three services for
each run through `.github/workflows/quality.yml`.

Local color effects and vectorization do not send images upstream. Smart cutout
uses local chroma-key removal for generated green or magenta backgrounds and the
configured local subject model for other images. AI generation and editing send
only the selected source and optional mask to the configured Sub2API endpoint.

## Image workflow

1. Upload a customer image, select a previous result, or generate new artwork.
2. Choose a plain-language preset such as `忠实重绘`, `Logo 清理`, or `线稿增强`.
3. Compare the result with its parent and verify text, logos, colors, and geometry.
4. Continue from that result with cutout, masked repair, recolor, variants, or SVG.
5. Download the current PNG or the generated SVG.

AI reconstruction can recover a clean, printable-looking design, but it cannot
guarantee a mathematically identical copy of information that is unclear in the
source photo. Always verify brand marks, small text, and exact geometry before
production.

## API

- `GET /api/health`
- `GET /api/v1/system/status` (authentication gate; implemented in phase 02)
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `POST /api/v1/auth/logout-all`
- `GET/PATCH /api/v1/auth/me`
- `POST /api/v1/auth/password/change`
- `POST /api/v1/auth/password/forgot`
- `POST /api/v1/auth/password/reset`
- `GET /api/v1/auth/sessions`
- `DELETE /api/v1/auth/sessions/{session_id}`
- `GET/POST /api/v1/admin/users`
- `GET/PATCH /api/v1/admin/users/{user_id}`
- `POST /api/v1/admin/users/{user_id}/disable`
- `POST /api/v1/admin/users/{user_id}/enable`
- `POST /api/v1/admin/users/{user_id}/revoke-sessions`
- `GET /api/v1/admin/permissions`
- `GET/POST /api/v1/admin/roles`
- `PATCH /api/v1/admin/roles/{role_id}`
- `PUT /api/v1/admin/roles/{role_id}/permissions`
- `GET/PUT /api/v1/admin/users/{user_id}/roles`
- `GET /api/v1/membership/me`
- `GET /api/v1/membership/plans`
- `GET/POST /api/v1/admin/membership-plans`
- `GET/PATCH /api/v1/admin/membership-plans/{plan_id}`
- `POST /api/v1/admin/membership-plans/{plan_id}/activate`
- `POST /api/v1/admin/membership-plans/{plan_id}/deactivate`
- `GET/POST /api/v1/admin/users/{user_id}/memberships`
- `POST /api/v1/admin/memberships/{membership_id}/renew`
- `POST /api/v1/admin/memberships/{membership_id}/change-plan`
- `POST /api/v1/admin/memberships/{membership_id}/cancel`
- `GET /api/v1/points/balance`
- `GET /api/v1/points/transactions`
- `GET /api/v1/admin/points/accounts`
- `GET /api/v1/admin/points/transactions`
- `GET /api/v1/admin/users/{user_id}/points`
- `POST /api/v1/admin/users/{user_id}/points/adjustments`
- `GET /api/v1/admin/point-adjustments`
- `POST /api/v1/admin/point-adjustments/{adjustment_id}/approve`
- `POST /api/v1/admin/point-adjustments/{adjustment_id}/reject`
- `POST /api/v1/admin/point-transactions/{transaction_id}/reverse`
- `GET /api/v1/operations`
- `POST /api/v1/jobs/quote`
- `GET/POST /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`
- `POST /api/v1/jobs/{job_id}/cancel`
- `GET /api/v1/jobs/{job_id}/events`
- `GET/PATCH /api/v1/admin/operations[/{code}]`
- `GET/POST /api/v1/admin/operation-prices`
- `GET /api/v1/admin/jobs`
- `POST /api/v1/admin/jobs/{job_id}/retry`
- `POST /api/v1/admin/jobs/{job_id}/reconcile`
- `POST /api/v1/assets/upload`
- `GET /api/v1/assets`
- `GET /api/v1/assets/{asset_id}`
- `GET /api/v1/assets/{asset_id}/lineage`
- `POST /api/v1/assets/{asset_id}/download-url`
- `DELETE /api/v1/assets/{asset_id}`
- `POST /api/v1/assets/{asset_id}/restore`
- `GET /api/v1/admin/assets`
- `GET /api/v1/admin/assets/{asset_id}`
- `POST /api/v1/admin/assets/{asset_id}/download-url`
- `POST /api/v1/admin/assets/{asset_id}/quarantine`
- `POST /api/v1/admin/assets/{asset_id}/restore`
- `DELETE /api/v1/admin/assets/{asset_id}`
- `POST /api/v1/admin/storage/orphans/scan`
- `POST /api/v1/admin/storage/orphans/reconcile`
- `GET /api/v1/admin/config`
- `GET /api/v1/admin/config/{group}`
- `POST /api/v1/admin/config/{group}/drafts`
- `PATCH /api/v1/admin/config/{group}/drafts/{version}`
- `POST /api/v1/admin/config/{group}/drafts/{version}/test`
- `POST /api/v1/admin/config/{group}/drafts/{version}/publish`
- `POST /api/v1/admin/config/{group}/versions/{version}/rollback`
- `POST /api/v1/admin/config/{group}/secrets/{key}/clear`
- `GET /api/v1/admin/config/{group}/history`
- `GET /api/v1/admin/dashboard/summary`
- `GET /api/v1/admin/dashboard/timeseries`
- `GET /api/v1/admin/search`
- `GET/POST /api/v1/admin/action-requests`
- `POST /api/v1/admin/action-requests/{id}/approve`
- `POST /api/v1/admin/action-requests/{id}/reject`
- `GET/POST /api/v1/admin/saved-views`
- `DELETE /api/v1/admin/saved-views/{id}`
- `GET /api/v1/admin/audit`
- `GET /api/v1/admin/audit-logs[/{id}]`
- `POST /api/v1/admin/audit-logs/export`
- `GET /api/v1/admin/security/events`
- `POST /api/v1/admin/security/events/{id}/resolve`
- `GET/POST /api/v1/admin/security/blocks`
- `POST /api/v1/admin/security/blocks/{id}/revoke`
- `GET /api/v1/admin/security/rate-limits`
- `PATCH /api/v1/admin/security/rate-limits/{policy}`
- `GET /api/v1/admin/export/{module}`
- `GET /api/v1/admin/exports/download`
- `GET /api/v1/admin/system/health` (permission gate)
- `GET /api/v1/admin/system/workers` (permission gate)
- `POST /api/v1/admin/system/jobs/reconcile` (permission gate)
- `GET /api/v1/app/bootstrap`
- `GET/PATCH /api/v1/me/preferences`
- `GET /api/v1/me/notifications`
- `POST /api/v1/me/notifications/{id}/read`
- `POST /api/v1/me/notifications/read-all`
- `GET /api/capabilities`
- `POST /api/generate`
- `POST /api/edit`
- `POST /api/remove-background`
- `POST /api/upscale`
- `POST /api/assets`
- `GET /api/assets`
- `GET /api/assets/{id}/lineage`
- `GET /api/jobs`
- `POST /api/extract-print`
- `POST /api/restore`
- `POST /api/chain/remove-background`
- `POST /api/chain/upscale`
- `POST /api/chain/remove-solid-background`
- `POST /api/chain/smart-cutout`
- `POST /api/chain/ai-reconstruct`
- `POST /api/chain/ai-transform`
- `POST /api/chain/generate`
- `POST /api/chain/color-effect`
- `POST /api/chain/vectorize`
- `POST /api/preflight`
- `GET /api/results/{filename}`

Generated and edited images are returned by Sub2API as Base64 and saved to the
local result directory. Background removal and upscaling never leave the host.

## Security

- `.env` is ignored by Git.
- Passwords use Argon2id; session and reset tokens are stored only as keyed hashes.
- Browser authentication uses server-side `HttpOnly`, `Secure`, `SameSite=Lax` cookies.
- Login failures are throttled by both account and IP fingerprints without exposing account existence.
- RBAC permissions come from the database catalog; built-in role grants are versioned migrations,
  and permission changes take effect on the next request without replacing the session cookie.
- Membership roles are independent from RBAC. Free, Basic, and Pro entitlements are stored as
  per-cycle snapshots, and all administrator membership changes require an idempotency key.
- Point balances are changed atomically and backed by an append-only ledger. PostgreSQL blocks
  transaction updates and deletes; corrections are recorded as reversal transactions.
- Large manual point adjustments require approval by a second `points.adjust` administrator.
  Daily reconciliation freezes inconsistent accounts for investigation instead of rewriting history.
- Image jobs use five-minute versioned quotes, atomically persist their pricing snapshot and charge,
  and enforce per-membership concurrency limits. Replayed task requests cannot charge twice.
- Workers claim queued jobs with guarded state transitions. Cancellation, terminal failure, and timeout
  refund once; delayed Worker results cannot replace a compensated terminal state.
- Production assets use server-generated R2 object keys and remain private. Uploads are decoded,
  normalized without EXIF, hashed, and tied to an owner before a short-lived URL can be signed.
- Deletion is soft for seven days, then an idempotent Scheduler queue removes the object. Stale uploads,
  missing objects, and untracked R2 objects are reconciled without exposing storage credentials.
- Sub2API, R2, and email secrets use an independent AES-GCM nonce per field and bind the
  configuration group plus key name as authenticated data. Read APIs return only presence,
  last four characters, and timestamps; publication invalidates 30-60 second runtime caches.
- New Sub2API jobs record the active configuration version without copying credentials. Workers
  resolve that version in memory and retain their last valid configuration if refresh fails.
- Administrator exports require both module-read and audit-export permissions. Download URLs are
  short-lived, signed, bound to the requesting administrator, redacted, and audited twice.
- High-risk operation requests reject secret-bearing payloads, require explicit confirmation,
  and cannot be approved by the administrator who created the request.
- Business audit outbox events are projected in the same transaction to an append-only audit table;
  exports and detail APIs redact credentials, full prompts, image bytes, and IP values.
- Login, upload, quote, job creation, and download have independently configurable IP/user limits.
  User and IP-fingerprint blocks are checked server-side before protected work is performed.
- Repeated upload hashes, anomalous daily point spend, failure bursts, rate-limit violations, and
  upstream circuit opening create reviewable security events.
- Inputs are decoded and normalized before processing or upstream upload.
- Upload byte size and decoded pixel count are limited.
- Legacy synchronous workflow assets remain local and are not sent upstream unless an AI task is run.
- EXIF metadata is removed during normalization.

## Legacy asset migration

After configuring PostgreSQL and R2, validate an existing local catalog and
result directory without uploading:

```powershell
.\.venv\Scripts\python -m app.cli migrate-local-results `
  --owner-id <user-uuid> --dry-run
```

Remove `--dry-run` to upload. The command checks each legacy catalog hash,
preserves version lineage, reads every uploaded object back, and verifies its
SHA-256. Source files are never deleted.
