# lbs-calculator

Downloads labelled data from Label Studio with a Personal Access Token and totals
up how many hours of audio transcription have been labelled.

Stdlib only — no `pip install`.

## Setup

```bash
cp .env.example .env     # then paste your URL + token
```

Get the token from **Label Studio → Account & Settings → Personal Access Token**.
Legacy 40-char API keys also work; the script detects which flavour you gave it.

## Use

```bash
python3 lbs_hours.py --list-projects          # find project ids
python3 lbs_hours.py --project 12             # one project
python3 lbs_hours.py --project 12 34 56       # several
python3 lbs_hours.py --project all            # everything the token can see
python3 lbs_hours.py --project all --csv breakdown.csv
python3 lbs_hours.py --from-file exports/project-12.json   # re-tally, no download
```

Exports land in `exports/project-<id>.json`, so you keep the raw labelled data.

## How the hours are counted

Two project shapes, auto-detected per project:

**Segment-based** (results carry `value.start` / `value.end` — speaker turns,
utterances). Three figures are reported:

| figure | meaning |
|---|---|
| `segment total` | every labelled region added up |
| `merged coverage` | **the headline number** — overlapping regions counted once, so it is real audio wall-clock |
| `with transcript text` | only the regions that actually contain text |

A region normally produces two result entries — a `labels` entry for the speaker
and a `textarea` entry for the transcript — sharing one region id. They are
collapsed by id, so a 30 s region counts as 30 s, not 60 s.

**Whole-file** (one transcript per file, no start/end). Hours come from each
file's own duration, read from `task.data` (`duration`, `audio_duration`, …) or
probed with ffprobe via `--probe-missing`. The run reports how many durations it
could not determine — if that count is high, point `--duration-field` at the
right key or install ffmpeg.

`audio files total` and `labelling time spent` (annotator `lead_time`) are always
reported alongside, so you can sanity-check coverage and effort against the audio hours.

## Counting rules

- One annotation per task by default, so audio labelled twice is not double counted.
  `--count all-annotations` switches to annotation effort instead.
- Skipped annotations (`was_cancelled`) are excluded; `--include-cancelled` keeps them.
- `--require-text` counts only regions with non-empty transcript text.
- `--only-ground-truth` restricts to ground-truth annotations.
# label-studio-transcribe-calculator
