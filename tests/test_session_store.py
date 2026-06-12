import json
import time
from session_store import SessionStore


def test_create_and_get(tmp_path):
    s = SessionStore(path=tmp_path / "s.json")
    s.create("thread1", "uuid-aaa")
    assert s.get("thread1") == "uuid-aaa"


def test_save_is_atomic_leaves_no_temp_and_valid_json(tmp_path):
    p = tmp_path / "s.json"
    s = SessionStore(path=p)
    s.create("t", "uuid-1")
    # The real file must be present and parse as complete JSON...
    assert json.loads(p.read_text())["t"]["session_id"] == "uuid-1"
    # ...and no leftover temp/partial files in the directory.
    assert [f.name for f in tmp_path.iterdir()] == [p.name]


def test_preexisting_temp_file_does_not_corrupt_load(tmp_path):
    # A stale/partial temp file from a previous crash must not be read as the store.
    p = tmp_path / "s.json"
    SessionStore(path=p).create("good", "uuid-good")
    (tmp_path / (p.name + ".tmp")).write_text("{ this is not valid json")
    # A fresh load reads the real file, ignoring the temp.
    assert SessionStore(path=p).get("good") == "uuid-good"


def test_failed_write_does_not_truncate_existing_file(tmp_path, monkeypatch):
    # A crash mid-write must NOT destroy the already-persisted data.
    # Atomic save (temp + os.replace) leaves the real file intact when the
    # write fails; a direct write_text would truncate it.
    p = tmp_path / "s.json"
    s = SessionStore(path=p)
    s.create("keep", "uuid-keep")

    import session_store as ss
    orig_replace = ss.os.replace

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure during commit")

    # Fail the atomic commit step; the original file must survive untouched.
    monkeypatch.setattr(ss.os, "replace", boom)
    try:
        s.create("new", "uuid-new")
    except OSError:
        pass
    monkeypatch.setattr(ss.os, "replace", orig_replace)

    # Reload from disk: the pre-existing entry must still be readable.
    assert SessionStore(path=p).get("keep") == "uuid-keep"


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


def test_cleanup_returns_expired_keys(tmp_path):
    # cleanup reports which threads it evicted so callers can drop matching
    # in-memory state (e.g. bot._room_states).
    p = tmp_path / "s.json"
    s = SessionStore(path=p)
    s.create("old", "u1")
    s.create("new", "u2")
    s._data["old"]["created"] = time.time() - (49 * 3600)
    s._save()
    expired = s.cleanup()
    assert expired == ["old"]


def test_cleanup_returns_empty_when_nothing_expired(tmp_path):
    s = SessionStore(path=tmp_path / "s.json")
    s.create("fresh", "u1")
    assert s.cleanup() == []
