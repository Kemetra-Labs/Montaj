# MJ-2 Pilot G1 review: QA table

Jira [MJ-2](https://rahmaqanater.atlassian.net/browse/MJ-2). Plan: [2026-11-25-mj-2-pilot-review.md](../superpowers/plans/2026-11-25-mj-2-pilot-review.md).

**Status: Claude's pre-review done for 4 of 10 clips. The owner's review is not done yet. No clip is approved.**

## Header

| | |
| --- | --- |
| Source | "Big Tech Is Facing Lawsuits — Could You or Your Family Qualify? \| Anthony Johnson", Mindful Podcast, 48:33, <https://www.youtube.com/watch?v=6uSskUaDL90> |
| Permission basis | Owner's statement, 2026-11-25: a promotional campaign test, and the video's owner gave permission. Written proof to be kept by the owner. A 7.4 GB "Anthony Johnson Interview.mp4" is shared to the owner's Drive from another account. |
| Pilot record | Source 1, campaign 1. Candidate sets from Sonnet (#18, #33, #36, #37, #43) and Haiku (#46, #47, #48, #55, #56), all ranked by Haiku (from MJ-2). |
| Files reviewed | Four 720×1280 copies from the Drive folder "Montaj - Sonnet vs Haiku best clips", downloaded 2026-11-25, sizes matching Drive byte for byte. They are **not** the 1080×1920 masters. |
| Not found | The other six clips; every master; the pilot's `montaj.db`, and with it the stored check results, transcript and campaign rules. Searched: this laptop's user folder, the repo's `data/` and `data/validation/workspace`, and the whole Drive. |
| Tools | FFmpeg 9.1 (gyan.dev full build) for `ffprobe`, `ebur128`, `silencedetect`, `blackdetect` and `freezedetect`; frames every 2 s read as images. No model or paid API calls. |
| Reviewer | Claude (pre-review, 2026-11-25). Owner review: pending. |

Method tags in the findings: **[measured]** by ffmpeg, **[frame]** seen in a sampled frame, **[caption]** read from the burned-in caption, **[owner, by ear]** to be checked by listening. Claude cannot hear the clips, so sync and audio quality are left to the owner.

## Findings that apply to all four clips

- **Too quiet [measured].** Integrated loudness is −29.8 to −32.5 LUFS, 16 to 18 LU below the −14 LUFS short-form reference. True peaks of −10.9 to −13.0 dBTP leave room for gain. Montaj's opt-in loudness gain (`MONTAJ_LOUDNESS_TARGET_LUFS`, at most +12 dB) was evidently off for this render; even its maximum would stop near −18 to −20 LUFS. This feeds MJ-3 and MJ-14. Measured on the 720p copies; the masters' audio is expected to match but is not verified.
- **Speech at the very first frame [measured].** The mean level of the first 0.3 s is −29 to −36 dB in every clip, so each may start inside a word. To be confirmed [owner, by ear].
- **Small captions [frame].** Two lines of white text with yellow highlights sit at chest level, at roughly 3 to 4 % of the frame height. They are inside the safe area and do not cover the face, but they may be hard to read on a phone. To be judged by the owner on a phone. This feeds MJ-3.
- **Clean picture [measured].** No black or frozen frames. 30 fps, H.264 with AAC at 48 kHz stereo.
- **Content risk [caption].** Every clip makes the source's case that apps are built to addict children. That is the speaker's view and the subject of lawsuits, not an established fact. A clip posted for a settlement-qualification campaign must not promise compensation or eligibility, and its post caption should attribute the claims to the speaker. None of the four clips mentions compensation or eligibility.

## Clips

| Clip | Set | File reviewed | Master verified | Technical findings | Content findings | Claude's recommendation | Owner's decision | Next action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #18 | Sonnet | 720p copy, 54.9 s | review-pending | −32.5 LUFS, LRA 9.0, peak −10.9 dBTP [measured]. Twelve pauses of 0.5 s or more, three of them in 5.6 to 8.5 s [measured]. Cut-aways to the host around 0:08 to 0:11 and 0:30 crop his face at the frame edge [frame]. | Hook: "Yeah, it's always interesting, like, the comment that the only two industries that use the word users are drug dealers and software" [caption]. The strong line arrives after about 3 s of filler. The strongest language of the four, including "dopamine", "addiction" and "giving crack to children and then charging them for it after" (about 0:50) [caption]. Ends on that line, a complete thought [caption]. | **Revise.** Trim the opening filler, fix or drop the host cut-aways, and raise the loudness. Owner to decide whether the "crack to children" line is acceptable for the campaign. | | Owner watches and listens; decides. |
| #33 | Sonnet | not found | review-pending | — | — | — | | Find the render or the pilot data folder, or re-render (a separate, paid decision). |
| #36 | Sonnet | 720p copy, 22.7 s | review-pending | −29.8 LUFS, LRA 4.5, peak −12.4 dBTP [measured]. Pauses at 0.7 to 1.2 s and 1.9 to 2.8 s, a slow start [measured]. Face framed throughout [frame]. | Hook: "If you're a parent, I would say there's been a lot of struggle with some parents blaming themselves" [caption]; it speaks straight to the audience. **Ends mid-sentence**: "…work against, like, their whole friend groups on" [caption]. | **Revise.** Extend or re-cut the end to a complete thought, tighten the two opening pauses, and raise the loudness. | | Owner watches and listens; decides. |
| #37 | Sonnet | not found | review-pending | — | — | — | | As #33. |
| #43 | Sonnet | not found | review-pending | — | — | — | | As #33. |
| #46 | Haiku | not found | review-pending | — | — | — | | As #33. |
| #47 | Haiku | 720p copy, 25.3 s | review-pending | −30.6 LUFS, LRA 4.3, peak −13.0 dBTP [measured]. Two short pauses at 6.4 and 13.3 s [measured]. Face framed throughout; hand gestures stay clear of the face [frame]. | "…things that are in the design that make it very addictive for children, and it's intentionally designed that way… intermittent variable reward systems… likes and notifications… that dopamine hit… a way that maximizes addiction." [caption] A complete thought that ends on "addiction." Opens with "And", a small mid-thought start. Claims of intent ("intentionally designed") stated as fact [caption]. | **Revise, the strongest of the four.** Raise the loudness. Attribute the claims to the speaker in the post caption. Check the "And" start by ear. | | Owner watches and listens; decides. |
| #48 | Haiku | 720p copy, 25.9 s | review-pending | −30.2 LUFS, LRA 5.9, peak −10.9 dBTP [measured]. Five pauses of 0.5 s or more [measured]. Face framed; the host's hand enters the right edge at times [frame]. | "I remember there's a point in the movie where he talks about Zuck wanting three dials… healthy for your kids… you're cranking the revenue number or you're cranking the time on-site, like get them addicted, keep them on the site… more money to do that." [caption] "The movie" and "he" lack context. **Names a real person (Zuckerberg) and attributes intent to addict** [caption]. | **Reject for this campaign**, unless the owner gets a legal OK: a named person with attributed intent is a defamation risk, and the opening needs context the clip does not give. | | Owner decides; legal OK if kept. |
| #55 | Haiku | not found | review-pending | — | — | — | | As #33. |
| #56 | Haiku | not found | review-pending | — | — | — | | As #33. |

## Before any external publishing (MJ-2 step 4)

Each of these still has to be verified on its own, outside this table:
- campaign rights, with the owner's written permission;
- the target platforms' policies on paid legal promotion and on health claims;
- the human review gate: approval in the clip preview after a current check.

Rendered, previewed, reviewed and approved are four different states. No clip here has reached "approved".
