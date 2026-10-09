"""A team's archive opt-out (archive.md, "Opting out"): the gate on the call path, the erasure of
what the team stored, and the settings that record it.

Runs in the sqlite suite and in CI's serial Postgres job like test_archive.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from conftest import verified_signup
from treg import archive, audit, bootstrap
from treg.application import archive_erasure
from treg.application.call import service as call_service
from treg.infra.db import session_maker
from treg.models import ArchiveEndpointStat, ArchiveKey, ArchiveKeyOrg, ArchiveSnapshot, Org
from tests.fake_object_store import MemoryObjectStore
from tests.test_archive import (  # noqa: F401 - fixtures
    EP, OWN, PLAT, _own_key, _rows, _spend_entries, _vendor_says, own_key_serve, platform_on, serve,
)


async def _org_id(clients, headers=None) -> int:
    return (await clients.get("/orgs", headers=headers or {})).json()[0]["org_id"]


async def _settings(clients, org_id, headers=None) -> dict:
    r = await clients.get(f"/orgs/{org_id}/settings", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _opt_out(clients, org_id, headers=None) -> dict:
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _marks():
    async with session_maker() as s:
        return (await s.execute(select(ArchiveKeyOrg))).scalars().all()


@pytest.fixture
def no_grace(monkeypatch):
    """Completion normally waits out a call's lifetime (archive_erasure.grace_s); the erasure
    tests are about what is erased, so they let the sweep declare itself done at once."""
    monkeypatch.setattr(archive_erasure, "grace_s", lambda: 0)


async def _record_own_answer(org_id: int, body: bytes, question: str = "aweme_id=7") -> None:
    """What a late recorder does: an own-credential answer for `org_id`, keyed to the org."""
    await archive._store(
        method="GET", endpoint_id=EP, provider="tikhub",
        url=f"https://api.tikhub.io/x?{question}", caller_body=b"", headers={},
        status_code=200, media_type="application/json", body=body,
        origin_org_id=org_id, scope=f"org:{org_id}")


# ---------------------------------------------------------------------------------------------
# The gate: an opted-out team is never answered from the archive and never recorded into it

async def test_an_opted_out_team_is_neither_served_nor_recorded_on_treg_key(
        clients: AsyncClient, serve, monkeypatch):
    org_id = await _org_id(clients)
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1 and len(await _marks()) == 1
    live = int(r1.headers["X-Treg-Cost-Micro"])

    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    cfg = await _opt_out(clients, org_id)
    assert cfg["archive"] is False and cfg["archive_erasure"] == "pending"
    # The public answer is on file and the question is a repeat for this team, yet it reaches
    # the vendor at full price: no hit, no repeat discount, no mark.
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.status_code == 200 and "x-treg-cache" not in r2.headers
    assert int(r2.headers["X-Treg-Cost-Micro"]) == live
    props = [p for e, p in events if e == "tool_called"][-1]
    assert props["cache_outcome"] == "org_opt_out"
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1                     # nothing new recorded
    # A fresh question: not recorded either, and no mark for the team.
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert [row.get("cached") for row in rows[:2]] == [False, False]
    # Back in: the next call is a hit again, at full price (the erasure took the team's mark).
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True})
    assert r.json()["archive"] is True and r.json()["archive_erasure"] is None
    r3 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r3.headers["X-Treg-Cache"] == "hit"


async def test_an_opted_out_teams_own_key_calls_skip_the_archive(
        clients: AsyncClient, own_key_serve, monkeypatch):
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    org_id = await _org_id(clients)
    await _opt_out(clients, org_id)
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200 and r1.content == OWN
    keys, snaps = await _rows()
    assert keys == [] and snaps == []                              # nothing recorded
    _vendor_says(monkeypatch, b'{"changed": true}')
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.status_code == 200 and r2.content == b'{"changed": true}'   # always live
    assert "x-treg-cache" not in r2.headers
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert not any(row.get("cached") for row in rows[:2])
    assert not any(row["has_result"] for row in rows[:2])


# ---------------------------------------------------------------------------------------------
# The erasure: what the team stored goes, what other teams stored stays

async def test_erasure_removes_the_teams_private_keys_and_marks_and_nothing_else(
        clients: AsyncClient, own_key_serve, monkeypatch, no_grace):
    # Team A: an own-key answer (org-scoped key) and a platform answer it paid for (public key).
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    await archive.drain()
    org_a = await _org_id(clients)
    # Team B: its own own-key answer, and a platform answer to A's question.
    other = await verified_signup(clients, json={"email": "other-team@example.com"})
    hb = {"X-Treg-Token": other.json()["token"]}
    _vendor_says(monkeypatch, PLAT)
    await clients.get(f"/call/{EP}?aweme_id=7&count=5", headers=hb)       # public key, B's mark
    await archive.drain()
    await _own_key(clients, hb)
    _vendor_says(monkeypatch, b'{"b": "own"}')
    await clients.get(f"/call/{EP}?aweme_id=9", headers=hb)             # B's org key
    await archive.drain()
    # A keeps its own key, so no metered call ever marks it; give A a mark by hand to prove the
    # sweep takes the team's marks along with its keys.
    async with session_maker() as s:
        s.add(ArchiveKeyOrg(org_id=org_a, key_hash="a" * 64))
        await s.commit()
    keys, snaps = await _rows()
    assert sorted(k.scope or "public" for k in keys) == ["org", "org", "public"]
    assert sorted(r.org_id == org_a for r in await _marks()) == [False, True]

    await _opt_out(clients, org_a)
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
    keys, snaps = await _rows()
    assert sorted(k.scope or "public" for k in keys) == ["org", "public"]
    assert all(s.origin_org_id != org_a for s in snaps)
    assert all(r.org_id != org_a for r in await _marks())
    assert (await _settings(clients, org_a))["archive_erasure"] == "done"
    # A second sweep finds nothing and changes nothing.
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 0
    # B is untouched: its own answer is still a hit.
    _vendor_says(monkeypatch, b'{"must": "not be asked"}')
    r = await clients.get(f"/call/{EP}?aweme_id=9", headers=hb)
    assert r.headers["X-Treg-Cache"] == "hit" and r.content == b'{"b": "own"}'
    # The endpoint's running totals went down with the rows, never below zero.
    async with session_maker() as s:
        stat = (await s.execute(select(ArchiveEndpointStat).where(
            ArchiveEndpointStat.endpoint_id == EP))).scalar_one()
    assert stat.keys == 2 and stat.snapshots == 2 and stat.bodies_kept == 2


async def test_erasure_deletes_orphaned_objects_but_keeps_shared_ones(
        clients: AsyncClient, own_key_serve, monkeypatch, no_grace):
    """Bodies are content-addressed: an object another team's snapshot still points at stays."""
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")                 # A: OWN bytes
        await clients.get(f"/call/{EP}?aweme_id=8")                 # A: OWN bytes again (same hash)
        await archive.drain()
        other = await verified_signup(clients, json={"email": "sharer@example.com"})
        hb = {"X-Treg-Token": other.json()["token"]}
        await _own_key(clients, hb)
        await clients.get(f"/call/{EP}?aweme_id=7", headers=hb)      # B: the SAME bytes, its own key
        _vendor_says(monkeypatch, b'{"only": "a"}')
        await clients.get(f"/call/{EP}?aweme_id=10")                # A: bytes nobody else has
        await archive.drain()
        import hashlib
        shared, only_a = hashlib.sha256(OWN).hexdigest(), hashlib.sha256(b'{"only": "a"}').hexdigest()
        assert shared in store.objects and only_a in store.objects
        org_a = await _org_id(clients)
        await _opt_out(clients, org_a)
        assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
        assert shared in store.objects and only_a not in store.objects
        assert store.delete_calls == 1
        async with session_maker() as s:
            left = (await s.execute(select(ArchiveSnapshot))).scalars().all()
        assert len(left) == 1 and left[0].origin_org_id != org_a
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


