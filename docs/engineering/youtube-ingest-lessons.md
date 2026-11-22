# YouTube ingest reliability

## Incident and confirmed causes

The first pilot URL reached yt-dlp in the Linux container on Windows Docker Desktop. Python 3.12, FFmpeg, ffprobe, and the initialized database were available. yt-dlp `2026.10.26` reported `No supported JavaScript runtime could be found` followed by `Requested format is not available`. Montaj showed `The download failed.` The format selector failed before a media transfer or FFmpeg normalization. The subprocess wrapper discarded yt-dlp stderr before the ingest error handler could classify it.

The image installed the plain yt-dlp wheel with `--no-deps` and contained neither an enabled JavaScript runtime nor the wheel's optional EJS solver. The selector `best[ext=mp4]/best` only selected a single combined video and audio format. It could reject a source with usable separate tracks. In a controlled metadata-only replay on the incident URL, the EJS package stayed installed while Deno availability and the selector changed.

| Runtime | Selector | Exit | Result |
| --- | --- | --- | --- |
| Disabled | `best[ext=mp4]/best` | 1 | Missing runtime warning and unavailable format error |
| Deno | `best[ext=mp4]/best` | 0 | Combined format `18` |
| Disabled | `bv*+ba/b` | 0 | Separate formats `399+251`, with the missing runtime warning |
| Deno | `bv*+ba/b` | 0 | Separate formats `399+251` |

Both changes independently restored format selection on this replay. Deno restored a format the original selector could use, and the new selector used separate streams that remained visible without the runtime. YouTube can change the formats it exposes, and these results describe the observed run. The upstream release pins yt-dlp-ejs `0.8.0` for its solver. The [tagged package configuration](https://github.com/yt-dlp/yt-dlp/blob/2026.10.26/pyproject.toml) and [EJS installation guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS) define the supported combination.

The replay held EJS constant. Its separate effect on this source's formats remains unmeasured.

## Why the checks missed it

**A dependency passing `--version` does not establish that its complete operational dependency chain is ready.** The original Docker build installed yt-dlp and checked the application entry point. Doctor checked that yt-dlp could report its version. CI ran FFmpeg fixture tests with fake downloads, and it did not exercise yt-dlp runtime discovery, split-format selection, or the pilot image. Paid-service connectivity was unrelated to the failure boundary.

## Corrective changes and regression checks

- The image copies Deno `2.9.8` from the official, digest-pinned binary image and installs yt-dlp `2026.10.26` with yt-dlp-ejs `0.8.0` at build time. The build executes Deno and reads both solver scripts as the application user. Offline doctor checks whether yt-dlp discovers those prerequisites.
- yt-dlp selects video and audio together or merges separate tracks, aborts when a required fragment is missing, and reports the completed output path. Montaj checks the staged file and uses FFmpeg to normalize it to `video.mp4` and mono 16 kHz `audio.wav`. It probes for video and audio streams, checks duration and size before normalization, and verifies that the WAV contains readable audio. The source lock, temporary-directory cleanup, and metadata-last publication support safe retries.
- Ingest translates subprocess failures into fixed, actionable messages. Tests feed failures containing a signed URL, cookie, and authorization header through the actual subprocess wrapper and check that the diagnostic contains none of them.
- The offline tests cover split-only selector behavior in real yt-dlp, a loopback DASH download and merge, corrupt media, missing audio, permission checks, limits, retries, and local-file ingest. The Docker test command disables external networking.

## Reproduction and recovery

Keep incident URLs and permission notes in private pilot records. Set `$url` to the original permitted URL and `$basis` to its recorded permission basis before using these PowerShell commands. The original ingest invocation was:

```powershell
docker compose run --rm montaj ingest "$url" --niche health --allowed --basis "$basis"
```

Start recovery with an image rebuild and an offline health check. The placeholder values below check key presence without making API calls. Use `db init` if doctor reports a missing database.

```powershell
docker compose build
docker compose run --rm -e DEEPGRAM_API_KEY=offline -e ANTHROPIC_API_KEY=offline montaj --help
docker compose run --rm -e DEEPGRAM_API_KEY=offline -e ANTHROPIC_API_KEY=offline montaj doctor
docker run --rm --network none --entrypoint deno montaj:local --version
docker run --rm --network none --entrypoint python montaj:local -c 'from importlib.metadata import version; from yt_dlp_ejs.yt import solver; print(version("yt-dlp"), version("yt-dlp-ejs")); assert solver.core() and solver.lib()'
docker run --rm --network none -e MONTAJ_REQUIRE_FFMPEG=1 --entrypoint python montaj:local -m pytest -p no:cacheprovider -q -ra --tb=short
```

When network access is available, probe the original source without downloading its media. To repeat the comparison, use `best[ext=mp4]/best` in place of `bv*+ba/b` and add `--no-js-runtimes` before `--` for the runtime-disabled cases.

```powershell
docker run --rm --entrypoint yt-dlp montaj:local --ignore-config --no-plugin-dirs --no-remote-components --xff never --no-playlist --socket-timeout 20 --simulate --format 'bv*+ba/b' --print '%(id)s %(duration)s %(format_id)s' -- "$url"
```

If the source still fails, follow the reported category. For a runtime or solver warning, restore the pinned installation. For a network or incomplete-media failure, confirm connectivity and retry the same ingest command with the same permission basis and source settings. For an unavailable, restricted, or unsupported source, verify access with the owner or obtain an explicitly permitted file of the same footage. Supplying a local file registers a content-hash identity, so capture the resulting source ID. Avoid posting raw verbose downloader output, which may contain signed URLs or environment details.

## Pin and update policy

Update yt-dlp and EJS together using the requirements of the selected stable release. Review Deno's supported version and the official image manifest, then update its version and digest together. Update the Dockerfile and the doctor pins in the same change. Rebuild, run the offline checks, and repeat the opt-in metadata probe before an operator retries the pilot. The image installs these dependencies at build time. Ingestion uses the packaged components.

## Limits of verification

The repair build, CLI help, offline doctor, Deno execution, EJS script reads, Ruff lint and format checks, and mypy all exited `0`. The network-disabled Docker suite passed 129 tests. Its media-only run passed 9 tests. The final metadata probe exited `0` with format `399+251` and a duration of 2,914 seconds. No provider calls were made.

Metadata selection checks the extractor and selector. The short loopback fixture exercises download, merge, normalization, and persistence using real tools. The pilot video was checked through metadata selection only. CDN transfer, source-specific decoding and total byte size still require an authorized full ingest of that source. Source duration and byte ceilings are enforced on actual downloaded media, so the downloader may transfer data before an oversized source is rejected. Platforms may restrict access or change available formats between checks.
