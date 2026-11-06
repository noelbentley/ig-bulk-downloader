# Live throughput investigation — 6 November 2026

Work is on the user-authorized `ig-fast-download` branch. `main` remains at
`4c34789`; no commits or pushes were made. A source snapshot is retained at
ignored `build/before-ig-fast-download.zip`. The active performance executable and
the user's saved queue were not stopped, overwritten or modified.

## What was actually measured

The completed ordinary desktop run already recorded **460 files in 432.740 s,
63.78 files/minute**. Its enumeration phase was 276.198 s (63.8% of elapsed),
media work 152.079 s (35.1%), and discovery 3.663 s. Application pacing,
RateController waits, cooldowns and failed requests were zero. That older report
did not distinguish actual HTTP from native pre-request sleep, so it cannot by
itself explain enumeration cost. The user's 40 files/minute observation is not
treated as a slower result than the earlier 17.7 files/minute.

With permission, three bounded anonymous tests used the newly queued `lilychee`
profile, photos only, no captions, no JSON metadata, no saved session and one media
transfer. Each stopped after 20 saved files, with an additional cap of 12 posts
or 120 seconds at safe boundaries. Test media went to ignored folders under
`build`; the desktop's queued entry and destination were left unchanged. A guard
stops a test if the desktop queue becomes active. No restriction response occurred.

| Live result | Fresh sessions | Interleaved connection comparison | Pooled + actual Qt worker |
| --- | ---: | ---: | ---: |
| Saved media files | 20 | 20 | 20 |
| Elapsed seconds | 22.245 | 18.288 | 11.323 |
| Effective files/minute, including startup | 53.95 | 65.62 | 105.98 |
| Native request sleep | 12.872 s | 10.514 s | 6.250 s |
| Actual HTTP headers + body-read time | 9.178 s | 7.569 s | 4.858 s |
| Media request/write phase | 6.090 s | 4.636 s | 1.693 s |
| Average saved-file transfer duration | 0.305 s | 0.232 s | 0.085 s |
| Average inter-file idle gap | 0.122 s | 0.193 s | 0.055 s |
| HTTP requests / saved file | 26 / 20 = 1.3 | 26 / 20 = 1.3 | 26 / 20 = 1.3 |
| Full-post metadata requests | 4 | 4 | 4 |
| Media GETs | 20 | 20 | 20 |
| Media sessions created | 20 | 11 | 1 |
| Duplicate-check time | 0.000397 s | 0.000448 s | 0.000410 s |
| GUI callback time | Not exercised | Not exercised | 0.010580 s |
| SQLite history update | Not exercised | Not exercised | 0.003211 s |
| Errors, retries, restrictions | 0 | 0 | 0 |
| Application pacing / native RateController / cooldown waits | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

Every sample made 20 media GETs, four full-post queries, one profile-page request
and one startup-page request; all 26 responses were HTTP 2xx. All 20 matching media
files were byte-for-byte identical across all three runs. No recompression or
lower-resolution substitution was introduced.

**Do not attribute the whole 53.95 → 105.98 result to connection pooling.** Native
randomized sleep varied by 6.62 seconds, the network was not invariant, the CDN may
have warmed and the sample is small/carousel-heavy. These are not whole-profile
benchmarks or a guarantee of sustained throughput.

The within-run alternating comparison is narrower evidence: ten fresh-session
GETs averaged **0.251 s to headers**, while ten pooled GETs averaged **0.108 s**.
The pooled arm transferred slightly more bytes (7.68 MB vs 7.01 MB), so this is not
explained by deliberately using smaller images. It supports eliminating per-file
session churn, but it still has order/host/network noise and does not isolate TLS
from DNS, server response time or TCP. No requests ran concurrently.

Raw numeric reports:

- `build/live-throughput-01/report.json`
- `build/live-throughput-ab/report.json`
- `build/live-throughput-pooled-gui/report.json`
- `build/live-bulk-probe.json`

These are ignored local artifacts. They contain aggregate numbers/purpose labels,
not cookies, credentials, tokens, request bodies, signed URLs or account identities.
The CLI outcome records `bounded_stop`, not a failed transfer.

## The measured bottleneck and the metadata distinction

In the first live sample, **native request sleeps consumed 57.9% of total time**,
and HTTP headers/body reads consumed another 41.3%. GUI, duplicate checks and
history are not credible explanations for a 12–17× gap in this measured sample.

The bundled source explains why public enumeration is expensive:

1. `Profile.get_posts()` uses `polaris_ordered_timeline_connection` anonymously.
2. Its node wrapper eagerly calls `Post.from_shortcode()` for every node, before
   the desktop can filter out unwanted post types.
3. `Post.from_shortcode()` immediately obtains full post metadata using the
   `27128499623469141` query. That response provides the actual media URLs,
   carousel children and timestamp used by the unchanged download engine.
4. `InstaloaderContext.do_sleep()` runs before Instagram API/page requests. Its
   existing capped exponential delay averages approximately 1.666 s per call.
   This is separate from `RateController.sleep()` and application pacing; the
   previous report did not instrument it.

All four observed public listing nodes had **zero direct image candidates,
display URLs, thumbnails, video URLs, or timestamps** in the measured fields.
They did have caption fields. Hence disabling caption/JSON exports does not remove
the URL-discovery requests. Without those responses this particular listing path
cannot download the photos. The diagnostic also records listing field-presence
counts so a future payload change can be detected without retaining URLs.

A single, normal anonymous probe of the native richer web-timeline query
`29104886249145907` returned HTTP 2xx but **zero nodes and zero media URL nodes**.
This is evidence that the tested alternative did not supply public bulk media in
this environment, not proof that every possible public endpoint is unavailable.
No further endpoint probing, authentication escalation, identity rotation or
restriction bypass was performed. The request's reason for returning no nodes
has not been established. No replacement bulk path was enabled on that evidence.

