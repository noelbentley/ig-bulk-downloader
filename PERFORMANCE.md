# Performance optimization and validation

**Follow-up:** [LIVE_THROUGHPUT.md](LIVE_THROUGHPUT.md) supersedes the unknown live
bottleneck with measured samples and documents the `ig-fast-download` experimental
branch. Its sequential CDN connection reuse was added after live measurement;
the audit below describes the original integration. The mocked results here remain
local-overhead measurements, not evidence of Instagram throughput.

The desktop defaults to **Balanced**: one active profile, one media transfer,
application randomized pacing off and periodic rests off. Instaloader's own
RateController, normal API request delays, access restrictions and server cooldowns
remain enabled. **Conservative** restores batches of 3–8 files, 15–45 second pauses
and 120–300 second rests every 40–60 files. **Custom** retains editable pacing.
Old saved pacing values migrate into Custom; the new default is Balanced.

## Measured bottlenecks

The prior GUI rebuilt every cell of the entire queue and repainted/logged on every
activity, state, file and item event, even when values were unchanged. Updates are
now coalesced at 100 ms, cells are reused, unchanged values are retained, repeated
mode notifications are suppressed, and final counts are flushed before history
is recorded. Counts are updated immediately in the model; only presentation is
coalesced. Activity shows the latest message in an update, not every engine fragment.

Deliberate application pacing was the largest modeled delay. With a fixed random
seed, 70 new files incurred 564 seconds of batch/long-rest waits. Balanced removes
these extra waits. Native local media processing was already fast in this fixture;
the data does not establish that Instaloader HTTP discovery or live transfers are
the bottleneck.

## Offline before/after benchmark

Measurements on this machine use medians of two runs with 50 posts / 70 files:
every fifth post is a three-photo carousel, with other photos and videos mixed in.
Payloads are synthetic 256 KiB JPEG-sized and 1 MiB MP4-sized byte buffers. The
real Instaloader download methods, temporary writes, atomic replacement, timestamps,
optional captions and real Qt widgets are exercised. HTTP responses are mocked;
all outbound socket connections are blocked. Temporary fixtures stay under `build`.

| Case (70 files) | Before | After |
| --- | ---: | ---: |
| Native local processing, no pacing, no GUI | 0.063 s | 0.084 s |
| Conservative local processing, no GUI | 0.065 s | 0.069 s |
| Conservative waits, simulated without sleeping | 564 s | 564 s |
| Local processing including GUI updates, pacing off | 3.484 s | 0.166 s |
| GUI callback time | 3.371 s | 0.068 s |
| Native raw writing time in the GUI case | 0.048 s | 0.044 s |
| Queue refreshes in the GUI case | 466 | 3 |

The GUI case improved about 21 times in this offline fixture. The additional
instrumentation has small local overhead; small disk/cache timing differences are
expected. These are **not live Instagram throughput measurements** and do not
predict matching WFDownloader. Mocking excludes DNS/TLS, remote bandwidth, actual
pagination/API delays and live rate-limit waits. Pacing durations are modeled from
the real scheduler's random draws rather than actually sleeping for nine minutes.
The approximately 7.45 files/minute Conservative projection includes those waits;
the much higher no-network rates are local-processing ceilings, not expected speeds.

Raw samples are in ignored `build/benchmark-before.json` and
`build/benchmark-after.json`. Reproduce the current benchmark with:

```powershell
.\.venv\Scripts\python.exe -m desktop.benchmark --phase after --posts 50 --repetitions 2 --output build\benchmark-after.json
```

The before file was captured before the optimizations in this pass. `--phase` is
a report label, not a switch that reverts the source. Raw writing time excludes
nested synchronous GUI callbacks in the benchmark; the remaining processing column
contains enumeration, caption/post work and dispatch overhead.

## Why 2–4 workers are not enabled

The source audit found:

- `InstaloaderContext` owns mutable Requests sessions, headers, cookies, GraphQL
  page lengths, token/profile caches and authentication state.
- `RateController` mutates timestamp dictionaries and request deadlines without
  locking. A shared instance cannot safely receive concurrent requests.
- `download_post()` and `download_storyitem()` synchronously call `download_pic()`;
  completion booleans, timestamps, caption writes, seen-post tracking, error handling
  and carousel loops depend on those calls completing before they return.
