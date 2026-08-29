"""
Consent records behind the Privacy Centre.

The client reported a Privacy Centre showing "Terms of Service (required)"
switched off, on an account that had just accepted the Terms to exist at all.

Signup asked for eleven agreements and kept them as a dict on the user
document. The Privacy Centre read `db.consents`, which nothing wrote until the
person touched a toggle. Two stores for one thing, using different words for it
- `ai_ocr_processing` on one side, `ai_document_analysis` on the other.

Under GDPR the trail is the point, so what is pinned here is that a decision
leaves a record wherever it was made: the same shape, in the same place, with
its source attached.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import ConsentType
from app.services import consents

USER_ID = str(ObjectId())

# Exactly what step 7 of client signup sends.
SIGNUP_AGREEMENTS = {
    "terms_and_conditions": True,
    "privacy_policy": True,
    "gdpr_data_processing": True,
    "immigration_case": True,
    "sensitive_data_processing": True,
    "whatsapp_notifications": False,
    "email_notifications": True,
    "ai_ocr_processing": False,
    "ai_legal_assistant": False,
    "partner_data_sharing": False,
    "marketing_messages": False,
}


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


async def current(db):
    return {c["type"]: c["granted"] async for c in db.consents.find({"user_id": USER_ID})}


class TestSignupAgreements:
    @pytest.mark.asyncio
    async def test_the_required_consents_reach_the_privacy_centre(self, db):
        """The reported bug. A client who has just accepted the Terms must not
        be shown a Terms toggle that is off."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)

        state = await current(db)
        assert state[ConsentType.TERMS_OF_SERVICE.value] is True
        assert state[ConsentType.PRIVACY_POLICY.value] is True
        assert state[ConsentType.DATA_PROCESSING.value] is True

    @pytest.mark.asyncio
    async def test_a_declined_agreement_is_recorded_as_declined(self, db):
        """Not just the granted ones. "They never agreed" has to be provable
        too, and an absent row cannot tell that from "never asked"."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)

        state = await current(db)
        assert state[ConsentType.MARKETING_EMAILS.value] is False
        assert state[ConsentType.AI_DOCUMENT_ANALYSIS.value] is False

    @pytest.mark.asyncio
    async def test_the_two_vocabularies_are_reconciled(self, db):
        """Signup says `ai_ocr_processing`; the Privacy Centre says
        `ai_document_analysis`. Same consent, different word."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID,
            agreements={**SIGNUP_AGREEMENTS, "ai_ocr_processing": True,
                        "partner_data_sharing": True})

        state = await current(db)
        assert state[ConsentType.AI_DOCUMENT_ANALYSIS.value] is True
        assert state[ConsentType.DOCUMENT_SHARING_WITH_PARTNERS.value] is True

    @pytest.mark.asyncio
    async def test_every_privacy_centre_toggle_is_covered(self, db):
        """A toggle with no signup answer behind it is one the Privacy Centre
        would show as off forever."""
        written = await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)

        assert written == len(ConsentType)
        assert set(await current(db)) == {t.value for t in ConsentType}

    @pytest.mark.asyncio
    async def test_where_the_decision_came_from_is_kept(self, db):
        """A checkbox on a signup form and a deliberate toggle are different
        acts, and the trail should say which it was."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)

        row = await db.consents.find_one({"type": ConsentType.TERMS_OF_SERVICE.value})
        assert row["source"] == "signup"
        assert row["granted_at"] is not None

    @pytest.mark.asyncio
    async def test_the_signup_ip_is_captured(self, db):
        """What makes the record evidence rather than a flag."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS,
            session={"ip": "203.0.113.9", "user_agent": "WebImove/1.0"})

        row = await db.consents.find_one({"type": ConsentType.PRIVACY_POLICY.value})
        assert row["ip"] == "203.0.113.9"
        assert row["user_agent"] == "WebImove/1.0"

    @pytest.mark.asyncio
    async def test_a_partial_payload_writes_what_it_has(self, db):
        written = await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements={"terms_and_conditions": True})

        assert written == 1
        assert await current(db) == {ConsentType.TERMS_OF_SERVICE.value: True}

    @pytest.mark.asyncio
    async def test_a_failure_here_does_not_take_the_signup_down(self, db, monkeypatch):
        """The account is the thing that is hard to recover - it has been paid
        for. A consent row can always be re-toggled."""
        async def explode(*a, **k):
            raise RuntimeError("write failed")

        monkeypatch.setattr(consents, "record", explode)

        assert await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS) == 0


