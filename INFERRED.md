# What is inferred, and what to check

The Figma Make file (`bgwK3EI6f9fbHVXfgfJbsX`) exposes its **file listing** through the
Figma MCP but not its **file contents** — `read_skill_uri` rejects non-`skill://` URIs and
nothing else can reach `file://figma/make/source/...`. The Chrome extension wasn't installed,
so the preview walkthrough wasn't available either.

So everything below was designed from **page names + the immigration domain + the static
Figma design file**, not from the Make source. It is structurally sound and runs, but the
field-level shapes are my best reading, not ground truth.

**The fastest way to close this gap:** drop `src/store/AppStore.tsx`, `AdminStore.tsx`,
`PlatformStore.tsx` and `TenantStore.tsx` into this folder. Those four files hold the real
entity shapes and would let me correct all of this in one pass.

---

## Confidence: HIGH — structure is near-certain, only field names may differ

| Make page | Endpoints | Notes |
|---|---|---|
| `AdminDashboard.tsx` | `GET /admin/dashboard`, `GET /admin/stats` | Counters, growth, plan mix, recent audit + tickets |
| `admin/Organizations.tsx` | `GET /admin/organizations` | Filter by status/plan, seats and active cases per row |
| `admin/OrganizationDetail.tsx` | `GET /admin/organizations/{id}` + status/plan overrides | Usage, 30-day activity, team, storage bytes |
| `admin/AdminUsers.tsx` | `GET/POST/PATCH/DELETE /admin/users` | Platform staff with `admin_role` |
| `admin/Billing.tsx` | `GET /admin/billing/*` | MRR, ARR, churn, plan mix, revenue by month |
| `admin/PlatformSettings.tsx` | `GET/PUT/DELETE /admin/settings` | Key–value with defaults merged |
| `support/HelpSupport.tsx` | `POST/GET /support/tickets` | Customer-facing ticketing |
| `admin/Helpdesk.tsx` | `GET /admin/helpdesk/*` | Same tickets, platform-wide queue |
| `Security.tsx` | `GET /security/*`, `POST /security/2fa/*` | Sessions, revoke, login history, 2FA |
| `legal/*.tsx`, `PlatformDocs.tsx` | `GET /legal/policies`, `POST .../accept` | Versioned, public, acceptance tracked |

## Confidence: MEDIUM — the shape is a real decision I made

### `partner/PartnerEarnings.tsx` → `/earnings`
Modelled as an **accrual ledger**: consultant accrues a fee against a completed task →
approves it → batches approved rows into a payout. Statuses `accrued / approved / paid /
cancelled`.

*Alternatives I rejected:* a percentage commission split off the client invoice, or a flat
per-task rate stored on the partner record. If your design does either of those, the
`amount` field moves and `POST /earnings` changes shape.

### `client/ClientBilling.tsx` → `/invoices`
Consultant→client invoicing, with line items, tax rate, and `draft → sent → paid/overdue/void`
Deliberately separate from `/subscription` (consultant→WebImove).

*Check:* whether clients pay through the platform (needs a payment intent endpoint) or
offline (current assumption — `mark-paid` is a manual consultant action).

### `client/ClientConsent.tsx` + `client/PrivacyCentre.tsx` → `/privacy`
Six consent types as auditable records with IP, user agent and full history — not booleans.
Data requests (export / deletion / rectification) carry a 30-day GDPR due date.

*Check:* the exact consent list, and whether deletion is self-service or consultant-approved
(current assumption: consultant-approved, because immigration files have legal retention
requirements that would conflict with instant deletion).

### `AnalysisReport.tsx` → `/analysis/case/{id}`, `/analysis/request/{id}`
Aggregates existing per-document `ai_analysis` into one report: average/lowest confidence,
issues by document, flagged-for-review, extracted fields, expiring documents.

*Check:* whether this is a generated PDF deliverable rather than a JSON view. If it's a PDF,
this becomes a render endpoint.

## Confidence: LOW — thresholds are placeholders

### `admin/Oversight.tsx` → `/admin/oversight/flags`
Four heuristics, all invented: 5+ failed logins in 24h, tenants active 14+ days with zero
clients, past-due subscriptions, GDPR requests older than 25 days. **Tune these against real
traffic** — right now they are guesses about what "oversight" means.

---

## Architectural decisions worth knowing

1. **Tickets live in the platform DB**, not tenant DBs. A super admin needs one queue across
   every organization; with database-per-tenant, tenant-local tickets would mean a fan-out
   query across every organization database on each helpdesk page load. Tickets carry
   `tenant_id`.

2. **2FA is email OTP, not TOTP.** The OTP service already has expiry, resend cooldown and
   attempt caps. Adding `pyotp` would mean a second parallel mechanism. `POST /auth/login`
   now returns `{two_factor_required: true, challenge_token}` when 2FA is on; complete with
   `POST /auth/login/2fa`.

3. **Sessions are refresh-token rows.** `/security/sessions` lists them with IP, user agent
   and device (from the `X-Device-Name` header); revoking deletes the row.

4. **The audit log is platform-side** (`app/services/audit.py`), so oversight spans tenants.
   Login, failed login, tenant creation, plan change and GDPR requests are wired in.

## Not built (no evidence of what they do)

`auth/Launcher.tsx`, `DesignUpdate.tsx`, and the `src/imports/*` PDFs. `Launcher` looks like
a role-picker shell with no backend need; `DesignUpdate` looks like internal documentation.
