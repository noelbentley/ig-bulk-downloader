# WFDownloader architecture investigation

Date: 6 November 2026 (UTC). Investigation: read-only static inspection of the
installed JARs, compared with the local Instaloader integration. No application
implementation changes were made for this investigation.

## Findings

**The installed WFDownloader has a materially different scheduling architecture
from IG Bulk Downloader's current sequential public path.** Its core library
separates link extraction, batches, and individual file transfers. The batch
scheduler submits file-download tasks to an executor and initializes its logical
simultaneous-download allowance to **five**. Its Instagram handler contains
paginated, media-rich response parsers and separate secondary post-resolution
paths, including an optional executor for those lookups.

These findings support concurrency and fewer discovery requests as architectural
explanations for the reported speed difference. They do **not** establish which
paths or settings were active in the user's WFDownloader run, nor how many live
requests it made. The Java language itself is not established as the explanation.

| Question | Finding | Evidence status |
| --- | --- | --- |
| Separate discovery from file downloads? | Link-fetching handlers produce collections of `WebLink`; a separate batch object creates and schedules `FileDownloader` instances. | Confirmed in bytecode; complete runtime ordering was not observed. |
| Concurrent file transfers? | Configurable allowance, constructor default five; queued file tasks submitted to an executor. | Confirmed capability/default; actual user setting unknown. |
| Concurrent discovery? | Instagram secondary-resolution path has a configurable fixed executor; `plfNumConcurrent` falls back to three when that feature is enabled. | Confirmed capability; not necessarily active or used by every mode. |
| Batch media discovery? | Multiple parsers consume JSON `edges`/`items`, handle carousels, and continue with cursors. Some paths directly extract media URLs. | Confirmed implementation; current anonymous endpoint behavior untested. |
| Fewer requests than Instaloader? | Media-rich listing paths can avoid the explicit per-post resolution stage; sparse/Original paths still resolve posts. | Confirmed alternatives in code; live request reduction not measured. |
| Raw versus Large? | Handler explicitly branches on an Original-resolution label. Non-Original paths can use listing-supplied media. Literal Raw/Large labels were not located in the inspected Instagram dispatcher. | Partial; Raw cannot safely be equated with Original. |
| Special faster HTTP client? | Main file-transfer path uses JDK `HttpURLConnection`; supporting HTML code uses jsoup. | Confirmed inspected path; no evidence that an exotic HTTP client causes the speed. |

## Scope and method

Installation inspected:

`C:\Users\noelb\AppData\Local\WFDownloaderApp`

Inspected only packaged application code/resources and the bundled runtime's
`release` file. Used ZIP manifest/class inventories, class constant-pool reference
inspection, and the installed JDK's **`javap -p -c` / `javap -p -verbose`**.
`javap` reported version `1.8.0_392` and successfully read the target classes.
No decompiler download was necessary. Bytecode inspection did not execute the
WFDownloader classes or their initializers.

No installation files were changed. No processes were attached to, stopped,
instrumented, or injected into. No live Instagram or WFDownloader service requests
were made for this investigation. No cookies, credentials, saved sessions,
download lists, signed media URLs, application protections, or licensing mechanisms
were examined. Static request routes were inspected without retaining their query
strings or payloads. Proprietary implementations are not reproduced here or copied
into the Python application. The report records independently written findings,
symbol names, and bytecode locations for verification.

The local inspection helper is an independently written, ignored developer
artifact under `build`; it invokes `javap` and filters output. It contains no
WFDownloader implementation. The report is the only deliverable added outside
ignored build artifacts. No commits or pushes were made.

## Artifacts, libraries, and runtime

| Artifact | Observed role/evidence |
| --- | --- |
| `app\WFDownloaderApp.jar` | 1,735,550 bytes; 580 classes. Manifest main class `org.wfdownloader.app.WFDownloaderApp`. JavaFX FXML views, UI/configuration classes, and help resources. |
| `app\lib\WFDownloader.jar` | 1,522,319 bytes; 821 classes. Contains `FileDownloader`, `ResumableBatchDownloader`, `DownloadResult`, link-fetcher/crawler packages, `InstagramFetchHandler`, HLS, and utility code. Confirms its core-library role. |
| `jsoup-1.22.1.jar` | HTML DOM/parser library. The Instagram post-resolution worker calls a helper returning a jsoup `Document`; the core references `Jsoup` and DOM types. |
| `gson-2.13.2.jar` | JSON library. Instagram parsing directly invokes Gson `JsonObject`, `JsonArray`, and primitive accessors. |
| `fastjson-1.2.83.jar` | Bundled JSON library listed in the desktop manifest. Its presence alone does not establish use in the inspected Instagram transfer path. |
| `commons-io-2.4.jar`, `commons-codec-1.14.jar` | Bundled IO/encoding utilities. Not evidence of transfer concurrency by themselves. |
| `antlr-runtime-4.7.2.jar` | Bundled parser runtime; core resources include HLS and programmable link-fetching grammars. |
| `richtextfx-fat-0.11.7.jar` | Desktop text/UI dependency. Not the media-transfer engine. |
| Bundled runtime | `runtime\release` reports `JAVA_VERSION="26"`; includes JavaFX modules and `java.net.http`. Module availability does not prove the app uses its HTTP client. |

