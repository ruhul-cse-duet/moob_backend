"""Each organization decides what work it does.

The review's fourth and fifth findings are one objection said twice: the
platform was immigration, hard-coded. Nine visa types shipped in the source, the
word "immigration" in every menu, and no screen anywhere to add a tenth type -
which makes the product usable by exactly one kind of customer, and WebImove's
customers are consultancies that also do labour, civil and tax work.

So there are two levels now, both owned by the tenant: process areas, and the
procedures inside them. Two properties matter more than the CRUD:

  * one tenant's catalogue is invisible to the next - they are separate
    databases, which is what makes that true rather than intended;
  * editing a procedure does not reach backwards into cases already running on
    it, because a case copies what it needs when it opens.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.modules.catalog import service

OWNER_ID = ObjectId()


class _User:
    def __init__(self, id=None, role=Role.CONSULTANT_OWNER):
        self.id = id or str(OWNER_ID)
        self.role = role
        self.raw = {"full_name": "Sarah Jenkins"}


class _AreaIn:
    def __init__(self, key, name, **kwargs):
        self.key = key
        self.name = name
        self.description = kwargs.get("description")
        self.icon = kwargs.get("icon")
        self.active = kwargs.get("active", True)

    def model_dump(self):
        return {"key": self.key, "name": self.name,
                "description": self.description, "icon": self.icon,
                "active": self.active}


class _AreaUpdate:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name")
        self.description = kwargs.get("description")
        self.icon = kwargs.get("icon")
        self.active = kwargs.get("active")

    def model_dump(self):
        return {"name": self.name, "description": self.description,
                "icon": self.icon, "active": self.active}


class _ProcedureIn:
    def __init__(self, area_key, name, **kwargs):
        self.area_key = area_key
        self.name = name
        self.description = kwargs.get("description")
        self.required_documents = kwargs.get("required_documents", [])
        self.client_fields = kwargs.get("client_fields", [])
        self.workflow_stages = kwargs.get("workflow_stages", [])
        self.default_deadline_days = kwargs.get("default_deadline_days")
        self.active = kwargs.get("active", True)

    def model_dump(self, **kwargs):
        return {"area_key": self.area_key, "name": self.name,
                "description": self.description,
                "required_documents": self.required_documents,
                "client_fields": self.client_fields,
                "workflow_stages": self.workflow_stages,
                "default_deadline_days": self.default_deadline_days,
                "active": self.active}


class _ProcedureUpdate:
    def __init__(self, **kwargs):
        self._given = kwargs

    def model_dump(self, exclude_unset=False, **kwargs):
        return dict(self._given)


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


class TestTheAreasAWorkspaceStartsWith:
    async def test_four_common_areas_are_seeded(self, db):
        areas = await service.list_areas(db)

        assert {a["key"] for a in areas} == {"immigration", "labour", "civil", "tax"}
        assert all(a["built_in"] for a in areas)

    async def test_seeding_twice_does_not_duplicate(self, db):
        await service.ensure_defaults(db)
        await service.ensure_defaults(db)

        assert await db.process_areas.count_documents({}) == 4

    async def test_a_tenant_can_add_its_own(self, db):
        await service.ensure_defaults(db)

        area = await service.create_area(
            db, _User(), _AreaIn("intellectual_property", "Intellectual property"))

        assert area["key"] == "intellectual_property"
        assert area["built_in"] is False

    async def test_two_areas_cannot_share_a_key(self, db):
        await service.ensure_defaults(db)

        with pytest.raises(Conflict):
            await service.create_area(db, _User(), _AreaIn("tax", "Taxation"))

    async def test_an_area_can_be_switched_off(self, db):
        await service.ensure_defaults(db)
        tax = await db.process_areas.find_one({"key": "tax"})

        await service.update_area(db, str(tax["_id"]), _AreaUpdate(active=False))

        assert "tax" not in {a["key"] for a in await service.list_areas(db)}
        # Still there, just not offered - procedures inside it keep working.
        assert "tax" in {a["key"] for a in
                         await service.list_areas(db, include_inactive=True)}

    async def test_a_built_in_area_can_be_renamed(self, db):
        # "Immigration" is a starting point, not the platform's opinion.
        await service.ensure_defaults(db)
        imm = await db.process_areas.find_one({"key": "immigration"})

        updated = await service.update_area(
            db, str(imm["_id"]), _AreaUpdate(name="Extranjería"))

        assert updated["name"] == "Extranjería"
        # The key does not move, so nothing filed under it is lost.
        assert updated["key"] == "immigration"


class TestProcedures:
    async def test_one_is_created_inside_an_area(self, db):
        await service.ensure_defaults(db)

        procedure = await service.create_procedure(
            db, _User(), _ProcedureIn("labour", "Dismissal claim"))

        assert procedure["area_key"] == "labour"
        assert procedure["area_name"] == "Labour"

    async def test_an_unknown_area_is_refused_with_a_way_forward(self, db):
        await service.ensure_defaults(db)

        with pytest.raises(BadRequest, match="Create the area first"):
            await service.create_procedure(
                db, _User(), _ProcedureIn("maritime", "Salvage claim"))

    async def test_two_procedures_in_one_area_cannot_share_a_name(self, db):
        await service.ensure_defaults(db)
        await service.create_procedure(
            db, _User(), _ProcedureIn("tax", "Income tax return"))

        with pytest.raises(Conflict):
            await service.create_procedure(
                db, _User(), _ProcedureIn("tax", "Income tax return"))

    async def test_the_same_name_in_a_different_area_is_fine(self, db):
        await service.ensure_defaults(db)
        await service.create_procedure(db, _User(), _ProcedureIn("civil", "Appeal"))

        await service.create_procedure(db, _User(), _ProcedureIn("tax", "Appeal"))

        assert await db.procedures.count_documents({}) == 2

    async def test_duplicating_names_the_copy(self, db):
        await service.ensure_defaults(db)
        original = await service.create_procedure(
            db, _User(), _ProcedureIn("labour", "Dismissal claim"))

        copy = await service.duplicate_procedure(db, _User(), original["id"])

        assert copy["name"] == "Dismissal claim (copy)"
        assert copy["id"] != original["id"]

    async def test_a_copy_carries_the_checklist(self, db):
        await service.ensure_defaults(db)
        original = await service.create_procedure(
            db, _User(), _ProcedureIn(
                "labour", "Dismissal claim",
                required_documents=[{"name": "Termination letter",
                                     "mandatory": True}]))

        copy = await service.duplicate_procedure(db, _User(), original["id"])

        assert copy["required_documents"][0]["name"] == "Termination letter"

    async def test_retiring_one_keeps_it_readable(self, db):
        """Deleting would orphan every case that names it."""
        await service.ensure_defaults(db)
        procedure = await service.create_procedure(
            db, _User(), _ProcedureIn("civil", "Divorce"))

        await service.deactivate_procedure(db, procedure["id"])

        assert await service.list_procedures(db) == []
        assert await service.get_procedure(db, procedure["id"])
        assert len(await service.list_procedures(db, include_inactive=True)) == 1


class TestTemplates:
    def test_the_templates_span_more_than_immigration(self):
        areas = {t["area_key"] for t in service.list_templates()}

        assert areas == {"immigration", "labour", "civil", "tax"}

    def test_they_can_be_filtered_by_area(self):
        labour = service.list_templates("labour")

        assert labour and all(t["area_key"] == "labour" for t in labour)

    async def test_copying_one_produces_an_editable_procedure(self, db):
        await service.ensure_defaults(db)

        procedure = await service.create_from_template(db, _User(), "dismissal_claim")

        assert procedure["name"] == "Dismissal claim"
        assert procedure["from_template"] == "dismissal_claim"
        # Everything the template carried, now the tenant's to change.
        assert any(d["name"] == "Termination letter"
                   for d in procedure["required_documents"])
        assert any(f["key"] == "employer_name" for f in procedure["client_fields"])

    async def test_a_copy_can_be_renamed_on_the_way_in(self, db):
        await service.ensure_defaults(db)

        procedure = await service.create_from_template(
            db, _User(), "student_visa", name="Visado de estudiante")

        assert procedure["name"] == "Visado de estudiante"

    async def test_an_unknown_template_is_refused(self, db):
        with pytest.raises(NotFound):
            await service.create_from_template(db, _User(), "no_such_template")


class TestEditsDoNotReachOpenCases:
    """The one property the spec calls out by name.

    "Changes to a procedure do not break cases already open." A case copies the
    checklist it was opened with, so a procedure edited in March does not
    silently rewrite what a case opened in January was assessed against.
    """

    async def test_a_case_copies_the_checklist_rather_than_pointing_at_it(self, db):
        await service.ensure_defaults(db)
        procedure = await service.create_procedure(
            db, _User(), _ProcedureIn(
                "immigration", "Student visa",
                required_documents=[{"name": "Passport", "mandatory": True}]))

        snapshot = await service.snapshot_for_case(db, procedure["id"])

        # Now the catalogue changes underneath it.
        await service.update_procedure(
            db, procedure["id"],
            _ProcedureUpdate(required_documents=[
                {"name": "Passport", "mandatory": True},
                {"name": "Biometrics appointment", "mandatory": True},
            ]))

        assert [d["name"] for d in snapshot["required_documents"]] == ["Passport"]
        # And the procedure itself did move, for the next case opened on it.
        current = await service.get_procedure(db, procedure["id"])
        assert len(current["required_documents"]) == 2

    async def test_the_snapshot_records_where_it_came_from(self, db):
        await service.ensure_defaults(db)
        procedure = await service.create_procedure(
            db, _User(), _ProcedureIn("tax", "Income tax return"))

        snapshot = await service.snapshot_for_case(db, procedure["id"])

        assert snapshot["procedure_id"] == procedure["id"]
        assert snapshot["procedure_name"] == "Income tax return"
        assert snapshot["process_area"] == "tax"


async def test_one_tenants_catalogue_is_not_another_tenants():
    """Separate databases, which is what makes this structural rather than a
    filter somebody has to remember to apply."""
    client = AsyncMongoMockClient()
    firm_a = client["webimove_tenant_aaa"]
    firm_b = client["webimove_tenant_bbb"]

    await service.ensure_defaults(firm_a)
    await service.ensure_defaults(firm_b)
    await service.create_procedure(
        firm_a, _User(), _ProcedureIn("immigration", "Golden visa"))

    assert [p["name"] for p in await service.list_procedures(firm_a)] == ["Golden visa"]
    assert await service.list_procedures(firm_b) == []


class TestAreaKeyFromName:
    """The form asks for a name; the key is ours to derive."""

    def test_the_key_is_derived(self):
        from app.modules.catalog.schemas import ProcessAreaIn

        assert ProcessAreaIn(name="Intellectual property").key == "intellectual_property"
        assert ProcessAreaIn(name="Extranjería").key == "extranjeria"

    def test_a_given_key_wins(self):
        from app.modules.catalog.schemas import ProcessAreaIn

        assert ProcessAreaIn(name="Labour law", key="labour").key == "labour"

    def test_a_name_with_nothing_to_key_on_is_refused(self):
        from pydantic import ValidationError
        from app.modules.catalog.schemas import ProcessAreaIn

        with pytest.raises(ValidationError):
            ProcessAreaIn(name="!!")


class TestSeedingUnderConcurrency:
    """Eight areas, each twice, on a real workspace.

    The catalogue page loads areas and procedures at the same moment - and
    React's development mode fetches everything twice - so seeding is always
    asked for concurrently. Read-then-insert saw no areas in both requests and
    inserted four in each. The unique index that should have stopped it is only
    created at provisioning, and that workspace predated the catalogue.
    """

    @pytest.fixture(autouse=True)
    def fresh_cache(self):
        service._ENSURED.clear()
        yield
        service._ENSURED.clear()

    async def test_concurrent_first_reads_seed_four_not_eight(self, db):
        import asyncio

        await asyncio.gather(*(service.list_areas(db) for _ in range(5)))

        assert await db.process_areas.count_documents({}) == 4

    async def test_duplicates_already_written_are_cleared(self, db):
        """The workspace that already has eight has to come back to four."""
        from app.core.utils import utcnow

        for _ in range(2):
            await db.process_areas.insert_many([
                {"key": k, "name": k.title(), "active": True, "built_in": True,
                 "created_at": utcnow()}
                for k in ("immigration", "labour", "civil", "tax")
            ])
        assert await db.process_areas.count_documents({}) == 8

        areas = await service.list_areas(db)

        assert len(areas) == 4
        assert await db.process_areas.count_documents({}) == 4

    async def test_the_survivor_keeps_what_the_tenant_changed(self, db):
        # Oldest first: a rename on the original row must not be thrown away in
        # favour of the untouched copy the race added after it.
        await db.process_areas.insert_one(
            {"key": "immigration", "name": "Extranjería", "active": True})
        await db.process_areas.insert_one(
            {"key": "immigration", "name": "Immigration", "active": True})

        await service.ensure_defaults(db)

        row = await db.process_areas.find_one({"key": "immigration"})
        assert row["name"] == "Extranjería"
