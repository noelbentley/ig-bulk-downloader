# IG Bulk Downloader — Windows desktop

A native, local PySide6 application around the bundled Instaloader 4.16 engine.
The original `instaloader/` source, CLI, MIT license and attribution are preserved.
No web server, hosted service, telemetry, Docker or external database is used.

## Run

From PowerShell in `W:\ig-downloader` (Python 3.12 x64 used for this build):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt
.\.venv\Scripts\python.exe -m desktop
```

After installation, the last line is the one-command launch. For a console-free
source launch use `.\.venv\Scripts\pythonw.exe -m desktop`.
The current baseline on `main` builds `dist\flat-folders\IG-Bulk-Downloader.exe`.
See [OPTIMIZED_ENGINE.md](OPTIMIZED_ENGINE.md) for its opt-in Instagram pipeline,
worker controls, offline measurements and limitations. The earlier executables in
`dist\performance` and `dist\ig-fast-download` remain untouched.
Its target machine needs no Python installation.

## Use

1. Set the destination (defaults to `D:\Instagram` if D: exists, otherwise your
   Windows Downloads known folder). New folders are created at download time.
2. Select media in Download Options. Photos/videos filter feed media; Reels selects
   the Reels tab. Stories/highlights select all their media and require a session.
3. Start in Public / No Login mode. Optional sign-in under Accounts saves a session
   without automatically activating it. Two-factor codes are supported; complete
   Instagram checkpoints in Instagram. This app does not bypass access controls.
4. Add usernames or Instagram profile URLs, then Start Downloads. Each entry
   captures its destination and options. Pending profiles run sequentially.
5. Pause/resume or cancel selected entries. A current request/item completes first;
   rate-limit waits can be cancelled. Close waits for the worker to finish safely.
   Retry and restart skip media already present using Instaloader's native checks.
6. History can be filtered by outcome, retried, or opened in Explorer.

New downloads use one folder: **`destination\username\media files`**, with no
`posts`, `reels`, extra username or highlight subfolders. Stories use a stable ID
suffix to distinguish them from feed files at the same timestamp. Reserved Windows
device names retain an `@` prefix so they remain valid and cannot collide with
other usernames.

Existing downloaded archives are not moved, renamed or cleaned up. Queue/history
records created before the layout change retain their legacy `@username` folder
and section paths, including on Retry. Newly added jobs use the flat layout and
save that layout with the queue. Re-adding an old profile as a new job writes into
the new folder; existing-file checks apply within that job's folder.

No conversion/recompression is performed. Caption `.txt` and JSON metadata exports
are disabled, including saved options/queue entries that previously enabled them.
The caption checkbox is removed. Existing text and JSON files are left in place.
Inter is used if available,
otherwise Segoe UI/system fallback.
Theme and accent are configurable.

## Public-first authentication

Every application launch starts in **Public Mode — No Login**, even when sessions
exist or an account was previously selected. Public photos, videos, carousels,
profile pictures and Reels are attempted using an anonymous Instaloader instance.
No saved session is loaded, no browser cookies are imported, and no account login
request is made just because credentials are available. Instagram's own initial
anonymous cookie jar can contain empty/default cookies; it does not receive saved
session cookies, CSRF values or authentication headers from an account context.

Stories and Highlights ask for authentication before their own requests. Private
profiles prompt after an anonymous lookup establishes the access requirement.
The nonblocking dialog offers **Login with Instagram**, **Use Saved Session** and
**Skip This Content**. The saved-session button is disabled when none exist.
Choosing Login opens Accounts and waits for an explicit sign-in; it does not send
credentials automatically. Closing the prompt means Skip. You can still pause or
cancel the download worker while it waits for a decision.

Declining Stories or Highlights leaves public files intact and skips only that
collection. Declining a private profile skips the remaining selected content of
that profile, then the queue continues. Permission errors explain that login does
not grant access to an unfollowed private account. Unavailable public content or
an explicit login-required response can offer one authenticated retry for that
content. HTTP 400/403/429, security challenges and network errors do not trigger a
login recommendation or automatic mode switch. Rate, security and network failures
stop the batch; content access failures remain scoped to the affected profile.

The desktop coordinator owns a separate engine, Requests session and RateController
for the anonymous identity and for each approved account. Each context is reused
within the queue run; switching identities does not repeatedly log in or log out.
Its pacing scheduler is shared across all these contexts so file counts and rests
continue without resetting. An anonymous profile always uses the anonymous context
even after another profile used authentication. A private-profile approval covers
only that profile's selected content. Other approvals cover the named collection.

To deliberately authenticate every download in one queue run, select a saved
account in Accounts and tick **Use selected saved session for every download in
this queue run**. This authorization is never persisted and is cleared when the
queue becomes idle. Signing in or selecting an account alone does not authorize
authenticated downloads. The status indicator shows the active mode; a saved
session is verified before it is reported as Authenticated.

Expired/invalid account contexts are closed and excluded from automatic reuse.
Recovery requires another explicit choice to sign in, use a saved session, or
skip. A known-invalid account is not loaded again for later content unless the
user approves a new attempt. There is no repeated background login or blocked-request
retry loop. New saved credentials replace the expired context, preserving isolation.

History records skipped content explicitly. Partial file/item counts include
successful media before an optional retry; existing files are skipped on retry.
Feed/Reels deduplication spans the selected public collections. Anonymous access
remains visible to Instagram through the connection and request patterns, and
does not guarantee availability or protection from rate limits.

## Performance profiles and diagnostics

The latest investigation and measured live results are in [LIVE_THROUGHPUT.md](LIVE_THROUGHPUT.md).
The baseline provides request-level diagnostics, exportable numeric reports and
sequential anonymous CDN connection reuse. The experimental branch additionally
offers [optimized public Instagram downloads](OPTIMIZED_ENGINE.md). Neither removes required media-URL discovery or
claim WFDownloader's throughput. `Settings → Performance → Export diagnostic report`
saves a whitelisted report; the latest GUI run is also saved under the local data
directory in `diagnostics/latest.json`. Request sleeps, actual HTTP time, media
durations/gaps, duplicate checks, GUI and history costs are distinguished.

Settings → Performance provides **Balanced** (default), **Conservative** and
**Custom**. Balanced turns application pacing and long rests off while preserving
Instaloader's rate safeguards. Conservative restores the randomized pacing below.
Custom retains manual settings. Existing saved pacing values migrate to Custom
while the initial selected profile becomes Balanced.

One Instagram profile is active at a time; additional profiles stay queued.
The default Instaloader path uses one transfer. The opt-in public-feed pipeline
supports 1–5 isolated media workers and keeps API discovery/rate accounting serial.
Native fallback remains available for other Instagram operations. The Settings panel shows current transfers, files/minute, protection
status and a persisted last-run numeric performance summary. See [PERFORMANCE.md](PERFORMANCE.md)
for measured before/after results, the concurrency audit, metric definitions, safe
restriction cooldown behavior and a reproducible ordinary-download comparison.

Confirmed throttling stops the failed request and persists a cooldown deadline,
honoring Retry-After seconds or dates. No blocked request is retried automatically.
The next authorized run waits before requests begin. A second confirmed throttle,
or a security challenge, requires manual review in Settings. Acknowledging review
does not shorten a server deadline. There is no IP/session rotation or automatic
authentication fallback.

## Randomized download pacing

Settings → Randomized Download Pacing controls extra breaks after completed media
files. In Conservative it is enabled: batches of 3–8 files, pauses of 15–45 seconds,
and a longer 120–300 second rest after an independent 40–60 file interval. Every
batch size, pause duration, long-rest interval and long-rest duration is drawn
independently, with inclusive bounds. Long rests are enabled in Conservative. Downloads
use one worker on the baseline path; the experimental pipeline drains its bounded
worker wave before pausing, without overshooting a threshold. A long rest replaces a simultaneous batch pause,
then starts a fresh batch and long-rest interval.

All eight numeric bounds and both toggles are stored in local settings. File
counts accept 1–100,000 and durations accept 1–86,400 whole seconds, with minimum
≤ maximum. The GUI keeps paired bounds valid; malformed stored pacing settings
restore defaults with a status message. Controls are disabled during a queue run;
changes take effect on the next Start Downloads.

The dashboard and queue show a remaining countdown. Manual Pause freezes the
remaining duration, including fractions of a second internally; Resume continues
it. Cancel wakes a pacing wait immediately. Cancelling one profile keeps any
unfinished rest for the next profile in the same queue run. Counters continue
across profiles in that run; a new Start or application restart begins a new pacing
schedule. They do not persist across application restarts.

The adapter observes the existing app-owned `InstaloaderContext.write_raw` method
after its original implementation has finished streaming, closed the temporary
file and atomically renamed it. JPG/JPEG and MP4 writes count individually,
including carousel files, stories and profile pictures. Skipped duplicates,
captions, metadata and failed writes do not count. No engine source is changed.
There is no dedicated high-level per-file callback in this engine version; this
documented low-level writer is the narrow boundary used by the desktop adapter.

The file is fully saved before a pacing wait begins. Its timestamp may be finalized
after the wait; cancellation can leave that for a retry. File counts include saved files even when their containing post is not
yet finished. If the final file hits a threshold, the rest completes before the
profile is marked completed. Pacing does not interrupt an active transfer.

Pacing supplements Instaloader's existing RateController. It neither changes rate
accounting nor shortens server-required waits, handles/retries HTTP 429, rewrites
Retry-After headers, recreates sessions between batches, or bypasses authentication.
Rate, security and network failures still stop the batch. Random timing is not a guarantee
against rate limits; restarting a queue must not be used to evade restrictions.

## Local data and sessions

`%LOCALAPPDATA%\IG Bulk Downloader` contains settings, the saved queue, SQLite
history and DPAPI-encrypted session cookies. Sessions belong to the current
Windows user; copying the file to another user/computer is not a login migration.
Passwords and two-factor codes are never written to disk. Logout deletes the
selected local session; it does not revoke other Instagram logins.
Multiple saved accounts are supported. Each approved identity has its own context;
sessions are loaded and checked only after consent. Account controls are disabled
during downloads except when the worker is waiting for a requested sign-in.
Only one app instance uses the local data at a time.

Queue state is persisted at state boundaries; interrupted active jobs return to
Queued on the next launch. History records outcomes and per-run counts, including
partial counts on failure/cancellation. History shows the latest 1,000 attempts.
`.gitignore` excludes runtime data, media, sessions, virtual environments and builds.
Keep downloaded caption `.txt` files and any old metadata files inside archive
folders (`@username`), never beside source files. Do not force-add runtime files to Git.

## Progress and limitations

- The progress bar measures finished profiles. Items checked and newly written
  media files are separate counts. No invented byte percentage or total file count.
- Pause/cancel are cooperative, between items and after completed media writes,
  including individual carousel files. Pacing countdowns can be cancelled immediately.
  Network requests use a 30-second timeout; this is not a total task deadline.
  An engine call may involve several requests. Partial files use engine `.temp`
  writes on the native path; the optimized path uses unique `.part` files and
  publishes complete media without overwriting an existing target.
- Incremental runs re-scan selected collections and skip existing media, rather
  than assume ordering or stop at the first existing post. Reels/feed overlaps
  are deduplicated within a run. Existing caption text is not rewritten.
- Instagram can restrict anonymous access, private profiles, expired sessions and
  APIs. Rate/security/network failures stop the batch and leave pending entries
  queued. Instaloader's normal rate accounting persists per context in the run;
  HTTP 400/403/429 do not prompt for login. A known login requirement/session expiry
  offers explicit recovery; public content can still be unavailable after login.
  Restarting the app is not a way to evade rate limits.
- Stories and highlights select their own photos/videos. Highlights use stable IDs
  for folders; highlight cover images are not currently downloaded. Metadata-only
  queues, browser-cookie imports, exact byte progress and checkpoint automation are
  not implemented. Instagram's API changes can require updating the engine.
- The executable is unsigned. It was built/tested on this Windows machine, not
  certified across every clean Windows 11 installation.

## Tests and build

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m desktop --smoke-test build\qa-source
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
.\dist\flat-folders\IG-Bulk-Downloader.exe --smoke-test build\qa-flat-packaged
```

