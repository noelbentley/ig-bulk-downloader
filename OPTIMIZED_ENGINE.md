# Experimental Instagram public-download pipeline

Development branch: `feature/optimized-download-engine`.
Working desktop baseline on `main`: `2a3ee0fecac314d343e3e220d3473a609769e5d6`.
The original Instaloader package is unchanged. This is an independently developed
Instagram implementation; no WFDownloader code or endpoint implementation was copied.

## Selecting a pipeline

Launch `dist\flat-folders\IG-Bulk-Downloader.exe`, after closing any other
IG Bulk Downloader instance normally. Settings → **Optimized Public Downloads —
Experimental** enables the public-feed pipeline and selects **1–5** media workers.
The optimization is **off by default**; its enabled default worker selection is
three. Disabling it retains the working Instaloader path.

Public feed photos/videos, including individual carousel children, are eligible.
Caption text and JSON exports are now always disabled, including legacy saved
settings. New jobs write directly into `destination\username`; existing jobs retain
their old folder layout, and existing media/text files are not moved or deleted.
Public-first consent remains in place. Profile
pictures, Reels, Stories, Highlights, authenticated/private operations and custom
native filename/slide options retain Instaloader. A mixed queue may use both paths,
section by section, without losing its totals. No unrelated download-site support
has been added.

Settings are saved locally and captured once per queue run. The current run is not
changed by later preferences. Existing queue/history/session formats remain usable.
Saved sessions are never automatically selected or copied to media workers.

## Discovery

Instagram API/page discovery stays on the existing background coordinator thread,
using the same native profile lookup, pagination queries, native request sleeps,
RateController and restriction gate. Only one Instagram profile is active.
The new pipeline does not rotate endpoints, request identities or sessions.

On the bundled Instaloader **4.16**, a guarded adapter at `NodeIterator._node_wrapper`
can consume a complete media-rich node before native eager shortcode hydration.
It validates timestamp, selected URLs/types and every selected carousel item. It
accepts native web-style or mobile-style response fields, uses the same public
`display_url`/first native candidate, and preserves the URL without rewriting it.
Thumbnails are not substituted. Missing fields, incomplete carousels, other media
formats or unrecognized media hosts retain native hydration/download behavior.

This is an adapter at a **private, version-specific construction boundary**, not a
new supported Instagram endpoint. The native iterator's query/pagination behavior
is not replaced. The guarded rich-node shortcut is disabled if the bundled version
changes; ordinary native iteration and hydration still work. Tests exercise the
real iterator/newest-post callback and both rich and sparse payloads.

The current anonymous response observed in the earlier live investigation was
sparse. This implementation does **not** make it rich: it still needs one native
post-detail request per sparse post. No unverified alternative public endpoint was
enabled, and no new live endpoint probe was made during this development.

Discovery prepares at most **12 posts per window** before draining their selected
media. It does not collect the entire profile's signed URLs or persist a URL cache.
Post IDs/seen paths remain in memory for duplicate and progress accounting, as in
the existing queue. Very large profiles still have bookkeeping proportional to the
number of encountered posts/paths; only the pending URL window is bounded.

## Transfer scheduling and restrictions

The independent scheduler dispatches waves of at most the configured worker count.
Each worker uses an exclusively leased, anonymous Requests session with its own
connection pool. Default anonymous cookies are restored between tasks; authenticated
API sessions and mutable Instaloader objects are never shared with workers.
Connections survive between waves; the pool closes after that profile section.

Only HTTPS `cdninstagram.com` / `fbcdn.net` media hosts are eligible for parallel
transfer. Bounded redirects must remain on those media hosts. HTML/API/login routes
cannot enter the worker pool. Media responses must match the planned JPG/MP4 type;
known content length and nonempty-body checks precede publication.

All Instagram API accounting remains native and serialized. Every media response
passes the shared restriction gate before status handling. HTTP 429 and server
Retry-After stop further admission, persist the restriction through the existing
queue, and are never retried automatically. An observed restriction takes priority
over another in-flight network error. Already admitted requests can be in flight
when a restriction arrives; the scheduler cannot retroactively prevent them.
Other failures also stop the current group rather than create an automatic retry
loop. Existing explicit queue Retry remains available after restrictions permit it.

One profile at a time does not imply one media transfer: only the experimental
public section can have 1–5 concurrent media files. Native fallback remains serial.

## Pause, cancellation, pacing and file integrity

- Manual pause prevents the next wave. Already active files finish normally, then
  the coordinator waits for Resume. It does not deliberately stall an active stream.
- Cancellation prevents new work and checks active streams between chunks. A request
  blocked waiting for network data must return or hit the existing request timeout;
  cancellation does not forcibly terminate a socket or worker thread.
- Each successful file updates progress once. Failed/skipped files do not increment
  media totals; a post finishes only after its selected files finish or are already
  present. Successful files remain counted if a later wave fails.
- Unique `.part` files in the destination are published atomically only after the
  full body and timestamp are ready. Publication refuses to overwrite a target that
  appeared while downloading. Failed/cancelled partials are removed. An OS/process
  crash can leave an uncounted orphan `.part`; retry creates a new partial and skips
  complete existing media. It does not automatically delete unrelated files.
- Timestamp/carousel filenames match the default Instaloader naming. New jobs use
  one username folder; older saved jobs retain their section folders. Duplicate
  detection uses existing paths, matching baseline semantics; this is not a content
  hash deduplicator for renamed files elsewhere.
- Supplemental pacing still counts successful JPG/MP4 files and spans queue profiles.
  Admission is capped at the remaining batch/long-rest threshold, so concurrent
  waves cannot overshoot it. All active transfers drain before a countdown begins.
  Long rests replace overlapping batch pauses. Existing pause/resume countdown
  preservation and immediate countdown cancellation remain in use.