Both principal manifests report Ant 1.10.15 and a build JDK of Azul 21.0.1+12-LTS.
The inspected app entry, batch, file-downloader, and Instagram-handler classes
have class-file major version **52** (Java 8 format). Build JDK, class format,
inspection-tool version, and bundled runtime version are distinct facts.

SHA-256 identifiers:

| JAR | SHA-256 |
| --- | --- |
| `WFDownloaderApp.jar` | `55511F26677F44A6488D6A09395574FFF5B4BD33F51D25A4EB8D4ACFC07D5441` |
| `WFDownloader.jar` | `ACFF16E2B14B914BE28C8634A27784CF674C540CAEAFF9DA15FAC4CC5E7DE049` |

These hashes identify this installed build; findings should not be assumed to
apply unchanged to every WFDownloader version.

## Confirmed downloader scheduling model

The independently summarized object flow is:

1. A site-specific link fetcher extracts `WebLink` collections, including addresses
   and file/referrer properties. Instagram has its own handler and post model.
2. `ResumableBatchDownloader` accepts link collections. Its internal
   `T(WebLink)` method constructs a `FileDownloader` and supplies timeouts,
   file-exists policy, retry configuration, and an item-delay setting.
3. Each `FileDownloader` implements `Callable<DownloadResult>`.
4. The batch scheduler polls its queue, checks a concurrency allowance, and submits
   individual file tasks through `ExecutorService.submit(Callable)`.
5. A batch listener/controller handles results and further scheduling. Separate
   executor references also exist in progress/result and UI work; these must not
   be counted as additional media transfers.

Specific evidence:

- `ResumableBatchDownloader(String, boolean)`: bytecode offsets 28–36 initialize
  internal pool capacity `ajp` to **500** and `allowedNumOfConcurrentDownloads` to
  **5**. Offsets 68–69 initialize `downloadItemDelay` to zero.
- `c(Queue<...>)`: offsets 31–39 create a fixed pool from `ajp`; 51–60 read the
  scheduler count and configured allowance; 120–123 compare those counts;
  134–140 poll an item; 182–195 get its `FileDownloader` and submit it.
- `L(List<...>)` also submits file tasks to the executor, including eligible
  restart/retry items. `setAllowedNumOfConcurrentDownloads(int)` changes the
  logical allowance under synchronization.
- `T(WebLink)`: offsets 484–490 construct the file downloader; 558–564 propagate
  the batch's item-delay value to it.
- `org/wfdownloader/app/ui/SettingsBox.fxml` includes controls labeled
  “Allowed number of simultaneous downloads” and “Maximum number of crawls at
  the same time.” These are distinct settings.

**500 is an internal pool-capacity setting, not evidence of 500 active media
downloads.** Five is the core constructor default, not a measurement or a claim
about the application's saved preferences. UI configuration or site behavior can
change what actually runs. A zero default item delay does not imply the absence
of crawl waits, error backoff, per-site restrictions, or manual pauses.

The separation supports preparing a link batch before file transfer, unlike the
Python app's interleaved post discovery/download loop. Static inspection does not
prove that every WF workflow waits for all discovery to finish before any transfer.

## HTTP request architecture

The main inspected `FileDownloader` path obtains a `HttpURLConnection` through
`org.wfdownloader.lib.utils.r`, checks response status/type/length, obtains an
input stream, and writes through a `BufferedOutputStream` using a byte buffer.
This is streaming file IO, rather than a JavaFX-mediated transfer.

Evidence locations:

- `FileDownloader.b(WebLink, String)`: offsets 459–479 call the connection helper;
  491 obtains the response code; 1295 obtains the input stream.