To build without replacing the existing executable:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -OutputDirectory dist\flat-folders
.\dist\flat-folders\IG-Bulk-Downloader.exe --smoke-test build\qa-flat-packaged
```

The build script refuses to overwrite its target if that executable is running.
This update is delivered in `dist\flat-folders\IG-Bulk-Downloader.exe`; the prior
`dist\IG-Bulk-Downloader.exe`, `dist\pacing\IG-Bulk-Downloader.exe` and
`dist\public-first\IG-Bulk-Downloader.exe`, `dist\performance\IG-Bulk-Downloader.exe`
and `dist\ig-fast-download\IG-Bulk-Downloader.exe`, plus the earlier
`dist\optimized-engine\IG-Bulk-Downloader.exe`, are untouched.
After your current downloads finish,
close the old app normally and launch the new executable. Both use the same local
settings/sessions/history with a single-instance lock. If desired, copy the new
executable over the old one only after both are closed. Do not stop active jobs
just to replace a build. The isolated smoke diagnostic does not use your live data.

The build script creates `dist\flat-folders\IG-Bulk-Downloader.exe` with no console, Qt's Windows
platform plugin and Python bundled. Build from Windows with the lock file for the
tested environment. This is a repeatable dependency-pinned build, not a claim of
byte-for-byte deterministic output. Dependency license notices are bundled.
Review THIRD_PARTY_NOTICES.md before redistribution.

`--smoke-test` opens the native event loop with isolated temporary app data, blocks
network access, exercises queue entry and theme changes, renders all five tabs
and the authentication prompt, diagnostic summary and optimized settings in both themes (16 views), then exits. Check its output `result.json` and PNG previews. It
does not contact Instagram or change your normal app settings or sessions.

Tests mock Instagram: parsing, local storage, encrypted session round trips and
deletion, queue order/retry/failure, worker thread signals, pause/cancel, engine
filtering, destination creation and existing-file handling. Source and packaged
GUI launches should be visually checked. Real Instagram login/2FA, private access,
rate-limit responses and real media downloads still require an authorized account
and manual testing; the offline tests do not prove those live APIs work.
Pacing tests also cover independent inclusive random draws, overlap/reset,
multi-profile continuity, real Instaloader carousel writes, failed transfers,
pause/resume countdown time, cancellation, retry/history and local configuration.
Public-first tests inspect actual Instaloader cookie jars and headers, multiple
account isolation, authenticated-to-anonymous transitions, scoped consent/skip,
GUI saved-session and sign-in actions, expiry, restriction classification and
native media/caption downloads with JSON exports disabled. All HTTP operations in
tests are mocked; these checks do not establish current live Instagram availability.

## Source map

- `desktop/ui/`: main window and light/dark styling
- `desktop/services/`: engine adapter and sequential queue
- `desktop/services/public_first.py`, `errors.py`: content-scoped consent, isolated contexts and error categories
- `desktop/services/performance.py`, `safety.py`: local numeric diagnostics, performance profiles and persisted restriction gates
- `desktop/services/request_trace.py`, `media_pool.py`: per-context HTTP diagnostics and isolated sequential media connection pools
- `desktop/services/optimized.py`, `pipeline.py`: optional Instagram public-feed planning, bounded media waves and persisted controls
- `desktop/optimized_benchmark.py`: independent native/optimized Instagram fixture comparison (network blocked)
- `desktop/live_diagnostic.py`, `live_bulk_probe.py`: explicitly invoked, bounded live diagnostic tools; never run automatically
- `desktop/benchmark.py`: offline native-engine/GUI benchmark (network blocked)
- `desktop/services/pacing.py`, `control.py`: supplemental scheduler and pausable countdown
- `desktop/workers/`: QThread download and account tasks
- `desktop/storage/`: settings, queue, local SQLite history, Windows DPAPI sessions
- `desktop/models.py`: validation, options and job model
- `tests/`: offline desktop test suite; `test/` retains the upstream test pointer
- `IG-Bulk-Downloader.spec`, `scripts/build.ps1`: packaging

No changes to the original engine were required. No commits or pushes are made by
these commands.
