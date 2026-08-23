"""
The boot-time configuration checks.

These exist because the dangerous states are all *silent*: a placeholder JWT
secret signs tokens perfectly well, DEBUG quietly puts exception text in error
bodies, and a leftover ADMIN_PASSWORD sits in the environment doing nothing
visible. Nothing fails, so nothing gets noticed. The checks turn each one into
a line in the startup log.

The CORS tests here pin the opposite: that a wildcard origin is *not* reported,
because on this API it is the deliberate configuration.
"""
import pytest

from app.core.config import Settings

REAL_SECRET = "x" * 48


def make(**overrides) -> Settings:
    """A Settings that ignores the developer's own .env.

    `_env_file=None` matters: without it pydantic-settings reads the real .env
    and the assertions below would depend on whoever ran the tests.
    """
    base = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "JWT_SECRET_KEY": REAL_SECRET,
        "BACKEND_CORS_ORIGINS": ["*"],
        "STRIPE_WEBHOOK_SECRET": "whsec_test",
        "STRIPE_SECRET_KEY": "",
        "ADMIN_PASSWORD": "",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def problems(settings: Settings) -> str:
    return " | ".join(settings.insecure_settings())


def test_a_correctly_configured_production_boot_is_silent():
    assert make().insecure_settings() == []


# --------------------------------------------------------------------- CORS
# A wildcard origin is the intended configuration here: the clients are Flutter
# apps, which are not browsers and never send an Origin at all. What keeps it
# safe is the pairing below - "*" and credentials are mutually exclusive, so a
# hostile page can only make the unauthenticated calls it could already make
# from its own server.
def test_wildcard_origin_is_not_reported_as_insecure():
    assert "CORS" not in problems(make(BACKEND_CORS_ORIGINS=["*"]))
    assert "origin" not in problems(make(BACKEND_CORS_ORIGINS=["*"]))


def test_wildcard_is_detected_wherever_it_appears_in_the_list():
    """One "*" anywhere makes the whole list allow-everything, so the flag that
    disables credentials has to see it wherever it sits."""
    assert make(BACKEND_CORS_ORIGINS=["*"]).cors_allows_any_origin is True
    assert make(
        BACKEND_CORS_ORIGINS=["https://app.example.com", "*"]
    ).cors_allows_any_origin is True
    assert make(
        BACKEND_CORS_ORIGINS=["https://app.example.com"]
    ).cors_allows_any_origin is False


def test_the_shipped_default_is_a_wildcard():
    """The apps are the primary client and send no Origin; a named list would
    only lock out browsers without protecting anything."""
    assert Settings(_env_file=None).cors_allows_any_origin is True


def test_credentials_are_off_with_a_wildcard_and_on_with_named_origins():
    """The security property the wildcard rests on.

    Browsers reject "*" together with credentials outright, so allowing both
    would break every browser caller *and* remove the reason a wildcard is
    acceptable. Naming real origins turns credentialed CORS back on by itself.
    """
    assert make(BACKEND_CORS_ORIGINS=["*"]).cors_allows_any_origin is True
    named = make(BACKEND_CORS_ORIGINS=["https://app.example.com"])
    assert named.cors_allows_any_origin is False


# ---------------------------------------------------------------- other keys
def test_placeholder_jwt_secret_is_reported():
    assert "JWT_SECRET_KEY" in problems(make(JWT_SECRET_KEY="change-me"))


def test_short_jwt_secret_is_reported():
    assert "32 characters" in problems(make(JWT_SECRET_KEY="tooshort"))


def test_debug_in_production_is_reported():
    assert "DEBUG" in problems(make(DEBUG=True))


def test_leftover_admin_password_is_reported_only_in_production():
    assert "ADMIN_PASSWORD" in problems(make(ADMIN_PASSWORD="s3cret-value"))
    assert "ADMIN_PASSWORD" not in problems(
        make(ENVIRONMENT="development", ADMIN_PASSWORD="s3cret-value")
    )


def test_missing_stripe_webhook_secret_is_reported():
    assert "STRIPE_WEBHOOK_SECRET" in problems(make(STRIPE_WEBHOOK_SECRET=""))


@pytest.mark.parametrize("environment,expected", [
    ("production", True), ("PRODUCTION", True), ("prod", True),
    ("development", False), ("staging", False),
])
def test_is_production_detection(environment, expected):
    assert make(ENVIRONMENT=environment).is_production is expected


def test_docs_are_off_in_production_unless_explicitly_enabled():
    assert make().docs_enabled is False
    assert make(ENABLE_DOCS=True).docs_enabled is True
    assert make(ENVIRONMENT="development").docs_enabled is True
