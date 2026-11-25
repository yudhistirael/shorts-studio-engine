#!/usr/bin/env python3
"""Local-only YouTube publish queue. No upload, OAuth, or remote enumeration.

Python 3.9+ standard library; no pip dependencies. --help lists commands.
Use sync only with an authenticated, complete enumeration of published and
scheduled channel videos. A JSON file is evidence supplied by the caller,
not proof this tool queried YouTube. Never put tokens in that JSON.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

JAKARTA = ZoneInfo('Asia/Jakarta')
UTC = timezone.utc
DEFAULT_DB = Path.home() / '.local/share/shorts-studio-engine/youtube/queue.sqlite3'
SLOTS = (time(7), time(13), time(19))
ACTIVE = ('reserved', 'uploading', 'uncertain', 'scheduled', 'published', 'failed')
TRANSITIONS = {
    'reserved': {'failed', 'cancelled'},
    'uploading': {'uncertain', 'scheduled', 'published', 'failed', 'cancelled'},
    'uncertain': {'scheduled', 'published', 'failed', 'cancelled'},
    'scheduled': {'published', 'uncertain'},
    'failed': {'uncertain', 'scheduled', 'published', 'cancelled'},
    'published': set(), 'cancelled': set(),
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, asset_sha256 TEXT NOT NULL,
 asset_path TEXT NOT NULL, publish_at TEXT NOT NULL, status TEXT NOT NULL,
 video_id TEXT, UNIQUE(channel_id,asset_sha256));
CREATE UNIQUE INDEX IF NOT EXISTS job_video ON jobs(channel_id,video_id)
 WHERE video_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS external (
 channel_id TEXT NOT NULL, video_id TEXT NOT NULL, publish_at TEXT NOT NULL,
 status TEXT NOT NULL, PRIMARY KEY(channel_id,video_id));
CREATE TABLE IF NOT EXISTS syncs (channel_id TEXT PRIMARY KEY, synced_at TEXT NOT NULL);
'''


def aware(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError('Timestamp must include timezone offset')
    return d.astimezone(UTC)


def iso(d):
    return d.astimezone(UTC).isoformat(timespec='seconds')


def channel(value):
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
        raise ValueError('Use actual UC channel ID (24 characters), not a handle')
    return value


def connect(path, readonly=False):
    path = Path(path).expanduser()
    if readonly:
        if path.exists():
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
        else:
            db = sqlite3.connect(':memory:')
            db.executescript(SCHEMA)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def entries(db, cid):
    # Remote snapshot is authoritative for remote IDs; local rows not in it
    # retain their capacity until explicitly reconciled/cancelled.
    rows = [dict(r) for r in db.execute(
        "SELECT video_id,publish_at,status FROM external WHERE channel_id=?", (cid,))]
    remote_ids = {r['video_id'] for r in rows}
    for r in db.execute('SELECT * FROM jobs WHERE channel_id=?', (cid,)):
        if r['status'] in ACTIVE and r['video_id'] not in remote_ids:
            rows.append(dict(r))
    return rows


def conflicts(rows):
    days = {}
    times = sorted(aware(r['publish_at']) for r in rows)
    for d in times:
        key = d.astimezone(JAKARTA).date().isoformat()
        days[key] = days.get(key, 0) + 1
    result = [f'{day}: {n} videos exceeds daily maximum 3' for day, n in days.items() if n > 3]
    for a, b in zip(times, times[1:]):
        if b - a < timedelta(hours=4):
            result.append(f'Less than 4h separation: {iso(a)} / {iso(b)}')
    return result


def next_slot(rows, now):
    times = [aware(r['publish_at']) for r in rows]
    start = now + timedelta(minutes=15)
    day = start.astimezone(JAKARTA).date()
    # Finite inputs always leave capacity beyond their last scheduled date.
    last_day = max([day] + [d.astimezone(JAKARTA).date() for d in times])
    horizon = max(366, (last_day - day).days + 2)
    for offset in range(horizon):
        candidate_day = day + timedelta(days=offset)
        count = sum(d.astimezone(JAKARTA).date() == candidate_day for d in times)
        if count >= 3:
            continue
        for slot in SLOTS:
            d = datetime.combine(candidate_day, slot, JAKARTA).astimezone(UTC)
            if d >= start and all(abs(d - x) >= timedelta(hours=4) for x in times):
                return d
    raise ValueError('No available slot within planning horizon')


def serialize(row):
    result = dict(row)
    result['publish_at_wib'] = aware(result['publish_at']).astimezone(JAKARTA).isoformat()
    return result


def sync(db, manifest, now):
    cid = channel(manifest['channel_id'])
    stamp = aware(manifest['synced_at'])
    if manifest.get('complete') is not True or not isinstance(manifest.get('items'), list):
        raise ValueError('Sync requires complete:true and items from authenticated enumeration')
    if stamp > now or now - stamp > timedelta(minutes=15):
        raise ValueError('Sync timestamp must be from the last 15 minutes, not the future')
    cleaned = []
    seen = set()
    for item in manifest['items']:
        vid = item['video_id']
        if not isinstance(vid, str) or not vid.strip() or vid in seen:
            raise ValueError('Remote video IDs must be nonempty and unique')
        seen.add(vid)
        status = item['status']
        if status not in ('scheduled', 'published'):
            raise ValueError('Remote status must be scheduled or published')
        cleaned.append((cid, vid, iso(aware(item['publish_at'])), status))
    with transaction(db):
        prior = db.execute('SELECT synced_at FROM syncs WHERE channel_id=?', (cid,)).fetchone()
        if prior and aware(prior['synced_at']) > stamp:
            raise ValueError('Cannot replace a newer remote snapshot with an older one')
        db.execute('DELETE FROM external WHERE channel_id=?', (cid,))
        db.executemany('INSERT INTO external VALUES (?,?,?,?)', cleaned)
        for _, vid, when, status in cleaned:
            db.execute('UPDATE jobs SET publish_at=?,status=? WHERE channel_id=? AND video_id=?',
                       (when, status, cid, vid))
        db.execute('INSERT INTO syncs VALUES (?,?) ON CONFLICT(channel_id) DO UPDATE SET synced_at=excluded.synced_at', (cid, iso(stamp)))
        warnings = conflicts(entries(db, cid))
    return {'channel_id': cid, 'synced_at': iso(stamp), 'conflicts': warnings}


def reserve(db, cid, asset, now):
    cid = channel(cid)
    asset = Path(asset).resolve(strict=True)
    if not asset.is_file():
        raise ValueError('Asset must be a regular file')
    digest = hashlib.sha256()
    with asset.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    digest = digest.hexdigest()
    with transaction(db):
        existing = db.execute('SELECT * FROM jobs WHERE channel_id=? AND asset_sha256=?', (cid, digest)).fetchone()
        if existing:
            return serialize(existing)
        stamp = db.execute('SELECT synced_at FROM syncs WHERE channel_id=?', (cid,)).fetchone()
        if not stamp or not timedelta(0) <= now - aware(stamp['synced_at']) <= timedelta(minutes=15):
            raise ValueError('Fresh complete remote sync required before reserve; plan is offline/read-only')
        if db.execute("SELECT 1 FROM jobs WHERE channel_id=? AND status IN ('uploading','uncertain')", (cid,)).fetchone():
            raise ValueError('Resolve in-flight/uncertain upload before reserving another video')
        when = next_slot(entries(db, cid), now)
        cursor = db.execute('INSERT INTO jobs(channel_id,asset_sha256,asset_path,publish_at,status) VALUES (?,?,?,?,?)',
                            (cid, digest, str(asset), iso(when), 'reserved'))
        return serialize(db.execute('SELECT * FROM jobs WHERE id=?', (cursor.lastrowid,)).fetchone())


def begin_upload(db, job_id, now):
    """Claim one reserved job; uploader must mark uncertainty after ambiguous I/O."""
    with transaction(db):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['status'] != 'reserved':
            raise ValueError('Only a reserved job can begin uploading')
        cid = row['channel_id']
        stamp = db.execute('SELECT synced_at FROM syncs WHERE channel_id=?', (cid,)).fetchone()
        if not stamp or not timedelta(0) <= now - aware(stamp['synced_at']) <= timedelta(minutes=15):
            raise ValueError('Fresh remote sync required before upload')
        if db.execute("SELECT 1 FROM jobs WHERE channel_id=? AND status IN ('uploading','uncertain')", (cid,)).fetchone():
            raise ValueError('Resolve in-flight/uncertain upload first')
        when = aware(row['publish_at'])
        if when < now + timedelta(minutes=15):
            raise ValueError('Reserved slot is too close or past; do not upload to a past publishAt')
        all_rows = entries(db, cid)
        others = [r for r in all_rows if r.get('id') != job_id]
        day = when.astimezone(JAKARTA).date()
        if sum(aware(r['publish_at']).astimezone(JAKARTA).date() == day for r in others) >= 3:
            raise ValueError('Remote changes filled this reservation day')
        if any(abs(when - aware(r['publish_at'])) < timedelta(hours=4) for r in others):
            raise ValueError('Remote changes conflict with reserved slot separation')
        asset = Path(row['asset_path'])
        digest = hashlib.sha256()
        with asset.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != row['asset_sha256']:
            raise ValueError('Asset changed after reservation')
        db.execute("UPDATE jobs SET status='uploading' WHERE id=?", (job_id,))
        return serialize(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def reschedule_existing(db, job_id, now):
    """Replan an already uploaded, verified-private video; never clear its ID.

    Caller must first confirm on YouTube that this exact video is still private,
    has never published, and is ready to schedule. The complete sync excludes
    unrelated private uploads, so this helper cannot establish that by itself.
    No quota is released until the new slot is committed atomically.
    """
    with transaction(db):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['status'] != 'uncertain' or not row['video_id']:
            raise ValueError('Reschedule requires existing uncertain job with a known remote video ID')
        cid = row['channel_id']
        stamp = db.execute('SELECT synced_at FROM syncs WHERE channel_id=?', (cid,)).fetchone()
        if not stamp or not timedelta(0) <= now - aware(stamp['synced_at']) <= timedelta(minutes=15):
            raise ValueError('Fresh complete remote sync required before reschedule')
        if db.execute("SELECT 1 FROM jobs WHERE channel_id=? AND id!=? AND status IN ('uploading','uncertain')",
                      (cid, job_id)).fetchone():
            raise ValueError('Resolve other in-flight/uncertain uploads first')
        others = [r for r in entries(db, cid)
                  if r.get('id') != job_id and r.get('video_id') != row['video_id']]
        when = next_slot(others, now)
        db.execute('UPDATE jobs SET publish_at=? WHERE id=?', (iso(when), job_id))
        return serialize(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


# Compatibility name used by the uploader: same guarded transaction.
reschedule_uploaded = reschedule_existing


def transition(db, job_id, status, video_id=None, proven_not_uploaded=False):
    with transaction(db):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job')
        if status not in TRANSITIONS[row['status']]:
            raise ValueError(f'Invalid transition {row["status"]} -> {status}')
        if status == 'cancelled' and (not proven_not_uploaded or row['video_id']):
            raise ValueError('Cancel requires --proven-not-uploaded and no known remote video ID')
        if video_id is not None and (not video_id.strip() or row['video_id'] not in (None, video_id)):
            raise ValueError('Cannot clear/replace a known remote video ID')
        vid = video_id or row['video_id']
        if status in ('scheduled', 'published') and not vid:
            raise ValueError('Remote video ID required for scheduled/published')
        db.execute('UPDATE jobs SET status=?,video_id=? WHERE id=?', (status, vid, job_id))
        return serialize(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--now', help='Timezone-aware timestamp; omit to use current UTC time')
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('plan', 'list'):
        p = sub.add_parser(cmd)
        p.add_argument('--channel', required=True)
    p = sub.add_parser('reserve')
    p.add_argument('--channel', required=True)
    p.add_argument('--asset', required=True, type=Path)
    p = sub.add_parser('sync', help='Import complete remote manifest; does not query YouTube')
    p.add_argument('manifest', type=Path)
    p = sub.add_parser('status')
    p.add_argument('job_id', type=int)
    p.add_argument('status', choices=list(TRANSITIONS))
    p.add_argument('--video-id')
    p.add_argument('--proven-not-uploaded', action='store_true')
    args = parser.parse_args(argv)
    db = None
    try:
        now = aware(args.now) if args.now else datetime.now(UTC)
        db = connect(args.db, readonly=args.command in ('plan', 'list'))
        if args.command == 'sync':
            result = sync(db, json.loads(args.manifest.read_text()), now)
        elif args.command == 'reserve':
            result = reserve(db, args.channel, args.asset, now)
        elif args.command == 'status':
            result = (begin_upload(db, args.job_id, now) if args.status == 'uploading'
                      else transition(db, args.job_id, args.status, args.video_id, args.proven_not_uploaded))
        else:
            cid = channel(args.channel)
            rows = entries(db, cid)
            if args.command == 'list':
                result = {'items': [serialize(r) for r in rows], 'conflicts': conflicts(rows)}
            else:
                d = next_slot(rows, now)
                result = {'offline_preview_only': True, 'reserved': False,
                          'publish_at': iso(d), 'publish_at_wib': d.astimezone(JAKARTA).isoformat()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1
    finally:
        if db:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