- `FileDownloader.a(File, boolean, InputStream)`: buffer allocation at 24–28;
  input read at 136 and buffered output writes at 98/174.
- `utils.r.a(String, Map, String, PostData, int, int, int)`: `URL.openConnection`
  at 63, connect/read timeout setters at 613/620, method setter at 630.
- The connection utilities contain cleanup/disconnect and delay paths. The
  Instagram handler itself also calls `Thread.sleep` via `ar(int)`.

Reference scans of both principal JARs found direct `java.net.HttpURLConnection`
and jsoup references, and no direct `okhttp3`, Apache HTTP client, or
`java.net.http` client references. This finding is limited to those JARs and direct
references: it does not rule out reflection, browser-assisted behavior, or other
dependency internals.

There was no packet/connection measurement. HTTP protocol version, socket reuse,
TLS reuse, negotiated compression, network latency, and live requests per saved
file remain **unmeasured**. A `HttpURLConnection` reference is not sufficient to
claim better connection reuse than Python Requests. The experimental Python
branch already reuses media connections.

## Instagram-specific discovery

The core includes
`org.wfdownloader.lib.linkfetcher.customhandlers.impl.InstagramFetchHandler`,
its nested configuration/post objects, and a secondary worker `impl.u`.

Confirmed behavior in the handler:

- Reads collections from response `edges`/`items` rather than having only a
  single-post entry point.
- Supports cursor progression with `page_info`, `has_next_page`, `end_cursor`,
  and in other paths `more_available`/`next_max_id`.
- Extracts `display_url` or `image_versions2.candidates[0].url` from media-rich
  objects, and expands `carousel_media`. Video paths also read `video_versions`.
- Builds collections of `WebLink` for downstream processing. The post model
  includes dimensions and expirable media/carousel URL fields. Only field names,
  not actual values, were inspected.
- Contains more than one retrieval/parsing path, including static route families
  for web GraphQL, profile information, post pages, and mobile API feeds/media.
  Their presence does not establish anonymous access or present-day success.
- Contains a fallback/secondary post-resolution mechanism. Its fixed executor
  submits `impl.u` workers, which fetch an HTML document and call back into the
  handler's post parsing. Thus not every mode avoids per-post requests.

The secondary worker count is read through `InstagramFetchHandler$b.getNumConcurrent`.
The dispatcher reads `plfNumConcurrent` with a fallback of **3**, and separately
checks whether that concurrent lookup feature is enabled. The executor receives
that configured count, submits jobs, shuts down, and waits for completion.
This is discovery concurrency, separate from the batch's transfer allowance.

The handler also has explicit crawl-delay plumbing. Its entry point raises a
detected logged-in crawl delay to at least **13,000 ms** if it was lower. This is
evidence against assuming that WF always omits waits. It is not a timing measurement
of public mode, and does not justify changing our native safeguards.

No endpoint from the JAR was replayed or ported into the Python application.
Current reachability, required permissions/session, pagination limits, and media
completeness were not tested. Existing branches may include compatibility or
fallback code that is no longer used successfully.

## Raw versus Large: established and unresolved

The exact user-visible **Raw** and **Large** labels could not be mapped fully from
the inspected packaged code. No saved/user site configuration was read to resolve
that ambiguity. In particular, **Raw must not be silently equated with Original**.

What the Instagram resolution dispatcher does establish:

1. It reads the `instagram.com.Resolution` choice.
2. At dispatcher offsets 563–583 it compares that choice with the literal
   **“Original (very slow and might trigger Instagram)”** and sets an Original flag.
3. Offsets 585–628 check a separate `allowOriginalsMode` property and can clear
   that flag. The flag is retained as configuration field `axT`, read by `AK()`
   and synthetic accessor `f(...)`. This guard was observed, not changed.
4. Multiple listing methods branch on that flag. For example,
   `a(String, String, Map, InstagramFetchHandler$b)` at 423–438 routes Original
   through a secondary edge-to-post-link resolver. Its non-Original branch at
   454–503 uses the response-array parsers directly.
5. The direct media parsers include `display_url` and the first image candidate
   from `image_versions2`. That is an observable extraction rule, not proof of a
   fixed image width, original upload quality, or equivalence to Instaloader output.

Consequently, a Large choice supplied to this dispatcher would not set the exact
Original flag. That supports a potentially cheaper listing-based path **when the
selected retrieval path supplies media-rich responses**. It does not establish
that Large is always a particular size, always public, or always free of per-post
requests. One inspected listing variant unconditionally takes a resolution/fallback
branch even when the Original flag is false, so the resolution label alone does
not determine total request count.

