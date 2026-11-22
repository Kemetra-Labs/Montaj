# Montaj

Montaj turns a long video from a permitted source into short vertical clips with burned-in captions, two hook options, and a campaign-rules check. One person runs it from the command line and reviews every clip before posting. Nothing is posted automatically.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` on `PATH`
- `yt-dlp` on `PATH` (only for a video URL; a local file does not need it)

## Install

```bash
uv sync
```

Run commands as `uv run montaj ...`.

## Setup

Copy `.env.example` to `.env` and fill in the keys. Never commit `.env`.

| Variable | Used by |
| --- | --- |
| `DEEPGRAM_API_KEY` | `transcribe`, `doctor --online` |
| `ANTHROPIC_API_KEY` | `candidates`, `rank`, `draft`, `doctor --online` |
| `MONTAJ_DATA_DIR` | Where the database and media live. Default `data/`. |
| `MONTAJ_DEEPGRAM_LANGUAGE` | Language sent to Deepgram. Default `en`. |
| `MONTAJ_DEEPGRAM_MODEL` | Default `nova-3`. |
| `MONTAJ_CLAUDE_MODEL` | Default `claude-sonnet-5-5`. |
| `MONTAJ_MAX_COST_PER_RUN_USD` | Spending ceiling for one run. Default `0.50`. |
| `MONTAJ_MAX_COST_PER_SOURCE_USD` | Spending ceiling for one source. Default `2.00`. |
| `MONTAJ_MAX_SOURCE_MINUTES` | Longest source accepted at ingest. Default `120`. |
| `MONTAJ_MAX_SOURCE_MB` | Largest source accepted at ingest. Default `1024`. |

```bash
uv run montaj db init
uv run montaj doctor
```

`doctor` stays offline. It checks `ffmpeg`, `ffprobe`, `yt-dlp`, the two keys (set or missing, never the value), the data directory, the database schema, and the caption font.

`doctor --online` sends one one-second silent clip to Deepgram and one one-token request to Claude, using the configured model names. It prints `OK` or `FAIL` for each service and the estimated cost, which stays under one cent. The calls are written to the cost log and count against the budget. It still does not print key values.

```bash
uv run montaj doctor --online
```

Paid stages (`transcribe`, `candidates`, `rank`, `draft`) stop before the request when the next call would pass either cost ceiling. `--budget 1.00` on those commands overrides both ceilings for that command.

## Stage commands

Each command takes a source id printed by ingest. A finished stage is reused when its inputs have not changed. Ingest is separate from the later stages.

1. **Ingest.** Copy a local file, or download a URL with `yt-dlp`. `--allowed` confirms permission. `--basis` is a short note of why this source may be used (required, 200 characters or less). A source over the minute or megabyte limit is refused before it replaces any saved file. Private, removed, age-restricted, geo-blocked, and network failures each stop with a plain message. A file that is not a video stops too.

   ```bash
   uv run montaj ingest C:\videos\interview.mp4 --niche games --allowed --basis "creator written permission"
   ```

   Niches are `business`, `health`, and `games`. Optional `--campaign` must be an id from `montaj campaign list`. When that campaign's rules include `allowed_sources`, the path or URL must contain one of those strings. See [docs/campaign-rules.md](docs/campaign-rules.md).

2. **Transcribe.** Deepgram Nova-3, word timestamps. The default language is `en`. Pass `--language` for another code Deepgram accepts, such as `ar-EG`, `ar`, or `multi`. `en` matches `en-US`, and `ar` matches `ar-EG`. `multi` accepts whatever was detected. Any other mismatch stops before a Claude call. `--force-language` continues with the detected language. An empty transcript, or too little speech, also stops the later paid stages.

   ```bash
   uv run montaj transcribe 1 --language en
   ```

3. **Candidates.** Claude proposes moments from the transcript. Defaults are 20 to 60 seconds.

   ```bash
   uv run montaj candidates 1
   ```

4. **Rank.** One Claude pass scores the candidates and keeps the top picks. `--top` defaults to 10. `--campaign` applies that campaign's rules.

   ```bash
   uv run montaj rank 1 --top 10
   ```

5. **Cut.** `ffmpeg` trims each ranked pick to a vertical 9:16 file.

   ```bash
   uv run montaj cut 1
   ```

6. **Captions.** Burns word-by-word captions into the cut. Arabic text uses the bundled Noto Sans Arabic font unless `MONTAJ_FONT_PATH` points at another font file.

   ```bash
   uv run montaj captions 1
   ```

7. **Draft.** One Claude call writes two hooks, a caption, and hashtags for each captioned clip. Hooks stay within 12 words and follow the transcript.

   ```bash
   uv run montaj draft 1
   ```

8. **Check.** Compares each clip with the campaign rules: length, banned words and topics, required hashtags, disclosure, permission, a clip already in the post log, and the daily post cap. A health source, or a campaign that sets `flag_medical_claims`, warns on phrases in `config/medical_claims.txt`. Failures stay visible for a later review. They are not deleted.

   ```bash
   uv run montaj check 1
   ```

`cut`, `captions`, `draft`, and `check` accept `--clip` to limit the command to one clip.

`run` calls those stages in order for one source: transcribe, candidates, rank, cut, captions, draft, check. It skips a stage whose output is already current, stops at the first failure, and resumes there on the next run. It never reviews, exports, or posts. `--until captions` stops after captions. `--campaign` is passed to rank. `--budget` overrides both spending ceilings for the whole run, so every paid stage in that run shares one ceiling.

```bash
uv run montaj run 1 --until check
```

```bash
uv run montaj costs
```

`costs` prints the estimated API spend by stage and source. `--source 1` limits the report.

## Preview

`preview` is read-only. It does not approve, export, or post.

```bash
uv run montaj preview serve
```

That listens on `http://127.0.0.1:8765/` and will not bind any other address. The page lists rendered clips that are not rejected or already posted, with the transcript, both hooks, the caption, the check result, the permission basis, and a video player. `--port` changes the port (default `8765`).