## Diagnostics

Settings retains numeric report export and automatic `diagnostics/latest.json`.
New counters distinguish rich posts, native hydration, fallback sections/posts,
discovery windows, optimized successes/duplicates/failures and configured/peak
workers. The report never stores cookies, credentials or signed media URLs.

`transfer_seconds` measures the union of active transfer time. Average per-file
transfer duration is a separate sum/sample metric; overlapping durations must not
be added to wall elapsed time. Worker completion intervals are accounted in time
order after each wave. Existing coarse discovery/enumeration phases can include
API sleep/HTTP; those diagnostic subcategories must not be double-counted.

## Offline comparison

Command:

```powershell
.\.venv\Scripts\python.exe -m desktop.optimized_benchmark --posts 24 --repetitions 3 --output build\optimized-comparison.json
```

The comparison runs the baseline `EngineService` and optimized service separately,
with real native `Post`/`NodeIterator`, Requests adapter observation and file writes.
All networking is disabled. Every case contains the same 24 posts / **34 files**,
including carousels/videos. It uses 8 KiB deterministic payloads, modeled 5 ms API
response time plus 5 ms pre-request sleep, and 20 ms media response time. The
fixture alone substitutes native waits/rate accounting; production does not.
Results below are medians of three repetitions on this computer.

| Fixture / pipeline | Enumeration | Media wall time | Total elapsed | HTTP requests | Detail requests | Peak transfers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Rich / baseline | 0.325 s | 0.772 s | 1.121 s | 60 | 24 | 1 |
| Rich / optimized, 1 | 0.027 s | 0.755 s | 0.809 s | 36 | 0 | 1 |
| Rich / optimized, 3 | 0.027 s | 0.292 s | 0.334 s | 36 | 0 | 3 |
| Rich / optimized, 5 | 0.027 s | 0.205 s | 0.244 s | 36 | 0 | 5 |
| Sparse / baseline | 0.328 s | 0.777 s | 1.129 s | 60 | 24 | 1 |
| Sparse / optimized, 1 | 0.330 s | 0.754 s | 1.112 s | 60 | 24 | 1 |
| Sparse / optimized, 3 | 0.324 s | 0.292 s | 0.633 s | 60 | 24 | 3 |
| Sparse / optimized, 5 | 0.327 s | 0.207 s | 0.553 s | 60 | 24 | 5 |

The rich fixture avoids 24 detail requests: **40% fewer total requests** and about
**4.59×** lower modeled total time with five workers. The sparse fixture makes the
same requests as baseline; overlapping transfers reduces modeled total time about
**2.04×**, leaving discovery unchanged. Exact file sets and SHA-256 bytes matched
the baseline in every repetition. Native and optimized request probes matched the
independent fixture's counts.

**These are mocked transport results, not live Instagram improvements.** They do
not establish 500–667 files/minute, a new anonymous rich endpoint, equal behavior
under real restrictions, or sustained throughput. Real API sleeps, latency,
bandwidth, media size, URL expiry and server limits can dominate. The sparse case
is particularly relevant to the payload observed earlier. No live comparison was
run during this development and no new speed claim is made from these timings.

The compact numeric summary is in
[`research/optimized-engine-benchmarks.json`](research/optimized-engine-benchmarks.json).
Full local samples are ignored under `build`.
These measurements predate the flat-folder/text-export update; they are historical
synthetic engine measurements, not a performance measurement of the layout change.

## Validation and packaging

The current full suite passed **194 tests**, including optimized-path cases and
flat-folder/legacy-path/text-export regression checks. Checks cover actual worker overlap, session isolation,
serialized API discovery, byte/path equivalence, pagination, media selection,
duplicate/collision behavior, partial bodies, local disk errors, restrictions,
pause/resume, cancellation, pacing thresholds and continuity, explicit private
consent/native fallback, queue/history, config persistence and version fallback.
The original package emits existing `utcfromtimestamp` deprecation warnings in
native paths; it was deliberately left unchanged.

Source GUI smoke checks render **16 views** across light/dark themes, including
the enabled five-worker settings and full diagnostic summary, with networking
blocked and temporary settings. Frozen smoke checks use the same isolated mode and perform a four-file native/optimized
comparison with identical bytes, seven versus five requests, and two overlapping workers.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m desktop --smoke-test build\qa-optimized-source
.\scripts\build.ps1 -OutputDirectory dist\flat-folders
.\dist\flat-folders\IG-Bulk-Downloader.exe --smoke-test build\qa-flat-packaged
```

The original executables in `dist`, `dist\performance`, `dist\ig-fast-download`
and `dist\optimized-engine`
are not replaced. Build checks refuse to overwrite an executable that is in use.
Close an existing application normally before launching the experimental build;
the ordinary application lock prevents two active copies sharing queue/history.

For a later controlled live comparison, use fresh destinations, identical public
profile/content/quality, Balanced pacing and one client at a time.
Run baseline and optimized with one worker first; then compare a chosen higher
worker count during ordinary authorized use. Record discovery, transfer and total
elapsed time, bytes/count/dimensions, requests and every wait category. Stop on
restrictions; do not repeatedly retry or change sessions to evade them. Export
both diagnostic reports. Do not treat time spent waiting for user consent as
network throughput or compare pre-discovered transfers with full discovery time.

## Git boundaries

The working desktop implementation was tested, committed and pushed to `main`
before this branch was created. All optimized implementation, tests, settings and
build changes remain on `feature/optimized-download-engine`. No merge to `main`
is authorized or performed. Generated media, credentials, sessions, local runtime
data, benchmark working directories and executables are ignored and not committed.
