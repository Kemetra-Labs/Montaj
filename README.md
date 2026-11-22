# Montaj
Finds the best moments in long videos, cuts vertical clips, and drafts hooks and captions for content-rewards campaigns.
# Montaj

Finds the best moments in long videos, cuts vertical clips, and drafts hooks and captions for content-rewards campaigns.

## What it does

- Finds strong moments in long videos (podcasts, streams, interviews).
- Cuts them into vertical 9:16 clips.
- Drafts a hook and a caption for each clip, ready to post.

## Status

Early idea. No code yet. This repo holds the plan and will hold the first prototype.

## Planned pipeline

1. **Ingest**: take a video file or link.
2. **Transcribe**: speech to text with timestamps.
3. **Score moments**: rank segments by hook strength, emotion, and standalone sense.
4. **Cut**: trim each pick and reframe to 9:16.
5. **Draft**: write a hook and a caption per clip, following the campaign rules.
6. **Export**: clips plus a metadata file (title, hook, caption, source timestamps).

## Notes

- Content-rewards campaigns have their own rules (length, topic, source, disclosure). Keep each campaign's rules in a config file, not in code.
- A human reviews every clip before posting.
- Name: Montaj (مونتاج), Arabic for editing.
