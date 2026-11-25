# MJ-14 Audio loudness baseline: report

Jira [MJ-14](https://rahmaqanater.atlassian.net/browse/MJ-14). Plan: [2026-11-25-mj-14-loudness.md](../superpowers/plans/2026-11-25-mj-14-loudness.md).

**Status: measurements done. The blind listening test is waiting for the owner. No recommendation is final until the owner has listened.**

## Method

- **Inputs:** the four pilot clips from MJ-2, which are 720×1280 copies from Drive, not the masters (Sonnet #18 and #36, Haiku #47 and #48). Measured on 720p copies; masters not verified.
- **Tools:** FFmpeg 9.1 (gyan.dev full build), using the built-in `loudnorm` and `ebur128` filters only. No new dependency, no model or paid API calls, no change to Montaj or its data.
- **Variants per clip:**
  - **A:** original.
  - **B:** Montaj's shipped constant-gain policy, `loudness.linear_gain_db(I, TP, target=−14, ceiling=−2, max_gain=12)`, applied with `volume`.
  - **C:** two-pass `loudnorm` to I −16, TP −1.5, LRA 11.
  - **D:** two-pass `loudnorm` to I −14, TP −1.0, LRA 11.
- **Encoding:** the picture is stream-copied. The audio is re-encoded as AAC at 192 kbps and 48 kHz; A stays a byte copy.
- **Measurement:** every output is measured again with `ebur128=peak=true`.

## Measurements

| Clip | Variant | Integrated (LUFS) | True peak (dBTP) | LRA (LU) | Notes |
| --- | --- | --- | --- | --- | --- |
| Sonnet #18 | A original | −32.5 | −10.9 | 9.0 | |
| | B Montaj policy | −23.6 | −2.0 | 9.0 | +8.9 dB, limited by the −2 dBTP ceiling |
| | C loudnorm −16 | −16.2 | −1.5 | 6.9 | dynamic mode |
| | D loudnorm −14 | −14.5 | −1.0 | 6.1 | dynamic mode |
| Sonnet #36 | A original | −29.8 | −12.4 | 4.5 | |
| | B Montaj policy | −19.4 | −2.0 | 4.6 | +10.4 dB, limited by the ceiling |
| | C loudnorm −16 | −16.1 | −1.5 | 3.8 | dynamic mode |
| | D loudnorm −14 | −14.4 | −1.0 | 3.0 | dynamic mode |
| Haiku #47 | A original | −30.6 | −13.0 | 4.3 | |
| | B Montaj policy | −19.6 | −2.0 | 4.3 | +11.0 dB, limited by the ceiling |
| | C loudnorm −16 | −16.1 | −1.5 | 3.6 | dynamic mode |
| | D loudnorm −14 | −14.3 | −1.0 | 3.4 | dynamic mode |
| Haiku #48 | A original | −30.2 | −10.9 | 5.9 | |
| | B Montaj policy | −21.4 | −2.1 | 5.9 | +8.9 dB, limited by the ceiling |
| | C loudnorm −16 | −16.1 | −1.5 | 4.4 | dynamic mode |
| | D loudnorm −14 | −14.4 | **−0.9** | 3.8 | dynamic mode; **0.1 dB over its −1.0 ceiling** after AAC encoding |

## What the numbers say

1. **Montaj's shipped policy cannot reach a normal level on these clips.** Constant gain stops when the true peak reaches −2 dBTP, which happens 5 to 10 LU short of −14. It never needed its +12 dB cap: the peak ceiling bound first every time. B leaves the clips at −19.4 to −23.6 LUFS, still well below typical short-form playback.
2. **Reaching −16 or −14 requires dynamic processing.** `loudnorm` could not stay linear on any clip at either target, because the gain needed would push peaks above the ceiling. It switched to dynamic mode every time, which compresses the loudness range: on #18, from 9.0 LU to 6.9 at −16 and to 6.1 at −14.
3. **−14 with a −1.0 dBTP ceiling leaves almost no margin.** One clip measured −0.9 dBTP after AAC encoding. Montaj's verify step (`VERIFY_TRUE_PEAK_DBTP = −1.0`) would flag that. −16 with −1.5 held its ceiling on all four.
4. **Whether dynamic mode is audible** (pumping, breathing, raised noise) is what the listening test decides. The numbers cannot.

## Blind listening (pending)

- **Files:** 16 files with random labels, L01 to L16, grouped by clip, in a local scratch folder. The label key is kept in a separate folder and opened only after scoring.
- **How the owner listens:** at one fixed phone volume, on the phone speaker and then on headphones.
- **What the owner scores**, from 1 to 5: loud enough, no pumping, no harshness, no raised noise. The owner also picks a favourite per clip.

Results: *to be added after the owner's scores.*

## Recommendation for MJ-3 (draft, final after listening; proposal, needs explicit acceptance)

- If C or D sounds clean, Montaj needs a peak-safe limiting stage, for example two-pass `loudnorm` to −16 LUFS and −1.5 dBTP. A higher gain cap would not help, because the ceiling binds first.
- If the dynamic variants sound worse than B, keep constant gain. Its ceiling is the only lever, so consider −1.0 dBTP, which still lands near −18 to −23 LUFS, and accept quieter clips.
- Either way, re-measure on the masters before any default changes.
