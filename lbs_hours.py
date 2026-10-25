#!/usr/bin/env python3
"""
Download labelled data from Label Studio with a Personal Access Token and
total up how many hours of audio transcription have been labelled.

Handles both shapes of audio project:

  * segment-based  - results carry value.start / value.end (speaker turns,
                     utterances). Hours = sum of the labelled regions.
  * whole-file     - one transcript per audio file, no start/end. Hours = sum
                     of the audio files' own durations, taken from a field in
                     task.data or probed with ffprobe (--probe-missing).

Zero third-party dependencies: stdlib only.

Usage
-----
  export LABEL_STUDIO_URL=https://app.humansignal.com
  export LABEL_STUDIO_TOKEN=<personal access token>

  python3 lbs_hours.py --list-projects
  python3 lbs_hours.py --project all
  python3 lbs_hours.py --project 12 34 --csv breakdown.csv
  python3 lbs_hours.py --from-file exports/project-12.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 180
POLL_INTERVAL = 2.0
POLL_LIMIT = 600  # seconds to wait for an export snapshot

# task.data keys that are commonly used to carry a precomputed audio duration
DURATION_KEYS = (
    "duration",
    "audio_duration",
    "duration_sec",
    "duration_seconds",
    "audio_length",
    "length_seconds",
)

# task.data keys that commonly carry the audio URL/path
AUDIO_KEYS = ("audio", "audio_url", "url", "file", "path", "mp3", "wav")


# --------------------------------------------------------------------------- #
# Label Studio client
# --------------------------------------------------------------------------- #

class LabelStudioError(RuntimeError):
    pass


class Client:
    """Minimal Label Studio API client with Personal Access Token support."""

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT,
                 insecure: bool = False, verbose: bool = False):
        if not base_url:
            raise LabelStudioError("No Label Studio URL. Set LABEL_STUDIO_URL or pass --url.")
        if not token:
            raise LabelStudioError("No token. Set LABEL_STUDIO_TOKEN or pass --token.")
        self.base = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.verbose = verbose
        self._ctx = ssl._create_unverified_context() if insecure else None
        self._auth: dict[str, str] | None = None
        self._auth_kind = "?"

    # -- plumbing ----------------------------------------------------------- #

    def _open(self, method, path, data=None, params=None, authed=True):
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        headers = {"Accept": "application/json", "User-Agent": "lbs-hours/1.0"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if authed:
            headers.update(self.auth_headers())
        if self.verbose:
            print(f"  [http] {method} {url}", file=sys.stderr)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)

    def request(self, method, path, data=None, params=None, authed=True):
        try:
            with self._open(method, path, data, params, authed) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:1500].decode("utf-8", "replace").strip()
            raise LabelStudioError(f"{method} {path} -> HTTP {exc.code} {exc.reason}: {detail}")
        except urllib.error.URLError as exc:
            raise LabelStudioError(f"{method} {path} -> cannot reach {self.base}: {exc.reason}")
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload.decode("utf-8", "replace")

    def download(self, path, dest: Path, params=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._open("GET", path, params=params) as resp, dest.open("wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:1500].decode("utf-8", "replace").strip()
            raise LabelStudioError(f"GET {path} -> HTTP {exc.code}: {detail}")
        return dest

    # -- auth --------------------------------------------------------------- #

    def auth_headers(self) -> dict[str, str]:
        if self._auth is None:
            self._auth = self._detect_auth()
        return self._auth

    def _detect_auth(self) -> dict[str, str]:
        """
        Label Studio has two token flavours and the PAT is the newer one:

          * Personal Access Token (JWT, 1.13+ / HumanSignal cloud) - the PAT is
            a *refresh* token; exchange it at /api/token/refresh for a short
            lived access token used as `Authorization: Bearer <access>`.
          * Legacy API key (40-char hex) - used directly as
            `Authorization: Token <key>`.

        Try whichever the token's shape suggests first, then fall back.
        """
        looks_jwt = self.token.count(".") == 2
        order = ["pat", "legacy", "bearer"] if looks_jwt else ["legacy", "pat", "bearer"]
        errors = []
        for kind in order:
            if kind == "pat":
                headers = self._exchange_pat()
                if headers is None:
                    errors.append("pat: /api/token/refresh rejected the token")
                    continue
            elif kind == "legacy":
                headers = {"Authorization": f"Token {self.token}"}
            else:
                headers = {"Authorization": f"Bearer {self.token}"}
            who = self._whoami(headers)
            if who is not None:
                self._auth_kind = kind
                label = who.get("email") or who.get("username") or f"user {who.get('id')}"
                print(f"Authenticated as {label} ({kind} token) at {self.base}")
                return headers
            errors.append(f"{kind}: /api/current-user/whoami returned 401")
        raise LabelStudioError(
            "Could not authenticate against " + self.base + ".\n  tried -> "
            + "\n  tried -> ".join(errors)
            + "\nCheck the URL (include https:// and no trailing /api) and that the "
              "token is current: Label Studio > Account & Settings > Personal Access Token."
        )

    def _exchange_pat(self) -> dict[str, str] | None:
        try:
            data = self.request("POST", "/api/token/refresh",
                                data={"refresh": self.token}, authed=False)
        except LabelStudioError:
            return None
        access = (data or {}).get("access")
        return {"Authorization": f"Bearer {access}"} if access else None

    def _whoami(self, headers) -> dict | None:
        req = urllib.request.Request(
            self.base + "/api/current-user/whoami",
            headers={"Accept": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError:
            return None
        except urllib.error.URLError as exc:
            raise LabelStudioError(f"Cannot reach {self.base}: {exc.reason}")

    # -- resources ---------------------------------------------------------- #

    def projects(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self.request("GET", "/api/projects",
                                params={"page": page, "page_size": 100})
            if isinstance(data, list):           # older versions return a bare list
                return data
            results = (data or {}).get("results") or []
            out.extend(results)
            if not (data or {}).get("next") or not results:
                return out
            page += 1

    def export(self, project_id: int, dest: Path) -> Path:
        """
        Export a project's annotations as JSON.

        Prefers the snapshot API (handles large projects server-side), falling
        back to the synchronous /export endpoint on older installs.
        """
        try:
            snap = self.request(
                "POST", f"/api/projects/{project_id}/exports",
                data={"title": "lbs-hours"}, params={"exportType": "JSON"},
            )
            pk = (snap or {}).get("id")
            if pk is None:
                raise LabelStudioError("snapshot response had no id")
            waited = 0.0
            while True:
                info = self.request("GET", f"/api/projects/{project_id}/exports/{pk}") or {}
                status = (info.get("status") or "").lower()
                if status == "completed":
                    break
                if status == "failed":
                    raise LabelStudioError(f"snapshot failed: {info.get('error')}")
                if waited >= POLL_LIMIT:
                    raise LabelStudioError(f"snapshot still '{status}' after {POLL_LIMIT}s")
                time.sleep(POLL_INTERVAL)
                waited += POLL_INTERVAL
            return self.download(f"/api/projects/{project_id}/exports/{pk}/download",
                                 dest, params={"exportType": "JSON"})
        except LabelStudioError as exc:
            print(f"  snapshot export unavailable ({exc.args[0].splitlines()[0][:120]});"
                  " falling back to direct export", file=sys.stderr)
            return self.download(f"/api/projects/{project_id}/export", dest,
                                 params={"exportType": "JSON",
                                         "download_all_tasks": "false"})


# --------------------------------------------------------------------------- #
# Duration maths
# --------------------------------------------------------------------------- #

def to_seconds(value) -> float | None:
    """Coerce a duration field to seconds. Accepts numbers, numeric strings, HH:MM:SS."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
            return num if num > 0 else None
        except ValueError:
            pass
        parts = text.split(":")
        if 2 <= len(parts) <= 3:
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                return None
            total = 0.0
            for num in nums:
                total = total * 60 + num
            return total if total > 0 else None
    return None


