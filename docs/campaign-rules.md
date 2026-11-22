# Campaign rules

Supply a JSON file to `montaj campaign add --rules <path>`. The duration range is required. The lists default to empty, `flag_medical_claims` defaults to `false`, and `source_must_be_allowed` defaults to `true`. Montaj always requires permission to process media.

```json
{
  "min_seconds": 20,
  "max_seconds": 60,
  "banned_topics": ["unrelated promotions"],
  "banned_words": [],
  "required_hashtags": ["#ExampleCampaign"],
  "flag_medical_claims": false,
  "source_must_be_allowed": true
}
```

Montaj validates the file and stores its rules with the campaign. Use `montaj campaign list` to find the campaign ID.
