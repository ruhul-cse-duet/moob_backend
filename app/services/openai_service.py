"""OpenAI integration: document OCR/analysis, case guidance, and the AI assistant."""
import base64
import json
import logging
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def client() -> Optional[AsyncOpenAI]:
    global _client
    if not settings.OPENAI_API_KEY:
        return None
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


DOCUMENT_ANALYSIS_PROMPT = """You are an immigration document analyst for a licensed consultancy.
Analyse the supplied document and return STRICT JSON with this shape:
{
  "document_type": "passport | employment_letter | bank_statement | marriage_certificate | other",
  "confidence": 0-100,
  "extracted_fields": {"field_name": "value"},
  "issues": ["short description of each problem found"],
  "expiry_date": "YYYY-MM-DD or null",
  "is_legible": true,
  "recommendation": "approve | request_reupload | manual_review",
  "summary": "one or two sentences for the consultant"
}
Flag documents older than 3 months when recency matters (e.g. bank statements).
Never invent values you cannot read. Return JSON only."""

CASE_GUIDANCE_PROMPT = """You are a senior immigration case strategist.
Given the case context, return STRICT JSON:
{
  "process_guidance": ["ordered next actions for the consultant"],
  "missing_documents": ["document names still needed"],
  "form_suggestions": ["government forms likely required"],
  "risk_flags": ["anything that could delay or reject the case"],
  "client_tasks": [{"title": "...", "description": "...", "due_in_days": 7}]
}
Be specific to the destination country and visa type. Return JSON only."""

ASSISTANT_PROMPT = """You are the WebImove AI assistant embedded in an immigration case
management workspace. You help consultants with process guidance, document requirements and
drafting. You are not a lawyer: never give a definitive legal determination, and tell the
consultant to verify against the current official government source. Be concise."""


def _parse_json(content: str) -> Dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        content = content[4:] if content.startswith("json") else content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"summary": content.strip()}


async def analyze_document(
    *, file_bytes: bytes, mime_type: str, document_name: str, context: str = ""
) -> Dict[str, Any]:
    api = client()
    if api is None:
        return {
            "confidence": 0,
            "recommendation": "manual_review",
            "summary": "AI analysis unavailable (OPENAI_API_KEY not configured).",
            "extracted_fields": {},
            "issues": [],
        }

    is_image = mime_type.startswith("image/")
    user_content: List[Dict[str, Any]] = [
        {"type": "text", "text": f"Document name: {document_name}\nCase context: {context}"}
    ]
    if is_image:
        b64 = base64.b64encode(file_bytes).decode()
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}
        )
    else:
        user_content.append(
            {"type": "text", "text": "The document is a PDF; analyse from the metadata and name."}
        )

    try:
        resp = await api.chat.completions.create(
            model=settings.OPENAI_VISION_MODEL,
            max_tokens=settings.OPENAI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": DOCUMENT_ANALYSIS_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        logger.exception("Document analysis failed")
        return {
            "confidence": 0,
            "recommendation": "manual_review",
            "summary": "Automatic analysis failed. Review this document manually.",
            "extracted_fields": {},
            "issues": [],
        }


async def case_guidance(*, case_context: Dict[str, Any]) -> Dict[str, Any]:
    api = client()
    if api is None:
        return {"process_guidance": [], "missing_documents": [], "form_suggestions": [],
                "risk_flags": [], "client_tasks": []}
    try:
        resp = await api.chat.completions.create(
            model=settings.OPENAI_MODEL,
            max_tokens=settings.OPENAI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": CASE_GUIDANCE_PROMPT},
                {"role": "user", "content": json.dumps(case_context, default=str)},
            ],
        )
        return _parse_json(resp.choices[0].message.content or "")
    except Exception:  # noqa: BLE001
        logger.exception("Case guidance failed")
        return {"process_guidance": [], "missing_documents": [], "form_suggestions": [],
                "risk_flags": [], "client_tasks": []}


async def assistant_reply(*, history: List[Dict[str, str]], message: str,
                          context: Optional[Dict[str, Any]] = None) -> str:
    api = client()
    if api is None:
        return "The AI assistant is not configured. Add OPENAI_API_KEY to enable it."
    messages: List[Dict[str, str]] = [{"role": "system", "content": ASSISTANT_PROMPT}]
    if context:
        messages.append(
            {"role": "system", "content": f"Workspace context: {json.dumps(context, default=str)}"}
        )
    messages.extend(history[-20:])
    messages.append({"role": "user", "content": message})
    try:
        resp = await api.chat.completions.create(
            model=settings.OPENAI_MODEL,
            max_tokens=settings.OPENAI_MAX_TOKENS,
            messages=messages,
        )
        return resp.choices[0].message.content or ""
    except Exception:  # noqa: BLE001
        logger.exception("Assistant reply failed")
        return "The assistant is temporarily unavailable. Please try again."


async def suggest_required_documents(*, visa_type: str, destination_country: str,
                                     summary: str) -> List[Dict[str, Any]]:
    api = client()
    if api is None:
        return []
    prompt = (
        "Return STRICT JSON: {\"documents\": [{\"name\": \"...\", "
        "\"category\": \"identity|employment|financial|civil|education|medical|other\", "
        "\"why\": \"...\", \"due_in_days\": 14}]}. "
        f"Visa type: {visa_type}. Destination: {destination_country}. Request: {summary}"
    )
    try:
        resp = await api.chat.completions.create(
            model=settings.OPENAI_MODEL,
            max_tokens=800,
            messages=[
                {"role": "system", "content": "You are an immigration document checklist expert."},
                {"role": "user", "content": prompt},
            ],
        )
        return _parse_json(resp.choices[0].message.content or "").get("documents", [])
    except Exception:  # noqa: BLE001
        logger.exception("Document suggestion failed")
        return []