async def test_a_failed_object_delete_leaves_the_team_pending_for_the_next_sweep(
        clients: AsyncClient, own_key_serve, monkeypatch, no_grace):
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
        org_a = await _org_id(clients)
        await _opt_out(clients, org_a)
        store.fail_deletes = True
        assert await archive_erasure.sweep_once(session_factory=session_maker) == 0
        assert (await _settings(clients, org_a))["archive_erasure"] == "pending"
        keys, _ = await _rows()
        assert len(keys) == 1                                       # the rows wait for the retry
        store.fail_deletes = False
        assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
        assert store.objects == {}
        assert (await _settings(clients, org_a))["archive_erasure"] == "done"
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


async def test_deleting_a_team_erases_its_archive_too(clients: AsyncClient, own_key_serve, monkeypatch):
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
        org = (await clients.get("/orgs")).json()[0]
        assert store.objects
        # A second team to prove the owner's delete only takes the owner's rows.
        other = await verified_signup(clients, json={"email": "stays@example.com"})
        hb = {"X-Treg-Token": other.json()["token"]}
        await _own_key(clients, hb)
        _vendor_says(monkeypatch, PLAT)
        await clients.get(f"/call/{EP}?aweme_id=7", headers=hb)
        await archive.drain()
        r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
        assert r.status_code == 200, r.text
        keys, snaps = await _rows()
        assert len(keys) == 1 and len(snaps) == 1 and snaps[0].origin_org_id != org["org_id"]
        assert list(store.objects) == [snaps[0].content_hash]
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