def to_offset(value) -> float | None:
    """Coerce a region offset to seconds. Unlike to_seconds, 0 is a valid value."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return to_seconds(text)
    return None


def regions(annotation: dict) -> dict[str, tuple[float, float, bool]]:
    """
    Collect the distinct labelled audio regions in one annotation.

    A single region usually produces several result entries that share a region
    id - e.g. a `labels` entry for the speaker and a `textarea` entry for the
    transcript. Keying on the region id collapses them so the span is counted
    once. Returns {key: (start, end, has_text)}.
    """
    found: dict[str, tuple[float, float, bool]] = {}
    for item in annotation.get("result") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        start, end = to_offset(value.get("start")), to_offset(value.get("end"))
        if start is None or end is None or end <= start or start < 0:
            continue
        texts = value.get("text") or value.get("transcription") or []
        if isinstance(texts, str):
            texts = [texts]
        has_text = any(str(t).strip() for t in texts if t is not None)
        key = item.get("id") or f"{round(start, 3)}:{round(end, 3)}"
        prev = found.get(key)
        if prev is None:
            found[key] = (start, end, has_text)
        else:
            # same region seen twice: keep the widest span, OR the text flags
            found[key] = (min(prev[0], start), max(prev[1], end), prev[2] or has_text)
    return found


def merge_spans(spans: list[tuple[float, float]]) -> float:
    """Total wall-clock seconds covered by spans, overlaps counted once."""
    if not spans:
        return 0.0
    ordered = sorted(spans)
    total, (cur_start, cur_end) = 0.0, ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    return total + (cur_end - cur_start)


def file_duration(task: dict, field: str | None) -> float | None:
    """Pull a whole-file audio duration out of a task, if it carries one."""
    data = task.get("data") or {}
    if field:
        return to_seconds(data.get(field))
    for key in DURATION_KEYS:
        for holder in (data, task):
            if key in holder:
                seconds = to_seconds(holder[key])
                if seconds:
                    return seconds
    for key, value in data.items():
        if any(k in key.lower() for k in ("duration", "_len", "length")):
            seconds = to_seconds(value)
            if seconds:
                return seconds
    return None


def audio_ref(task: dict) -> str | None:
    data = task.get("data") or {}
    for key in AUDIO_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in data.values():
        if isinstance(value, str) and value.lower().rsplit(".", 1)[-1] in (
            "wav", "mp3", "flac", "ogg", "m4a", "opus", "aac", "webm", "mp4"
        ):
            return value.strip()
    return None


# --------------------------------------------------------------------------- #
# ffprobe fallback for whole-file projects with no duration field
# --------------------------------------------------------------------------- #

class Prober:
    """Probe audio durations with ffprobe, downloading via the API when needed."""

    def __init__(self, client: Client | None, cache_path: Path, enabled: bool):
        self.client = client
        self.enabled = enabled
        self.cache_path = cache_path
        self.cache: dict[str, float | None] = {}
        self.have_ffprobe = bool(_which("ffprobe"))
        if enabled and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                self.cache = {}
        if enabled and not self.have_ffprobe:
            print("  --probe-missing needs ffprobe on PATH (brew install ffmpeg); skipping",
                  file=sys.stderr)
            self.enabled = False

    def duration(self, ref: str | None) -> float | None:
        if not (self.enabled and ref):
            return None
        if ref in self.cache:
            return self.cache[ref]
        seconds = self._probe(ref)
        self.cache[ref] = seconds
        return seconds

    def _probe(self, ref: str) -> float | None:
        tmp = None
        try:
            target = ref
            if ref.startswith(("s3://", "gs://", "azure://")):
                return None
            if not ref.startswith(("http://", "https://")):
                # Label Studio-hosted media: needs the auth header, so fetch it
                if self.client is None:
                    return None
                suffix = "." + ref.rsplit(".", 1)[-1] if "." in ref.rsplit("/", 1)[-1] else ""
                tmp = self.cache_path.parent / f".probe{abs(hash(ref))}{suffix}"
                self.client.download(ref if ref.startswith("/") else "/" + ref, tmp)
                target = str(tmp)
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", target],
                capture_output=True, text=True, timeout=120,
            )
            return to_seconds(out.stdout.strip()) if out.returncode == 0 else None
        except (LabelStudioError, subprocess.SubprocessError, OSError):
            return None
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def save(self):
        if self.enabled and self.cache:
            try:
                self.cache_path.write_text(json.dumps(self.cache, indent=1))
            except OSError:
                pass


def _which(name):
    from shutil import which
    return which(name)


# --------------------------------------------------------------------------- #
# Tallying
# --------------------------------------------------------------------------- #

class Tally:
    def __init__(self, label: str):
        self.label = label
        self.tasks_total = 0
        self.tasks_counted = 0
        self.annotations = 0
        self.annotations_skipped = 0
        self.segments = 0
        self.segment_seconds = 0.0
        self.merged_seconds = 0.0
        self.text_seconds = 0.0
        self.file_seconds = 0.0
        self.files_counted = 0
        self.files_unknown = 0
        self.lead_seconds = 0.0
        self.rows: list[dict] = []
        # when this tally is a roll-up of several projects, each project
        # contributes its own headline figure (segment- or file-based), since a
        # combined counter cannot pick one basis for a mix of project shapes.
        self.is_aggregate = False
        self.headline_sum = 0.0
        self.file_based_projects = 0
        self.segment_based_projects = 0

    @property
    def segment_based(self) -> bool:
        return self.segments > 0

    @property
    def mixed(self) -> bool:
        return bool(self.segment_based_projects and self.file_based_projects)

    @property
    def total_hours(self) -> float:
        """Headline figure: merged segment coverage, or whole-file duration."""
        if self.is_aggregate:
            return self.headline_sum / 3600.0
        base = self.merged_seconds if self.segment_based else self.file_seconds
        return base / 3600.0

    def absorb(self, other: "Tally"):
        for attr in ("tasks_total", "tasks_counted", "annotations", "annotations_skipped",
                     "segments", "segment_seconds", "merged_seconds", "text_seconds",
                     "file_seconds", "files_counted", "files_unknown", "lead_seconds"):
            setattr(self, attr, getattr(self, attr) + getattr(other, attr))
        self.is_aggregate = True
        self.headline_sum += other.total_hours * 3600.0
        if other.segment_based:
            self.segment_based_projects += 1
        else:
            self.file_based_projects += 1


def tally_tasks(tasks: list[dict], label: str, args, prober: Prober) -> Tally:
    tally = Tally(label)
    for task in tasks:
        tally.tasks_total += 1
        anns = [a for a in (task.get("annotations") or []) if isinstance(a, dict)]
        usable = []
        for ann in anns:
            if ann.get("was_cancelled") and not args.include_cancelled:
                tally.annotations_skipped += 1
                continue
            if args.only_ground_truth and not ann.get("ground_truth"):
                tally.annotations_skipped += 1
                continue
            usable.append(ann)
        if not usable:
            continue

        counted = usable if args.count == "all-annotations" else usable[:1]
        tally.tasks_counted += 1
        tally.annotations += len(counted)

        spans: list[tuple[float, float]] = []
        seg_seconds = text_seconds = 0.0
        for ann in counted:
            tally.lead_seconds += to_seconds(ann.get("lead_time")) or 0.0
            for start, end, has_text in regions(ann).values():
                if args.require_text and not has_text:
                    continue
                spans.append((start, end))
                seg_seconds += end - start
                if has_text:
                    text_seconds += end - start

        merged = merge_spans(spans)
        tally.segments += len(spans)
        tally.segment_seconds += seg_seconds
        tally.merged_seconds += merged
        tally.text_seconds += text_seconds

        seconds = file_duration(task, args.duration_field)
        if seconds is None:
            seconds = prober.duration(audio_ref(task))
        if seconds is None:
            tally.files_unknown += 1
        else:
            tally.file_seconds += seconds
            tally.files_counted += 1

        if args.csv:
            tally.rows.append({
                "project": label,
                "task_id": task.get("id"),
                "annotations_counted": len(counted),
                "segments": len(spans),
                "segment_seconds": round(seg_seconds, 3),
                "merged_seconds": round(merged, 3),
                "transcribed_seconds": round(text_seconds, 3),
                "file_seconds": round(seconds, 3) if seconds is not None else "",
                "audio": audio_ref(task) or "",
            })
    return tally


def hhmm(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def report(tally: Tally, indent="  "):
    pad = indent
    print(f"{pad}tasks with annotations : {tally.tasks_counted:,} of {tally.tasks_total:,}")
    print(f"{pad}annotations counted    : {tally.annotations:,}"
          + (f"  ({tally.annotations_skipped:,} skipped/filtered out)"
             if tally.annotations_skipped else ""))
    if tally.segment_based:
        scope = " (segment projects only)" if tally.mixed else ""
        print(f"{pad}labelled segments      : {tally.segments:,}{scope}")
        print(f"{pad}segment total          : {tally.segment_seconds / 3600:>8.2f} h"
              f"   ({hhmm(tally.segment_seconds)})")
        print(f"{pad}merged coverage        : {tally.merged_seconds / 3600:>8.2f} h"
              f"   ({hhmm(tally.merged_seconds)})  <- overlaps counted once")
        print(f"{pad}with transcript text   : {tally.text_seconds / 3600:>8.2f} h")
    else:
        print(f"{pad}labelled segments      : none (whole-file transcription)")
    unknown = f"  ({tally.files_unknown:,} duration unknown)" if tally.files_unknown else ""
    print(f"{pad}audio files total      : {tally.file_seconds / 3600:>8.2f} h"
          f"   ({tally.files_counted:,} files){unknown}")
    if tally.lead_seconds:
        print(f"{pad}labelling time spent   : {tally.lead_seconds / 3600:>8.2f} h"
              f"   (annotator lead_time)")
    if tally.mixed:
        basis = (f"per project - {tally.segment_based_projects} by segment coverage, "
                 f"{tally.file_based_projects} by whole file")
    elif tally.segment_based:
        basis = "merged segment coverage"
    else:
        basis = "whole audio files"
    print(f"{pad}\033[1mAUDIO HOURS            : {tally.total_hours:>8.2f} h\033[0m"
          f"   (basis: {basis})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Download Label Studio annotations and total up audio transcription hours.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url", default=os.environ.get("LABEL_STUDIO_URL"),
                   help="Label Studio base URL (env: LABEL_STUDIO_URL)")
    p.add_argument("--token", default=os.environ.get("LABEL_STUDIO_TOKEN"),
                   help="Personal Access Token or legacy API key (env: LABEL_STUDIO_TOKEN)")
    p.add_argument("--project", nargs="+", default=None, metavar="ID",
                   help="project id(s), or 'all' for every project you can see")
    p.add_argument("--list-projects", action="store_true",
                   help="list visible projects and exit")
    p.add_argument("--from-file", nargs="+", type=Path, metavar="JSON",
                   help="skip the download, tally these already-exported JSON files")
    p.add_argument("--out", type=Path, default=Path("exports"),
                   help="directory for downloaded exports (default: exports/)")
    p.add_argument("--reuse", action="store_true",
                   help="reuse an existing export file in --out instead of re-downloading")
    p.add_argument("--count", choices=("unique-task", "all-annotations"),
                   default="unique-task",
                   help="count one annotation per task (default, = audio hours) or every "
                        "annotation (= annotation effort, double counts duplicated audio)")
    p.add_argument("--require-text", action="store_true",
                   help="only count regions that actually carry transcript text")
    p.add_argument("--include-cancelled", action="store_true",
                   help="include annotations the annotator skipped (was_cancelled)")
    p.add_argument("--only-ground-truth", action="store_true",
                   help="only count annotations flagged as ground truth")
    p.add_argument("--duration-field", default=os.environ.get("LABEL_STUDIO_DURATION_FIELD"),
                   help="task.data key holding the audio duration (auto-detected otherwise)")
    p.add_argument("--probe-missing", action="store_true",
                   help="probe durations with ffprobe when no duration field exists "
                        "(downloads audio; results are cached)")
    p.add_argument("--csv", type=Path, help="write a per-task breakdown to this CSV")
    p.add_argument("--insecure", action="store_true",
                   default=os.environ.get("LABEL_STUDIO_INSECURE", "").lower()
                   in ("1", "true", "yes", "on"),
                   help="skip TLS certificate verification, for a server with a bad or "
                        "expired cert (env: LABEL_STUDIO_INSECURE=1)")
    p.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")
    return p.parse_args(argv)


def read_export(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("tasks") or data.get("results") or [data]
    if not isinstance(data, list):
        raise LabelStudioError(f"{path}: unexpected export shape {type(data).__name__}")
    return data


def main(argv=None) -> int:
    load_env_file(Path(__file__).resolve().parent / ".env")
    args = parse_args(argv)
    if not (args.project or args.list_projects or args.from_file):
        print("Nothing to do: pass --project <id ...> / --project all, --list-projects, "
              "or --from-file <export.json>.", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    client = None
    if not args.from_file:
        if args.insecure:
            print("TLS certificate verification is OFF for this run (--insecure).",
                  file=sys.stderr)
        client = Client(args.url, args.token, insecure=args.insecure, verbose=args.verbose)
        client.auth_headers()  # fail fast, and before any other output

    if args.list_projects:
        projects = client.projects()
        print(f"\n{'ID':>6}  {'TASKS':>7}  {'DONE':>7}  TITLE")
        for proj in projects:
            print(f"{proj.get('id'):>6}  {proj.get('task_number') or 0:>7}"
                  f"  {proj.get('num_tasks_with_annotations') or 0:>7}"
                  f"  {proj.get('title')}")
        if not projects:
            print("  (none visible to this token)")
        return 0

    prober = Prober(client, args.out / ".duration-cache.json", args.probe_missing)
    sources: list[tuple[str, Path]] = []

    if args.from_file:
        sources = [(p.stem, p) for p in args.from_file]
    else:
        if len(args.project) == 1 and args.project[0].lower() == "all":
            projects = [(p["id"], p.get("title") or f"project {p['id']}")
                        for p in client.projects()]
        else:
            projects = []
            for raw in args.project:
                try:
                    projects.append((int(raw), None))
                except ValueError:
                    print(f"Not a project id: {raw!r}", file=sys.stderr)
                    return 2
        if not projects:
            print("No projects visible to this token.", file=sys.stderr)
            return 1
        for pid, title in projects:
            dest = args.out / f"project-{pid}.json"
            label = f"{pid} {title}" if title else str(pid)
            if args.reuse and dest.exists():
                print(f"Reusing {dest}")
            else:
                print(f"Exporting project {label} -> {dest}")
                client.export(pid, dest)
            sources.append((label, dest))

    grand = Tally("TOTAL")
    rows: list[dict] = []
    print()
    for label, path in sources:
        tasks = read_export(path)
        tally = tally_tasks(tasks, label, args, prober)
        print(f"Project {label}   [{path}]")
        report(tally)
        print()
        rows.extend(tally.rows)
        grand.absorb(tally)

    prober.save()

    if len(sources) > 1:
        print("=" * 68)
        print("ALL PROJECTS")
        report(grand)
        print()

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "project", "task_id", "annotations_counted", "segments",
                "segment_seconds", "merged_seconds", "transcribed_seconds",
                "file_seconds", "audio",
            ])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Per-task breakdown -> {args.csv} ({len(rows):,} rows)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LabelStudioError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
