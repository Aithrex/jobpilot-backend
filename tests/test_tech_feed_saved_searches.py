"""A saved search survives a new device, and a lost server.

Two properties, and they pull in opposite directions:

  * the SERVER owns the list, so clearing site data does not lose it;
  * the CLIENT keeps a mirror, so an unreachable API does not either.

The reconcile contract is what reconciles them, and its asymmetry is the part
worth testing: it pushes UP and never deletes, because a search the client
does not have is one saved on another device.
"""
from __future__ import annotations

import pytest

from _vg_support import current_db
from services import tech_feed_saved_searches as tfss

USER = "seeker@example.com"
OTHER = "someone.else@example.com"


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[tfss.SAVED_SEARCHES].delete_many({})
    yield
    db[tfss.SAVED_SEARCHES].delete_many({})


def _filters(**over):
    base = {"query": "kubernetes", "role_families": ["cloud_platform"],
            "tech_scope": ["core"], "employer_scope": "tech_only"}
    base.update(over)
    return base


class TestRoundTrip:

    def test_a_saved_search_comes_back(self):
        tfss.save(USER, client_id="tfs_abc123", name="Remote SRE",
                  filters=_filters())
        out = tfss.list_for(USER)
        assert len(out["searches"]) == 1
        row = out["searches"][0]
        assert row["id"] == "tfs_abc123"
        assert row["name"] == "Remote SRE"
        assert row["filters"]["query"] == "kubernetes"

    def test_saving_the_same_client_id_twice_is_one_row(self):
        """An offline queue draining, or a retried request."""
        tfss.save(USER, client_id="tfs_abc123", name="First", filters=_filters())
        tfss.save(USER, client_id="tfs_abc123", name="Renamed", filters=_filters())
        out = tfss.list_for(USER)
        assert len(out["searches"]) == 1
        assert out["searches"][0]["name"] == "Renamed"

    def test_a_search_with_no_client_id_keys_on_its_own_filters(self):
        tfss.save(USER, client_id="", name="A", filters=_filters())
        tfss.save(USER, client_id=None, name="B", filters=_filters())
        assert len(tfss.list_for(USER)["searches"]) == 1

    def test_delete_removes_only_that_search(self):
        tfss.save(USER, client_id="a", name="A", filters=_filters())
        tfss.save(USER, client_id="b", name="B", filters=_filters(query="rust"))
        tfss.delete(USER, "a")
        assert [s["id"] for s in tfss.list_for(USER)["searches"]] == ["b"]


class TestIsolation:

    def test_one_account_never_sees_another(self):
        tfss.save(USER, client_id="a", name="Mine", filters=_filters())
        tfss.save(OTHER, client_id="a", name="Theirs", filters=_filters())
        assert [s["name"] for s in tfss.list_for(USER)["searches"]] == ["Mine"]
        assert [s["name"] for s in tfss.list_for(OTHER)["searches"]] == ["Theirs"]

    def test_deleting_someone_elses_id_does_nothing(self):
        tfss.save(OTHER, client_id="a", name="Theirs", filters=_filters())
        tfss.delete(USER, "a")
        assert len(tfss.list_for(OTHER)["searches"]) == 1


class TestTheBoundary:

    def test_an_unrecognised_filter_key_never_reaches_storage(self):
        """`normalize_request` on the WRITE, so the collection cannot grow a
        schema through a client that invented a field."""
        tfss.save(USER, client_id="a", name="A",
                  filters=_filters(wibble="nonsense", drop_tables=True))
        stored = tfss.list_for(USER)["searches"][0]["filters"]
        assert "wibble" not in stored
        assert "drop_tables" not in stored
        assert stored["query"] == "kubernetes"

    def test_volatile_request_fields_are_not_stored(self):
        """A cursor and a page size describe a REQUEST, not a search, and
        replaying one months later is meaningless."""
        tfss.save(USER, client_id="a", name="A",
                  filters=_filters(cursor="abc", page_size=50, user_email="x@y.z"))
        stored = tfss.list_for(USER)["searches"][0]["filters"]
        for key in ("cursor", "page_size", "user_email"):
            assert key not in stored

    def test_a_hostile_client_id_is_constrained_not_trusted(self):
        tfss.save(USER, client_id="../../etc/passwd", name="A", filters=_filters())
        got = tfss.list_for(USER)["searches"][0]["id"]
        assert got == "etcpasswd"

    def test_the_per_account_cap_is_enforced(self):
        for i in range(tfss.MAX_PER_USER):
            tfss.save(USER, client_id=f"c{i}", name=f"S{i}",
                      filters=_filters(query=f"q{i}"))
        with pytest.raises(ValueError):
            tfss.save(USER, client_id="one-too-many", name="X", filters=_filters())
        # An UPDATE to an existing row still works at the cap - the cap is on
        # how many you may keep, not on whether you may rename them.
        tfss.save(USER, client_id="c0", name="Renamed", filters=_filters())
        assert any(s["name"] == "Renamed" for s in tfss.list_for(USER)["searches"])


class TestReconcile:

    def test_local_only_searches_are_pushed_up(self):
        out = tfss.reconcile(USER, [
            {"id": "local1", "name": "Saved offline", "filters": _filters()},
            {"id": "local2", "name": "Also offline", "filters": _filters(query="go")},
        ])
        assert out["pushed_from_client"] == 2
        assert {s["id"] for s in out["searches"]} == {"local1", "local2"}

    def test_reconcile_never_deletes_a_search_the_client_lacks(self):
        """It is on another device, not removed."""
        tfss.save(USER, client_id="from_laptop", name="Laptop", filters=_filters())
        out = tfss.reconcile(USER, [
            {"id": "from_phone", "name": "Phone", "filters": _filters()}])
        assert {s["id"] for s in out["searches"]} == {"from_laptop", "from_phone"}

    def test_reconcile_does_not_overwrite_the_server_copy(self):
        tfss.save(USER, client_id="shared", name="Server name", filters=_filters())
        out = tfss.reconcile(USER, [
            {"id": "shared", "name": "Stale local name", "filters": _filters()}])
        assert out["searches"][0]["name"] == "Server name"
        assert out["pushed_from_client"] == 0

    def test_reconcile_survives_rubbish(self):
        out = tfss.reconcile(USER, ["not a dict", None, 42, {"id": "ok",
                                                             "name": "Fine",
                                                             "filters": _filters()}])
        assert out["pushed_from_client"] == 1

    def test_reconcile_with_no_account_returns_nothing(self):
        assert tfss.reconcile("", [{"id": "a", "name": "A",
                                    "filters": _filters()}])["searches"] == []
