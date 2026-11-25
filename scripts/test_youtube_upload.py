import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock
import youtube_upload as u
import youtube_queue as q

CID='UC'+'a'*22
NOW=datetime(2026,11,24,5,30,tzinfo=timezone.utc)

def api():
    a=MagicMock()
    a.channels.return_value.list.return_value.execute.return_value={'items':[{
        'id':CID,'snippet':{'title':'DG','customUrl':'@ExampleChannel'},
        'contentDetails':{'relatedPlaylists':{'uploads':'UP'}}}]}
    return a

class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.asset=self.root/'video.mp4';self.asset.write_bytes(b'fake-test-video')
        self.db=q.connect(self.root/'queue.db')
        q.sync(self.db,{'channel_id':CID,'synced_at':u.iso(NOW),'complete':True,'items':[]},NOW)
        self.row=q.reserve(self.db,CID,self.asset,NOW)
        self.queue=u.QueueBridge(q,self.db,test_clock=True)
        self.metadata={'title':'Test','description':'Description','category_id':'27',
                       'made_for_kids':False,'contains_synthetic_media':False}
    def tearDown(self):
        self.db.close();self.tmp.cleanup()
    def run_upload(self,a):
        return u.upload_scheduled(a,self.queue,self.row['id'],CID,self.metadata,
                                 self.root/'journal',now=NOW,media_factory=MagicMock())
    def test_success_persists_id_and_schedule(self):
        a=api();request=a.videos.return_value.insert.return_value
        request.resumable_uri='https://secret-session'
        request.next_chunk.return_value=(None,{'id':'VIDEO'})
        a.playlistItems.return_value.list.return_value.execute.return_value={'items':[]}
        a.videos.return_value.list.return_value.execute.side_effect=[{'items':[{
            'id':'VIDEO','status':{'privacyStatus':'private','uploadStatus':'processed'},
            'processingDetails':{'processingStatus':'succeeded'}}]}, {'items':[{
            'id':'VIDEO','status':{'privacyStatus':'private','publishAt':self.row['publish_at'],
                                 'uploadStatus':'processed'},'processingDetails':{'processingStatus':'succeeded'}}]}]
        result=self.run_upload(a)
        self.assertEqual(result['status'],'scheduled')
        self.assertEqual(self.queue.get(self.row['id'])['video_id'],'VIDEO')
        self.assertEqual(self.queue.get(self.row['id'])['status'],'scheduled')
        body=a.videos.return_value.insert.call_args.kwargs['body']
        self.assertEqual(body['status']['privacyStatus'],'private')
        self.assertFalse(body['status']['selfDeclaredMadeForKids'])
        self.assertNotIn('publishAt',body['status'])
        a.videos.return_value.update.assert_called_once()
        a.videos.return_value.insert.return_value.next_chunk.assert_called_once_with(num_retries=0)
    def test_ambiguous_failure_blocks_duplicate(self):
        a=api();req=a.videos.return_value.insert.return_value
        req.resumable_uri='https://secret-session';req.next_chunk.side_effect=TimeoutError('sensitive error')
        with self.assertRaises(u.UploadError):self.run_upload(a)
        self.assertEqual(self.queue.get(self.row['id'])['status'],'uncertain')
        with self.assertRaises(u.UploadError):self.run_upload(a)
        self.assertEqual(a.videos.return_value.insert.call_count,1)
        journal=json.loads(next((self.root/'journal').glob('*.json')).read_text())
        self.assertEqual(journal['resumable_uri'],'https://secret-session')
    def test_verify_failure_retains_remote_id(self):
        a=api();a.videos.return_value.insert.return_value.resumable_uri='https://session'
        a.videos.return_value.insert.return_value.next_chunk.return_value=(None,{'id':'VIDEO'})
        a.videos.return_value.list.return_value.execute.return_value={'items':[]}
        with self.assertRaises(u.UploadError):self.run_upload(a)
        row=self.queue.get(self.row['id'])
        self.assertEqual(row['video_id'],'VIDEO');self.assertEqual(row['status'],'uncertain')
    def test_processing_upload_stays_private_without_schedule(self):
        a=api();req=a.videos.return_value.insert.return_value
        req.resumable_uri='https://session';req.next_chunk.return_value=(None,{'id':'VIDEO'})
        a.videos.return_value.list.return_value.execute.return_value={'items':[{
            'id':'VIDEO','status':{'privacyStatus':'private'},
            'processingDetails':{'processingStatus':'processing'}}]}
        result=self.run_upload(a)
        self.assertEqual(result['status'],'private-processing')
        self.assertEqual(self.queue.get(self.row['id'])['status'],'uncertain')
        a.videos.return_value.update.assert_not_called()
        self.assertNotIn('publishAt',a.videos.return_value.insert.call_args.kwargs['body']['status'])

    def test_reconcile_after_missed_slot_schedules_future_same_id(self):
        q.begin_upload(self.db,self.row['id'],NOW)
        q.transition(self.db,self.row['id'],'uncertain',video_id='VIDEO')
        later=datetime(2026,11,24,13,0,tzinfo=timezone.utc)
        a=api();a.playlistItems.return_value.list.return_value.execute.return_value={'items':[]}
        counter=[0]
        def video_response():
            counter[0]+=1
            status={'privacyStatus':'private','uploadStatus':'processed'}
            if counter[0]>1:status['publishAt']=self.queue.get(self.row['id'])['publish_at']
            return {'items':[{'id':'VIDEO','status':status,
                             'processingDetails':{'processingStatus':'succeeded'}}]}
        a.videos.return_value.list.return_value.execute.side_effect=video_response
        result=u.schedule_existing(a,self.queue,self.row['id'],CID,now=later)
        self.assertEqual(result['video_id'],'VIDEO')
        self.assertGreater(u.timestamp(result['publish_at']),later)
        self.assertNotEqual(result['publish_at'],self.row['publish_at'])
        a.videos.return_value.insert.assert_not_called()
        self.assertEqual(a.videos.return_value.update.call_args.kwargs['body']['id'],'VIDEO')

    def test_mutated_asset_prevents_mutation(self):
        a=api();self.asset.write_bytes(b'changed')
        with self.assertRaises(u.UploadError):self.run_upload(a)
        a.videos.return_value.insert.assert_not_called()
        self.assertEqual(self.queue.get(self.row['id'])['status'],'reserved')
    def test_wrong_channel_prevents_mutation(self):
        a=api();a.channels.return_value.list.return_value.execute.return_value={'items':[{'id':'OTHER'}]}
        with self.assertRaises(u.UploadError):self.run_upload(a)
        a.videos.return_value.insert.assert_not_called()
    def test_exclusive_publication_lock_rejects_second_holder(self):
        path=self.root/'queue.db'
        with u.publication_lock(path):
            with self.assertRaises(u.UploadError):
                with u.publication_lock(path):
                    self.fail('Second holder acquired publication lock')
        with u.publication_lock(path):
            pass

    def test_publication_lock_released_after_error(self):
        path=self.root/'queue.db'
        with self.assertRaises(RuntimeError):
            with u.publication_lock(path):
                raise RuntimeError('Simulated remote error')
        with u.publication_lock(path):
            pass

    def test_atomic_private_file(self):
        path=self.root/'token.json';u.atomic_json(path,{'x':1})
        self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_hidden_videos_caveated_not_current_inventory_incomplete(self):
        a=api();a.playlistItems.return_value.list.return_value.execute.return_value={'items':[
            {'contentDetails':{'videoId':'hidden'}},{'contentDetails':{'videoId':'published'}}]}
        a.videos.return_value.list.return_value.execute.return_value={'items':[
            {'id':'hidden','status':{'privacyStatus':'private'}},
            {'id':'published','status':{'privacyStatus':'public'},'snippet':{'publishedAt':u.iso(NOW)}}]}
        result=u.inventory(a,CID,NOW)
        self.assertTrue(result['complete']);self.assertEqual(len(result['items']),1)
        self.assertEqual(result['unresolved_video_ids'],['hidden'])
    def test_inventory_pagination_and_missing_results(self):
        a=api();a.playlistItems.return_value.list.return_value.execute.side_effect=[
            {'items':[{'contentDetails':{'videoId':'one'}}],'nextPageToken':'NEXT'},
            {'items':[{'contentDetails':{'videoId':'missing'}}]}]
        a.videos.return_value.list.return_value.execute.return_value={'items':[
            {'id':'one','status':{'privacyStatus':'private','publishAt':self.row['publish_at']}}]}
        result=u.inventory(a,CID,NOW)
        self.assertFalse(result['complete']);self.assertEqual(result['missing_video_ids'],['missing'])
        self.assertEqual(a.playlistItems.return_value.list.call_count,2)
    def test_metadata_requires_explicit_audience(self):
        del self.metadata['made_for_kids']
        with self.assertRaises(u.UploadError):u.validate_metadata(self.metadata)

if __name__=='__main__':unittest.main()
