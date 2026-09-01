# MixMill

Build custom workout-class mixes from videos you already own. MixMill finds your
releases, helps you check where each track starts and ends, then joins selected
tracks into one new video. Your original files stay untouched.

**[Download MixMill for Windows](https://github.com/nextthrive/mixmill-class-remixer/releases/latest)**

No video editor required. Windows users do not need Python, Docker, Git, or a
terminal.

## What MixMill does

- Scans a folder and finds workout release videos.
- Detects track boundaries from video chapters or matching music files.
- Lets you preview, rename, trim, accept, or reject detected segments.
- Builds mixes manually or generates them from reviewed releases.
- Plays mixes inside the app.
- Exports a mix as one MP4 video.
- Builds a study PDF from matching choreography-note pages.
- Downloads a complete ZIP containing the video, available study notes, and
  matched music.

MixMill does **not** provide or download workout videos, music, or choreography
notes. You must supply media you are allowed to use. It does not upload media
to a cloud service or modify your originals.

## Windows installation

1. Open the [latest release](https://github.com/nextthrive/mixmill-class-remixer/releases/latest).
2. Download `MixMill-1.0.1-Windows-x64-Setup.exe`.
3. Run the installer. It installs for your Windows account and does not require
   administrator access.
4. If Windows SmartScreen shows **Unknown publisher**, confirm the filename and
   `SHA256SUMS.txt`, then choose **More info → Run anyway**.
5. Open MixMill and choose the folder containing your workout library.

The portable ZIP is an alternative: extract it and open `MixMill.exe`. MixMill
supports 64-bit Windows 10 version 1809 or newer and Windows 11.

## Quick start

1. Put every release in its own folder using the example below.
2. Open MixMill and choose the top-level library folder.
3. Select **Rescan video folder**.
4. Open a release and select **Auto-detect tracks**.
5. Play a few seconds around each boundary. Correct names or times when needed.
6. Mark the release **Curated** after checking it.
7. Open **Mixes**. Create an empty mix or generate one automatically.
8. Add or reorder tracks, preview the result, then export it.
9. Download the video or the complete package.

## Library layout

Keep the video and choreography PDF inside the release folder. Put music files
inside one subfolder. That subfolder can have any name.

```text
Workout Library/
└── BodyCombat/
    └── BodyCombat 92/
        ├── BODYCOMBAT92.mp4
        ├── BODYCOMBAT92ChoreographyNotes.pdf
        └── Music/
            ├── 01 Ready or Not.m4a
            ├── 02 Pum Pum.m4a
            ├── 03 Hope.m4a
            └── ...
```

One copy of each file is enough. MixMill reads the files from this folder; it
does not copy them into its database.

The program folder becomes the program name. The release folder becomes the
release title. Flatter layouts also work, and names can be changed inside
MixMill.

Choreography PDFs may use their original vendor filenames. A name containing
`Choreography` or `Choreo` is preferred when a release folder has several
PDFs. Track headings such as `03A.` are matched to the numbered MixMill track;
the source pages remain unchanged in the generated study document.

Supported video files: MP4, M4V, MKV, MOV, AVI, TS, and WebM. Supported music
files: MP3, M4A, AAC, FLAC, WAV, OGG, Opus, and WMA.

## What is a track segment?

A segment is only a name, start time, and end time saved in MixMill's database.
It is not another video or music file.

```text
02 COMBAT 1
Start: 06:17
End:   12:19
```

MixMill can get these times from chapter markers embedded in the video, by
matching your music files against the video's audio, or from times you enter
manually. Repeating Auto-detect is safe in version 1.0.1 and newer.

## Release and segment states

- **Discovery** — MixMill found the release, but nobody has checked it yet.
- **Curated** — you reviewed the release and confirmed its tracks. Automatic
  mix generation uses Curated releases by default.
- **Rejected segments** — a recycle bin for unwanted segments. Rejected
  segments stay out of normal mixes, can be restored, or can be deleted
  permanently.

## Create and export a mix

Choose **Create empty mix** when you want full control. Choose the generator
when you want MixMill to select tracks for a target duration. Review generated
mixes before exporting them.

- **Fast export** keeps the original video encoding and finishes sooner. Cuts
  begin at a nearby video keyframe, so a short lead-in is possible.
- **Precise export** re-encodes segments for exact cuts and better compatibility
  between different source videos. It takes longer.

Windows downloads open a normal **Save As** window. MixMill stores its settings,
database, backups, caches, and completed exports under
`%LOCALAPPDATA%\MixMill`. Updating or uninstalling the program preserves that
folder.

Need every step explained? Read the **[beginner user guide](docs/USER_GUIDE.md)**.

## Open source and privacy

MixMill is free/open-source software licensed under GNU AGPL version 3 or later.
See [LICENSE](LICENSE). Donations may support development, but never unlock
features or change the license.

Windows packages include FFmpeg under GNU GPL version 3. Each release includes
the matching FFmpeg source ZIP and `SHA256SUMS.txt`.

See [privacy](PRIVACY.md), [support](SUPPORT.md), [security](SECURITY.md),
[Windows build details](docs/WINDOWS.md), and
[third-party notices](THIRD_PARTY_NOTICES.md).

## Quick start — Docker / Portainer on a NAS

Deploy it as a **Repository** stack in Portainer, pointed at this repo.
Portainer clones the repo and builds the image from the `Dockerfile` itself —
no SSH, no registry.

1. Create a dedicated data directory and make its existing contents owned by
   MixMill's unprivileged container user:

   ```bash
   mkdir -p /volume2/docker/mixmill/data
   chown -R 10001:10001 /volume2/docker/mixmill/data
   chmod 700 /volume2/docker/mixmill/data
   ```

   Grant UID 10001 read access to the media library through the NAS ACL; write
   access is neither required nor recommended.
2. In Portainer's stack environment, define the values shown in `.env.example`:
   the exact existing `MIXMILL_MEDIA_PATH`, the dedicated existing
   `MIXMILL_DATA_PATH`, its numeric owner as `MIXMILL_UID` and `MIXMILL_GID`,
   a username, and any non-empty password. Compose deliberately refuses missing
   or mistyped paths.
3. Port `2999` is published on every network interface of the NAS by default.
   Set `MIXMILL_PORT` if that port is already in use. MixMill uses its dedicated
   `mixmill_net` bridge on subnet `10.208.0.0/24`; its container IP is internal
   and is not the address to open from another device.
4. In Portainer: **Stacks → Add stack → Repository**, give it this repo's URL
   and `docker-compose.yml` as the compose path, then deploy.
5. Open `http://NAS-IP:2999` (for example `http://192.168.1.20:2999`), sign in
   with HTTP Basic authentication, hit **Rescan video folder**, and start
   marking tracks. Use the Docker host's IP if Portainer is not running on the
   NAS itself.

App data (track markers, mixes, exported files) lives in
`/volume2/docker/mixmill/data`, so it survives container recreation.

Verify the live mount permissions after deployment:

```bash
docker inspect mixmill --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}'
```

The result must show `/LM false` and `/data true`. Stop the container if `/LM`
is ever reported as writable. Avoid symlinks and nested mount points beneath
the media library.

### Updating

Push to the repo, then hit **Pull and redeploy** on the stack. Portainer
re-clones and rebuilds; the first build takes a few minutes (ffmpeg), later
ones reuse cached layers unless the `Dockerfile` changed.

Note the compose file deliberately has **no `image:` key**. Naming an image
makes Portainer's redeploy try to pull it from Docker Hub, which fails with
`pull access denied` — the image only ever exists on your NAS.

### Without Portainer

```bash
docker compose up -d --build
```

### Without Docker

Needs Python 3.11+ and ffmpeg on the PATH:

```bash
pip install -r requirements.txt
MIXMILL_USERNAME=admin MIXMILL_PASSWORD='a-long-unique-password' \
MIXMILL_REQUIRE_READ_ONLY=0 VIDEO_DIR=/path/to/videos DATA_DIR=./data \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

`MIXMILL_REQUIRE_READ_ONLY=0` removes the operating-system media protection and
is intended only for isolated development against disposable fixtures.

## Export modes

- **Fast** — lossless stream copy, takes seconds. Each segment starts at the
  nearest keyframe at or before your marked start and runs through your marked
  end. The lead-in is however far back that keyframe sits — usually under a
  second, but several seconds on sources with long gaps between keyframes.
- **Precise** — re-encodes every segment (1080p / 30 fps / x264) for
  frame-exact cuts and guaranteed compatibility when mixing releases with
  different resolutions. Slower (roughly real-time or faster depending on your
  NAS CPU).

## Automation and study tools

- **Automatic DB backups** — a snapshot of the database is taken on every
  startup into `/data/backups` (or `<DATA_DIR>/backups`), keeping the 10 most
  recent.
- **Export queue** — exports run one at a time; kicking off a second export
  while one is in progress queues it instead of racing ffmpeg against itself.
- **Purge missing** — one button removes releases whose video file has gone
  missing (moved/deleted on disk), along with their tracks and mix entries.
- **Auto-detect, in the background** — per-release auto-detect (chapters or
  music-folder correlation) runs as a background job so the UI doesn't block;
  a bulk button runs it across every unreviewed release at once and reports
  which ones failed.
- **Chapter markers in exports** — exported mp4s carry a named chapter per
  segment, so scrubbing in a normal video player jumps track-to-track.
- **Songs zip download** — download all of a mix's paired songs as a single
  zip, numbered in mix order.
- **Complete package** — one action creates a frame-accurate mix video, then
  downloads it with available study notes, matched music, and a coverage
  manifest in one zip.
- **Choreography study PDF** — download one indexed document containing the
  matching original choreography and coaching pages from every release used
  by the mix. Missing source notes are listed without blocking available pages.
  Mapping Studio can browse and jump through every source page for manual
  ranges, including bonus labels written as either `05B` or `B5`.
- **Duplicate mix** — clone a mix (tracks, order, and song pairing) as a
  starting point for variations.
- **Smart mix generator** — generate a mix automatically, either from a
  specific program (one track per numbered slot) or in "surprise" mode across
  everything you've reviewed.
- **Player shortcuts** — ← / → seek ±10s, ↑ / P previous track, ↓ / N next
  track, Space toggles pause.
- **PWA note** — Add to home screen works over LAN http (iOS gets a real icon
  via `apple-touch-icon.png`); the real install prompt requires serving MixMill
  over HTTPS (e.g. behind a reverse proxy). The raster icons are committed;
  regenerate them from the SVG geometry with `python tools/make_icons.py`
  (needs Pillow, dev machine only).

## Library, preview, and playback tools

- **Grid view** — Library and Mixes switch between list and poster grid; the
  toggle is remembered. Thumbnails are grabbed from each video on demand and
  cached in `<DATA_DIR>/thumbcache`.
- **Vault** — park releases you don't want in rotation. Vaulted releases
  disappear from the library, the mix picker and the generator, but existing
  mixes that use them keep working. Unvault anytime from the Vault tab.
- **Library sorting** — sort releases by program (grouped, the default), title,
  newest, or track count.
- **Structured surprise mixes** — "surprise me" now builds a class-shaped mix:
  one track per slot number in order (warmup → … → cooldown), filled from any
  program, instead of a flat shuffle that could hand you nine cardio tracks in
  a row. Advanced filters restrict it to chosen programs, a slot window (e.g.
  only 7–9), or a release-number window (e.g. BodyAttack 10–20).
- **Previews everywhere** — every track row in the mix editor and in the picker
  plays its video segment inline (▷) or its matched song (♫), so you can hear
  what you're adding before you add it. Picker rows also show which song is
  paired with each segment.
- **Release-editor player parity** — ±10 s buttons, ← / → seeking, and
  picture-in-picture, in both the library player and the mix player.
- **Voice toggle that works** — switching voice off genuinely swaps to the
  release's music-only audio stream instead of muting. The first time a release
  needs it, extraction runs as a background job with a "preparing voice-free
  audio…" badge while the normal audio keeps playing, and swaps over when it's
  ready. The library's **Prepare voice audio** button pre-extracts the whole
  library so later toggles are instant.
- **Mix housekeeping** — search box on the mixes list, one-click shuffle, and
  slot-order sort inside a mix.

## Notes & limits

- **Authentication is mandatory by default.** HTTP Basic credentials are only
  encoded, not encrypted; use HTTPS at a reverse proxy or a VPN such as
  Tailscale/WireGuard when the network is not fully trusted. Never port-forward
  MixMill directly to the internet.
- Supported containers: mp4, m4v, mkv, mov, avi, ts, webm. The live player
  depends on the browser being able to play the codec (h264/aac mp4 is safe
  everywhere; mkv/hevc may not play in-browser — exports still work fine).
- "Fast" export concatenates via MPEG-TS intermediates, which handles mixing
  segments from different files of the same program well. If sources have very
  different codecs/resolutions, use *Precise*.
- The video folder is mounted read-only — MixMill never touches your originals.
- `/data` must be dedicated to MixMill. The app intentionally creates and
  removes generated exports, export scratch directories, transient song ZIPs,
  thumbnails, extracted-audio caches, and database backups retained beyond the
  newest ten. It refuses to start if unrelated top-level entries are present.
- The production container runs as UID/GID 10001 with a read-only root
  filesystem, no Linux capabilities, no privilege escalation, a private tmpfs,
  and bounded CPU, memory and process counts. Existing root-owned data must be
  reassigned to UID/GID 10001 before upgrading.