From your own machine, tunnel to a server and open the same local address:

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

To download the renders for one source, copy them into one folder with an index page:

```bash
uv run montaj preview export 1
```

The folder defaults to `data/preview/1/`. `--dest` picks another folder. Files outside the data directory are refused.

## Review

`review` is the approval step. It prints the rendered path, the loopback preview address, the permission basis, the transcript, both hooks, the caption, and the check. It does not post.

```bash
uv run montaj review 1
```

Keys are `a` approve, `r` reject with a one-word reason, `1` or `2` pick a hook, `c` replace the caption, `t` trim by seconds, `u` pick the posting account, `n` skip, and `q` quit. A failed check cannot be approved. A warning asks for `y` before approval and records that confirmation. Trim re-renders that clip and runs the check again. A hook or caption edit runs the check again and returns an approved clip to review.

## Export

`export` copies approved clips into a folder you can upload by hand. It does not post.

```bash
uv run montaj export --account 1
```

A clip is copied only when it is approved, its check is a current pass or a confirmed warning, and its source is allowed with a permission basis recorded. Anything else is left in place and the reason is printed. The folder is `data/exports/<UTC date>/<account id>-<handle>/`. Each clip is an `mp4` plus a text file with the chosen hook, caption, and hashtags. `manifest.csv` lists the clip id, file name, platform, campaign, and the time the package was written. Run it again and clips already exported stay as they are. New clips are added to the same day's manifest.

## Still planned

This command is not in this build yet:

- `montaj post-log` records a post that was published by hand.

Until that command exists, upload an exported clip yourself. Do not post a clip whose check failed.

## Where files go

`data/` is git-ignored.

```text
data/montaj.db
data/sources/<source id>/video.mp4
data/sources/<source id>/audio.wav
data/sources/<source id>/transcript.json
data/sources/<source id>/candidates.json
data/sources/<source id>/rank.json
data/clips/<clip id>/raw.mp4
data/clips/<clip id>/final.mp4
data/clips/<clip id>/captions.ass
data/exports/<UTC date>/<account id>-<handle>/<NN>_<slug>.mp4
data/exports/<UTC date>/<account id>-<handle>/<NN>_<slug>.txt
data/exports/<UTC date>/<account id>-<handle>/manifest.csv
```

## Rules that never change

- Only process a source marked allowed, with a short permission basis.
- A person reviews every clip before it is posted. There is no auto-posting.
- No proxies, fake browsers, or fingerprint tricks. Official APIs or manual posting only.
- Do not commit secrets, API keys, cookies, source videos, or rendered clips.

## More

- [Architecture](docs/ARCHITECTURE.md)
- [Pipeline contract](docs/pipeline-contract.md)
- [Campaign rules file](docs/campaign-rules.md)
- [Build roadmap](https://github.com/Kemetra-Labs/montaj/issues/31)
