"""The dictionary answers in the language it claims to, in the variety the
client asked for.

Item A4: the Portuguese was European - "A aguardar envio", "palavra-passe",
"convidou-o", "ficheiros" - while the frontend had already been written in
Brazilian Portuguese, so the two halves of the same screen disagreed.

Items A1 and A2: the plan catalogue and the consent screen were built in English
in the router and described every case as an immigration one, on a platform that
also runs Civil, Tax and Labour work.
"""
import re

import pytest

from app.core.i18n import CATALOGUE, translate
from app.modules.subscriptions import plans as plans_module
from app.modules.subscriptions.plans import PLANS

LANGUAGES = ("en", "pt", "es")

#: Spellings that mark European rather than Brazilian Portuguese.
EUROPEAN_PORTUGUESE = {
    "estar a + infinitive": r"\b[Aa] aguardar\b",
    "palavra-passe": r"\bpalavra-passe\b",
    "ficheiro": r"\bficheiros?\b",
    "ecrã": r"\becrã\b",
    "telemóvel": r"\btelemóvel\b",
    "utilizador": r"\butilizador\b",
    "contacto": r"\bcontact[oa]s?\b",
    "enclitic pronoun": r"\b\w+(?:ou|ar|er)-(?:o|a|os|as|lhe|lhes)\b",
}


def portuguese_strings():
    """Every Portuguese string, including both halves of a plural entry."""
    for key, entry in CATALOGUE.items():
        value = entry.get("pt")
        if isinstance(value, str):
            yield key, value
        elif isinstance(value, dict):
            for form, text in value.items():
                if isinstance(text, str):
                    yield f"{key}[{form}]", text


class TestBrazilianPortuguese:
    @pytest.mark.parametrize("name,pattern", sorted(EUROPEAN_PORTUGUESE.items()))
    def test_no_european_spellings(self, name, pattern):
        offenders = [
            f"{key}: {value}"
            for key, value in portuguese_strings()
            if re.search(pattern, value)
        ]
        assert not offenders, f"European Portuguese ({name}):\n  " + "\n  ".join(offenders)

    def test_the_gerund_is_used_where_european_portuguese_would_not(self):
        assert translate("status.waiting_for_client", "pt") == "Aguardando o cliente"


class TestEveryEntryAnswersInAllThree:
    def test_nothing_is_missing_a_language(self):
        # An entry is either one string per language, or a plural entry with
        # the same shape under each.
        incomplete = [
            key for key, entry in CATALOGUE.items()
            if not all(entry.get(lang) for lang in LANGUAGES)
        ]
        assert not incomplete, f"entries missing a language: {incomplete[:10]}"


class TestThePlanCatalogue:
    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_it_speaks_the_workspace_language(self, lang):
        starter = plans_module.localise(PLANS, lang)[0]
        expected = translate("plan.starter.tagline", lang)
        assert starter["tagline"] == expected
        assert starter["features"][0] == translate("plan.starter.feature1", lang)

    def test_it_is_priced_in_the_platform_currency(self):
        for plan in plans_module.localise(PLANS, "pt"):
            assert plan["currency"] == "EUR"

    def test_it_no_longer_calls_every_case_an_immigration_one(self):
        for lang in LANGUAGES:
            for plan in plans_module.localise(PLANS, lang):
                blob = " ".join(plan["features"]).lower()
                assert "immigration case" not in blob
                assert "caso de imigração" not in blob
                assert "caso de inmigración" not in blob


class TestTheConsentScreen:
    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_it_is_written_in_the_client_language(self, lang):
        title = translate("consent.data_processing.title", lang)
        assert title
        # A missing entry returns the key itself.
        assert title != "consent.data_processing.title"

    def test_each_language_says_something_different(self):
        said = {translate("consent.data_processing.description", lang)
                for lang in LANGUAGES}
        assert len(said) == len(LANGUAGES)
