import concurrent.futures
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

import youtube_queue as q

CID = 'UC' + 'a' * 22
NOW = q.aware('2026-11-24T12:30:00+07:00')


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'queue.sqlite3'
        self.db = q.connect(self.path)
        self.addCleanup(self.db.close)
        self.manifest = {'channel_id': CID, 'synced_at': q.iso(NOW), 'complete': True, 'items': []}
        q.sync(self.db, self.manifest, NOW)

    def asset(self, name):
        p = self.root/name
        p.write_bytes(name.encode())
        return p

    def test_slots_day_rollover_and_idempotency(self):
        rows = [q.reserve(self.db,CID,self.asset(str(i)),NOW) for i in range(4)]
        self.assertEqual([r['publish_at_wib'] for r in rows], [
            '2026-11-24T13:00:00+07:00','2026-11-24T19:00:00+07:00',
            '2026-11-25T07:00:00+07:00','2026-11-25T13:00:00+07:00'])
        again=q.reserve(self.db,CID,self.root/'0',NOW+timedelta(hours=2))
        self.assertEqual(again['id'],rows[0]['id'])

    def test_three_per_day_manual_and_cross_day_spacing(self):
        self.manifest['items']=[{'video_id':str(i),'publish_at':t,'status':'published'} for i,t in enumerate([
            '2026-11-24T07:00:00+07:00','2026-11-24T13:00:00+07:00','2026-11-24T19:00:00+07:00'])]
        q.sync(self.db,self.manifest,NOW)
        row=q.reserve(self.db,CID,self.asset('a'),NOW)
        self.assertEqual(row['publish_at_wib'],'2026-11-25T07:00:00+07:00')
        rows=[{'publish_at':'2026-11-24T23:30:00+07:00'}]
        slot=q.next_slot(rows,q.aware('2026-11-24T23:00:00+07:00'))
        self.assertEqual(slot.astimezone(q.JAKARTA).isoformat(),'2026-11-25T07:00:00+07:00')

    def test_sync_required_stale_or_future(self):
        with self.assertRaises(ValueError): q.reserve(self.db,CID,self.asset('a'),NOW+timedelta(minutes=16))
        self.manifest['synced_at']=q.iso(NOW+timedelta(seconds=1))
        with self.assertRaises(ValueError): q.sync(self.db,self.manifest,NOW)
        self.manifest['complete']=False
        with self.assertRaises(ValueError): q.sync(self.db,self.manifest,NOW)

    def test_failure_keeps_capacity_uncertainty_blocks(self):
        a=q.reserve(self.db,CID,self.asset('a'),NOW)
        q.begin_upload(self.db,a['id'],NOW)
        q.transition(self.db,a['id'],'uncertain')
        with self.assertRaises(ValueError):q.reserve(self.db,CID,self.asset('b'),NOW)
        q.transition(self.db,a['id'],'failed')
        b=q.reserve(self.db,CID,self.root/'b',NOW)
        self.assertEqual(b['publish_at_wib'],'2026-11-24T19:00:00+07:00')
        with self.assertRaises(ValueError):q.transition(self.db,a['id'],'cancelled')
        q.transition(self.db,a['id'],'cancelled',proven_not_uploaded=True)
        # Same bytes never trigger a second reservation/upload.
        self.assertEqual(q.reserve(self.db,CID,self.root/'a',NOW)['status'],'cancelled')

    def test_remote_dedupe_sync_updates_and_conflicts(self):
        a=q.reserve(self.db,CID,self.asset('a'),NOW)
        q.begin_upload(self.db,a['id'],NOW)
        q.transition(self.db,a['id'],'scheduled','remote')
        self.manifest['items']=[{'video_id':'remote','publish_at':a['publish_at'],'status':'scheduled'}]
        q.sync(self.db,self.manifest,NOW)
        self.assertEqual(len(q.entries(self.db,CID)),1)
        self.manifest['items'].append({'video_id':'other','publish_at':a['publish_at'],'status':'scheduled'})
        result=q.sync(self.db,self.manifest,NOW)
        self.assertTrue(result['conflicts'])

    def test_begin_upload_rechecks_asset_and_remote_changes(self):
        p=self.asset('a');a=q.reserve(self.db,CID,p,NOW)
        p.write_bytes(b'changed')
        with self.assertRaises(ValueError):q.begin_upload(self.db,a['id'],NOW)
        p.write_bytes(b'a')
        self.manifest['items']=[{'video_id':'other','publish_at':a['publish_at'],'status':'scheduled'}]
        q.sync(self.db,self.manifest,NOW)
        with self.assertRaises(ValueError):q.begin_upload(self.db,a['id'],NOW)

    def test_concurrent_reservations_unique(self):
        assets=[self.asset(str(i)) for i in range(8)]
        def allocate(p):
            db=q.connect(self.path)
            try:return q.reserve(db,CID,p,NOW)
            finally:db.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows=list(pool.map(allocate,assets))
        self.assertEqual(len({r['publish_at'] for r in rows}),8)
        self.assertFalse(q.conflicts(q.entries(self.db,CID)))

    def test_readonly_preview_creates_no_disk_state(self):
        missing=self.root/'missing'/'queue.sqlite3'
        db=q.connect(missing,readonly=True)
        q.next_slot(q.entries(db,CID),NOW)
        db.close()
        self.assertFalse(missing.parent.exists())

    def test_spacing_exact_four_hours_and_early_manual_publish(self):
        rows=[{'publish_at':'2026-11-24T03:00:00+07:00'}]
        now=q.aware('2026-11-24T02:00:00+07:00')
        self.assertEqual(q.next_slot(rows,now).astimezone(q.JAKARTA).hour,7)
        rows=[{'publish_at':'2026-11-24T04:30:00+07:00'}]
        self.assertEqual(q.next_slot(rows,now).astimezone(q.JAKARTA).hour,13)

    def test_reschedule_existing_keeps_identity_and_handles_missed_slot(self):
        a=q.reserve(self.db,CID,self.asset('a'),NOW)
        q.begin_upload(self.db,a['id'],NOW)
        q.transition(self.db,a['id'],'uncertain','already-uploaded')
        later=q.aware('2026-11-24T18:00:00+07:00')
        with self.assertRaises(ValueError):q.reschedule_existing(self.db,a['id'],later)
        self.manifest['synced_at']=q.iso(later)
        self.manifest['items']=[{'video_id':'other','publish_at':'2026-11-24T19:00:00+07:00','status':'scheduled'}]
        q.sync(self.db,self.manifest,later)
        updated=q.reschedule_existing(self.db,a['id'],later)
        self.assertEqual(updated['publish_at_wib'],'2026-11-25T07:00:00+07:00')
        self.assertEqual(updated['video_id'],'already-uploaded')
        self.assertEqual(updated['id'],a['id'])
        self.assertEqual(updated['asset_sha256'],a['asset_sha256'])
        self.assertEqual(updated['status'],'uncertain')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_reschedule_rejects_unknown_remote_id_and_other_inflight(self):
        a=q.reserve(self.db,CID,self.asset('a'),NOW)
        b=q.reserve(self.db,CID,self.asset('b'),NOW)
        with self.assertRaises(ValueError):q.reschedule_existing(self.db,a['id'],NOW)
        q.begin_upload(self.db,a['id'],NOW)
        q.transition(self.db,a['id'],'uncertain','known')
        # Simulate recovered second ambiguous upload from another client.
        self.db.execute("UPDATE jobs SET status='uncertain' WHERE id=?",(b['id'],))
        self.db.commit()
        with self.assertRaises(ValueError):q.reschedule_existing(self.db,a['id'],NOW)
        self.assertEqual(self.db.execute('SELECT publish_at FROM jobs WHERE id=?',(a['id'],)).fetchone()[0],a['publish_at'])

    def test_unknown_status_and_naive_time(self):
        with self.assertRaises(ValueError):q.aware('2026-11-24T12:00:00')
        a=q.reserve(self.db,CID,self.asset('a'),NOW)
        with self.assertRaises(ValueError):q.transition(self.db,a['id'],'published')


if __name__=='__main__':unittest.main()
