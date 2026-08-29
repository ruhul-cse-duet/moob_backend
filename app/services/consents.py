"""
Consent records — the auditable store behind the Privacy Centre.

Signup asks eleven questions and the Privacy Centre now shows eleven toggles,
but they used to be two separate worlds: six toggles here, and the signup
answers kept as a dict on the user document. Nothing wrote `db.consents` until
the person touched a toggle, so a client who had just accepted the Terms saw
"Terms of Service (required)" switched off - and the record of what they had
actually agreed to was not in the store that is meant to be the record. Five of
the eleven had no toggle at all, so they could not be seen or withdrawn.

The two also use different words for the same things - `ai_ocr_processing` and
`ai_document_analysis`, `terms_and_conditions` and `terms_of_service` - so they
cannot be reconciled without saying which is which. That mapping is here.

Writing is the same shape the Privacy Centre's own PUT uses: the current state
in `consents`, an append-only row in `consent_history`, both stamped with when,
from where and by what. Under GDPR the trail is the point - a granted flag with
no history cannot answer "what did they agree to, and when".
"""
import logging
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.enums import ConsentType
from app.core.utils import utcnow

logger = logging.getLogger(__name__)

#: Signup's field name -> the consent the Privacy Centre shows.
#:
#: Every question signup asks now has a toggle behind it. The two sides still
#: use different words for the same things - `ai_ocr_processing` here,
#: `ai_document_analysis` there - and this table is the only place that says
#: which is which. Renaming a signup field without touching this line silently
#: drops that consent, so the mapping is covered by a test.
SIGNUP_FIELD_TO_CONSENT = {
    "terms_and_conditions": ConsentType.TERMS_OF_SERVICE,
    "privacy_policy": ConsentType.PRIVACY_POLICY,
    "gdpr_data_processing": ConsentType.DATA_PROCESSING,
    "immigration_case": ConsentType.IMMIGRATION_CASE_HANDLING,
    "sensitive_data_processing": ConsentType.SENSITIVE_DATA_PROCESSING,
    "partner_data_sharing": ConsentType.DOCUMENT_SHARING_WITH_PARTNERS,
    "ai_ocr_processing": ConsentType.AI_DOCUMENT_ANALYSIS,
    "ai_legal_assistant": ConsentType.AI_LEGAL_ASSISTANT,
    "email_notifications": ConsentType.EMAIL_NOTIFICATIONS,
    "whatsapp_notifications": ConsentType.WHATSAPP_NOTIFICATIONS,
    "marketing_messages": ConsentType.MARKETING_EMAILS,
}


async def record(
    db: AsyncIOMotorDatabase,
    *,
    user_id: str,
    consent_type: ConsentType,
    granted: bool,
    source: str,
    policy_version: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Dict[str, Any]:
    """Write one consent, current state and history together.

    ``source`` says where the decision came from - ``signup`` or
    ``privacy_centre``. Without it the trail shows a granted flag with no way to
    tell a deliberate toggle from a checkbox nobody read.
    """
    now = utcnow()
    entry: Dict[str, Any] = {
        "user_id": user_id,
        "type": consent_type.value,
        "granted": granted,
        "policy_version": policy_version,
        "source": source,
        "ip": ip,
        "user_agent": user_agent,
        "updated_at": now,
    }
    entry["granted_at" if granted else "revoked_at"] = now

    await db.consents.update_one(
        {"user_id": user_id, "type": consent_type.value},
        {"$set": entry, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    await db.consent_history.insert_one({**entry, "created_at": now})
    return entry


async def record_signup_agreements(
    db: AsyncIOMotorDatabase,
    *,
    user_id: str,
    agreements: Dict[str, Any],
    session: Optional[Dict[str, Any]] = None,
) -> int:
    """Move the signup answers into the consent store.

    Returns how many were written. Never raises: a consent row that fails to
    write must not undo an account the person has already paid for and been
    charged for - the account is the thing that is hard to recover, and the
    Privacy Centre can always be re-toggled.
    """
    session = session or {}
    written = 0
    for field, consent_type in SIGNUP_FIELD_TO_CONSENT.items():
        if field not in agreements:
            continue
        try:
            await record(
                db, user_id=user_id, consent_type=consent_type,
                granted=bool(agreements[field]), source="signup",
                ip=session.get("ip"), user_agent=session.get("user_agent"),
            )
            written += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not record %s consent for %s",
                             consent_type.value, user_id)
    return written
