"""
Generate a Postman v2.1 collection + environment from the live OpenAPI schema.

Run this again whenever routes change:
    python postman/generate_collection.py

Generating from app.openapi() rather than hand-writing the collection means the
collection cannot drift from the code.
"""
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent
COLLECTION = OUT_DIR / "WebImove_API.postman_collection.json"
ENVIRONMENT = OUT_DIR / "WebImove_Local.postman_environment.json"

# Path params that should read from a collection variable rather than be typed each time.
PATH_VARS = {
    "request_id", "case_id", "document_id", "task_id", "user_id", "partner_id",
    "admin_id", "tenant_id", "ticket_id", "invoice_id", "earning_id", "policy_id",
    "conversation_id", "notification_id", "thread_id", "appointment_id", "session_id",
    "file_id", "plan_code", "kind", "key", "consultant_id", "client_id",
}

# Endpoints whose response should populate collection variables automatically.
CAPTURE: Dict[str, List[tuple]] = {
    "POST /api/v1/auth/login": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
        ("tenant_id", "tenant_id"), ("challenge_token", "challenge_token"),
        # A signed-in consultant IS the consultant_id used by every ownership filter.
        ("consultant_id", "user.id"), ("user_id", "user.id"),
    ],
    "POST /api/v1/auth/login/2fa": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
        ("tenant_id", "tenant_id"), ("consultant_id", "user.id"),
    ],
    "POST /api/v1/auth/refresh": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
    ],
    "POST /api/v1/auth/verify-email": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
    ],
    "POST /api/v1/auth/invite/accept": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
    ],
    "POST /api/v1/auth/signup/personal": [("onboarding_token", "onboarding_token")],
    "POST /api/v1/auth/signup/organization": [
        ("onboarding_token", "onboarding_token"), ("otp_code", "debug_code"),
    ],
    "POST /api/v1/auth/signup/verify": [("onboarding_token", "onboarding_token")],
    "POST /api/v1/auth/signup/verify/resend": [("otp_code", "debug_code")],
    "POST /api/v1/auth/signup/plan": [("onboarding_token", "onboarding_token")],
    "POST /api/v1/auth/signup/payment": [
        ("access_token", "access_token"), ("refresh_token", "refresh_token"),
        ("tenant_id", "tenant_id"),
    ],
    "POST /api/v1/clients": [("client_id", "id"), ("user_id", "id")],
    "POST /api/v1/auth/register/client": [
        ("otp_code", "debug_code"), ("user_id", "user_id"),
        ("tenant_id", "tenant_id"), ("consultant_id", "consultant.id"),
    ],
    "POST /api/v1/auth/forgot-password": [("otp_code", "debug_code")],
    "POST /api/v1/requests": [("request_id", "id")],
    "POST /api/v1/requests/{request_id}/complete": [("case_id", "id")],
    "POST /api/v1/cases": [("case_id", "id")],
    "POST /api/v1/tasks": [("task_id", "id")],
    "POST /api/v1/partners": [("partner_id", "id")],
    "POST /api/v1/invoices": [("invoice_id", "id")],
    "POST /api/v1/earnings": [("earning_id", "id")],
    "POST /api/v1/support/tickets": [("ticket_id", "id")],
    "POST /api/v1/admin/users": [("admin_id", "id")],
    "POST /api/v1/legal/admin/policies": [("policy_id", "id")],
    "POST /api/v1/ai/chat": [("conversation_id", "conversation_id")],
    "POST /api/v1/messages/threads": [("thread_id", "id")],
    "POST /api/v1/agenda": [("appointment_id", "id")],
}

# Steps 1-5 of consultant signup carry the onboarding token, not the access token.
ONBOARDING_PATHS = {
    "/api/v1/auth/signup/organization",
    "/api/v1/auth/signup/verify",
    "/api/v1/auth/signup/verify/resend",
    "/api/v1/auth/signup/plan",
    "/api/v1/auth/signup/payment",
}

