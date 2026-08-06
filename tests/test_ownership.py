"""Consultant ownership: single owner for clients, many-to-many for partners."""
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role
from app.schemas.common import PageParams
from app.services.ownership import (
    attach_consultants,
    consultant_map,
    link_partner_to_consultant,
    resolve_consultant_id,
)
from app.services.pagination import paginate


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


@pytest.fixture
async def seeded(db):
    owner = await db.users.insert_one({
        "full_name": "Sarah Jenkins", "title": "Senior Consultant",
        "email": "sarah@jenkinslaw.com", "role": Role.CONSULTANT_OWNER.value})
    second = await db.users.insert_one({
        "full_name": "James Okoro", "title": "Consultant",
        "email": "james@jenkinslaw.com", "role": Role.CONSULTANT.value})
    partner = await db.users.insert_one({
        "full_name": "Nadia Volkova", "role": Role.PARTNER.value,
        "consultant_ids": []})
    return {"owner": str(owner.inserted_id), "second": str(second.inserted_id),
            "partner": str(partner.inserted_id)}


async def test_resolve_falls_back_to_owner(db, seeded):
    assert await resolve_consultant_id(db) == seeded["owner"]


async def test_resolve_prefers_the_parent_case(db, seeded):
    case = await db.cases.insert_one({"consultant_id": seeded["second"]})
    assert await resolve_consultant_id(db, case_id=str(case.inserted_id)) == seeded["second"]


async def test_resolve_prefers_the_client_owner_over_workspace_owner(db, seeded):
    client = await db.users.insert_one({"role": Role.CLIENT.value,
                                        "consultant_id": seeded["second"]})
    got = await resolve_consultant_id(db, client_id=str(client.inserted_id))
    assert got == seeded["second"]


async def test_partner_accumulates_consultants_instead_of_overwriting(db, seeded):
    """The whole reason partners use an array: two consultants, same partner."""
    await link_partner_to_consultant(db, seeded["partner"], seeded["owner"])
    await link_partner_to_consultant(db, seeded["partner"], seeded["second"])
    await link_partner_to_consultant(db, seeded["partner"], seeded["owner"])  # idempotent

    partner = await db.users.find_one({"role": Role.PARTNER.value})
    assert sorted(partner["consultant_ids"]) == sorted([seeded["owner"], seeded["second"]])


async def test_consultant_map_is_one_query_for_many_ids(db, seeded):
    lookup = await consultant_map(db, [seeded["owner"], seeded["second"],
                                       seeded["owner"], None, "not-an-objectid"])
    assert set(lookup) == {seeded["owner"], seeded["second"]}
    assert lookup[seeded["owner"]]["full_name"] == "Sarah Jenkins"
    assert lookup[seeded["owner"]]["is_owner"] is True
    assert lookup[seeded["second"]]["is_owner"] is False


async def test_attach_consultants_expands_both_shapes(db, seeded):
    items = [
        {"id": "1", "consultant_id": seeded["owner"]},
        {"id": "2", "consultant_ids": [seeded["owner"], seeded["second"]]},
        {"id": "3"},
    ]
    out = await attach_consultants(db, items)
    assert out[0]["consultant"]["full_name"] == "Sarah Jenkins"
    assert len(out[1]["consultants"]) == 2
    assert "consultant" not in out[2]


async def test_list_responses_carry_the_consultant_object(db, seeded):
    await db.cases.insert_many([
        {"reference": "CAS-089", "consultant_id": seeded["owner"]},
        {"reference": "CAS-104", "consultant_id": seeded["second"]},
    ])
    page = await paginate(db, "cases", {}, PageParams(page=1, page_size=10))
    assert page["total"] == 2
    for item in page["items"]:
        assert item["consultant_id"]
        assert item["consultant"]["full_name"] in {"Sarah Jenkins", "James Okoro"}
        assert item["consultant"]["title"]


async def test_enrichment_is_skipped_on_the_platform_database():
    """Platform DB has no tenant users collection - must not try to enrich."""
    pdb = AsyncMongoMockClient()["webimove_platform"]
    await pdb.tenants.insert_one({"name": "Jenkins Immigration Law",
                                  "consultant_id": "should-not-be-resolved"})
    page = await paginate(pdb, "tenants", {}, PageParams(page=1, page_size=10))
    assert page["items"][0]["consultant_id"] == "should-not-be-resolved"
    assert "consultant" not in page["items"][0]
