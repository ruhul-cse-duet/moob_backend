# WebImove — Backend API

AI-powered immigration case management. Python · FastAPI · MongoDB (database per tenant) · SMTP OTP · OpenAI.

Built directly from the Figma file `moob02 || AI-Powered Immigration Case Management`
(pages: *App UI* — Consultant / Partner / Client mobile apps, and *Website & Platform Admin* —
the consultant web workspace).

---

## Architecture decisions (and why)

**One API, role-scoped routers — not one API per surface.**
The three mobile apps and the consultant website consume the same endpoints. What a caller
can see is decided by the `role` claim inside the JWT and the guards in `app/core/deps.py`,
not by a separate service per dashboard. Separate APIs would duplicate auth, models and
permission logic — three places for the same bug.

**Files live in MongoDB GridFS, inside the tenant's own database — never on local disk.**
Uploads go to two GridFS buckets per tenant DB: `documents.*` (client documents) and
`deliverables.*` (partner work product). GridFS rather than a binary field because BSON
documents cap at 16 MB and uploads are allowed up to `MAX_UPLOAD_MB` (25). Downloads stream
in 256 KB chunks, so a large file is never held in memory twice. Replacing an existing upload
deletes the previous blob, so GridFS does not accumulate orphans. Because storage lives in
the tenant database, deleting or restoring an organization takes its files with it.

**Database per tenant.**
- `webimove_platform` — tenant registry, global email directory, OTP codes, refresh tokens,
  subscriptions, platform admins.
- `webimove_tenant_<tenantId>` — users, requests, documents, cases, tasks, notifications,
  messages, activities for exactly one organization.

The global email directory in the platform DB is what makes login possible: an email maps to
exactly one tenant, so the API knows which database to open. **A tenant database name is
never derived from user input** — only from the tenant document id.

**Roles.** One account, one role: `consultant_owner`, `consultant`, `partner`, `client`,
plus `super_admin` which lives in the platform DB only. Clients self-register by picking a
consultant organization; they land inside that organization's database.

---

## Quick start

```bash
cp .env.example .env          # then fill in MONGODB_URI, SMTP_*, OPENAI_API_KEY, JWT_SECRET_KEY
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

Tests (in-memory Mongo, no server needed):

```bash
pip install -r requirements-dev.txt
pytest
```

Docker:

```bash
docker compose up --build     # API on :8002, Mongo on :27017
```

Create the platform super admin:

```bash
python scripts_create_superadmin.py moob@yopmail.com '123456**'
```

Docs: <http://10.10.20.11:8002/docs> · Health: <http://10.10.20.11:8002/health>

---

## Project layout

```
app/
├── main.py                  FastAPI app, CORS, lifespan, health
├── api.py                   Router registry
├── core/
│   ├── config.py            Settings from .env
│   ├── enums.py             Roles, statuses, the ordered case pipeline
│   ├── security.py          Password hashing, access/refresh/onboarding JWTs
│   ├── deps.py              get_current_user, role guards, tenant DB resolver
│   ├── exceptions.py        Typed HTTP errors
│   └── utils.py             ObjectId helpers, serialization, REQ-/CAS- references
├── db/
│   ├── mongo.py             Client, platform_db(), tenant_db()
│   └── indexes.py           Index bootstrap + reference counters
├── services/
│   ├── email.py             SMTP (aiosmtplib) + branded templates
│   ├── otp.py               6-digit codes, 10-min expiry, resend cooldown, attempt cap
│   ├── openai_service.py    Document analysis, case guidance, assistant, checklist
│   ├── storage.py           GridFS upload/stream/delete inside the tenant database
│   ├── events.py            Notifications + activity feed
│   └── pagination.py
└── modules/
    auth · users · tenants · subscriptions · partners · requests ·
    documents · cases · tasks · ai · agenda · messages ·
    notifications · reporting · search · admin
