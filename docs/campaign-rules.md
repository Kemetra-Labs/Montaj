# Campaign rules

Supply a JSON file to `montaj campaign add --rules <path>`. The duration range is required. The lists default to empty, `flag_medical_claims` defaults to `false`, and `source_must_be_allowed` defaults to `true`. `required_disclosure`, `max_posts_per_day`, and `allowed_sources` are optional. When `allowed_sources` is not empty, ingest accepts only a URL or path that contains one of those strings. Montaj always requires permission to process media. A disclosure must appear in the caption. The daily cap counts posts logged today for the matching account.

```json
{
  "min_seconds": 20,
  "max_seconds": 60,
  "banned_topics": ["unrelated promotions"],
  "banned_words": [],
  "required_hashtags": ["#ExampleCampaign"],
  "allowed_sources": ["https://example.test/"],
  "required_disclosure": "#ad",
  "max_posts_per_day": 5,
  "flag_medical_claims": false,
  "source_must_be_allowed": true,
  "brief": {
    "objective": "Encourage new creators to try short-form editing",
    "target_audience": "Creators making their first short videos",
    "key_messages": ["Start with one clear idea", "Make the opening earn attention"],
    "content_guidance": ["Prefer actionable advice", "Avoid jargon-heavy excerpts"]
  }
}
```

Montaj validates the file and stores its rules with the campaign. Use `montaj campaign list` to find the campaign ID.

## Use the same brief across sources

The optional `brief` requires a nonblank `objective` and `target_audience`. The
`key_messages` and `content_guidance` lists can be omitted. Montaj sends the
brief to Claude during candidate discovery and ranking. Ranking considers
campaign fit alongside the hook, standalone coherence, and retention potential.
Hard checks such as duration limits and banned terms still run in code.

Set `--campaign <id>` when ingesting a new source. For a source that's already
ingested or transcribed, use its existing source ID.

```sh
montaj campaign link-source 12 --campaign 3
montaj candidates 12
montaj rank 12 --top 10
```

Once linked, `montaj run 12 --until rank` also picks up the brief automatically
and reuses a current transcript. Sources without a brief use the generic
discovery prompt. Older campaign files still work and retain their ranking
rules. The ranking-only `--campaign` option still overrides the ranking
campaign for that run.

To revise a brief, edit the complete rules file and run
`montaj campaign update-rules 3 --rules path/to/rules.json`. Then rerun candidates
and rank for any linked source you want to process. Montaj refreshes stale
selections and retains the existing media, transcript, and clip review records.
Campaign-aware batch processing skips clips with a recorded review decision.
Human review is still required before newly selected clips can be exported.
