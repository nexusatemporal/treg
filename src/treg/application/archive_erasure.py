"""Erase what a team stored in the archive (docs/context/architecture/archive.md, "Opting out").

A team that opts out of the archive, or is deleted, has a right to have what it stored removed:
its own-credential answers (every key under its `org:` or `conn:` scope, with the request shape
those keys carry), and the `ArchiveKeyOrg` marks that tie the team to questions it paid for.
Platform-key answers are public questions by construction and are not the team's data; nothing
there names the team once its marks are gone.

Objects before rows, in separate sessions, because a request holds no database connection while
object I/O is in flight (AGENTS.md, non-negotiable 3) and because a row that outlives a failed
object delete is what makes the next pass able to retry. The row rules live in
`governance.teams` (`private_archive_key_ids`, `archive_bodies_only_under`, `erase_archive_rows`);
team deletion's cascade uses the last of them, so a deleted team never leaves rows behind even
when nobody ran the object phase. Bodies are content-addressed and deduplicated across keys, so
only a body no other key's snapshot points at is deleted; the judgement and the delete are not
atomic with a recording that lands between them, and such a snapshot reads back as "bytes not on
file", which every reader already tolerates.

`sweep_once` is the opt-out path: every team with `archive_opt_out_at` set and `archive_purged_at`
still NULL is erased and marked. It is idempotent and resumable: a pass that dies leaves the mark
unset and the next pass finishes the job. The sweep worker runs from the lifespan beside the
archive's other workers and is poked by the settings route so an opt-out does not wait a full
interval.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update

from .. import archive_bodies
from ..config import get_settings
from ..domain.governance.teams import archive_bodies_only_under, erase_archive_rows, private_archive_key_ids
from ..infra.db import background_session_maker
from ..models import Org
from ..timeutil import utcnow_naive

_log = logging.getLogger("treg.archive.erasure")

_KEY_BATCH = 200          # keys per pass: bounded lock footprint, resumable

_poke: asyncio.Event | None = None


def _event() -> asyncio.Event:
    global _poke
    if _poke is None:
        _poke = asyncio.Event()
    return _poke


def poke() -> None:
    """Wake the sweep worker in this process, if there is one. Durable state is the org row; the
    poke only shortens the wait."""
    if _poke is not None:
        _poke.set()


async def _delete_objects(hashes: set[str]) -> int:
    """Remove the given bodies from the object store -> how many could not be removed. Without a
    configured store there is nothing to do: bodies then live only in the rows."""
    store = archive_bodies._store
    failed = 0
    if store is None:
        return 0
    for content_hash in sorted(hashes):
        try:
            await store.delete(content_hash)
        except Exception as exc:  # noqa: BLE001 - one object failing must not stop the rest
            failed += 1
            _log.warning("archive erasure: object %s not deleted: %s", content_hash[:12], exc)
    return failed


async def erase_org(org_id: int, *, session_factory=background_session_maker) -> dict:
    """Erase everything the team stored: objects first, then rows, in bounded passes.

    Each pass finds a batch of the team's keys and the bodies only they point at, closes the
    session, deletes those objects, then deletes the rows in a fresh transaction. A pass whose
    object deletes failed stops before the rows and raises: the rows are what the next pass
    retries from. Between the two steps the team's own rows point at deleted bytes, which only
    the team itself could have read, and the opt-out gate already keeps it from reading them."""
    keys = objects = 0
    while True:
        async with session_factory() as db:
            key_ids = await private_archive_key_ids(db, org_id, limit=_KEY_BATCH)
            orphans = await archive_bodies_only_under(db, key_ids) if key_ids else set()
        if not key_ids:
            break
        failed = await _delete_objects(orphans)
        if failed:
            raise RuntimeError(f"{failed} archive object(s) could not be deleted")
        objects += len(orphans)
        async with session_factory() as db:
            keys += await erase_archive_rows(db, org_id, key_ids)
            await db.commit()
    async with session_factory() as db:
        await erase_archive_rows(db, org_id, [])      # the marks alone, when no key was left
        await db.commit()
    return {"org_id": org_id, "keys": keys, "objects": objects}


async def sweep_once(*, session_factory=background_session_maker) -> int:
    """Erase every opted-out team not yet purged -> how many were marked purged."""
    async with session_factory() as db:
        org_ids = (await db.execute(
            select(Org.id).where(Org.archive_opt_out_at.is_not(None),
                                 Org.archive_purged_at.is_(None)).order_by(Org.id))).scalars().all()
    done = 0
    for org_id in org_ids:
        try:
            result = await erase_org(org_id, session_factory=session_factory)
        except Exception:  # noqa: BLE001 - the mark stays unset; the next pass retries this team
            _log.warning("archive erasure for org %s did not finish", org_id, exc_info=True)
            continue
        async with session_factory() as db:
            # Only a team still opted out and still unmarked takes the mark: an opt-back-in that
            # raced the sweep is left alone (its new recordings are not what was erased).
            marked = await db.execute(
                update(Org).where(Org.id == org_id, Org.archive_opt_out_at.is_not(None),
                                  Org.archive_purged_at.is_(None))
                .values(archive_purged_at=utcnow_naive()))
            await db.commit()
        if marked.rowcount:
            done += 1
            _log.info("archive erasure for org %s: %s", org_id, result)
    return done


def worker_enabled() -> bool:
    return get_settings().archive_erasure_interval_s > 0


async def sweep_worker() -> None:
    """Run forever from the lifespan: a pass per interval, or sooner when poked."""
    event = _event()
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.warning("archive erasure sweep failed", exc_info=True)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=get_settings().archive_erasure_interval_s)
        except TimeoutError:
            pass