PUBLIC_PATHS = {
    "/api/v1/auth/signup/personal", "/api/v1/auth/login", "/api/v1/auth/login/2fa",
    "/api/v1/auth/consultants", "/api/v1/auth/invite/{token}",
    "/api/v1/auth/verify-email/resend",
    "/api/v1/auth/refresh", "/api/v1/auth/logout", "/api/v1/auth/forgot-password",
    "/api/v1/auth/reset-password", "/api/v1/auth/verify-email",
    "/api/v1/auth/invite/accept", "/api/v1/auth/organizations",
    "/api/v1/auth/register/client", "/api/v1/auth/plans",
    "/api/v1/legal/policies", "/", "/health",
}

schema = app.openapi()
components = schema.get("components", {}).get("schemas", {})


def resolve(node: Any, depth: int = 0) -> Any:
    """Follow $ref, flatten anyOf/allOf. Depth-capped so recursive models terminate."""
    if depth > 8 or not isinstance(node, dict):
        return node if isinstance(node, dict) else {}
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        return resolve(components.get(name, {}), depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in node:
            merged: Dict[str, Any] = {}
            for part in node[key]:
                part = resolve(part, depth + 1)
                if part.get("type") == "null":
                    continue
                merged = {**part, **{k: v for k, v in merged.items() if k != "type"}}
                if merged.get("type"):
                    break
            return {**{k: v for k, v in node.items() if k not in ("allOf", "anyOf", "oneOf")},
                    **merged}
    return node


def example_for(spec: Dict[str, Any], name: str = "", depth: int = 0) -> Any:
    spec = resolve(spec, depth)
    if depth > 6:
        return None
    for key in ("example", "default"):
        if key in spec:
            return spec[key]
    if spec.get("examples"):
        return spec["examples"][0]
    if "enum" in spec and spec["enum"]:
        return spec["enum"][0]

    typ = spec.get("type")
    fmt = spec.get("format")

    if typ == "object" or "properties" in spec:
        return {k: example_for(v, k, depth + 1)
                for k, v in (spec.get("properties") or {}).items()}
    if typ == "array":
        item = example_for(spec.get("items", {}), name, depth + 1)
        return [item] if item is not None else []
    if typ == "boolean":
        return True
    if typ == "integer":
        return spec.get("minimum", 1)
    if typ == "number":
        return spec.get("minimum", 100.0) or 100.0
    if fmt == "date-time":
        return "2026-09-01T10:00:00Z"
    if fmt == "email":
        return "sarah.jenkins@jenkinslaw.com"
    if fmt == "binary" or spec.get("contentMediaType"):
        return None
    return placeholder(name)


def placeholder(name: str) -> str:
    n = name.lower()
    table = {
        "email": "sarah.jenkins@jenkinslaw.com",
        "password": "Str0ng!Pass1",
        "confirm_password": "Str0ng!Pass1",
        "current_password": "Str0ng!Pass1",
        "full_name": "Sarah Jenkins",
        "name_on_card": "Sarah Jenkins",
        "card_number": "4242424242424242",
        "expiry": "12/28",
        "cvc": "123",
        "mobile": "+15550142042",
        "code": "{{otp_code}}",
        "token": "{{invite_token}}",
        "refresh_token": "{{refresh_token}}",
        "organization_id": "{{organization_id}}",
        "organization_name": "Jenkins Immigration Law",
        "office_address": "12 King Street, London, EC2V 8AS",
        "country": "United Kingdom",
        "destination_country": "Canada",
        "visa_type": "Student Visa",
        "client_id": "{{user_id}}",
        "case_id": "{{case_id}}",
        "task_id": "{{task_id}}",
        "request_id": "{{request_id}}",
        "assignee_id": "{{partner_id}}",
        "partner_id": "{{partner_id}}",
        "agent_id": "{{admin_id}}",
        "currency": "USD",
        "subject": "Cannot upload a document",
        "body": "Adding more detail here.",
        "message": "The upload fails at 90% on the mobile app.",
        "title": "Translate Employment Contract (ES to EN)",
        "notes": "Eligibility confirmed; awaiting bank statement.",
        "feedback": "Statement is older than 3 months. Please upload a recent one.",
        "comment": "Checked against the checklist.",
        "reason": "No longer using the service.",
        "version": "2.0",
        "reference": "PAY-2026-0001",
    }
    if n in table:
        return table[n]
    for key, value in table.items():
        if key in n:
            return value
    return f"<{name or 'value'}>"


def capture_script(key: str) -> Optional[Dict[str, Any]]:
    rules = CAPTURE.get(key)
    if not rules:
        return None
    lines = [
        "const ok = pm.response.code >= 200 && pm.response.code < 300;",
        "pm.test('2xx', function () { pm.expect(ok).to.be.true; });",
        "if (ok) {",
        "  let body = {};",
        "  try { body = pm.response.json(); } catch (e) { body = {}; }",
    ]
    for var, field in rules:
        expr = "body" + "".join(f"?.{part}" for part in field.split("."))
        lines.append(
            f"  if ({expr} !== undefined && {expr} !== null) "
            f"{{ pm.collectionVariables.set('{var}', {expr}); "
            f"console.log('saved {var}'); }}"
        )
    lines.append("}")
    return {"listen": "test", "script": {"type": "text/javascript", "exec": lines}}


def build_url(path: str, params: List[Dict[str, Any]]) -> Dict[str, Any]:
    postman_path = path
    variables = []
    for match in re.findall(r"\{(\w+)\}", path):
        if match in PATH_VARS:
            postman_path = postman_path.replace("{" + match + "}", "{{" + match + "}}")
        else:
            postman_path = postman_path.replace("{" + match + "}", ":" + match)
            variables.append({"key": match, "value": f"<{match}>"})

    query = []
    for p in params:
        if p.get("in") != "query":
            continue
        value = example_for(p.get("schema", {}), p["name"])
        query.append({
            "key": p["name"],
            "value": "" if value is None else str(value),
            "description": p.get("description", ""),
            "disabled": not p.get("required", False),
        })

    raw = "{{base_url}}" + postman_path
    if query:
        enabled = [q for q in query if not q["disabled"]]
        if enabled:
            raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in enabled)
    url: Dict[str, Any] = {
        "raw": raw,
        "host": ["{{base_url}}"],
        "path": [seg for seg in postman_path.strip("/").split("/") if seg],
    }
    if query:
        url["query"] = query
    if variables:
        url["variable"] = variables
    return url