The executor's enable flag is `axN`/`AI()`/accessor `p(...)`; it is **different**
from the Original flag. Parallel secondary resolution must not be presented as a
feature controlled solely by the Original/Large choice.

## Comparison with the current Instaloader integration

| Dimension | Installed WFDownloader: static finding | IG Bulk Downloader: local source/previous measurements |
| --- | --- | --- |
| Transfer scheduling | File tasks submitted concurrently; logical allowance defaults to five. | One file transfer, one active profile. No transfer concurrency added. |
| Discovery scheduling | Separate link-fetching subsystem; optional parallel post resolution. | Public posts enumerated and downloaded serially. |
| Public listing hydration | Rich-response parsers can extract media directly; alternate paths resolve individual posts. | Anonymous `Profile.get_posts()` wraps each node with eager `Post.from_shortcode`. |
| Carousel handling | Extracts carousel URLs into link collections. | Downloads carousel children individually through the original engine. |
| Request waits | Crawl-delay, worker-delay and other wait paths exist; active values unknown. | Native API pre-request exponential sleep plus native RateController and app restriction gates. |
| Metadata exports | Separate post model exists; export behavior not assessed. | Caption/JSON exports off in the tested photo configuration; URL discovery still required. |
| HTTP | JDK `HttpURLConnection` in inspected transfer path. | Requests/urllib3; media connection reuse now enabled on the experimental branch. |
| Exact public-mode requests/file | Not measured. | Prior bounded samples: 26 HTTP requests for 20 files, including four full-post queries and two startup/profile requests. |

Relevant Python evidence:

- `instaloader/structures.py:1403`: `Profile.get_posts`; anonymous node wrapper
  at 1424 calls `Post.from_shortcode`, which eagerly obtains full metadata.
- `instaloader/structures.py:203` and `:413`: shortcode construction and the
  full-post query; actual media/carousel discovery is not disabled by JSON export
  settings.
- `instaloader/instaloadercontext.py:395`: native capped exponential pre-request
  sleep, separate from RateController waiting.
- `desktop/services/engine.py:201` and `:215`: enumeration and download loop;
  `save_metadata=False` remains in effect. This investigation made no edits there.
- [Previous live investigation](../LIVE_THROUGHPUT.md): four observed anonymous
  listing nodes lacked image candidates/direct URLs, requiring post hydration.
  One richer anonymous-query probe returned zero nodes. That does not establish
  whether WF uses that query or whether another permitted response can supply
  equivalent media.

## Performance interpretation

The requested comparison is the user's **approximately 2,000 images in 3–4
minutes** with WFDownloader (500–667/min) versus **approximately 40/min** with IG
Bulk Downloader: a **12.5–16.7×** gap. Those observations are not a controlled
paired benchmark; neither figure was remeasured for this static investigation.

Two explanations now have concrete architectural support:

1. **Transfer overlap.** Five simultaneously scheduled files can hide independent
   network waits. Illustratively, five workers averaging 0.45–0.60 seconds each
   would yield about 500–667 completions/minute in a steady-state, transfer-only
   model with sufficient bandwidth and no discovery stalls. This is arithmetic,
   not a measured WF latency or a throughput forecast. Five workers alone do not
   imply a 12–17× improvement over a serial pipeline.
2. **Avoided post hydration.** A rich listing can supply multiple posts and their
   carousel URLs in one response, while our current anonymous wrapper requests
   full details for each post. Eliminating those extra requests where their
   information already exists also avoids waiting before those requests.

For intuition only, a listing with `K` usable posts/page could need approximately
one page request per `K` posts plus the media GETs, rather than that same listing
cost **plus one detail request per post**. Startup, retries, redirects, URL expiry,
extra checks and missing fields would increase those totals. WF's actual `K` and
requests/file are unknown; the conditional example is not a measured count.

Discovery may also occur before the download clock the user observes. A comparison
must include both link search and transfers, while also reporting them separately.
Static separation makes this a plausible accounting difference, not proof that the
user timed the programs differently.

Our earlier bounded pooled run reached about **106/min**, with shorter native
random waits than earlier samples; it did not establish sustained performance.
The prior ordinary 460-file record was about **64/min**. These qualify the local
evidence without replacing the user's requested 40/min baseline. Earlier results
and mocked benchmarks do not establish parity with WF.