# ---------------------------------------------------------------------------------------------
# The setting itself

async def test_opt_out_is_admin_only_and_records_the_moment(clients: AsyncClient):
    org_id = await _org_id(clients)
    cfg = await _settings(clients, org_id)
    assert cfg["archive"] is True and cfg["archive_erasure"] is None and cfg["archive_opt_out_at"] is None
    cfg = await _opt_out(clients, org_id)
    assert cfg["archive"] is False and cfg["archive_opt_out_at"] and cfg["archive_erasure"] == "pending"
    first = cfg["archive_opt_out_at"]
    # Opting out again does not move the record: the objection stands from its first moment.
    assert (await _opt_out(clients, org_id))["archive_opt_out_at"] == first
    async with session_maker() as s:
        row = await s.get(Org, org_id)
    assert row.archive_opt_out_at is not None and row.archive_purged_at is None
    # A member who is not an admin may read it but not change it.
    inv = await clients.post(f"/orgs/{org_id}/invites", json={"email": "plain@example.com", "role": "member"})
    accepted = await clients.post("/invites/accept", json={"code": inv.json()["code"], "email": "plain@example.com"})
    assert accepted.status_code == 200, accepted.text
    headers = {"X-Treg-Token": accepted.json()["token"]}
    assert (await _settings(clients, org_id, headers))["archive"] is False
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True}, headers=headers)
    assert r.status_code == 403


async def test_sweep_leaves_a_team_that_opted_back_in_alone(clients: AsyncClient, no_grace):
    """A sweep that finds nothing to erase for a team marks it done; a team that opted back in
    before the sweep ran is not marked (its new recordings are not what was erased)."""
    org_id = await _org_id(clients)
    await _opt_out(clients, org_id)
    await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True})
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 0
    await _opt_out(clients, org_id)
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
    assert (await _settings(clients, org_id))["archive_erasure"] == "done"


# ---------------------------------------------------------------------------------------------
# The sweep against the archive's other writers: late recordings, late settles, a second eraser,
# an objection withdrawn while the sweep ran

async def test_completion_waits_out_a_call_that_began_before_the_objection(
        clients: AsyncClient, own_key_serve, monkeypatch):
    """The rows go at once; "done" waits until no call from before the opt-out can still land."""
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    org_id = await _org_id(clients)
    await _opt_out(clients, org_id)
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 0
    keys, _ = await _rows()
    assert keys == []                                               # erased
    assert (await _settings(clients, org_id))["archive_erasure"] == "pending"   # not yet claimed
    monkeypatch.setattr(archive_erasure, "grace_s", lambda: 0)
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
    assert (await _settings(clients, org_id))["archive_erasure"] == "done"


async def test_a_late_recording_for_an_opted_out_team_is_refused(
        clients: AsyncClient, own_key_serve, no_grace):
    """A call that read "in the archive" when it began and records after the opt-out: the
    recorder re-reads the team's standing and drops the answer, so the erasure stays complete."""
    org_id = await _org_id(clients)
    await _record_own_answer(org_id, OWN)
    keys, _ = await _rows()
    assert len(keys) == 1
    await _opt_out(clients, org_id)
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
    await _record_own_answer(org_id, b'{"late": true}', "aweme_id=8")   # the in-flight call lands
    keys, snaps = await _rows()
    assert keys == [] and snaps == []
    # A deleted team is refused the same way: no row, no recording.
    assert await archive._org_refuses_recording(org_id + 1000)


