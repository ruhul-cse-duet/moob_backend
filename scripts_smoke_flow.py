"""End-to-end smoke test of every endpoint the mobile app calls.

Walks one full journey — consultant signs up, client files a request, documents
are asked for, uploaded, approved, the consultation is closed, a partner is
invited and given work — and checks the response shapes the Flutter models
read. Any drift in the contract fails here rather than on a device.

    python scripts_smoke_flow.py                 # against localhost:8002
    python scripts_smoke_flow.py --base http://localhost:8002

Accounts are created under @yopmail.com with a random 6-digit suffix, so
scripts_cleanup_smoke_test.py can remove them afterwards.
"""
import argparse
import io
import random
import sys
from typing import Any, Dict, Optional

import requests
from pymongo import MongoClient

from app.core.config import settings

BASE = "http://127.0.0.1:8010"
API = "/api/v1"

_mongo = MongoClient(settings.MONGODB_URI)
_platform = _mongo[settings.PLATFORM_DB_NAME]

SUFFIX = f"{random.randint(100000, 999999)}"
PASSWORD = "SmokeTest!2026"

# A throwaway platform admin, created for this run and removed at the end, so
# the admin endpoints are genuinely exercised without needing a real password.
ADMIN_EMAIL = f"admin{SUFFIX}@yopmail.com"

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(f"{label}{f' — {detail}' if detail else ''}")
        print(f"  FAIL {label}{f' — {detail}' if detail else ''}")


def create_smoke_admin() -> None:
    from app.core.security import hash_password
    from app.core.utils import utcnow
    _platform.platform_admins.insert_one({
        "email": ADMIN_EMAIL,
        "full_name": "Smoke Admin",
        "password_hash": hash_password(PASSWORD),
        "status": "active",
        "admin_role": "super_admin",
        "created_at": utcnow(),
    })


def remove_smoke_admin() -> None:
    _platform.platform_admins.delete_one({"email": ADMIN_EMAIL})


def otp_for(email: str) -> str:
    """Reads the code straight out of the platform DB, as no inbox is watched."""
    row = _platform.otp_codes.find_one({"email": email.lower()})
    if not row:
        raise SystemExit(f"No OTP issued for {email}")
    return row["code"]