```

Each module is `schemas.py` (Pydantic) + `service.py` (business logic) + `router.py` (HTTP).
Routers contain no business logic; services never import FastAPI request objects.

---

## The flows, as designed

### Consultant signup — 5 steps (`/auth/signup/*`)
1. **Personal** → returns a short-lived `onboarding_token`; carry it as `Authorization: Bearer` through the remaining steps.
2. **Organization** → creates the private tenant record and emails the OTP.
3. **Verify** → six-digit code, 10-minute expiry, 30-second resend cooldown, 5-attempt cap.
4. **Plan** → Starter $49/mo ($470/yr) · Professional $129/mo ($1,290/yr) · Enterprise $299/mo ($2,990/yr).
5. **Payment** → charges, then creates the tenant database, the owner account and the subscription.

Nothing is written to the tenant registry until payment succeeds. Until then the whole signup
lives in `platform.signups` and expires after 7 days.

### Partner invitation → account → under that consultant

1. Consultant calls `POST /partners` with name, email, mobile and role. The account is
   created with status `invited`, **no password**, and `consultant_id` set to the inviting
   consultant.
2. An email goes out with a **single-use link to the frontend** (`FRONTEND_URL` +
   `/invite/accept?token=...`), valid for `INVITE_EXPIRE_DAYS` (7 by default).
3. The accept screen calls `GET /auth/invite/{token}` to show the organization, the role
   and who invited them — before asking for anything.
4. `POST /auth/invite/accept` with `{token, password, confirm_password}` sets the password,
   activates the account, burns the token, and returns a signed-in token pair.
5. From then on the partner signs in with email + password and lands **under the consultant
   who invited them**. `/tasks`, `/earnings` and `/cases` are all scoped accordingly.

`POST /partners/{id}/resend` issues a fresh token and invalidates the previous link.
`DELETE /partners/{id}` revokes access and removes the directory entry. The same mechanism
serves team consultants (`POST /team`) and consultant-created clients (`POST /clients`).

### Client self-signup → choose a consultant

1. `GET /auth/consultants` — a public directory spanning every active organization. A client
   picks a **consultant**, not a firm; the organization follows from that choice.
2. `POST /auth/register/client` with `consultant_id`. The account is created inside that
   consultant's workspace with `consultant_id` set, status `pending_verification`, and an
   email OTP is sent.
3. `POST /auth/verify-email` with the code activates the account and signs them in.
   `POST /auth/verify-email/resend` issues a fresh code.

**An unverified account cannot sign in.** `POST /auth/login` rejects
`pending_verification` and tells the caller to request a new code. (This was a real hole:
before this change a self-registered client could sign in without ever verifying.)

`organization_id` is still accepted on `/auth/register/client` for backward compatibility —
without a `consultant_id` the workspace owner becomes the consultant.

### Request → documents → case
`POST /requests` (client) → `POST /requests/{id}/documents/request` (consultant decides the
checklist; `GET .../documents/suggest` gives an AI-proposed list) → client uploads via
`POST /documents/{id}/upload`, which runs OpenAI analysis and returns a confidence score →
consultant approves / rejects with feedback / comments → once **every** required document is
approved, `POST /requests/{id}/complete` opens the case (`CAS-xxx`).

### Case pipeline
Eleven ordered stages from `new_request` to `completed`. `POST /cases/{id}/advance` moves one
step (or jumps to a named stage), recomputes progress %, appends to the timeline and notifies
the client. `POST /cases/{id}/ai-guidance` produces OCR findings, missing documents, form
suggestions and risk flags, and auto-creates the client tasks.

### Delegation
`POST /tasks` assigns a partner task to a case with a due date. Partners see only their own
tasks and upload deliverables. Partners never see billing — `/subscription` is owner-only.

---

## Security notes

- Passwords: bcrypt via passlib. Minimum 8 chars with a number and a symbol.
- JWT: access (60 min) + refresh (30 days, stored server-side so it can be revoked).
- Every tenant-scoped route runs `require_active_tenant`, which returns **402** when the
  subscription is not paid.
- Clients are scoped to their own records at the query level, not just in the UI.
- Private consultant review notes are stripped from client responses.
- Seat limits are enforced from the plan on every partner and team invite.

## Who works under whom

Every tenant-scoped record carries `consultant_id`, and every list response resolves it into
a `consultant` object so the UI never needs a follow-up call:

```json
{
  "reference": "CAS-089",
  "client_name": "Elena Rodriguez",
  "consultant_id": "66b1f0...",
  "consultant": {
    "id": "66b1f0...",
    "full_name": "Sarah Jenkins",
    "title": "Senior Consultant",
    "avatar_url": null,
    "is_owner": true
  }
}
```

That expansion costs **one** extra query per page, not one per row — `paginate()` collects
the ids across the page and resolves them in a single lookup. It is skipped automatically on
the platform database, which has no tenant `users` collection.

**Clients have exactly one consultant** (`consultant_id`). **Partners have several**
(`consultant_ids`, an array). That asymmetry is deliberate: the design states "The partner
joins Jenkins Immigration Law only" — a partner belongs to the organization, and Nadia can
translate for Sarah's case this week and James's next week. A single owner field would make
the second consultant overwrite the first. `invited_by` records who brought the partner in;
`consultant_ids` grows via `$addToSet` each time work is delegated.

Filter any list by owner: `?consultant_id=...` works on `/requests`, `/cases`, `/tasks`,
`/users` and `/partners`. On `/users` it matches clients by `consultant_id` **or** partners
by `consultant_ids`.

Ownership endpoints:

| Endpoint | Use |
|---|---|
| `GET /consultants` | Every consultant with client / partner / case / task counts |
| `GET /consultants/me/roster` | My own clients, partners, requests and cases |
| `GET /consultants/{id}/roster` | The same for one consultant |
| `GET /consultants/{id}/clients` | Paginated clients |
| `GET /consultants/{id}/partners` | Partners this consultant has delegated to |
| `POST /consultants/clients/{id}/assign` | Move a client to another consultant |

Reassignment moves **open** work only. Completed requests, cases and tasks keep their
original owner — rewriting them would falsify history.

### Backfilling existing data

```bash
python scripts_backfill_consultant_id.py --dry-run   # see what would change
python scripts_backfill_consultant_id.py             # every active tenant
python scripts_backfill_consultant_id.py <tenant_id> # just one
```

Children inherit from their parent case or request, falling back to the workspace owner.
Partners get `consultant_ids` seeded from `invited_by` plus every consultant who has actually
assigned them a task. Safe to re-run — it only touches records where the field is missing.

## Logging

`LOG_LEVEL` controls **your application's** verbosity. Library verbosity is separate, because
tying them together buries your own output:

| Setting | Default | What it controls |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `app.*` loggers and uvicorn |
| `MONGO_LOG_LEVEL` | `WARNING` | pymongo / motor |
| `HTTP_LOG_LEVEL` | `WARNING` | httpx, openai, urllib3, aiosmtplib |
| `ACCESS_LOG` | `true` | uvicorn per-request access lines |

Set `MONGO_LOG_LEVEL=DEBUG` only while debugging the driver. PyMongo uses *streaming*
heartbeats: it holds an open request to each replica-set node for 10 seconds so a failover is
noticed immediately. On a 3-node Atlas cluster that is three log records every 10 seconds,
forever. `Server heartbeat succeeded` is healthy — it is not an error.

## Postman

`postman/` holds a generated collection (159 requests, 30 folders) and a local environment.
Import both, pick the environment, and sign in — tokens, OTP codes and created-record ids are
captured into collection variables automatically, so nothing needs copy-pasting.

Regenerate after changing routes:

```bash
python postman/generate_collection.py
```

See `postman/README.md` for the suggested first run.

## Super Admin & the Figma Make additions

A second design source — the Figma Make file — added the platform admin surface plus
several tenant-side areas the static design did not cover: helpdesk, consent and privacy
centre, security and sessions, partner earnings, client invoicing, legal policies and the
consolidated AI analysis report.

**Those endpoints were inferred from page names, not from the Make source** (the Figma MCP
serves the file listing but not file contents). Read `INFERRED.md` before building against
them — it grades every area by confidence and lists exactly what to verify.

## Who owns the price

The super admin sets plan prices in the platform dashboard
(`PUT /admin/billing/plans/{code}/price`), so `platform_settings` is the source of truth —
not Stripe. That needs care, because **a Stripe Price is immutable**: its `unit_amount`
can never be edited.

So a price change does not edit anything at Stripe. It creates a **new** Price under the
plan's Product, and new signups bill against it. Prices are cached by
`(plan, cycle, currency, amount)` in `platform.stripe_prices`, so setting a price back to
an earlier value reuses that Price instead of accumulating duplicates.

**Existing subscribers keep the Price they signed up on.** Nothing migrates them, by
design — silently raising someone's recurring charge is a legal problem in several
jurisdictions. Moving them is a deliberate, separate action.

`STRIPE_PRICES` in `.env` stays empty in normal operation. An entry there pins a
hand-made Price and overrides the dashboard for that one plan/cycle.

## Before production

1. **Move the frontend to Stripe.js.** Recurring billing is built
   (`app/services/stripe_service.py` + `POST /api/v1/webhooks/stripe`), and needs only
   `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` to run — Prices create themselves,
   see below. What is still missing is browser-side tokenisation:
   `POST /auth/signup/payment` takes a `payment_method_id`, and **only that path creates
   a real subscription**. The legacy raw-card fields still work, but that is a one-off
   charge nothing renews, and Stripe blocks raw card data unless the account is
   PCI-certified.
2. Watch GridFS growth. It is correct and transactional with the tenant data, but it puts
   file bytes on your Mongo bill and in every backup. If document volume climbs, move
   `app/services/storage.py` to S3/GCS with signed URLs — the interface is already
   `save_upload / read_bytes / stream_file / delete_file`, so it is a one-file swap.
3. Add PDF text extraction (the vision model currently reasons over image documents only).
4. Rotate `JWT_SECRET_KEY`, and keep `.env` out of git — it already is.