## Corrections made on this branch

- Added per-session HTTPAdapter observation, including short-lived GraphQL and
  anonymous media sessions. It counts actual sends (including redirects), timings
  to headers, body reads/bytes, fixed-purpose request counts, response classes and
  transport/body failures. It never stores raw requests or response bodies.
- Added native request-sleep timing, saved-file transfer intervals, inter-file
  gaps, duplicate-check timings, GUI callback time and SQLite history-write time.
  Reports with no new probe data explicitly say HTTP detail is unavailable; older
  reports are not presented as having made zero requests.
- Enabled **anonymous media connection reuse**, the correction supported by the
  live alternating comparison. Only TCP connection pools survive between media
  requests. A fresh anonymous factory creates each identity's media session;
  default cookies are restored between files. Saved authentication cookies/headers
  are never copied into media pools. API discovery sessions and native rate rules
  are unchanged. Each engine/context has its own pool, used serially.
- Skipped/unconsumed media responses are closed before the next request, all pools
  close at engine shutdown, and failed/partial writes retain atomic completion
  behavior. No concurrency or new retry loop was introduced.
- Added whitelisted report export in Settings and automatic local
  `diagnostics/latest.json` after a GUI run. History, captions and ordinary queue
  behavior remain intact. No media metadata JSON exports are re-enabled.

The original `instaloader/` package and MIT attribution are unchanged. Native
request sleeps, RateController accounting, 429/Retry-After handling, restriction
gates, manual review and explicit authentication consent remain in effect.

## WFDownloader comparison: established versus unknown

The vendor's [Instagram tutorial](https://wfdownloader.medium.com/instagram-profile-downloader-save-all-images-and-videos-4c16f3ab7f08)
describes collecting links before creating/starting a download batch, and says it
uses a crawl delay. Therefore transfer-only timing after link collection is not
directly comparable with this app's end-to-end discovery-plus-transfer time.
That tutorial is dated 2021; it does **not** establish the current Large mode's
endpoint, concurrency, image dimensions, quality selection or implementation.

The `.xyz` changelog was inaccessible (HTTP 403) to the research tool. The local
WFDownloader app was detected, but the native UI helper was unavailable; its
active traffic was not intercepted and its source was not reverse-engineered.
Accordingly, no exact claim about its present Large-mode request path is made.
No advice from unrelated downloader sites or user speculation is used as proof.

**Can this architecture reach 500–667 files/minute?** The present public path
cannot be promised to do so. At one photo per post, a metadata request plus its
native pre-request sleep already exceeds the entire 0.09–0.12 s/file budget.
Carousels amortize that cost; the final pooled sample's interval between completions
was 0.128 s (~469/min after the first file), but its end-to-end rate was only
106/min and is not representative of a single-photo archive. It is not evidence
that the goal has been reached. A verified permitted bulk-media listing, or a
separately demonstrated safe scheduler, would be needed to remove the structural
cost. Removing needed URL discovery or silently using thumbnails is not a valid
quality-preserving optimization.

## Controlled live comparison procedure

1. Let current downloads finish normally. Close the old app and launch
   `dist/ig-fast-download/IG-Bulk-Downloader.exe`. Remain in Public Mode, Balanced,
   one profile and one worker. Keep captions, videos and other selections equal
   to the WFDownloader test; do not change accounts merely to avoid a restriction.
2. Use fresh destinations for a new-download comparison. An incremental scan with
   most files already present is a different workload and should be labeled so.
3. In WFDownloader, record **link-search time, download-only time and their total**.
   Record active transfer count, quality mode, downloaded file count/bytes and
   sample dimensions. Compare matching files/dimensions rather than assume Large
   means the same rendition as this app.
4. In IG Bulk Downloader, export Settings → Performance → Export diagnostic report.
   The latest numeric report is also under `%LOCALAPPDATA%/IG Bulk Downloader/diagnostics`.
   It contains actual HTTP requests/file, native/API sleep, media duration/gaps,
   GUI/history/duplicate cost, error/retry counts and all earlier wait metrics.
5. Compare end-to-end totals first, then transfer-only stages. Stop on any restriction;
   do not repeatedly retry, change identities or run both clients at once to obtain
   more samples. A larger ordinary run is needed before asserting sustained rates.

For an explicitly authorized bounded test, with the desktop queue idle:

```powershell
.\.venv\Scripts\python.exe -m desktop.live_diagnostic --profile USERNAME --output build\live-check --max-files 20 --max-posts 12 --seconds 120 --connection-mode pooled --gui
```

Each test needs a new output directory to avoid measuring skipped duplicates. A
request already in flight completes or times out at the existing safe boundaries;
the time budget is not a hard forced-termination deadline. Test folders are kept
for byte/quality verification. No live test starts automatically from the GUI.

## Offline validation

The complete automated suite passed: **130 tests**. The offline native-engine
benchmark also completed (50 posts / 70 media per case, two repetitions, network
disabled). Median local processing was 0.102 s without GUI callbacks and 0.193 s
with the real Qt callbacks; these are synthetic local timings, not Instagram
throughput. Both themes rendered 14 source GUI views, including the expanded
diagnostic report. The packaged build has an independent offline smoke-test mode.

Tests cover request/body timing, numeric-only privacy, session isolation, pool reuse and
cleanup, serialized borrowing, sparse public-list hydration, file intervals,
queue/history, atomic writes, pacing, restrictions and cancellation. Mocked
benchmarks validate overhead and accounting only; they do not establish live speed.
