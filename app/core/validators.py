"""Shared field types for the things every form asks for.

An email and a phone number arrive from a dozen screens - signup, invitations,
profile edits, client creation - and each of them used to decide for itself what
counted as valid. Which meant `mobile` was a string between 6 and 32 characters
and nothing else: "aaaaaa" was a phone number, and so was "!!!!!!". The first
anyone found out was when a consultant tried to call a client.

So the rules live here once, as types the schemas reuse:

    mobile: Phone
    email: Email

Phone numbers are checked against Google's libphonenumber, which knows how each
country's numbers are actually shaped. Length and digit checks alone cannot: a
Portuguese number written the way it is dialled at home, +351 0912 345 678, is
thirteen digits of nothing but digits and is not callable from anywhere. Only a
table of country rules can tell that from the number next to it that works.

Both *normalise* as well as validate. A number typed as "+1 (201) 555-0123" and
the same number typed as "+12015550123" are one number, and storing them as
typed means a search finds one and misses the other. Storing E.164 is what makes
them equal.
"""
from typing import Annotated, Optional

import phonenumbers
from phonenumbers import NumberParseException
from pydantic import AfterValidator, EmailStr

from app.core.config import settings


def normalize_phone(value: str) -> str:
    """One phone number, in the one shape we store: E.164, e.g. +351212345678.

    Accepts what people actually type - spaces, dashes, brackets, a leading 00
    for the international prefix - and refuses anything that is not a number
    somebody could dial.

    A country code is required unless the deployment names a default region:
    this is immigration software, so the consultant and the client are usually
    not in the same country, and a national number is only dialable from inside
    the country it belongs to. `DEFAULT_PHONE_REGION` exists for a single-country
    deployment that would rather assume one.
    """
    if value is None:
        raise ValueError("Enter a mobile number")

    text = str(value).strip()
    if not text:
        raise ValueError("Enter a mobile number")

    # 00 is the international prefix in most of the world and means the same
    # thing as +. libphonenumber only reads it as one when it is told which
    # country is dialling, which we are deliberately not assuming.
    if text.startswith("00"):
        text = "+" + text[2:]

    region = settings.DEFAULT_PHONE_REGION or None
    if not text.startswith("+") and not region:
        raise ValueError(
            "Include the country code, like +351 912 345 678"
        )

    try:
        parsed = phonenumbers.parse(text, region)
    except NumberParseException as exc:
        # The library's own messages name internal enum values, so they are
        # translated here into the two things a person can act on.
        if exc.error_type == NumberParseException.INVALID_COUNTRY_CODE:
            raise ValueError(
                "That country code does not exist - check the digits after the +"
            ) from exc
        raise ValueError(
            "That does not look like a phone number - try +351 912 345 678"
        ) from exc

    # `is_valid_number` is the country-aware check: right length *and* a real
    # prefix for that country. `is_possible_number` only checks the length,
    # which is what let the trunk-prefix case through before.
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(
            "That is not a valid number for its country - check the country "
            "code and drop any leading 0 after it"
        )

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _normalize_optional_phone(value: Optional[str]) -> Optional[str]:
    """The same rules, for a field somebody is allowed to leave empty.

    An empty string is treated as "not given" rather than as an invalid number:
    a form that submits "" for an untouched optional field is describing an
    absence, and answering it with a validation error is answering the wrong
    question.
    """
    if value is None or not str(value).strip():
        return None
    return normalize_phone(value)


def normalize_email(value: str) -> str:
    """Lowercased and trimmed, because Sarah@Firm.com is one account.

    `EmailStr` has already decided the address is well-formed by the time this
    runs; what is left is the part that decides whether two spellings are the
    same person. Sign-in already lowercases before it looks anyone up, so an
    address stored with capitals is an account that cannot be found by the
    directory it was written into.
    """
    return str(value).strip().lower()


#: A required phone number, stored in E.164.
Phone = Annotated[str, AfterValidator(normalize_phone)]

#: The same, for fields that may be left out.
OptionalPhone = Annotated[Optional[str], AfterValidator(_normalize_optional_phone)]

#: A valid address, stored the one way it will be looked up.
Email = Annotated[EmailStr, AfterValidator(normalize_email)]
