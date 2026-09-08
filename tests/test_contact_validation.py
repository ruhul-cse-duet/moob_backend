"""Email and phone, checked once instead of a dozen times badly.

`mobile` used to be "a string between 6 and 32 characters" and nothing else, on
every screen that asked for one. So "aaaaaa" was a phone number, and so was
"!!!!!!" - and the first anyone found out was a consultant trying to call a
client back.

Then it was digits and a length, which is better and still not enough: a
Portuguese number written the way it is dialled at home, +351 0912 345 678, is
thirteen digits of nothing but digits and cannot be reached from anywhere. Only
a table of per-country rules separates that from the number beside it that
works, which is what libphonenumber is. What is pinned here is that the check is
country-aware, not merely shaped like one.

Normalisation is the other half. "+1 (201) 555-0123" and "+12015550123" are the
same number, and storing them as typed means the directory finds one and misses
the other; the same goes for Sarah@Firm.com and sarah@firm.com, which sign-in
already lowercases before it looks anybody up.
"""
import pytest
from pydantic import ValidationError

from app.core.validators import normalize_email, normalize_phone
from app.modules.auth import schemas as auth_schemas
from app.modules.partners import schemas as partner_schemas
from app.modules.users import schemas as user_schemas


class TestPhoneNumbers:
    @pytest.mark.parametrize("typed,stored", [
        ("+351 21 234 5678", "+351212345678"),
        ("+1 201-555-0123", "+12015550123"),
        ("  +880 2-7111234  ", "+88027111234"),
        ("+44 (121) 234 5678", "+441212345678"),
        ("+34 810 12 34 56", "+34810123456"),
        # 00 is the international prefix almost everywhere and means the same
        # thing as +, so it must not become a second spelling of one number.
        ("00351212345678", "+351212345678"),
    ])
    def test_a_number_is_stored_one_way_however_it_was_typed(self, typed, stored):
        assert normalize_phone(typed) == stored

    @pytest.mark.parametrize("rubbish", [
        "aaaaaa",              # passed the original length check
        "!!!!!!",              # so did this
        "+",
        "",
        "   ",
        "+12345",              # too short for its country
        "+1234567890123456",   # longer than E.164 allows
        "+999912345678",       # no such country code
    ])
    def test_what_is_not_a_number_is_refused(self, rubbish):
        with pytest.raises(ValueError):
            normalize_phone(rubbish)

    def test_the_national_trunk_prefix_is_caught(self):
        """The case a digit count cannot see.

        +351 912 345 678 is a real Portuguese mobile. +351 0912 345 678 is how
        the same number is written inside Portugal, and dialling it from abroad
        reaches nobody. Both are digits, both are a plausible length, and only
        the country's own numbering rules tell them apart.
        """
        assert normalize_phone("+351912345678") == "+351912345678"

        with pytest.raises(ValueError, match="not a valid number"):
            normalize_phone("+3510912345678")

    def test_a_country_code_is_required(self):
        # Immigration software: the consultant and the client are usually not in
        # the same country, so a national number is not enough to call anyone.
        with pytest.raises(ValueError, match="country code"):
            normalize_phone("01700000000")

    def test_a_deployment_may_assume_one_country(self, monkeypatch):
        """`DEFAULT_PHONE_REGION` for an operator running in one country only."""
        from app.core import validators

        monkeypatch.setattr(validators.settings, "DEFAULT_PHONE_REGION", "PT")

        assert normalize_phone("21 234 5678") == "+351212345678"

    @pytest.mark.parametrize("typed,says", [
        # Each message names the one thing to change, rather than repeating
        # libphonenumber's own wording, which is an internal enum name.
        ("call me maybe", "country code"),
        ("+call me maybe", "does not look like a phone number"),
        ("+999912345678", "country code does not exist"),
        ("+3510912345678", "not a valid number"),
    ])
    def test_each_message_says_what_to_do(self, typed, says):
        with pytest.raises(ValueError, match=says):
            normalize_phone(typed)


class TestEmails:
    def test_case_and_spacing_do_not_make_a_second_account(self):
        assert normalize_email("  Sarah@Firm.COM ") == "sarah@firm.com"


class TestTheSchemasActuallyUseThem:
    """The rules are only worth having where the data comes in."""

    def test_signup_normalises_both(self):
        payload = auth_schemas.SignupPersonal(
            full_name="Sarah Jenkins", email="Sarah@Firm.COM",
            mobile="+351 21 234 5678")

        assert payload.email == "sarah@firm.com"
        assert payload.mobile == "+351212345678"

    def test_signup_refuses_a_non_number(self):
        with pytest.raises(ValidationError):
            auth_schemas.SignupPersonal(
                full_name="Sarah Jenkins", email="sarah@firm.com", mobile="aaaaaa")

    def test_client_signup_is_held_to_the_same_rules(self):
        with pytest.raises(ValidationError):
            auth_schemas.ClientSignupStep1(
                full_name="Ayesha Rahman", email="a@b.com", mobile="!!!!!!",
                accept_terms=True)

    def test_a_partner_invitation_too(self):
        payload = partner_schemas.PartnerInvite(
            full_name="Nadia Volkova", email="NADIA@Translations.com",
            mobile="00351212345678", role="Certified Translator")

        assert payload.email == "nadia@translations.com"
        assert payload.mobile == "+351212345678"

    def test_an_optional_number_may_be_left_out(self):
        # An untouched field submits "" from most forms. That is an absence,
        # not an invalid number, and answering it with an error answers the
        # wrong question.
        assert user_schemas.ProfileUpdate(mobile=None).mobile is None
        assert user_schemas.ProfileUpdate(mobile="").mobile is None
        assert user_schemas.ProfileUpdate(mobile="   ").mobile is None

    def test_but_a_number_that_is_given_is_still_checked(self):
        with pytest.raises(ValidationError):
            user_schemas.ProfileUpdate(mobile="not a phone")
