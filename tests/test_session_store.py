import time
from session_store import SessionStore


def test_create_and_get(tmp_path):
    s = SessionStore(path=tmp_path / "s.json")
    s.create("thread1", "uuid-aaa")
    assert s.get("thread1") == "uuid-aaa"


def test_get_missing_returns_none(tmp_path):
    s = SessionStore(path=tmp_path / "s.json")
    assert s.get("nope") is None


def test_persists_across_instances(tmp_path):
    p = tmp_path / "s.json"
    SessionStore(path=p).create("t", "uuid-1")
    assert SessionStore(path=p).get("t") == "uuid-1"


def test_expired_entry_returns_none(tmp_path):
    p = tmp_path / "s.json"
    s = SessionStore(path=p)
    s.create("t", "uuid-1")
    # force-expire by rewriting the created timestamp into the past
    s._data["t"]["created"] = time.time() - (49 * 3600)
    s._save()
    assert SessionStore(path=p).get("t") is None


def test_cleanup_removes_expired(tmp_path):
    p = tmp_path / "s.json"
    s = SessionStore(path=p)
    s.create("old", "u1")
    s.create("new", "u2")
    s._data["old"]["created"] = time.time() - (49 * 3600)
    s._save()
    s.cleanup()
    assert s.get("old") is None
    assert s.get("new") == "u2"
