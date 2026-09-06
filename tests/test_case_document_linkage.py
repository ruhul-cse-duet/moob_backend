"""A case must show the documents of the request it came from.

Opening a case stamps `case_id` onto the request's documents. Cases created
before that binding existed - and any document added to the request afterwards
- carry only `request_id`, so a case-scoped query found nothing and the screen
read "No documents linked yet" while the request itself listed two.
"""
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role
from app.modules.cases import service


class FakeUser:
    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role
        self.raw = {}


class Payload:
    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


@pytest.fixture
def db():
    return AsyncMongoMockClient()["wm_t_test"]


@pytest.fixture
async def seeded(db):
    client = await db.users.insert_one(
        {"role": Role.CLIENT.value, "full_name": "Client Ahsan"})
    client_id = str(client.inserted_id)
    request = await db.requests.insert_one(
        {"client_id": client_id, "case_id": None, "reference": "REQ-103"})
    request_id = str(request.inserted_id)
    for name in ("National ID", "Birth Certificate"):
        await db.documents.insert_one(
            {"name": name, "request_id": request_id, "client_id": client_id,
             "status": "with_consultant"})
    return {"client_id": client_id, "request_id": request_id,
            "user": FakeUser("consultant-1", Role.CONSULTANT_OWNER)}


async def test_opening_a_case_binds_the_requests_documents(db, seeded):
    out = await service.create_case(db, seeded["user"], Payload(
        client_id=seeded["client_id"], case_type="Business Visa",
        destination_country="UAE", deadline=None,
        request_id=seeded["request_id"]))

    case_id = out["id"]
    async for doc in db.documents.find({}):
        assert doc["case_id"] == case_id
    request = await db.requests.find_one({"reference": "REQ-103"})
    assert request["case_id"] == case_id


async def test_a_case_created_before_the_binding_still_shows_them(db, seeded):
    """The state the screenshot was in: a case whose documents were never stamped."""
    case = await db.cases.insert_one({
        "reference": "CAS-082", "client_id": seeded["client_id"],
        "request_id": seeded["request_id"], "stage": "new_request",
        "consultant_id": "consultant-1"})

    out = await service.get_case(db, seeded["user"], str(case.inserted_id))

    names = sorted(d["name"] for d in out["documents"])
    assert names == ["Birth Certificate", "National ID"]


async def test_reading_it_repairs_the_link(db, seeded):
    """So every other case-scoped query - a delegated partner's - finds them too."""
    case = await db.cases.insert_one({
        "reference": "CAS-082", "client_id": seeded["client_id"],
        "request_id": seeded["request_id"], "stage": "new_request",
        "consultant_id": "consultant-1"})
    case_id = str(case.inserted_id)

    await service.get_case(db, seeded["user"], case_id)

    stamped = await db.documents.count_documents({"case_id": case_id})
    assert stamped == 2


async def test_a_case_with_no_request_is_unaffected(db, seeded):
    """A standalone case must not pick up unrelated documents."""
    case = await db.cases.insert_one({
        "reference": "CAS-099", "client_id": seeded["client_id"],
        "request_id": None, "stage": "new_request",
        "consultant_id": "consultant-1"})

    out = await service.get_case(db, seeded["user"], str(case.inserted_id))
    assert out["documents"] == []
