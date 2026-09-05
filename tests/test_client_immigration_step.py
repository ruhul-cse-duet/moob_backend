"""Step 4 of the client signup: the immigration profile.

Reported as "nationality and current country do not save". They did save - when
the app used the server's own names for them. The screen calls one of them
*Current country*, the schema called it `country_of_residence`, and an unknown
key on a Pydantic model is dropped in silence: the POST returned 200 and stored
nothing, which is the worst of both.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.modules.auth import schemas as s
from app.modules.auth import service

SIGNUP_ID = ObjectId()


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["webimove_platform"]
    await database.signups.insert_one({"_id": SIGNUP_ID, "email": "a@b.com"})
    monkeypatch.setattr(service, "platform_db", lambda: database)
    monkeypatch.setattr(service, "_load_signup",
                        lambda token: database.signups.find_one({"_id": SIGNUP_ID}))
    monkeypatch.setattr(service, "create_onboarding_token",
                        lambda **kwargs: "onboarding-token")
    return database


class TestTheNamesTheAppMayUse:
    def test_the_screens_own_wording_is_understood(self):
        payload = s.ClientSignupImmigration(
            passport="X1234567", citizenship="Spanish",
            current_country="Portugal", immigration_type="Student Visa")
        assert payload.nationality == "Spanish"
        assert payload.country_of_residence == "Portugal"
        assert payload.passport_number == "X1234567"
        assert payload.preferred_immigration_type == "Student Visa"

    def test_the_documented_names_still_work(self):
        payload = s.ClientSignupImmigration(
            nationality="Spanish", country_of_residence="Portugal",
            destination_country="Canada")
        assert payload.nationality == "Spanish"
        assert payload.country_of_residence == "Portugal"
        assert payload.destination_country == "Canada"


async def test_what_was_sent_is_stored_and_echoed_back(db):
    payload = s.ClientSignupImmigration(
        passport_number="X1234567", nationality="Spanish",
        current_country="Portugal", visa_type="Student Visa")

    result = await service.set_client_immigration("token", payload)

    stored = (await db.signups.find_one({"_id": SIGNUP_ID}))["immigration_profile"]
    assert stored["nationality"] == "Spanish"
    assert stored["country_of_residence"] == "Portugal"
    assert stored["preferred_immigration_type"] == "Student Visa"
    # The app can now see what landed instead of assuming.
    assert result["saved"] == stored
    assert result["next_step"] == "consultant"


async def test_going_back_to_fix_one_field_keeps_the_others(db):
    await service.set_client_immigration("token", s.ClientSignupImmigration(
        passport_number="X1234567", nationality="Spanish",
        current_country="Portugal"))

    await service.set_client_immigration("token", s.ClientSignupImmigration(
        nationality="Portuguese"))

    stored = (await db.signups.find_one({"_id": SIGNUP_ID}))["immigration_profile"]
    assert stored["nationality"] == "Portuguese"
    assert stored["passport_number"] == "X1234567"
    assert stored["country_of_residence"] == "Portugal"
