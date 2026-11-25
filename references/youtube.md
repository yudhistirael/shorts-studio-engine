# YouTube scheduling

Requires macOS/Linux, Python 3.9+, network access, a Google Cloud project with YouTube Data API v3 enabled, and a Desktop OAuth client. The helpers keep token, channel pin, upload journal and SQLite queue under ~/.local/share/shorts-studio-engine/youtube/. Keep these out of version control.

From the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r scripts/requirements-youtube.txt
python scripts/youtube_upload.py auth --client-secrets /path/to/client_secret.json --expected-handle YOUR_HANDLE
```

Use the channel ID returned by auth as CHANNEL_ID. Before any live upload, confirm the audience and synthetic-media flags based on the episode and check the metadata file. Set QA passed only after reviewing the final MP4; asset_sha256 is the exact SHA-256 of that file. See examples.

```sh
python scripts/youtube_upload.py sync --channel CHANNEL_ID --out inventory.json
python scripts/youtube_queue.py sync inventory.json
python scripts/youtube_queue.py reserve --channel CHANNEL_ID --asset final.mp4
python scripts/youtube_upload.py upload --channel CHANNEL_ID --reservation JOB_ID --metadata metadata.json --qa-report qa.json
```

Replace JOB_ID with the reservation ID. The scheduler defaults to 07:00, 13:00 and 19:00 in Asia/Jakarta, at most three releases per local day, with four hours between releases. The upload starts private. If processing is pending, run `python scripts/youtube_upload.py reconcile --channel CHANNEL_ID --reservation JOB_ID` for the same reservation. If upload status is uncertain, inspect the existing upload before any retry. The helper schedules on YouTube when processing is ready, with no persistent background runner. It returns the confirmed video ID and schedule. Thumbnail image and timestamp are delivered separately.

See the official [YouTube Data API](https://developers.google.com/youtube/v3/docs/videos) and [desktop OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app).
