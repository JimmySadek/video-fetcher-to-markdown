#!/usr/bin/env python3
"""Transcribe a video from Instagram, TikTok, X, Vimeo and other sites, or a local media file.

yt-dlp downloads the media, ffmpeg prepares the audio, a local Whisper command-line tool
(mlx_whisper on Apple Silicon, otherwise whisper) writes the transcript, and ffmpeg makes a
contact sheet of frames so on-screen text can be read. Use fetch_transcript.py for YouTube
videos that have captions: it is faster and downloads nothing.

Exit codes:
    0 - Success
    1 - Runtime error (download, transcription or filesystem)
    2 - Missing dependencies or invalid CLI options
    3 - Existing output preserved
    4 - The site needs a login or blocked access (see the browser fallback in SKILL.md)
    130 - User cancelled
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

_CORE_PATH = Path(__file__).resolve().parent / "fetch_transcript.py"
_CORE_SPEC = importlib.util.spec_from_file_location("_youtube_fetcher_core", _CORE_PATH)
core = importlib.util.module_from_spec(_CORE_SPEC)
assert _CORE_SPEC and _CORE_SPEC.loader
_CORE_SPEC.loader.exec_module(core)

EXIT_SUCCESS = core.EXIT_SUCCESS
EXIT_ERROR = core.EXIT_ERROR
EXIT_MISSING_DEPS = core.EXIT_MISSING_DEPS
EXIT_DUPLICATE_SKIPPED = core.EXIT_DUPLICATE_SKIPPED
EXIT_ACCESS_BLOCKED = 4

DEFAULT_TIMEOUT = 30.0
FRAMES_AUTO_MAX_SECONDS = 180
DEFAULT_TILES = 24
TILE_COLUMNS = 6

ENGINES = {
    # Option spelling differs: mlx_whisper uses dashes, OpenAI whisper uses underscores.
    "mlx": {"command": "mlx_whisper", "model": "mlx-community/whisper-large-v3-turbo", "sep": "-"},
    "whisper": {"command": "whisper", "model": "turbo", "sep": "_"},
}

ACCESS_PATTERN = re.compile(
    r"login required|log ?in|sign in|--cookies|cookies-from-browser|private|rate.?limit"
    r"|registered users|authenticat|HTTP Error 40[13]|not available|age.?restrict",
    re.I,
)

BROWSER_FALLBACK = """\
The site needs a login or blocked the download. Options, in order:
  1. Browser fallback (no login): open the page in a browser tool, find the direct media links
     (page HTML or network requests; on Instagram the DASH manifest lists a separate audio link
     whose efg tag mentions "audio" and video links by bitrate), download them at once with curl
     (they expire), then run:
       fetch_media.py --source-url URL [--audio-file AUDIO] -- VIDEO_FILE
  2. Only if the user asks: --cookies-from-browser BROWSER reuses their browser login.
  3. Ask the user for the file."""


YTDLP_STALE_DAYS = 60


def ytdlp_age_note(today: date | None = None) -> str:
    """Warn when yt-dlp is old: site extractors break often and updates fix most blocks."""
    try:
        version = run_command(["yt-dlp", "--version"], timeout=20).stdout.strip()
        released = date(*[int(x) for x in version.split(".")[:3]])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return ""
    age = ((today or date.today()) - released).days
    if age < YTDLP_STALE_DAYS:
        return ""
    return (
        f"yt-dlp {version} is {age} days old, and sites change often. Updating it may fix this "
        "(brew upgrade yt-dlp, or pipx upgrade yt-dlp); ask the user before updating.\n"
    )


class AccessBlocked(RuntimeError):
    """The site refused anonymous access."""


class FetchFailed(RuntimeError):
    """A download, probe or transcription step failed."""


# ── Dependencies ───────────────────────────────────────────────────────────
def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source, re.I))


def available_engine(requested: str = "auto") -> str | None:
    """Return the engine to use, or None when no Whisper command is on PATH."""
    if requested != "auto":
        return requested if shutil.which(ENGINES[requested]["command"]) else None
    apple_silicon = sys.platform == "darwin" and platform.machine() == "arm64"
    order = ["mlx", "whisper"] if apple_silicon else ["whisper", "mlx"]
    for name in order:
        if shutil.which(ENGINES[name]["command"]):
            return name
    return None


def check_dependencies(need_ytdlp: bool, engine: str = "auto") -> list[dict]:
    missing = []
    for binary, install in (
        ("ffmpeg", "brew install ffmpeg  # or your system package manager"),
        ("ffprobe", "installed with ffmpeg"),
    ):
        if not shutil.which(binary):
            missing.append({"name": binary, "type": "system", "install": install})
    if need_ytdlp and not shutil.which("yt-dlp"):
        missing.append(
            {"name": "yt-dlp", "type": "system", "install": "brew install yt-dlp  # or: pipx install yt-dlp"}
        )
    if available_engine(engine) is None:
        install = (
            "pipx install mlx-whisper  # Apple Silicon; or: pipx install openai-whisper"
            if engine in ("auto", "mlx")
            else "pipx install openai-whisper"
        )
        missing.append({"name": "Whisper command-line tool", "type": "system", "install": install})
    return missing


# ── Subprocess helpers ─────────────────────────────────────────────────────
def run_command(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _tail(text: str, lines: int = 4) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:]) or "no details"


def _ytdlp_failure(result: subprocess.CompletedProcess) -> Exception:
    detail = _tail(result.stderr)
    if ACCESS_PATTERN.search(result.stderr or ""):
        return AccessBlocked(detail)
    return FetchFailed(f"yt-dlp failed: {detail}")


def ytdlp_base(timeout: float, cookies_from_browser: str | None) -> list[str]:
    command = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-cache-dir",
        "--no-warnings",
        "--socket-timeout",
        str(timeout),
        "--retries",
        "1",
    ]
    if cookies_from_browser:
        command += ["--cookies-from-browser", cookies_from_browser]
    return command


def fetch_remote_metadata(url: str, timeout: float, cookies_from_browser: str | None = None) -> dict:
    command = ytdlp_base(timeout, cookies_from_browser) + ["--skip-download", "--dump-single-json", "--", url]
    result = run_command(command, timeout=timeout * 4)
    if result.returncode != 0:
        raise _ytdlp_failure(result)
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise FetchFailed("yt-dlp returned unreadable metadata") from error
    vcodec = str(data.get("vcodec") or "")
    has_video = bool(data.get("width")) or (vcodec not in ("", "none"))
    return {
        "title": data.get("title") or data.get("description", "")[:60] or "Untitled",
        "creator": data.get("uploader") or data.get("channel") or data.get("creator") or "Unknown",
        "description": data.get("description") or "",
        "duration": float(data.get("duration") or 0),
        "upload_date": core._format_upload_date(str(data.get("upload_date") or "")),
        "platform": data.get("extractor_key") or data.get("extractor") or "web",
        "media_id": str(data.get("id") or ""),
        "url": data.get("webpage_url") or url,
        "has_video": has_video,
    }


def download_remote(
    url: str,
    workdir: Path,
    want_video: bool,
    timeout: float,
    cookies_from_browser: str | None = None,
) -> Path:
    fmt = "b[height<=1080]/bv*[height<=1080]+ba/b" if want_video else "ba/b"
    command = ytdlp_base(timeout, cookies_from_browser) + ["-f", fmt]
    if want_video:
        command += ["--merge-output-format", "mp4"]
    command += ["-o", str(workdir / "media.%(ext)s"), "--", url]
    result = run_command(command)
    if result.returncode != 0:
        raise _ytdlp_failure(result)
    files = [p for p in workdir.glob("media.*") if p.suffix not in (".part", ".json", ".ytdl")]
    if not files:
        raise FetchFailed("yt-dlp finished but saved no media file")
    return max(files, key=lambda p: p.stat().st_size)


def probe_media(path: Path) -> dict:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise FetchFailed(f"ffprobe could not read {path.name}: {_tail(result.stderr)}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    has_video = any(
        s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        for s in streams
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration": duration, "has_video": has_video, "has_audio": has_audio}


def extract_audio(source: Path, destination: Path) -> None:
    result = run_command(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(destination)]
    )
    if result.returncode != 0:
        raise FetchFailed(f"ffmpeg could not extract audio: {_tail(result.stderr)}")


# ── Transcription ──────────────────────────────────────────────────────────
def vocabulary_prompt(hint: str) -> str:
    words = ", ".join(w.strip() for w in hint.split(",") if w.strip())
    return f"Names and terms that may be spoken: {words}." if words else ""


def transcribe(
    audio: Path,
    workdir: Path,
    engine: str,
    model: str | None = None,
    language: str | None = None,
    hint: str = "",
) -> tuple[list[dict], str]:
    config = ENGINES[engine]
    sep = config["sep"]
    out_dir = workdir / "whisper"
    out_dir.mkdir(exist_ok=True)
    command = [
        config["command"],
        str(audio),
        "--model",
        model or config["model"],
        f"--output{sep}format",
        "json",
        f"--output{sep}dir",
        str(out_dir),
        "--verbose",
        "False",
    ]
    if language:
        command += ["--language", language]
    prompt = vocabulary_prompt(hint)
    if prompt:
        command += [f"--initial{sep}prompt", prompt]
    result = run_command(command)
    if result.returncode != 0:
        raise FetchFailed(f"{config['command']} failed: {_tail(result.stderr)}")
    outputs = sorted(out_dir.glob("*.json"))
    if not outputs:
        raise FetchFailed(f"{config['command']} wrote no transcript")
    data = json.loads(outputs[0].read_text(encoding="utf-8"))
    segments = []
    for item in data.get("segments") or []:
        text = core.sanitize_inline_text(item.get("text", ""))
        if text:
            start = float(item.get("start") or 0)
            end = max(start, float(item.get("end") or start))
            segments.append({"start": start, "end": end, "text": text})
    return segments, (data.get("language") or language or "unknown")


# ── Frames ─────────────────────────────────────────────────────────────────
def should_capture_frames(choice: str | None, duration: float, has_video: bool) -> bool:
    if not has_video:
        return False
    if choice is not None:
        return choice == "on"
    return 0 < duration <= FRAMES_AUTO_MAX_SECONDS


def tile_times(duration: float, tiles: int) -> list[float]:
    step = duration / tiles if tiles else 0
    return [round(i * step, 2) for i in range(tiles) if i * step < duration]


def make_contact_sheet(source: Path, duration: float, destination: Path, tiles: int = DEFAULT_TILES) -> list[float]:
    """Write one image of evenly spaced frames, left to right then top to bottom."""
    rows = math.ceil(tiles / TILE_COLUMNS)
    rate = tiles / max(duration, 0.1)
    vf = f"fps={rate:.6f},scale='if(gt(iw,ih),360,270)':-2,tile={TILE_COLUMNS}x{rows}"
    result = run_command(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-vf", vf, "-frames:v", "1", "-q:v", "3", str(destination)]
    )
    if result.returncode != 0 or not destination.exists():
        raise FetchFailed(f"ffmpeg could not make the contact sheet: {_tail(result.stderr)}")
    return tile_times(duration, tiles)


# ── Output ─────────────────────────────────────────────────────────────────
def format_srt_time(seconds: float, comma: bool = True) -> str:
    millis = int(round(seconds * 1000))
    h, rem = divmod(millis, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def render_raw(segments: list[dict], fmt: str, timestamps: bool = False) -> str:
    if fmt == "json":
        # Same shape as fetch_transcript.py's raw JSON: text, start, duration.
        items = [
            {"text": s["text"], "start": round(s["start"], 3), "duration": round(s["end"] - s["start"], 3)}
            for s in segments
        ]
        return json.dumps(items, indent=2, ensure_ascii=False)
    if fmt == "srt":
        blocks = [
            f"{i}\n{format_srt_time(s['start'])} --> {format_srt_time(s['end'])}\n{s['text']}\n"
            for i, s in enumerate(segments, 1)
        ]
        return "\n".join(blocks)
    if fmt == "vtt":
        blocks = [
            f"{format_srt_time(s['start'], False)} --> {format_srt_time(s['end'], False)}\n{s['text']}\n"
            for s in segments
        ]
        return "WEBVTT\n\n" + "\n".join(blocks)
    return "\n".join(
        f"[{core.format_timestamp(s['start'])}] {s['text']}" if timestamps else s["text"] for s in segments
    )


def build_markdown(
    meta: dict,
    segments: list[dict],
    language: str,
    engine: str,
    model: str,
    fetched: str,
    source_project: str,
    hint: str = "",
    frames_file: str = "",
    frame_times: list[float] | None = None,
    include_description: bool = True,
) -> str:
    q = core.yaml_quote
    cell = core.sanitize_table_value
    front = [
        f"title: {q(meta['title'])}",
        f"creator: {q(meta['creator'])}",
        f"url: {q(meta['url'])}",
        f"platform: {q(meta['platform'])}",
        f"media_id: {q(meta['media_id'])}",
        f"fetched: {q(fetched)}",
        f"source_project: {q(source_project)}",
        f"language: {q(language)}",
        'transcript_source: "whisper"',
        f"transcription_engine: {q(ENGINES[engine]['command'])}",
        f"transcription_model: {q(model)}",
    ]
    rows = [
        f"| URL | {cell(meta['url'])} |",
        f"| Platform | {cell(meta['platform'])} |",
        f"| Creator | {cell(meta['creator'])} |",
    ]
    if meta.get("duration"):
        front.append(f"duration: {q(core.format_duration(int(meta['duration'])))}")
        rows.append(f"| Duration | {core.format_duration(int(meta['duration']))} |")
    if meta.get("upload_date"):
        front.append(f"upload_date: {q(meta['upload_date'])}")
        rows.append(f"| Uploaded | {cell(meta['upload_date'])} |")
    if hint:
        front.append(f"vocabulary_hint: {q(hint)}")
    if frames_file:
        front.append(f"frames: {q(frames_file)}")
    rows += [
        f"| Fetched | {fetched} |",
        f"| Source | {cell(source_project)} |",
        f"| Language | {cell(language)} (machine transcription) |",
        f"| Transcription | {cell(ENGINES[engine]['command'])}, {cell(model)} |",
    ]

    sections = []
    description = (meta.get("description") or "").strip()
    if include_description and description:
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in description.splitlines())
        sections.append(f"## Description\n\n{quoted}")
    if frames_file:
        times = " · ".join(f"{i} {core.format_timestamp(t)}" for i, t in enumerate(frame_times or [], 1))
        sections.append(
            f"## Frames\n\n![Contact sheet](<{frames_file}>)\n\n"
            f"Tiles read left to right, top to bottom: {times}"
        )
    transcript = render_raw(segments, "txt", timestamps=True) if segments else "_No speech was detected._"
    sections.append(
        "## Transcript\n\n> Machine transcription (Whisper). Names and terms may be misheard.\n\n" + transcript
    )
    heading = core.sanitize_inline_text(meta["title"]) or "Untitled"
    return (
        "---\n" + "\n".join(front) + "\ntags:\n  - media-transcript\n---\n\n"
        f"# {heading}\n\n## Media Details\n\n| Field | Value |\n|-------|-------|\n"
        + "\n".join(rows)
        + "\n\n"
        + "\n\n".join(sections)
        + "\n"
    )


def media_key(meta: dict) -> str:
    return core.slugify(f"{meta['platform']}-{meta['media_id']}")


# ── Main ───────────────────────────────────────────────────────────────────
def id_from_url(url: str) -> str:
    """Last meaningful path part of a page URL, e.g. the reel code in /reel/<code>/."""
    path = re.sub(r"^https?://[^/]+", "", url or "").split("?")[0].split("#")[0]
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


def local_metadata(path: Path, args) -> dict:
    return {
        "title": args.title or path.stem,
        "creator": args.creator or "Unknown",
        "description": "",
        "duration": 0.0,
        "upload_date": "",
        "platform": args.platform or ("web" if args.source_url else "local file"),
        "media_id": id_from_url(args.source_url) or core.slugify(path.stem),
        "url": args.source_url or str(path.resolve()),
        "has_video": False,
    }


def run(args) -> int:
    source = args.source
    remote = is_url(source)
    if not remote and not Path(source).expanduser().is_file():
        print(f"Error: '{source}' is neither an http(s) URL nor an existing file.", file=sys.stderr)
        return EXIT_ERROR
    if args.audio_file and not Path(args.audio_file).expanduser().is_file():
        print(f"Error: audio file not found: {args.audio_file}", file=sys.stderr)
        return EXIT_ERROR

    missing = check_dependencies(need_ytdlp=remote, engine=args.engine)
    if missing:
        core.print_dependency_report(missing)
        return EXIT_MISSING_DEPS
    engine = available_engine(args.engine)
    model = args.model or ENGINES[engine]["model"]

    fmt = "md" if args.fmt in ("md", "markdown", "text") else args.fmt
    explicit = Path(args.output).expanduser() if args.output else None
    output_dir = core.resolve_output_directory(args.output, args.output_dir)
    if explicit and not args.stdout and explicit.exists() and not args.force:
        print(f"Existing file preserved: {explicit.absolute()}. Use --force only to replace it.", file=sys.stderr)
        return EXIT_DUPLICATE_SKIPPED

    with tempfile.TemporaryDirectory(prefix="media-fetcher-") as tmp:
        workdir = Path(tmp)
        try:
            if remote:
                meta = fetch_remote_metadata(source, args.timeout, args.cookies_from_browser)
            else:
                meta = local_metadata(Path(source).expanduser(), args)

            today = date.today().isoformat()
            out_path = explicit
            if out_path is None and not args.stdout:
                if fmt == "md" and not args.force:
                    existing = core.find_existing_transcript(media_key(meta), output_dir)
                    if existing:
                        print(f"Existing file preserved: {existing.absolute()}. Use --force only to replace it.", file=sys.stderr)
                        return EXIT_DUPLICATE_SKIPPED
                out_path = output_dir / f"{today}_{core.slugify(meta['title'])}_[{media_key(meta)}].{fmt}"

            if remote:
                want_video = fmt == "md" and should_capture_frames(args.frames, meta["duration"], meta["has_video"])
                print(f"Downloading from {meta['platform']}…", file=sys.stderr)
                media = download_remote(source, workdir, want_video or args.keep_media, args.timeout, args.cookies_from_browser)
            else:
                media = Path(source).expanduser()
            info = probe_media(media)
            meta["duration"] = meta["duration"] or info["duration"]
            meta["has_video"] = info["has_video"]

            audio_source = Path(args.audio_file).expanduser() if args.audio_file else media
            if audio_source is media and not info["has_audio"]:
                segments, language = [], args.lang or "none"
                print("No audio track found; the note has frames only.", file=sys.stderr)
            else:
                wav = workdir / "audio.wav"
                extract_audio(audio_source, wav)
                print(f"Transcribing with {ENGINES[engine]['command']} ({model})…", file=sys.stderr)
                segments, language = transcribe(wav, workdir, engine, model, args.lang, args.hint)

            if fmt != "md":
                output = render_raw(segments, fmt, args.timestamps)
                frames_file, times = "", []
            else:
                frames_file, times = "", []
                if should_capture_frames(args.frames, meta["duration"], info["has_video"]):
                    if args.stdout:
                        sheet = Path(tempfile.mkdtemp(prefix="media-fetcher-frames-")) / "frames.jpg"
                    else:
                        sheet = out_path.with_name(out_path.stem + ".frames.jpg")
                        if sheet.exists() and not args.force:
                            print(f"Existing file preserved: {sheet.absolute()}. Use --force only to replace it.", file=sys.stderr)
                            return EXIT_DUPLICATE_SKIPPED
                        sheet.parent.mkdir(parents=True, exist_ok=True)
                    times = make_contact_sheet(media, meta["duration"], sheet)
                    frames_file = str(sheet) if args.stdout else sheet.name
                    print(f"Frames: {sheet.absolute()}", file=sys.stderr)
                output = build_markdown(
                    meta,
                    segments,
                    language,
                    engine,
                    model,
                    today,
                    args.source_label or Path.cwd().name,
                    hint=args.hint,
                    frames_file=frames_file,
                    frame_times=times,
                    include_description=not args.no_description,
                )

            if args.keep_media and not args.stdout:
                kept = out_path.with_name(out_path.stem + media.suffix)
                if not kept.exists() or args.force:
                    shutil.copyfile(media, kept)
                    print(f"Media kept: {kept.absolute()}", file=sys.stderr)
        except AccessBlocked as error:
            print(f"Blocked: {error}\n\n{ytdlp_age_note()}{BROWSER_FALLBACK}", file=sys.stderr)
            return EXIT_ACCESS_BLOCKED
        except (FetchFailed, OSError, ValueError, subprocess.SubprocessError) as error:
            note = ytdlp_age_note() if remote and "yt-dlp" in str(error) else ""
            print(f"Error: {error}" + (f"\n{note}" if note else ""), file=sys.stderr)
            return EXIT_ERROR

    if args.stdout:
        print(output)
        return EXIT_SUCCESS
    try:
        core.write_output(out_path, output, force=args.force)
    except core.ExistingOutputError:
        print(f"Existing file preserved: {out_path.absolute()}. Use --force only to replace it.", file=sys.stderr)
        return EXIT_DUPLICATE_SKIPPED
    except OSError as error:
        print(f"Error: could not save {out_path}: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Saved to {out_path.absolute()}")
    return EXIT_SUCCESS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a video from Instagram, TikTok, X, Vimeo and other sites, or a local file.",
        epilog="Exit 4 means the site needs a login: see the browser fallback in SKILL.md.",
    )
    parser.add_argument("source", nargs="?", help="Video page URL, or a local video or audio file")
    parser.add_argument("--format", dest="fmt", default="md", choices=["md", "markdown", "text", "txt", "json", "srt", "vtt"],
                        help="md=archival note with frames (default); txt/json/srt/vtt=transcript only")
    parser.add_argument("--output", "-o", help="Exact output file")
    parser.add_argument("--output-dir", help="Directory for the default file name")
    parser.add_argument("--stdout", action="store_true", help="Print instead of saving (frames go to a temporary folder)")
    parser.add_argument("--force", action="store_true", help="Replace existing output")
    parser.add_argument("--timestamps", action="store_true", help="Add [mm:ss] to txt output")
    parser.add_argument("--lang", help="Spoken language code for Whisper (default: detect)")
    parser.add_argument("--hint", default="", help="Comma-separated names and terms Whisper should expect, e.g. 'Claude, HyperFrames'")
    parser.add_argument("--engine", default="auto", choices=["auto", "mlx", "whisper"], help="Whisper command-line tool")
    parser.add_argument("--model", help="Whisper model (default: large-v3-turbo for mlx, turbo for whisper)")
    frames = parser.add_mutually_exclusive_group()
    frames.add_argument("--frames", dest="frames", action="store_const", const="on", help="Always make a contact sheet")
    frames.add_argument("--no-frames", dest="frames", action="store_const", const="off", help="Never make a contact sheet")
    parser.add_argument("--keep-media", action="store_true", help="Save the downloaded media next to the note")
    parser.add_argument("--audio-file", help="Separate audio file for a local video without sound (browser fallback)")
    parser.add_argument("--source-url", help="Page URL to record for a local file")
    parser.add_argument("--title", help="Title for a local file")
    parser.add_argument("--creator", help="Creator for a local file")
    parser.add_argument("--platform", help="Platform name for a local file, e.g. Instagram")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="Reuse the user's browser login (chrome, safari, firefox…). Only when the user asks.")
    parser.add_argument("--no-description", action="store_true", help="Leave the creator's description out of the note")
    parser.add_argument("--source", dest="source_label", help="Capture-project label (default: current folder name)")
    parser.add_argument("--timeout", type=core.positive_timeout, default=DEFAULT_TIMEOUT, help="Network timeout in seconds")
    parser.add_argument("--check-deps", action="store_true", help="Report missing tools and exit")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.check_deps:
        missing = check_dependencies(need_ytdlp=True, engine=args.engine)
        if missing:
            core.print_dependency_report(missing)
            return EXIT_MISSING_DEPS
        print(f"All tools found. Whisper engine: {ENGINES[available_engine(args.engine)]['command']}")
        return EXIT_SUCCESS
    if not args.source:
        parser.print_usage(sys.stderr)
        print("Error: a URL or file is required.", file=sys.stderr)
        return EXIT_MISSING_DEPS
    if args.stdout and (args.output or args.output_dir):
        print("Error: --stdout cannot be combined with --output or --output-dir.", file=sys.stderr)
        return EXIT_MISSING_DEPS
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