There is no basis here to claim that further GUI tuning will close this gap, that
the Java runtime alone is faster by this amount, or that the present unchanged
serial anonymous Instaloader path can approach 500–667/min end-to-end. Equally,
this investigation does not prove that all uses of Instaloader are fundamentally
limited to the user's observed speed.

## Potential independent improvements for later review

No changes below have been implemented by this investigation. They follow the
findings and would need separate design and validation.

| Candidate | Evidence motivating it | Required demonstration before adoption |
| --- | --- | --- |
| Consume a permitted media-rich listing directly when available | WF has direct array-to-media parsers; our sparse wrapper hydrates every post. | Current authorized public/session behavior, pagination completeness, matching quality/carousels, and actual reduction in requests. Retain fallback when required fields are absent. |
| Separate discovery from a bounded media-transfer queue | WF passes links into an independently scheduled batch. | Backpressure, URL-expiry handling, accurate progress/history, safe cancellation, bounded memory and fair timing of both stages. |
| Consider bounded CDN transfer concurrency | WF's core explicitly schedules multiple files. | Independent proof of session/worker safety and centrally coordinated restriction handling. Keep Instagram API accounting under its existing safeguards; stop globally on restrictions. Do not simply share the current mutable Instaloader instance among threads. |
| Make quality selection measurable | Non-Original paths use supplied media candidates; labels alone do not prove equal quality. | Compare dimensions, bytes, completeness and representative image content. No silent thumbnail substitution. |
| Keep diagnostic timing split by stage | Discovery timing may be separate from visible transfer timing. | Include total time, search time, transfer time, HTTP requests, active workers and all wait classes in comparisons. |

Metadata exports are already off in our tested configuration. Further export/UI
tweaks have substantially weaker support than discovery/request strategy and safe
transfer scheduling. Removing required URL discovery is not an implementation of
the rich-listing alternative. No proprietary class, query implementation, or
restriction-handling strategy should be copied to implement these candidates.

## Evidence limitations and next observations needed

- This is static evidence of capability and control flow, not a trace of the
  successful 2,000-image WF run. Code may be optional, fallback-only or obsolete.
- Actual simultaneous transfers, discovery workers, session state, selected fetch
  mode, request counts, waits and output dimensions are unknown. No saved settings
  or account data was read to infer them.
- The exact Raw label remains unmapped. Large-to-non-Original dispatch is a
  conditional inference from the observed comparison, not a complete current UI
  mode specification.
- No authenticated/private retrieval was attempted; endpoint strings do not show
  that access is public or permitted. No guards were disabled or bypassed.
- No packet capture, proxy interception, endpoint replay, live benchmark, rebuild,
  automated app test, or application source change was needed for this report.
- A fair user-run comparison should record search and download-only times,
  simultaneous-file count, output count/bytes/dimensions, quality and authentication
  selections, and total elapsed time. Run clients separately and preserve all
  server restrictions. Actual requests/file would require a separately scoped,
  privacy-preserving measurement; it cannot be recovered from bytecode alone.

The strongest result is now a verified architectural difference: **a configurable
concurrent file scheduler plus multiple Instagram discovery/hydration paths**, as
opposed to our current serial discovery/download path. How much each contributes
to the user's observed gap remains to be measured.

## Reproducing the static checks

Run standard inspection tools against the installed JARs without launching their
main classes. For example, in PowerShell:

```powershell
$wfResearchRoot = 'C:\Users\noelb\AppData\Local\WFDownloaderApp\app'
Get-FileHash -LiteralPath "$wfResearchRoot\WFDownloaderApp.jar", "$wfResearchRoot\lib\WFDownloader.jar" -Algorithm SHA256
javap -classpath "$wfResearchRoot\lib\WFDownloader.jar" -p -c org.wfdownloader.lib.ResumableBatchDownloader
javap -classpath "$wfResearchRoot\lib\WFDownloader.jar" -p -c org.wfdownloader.lib.FileDownloader
javap -classpath "$wfResearchRoot\lib\WFDownloader.jar" -p -c org.wfdownloader.lib.linkfetcher.customhandlers.impl.InstagramFetchHandler
```

Offsets refer to methods in the hashed core JAR and are not global file offsets.
Several methods are overloaded and their names are obfuscated, so the parameter
types matter. Manifest/resource evidence is available by reading entries directly
from the JAR ZIPs without extracting them into the installation. The original
package and class boundaries were inspected as supplied; no deobfuscation or
protection removal was performed.