async def test_a_late_settle_mark_for_an_opted_out_team_is_refused(clients: AsyncClient):
    org_id = await _org_id(clients)
    async with session_maker() as s:
        await archive.note_org_use_in_transaction(s, org_id, "b" * 64)
        await s.commit()
    assert len(await _marks()) == 1
    await _opt_out(clients, org_id)
    async with session_maker() as s:
        await archive.note_org_use_in_transaction(s, org_id, "c" * 64)   # a settle landing late
        await s.commit()
    assert [m.key_hash for m in await _marks()] == ["b" * 64]            # the sweep's to remove


async def test_an_objection_withdrawn_while_the_sweep_runs_stops_it(
        clients: AsyncClient, own_key_serve, no_grace):
    """`erase_org` is bound to the objection it read: a team that opted back in (and recorded
    anew) since is left alone, and the mark for that old objection is never written."""
    org_id = await _org_id(clients)
    await _record_own_answer(org_id, OWN)
    first = (await _opt_out(clients, org_id))["archive_opt_out_at"]
    from datetime import datetime
    stale = datetime.fromisoformat(first)
    # The team opts back in and records again before the bound erasure gets to run.
    await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True})
    await _record_own_answer(org_id, b'{"fresh": 1}', "aweme_id=9")
    result = await archive_erasure.erase_org(org_id, opted_out_at=stale, session_factory=session_maker)
    assert result["aborted"] and result["keys"] == 0
    keys, _ = await _rows()
    assert len(keys) == 2                                            # nothing touched
    # Opted out again: a NEW objection, which the sweep binds to and completes.
    second = (await _opt_out(clients, org_id))["archive_opt_out_at"]
    assert second != first
    assert await archive_erasure.sweep_once(session_factory=session_maker) == 1
    keys, _ = await _rows()
    assert keys == []


async def test_two_erasers_on_the_same_rows_subtract_the_totals_once(
        clients: AsyncClient, own_key_serve, monkeypatch):
    """The running totals are taken from what each statement deleted, so a second eraser that
    finds the rows gone (the sweep racing an owner delete, two instances) subtracts nothing."""
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    org_id = await _org_id(clients)
    from treg.domain.governance.teams import erase_archive_rows, private_archive_key_ids
    async with session_maker() as s:
        key_ids = await private_archive_key_ids(s, org_id)
    assert len(key_ids) == 2
    async with session_maker() as a, session_maker() as b:
        assert await erase_archive_rows(a, org_id, key_ids) == 2
        await a.commit()
        assert await erase_archive_rows(b, org_id, key_ids) == 0    # the loser: nothing counted
        await b.commit()
    async with session_maker() as s:
        stat = (await s.execute(select(ArchiveEndpointStat).where(
            ArchiveEndpointStat.endpoint_id == EP))).scalar_one()
    assert (stat.keys, stat.snapshots, stat.bodies_kept, stat.kept_bytes) == (0, 0, 0, 0)


async def test_deleting_a_team_fences_its_writes_first(clients: AsyncClient, own_key_serve, monkeypatch):
    """The owner delete opts the team out before erasing, so a recording from a call still in
    flight is refused rather than recreating rows the cascade just removed."""
    org = (await clients.get("/orgs")).json()[0]
    seen = []
    real = archive_erasure.erase_org

    async def spy(org_id, **kw):
        async with session_maker() as s:
            row = await s.get(Org, org_id)
        seen.append(row.archive_opt_out_at is not None)
        return await real(org_id, **kw)
    monkeypatch.setattr(archive_erasure, "erase_org", spy)
    r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
    assert r.status_code == 200, r.text
    assert seen == [True]
    await _record_own_answer(org["org_id"], OWN)                     # the late recorder
    keys, _ = await _rows()
    assert keys == []


def test_worker_command_is_registered():
    from treg.worker import _admin_erase_archive  # noqa: F401
    assert archive_erasure.worker_enabled()