class Client:
    """One signed-in caller, with its own bearer token."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.token: Optional[str] = None

    def call(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        files: Any = None,
        expect: int = 200,
    ) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = requests.request(
            method, f"{BASE}{API}{path}",
            json=json, params=params, files=files, headers=headers, timeout=60,
        )
        if response.status_code != expect:
            raise SystemExit(
                f"[{self.name}] {method} {path} → {response.status_code} "
                f"(expected {expect})\n{response.text[:600]}"
            )
        return response.json() if response.content else {}

    get = lambda self, p, **kw: self.call("GET", p, **kw)  # noqa: E731
    post = lambda self, p, **kw: self.call("POST", p, **kw)  # noqa: E731
    patch = lambda self, p, **kw: self.call("PATCH", p, **kw)  # noqa: E731
    put = lambda self, p, **kw: self.call("PUT", p, **kw)  # noqa: E731


def sign_up_consultant() -> tuple[Client, str]:
    """Six steps, each carrying the onboarding token the previous one issued."""
    email = f"cons{SUFFIX}@yopmail.com"
    api = Client("consultant")
    print(f"\n1. Consultant signup ({email})")


    step = api.post("/auth/signup/personal", json={
        "full_name": "Daniel Reyes", "email": email,
        "mobile": "+351912345678",
    }, expect=201)
    api.token = step["onboarding_token"]
    check("step 1 hands back an onboarding token", bool(api.token))

    org = api.post("/auth/signup/organization", json={
        "organization_name": f"Smoke Consultancy {SUFFIX}",
        "business_type": "Immigration Consultancy",
        "country": "Portugal", "city": "Lisbon",
        "office_address": "Rua Augusta 1, Lisbon",
    })
    check("the organization is recorded", bool(org), str(org)[:160])

    verified = api.post("/auth/signup/verify", json={"code": otp_for(email)})
    if verified.get("onboarding_token"):
        api.token = verified["onboarding_token"]

    passworded = api.post("/auth/signup/password", json={
        "password": PASSWORD, "confirm_password": PASSWORD,
    })
    if passworded.get("onboarding_token"):
        api.token = passworded["onboarding_token"]

    api.post("/auth/signup/plan", json={
        "plan_code": "professional", "billing_cycle": "monthly",
    })
    # Stripe's shared test payment method — the same shape Stripe.js hands the
    # app in a browser, so this exercises the real subscription path.
    paid = api.post("/auth/signup/payment", json={
        "payment_method_id": "pm_card_visa",
        "name_on_card": "Daniel Reyes",
    })
    check("signup ends with a live session", "access_token" in paid, str(paid)[:200])
    api.token = paid["access_token"]
    return api, paid.get("tenant_id", "")


def sign_up_client(consultant_id: str) -> tuple[Client, str]:
    """Seven steps, on the same onboarding-token pattern."""
    email = f"cl{SUFFIX}@yopmail.com"
    print(f"\n2. Client signup ({email})")

    api = Client("client")

    step = api.post("/auth/client/signup/step1", json={
        "full_name": "Amara Okafor", "email": email,
        "mobile": "+4915112345678", "accept_terms": True,
    }, expect=201)
    api.token = step.get("client_onboarding_token") or step.get("onboarding_token")

    api.post("/auth/client/signup/verify", json={"code": otp_for(email)})
    api.post("/auth/client/signup/password", json={
        "password": PASSWORD, "confirm_password": PASSWORD,
    })
    api.post("/auth/client/signup/immigration", json={
        "nationality": "Nigerian", "destination_country": "Germany",
        "preferred_immigration_type": "Work Visa",
        "country_of_residence": "Nigeria",
    })
    api.post("/auth/client/signup/consultant", json={"consultant_id": consultant_id})
    api.post("/auth/client/signup/confirm", json={
        "gdpr_consent": True, "accept_terms_conditions": True,
    })
    done = api.post("/auth/client/signup/agreements", json={
        "terms_of_service": True, "privacy_policy": True,
        "data_processing": True, "ai_document_analysis": True,
    })
    pair = done.get("token_pair", done)
    check("client signup ends with a live session", "access_token" in pair,
          str(done)[:200])
    api.token = pair["access_token"]
    return api, email


def main() -> None:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    BASE = parser.parse_args().base.rstrip("/")

    create_smoke_admin()
    health = requests.get(f"{BASE}/health", timeout=10).json()
    if not health.get("database"):
        raise SystemExit("The backend is up but has no database connection.")

    consultant, tenant_id = sign_up_consultant()
    me = consultant.get("/me")
    consultant_id = me["id"]
    check("consultant profile carries an organization",
          bool(me.get("organization_info") or me.get("tenant_id")), str(me)[:200])

    # The workspace opens only once a platform admin approves it, so an admin
    # signs in and does that before the client can pick this consultant.
    admin = Client("admin")
    session = admin.post("/auth/platform/login", json={
        "email": ADMIN_EMAIL, "password": PASSWORD,
    })
    admin.token = session["access_token"]

    pending = admin.get("/admin/organizations", params={"tab": "approval"})
    mine = next((o for o in pending["items"] if o["id"] == tenant_id), None)
    check("a new workspace lands in the approval queue", bool(mine),
          str(pending["total"]))
    check("the API marks it approvable", bool(mine and mine.get("can_approve")),
          str(mine)[:160])
    admin.post(f"/admin/organizations/{tenant_id}/approve")

    client, client_email = sign_up_client(consultant_id)

    # ── The client files a request ───────────────────────────────────────────
    print("\n3. Client files a request")
    categories = client.get("/requests/client/categories")
    check("categories are listed", len(categories) > 0)

    request = client.post("/requests", json={
        "visa_type": "Work Visa", "destination_country": "Germany",
        "purpose": "Relocating for a software engineering role in Berlin.",
        "preferred_appointment": "This week, mornings",
    }, expect=201)
    request_id = request["id"]
    check("a submitted request is NEW, never 'draft'",
          request["status"] == "new", request["status"])
    check("is_draft is false on a submission", request.get("is_draft") is False)

    draft = client.post("/requests", json={
        "visa_type": "Student Visa", "destination_country": "Canada",
        "purpose": "Masters programme in Toronto.", "is_draft": True,
    }, expect=201)
    check("a draft is also NEW plus the flag",
          draft["status"] == "new" and draft.get("is_draft") is True,
          f"{draft['status']} / {draft.get('is_draft')}")
    # The old code wrote status "draft", which is outside the enum and made
    # this call 500.
    detail = client.get(f"/requests/{draft['id']}")
    check("a draft's detail loads", detail["id"] == draft["id"])

    # ── Attachments ──────────────────────────────────────────────────────────
    print("\n4. Request attachments")
    attachment = client.post(
        f"/requests/{request_id}/attachments",
        files={"file": ("notes.pdf", io.BytesIO(b"%PDF-1.4 smoke"), "application/pdf")},
        expect=201,
    )
    check("an attachment is stored in GridFS", bool(attachment.get("file_id")),
          str(attachment)[:200])
    back = client.get(f"/requests/{request_id}")
    check("the attachment comes back on the request",
          len(back.get("attached_files", [])) == 1)

    # ── The consultant asks for documents ────────────────────────────────────
    print("\n5. Consultant requests documents")
    queue = consultant.get("/requests", params={"status": "new"})
    check("the request reaches the consultant queue",
          any(item["id"] == request_id for item in queue["items"]))

    dashboard = consultant.get("/requests/consultant/dashboard")
    labels = {item["status_label"] for item in dashboard["open_requests"]}
    check("a new request is labelled 'New request', not 'Waiting for documents'",
          "New request" in labels or not labels, str(labels))

    consultant.post(f"/requests/{request_id}/documents/request", json={
        "documents": [
            {"name": "Passport", "category": "identity",
             "why": "A colour scan of the photo page.", "is_required": True},
            {"name": "Employment contract", "category": "employment",
             "why": "Signed by both parties.", "is_required": False},
        ],
        "message": "Two documents to get started.",
    })

    documents = client.get("/documents", params={"request_id": request_id})
    check("both documents reach the client", documents["total"] == 2)
    required = {d["name"]: d.get("is_required") for d in documents["items"]}
    check("is_required is persisted", required.get("Employment contract") is False,
          str(required))

    detail = client.get(f"/requests/{request_id}")
    due = [d.get("due_date") for d in detail["documents"]]
    check("no hardcoded 'Nov 15, 2026' due date",
          all(d != "Nov 15, 2026" for d in due), str(due))

    # ── The client uploads, the consultant decides ───────────────────────────
    print("\n6. Upload, reject, re-upload, approve")
    doc_ids = [d["id"] for d in documents["items"]]
    for doc_id in doc_ids:
        uploaded = client.post(
            f"/documents/{doc_id}/upload",
            files={"file": ("scan.pdf", io.BytesIO(b"%PDF-1.4 smoke scan"),
                            "application/pdf")},
        )
        check(f"upload lands on with_consultant ({uploaded['name']})",
              uploaded["status"] == "with_consultant", uploaded["status"])

    consultant.post(f"/documents/{doc_ids[0]}/reject",
                    json={"feedback": "The photo page is cropped."})
    sent_back = client.get(f"/documents/{doc_ids[0]}")
    check("a rejection lands on needs_reupload",
          sent_back["status"] == "needs_reupload", sent_back["status"])

    client.post(f"/documents/{doc_ids[0]}/upload",
                files={"file": ("scan2.pdf", io.BytesIO(b"%PDF-1.4 second try"),
                                "application/pdf")})
    for doc_id in doc_ids:
        approved = consultant.post(f"/documents/{doc_id}/approve")
        check(f"approval sticks ({approved['name']})",
              approved["status"] == "approved", approved["status"])

    # ── Client dashboard ─────────────────────────────────────────────────────
    print("\n7. Client dashboard")
    home = client.get("/requests/client/dashboard")
    check("progress is real, never the old 45% placeholder",
          home["active_hero_request"]["overall_progress"] in (0, 100),
          str(home["active_hero_request"]))
    check("the next step is not the old 'Upload 5' placeholder",
          "Upload 5 " not in home["action_next_step"]["title"],
          home["action_next_step"]["title"])
    check("activities come from the `activities` collection",
          isinstance(home.get("recent_activities"), list) and
          all("actor_name" in a or "action" in a for a in home["recent_activities"]),
          str(home.get("recent_activities"))[:200])
    check("the pending-document count is sent",
          "pending_documents_count" in home)

    # ── Closing the consultation ─────────────────────────────────────────────
    print("\n8. Completing the consultation")
    completed = consultant.post(f"/requests/{request_id}/complete", json={
        "case_type": "Work Visa",
        "outcome": {
            "summary": "Eligible for the EU Blue Card via the job offer.",
            "guidance": "File at the German consulate in Lagos.",
            "next_steps": ["Book a consulate appointment", "Pay the visa fee"],
            "timeline": "Roughly 8–12 weeks from filing.",
            "recommendations": "Keep the contract valid past the decision date.",
        },
    }, expect=201)
    check("completing opens a case", bool(completed.get("case_id")),
          str(completed)[:200])

    closed = client.get(f"/requests/{request_id}")
    outcome = closed.get("outcome") or {}
    check("the client can read the outcome summary",
          outcome.get("summary", "").startswith("Eligible"), str(outcome)[:200])
    check("the next steps survive the round trip",
          len(outcome.get("next_steps", [])) == 2, str(outcome.get("next_steps")))

    # ── Partners ─────────────────────────────────────────────────────────────
    print("\n9. Partner invitation and delegated work")
    partner_email = f"p{SUFFIX}@yopmail.com"
    invited = consultant.post("/partners", json={
        "full_name": "Sofia Marques", "email": partner_email,
        "mobile": "+351911111111", "role": "Certified translator",
    }, expect=201)
    check("the invite token is handed back for copying",
          bool(invited.get("invite_token")), str(invited)[:200])

    listed = consultant.get("/partners")
    pending = next((p for p in listed["items"] if p["email"] == partner_email), None)
    check("a pending partner still carries its invite code",
          bool(pending and pending.get("invite_token")), str(pending)[:200])

    partner = Client("partner")
    accepted = partner.post("/auth/invite/accept", json={
        "token": invited["invite_token"],
        "password": PASSWORD, "confirm_password": PASSWORD,
    })
    check("the partner can sign in with the code", "access_token" in accepted,
          str(accepted)[:200])
    partner.token = accepted["access_token"]

    task = consultant.post("/tasks", json={
        "title": "Certified translation of the employment contract",
        "description": "Portuguese to English, stamped.",
        "case_id": completed["case_id"],
        "assignee_id": pending["id"], "assignee_type": "partner",
    }, expect=201)

    partner_home = partner.get("/tasks/partner/dashboard")
    check("the partner dashboard names them",
          bool(partner_home.get("partner_name")), str(partner_home)[:200])
    check("the summary carries an overdue count",
          "overdue" in partner_home["summary"], str(partner_home["summary"]))

    partner.post(f"/tasks/{task['id']}/status", json={"status": "in_progress"})
    delivered = partner.post(
        f"/tasks/{task['id']}/deliverables",
        files={"file": ("translation.pdf", io.BytesIO(b"%PDF-1.4 translation"),
                        "application/pdf")},
    )
    check("a deliverable attaches and submits the task",
          delivered["status"] == "submitted" and len(delivered["deliverables"]) == 1,
          delivered["status"])
    done = partner.post(f"/tasks/{task['id']}/complete",
                        json={"delivery_notes": "Stamped and sealed."})
    check("the task closes", done["status"] == "completed", done["status"])

    # ── The assistant, for every role ────────────────────────────────────────
    print("\n10. The assistant answers every role")
    for caller in (consultant, client, partner):
        reply = caller.post("/ai/chat", json={
            "message": "What happens next with my case?",
        })
        check(f"{caller.name} gets an assistant reply",
              bool(reply.get("reply")) and bool(reply.get("conversation_id")),
              str(reply)[:120])

    threads = client.get("/ai/conversations")
    check("a client only sees their own threads", threads["total"] == 1,
          str(threads["total"]))

    # ── Privacy ──────────────────────────────────────────────────────────────
    print("\n11. Consent and privacy")
    consents = client.get("/privacy/consents")
    check("consents are listed", len(consents.get("consents", [])) > 0)
    client.put("/privacy/consents", json={"type": "marketing_emails", "granted": True})
    history = client.get("/privacy/consents/history")
    check("a consent change is recorded", history["total"] > 0)
    export = client.get("/privacy/export")
    check("the client can export their own data", bool(export), str(export)[:120])

    # ── Reporting ────────────────────────────────────────────────────────────
    print("\n12. Reporting")
    report = consultant.get("/reporting/summary", params={"period": "90d"})
    rate = report["approval_rate"]["value"]
    # One document was sent back and one was clean, so a 100% rate would mean
    # the sent-back document was not being counted.
    check("the approval rate counts sent-back documents",
          rate is None or rate < 100, str(rate))

    # ── The rights a client can exercise, and the desk that answers them ─────
    print("\n13. Data requests, end to end")
    raised = client.post("/privacy/requests",
                         json={"type": "export", "reason": "Moving advisers"},
                         expect=201)
    check("a client can raise a data request", raised["status"] == "pending",
          str(raised)[:160])
    check("the statutory deadline is stamped on it", bool(raised.get("due_at")),
          str(raised.get("due_at")))

    queue = consultant.get("/privacy/admin/requests")
    mine = next((r for r in queue["items"] if r["id"] == raised["id"]), None)
    check("it reaches the organization's queue", mine is not None)

    decided = consultant.post(f"/privacy/admin/requests/{raised['id']}/status",
                              json={"status": "completed",
                                    "note": "Sent by secure link."})
    check("the decision is a body, not a query string",
          decided["status"] == "completed", str(decided)[:160])
    check("the note reaches the record", decided.get("note") == "Sent by secure link.",
          str(decided.get("note")))
    check("who handled it is recorded", bool(decided.get("handled_by_name")),
          str(decided.get("handled_by_name")))

    mine_now = client.get("/privacy/requests")
    closed_one = next((r for r in mine_now["items"] if r["id"] == raised["id"]), None)
    check("the client sees it closed",
          (closed_one or {}).get("status") == "completed",
          str(closed_one)[:160])

    # ── Platform staff: policies, ticket routing, tenant oversight ───────────
    print("\n14. Platform administration")
    version = f"1.{random.randint(10, 99)}"
    written = admin.post("/legal/admin/policies", json={
        "kind": "privacy_policy", "version": version,
        "title": "Privacy Policy", "body_markdown": "## Scope\n\nSmoke test.",
        "publish": False,
    }, expect=201)
    check("a policy version can be written", written["version"] == version,
          str(written)[:160])
    check("an unpublished version stays unpublished",
          written.get("published") is False, str(written.get("published")))

    published = admin.post(f"/legal/admin/policies/{written['id']}/publish")
    check("publishing makes it live", published.get("published") is True,
          str(published.get("published")))
    live = client.get("/legal/policies/privacy_policy")
    check("the public endpoint serves the new version",
          live["version"] == version, str(live.get("version")))

    versions = admin.get("/legal/admin/policies", params={"kind": "privacy_policy"})
    check("every version stays readable", versions["total"] >= 1,
          str(versions["total"]))

    ticket = client.post("/support/tickets", json={
        "subject": "Cannot open my documents", "message": "The list is empty.",
        "category": "technical", "priority": "high",
    }, expect=201)
    staff = admin.get("/admin/platform-staff")
    me_row = next((a for a in staff["items"] if a["email"] == ADMIN_EMAIL), None)
    check("platform staff are listed for assignment", me_row is not None)

    assigned = admin.post(f"/admin/helpdesk/tickets/{ticket['id']}/assign",
                          json={"agent_id": me_row["id"]})
    check("a ticket can be handed to a named agent",
          assigned.get("assigned_to_name") == "Smoke Admin",
          str(assigned.get("assigned_to_name")))
    check("assigning moves it to in_progress",
          assigned["status"] == "in_progress", assigned["status"])

    unassigned = admin.post(f"/admin/helpdesk/tickets/{ticket['id']}/assign",
                            json={"agent_id": None})
    check("it can go back to the unassigned pile",
          unassigned.get("assigned_to_name") is None,
          str(unassigned.get("assigned_to_name")))

    inside = admin.get(f"/admin/oversight/tenant/{tenant_id}/activity")
    check("an admin can read one organization's activity",
          len(inside["items"]) > 0, str(len(inside["items"])))

    # ── One sign-in box for every role ───────────────────────────────────────
    print("\n15. Role-less sign-in")
    for who, email in [("consultant", f"cons{SUFFIX}@yopmail.com"),
                       ("client", f"cl{SUFFIX}@yopmail.com"),
                       ("partner", partner_email)]:
        anon = Client(who)
        session = anon.post("/auth/login", json={"email": email,
                                                 "password": PASSWORD})
        check(f"a {who} signs in without naming a role",
              bool(session.get("access_token")), str(session)[:140])
        check(f"the response says which workspace to open ({who})",
              session.get("user", {}).get("role") is not None,
              str(session.get("user", {}).get("role")))

    admin_anon = Client("admin")
    session = admin_anon.post("/auth/login", json={"email": ADMIN_EMAIL,
                                                   "password": PASSWORD})
    check("a platform admin is handed the platform session by the same box",
          session.get("admin_role") == "super_admin", str(session)[:160])

    # A caller that still names a portal is still held to it.
    wrong = Client("client")
    wrong.post("/auth/login",
               json={"email": f"cl{SUFFIX}@yopmail.com", "password": PASSWORD,
                     "role": "consultant"},
               expect=401)
    check("naming the wrong role is still refused", True)

    print(f"\n{'=' * 62}")
    print(f"{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  · {failure}")
        sys.exit(1)
    print("Every endpoint the app calls answers in the shape it expects.")


if __name__ == "__main__":
    try:
        main()
    finally:
        remove_smoke_admin()