def build_body(op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = (op.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        example = example_for(content["application/json"].get("schema", {}))
        return {"mode": "raw", "raw": json.dumps(example, indent=2, default=str),
                "options": {"raw": {"language": "json"}}}
    if "multipart/form-data" in content:
        spec = resolve(content["multipart/form-data"].get("schema", {}))
        rows = []
        for field, sub in (spec.get("properties") or {}).items():
            sub = resolve(sub)
            # Pydantic v2 emits contentMediaType for UploadFile, not format: binary.
            if sub.get("format") == "binary" or sub.get("contentMediaType"):
                rows.append({"key": field, "type": "file", "src": [],
                             "description": "Pick a file: PDF, JPG, PNG, WEBP, DOC or DOCX "
                                            "(max 25 MB)"})
            else:
                rows.append({"key": field, "type": "text",
                             "value": str(example_for(sub, field))})
        return {"mode": "formdata", "formdata": rows}
    return None


folders: Dict[str, Dict[str, Any]] = {}
count = 0

for path, methods in schema["paths"].items():
    for method, op in methods.items():
        if method not in {"get", "post", "put", "patch", "delete"}:
            continue
        count += 1
        tag = (op.get("tags") or ["Other"])[0]
        folder = folders.setdefault(tag, {"name": tag, "item": []})

        key = f"{method.upper()} {path}"
        headers = []
        body = build_body(op)
        if body and body["mode"] == "raw":
            headers.append({"key": "Content-Type", "value": "application/json"})

        request: Dict[str, Any] = {
            "method": method.upper(),
            "header": headers,
            "url": build_url(path, op.get("parameters") or []),
            "description": op.get("summary") or op.get("description") or "",
        }
        if body:
            request["body"] = body

        if path in PUBLIC_PATHS or (path == "/api/v1/legal/policies" and method == "get"):
            request["auth"] = {"type": "noauth"}
        elif path in ONBOARDING_PATHS:
            request["auth"] = {"type": "bearer",
                               "bearer": [{"key": "token", "value": "{{onboarding_token}}",
                                           "type": "string"}]}

        item: Dict[str, Any] = {
            "name": op.get("summary") or f"{method.upper()} {path}",
            "request": request,
            "response": [],
        }
        script = capture_script(key)
        if script:
            item["event"] = [script]
        folder["item"].append(item)

# Auth first, Super Admin last, everything else alphabetical.
def sort_key(name: str) -> tuple:
    if name.startswith("Auth"):
        return (0, name)
    if name.startswith("Super Admin"):
        return (2, name)
    return (1, name)


collection = {
    "info": {
        "name": "WebImove API",
        "_postman_id": "webimove-api-collection",
        "description": (
            "WebImove — AI powered immigration case management.\n\n"
            "Generated from the live FastAPI OpenAPI schema by "
            "`postman/generate_collection.py`. Re-run that script after changing routes; "
            "do not hand-edit this file.\n\n"
            "**How to use**\n"
            "1. Select the `WebImove — Local` environment.\n"
            "2. Run the consultant signup steps 1→5 in order, or `Auth → Sign in`. "
            "Tokens are captured automatically into collection variables.\n"
            "3. In non-production, `debug_code` in OTP responses is saved to `{{otp_code}}`, "
            "so verification steps work without opening an inbox.\n"
            "4. Ids from create calls (request, case, task, invoice…) are saved automatically, "
            "so detail and action endpoints resolve without copy-paste.\n\n"
            "Endpoints under **Super Admin** need a platform account: "
            "`python scripts_create_superadmin.py admin@webimove.com 'StrongPass1!'`"
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "auth": {"type": "bearer",
             "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}]},
    "event": [
        {"listen": "prerequest",
         "script": {"type": "text/javascript",
                    "exec": ["if (!pm.collectionVariables.get('base_url')) {",
                             "  pm.collectionVariables.set('base_url', "
                             "'http://localhost:8000');",
                             "}"]}},
    ],
    "variable": [
        {"key": "base_url", "value": "http://localhost:8000"},
        {"key": "access_token", "value": ""},
        {"key": "refresh_token", "value": ""},
        {"key": "onboarding_token", "value": ""},
        {"key": "challenge_token", "value": ""},
        {"key": "otp_code", "value": ""},
        {"key": "invite_token", "value": ""},
        {"key": "tenant_id", "value": ""},
        {"key": "organization_id", "value": ""},
        {"key": "user_id", "value": ""},
        {"key": "consultant_id", "value": ""},
        {"key": "client_id", "value": ""},
        {"key": "partner_id", "value": ""},
        {"key": "admin_id", "value": ""},
        {"key": "request_id", "value": ""},
        {"key": "case_id", "value": ""},
        {"key": "document_id", "value": ""},
        {"key": "task_id", "value": ""},
        {"key": "invoice_id", "value": ""},
        {"key": "earning_id", "value": ""},
        {"key": "ticket_id", "value": ""},
        {"key": "policy_id", "value": ""},
        {"key": "conversation_id", "value": ""},
        {"key": "thread_id", "value": ""},
        {"key": "notification_id", "value": ""},
        {"key": "appointment_id", "value": ""},
        {"key": "session_id", "value": ""},
        {"key": "file_id", "value": ""},
        {"key": "plan_code", "value": "professional"},
        {"key": "kind", "value": "privacy_policy"},
        {"key": "key", "value": "maintenance_mode"},
    ],
    "item": [folders[name] for name in sorted(folders, key=sort_key)],
}

environment = {
    "name": "WebImove — Local",
    "values": [
        {"key": "base_url", "value": "http://10.10.20.11:8002", "type": "default",
         "enabled": True},
        {"key": "consultant_email", "value": "[EMAIL_ADDRESS]",
         "type": "default", "enabled": True},
        {"key": "consultant_password", "value": "Ruhul@123", "type": "secret",
         "enabled": True},
        {"key": "admin_email", "value": "[moob@yopmail.com]", "type": "default",
         "enabled": True},
        {"key": "admin_password", "value": "Ruhul@104", "type": "secret",
         "enabled": True},
    ],
    "_postman_variable_scope": "environment",
}

COLLECTION.write_text(json.dumps(collection, indent=2, default=str))
ENVIRONMENT.write_text(json.dumps(environment, indent=2))

print(f"{count} requests across {len(folders)} folders")
print(f"  {COLLECTION}")
print(f"  {ENVIRONMENT}")
