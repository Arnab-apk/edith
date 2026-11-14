from types import SimpleNamespace

from bot import security


def _upd(uid, chat_type):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid),
                           effective_chat=SimpleNamespace(type=chat_type))


def test_owner_private_chat_allowed():
    assert security.is_allowed(_upd(111111111, "private"))


def test_owner_in_group_refused():
    # O-1: owner's commands in a group would leak screenshots/files to all members
    for t in ("group", "supergroup", "channel"):
        assert not security.is_allowed(_upd(111111111, t))


def test_stranger_refused():
    assert not security.is_allowed(_upd(42, "private"))
