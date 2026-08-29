"""
Notifications, in the reader's language.

A notification is written once and read later - sometimes months later, by
several people who do not share a language, and by someone who may have changed
theirs since. Storing a finished sentence freezes the writer's language into the
record, which is why the store holds a key and its parameters instead and the
words are chosen at read time.

Push is the exception that shapes the design: it has no read time to defer to,
so it is rendered per recipient before sending. A consultant reading English and
their client reading Spanish must get the same notification in different words
from one `notify()` call.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import NotificationType
from app.services import events

ES_USER = ObjectId()
EN_USER = ObjectId()
PT_USER = ObjectId()


@pytest.fixture
def db(monkeypatch):
    """Three accounts, three languages, and push captured rather than sent."""
    database = AsyncMongoMockClient()["webimove_tenant_test"]
    sent = []
    monkeypatch.setattr(events.push, "dispatch",
                        lambda uids, **kw: sent.append({"users": list(uids), **kw}))
    database.pushes = sent
    return database


async def seed_users(db):
    await db.users.insert_many([
        {"_id": ES_USER, "language": "es"},
        {"_id": EN_USER, "language": "en"},
        {"_id": PT_USER, "language": "pt"},
    ])


class TestRendering:
    def test_a_key_is_worded_in_the_language_asked_for(self):
        record = {"title_key": "notify.document_approved",
                  "params": {"document": "passport.pdf"}}

        assert events.render(record, "en")["title"] == "passport.pdf approved"
        assert events.render(record, "es")["title"] == "passport.pdf aprobado"
        assert events.render(record, "pt")["title"] == "passport.pdf aprovado"

    def test_a_parameter_that_is_itself_a_key_is_translated_too(self):
        """The *kind* of a data request is something we say, not something the
        user typed. Translating it where the notification is written would bake
        the writer's language into a record somebody else reads."""
        record = {"title_key": "notify.data_request_status",
                  "param_keys": {"kind": "data_request.export",
                                 "status": "data_request_status.ready"}}

        assert events.render(record, "en")["title"] == "Your export request is ready"
        assert "exportación" in events.render(record, "es")["title"]

    def test_plurals_follow_the_target_language(self):
        record = {"title_key": "notify.case_tasks_added",
                  "params": {"count": 1, "reference": "CAS-089"}}
        assert events.render(record, "pt")["title"] == "1 nova tarefa em CAS-089"

        record["params"]["count"] = 3
        assert events.render(record, "pt")["title"] == "3 novas tarefas em CAS-089"

    def test_words_a_person_wrote_are_left_alone(self):
        """An announcement or a task title is already in the language its author
        chose. Nothing here should touch it."""
        record = {"title": "Sistema em manutenção", "body": "Até às 14h"}

        assert events.render(record, "en") == {"title": "Sistema em manutenção",
                                               "body": "Até às 14h"}

    def test_a_record_written_before_keys_existed_still_reads(self):
        assert events.render({"title": "Old notification"}, "es")["title"] == \
            "Old notification"


class TestStoring:
    @pytest.mark.asyncio
    async def test_the_record_holds_a_key_not_a_sentence(self, db):
        await seed_users(db)
        await events.notify(
            db, user_ids=[str(ES_USER)], type=NotificationType.DOCUMENT_APPROVED,
            title_key="notify.document_approved", params={"document": "p.pdf"})

        row = await db.notifications.find_one({})
        assert row["title_key"] == "notify.document_approved"
        assert row["params"] == {"document": "p.pdf"}
        # No frozen wording: that is the whole point.
        assert "title" not in row

    @pytest.mark.asyncio
    async def test_authored_text_is_stored_as_text(self, db):
        await seed_users(db)
        await events.notify(
            db, user_ids=[str(ES_USER)], type=NotificationType.ANNOUNCEMENT,
            title="Manutenção programada", body="Das 13h às 14h")

        row = await db.notifications.find_one({})
        assert row["title"] == "Manutenção programada"
        assert "title_key" not in row


class TestPush:
    @pytest.mark.asyncio
    async def test_each_recipient_is_pushed_in_their_own_language(self, db):
        """One notify() call, three languages. Push has no read time to defer
        to, so this is the only chance to get it right."""
        await seed_users(db)

        await events.notify(
            db, user_ids=[str(ES_USER), str(EN_USER), str(PT_USER)],
            type=NotificationType.DOCUMENT_APPROVED,
            title_key="notify.document_approved", params={"document": "p.pdf"})

        titles = {p["title"] for p in db.pushes}
        assert titles == {"p.pdf approved", "p.pdf aprobado", "p.pdf aprovado"}

    @pytest.mark.asyncio
    async def test_recipients_are_grouped_not_pushed_one_by_one(self, db):
        """A hundred clients on one announcement is at most three renderings and
        three push calls, not a hundred."""
        await seed_users(db)
        more = [ObjectId() for _ in range(5)]
        await db.users.insert_many([{"_id": m, "language": "es"} for m in more])

        await events.notify(
            db, user_ids=[str(ES_USER), str(EN_USER)] + [str(m) for m in more],
            type=NotificationType.DOCUMENT_APPROVED,
            title_key="notify.document_approved", params={"document": "p.pdf"})

        assert len(db.pushes) == 2
        spanish = next(p for p in db.pushes if p["title"] == "p.pdf aprobado")
        assert len(spanish["users"]) == 6

    @pytest.mark.asyncio
    async def test_untranslatable_wording_is_one_group(self, db):
        """Nothing to group by when the words are the author's, so the delivery
        legs run once rather than once per language."""
        await seed_users(db)

        await events.notify(
            db, user_ids=[str(ES_USER), str(EN_USER), str(PT_USER)],
            type=NotificationType.ANNOUNCEMENT, title="Manutenção")

        assert len(db.pushes) == 1
        assert len(db.pushes[0]["users"]) == 3

    @pytest.mark.asyncio
    async def test_an_account_with_no_language_gets_english(self, db):
        await db.users.insert_one({"_id": EN_USER})

        await events.notify(
            db, user_ids=[str(EN_USER)], type=NotificationType.DOCUMENT_APPROVED,
            title_key="notify.document_approved", params={"document": "p.pdf"})

        assert db.pushes[0]["title"] == "p.pdf approved"

    @pytest.mark.asyncio
    async def test_a_failed_language_lookup_still_delivers(self, db, monkeypatch):
        """Best effort in the direction that matters: not knowing someone's
        language costs them English, never a notification that never arrives."""
        class Unreadable:
            def find(self, *a, **k):
                raise RuntimeError("users collection unavailable")

        original = type(db).__getitem__
        monkeypatch.setattr(type(db), "__getitem__",
                            lambda self, name: Unreadable() if name == "users"
                            else original(self, name))

        await events.notify(
            db, user_ids=[str(ES_USER)], type=NotificationType.DOCUMENT_APPROVED,
            title_key="notify.document_approved", params={"document": "p.pdf"})

        # Delivered, in the fallback language rather than not at all.
        assert db.pushes[0]["title"] == "p.pdf approved"


class TestCatalogueCoverage:
    def test_every_notification_key_used_in_the_code_exists(self):
        """A key with no entry renders as `notify.whatever` on someone's phone."""
        import pathlib
        import re

        from app.core.i18n import CATALOGUE

        used = set()
        for path in pathlib.Path("app/modules").rglob("*.py"):
            for match in re.finditer(r'title_key="(notify\.[a-z_.]+)"',
                                     path.read_text(encoding="utf-8")):
                used.add(match.group(1))

        assert used, "no notification keys found - did the call sites move?"
        assert used <= set(CATALOGUE), sorted(used - set(CATALOGUE))