- `get_raw()` creates an anonymous session per call, but that alone does not make
  the containing loader pipeline safe to parallelize. Its file preparation,
  `.temp` names and progress/pacing hooks are shared.
- Submitting downloads to threads and returning early would report posts complete
  before files are safe, lose exceptions and introduce same-path `.temp` races.
  Waiting on every submitted future would still provide only one effective transfer.

A correct parallel implementation needs a separate planning/transfer/finalization
pipeline with isolated sessions, synchronized rate budgets, filename ownership and
bounded pending work. That is a substantial replacement of the current integration,
not justified by the offline bottleneck measurements. This release therefore
supports **one worker only**; 2–4 are unavailable and unsupported stored values are
rejected/restored to one. Both the coordinator and engine reject overlapping
execution, and the queue owns at most one download worker. The original engine
package is unchanged.

Original Instaloader filename and duplicate behavior are retained for compatibility
with existing archives. Existing same-path media are skipped rather than overwritten;
no parallel writers or shared temporary paths are introduced. This is not a new
manifest-based deduplication/naming system. Existing metadata JSON is left alone;
new JSON exports remain disabled.

## Local diagnostics and restriction handling

Settings → Performance shows the current transfer count and files/minute, plus the
last queue-run summary. It persists only numeric timings/counters and the validated
profile label in local settings. No signed URLs, headers, credentials, cookies,
tokens, usernames or exception bodies are written to diagnostic data. Activity URLs
and credential assignments are redacted, and unexpected errors use generic messages.

The summary includes elapsed time, new media files, average files/minute, discovery,
collection enumeration, media request/stream-write work, post processing, randomized
pacing, native RateController waits, restriction cooldown waits, failed-request
time/count and explicitly approved authenticated retries/time. Files/minute uses
full elapsed wall time, including waits and user decisions. Phase measurements can
include nested request/rate waits; failed/retry timing is a subset of that work,
so phase totals must not be added to reconstruct wall time. Lazy post metadata and
carousel-node parsing occur in post processing. Native profile-picture response
preloading can fall outside the raw header/write phases. No exact wire-byte or TLS
breakdown is claimed. Live transfer counts are sampled; final totals are exact.

HTTP responses are observed on the app-owned context immediately before the existing
engine status checks. HTTP 429 stops requests without retry. Retry-After delta
seconds and HTTP dates are preserved as a minimum wait; absent/invalid 429 headers
use a 15-minute fallback. A Retry-After on another error response is also honored.
Cooldown deadlines are persisted and shared by every anonymous/account context.
Starting another queue or restarting the app cannot discard a recorded deadline.
The next authorized run waits locally before any discovery or transfer requests.
Cancellation wakes the wait; manual pause never shortens the server deadline.

There is no automatic retry or resume of the failed request. Two confirmed
throttling incidents require manual review in Settings. Checkpoint/challenge/feedback
restrictions require review immediately. Acknowledging review does not shorten a
cooldown or automatically start a queue. Generic 400/401/403 do not establish IP
blocking, cause identity rotation or automatically authorize login. The existing
public-first consent rules still apply to genuine authentication requirements.

## Manual live comparison

1. After any current downloads finish, close the old application normally. Launch
   `dist\performance\IG-Bulk-Downloader.exe`. Do not run downloader benchmarks in
   parallel with other Instagram clients.
2. Use one ordinary public profile in Public Mode with a fresh, explicit destination.
   Select the same media options for each comparison. Do not change identities,
   retry restrictions or create excessive duplicate requests to obtain timings.
3. Select Balanced, add the profile and start an ordinary download. At the normal
   end or safe cancellation boundary, read the Settings → Performance summary.
4. A later incremental run skips existing files and is not comparable to a fresh
   transfer. Conservative intentionally adds rests; use its reported pacing time
   to assess that overhead without repeatedly downloading the same archive.
5. Compare file sizes/types, caption settings, discovery/enumeration time, transfer
   time and rate waits before drawing conclusions about WFDownloader throughput.
   If restricted, stop and follow Instagram's cooldown/security instructions.

No live Instagram requests were made for the automated tests or benchmarks.
