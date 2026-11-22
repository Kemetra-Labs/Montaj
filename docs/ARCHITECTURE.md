# Montaj architecture

Montaj is a planned command-line tool for one operator. It takes a long video from a permitted source, finds the best moments, cuts vertical clips with word-by-word captions, drafts a hook and caption for each, and logs manual posts. A human reviews each clip before it is posted. Phase 2 adds performance tracking.

## Decisions

- One operator, not a SaaS. Speed and passing campaign rules matter more than software features.
- Three fresh accounts, one per niche: business, health, games. Each account stays distinct in style and topics.
- Review first, API posting later. Phase 1 posts by hand. Official APIs come after the pipeline works, starting with YouTube.
- No proxies, no fake browsers, no fingerprint tricks. Official APIs or manual posting only.
- Only clip sources with permission (a campaign's allowed sources or the creator's written OK). Every video carries a `source_allowed` flag and the pipeline refuses to run without it.

## Pipeline

Each stage is its own command and can be rerun from its persisted inputs. Reuse a successful stage output when its input hash still matches. [Issue #48](https://github.com/Kemetra-Labs/montaj/issues/48) defines clip status, render paths and cache invalidation and will add `docs/pipeline-contract.md`.

1. **ingest**: `yt-dlp` downloads the source (or copies a local file). Stores video, audio, metadata in `data/sources/<source_id>/`.
2. **transcribe**: Deepgram Nova-3 API on extracted audio. Output is word-level timestamps in JSON. Long files go in one request when under the API limits; split only if needed.
3. **candidates**: Claude reads the transcript in overlapping windows and returns candidate moments (start, end, reason, moment type, hook idea). Extra signals (audio energy, laughter, scene cuts) are added in a later phase.
4. **rank**: a second Claude pass ranks the top candidates globally and applies the campaign rules (length range, topic limits).
5. **cut**: `ffmpeg` trims each pick and reframes to 9:16 with a centered crop (raw render). Face tracking is optional and only for talking-head sources.
6. **captions**: burns word-by-word captions into the raw render (final render).
7. **draft**: Claude writes 2 hook options and a caption with hashtags per clip, in the style of the target account and the campaign rules.
8. **check** — Check campaign rules after captions and drafting. Rerun the check after any change made in review. Approval and export require a current check without failures.
9. **review** — Watch the clip in a read-only preview bound to loopback. On a remote host, access it through an SSH tunnel. Adjust start and end, pick a hook, and approve or reject in the terminal. Edits that change approved content require fresh approval after the check reruns.
10. **export** — Package approved clips with a current check for manual upload.
11. **post-log** — Record each manual post against its exported clip and account.

## Data model (SQLite)

- `sources`: id, url or path, title, niche, campaign_id, source_allowed, added_at
- `campaigns`: id, name, platform, rules_json, deadline
- `accounts`: id, platform, handle, niche
- `clips` stores clip metadata, `raw_path` for the uncaptioned render, `final_path` for the captioned render, and the status, check and review fields defined in [issue #48](https://github.com/Kemetra-Labs/montaj/issues/48).
- `posts`: id, clip_id, account_id, platform, posted_at, url
- `metrics`: id, post_id, taken_at, views, likes, comments, shares

## Tech choices

- Python 3.12, `uv` for packages, `typer` for the CLI, `pydantic` for data models, `pytest` for tests, `ruff` and `mypy` for checks.
- Deepgram Nova-3 for transcription with word timestamps and Arabic dialect support, including Egyptian Arabic. A small wrapper allows another backend such as `faster-whisper` to be swapped in. [Deepgram Nova-3 Arabic release](https://developers.deepgram.com/changelog/2026/1/27)
- Claude API for scoring and drafting. Prompts live in `prompts/` as files, versioned in git. Every call logs input size and cost.
- `ffmpeg` for cutting and captions. Optional face tracking with MediaPipe or OpenCV.
- Secrets in environment variables, never in git. Check the run and source budgets before each paid API call.

## Platform facts checked (Nov 2026)

- Keep TikTok posting manual. Direct Post limits unaudited clients to private posting and excludes tools built to upload to accounts the operator or their team manages. [TikTok Content Sharing Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
- The YouTube Data API currently gives each project 100 `videos.insert` calls per day by default. Additional quota requires a compliance audit. Verify the quota, upload cost and project privacy rules before building. [YouTube quota and compliance audits](https://developers.google.com/youtube/data/guides/quota_and_compliance_audits)
- Meta documents Reels publishing for professional Instagram accounts and a rolling 24-hour limit of 100 API-published posts per account. Verify the chosen login setup and permissions before building. [Meta Content Publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- YouTube evaluates reused content and inauthentic content under separate monetization policies. Reused clips need meaningful original contribution, and permission alone does not establish monetization eligibility. [YouTube channel monetization policies](https://support.google.com/youtube/answer/1311392)

## Six-week test plan

Goal: find out if the loop earns more than it costs. Hard stop at week 6.

- Setup: 3 new accounts (business, health, games), each with a clear style. Pick 1 or 2 campaigns per niche with clear rules and a good rate. Check each campaign's rules and permitted sources first.
- Volume: the target is a set number of clips per account, spread over the test. A target, not a quota: do not approve weak clips to reach it.
- Track from day one: time from source appearing to first post, review minutes per clip, API cost per clip, approved vs rejected by campaign, views at 1h/24h/7d, earnings.
- Hook test: for some clips, two hook styles on similar clips. Log the style.
- Week 3 checkpoint: if review takes over 3 minutes per clip or fewer than half the clips get approved, fix the pipeline before posting more.
- Week 6 decision: keep, change niche, or stop. Keep only if earnings per clip beat cost per clip (API cost plus time valued at a number Ahmed sets).
- Do not build analytics dashboards or API posting before the checks above justify them.

## Build phases

The weeks are planning estimates. Follow the [v3 build order](https://github.com/Kemetra-Labs/montaj/issues/31#issuecomment-7079949498). Phase 2 and API posting both require Phase 1 to be merged and the [first real run](https://github.com/Kemetra-Labs/montaj/issues/30) to pass. API posting also requires a satisfactory week 3 checkpoint and owner approval.

**Week 1: core pipeline, no polish.** Project setup, SQLite schema, ingest, transcribe, candidate finding and ranking. Run on 3 real permitted videos. Exit: a list of candidate moments per video that looks sensible when read.

**Week 2 — Cutting.** Cut and reframe to 9:16, burn in word-by-word captions, draft hooks and captions, and check campaign rules. Use a centered crop by default. Face tracking is optional for talking-head clips.

**Week 3 — Review and first posts.** Review queue (trim, pick hook, approve), export and post log. Aim to approve and export ten clips in the first real run. Approve fewer if needed and record what limited the count. Use manual posting on all three accounts and record each post through the CLI. Use the week 3 checkpoint to decide whether the workflow needs changes.

**Week 4 — Phase 2 metrics and reporting.** Add metrics entry and report views by hook style, moment type, niche and platform. Add audio energy and laughter signals to candidate scoring if scoring quality is the weak point.

**Week 5 (gated): first API.** YouTube upload through the official API for the account(s) that use it. Scheduled posting. Quota and error handling.

**Week 6 — Finish the test.** Keep posting, collect results and write the decision note. Evaluate Instagram API work only if the results justify it.

## Risks, in order

1. Campaign access: accounts or campaigns may not accept fresh accounts.
2. Low views per clip.
3. Time spent building instead of posting.

## Out of scope

Web app, multi-user support, proxies or multiple browser profiles, auto-posting without human review, clipping sources without permission.
