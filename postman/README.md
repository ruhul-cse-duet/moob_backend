# Postman collection

Two files, both generated — do not hand-edit them:

- `WebImove_API.postman_collection.json` — 159 requests in 30 folders
- `WebImove_Local.postman_environment.json` — local environment

Regenerate after any route change:

```bash
python postman/generate_collection.py
```

The generator reads `app.openapi()`, so the collection cannot drift from the code. If you
edit the JSON by hand, the next regeneration overwrites you.

## Import

1. Postman → Import → drop both files in.
2. Select the **WebImove — Local** environment (top right).
3. Point `base_url` at your API. Default `http://10.10.20.11:8002`.

## Tokens handle themselves

Collection-level auth is `Bearer {{access_token}}`, and test scripts capture tokens from
responses into collection variables. You never copy-paste a token.

| After you run | What gets saved |
|---|---|
| `Sign in` | `access_token`, `refresh_token`, `tenant_id` (or `challenge_token` when 2FA is on) |
| Signup steps 1–5 | `onboarding_token`, rolling forward each step |
| Any OTP-sending call | `otp_code`, from the `debug_code` field |
| Create calls | `request_id`, `case_id`, `task_id`, `invoice_id`, `ticket_id`, … |

`debug_code` is only returned when `ENVIRONMENT != production` — it exists so you can test
the OTP flows without opening an inbox. It disappears in production, by design.

The five signup steps are the exception to collection auth: they send
`Bearer {{onboarding_token}}`, because a signup in progress has no account yet.

## Suggested first run

**Consultant workspace, end to end:**

1. `Auth & Onboarding → Step 1 of 5 · Personal`
2. `Step 2 of 5 · Organization` — sends the OTP, saves `otp_code`
3. `Step 3 of 5 · Verify email` — body already reads `{{otp_code}}`
4. `Step 4 of 5 · Choose a plan`
5. `Step 5 of 5 · Pay and activate` — card `4242424242424242` succeeds; any card ending
   `0000` is declined on purpose, so you can test the failure path
6. You are now signed in as the tenant owner. Everything else resolves.

**Super Admin** needs a platform account first:

```bash
python scripts_create_superadmin.py admin@webimove.com 'StrongPass1!'
```

Then `Auth & Onboarding → Sign in` with those credentials and open the **Super Admin**
folders. Platform staff do not belong to a tenant, so `tenant_id` stays empty for them.

## Running it headless

```bash
npm install newman
npx newman run postman/WebImove_API.postman_collection.json \
  -e postman/WebImove_Local.postman_environment.json \
  --folder "Health"
```

Drop `--folder` to run everything, but note the collection is ordered for humans, not as a
regression suite — many requests depend on ids created by earlier ones.

## Folder order

**Auth & Onboarding** first, **Super Admin ·** folders last, everything else alphabetical —
so the sidebar matches the order you actually need things.
