"""End-to-end check of the deployed WebImove platform.

Drives the live API the way the three apps do - consultant, partner and client -
and asserts the rules the client's feedback is about: who may see what, whether a
case inherits its procedure, where personal data is asked for, and which
currency and language the screens speak.

Credentials are read from the environment, never from this file:

    MOOB_URL        API base, default https://clientes.webimove.com/api/v1
    MOOB_CONSULTANT email:password
    MOOB_PARTNER    email:password
    MOOB_CLIENT     email:password

Read-only by default. `--write` additionally exercises the flows that create
data (request -> case -> document -> task) and prints every id it created so the
rows can be removed afterwards.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = os.environ.get("MOOB_URL", "https://clientes.webimove.com/api/v1").rstrip("/")
TIMEOUT = 45

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
results: List[Tuple[str, str, str, str]] = []  # (area, status, name, detail)
created: List[str] = []


def record(area: str, status: str, name: str, detail: str = "") -> None:
    results.append((area, status, name, detail))
    mark = {PASS: "[ok]", FAIL: "[FAIL]", WARN: "[warn]", SKIP: "[skip]"}[status]
    line = f"{mark:7} {area:<12} {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)


def check(area: str, name: str, condition: bool, detail: str = "", soft: bool = False) -> bool:
    record(area, PASS if condition else (WARN if soft else FAIL), name, "" if condition else detail)
    return condition


class Session:
    """One signed-in actor."""

    def __init__(self, role: str, token: str, user: Dict[str, Any]):
        self.role = role
        self.token = token
        self.user = user or {}

    @property
    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def call(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", TIMEOUT)
        headers = dict(kw.pop("headers", {}))
        headers.update(self.headers)
        return requests.request(method, f"{BASE}{path}", headers=headers, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def patch(self, path, **kw):
        return self.call("PATCH", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)


def split_cred(raw: Optional[str]) -> Optional[Tuple[str, str]]:
    if not raw or ":" not in raw:
        return None
    email, password = raw.split(":", 1)
    return email.strip(), password


def login(role: str, cred: Tuple[str, str], portal_role: Optional[str] = None) -> Optional[Session]:
    email, password = cred
    body: Dict[str, Any] = {"email": email, "password": password}
    if portal_role:
        body["role"] = portal_role
    try:
        r = requests.post(f"{BASE}/auth/login", json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        record("auth", FAIL, f"{role} sign-in", f"network error: {exc}")
        return None
    if r.status_code != 200:
        record("auth", FAIL, f"{role} sign-in", f"HTTP {r.status_code} {r.text[:200]}")
        return None
    data = r.json()
    if data.get("two_factor_required"):
        record("auth", SKIP, f"{role} sign-in", "two-factor challenge - cannot continue unattended")
        return None
    token = data.get("access_token")
    if not token:
        record("auth", FAIL, f"{role} sign-in", "no access_token in response")
        return None
    got = data.get("role")
    check("auth", f"{role} sign-in returns role={role}", got == role, f"got {got!r}")
    return Session(role, token, data.get("user") or {})


def body_of(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {}


def items_of(r: requests.Response) -> List[Dict[str, Any]]:
    data = body_of(r)
    if isinstance(data, list):
        return data
    for key in ("items", "results", "data", "invoices", "cases", "requests", "tasks", "documents"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return value
    return []


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def test_auth(creds: Dict[str, Tuple[str, str]]) -> None:
    email, _ = creds["client"]
    r = requests.post(
        f"{BASE}/auth/login",
        json={"email": email, "password": "definitely-not-the-password-9f3a"},
        timeout=TIMEOUT,
    )
    check("auth", "wrong password is refused", r.status_code in (400, 401, 403),
          f"HTTP {r.status_code}")

    r = requests.post(f"{BASE}/auth/login", json={"email": email, "password": creds["client"][1],
                                                  "role": "consultant"}, timeout=TIMEOUT)
    check("auth", "client cannot sign in through the consultant door",
          r.status_code in (400, 401, 403), f"HTTP {r.status_code}")

    r = requests.get(f"{BASE}/cases", timeout=TIMEOUT)
    check("auth", "no token is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")


# --------------------------------------------------------------------------
# consultant
# --------------------------------------------------------------------------
def test_consultant(s: Session) -> Dict[str, Any]:
    facts: Dict[str, Any] = {}

    r = s.get("/requests/consultant/dashboard")
    check("consultant", "home dashboard loads", r.status_code == 200, f"HTTP {r.status_code}")

    r = s.get("/catalog/procedures")
    procedures = items_of(r) if r.status_code == 200 else []
    check("consultant", "procedure catalogue loads", r.status_code == 200, f"HTTP {r.status_code}")
    check("consultant", "catalogue is not empty", bool(procedures),
          "no procedures - a case cannot inherit anything")

    with_docs = [p for p in procedures if p.get("required_documents")]
    with_fields = [p for p in procedures if p.get("client_fields")]
    check("consultant", "procedures name the documents they need", bool(with_docs),
          f"{len(with_docs)}/{len(procedures)} have required_documents")
    check("consultant", "procedures name the fields the client fills", bool(with_fields),
          f"{len(with_fields)}/{len(procedures)} have client_fields", soft=True)
    facts["procedure"] = (with_docs or procedures or [None])[0]

    for path, label in [
        ("/requests", "request queue"),
        ("/requests/counts", "request badge counts"),
        ("/cases", "case list"),
        ("/cases/stage-counts", "case stage counts"),
        ("/documents", "document centre"),
        ("/tasks", "delegated work"),
        ("/invoices", "invoices"),
        ("/invoices/summary", "invoice totals"),
        ("/partners", "partner list"),
    ]:
        r = s.get(path)
        check("consultant", f"{label} loads", r.status_code == 200, f"{path} -> HTTP {r.status_code}")

    r = s.get("/cases")
    cases = items_of(r)
    facts["cases"] = cases
    if cases:
        case_id = cases[0].get("id") or cases[0].get("_id")
        facts["case_id"] = case_id
        detail = s.get(f"/cases/{case_id}")
        check("consultant", "case detail loads", detail.status_code == 200,
              f"HTTP {detail.status_code}")
        if detail.status_code == 200:
            body = body_of(detail)
            item = body.get("case", body) if isinstance(body, dict) else {}
            check("consultant", "case carries its procedure", bool(item.get("procedure_id")),
                  "case has no procedure_id (N1)", soft=True)
            check("consultant", "case carries a document checklist",
                  bool(item.get("required_documents")),
                  "case has no required_documents (N1)", soft=True)
            check("consultant", "case carries a deadline", item.get("deadline") is not None,
                  "case has no deadline (N1)", soft=True)
            check("consultant", "case carries workflow stages", bool(item.get("workflow_stages")),
                  "case has no workflow_stages (N1)", soft=True)
        t = s.get(f"/cases/{case_id}/timeline")
        check("consultant", "case timeline loads", t.status_code == 200, f"HTTP {t.status_code}")
        h = s.get(f"/cases/{case_id}/history")
        check("consultant", "case history loads", h.status_code == 200, f"HTTP {h.status_code}")
    else:
        record("consultant", SKIP, "case detail checks", "no cases on this account")
    return facts


# --------------------------------------------------------------------------
# partner
# --------------------------------------------------------------------------
def test_partner(s: Session) -> None:
    r = s.get("/tasks/partner/dashboard")
    check("partner", "home dashboard loads", r.status_code == 200, f"HTTP {r.status_code}")

    r = s.get("/tasks")
    tasks = items_of(r)
    check("partner", "task list loads", r.status_code == 200, f"HTTP {r.status_code}")

    r = s.get("/earnings/summary")
    if r.status_code == 200:
        currency = (body_of(r) or {}).get("currency")
        check("partner", "earnings are in euros", currency in (None, "EUR"),
              f"currency={currency!r}")
    else:
        record("partner", WARN, "earnings summary", f"HTTP {r.status_code}")

    # The rule the whole engagement turns on: everything reaches the partner
    # through the consultant, so the consultant's own areas must be shut.
    for path, label in [
        ("/cases", "case list"),
        ("/requests", "request queue"),
        ("/invoices", "invoices"),
        ("/partners", "partner directory"),
        ("/users/consultant/profile-overview", "consultant profile"),
    ]:
        r = s.get(path)
        check("partner", f"consultant's {label} is closed to the partner",
              r.status_code in (401, 403, 404), f"{path} -> HTTP {r.status_code}")

    # A partner must never hold a client's contact details.
    leaked: List[str] = []
    for task in tasks[:10]:
        blob = str(task).lower()
        for needle in ("@", "client_email", "client_phone", "phone"):
            if needle in blob:
                leaked.append(f"{task.get('reference') or task.get('id')}:{needle}")
                break
    check("partner", "tasks carry no client contact details", not leaked,
          f"possible contact data in {leaked[:3]}", soft=True)

    if tasks:
        task_id = tasks[0].get("id") or tasks[0].get("_id")
        r = s.get(f"/tasks/{task_id}")
        check("partner", "task detail loads", r.status_code == 200, f"HTTP {r.status_code}")
    else:
        record("partner", SKIP, "task detail", "no tasks assigned")


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
def test_client(s: Session, consultant_facts: Dict[str, Any]) -> None:
    r = s.get("/requests/client/dashboard")
    check("client", "home dashboard loads", r.status_code == 200, f"HTTP {r.status_code}")

    r = s.get("/requests/client/categories")
    categories = items_of(r)
    check("client", "request categories load", r.status_code == 200, f"HTTP {r.status_code}")

    # Error 3: the client picks a broad category, never the procedure itself.
    blob = str(categories).lower()
    check("client", "client is not asked to pick the procedure",
          "procedure_id" not in blob,
          "categories expose procedure_id - the client is choosing the procedure (error 3)",
          soft=True)

    for path, label in [
        ("/requests", "own requests"),
        ("/cases", "own cases"),
        ("/documents", "own documents"),
        ("/invoices", "own invoices"),
        ("/users/client/profile-overview", "profile"),
        ("/users/client/gdpr-consent", "consent record"),
        ("/users/client/privacy-policy", "privacy policy"),
        ("/users/client/settings", "settings"),
    ]:
        r = s.get(path)
        check("client", f"{label} loads", r.status_code == 200, f"{path} -> HTTP {r.status_code}")

    # N3: a client who has paid for nothing must not be shown a bill.
    r = s.get("/invoices")
    invoices = items_of(r)
    own = s.user.get("id") or s.user.get("_id")
    foreign = [i for i in invoices if own and i.get("client_id") not in (None, own)]
    check("client", "client sees only their own invoices", not foreign,
          f"{len(foreign)} invoice(s) belong to someone else")
    demo = [i for i in invoices if str(i.get("amount")) in ("1200", "1200.0", "1200.00")]
    check("client", "no demonstration invoices", not demo,
          f"{len(demo)} invoice(s) of 1200 - looks like seeded demo data (N3)", soft=True)
    non_eur = {i.get("currency") for i in invoices} - {None, "EUR"}
    check("client", "invoices are in euros", not non_eur, f"currencies: {non_eur}")

    # N2 / error 7: the case is where the client's personal data is asked for.
    r = s.get("/cases")
    cases = items_of(r)
    if cases:
        case_id = cases[0].get("id") or cases[0].get("_id")
        detail = s.get(f"/cases/{case_id}")
        check("client", "client can open their own case", detail.status_code == 200,
              f"HTTP {detail.status_code}")
        if detail.status_code == 200:
            body = body_of(detail)
            item = body.get("case", body) if isinstance(body, dict) else {}
            check("client", "case asks the client for their personal data",
                  bool(item.get("client_fields")),
                  "case has no client_fields - nowhere to fill personal data (N2/error 7)",
                  soft=True)
            check("client", "case shows its document checklist",
                  item.get("required_documents") is not None,
                  "case exposes no required_documents to the client", soft=True)
    else:
        record("client", SKIP, "case visibility", "this client has no case yet")

    # A client must not reach a consultant's or another client's data.
    for path, label in [
        ("/catalog/procedures", "the procedure catalogue"),
        ("/partners", "the partner directory"),
        ("/tasks", "delegated tasks"),
        ("/requests/consultant/dashboard", "the consultant dashboard"),
    ]:
        r = s.get(path)
        check("client", f"{label} is closed to the client",
              r.status_code in (401, 403, 404), f"{path} -> HTTP {r.status_code}")

    other = consultant_facts.get("case_id")
    if other:
        mine = {c.get("id") or c.get("_id") for c in cases}
        if other not in mine:
            r = s.get(f"/cases/{other}")
            check("client", "another client's case is closed",
                  r.status_code in (401, 403, 404), f"HTTP {r.status_code}")

    # The client must not be able to open a case - only a consultant may.
    r = s.post("/cases", json={"client_id": own, "case_type": "e2e probe"})
    check("client", "client cannot open a case themselves",
          r.status_code in (401, 403, 404, 422), f"HTTP {r.status_code}")


# --------------------------------------------------------------------------
# language
# --------------------------------------------------------------------------
def test_language(s: Session) -> None:
    r = s.get("/me/language")
    if r.status_code != 200:
        record("language", WARN, "language endpoint", f"HTTP {r.status_code}")
        return
    data = body_of(r)
    original = data.get("language") or data.get("current") or "en"
    options = data.get("options") or data.get("available") or []
    codes = {o.get("code") if isinstance(o, dict) else o for o in options}
    check("language", "the three workspace languages are offered",
          {"en", "pt", "es"} <= codes or not codes, f"offered: {sorted(c for c in codes if c)}",
          soft=True)

    for code in ("es", "pt", "en"):
        w = s.put("/me/language", json={"language": code})
        if w.status_code not in (200, 204):
            check("language", f"can switch to {code}", False, f"HTTP {w.status_code}")
            continue
        back = body_of(s.get("/me/language"))
        got = back.get("language") or back.get("current")
        check("language", f"{code} is saved and read back", got == code, f"got {got!r}")

    s.put("/me/language", json={"language": original})


# --------------------------------------------------------------------------
# write flow
# --------------------------------------------------------------------------
def test_write_flow(consultant: Session, client: Session, facts: Dict[str, Any]) -> None:
    """Client raises a request -> consultant approves and opens a case -> the
    case must arrive carrying the procedure's checklist, fields and deadline."""
    r = client.post("/requests", json={
        "category": "work",
        "description": "Automated end-to-end probe - safe to delete",
        "destination_country": "Portugal",
    })
    if r.status_code not in (200, 201):
        record("flow", FAIL, "client raises a request", f"HTTP {r.status_code} {r.text[:200]}")
        return
    request = body_of(r)
    request_id = request.get("id") or request.get("_id") or (request.get("request") or {}).get("id")
    created.append(f"request {request_id}")
    record("flow", PASS, "client raises a request", f"id={request_id}")

    r = consultant.post(f"/requests/{request_id}/approve")
    check("flow", "consultant takes the request into the queue",
          r.status_code in (200, 201), f"HTTP {r.status_code} {r.text[:160]}")

    procedure = facts.get("procedure") or {}
    procedure_id = procedure.get("id") or procedure.get("_id") or procedure.get("procedure_id")
    payload: Dict[str, Any] = {"case_type": "E2E probe"}
    if procedure_id:
        payload["procedure_id"] = procedure_id
    r = consultant.post(f"/requests/{request_id}/open-case", json=payload)
    if r.status_code not in (200, 201):
        record("flow", FAIL, "consultant opens the case", f"HTTP {r.status_code} {r.text[:200]}")
        return
    case = body_of(r)
    case = case.get("case", case) if isinstance(case, dict) else {}
    case_id = case.get("id") or case.get("_id")
    created.append(f"case {case_id} ({case.get('reference')})")
    record("flow", PASS, "consultant opens the case", f"{case.get('reference')} id={case_id}")

    if procedure_id:
        check("flow", "the case inherits its procedure",
              case.get("procedure_id") == procedure_id,
              f"case procedure_id={case.get('procedure_id')!r} (N1)")
        check("flow", "the case inherits the document checklist",
              len(case.get("required_documents") or []) == len(procedure.get("required_documents") or []),
              f"case has {len(case.get('required_documents') or [])}, "
              f"procedure names {len(procedure.get('required_documents') or [])} (N1)")
        check("flow", "the case inherits the client's fields",
              len(case.get("client_fields") or []) == len(procedure.get("client_fields") or []),
              f"case has {len(case.get('client_fields') or [])} (N2)", soft=True)
    check("flow", "the case opens with workflow stages", bool(case.get("workflow_stages")),
          "no workflow_stages (N1)")

    # The checklist must have become real document requests the client can see.
    docs = items_of(client.get("/documents"))
    linked = [d for d in docs if d.get("case_id") == case_id]
    check("flow", "the client is asked for the case's documents", bool(linked),
          f"{len(linked)} document request(s) reached the client (N1)", soft=True)

    # The client must see the case, and be able to fill a field on it.
    client_cases = items_of(client.get("/cases"))
    check("flow", "the client sees the new case",
          any((c.get("id") or c.get("_id")) == case_id for c in client_cases),
          "the case the consultant opened is not in the client's list (N2)")

    fields = case.get("client_fields") or []
    if fields:
        key = fields[0].get("key")
        r = client.patch(f"/cases/{case_id}/form/{key}", json={"value": "E2E probe value"})
        check("flow", "the client can fill a personal-data field",
              r.status_code in (200, 204), f"HTTP {r.status_code} {r.text[:160]}")
    else:
        record("flow", SKIP, "client fills a personal-data field", "no client_fields on the case")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="also run the flows that create data on the live system")
    args = ap.parse_args()

    creds = {}
    for role, env in [("consultant", "MOOB_CONSULTANT"), ("partner", "MOOB_PARTNER"),
                      ("client", "MOOB_CLIENT")]:
        cred = split_cred(os.environ.get(env))
        if not cred:
            print(f"missing {env} - expected 'email:password'", file=sys.stderr)
            return 2
        creds[role] = cred

    print(f"\nWebImove end-to-end check  ->  {BASE}")
    print(f"mode: {'read + write' if args.write else 'read-only'}\n")
    started = time.time()

    test_auth(creds)
    consultant = login("consultant", creds["consultant"])
    partner = login("partner", creds["partner"])
    client = login("client", creds["client"])

    facts: Dict[str, Any] = {}
    if consultant:
        facts = test_consultant(consultant)
        test_language(consultant)
    if partner:
        test_partner(partner)
    if client:
        test_client(client, facts)
    if args.write and consultant and client:
        test_write_flow(consultant, client, facts)
    elif args.write:
        record("flow", SKIP, "write flow", "consultant or client sign-in failed")

    print("\n" + "=" * 72)
    counts = {s: sum(1 for _, st, _, _ in results if st == s) for s in (PASS, FAIL, WARN, SKIP)}
    print(f"{counts[PASS]} passed   {counts[FAIL]} failed   "
          f"{counts[WARN]} warnings   {counts[SKIP]} skipped   "
          f"({time.time() - started:.1f}s)")

    for label, status in (("FAILURES", FAIL), ("WARNINGS", WARN)):
        rows = [r for r in results if r[1] == status]
        if rows:
            print(f"\n{label}")
            for area, _, name, detail in rows:
                print(f"  - [{area}] {name}" + (f"  -- {detail}" if detail else ""))

    if created:
        print("\nTEST DATA CREATED (safe to delete)")
        for item in created:
            print(f"  - {item}")

    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
