"""
The sender must not be told about their own message.

The bug the client reported as duplicated chat messages: `publish_message`
broadcasts to the thread room, and the sender is in that room. So a sent message
arrived twice - once as the REST response the app renders immediately, once over
the socket - and the app appended both.

The fix has to hold two things at once. The sender's *sending* socket is
skipped, and everybody else in the thread still receives it - silencing the room
would trade a duplicate for a chat that does not update.
"""
import pytest

from app.services import realtime


class FakeManager:
    """Stands in for the Socket.IO room manager."""

    def __init__(self, rooms):
        self._rooms = rooms

    def get_participants(self, namespace, room):
        for sid in self._rooms.get(room, []):
            yield sid, sid


@pytest.fixture
def sio(monkeypatch):
    """A recording Socket.IO server with two people in one thread."""
    emitted = []
    # Ayesha has the thread open on a phone and a laptop; Sarah on one device.
    sessions = {
        "sid-ayesha-phone": {"id": "user-ayesha"},
        "sid-ayesha-laptop": {"id": "user-ayesha"},
        "sid-sarah": {"id": "user-sarah"},
    }
    rooms = {"thread:t1": list(sessions), "user:user-sarah": ["sid-sarah"]}

    async def fake_emit(event, data, room=None, skip_sid=None, **kwargs):
        emitted.append({"event": event, "room": room, "skip_sid": skip_sid})

    async def fake_get_session(sid):
        return sessions.get(sid)

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    monkeypatch.setattr(realtime.sio, "get_session", fake_get_session)
    monkeypatch.setattr(realtime.sio, "manager", FakeManager(rooms))
    return emitted


MESSAGE = {"id": "m1", "thread_id": "t1", "sender_id": "user-ayesha", "body": "hi"}


def thread_emit(emitted):
    return next(e for e in emitted if e["event"] == "message")


@pytest.mark.asyncio
async def test_an_explicit_socket_id_is_the_only_one_skipped(sio):
    """The precise form. The sending device is silenced; the same person's other
    device still lights up, because they have no REST response to render."""
    await realtime.publish_message(MESSAGE, ["user-sarah"],
                                   skip_sid="sid-ayesha-phone",
                                   sender_id="user-ayesha")

    assert thread_emit(sio)["skip_sid"] == ["sid-ayesha-phone"]


@pytest.mark.asyncio
async def test_without_a_socket_id_every_socket_of_the_senders_is_skipped(sio):
    """The fallback, for an app that has not been updated to send its socket id.
    Blunter - the sender's second device waits for a reopen - but it fixes the
    duplicate with no client change at all."""
    await realtime.publish_message(MESSAGE, ["user-sarah"], sender_id="user-ayesha")

    skipped = thread_emit(sio)["skip_sid"]
    assert set(skipped) == {"sid-ayesha-phone", "sid-ayesha-laptop"}
    assert "sid-sarah" not in skipped


@pytest.mark.asyncio
async def test_the_other_participants_still_receive_it(sio):
    """The half that must not break: silencing the room would trade a duplicate
    for a chat that never updates."""
    await realtime.publish_message(MESSAGE, ["user-sarah"], sender_id="user-ayesha")

    emit = thread_emit(sio)
    assert emit["room"] == "thread:t1"
    # And the badge for the person not looking at the thread.
    assert any(e["event"] == "message_notice" and e["room"] == "user:user-sarah"
               for e in sio)


@pytest.mark.asyncio
async def test_nothing_is_skipped_when_the_sender_is_unknown(sio):
    """A caller that passes neither still delivers - `skip_sid=None` broadcasts
    to the whole room rather than to nobody."""
    await realtime.publish_message(MESSAGE, ["user-sarah"])

    assert thread_emit(sio)["skip_sid"] is None


@pytest.mark.asyncio
async def test_a_broken_room_manager_still_delivers_the_message(sio, monkeypatch):
    """Best effort, in the direction that matters.

    Working out who to skip reaches into Socket.IO's internals. If that ever
    changes shape the cost must be a duplicate message, never a message nobody
    receives.
    """
    class Exploding:
        def get_participants(self, *a, **k):
            raise RuntimeError("room manager changed shape")

    monkeypatch.setattr(realtime.sio, "manager", Exploding())

    await realtime.publish_message(MESSAGE, ["user-sarah"], sender_id="user-ayesha")

    emit = thread_emit(sio)
    assert emit["room"] == "thread:t1"
    assert emit["skip_sid"] is None


@pytest.mark.asyncio
async def test_a_socket_failure_never_fails_the_send(sio, monkeypatch):
    """The message is already stored by the time this runs."""
    async def exploding_emit(*a, **k):
        raise RuntimeError("socket is down")

    monkeypatch.setattr(realtime.sio, "emit", exploding_emit)

    await realtime.publish_message(MESSAGE, ["user-sarah"], sender_id="user-ayesha")
