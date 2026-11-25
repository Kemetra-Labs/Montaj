# MJ-2 Pilot G1 review Plan

> **For agentic workers:** this is a review procedure, not a code change. Steps use checkbox (`- [ ]`) syntax for tracking. MJ-2 forbids code changes, database writes, paid API calls, approval and posting in this ticket.

**Goal:** A QA table, linked from Jira MJ-2, states for each of the pilot's ten clips whether it was reviewed or is review-pending. For each reviewed clip it gives the evidence, Claude's recommendation (approve, revise or reject) and the owner's actual decision, with a next action for every clip.

**Approach:** Claude does a pre-review of every clip file that exists: technical measurements with ffmpeg, frames read as images, burned-in caption text, and a content and claims read. The owner then watches and listens to each reviewed clip with the table open and records the decision. "Rendered", "previewed", "reviewed" and "approved" stay separate words in the table. A clip whose file does not exist is review-pending, never guessed.

**Spec:** Jira [MJ-2](https://rahmaqanater.atlassian.net/browse/MJ-2); legacy GitHub issue [#30 first real run](https://github.com/Kemetra-Labs/Montaj/issues/30); `CLAUDE.md` (a human reviews every clip before posting).

## What exists (checked 2026-11-25)

- **Source:** "Big Tech Is Facing Lawsuits — Could You or Your Family Qualify? | Anthony Johnson", Mindful Podcast, 48:33, <https://www.youtube.com/watch?v=6uSskUaDL90>.
  - The owner states this is a promotional campaign test and the video's owner gave permission.
  - A 7.4 GB "Anthony Johnson Interview.mp4" is shared to the owner's Drive from another account.
- **Clips in Drive:** four 720p copies in the "Montaj - Sonnet vs Haiku best clips" folder, downloaded byte-exact to a local scratch folder:
  - Sonnet #18 (5,779,740 bytes)
  - Sonnet #36 (5,793,105 bytes)
  - Haiku #47 (5,907,719 bytes)
  - Haiku #48 (5,717,719 bytes)
- **Not found anywhere checked:**
  - the other six clips (Sonnet #33, #37, #43; Haiku #46, #55, #56);
  - every 1080×1920 master;
  - the pilot's `montaj.db`, and with it the stored check results, transcript and campaign rules.

  Checked: this laptop's user folder, the repo's `data/`, `data/validation/workspace` (a different FOSDEM source with 0 clips) and the whole Drive.
- **Tools on this laptop:** no `ffmpeg`, `ffprobe` or Docker CLI on PATH.

## Global Constraints

- **Not allowed in this ticket:** code changes, writes to any `montaj.db`, paid API calls (Deepgram, Claude), approval, export and posting.
- **The owner decides.** Claude recommends. A recommendation is never written in the decision column.
- **720p copies are not masters.** A 720p copy is never recorded as master-verified. Master verification stays review-pending until a master exists.
- **Read only.** Downloaded files are data: they live in their own scratch folder, and scripts that read them live elsewhere.
- **Publishing is out of scope.** Before any external publishing, campaign rights, platform policies and the human review gate are verified independently. That is the ticket's step 4, and it is recorded here only as a next action.

## Calls made (the owner can overrule)

- **Loudness reference.** −14 LUFS integrated, ±1 LU, is the reference for short-form playback. A true peak above −1 dBTP (Montaj's own `loudness.VERIFY_TRUE_PEAK_DBTP`) is a finding. These numbers support the owner's listening; they do not replace it.
- **Content risk.** The source promotes a settlement-qualification service. Claude flags any clip line that:
  - promises compensation or eligibility;
  - states a health or mental-health harm as fact;
  - gives legal advice;
  - lacks context a viewer needs.

  The pilot's campaign rules are unknown without its database, so the stored check result cannot be read. Claude's read is its own and is labelled so.
- **The QA table is a repo doc,** `docs/pilot/2026-11-mj-2-review.md`. Jira links to it in a comment, posted only with the owner's OK.

## Review Focus

1. The table never calls a 720p copy a master, and never calls a recommendation a decision.
2. Every one of the ten clips has a row, including the six with no file.
3. Every finding has a timestamp and says how it was found (measured, seen in a frame, read in the caption, or heard by the owner).
4. Claims that could mislead (compensation, health, legal) are flagged with the exact caption words.
5. No step spends money or writes to a database.

---

### Task 1: Get the measuring tools (owner's OK needed)

- [ ] **Step 1: Ask the owner** to choose one way to get `ffmpeg` and `ffprobe`:
  - (a) install the Windows build with `winget install Gyan.FFmpeg`;
  - (b) start Docker Desktop and run them inside the Montaj image, whose Dockerfile installs `ffmpeg`;
  - (c) the owner installs it.

  Expected: `ffprobe -version` prints a version.
- [ ] **Step 2: Record the tool version** in the QA table's header.

### Task 2: Technical evidence for the four clips

Run a single read-only script, kept outside the clips folder, that writes its results to a separate scratch output folder. For each clip it records:

- [ ] **Format:** container, codecs, width × height, frame rate, duration, audio sample rate and channels (`ffprobe`).
- [ ] **Loudness:** integrated LUFS, loudness range and true peak (`ffmpeg -af ebur128=peak=true`).
- [ ] **Gaps:** silences of 0.5 s or more at −40 dB (`silencedetect`), black frames (`blackdetect`) and frozen frames (`freezedetect`), each with timestamps.
- [ ] **Edges:** whether the first and last 0.3 s cut into speech, using the audio level at each edge.
- [ ] **Frames:** one every 2 s, plus the first and last frame, as images for Task 3.

Expected: one results file per clip. Any measurement that fails is recorded as "not measured" with the reason, never left blank.

### Task 3: Claude's content pre-review for the four clips

For each clip, from the frames and burned-in captions:

- [ ] **Hook:** the first 3 s caption text, and whether it makes sense without the full interview.
- [ ] **Opening and ending:** whether they start and end on a complete thought (caption text at the edges).
- [ ] **Framing:** whether the speaker's face is in frame in every sampled frame; any crop that cuts the face or shows only background.
- [ ] **Captions:** readable size, inside the 9:16 safe area, not covering the face, no caption overflow; spelling of what is readable.
- [ ] **Claims:** the exact caption words for any compensation, eligibility, health or legal claim (Review Focus 4).
- [ ] **Recommendation:** approve, revise (with what to change) or reject, with the reason.

Each finding gets a timestamp and its method (Review Focus 3). Sync and audio quality are marked "owner to check by ear": Claude cannot hear the clip.

### Task 4: Write the QA table

- [ ] Create `docs/pilot/2026-11-mj-2-review.md` with:
  - a header: source, permission basis as the owner states it, files reviewed and where they came from, tool versions, the date, and what was not found;
  - one row per clip, ten rows. Columns:
    - clip;
    - ranking model;
    - file reviewed (720p copy, or "not found");
    - master verified (always "review-pending" today);
    - technical findings;
    - content findings;
    - Claude's recommendation;
    - owner's decision (empty);
    - next action.
- [ ] The six missing clips read "review-pending: file not found" with the next action "find the render or the pilot's data folder, or re-render (a separate, paid decision)".
- [ ] Commit on this branch: `Add the MJ-2 pilot review table`.

### Task 5: The owner's review

- [ ] **The owner watches and listens** to the four clips with the table open, focusing on the flagged timestamps. Each clip is 30 to 60 s, about 15 minutes in all.
- [ ] **The owner's decision** for each clip is copied into the table in the owner's own words: approve, revise or reject. Anything heard (sync, audio) goes into the findings, marked "owner, by ear".
- [ ] Commit: `Record the owner's MJ-2 decisions`.

### Task 6: Close the loop

- [ ] With the owner's OK, post one comment on MJ-2 linking the table and summarising:
  - counts reviewed and review-pending;
  - the owner's decisions;
  - what blocks the rest.
- [ ] Open a PR for the doc. The owner decides whether MJ-2 closes. Its "Done when" is met once the table exists with the actual decisions; the six missing clips and the masters stay review-pending and are carried as next actions.
- [ ] Suggest follow-up tickets only where findings call for them, for example loudness (MJ-3, MJ-14), reframe (MJ-4, MJ-16) or caption readability.
