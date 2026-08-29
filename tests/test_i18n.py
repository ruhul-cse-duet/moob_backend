"""
Server-side translation.

The client reported Spanish screens with English words on them. The cause was
not a missing string in the app: the API was *building* English text from enum
codes, so no translation file could reach it. Now the language travels with the
request and the server renders in it.

Two things are pinned here beyond the obvious. That a missing translation is
visible rather than blank - the key itself is returned, so it shows up in the UI
as `status.new` and gets reported. And that plurals are chosen per language,
because appending an "s" is correct in English and wrong everywhere else.
"""
import pytest

from app.core.deps import language_for
from app.core.i18n import (
    CATALOGUE,
    DEFAULT_LANGUAGE,
    Language,
    from_accept_language,
    missing_keys,
    normalize,
    resolve,
    translate,
)


class TestLanguageResolution:
    @pytest.mark.parametrize("value,expected", [
        ("es", "es"), ("ES", "es"), ("es-419", "es"), ("pt_BR", "pt"),
        ("  En  ", "en"), ("fr", None), ("", None), (None, None),
    ])
    def test_a_language_is_reduced_to_one_we_have(self, value, expected):
        """Real clients send `ES`, `es-419`, `pt_BR`. The region is dropped: a
        Brazilian reader is better served by Portuguese than by English."""
        assert normalize(value) == expected

    def test_the_first_supported_entry_of_a_weighted_header_wins(self):
        """Not the first entry - the first one we can actually serve. A caller
        whose top choice we lack gets their second, not English."""
        assert from_accept_language("fr-FR,fr;q=0.9,es;q=0.8,en;q=0.7") == "es"

    def test_an_entirely_unsupported_header_falls_through(self):
        assert from_accept_language("fr-FR,de;q=0.9") is None

    def test_the_header_beats_the_saved_preference(self):
        """The app sets the header to whatever its UI is showing, so a response
        can never come back in a different language from the screen it is about
        to be drawn on."""
        assert resolve(header="pt", stored="es") == "pt"

    def test_the_saved_preference_covers_a_request_with_no_header(self):
        assert resolve(stored="es") == "es"

    def test_english_is_the_floor(self):
        assert resolve() == DEFAULT_LANGUAGE
        assert resolve(header="fr", stored="de") == DEFAULT_LANGUAGE

    def test_a_persons_language_is_read_from_their_account(self):
        """What email and push use: there is no request to read a header from."""
        class User:
            raw = {"language": "pt"}

        assert language_for(User()) == "pt"
        assert language_for(None) == "en"


class TestTranslation:
    @pytest.mark.parametrize("lang,expected", [
        ("en", "Waiting for client"),
        ("pt", "A aguardar o cliente"),
        ("es", "Esperando al cliente"),
    ])
    def test_the_status_the_client_reported(self, lang, expected):
        """"Waiting for client" showing in a Spanish UI is the exact complaint
        this whole change answers."""
        assert translate("status.waiting_for_client", lang) == expected

    def test_parameters_are_filled_in(self):
        assert translate("documents.summary", "es", approved=3, total=11) == \
            "3 de 11 aprobados"

    @pytest.mark.parametrize("lang,count,expected", [
        ("en", 1, "Upload 1 requested document"),
        ("en", 3, "Upload 3 requested documents"),
        ("es", 1, "Subir 1 documento solicitado"),
        ("es", 3, "Subir 3 documentos solicitados"),
        ("pt", 1, "Enviar 1 documento solicitado"),
        ("pt", 3, "Enviar 3 documentos solicitados"),
    ])
    def test_plurals_are_chosen_per_language(self, lang, count, expected):
        """The old code appended an "s". That is right in English and wrong in
        both other languages we ship."""
        assert translate("action.upload_documents", lang, count=count) == expected

    def test_an_unknown_language_falls_back_to_english(self):
        assert translate("status.new", "fr") == "New request"

    def test_an_unknown_key_returns_itself(self):
        """Visible and greppable in the UI, rather than a blank space nobody
        reports."""
        assert translate("status.invented_by_nobody", "es") == \
            "status.invented_by_nobody"

    def test_a_forgotten_parameter_does_not_take_the_response_down(self):
        result = translate("documents.summary", "en")
        assert isinstance(result, str) and result


class TestCatalogueCompleteness:
    def test_nothing_is_untranslated(self):
        """The check that stops a language shipping half-done. A gap here is a
        word an operator would otherwise find in production."""
        assert missing_keys() == {}

    def test_every_request_status_has_a_rendering(self):
        from app.core.enums import RequestStatus

        for status in RequestStatus:
            assert f"status.{status.value}" in CATALOGUE, status.value

    def test_every_case_stage_has_a_rendering(self):
        from app.core.enums import CASE_STAGE_ORDER

        for stage in CASE_STAGE_ORDER:
            assert f"stage.{stage.value}" in CATALOGUE, stage.value

    def test_every_document_status_has_a_rendering(self):
        from app.core.enums import DocumentStatus

        for status in DocumentStatus:
            assert f"document_status.{status.value}" in CATALOGUE, status.value

    def test_the_picker_names_each_language_in_itself(self):
        """Someone looking for Portuguese should not have to read English."""
        from app.core.i18n import LANGUAGE_NAMES

        assert LANGUAGE_NAMES[Language.PT] == "Português"
        assert LANGUAGE_NAMES[Language.ES] == "Español"
        assert set(LANGUAGE_NAMES) == set(Language)
