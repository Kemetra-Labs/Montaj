# Claude ranking output validation

A source with 30 saved candidates failed ranking twice. The local usage ledger showed four responses at exactly 2,200 output tokens, the old request limit. This strongly indicates truncation. The adapter discarded `stop_reason` and stored only usage, so the precise stop reason and any additional formatting or ID errors in those historical responses cannot be recovered. A successful HTTP response in the ledger records billable usage. It does not establish that Montaj accepted the grades.

Claude Sonnet 5.5 enables adaptive thinking by default and counts thinking and answer text against the same output cap. Montaj now requests 4,096 output tokens for rank and uses Sonnet 5.5's `between_tools` setting to reserve that space for the result in this tool-free request. The other Claude stages keep their existing request settings. The rank rubric and one-request comparison stay intact so the highest scores are selected globally, with clip ID breaking ties. [Claude Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview) · [Claude stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)

Ranking requires `end_turn` and exactly one valid grade per eligible clip. Truncation, refusal, invalid JSON or field values, and duplicate, missing, or unexpected IDs produce sanitized diagnostics. Rejections create no ranking or grade cache and record provider-reported usage as a failed attempt. Montaj does not resend a rejected ranking. Transport retries still use the existing retry policy and budget reservations. Valid cached grades remain reusable for another `--top` selection, and existing reviewed and approved clip records are protected.

## Production audit

Ingest prepares permitted media. The paid stages are transcribe, candidates, rank and draft. Cut and captions handle rendering locally. Check and human review still gate export.

| Stage or behavior | Classification | Evidence and action |
| --- | --- | --- |
| Transcribe | Already addressed | Deepgram receives each audio chunk once on a clean successful run. Chunk results and the complete transcript have separate caches. Uncached transport attempts reserve audio-duration cost before sending. Changing audio, language or model changes the key. |
| Candidate coverage | Unverified opportunity | Discovery uses 450-second windows at a 390-second stride. Adjacent windows can repeat 60 seconds, their timing tuples and the prompt. The overlap preserves boundary context. Prompt compaction and overlap changes need a quality comparison. Existing window caches, word snapping and deduplication remain in use. |
| Candidate and draft stop reasons | Measurable waste | A response explicitly truncated by Claude could enter the same JSON correction path and incur a second request. Both stages now stop on a provider-reported output cap, context limit or refusal. The existing single JSON correction for a malformed completion with a normal stop still applies. |
| Rank | Already addressed | One global pass grades all eligible IDs after deterministic duration and banned-term filters. The 4,096-token output limit and strict validation from PR #110 remain. Paid grades are keyed independently of `--top`. |
| Published rank cache | Confirmed defect | A matching hash could allow an incomplete or incorrectly selected rank file to pass. A rerun now validates coverage and the global top selection, rebuilding from valid paid grades. Recovery asks for operator action if those grades are unavailable. |
| Campaign briefs | Already addressed | PR #111 loads the linked brief for discovery and ranking, separates campaign cache contexts and preserves generic behavior and deterministic compliance. Linking keeps the saved transcript. |
| Draft | Unverified opportunity | One request per newly drafted clip (with a correction only for malformed JSON) repeats the instructions and rules over different excerpts. The per-clip cache and input fingerprints preserve successful work. A batching or prompt-caching change needs measured usage and quality evidence. |
| Cost ledger | Confirmed defect | A billed transport success previously showed as successful even when Claude's ranking, candidate or draft output failed validation. `montaj costs` now separates accepted calls, failed attempts and outstanding reservations while preserving the stage, source and approved-clip totals. Reused work has no ledger row. The stage commands and `montaj run` report reuse. |

The two changes address cache integrity and paid-response handling. Model selection,
prompts, windowing, grade limits and campaign checks are unchanged. Cost estimates use
the repository's configured rates and provider-reported usage when available.
Failures without usage can retain the budget estimate. Historical outcomes are
unchanged, so old successful rows may include rejected output.

## Offline comparison

Run `uv run pytest tests/test_candidates.py tests/test_rank.py tests/test_campaign_discovery.py tests/test_costs.py`.
Tests use a temporary database, fixture media and canned responses, with network
access blocked. Candidate truncation uses the same response and rates for both
versions ($2 per million input tokens and $10 per million output tokens).

| Fixture | Baseline | After |
| --- | --- | --- |
| Candidate window returns truncated JSON | 2 requests, 2,000 input / 4,400 output tokens, $0.048 estimated, no published picks | 1 request, 1,000 input / 2,200 output tokens, $0.024 estimated, no published picks |
| 30 valid rank grades | 1 request, top IDs 19, 5, 28 | 1 request, same 30 grades and top IDs |
| Current rank file loses a grade, repeats an ID or contains the wrong selection | Cache accepted the damage, 0 requests | Correct rank rebuilt from existing grades, 0 requests |
| Rank response is unusable | 1 request, 1,500 input / 610 output tokens, $0.0091 estimated, ledger showed success | Same usage and estimate, ledger shows failure and no new approved clips |

The $0.024 difference applies only to the simulated repeated truncation case.
Live savings and selection quality changes are unverified. The supplied historical
pilot estimate was $1.029080. This audit did not read or rerun the pilot.

## Resume safely

Confirm the deployed image includes this change and keep a backup of the current
database and source directory. Use `montaj costs --source 1` and check the saved
transcript and candidate file before any paid work. If they are current, obtain
authorization for paid processing and run only
`docker compose run --rm montaj rank 1 --top 3`, keeping the run and source
ceilings in place. A change to `--top` changes selection size. Ranking still grades
every eligible clip when it needs a new model response. If Montaj reports damaged
cached grades, preserve the file and restore a valid backup before considering
a paid retry. Continue cut, captions, draft and check only after rank succeeds,
then review the clips by hand.

Prioritize investigating safe request compaction over changing overlap or output
headroom. Compare on the same representative multilingual transcripts, checking
boundary coverage, campaign fit, candidate diversity and valid grading before
changing any default. Cached history counts would need separate instrumentation.
