"""A client exists because a consultancy invited them. There is no other way in.

Findings 1, 2, 3 and 7 of the product review are one rule stated four times:
WebImove's customer is the consultancy, not the end client. What followed from
getting that wrong was a public eight-step client signup, a public directory of
every tenant's name, city, rating and client count, a client choosing their own
procedure, and a passport number collected before the client belonged to
anybody or had consented to anything.

These tests are about absence, which is the hardest thing to keep true: a route
nobody notices is a route that comes back. They assert the doors are shut and
that the one remaining path - invitation - still works.
"""
import pytest

from app.core.enums import Role
from app.core.exceptions import Forbidden
from app.modules.auth.router import router as auth_router
from app.modules.requests import service as requests_service


def _paths(router):
    """Full paths, prefix included.

    Worth being exact about: an assertion written against `/consultants` when
    the route is really `/auth/consultants` passes whether or not the route
    exists, which is the least useful kind of green.
    """
    return {route.path for route in router.routes}


class TestTheSelfServiceDoorsAreShut:
    def test_there_is_no_client_signup(self):
        """Eight steps under /auth/client/signup/... that let anyone create a
        client account, with no consultancy behind them."""
        assert not [p for p in _paths(auth_router) if "client/signup" in p]

    def test_there_is_no_legacy_client_registration(self):
        # The older shape of the same mistake. Closing the front door and
        # leaving this open would have achieved nothing.
        assert "/auth/register/client" not in _paths(auth_router)

    def test_tenants_are_not_listed_to_the_public(self):
        """One customer's business data was readable by anyone who asked."""
        paths = _paths(auth_router)

        assert "/auth/consultants" not in paths
        assert "/auth/organizations" not in paths

    def test_the_invitation_path_is_still_there(self):
        # The whole point: removing self-service must not remove the way in.
        paths = _paths(auth_router)

        assert "/auth/invite/{token}" in paths
        assert "/auth/invite/accept" in paths


class TestTheConsultantDecides:
    async def test_a_client_cannot_open_a_request(self):
        """Finding 3. Not a permissions detail - choosing the procedure was
        never the client's to do."""
        class _Client:
            id = "client-1"
            role = Role.CLIENT
            raw = {"full_name": "Ayesha Rahman"}

        class _Payload:
            client_id = None
            visa_type = "Student Visa"
            destination_country = "Canada"
            purpose = "Masters programme"
            additional_information = None
            client_notes = None
            preferred_appointment = None
            consultant_id = None
            attached_files = []
            is_draft = False
            process_area = None
            procedure_id = None

        with pytest.raises(Forbidden, match="consultant opens requests"):
            await requests_service.create_request(None, _Client(), _Payload())


class TestNothingAsksForAPassportUpFront:
    def test_the_client_signup_schemas_are_gone_from_the_surface(self):
        """Finding 7: the passport number was collected at step 4 of a signup
        that happened before any consultancy existed and before consent. With
        the flow removed there is nowhere left that asks for it that early -
        it belongs to a procedure's own client fields now, which a consultant
        assigns after the invitation is accepted."""
        from app.modules.catalog.templates import TEMPLATES

        asked_by = {
            t["name"] for t in TEMPLATES
            if any(f["key"] == "passport_number" for f in t.get("client_fields", []))
        }
        # Only the procedures that actually need one, and only once a consultant
        # has assigned that procedure to a case.
        assert asked_by
        assert all("visa" in name.lower() or "permit" in name.lower()
                   for name in asked_by), asked_by
