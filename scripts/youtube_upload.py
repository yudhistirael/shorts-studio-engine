#!/usr/bin/env python3
"""YouTube OAuth, inventory and guarded scheduled upload. Never calls a paid API.
Install: python3 -m pip install -r requirements-youtube.txt
Google SDKs are imported lazily; --help and pure tests need only Python 3.9+.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl',
          'https://www.googleapis.com/auth/youtube.upload',
          'https://www.googleapis.com/auth/youtube.readonly']
STATE = Path.home()/'.local/share/shorts-studio-engine/youtube'

class UploadError(RuntimeError):
    pass

def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise UploadError('Timestamp must include timezone')
    return dt.astimezone(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def read_json(path):
    with Path(path).open() as stream:
        return json.load(stream)

def save_token(path, credentials):
    atomic_json(path, json.loads(credentials.to_json()))

def service(credentials):
    from googleapiclient.discovery import build
    return build('youtube', 'v3', credentials=credentials, cache_discovery=False)

def load_credentials(path):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not Path(path).exists():
        raise UploadError('OAuth token missing: run auth first')
    credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_token(path, credentials)
    if not credentials.valid or not credentials.has_scopes(SCOPES):
        raise UploadError('OAuth missing required scopes or expired: run auth again')
    return credentials

def own_channel(api, channel_id=None, expected_handle=None):
    response = api.channels().list(part='id,snippet,contentDetails', mine=True).execute()
    items = response.get('items', [])
    # One authenticated identity must map unambiguously to the pinned channel.
    if len(items) != 1:
        raise UploadError('OAuth must select exactly one channel/Brand Account')
    item = items[0]
    if channel_id and item['id'] != channel_id:
        raise UploadError('Authenticated channel differs from pinned channel')
    if expected_handle:
        resolved = api.channels().list(part='id', forHandle=expected_handle.lstrip('@')).execute()
        matches = resolved.get('items', [])
        if len(matches) != 1 or matches[0]['id'] != item['id']:
            raise UploadError('Authenticated channel does not match requested @handle')
    return item

def authorize(client_secrets, token_path, config_path, expected_handle, channel_id=None):
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    credentials = flow.run_local_server(host='127.0.0.1', port=0,
                                       access_type='offline', prompt='consent')
    item = own_channel(service(credentials), channel_id, expected_handle)
    config = {'channel_id': item['id'], 'title': item['snippet']['title'],
              'custom_url': item['snippet'].get('customUrl'),
              'expected_handle': expected_handle}
    save_token(token_path, credentials)
    atomic_json(config_path, config)
    return config

def inventory(api, channel_id, now=None):
    now = now or datetime.now(timezone.utc)
    channel = own_channel(api, channel_id)
    playlist = channel['contentDetails']['relatedPlaylists']['uploads']
    ids = []
    page = None
    while True:
        args = dict(part='contentDetails', playlistId=playlist, maxResults=50)
        if page:
            args['pageToken'] = page
        response = api.playlistItems().list(**args).execute()
        ids.extend(x['contentDetails']['videoId'] for x in response.get('items', []))
        page = response.get('nextPageToken')
        if not page:
            break
    items, unresolved, missing = [], [], []
    for offset in range(0, len(ids), 50):
        response = api.videos().list(part='snippet,status,processingDetails',
                                     id=','.join(ids[offset:offset+50])).execute()
        seen = set()
        for video in response.get('items', []):
            seen.add(video['id'])
            status = video.get('status', {})
            if status.get('privacyStatus') == 'public':
                when = video.get('snippet', {}).get('publishedAt')
                kind = 'published'
            elif status.get('privacyStatus') == 'private' and status.get('publishAt'):
                when, kind = status['publishAt'], 'scheduled'
            else:
                # A hidden video might previously have been public. The API does not
                # supply trustworthy historical publication events for this case.
                unresolved.append(video['id'])
                continue
            if not when:
                unresolved.append(video['id'])
                continue
            items.append({'video_id': video['id'], 'publish_at': iso(timestamp(when)), 'status': kind})
        missing.extend(sorted(set(ids[offset:offset+50])-seen))
    return {'channel_id': channel_id, 'synced_at': iso(now),
            'complete': not missing, 'items': items,
            'unresolved_video_ids': sorted(set(unresolved)), 'missing_video_ids':sorted(set(missing)),
            'note': 'Hidden/unscheduled videos require history reconciliation before strict daily-cap claims.'}

def validate_metadata(data):
    title, description = data.get('title'), data.get('description')
    if not isinstance(title, str) or not title.strip() or len(title)>100 or '<' in title or '>' in title:
        raise UploadError('Valid nonempty title <=100 characters required')
    if not isinstance(description, str) or len(description.encode('utf-8'))>5000:
        raise UploadError('Description must be text <=5000 UTF-8 bytes')
    for key in ['made_for_kids', 'contains_synthetic_media']:
        if type(data.get(key)) is not bool:
            raise UploadError(key+' must explicitly be true or false')
    category = str(data.get('category_id', ''))
    if not category.isdigit():
        raise UploadError('Explicit numeric category_id required')
    return {'snippet': {'title': title, 'description': description, 'categoryId': category,
                        'defaultLanguage': 'id'},
            'status': {'privacyStatus': 'private',
                       'selfDeclaredMadeForKids': data['made_for_kids'],
                       'containsSyntheticMedia': data['contains_synthetic_media']}}

def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def verify_scheduled(api, video_id, publish_at):
    response = api.videos().list(part='status,processingDetails', id=video_id).execute()
    items = response.get('items', [])
    if len(items)!=1:
        raise UploadError('Uploaded video unavailable for verification')
    video = items[0]
    status = video.get('status', {})
    if (status.get('privacyStatus') != 'private' or not status.get('publishAt') or
            timestamp(status['publishAt']) != timestamp(publish_at)):
        raise UploadError('Remote schedule does not match reserved slot')
    if status.get('uploadStatus') in ('failed','rejected','deleted') or video.get('processingDetails', {}).get('processingStatus') in ('failed','terminated'):
        raise UploadError('YouTube reports upload/processing failure')
    return video


def upload_scheduled(api, queue, reservation_id, channel_id, metadata, state_dir, now=None,
                     media_factory=None):
    """queue bridge contract: get, begin_upload, mark_uncertain, record_video, mark_scheduled.
    begin_upload MUST atomically revalidate reserved state, recent complete sync,
    channel, reserved slot constraints and <=3/day before returning claimed row.
    """
    now = now or datetime.now(timezone.utc)
    own_channel(api, channel_id)
    body = validate_metadata(metadata)
    row = queue.get(reservation_id)
    if row['channel_id'] != channel_id or row['status'] != 'reserved':
        raise UploadError('Reservation channel/state mismatch')
    file = Path(row['file']).resolve(strict=True)
    if sha256(file) != row['asset_sha256']:
        raise UploadError('Asset changed since reservation')
    when = timestamp(row['publish_at'])
    if when < now+timedelta(minutes=15):
        raise UploadError('Reserved slot is too near/past; replan explicitly before upload')
    if media_factory is None:
        from googleapiclient.http import MediaFileUpload
        media_factory = MediaFileUpload
    media = media_factory(str(file), mimetype='video/mp4', chunksize=8*1024*1024, resumable=True)
    # State is claimed before any API mutation. Exceptions are deliberately not retried.
    claimed = queue.begin_upload(reservation_id, channel_id, now)
    for key in ['file','asset_sha256','publish_at','channel_id']:
        if claimed[key] != row[key]:
            queue.mark_uncertain(reservation_id, 'reservation changed during preflight')
            raise UploadError('Reservation changed during upload claim')
    journal = Path(state_dir)/('upload-'+str(reservation_id)+'.json')
    if journal.exists():
        queue.mark_uncertain(reservation_id, 'existing upload journal requires reconciliation')
        raise UploadError('Existing upload journal: reconcile, do not create duplicate')
    try:
        atomic_json(journal, {'reservation_id': reservation_id, 'phase':'intent',
                              'channel_id':channel_id, 'publish_at':iso(when)})
        request = api.videos().insert(part='snippet,status', body=body, media_body=media)
        response = None
        while response is None:
            try:
                _, response = request.next_chunk(num_retries=0)
            finally:
                if response and response.get('id'):
                    queue.record_video(reservation_id,response['id'])
                uri = getattr(request, 'resumable_uri', None)
                if uri and response is None:
                    atomic_json(journal, {'reservation_id':reservation_id,'phase':'uploading',
                                          'resumable_uri':uri,'publish_at':iso(when)})
        video_id = response.get('id')
        if not video_id:
            raise UploadError('Upload response lacks video id')
        atomic_json(journal, {'reservation_id':reservation_id,'phase':'uploaded',
                              'video_id':video_id,'publish_at':iso(when)})
        return schedule_existing(api,queue,reservation_id,channel_id,now=now if queue.test_clock else None)
    except Exception:
        queue.mark_uncertain(reservation_id, 'upload outcome needs reconciliation; no automatic retry')
        # Do not print API bodies, OAuth tokens or resumable URI in logs.
        raise UploadError('Upload incomplete/uncertain; inspect protected journal and reconcile before retry') from None


def schedule_existing(api, queue, reservation_id, channel_id, now=None):
    now = now or datetime.now(timezone.utc)
    row = queue.get(reservation_id)
    video_id = row.get('video_id')
    if row['channel_id']!=channel_id or not video_id or row['status'] not in ('uncertain','scheduled'):
        raise UploadError('Reconciliation requires the pinned existing video ID')
    own_channel(api,channel_id)
    result=api.videos().list(part='status,processingDetails',id=video_id).execute().get('items',[])
    if len(result)!=1:
        raise UploadError('Existing upload not found')
    video=result[0];status=video.get('status',{})
    if status.get('privacyStatus')!='private':
        raise UploadError('Existing upload is not private; reconcile manually')
    if status.get('publishAt'):
        verify_scheduled(api,video_id,row['publish_at'])
        if row['status']!='scheduled':queue.mark_scheduled(reservation_id,video_id)
        return {'status':'scheduled','video_id':video_id,'publish_at':row['publish_at']}
    processing=video.get('processingDetails',{}).get('processingStatus')
    if status.get('uploadStatus') in ('failed','rejected','deleted') or processing in ('failed','terminated'):
        raise UploadError('Private upload failed processing; no publication attempted')
    if processing!='succeeded':
        return {'status':'private-processing','video_id':video_id,
                'next_action':'Run reconcile later for this same reservation; do not upload again'}
    queue.sync_remote(inventory(api,channel_id,now=now),now)
    row=queue.reschedule_uploaded(reservation_id,now)
    when=timestamp(row['publish_at'])
    actual_now=datetime.now(timezone.utc) if not queue.test_clock else now
    if when<actual_now+timedelta(minutes=15):
        raise UploadError('Slot too near after processing; reconcile again to choose a future slot')
    mutable=('license','embeddable','publicStatsViewable','selfDeclaredMadeForKids','containsSyntheticMedia')
    new_status={k:status[k] for k in mutable if k in status}
    new_status.update(privacyStatus='private',publishAt=iso(when))
    api.videos().update(part='status',body={'id':video_id,'status':new_status}).execute(num_retries=0)
    verify_scheduled(api,video_id,iso(when))
    queue.mark_scheduled(reservation_id,video_id)
    return {'status':'scheduled','video_id':video_id,'publish_at':iso(when)}


class QueueBridge:
    """Adapter to sibling youtube_queue.py; queue owns all state transitions."""
    def __init__(self, module, db, test_clock=False):
        self.module, self.db = module, db
        self.test_clock=test_clock
    def sync_remote(self,manifest,now):
        return self.module.sync(self.db,manifest,now)
    def reschedule_uploaded(self,job_id,now):
        row=self.module.reschedule_uploaded(self.db,job_id,now)
        row["file"]=row["asset_path"]
        return row
    def get(self, job_id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None:
            raise UploadError('Unknown reservation')
        result = dict(row)
        result['file'] = result['asset_path']
        return result
    def begin_upload(self, job_id, channel_id, now):
        row = self.module.begin_upload(self.db,job_id,now)
        if row['channel_id'] != channel_id:
            raise UploadError('Queue channel changed')
        row['file'] = row['asset_path']
        return row
    def mark_uncertain(self, job_id, reason):
        # reason stays generic; queue transition preserves an existing video ID.
        if self.get(job_id)['status'] != 'uncertain':
            self.module.transition(self.db,job_id,'uncertain')
    def record_video(self, job_id, video_id):
        self.module.transition(self.db,job_id,'uncertain',video_id=video_id)
    def mark_scheduled(self, job_id, video_id):
        self.module.transition(self.db,job_id,'scheduled',video_id=video_id)


def queue_module():
    location = Path(__file__).with_name('youtube_queue.py')
    if not location.exists():
        raise UploadError('Sibling youtube_queue.py required')
    spec = importlib.util.spec_from_file_location('dg_youtube_queue',location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def publication_lock(db_path):
    """Serialize full upload/reconcile remote operations across chats/processes."""
    import fcntl
    db_path=Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True,exist_ok=True)
    lock_path=db_path.with_name(db_path.name+'.publish.lock')
    flags=os.O_CREAT|os.O_RDWR
    if hasattr(os,'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd=os.open(lock_path,flags,0o600)
    acquired=False
    try:
        os.fchmod(fd,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise UploadError('Another upload/reconcile is active for this queue; try after it finishes') from None
        acquired=True
        yield
    finally:
        if acquired:
            fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', type=Path, default=STATE/'token.json')
    parser.add_argument('--config',type=Path,default=STATE/'channel.json')
    commands=parser.add_subparsers(dest='command',required=True)
    auth=commands.add_parser('auth');auth.add_argument('--client-secrets',type=Path,required=True)
    auth.add_argument('--expected-handle',required=True);auth.add_argument('--channel')
    sync=commands.add_parser('sync');sync.add_argument('--channel',required=True)
    sync.add_argument('--out',type=Path,required=True)
    upload=commands.add_parser('upload')
    upload.add_argument('--channel',required=True)
    upload.add_argument('--reservation',required=True,type=int)
    upload.add_argument('--db',type=Path,default=STATE/'queue.sqlite3')
    upload.add_argument('--metadata',type=Path,required=True)
    upload.add_argument('--qa-report',type=Path,required=True)
    upload.add_argument('--state-dir',type=Path,default=STATE/'uploads')
    reconcile=commands.add_parser('reconcile')
    reconcile.add_argument('--channel',required=True)
    reconcile.add_argument('--reservation',required=True,type=int)
    reconcile.add_argument('--db',type=Path,default=STATE/'queue.sqlite3')
    args=parser.parse_args()
    try:
        with publication_lock(args.db) if args.command in ('upload','reconcile') else nullcontext():
            if args.command=='auth':
                result=authorize(args.client_secrets,args.token,args.config,args.expected_handle,args.channel)
            else:
                if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',args.channel):
                    raise UploadError('Expected exact YouTube UC channel id')
                config=read_json(args.config)
                if config.get('channel_id')!=args.channel:
                    raise UploadError('Channel differs from saved OAuth pin')
                api=service(load_credentials(args.token))
                own_channel(api,args.channel,config.get('expected_handle'))
                remote=inventory(api,args.channel)
                if args.command=='sync':
                    atomic_json(args.out,remote)
                    result={'snapshot':str(args.out),'complete':remote['complete'],
                            'items':len(remote['items']),'unresolved':len(remote['unresolved_video_ids'])}
                elif args.command=='reconcile':
                    module=queue_module()
                    with module.connect(args.db) as db:
                        bridge=QueueBridge(module,db)
                        result=schedule_existing(api,bridge,args.reservation,args.channel)
                else:
                    metadata=read_json(args.metadata)
                    validate_metadata(metadata)
                    qa=read_json(args.qa_report)
                    module=queue_module()
                    with module.connect(args.db) as db:
                        bridge=QueueBridge(module,db)
                        row=bridge.get(args.reservation)
                        if qa.get('passed') is not True or qa.get('asset_sha256')!=row['asset_sha256']:
                            raise UploadError('QA report must pass and match reserved final asset hash')
                        module.sync(db,remote,datetime.now(timezone.utc))
                        result=upload_scheduled(api,bridge,args.reservation,args.channel,
                                                metadata,args.state_dir)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        print('Error: '+str(exc) if isinstance(exc,UploadError) else 'Error: operation failed; check credentials/network without logging secrets',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