class TestConsentHistory:
    @pytest.mark.asyncio
    async def test_every_decision_appends_to_the_trail(self, db):
        """`consents` is the current state and can only ever hold one row per
        type; the history is what answers "and when did that change"."""
        await consents.record(db, user_id=USER_ID,
                              consent_type=ConsentType.AI_DOCUMENT_ANALYSIS,
                              granted=True, source="privacy_centre")
        await consents.record(db, user_id=USER_ID,
                              consent_type=ConsentType.AI_DOCUMENT_ANALYSIS,
                              granted=False, source="privacy_centre")

        assert await db.consents.count_documents({}) == 1
        assert await db.consent_history.count_documents({}) == 2

    @pytest.mark.asyncio
    async def test_revoking_stamps_a_revoked_at(self, db):
        await consents.record(db, user_id=USER_ID,
                              consent_type=ConsentType.MARKETING_EMAILS,
                              granted=False, source="privacy_centre")

        row = await db.consents.find_one({})
        assert row["granted"] is False
        assert row["revoked_at"] is not None

    @pytest.mark.asyncio
    async def test_a_toggle_replaces_the_signup_answer(self, db):
        """The Privacy Centre is where someone changes their mind, so the later
        decision has to win in the current state."""
        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)
        await consents.record(db, user_id=USER_ID,
                              consent_type=ConsentType.AI_DOCUMENT_ANALYSIS,
                              granted=True, source="privacy_centre")

        state = await current(db)
        assert state[ConsentType.AI_DOCUMENT_ANALYSIS.value] is True
        # And both acts survive in the trail.
        rows = [r async for r in db.consent_history.find(
            {"type": ConsentType.AI_DOCUMENT_ANALYSIS.value})]
        assert [r["source"] for r in rows] == ["signup", "privacy_centre"]


class TestThePrivacyCentreScreen:
    """What the screen draws, in one call.

    Five of the eleven things signup asks about had no toggle at all, so a
    client could neither see nor withdraw them - which is the half of GDPR that
    matters most. Every question now has a switch behind it.
    """

    @pytest.mark.asyncio
    async def test_every_signup_question_has_a_toggle(self, db):
        """The mapping table is the only place that says `ai_ocr_processing`
        and `ai_document_analysis` are the same consent. A signup field renamed
        without touching it would silently drop that consent."""
        assert set(consents.SIGNUP_FIELD_TO_CONSENT) == set(SIGNUP_AGREEMENTS)
        assert set(consents.SIGNUP_FIELD_TO_CONSENT.values()) == set(ConsentType)

    @pytest.mark.asyncio
    async def test_the_screen_needs_no_table_of_its_own(self, db):
        """Each row carries its own wording, so adding a consent makes a
        labelled toggle appear rather than a blank one."""
        from unittest.mock import Mock

        from app.modules.privacy import router as privacy

        await consents.record_signup_agreements(
            db, user_id=USER_ID, agreements=SIGNUP_AGREEMENTS)

        result = await privacy.my_consents(user=Mock(id=USER_ID), db=db, lang="es")
        rows = result["consents"]

        assert len(rows) == len(ConsentType)
        for row in rows:
            assert row["label"], row["type"]
            assert row["description"], row["type"]
            # A label that fell through to its key is a missing translation.
            assert not row["label"].startswith("consent."), row["type"]

    @pytest.mark.asyncio
    async def test_the_required_ones_are_marked(self, db):
        """So the app can warn before someone withdraws the consent their case
        runs on. It does not block the withdrawal - that right is not ours to
        take away - it just has to be able to say what it costs."""
        from unittest.mock import Mock

        from app.core.enums import REQUIRED_CONSENTS
        from app.modules.privacy import router as privacy

        result = await privacy.my_consents(user=Mock(id=USER_ID), db=db, lang="en")
        required = {r["type"] for r in result["consents"] if r["required"]}

        assert required == {c.value for c in REQUIRED_CONSENTS}
        assert ConsentType.MARKETING_EMAILS.value not in required

    @pytest.mark.asyncio
    async def test_the_screen_speaks_the_callers_language(self, db):
        """The reported bug was English words on a Spanish screen."""
        from unittest.mock import Mock

        from app.modules.privacy import router as privacy

        by_lang = {}
        for lang in ("en", "pt", "es"):
            result = await privacy.my_consents(user=Mock(id=USER_ID), db=db, lang=lang)
            by_lang[lang] = next(r["label"] for r in result["consents"]
                                 if r["type"] == ConsentType.MARKETING_EMAILS.value)

        assert by_lang["en"] == "Marketing emails"
        assert by_lang["es"] == "Correos de marketing"
        assert by_lang["pt"] == "Emails de marketing"
