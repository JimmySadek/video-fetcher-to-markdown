#!/usr/bin/env python3
"""Fetch YouTube video transcripts and save as structured Markdown files.

Uses yt-dlp for video metadata (title, channel, description, duration, chapters)
and youtube-transcript-api for the actual transcript/captions.

Exit codes:
    0 - Success
    1 - Runtime error (fetch failed, invalid URL, etc.)
    2 - Missing dependencies or invalid CLI options
    3 - Existing output preserved
    130 - User cancelled
"""

from __future__ import annotations

import argparse
import errno
import importlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Exit codes ──────────────────────────────────────────────────────────────
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_MISSING_DEPS = 2
EXIT_DUPLICATE_SKIPPED = 3

DEFAULT_OUTPUT_DIRNAME = "yt_transcripts"
OUTPUT_DIR_ENV = "VIDEO_FETCHER_DIR"
LEGACY_OUTPUT_DIR_ENV = "YOUTUBE_FETCHER_DIR"  # the name before v2.0.0; still honoured
DEFAULT_TIMEOUT = 15.0
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
SHORT_YOUTUBE_HOSTS = {"youtu.be", "www.youtu.be"}


# ── Dependency checks ──────────────────────────────────────────────────────
def check_dependencies() -> list[dict]:
    """Check all required dependencies and return a list of missing ones."""
    missing = []

    # Python packages
    python_deps = [
        {
            "module": "youtube_transcript_api",
            "name": "youtube-transcript-api",
            "install": "python3 -m pip install youtube-transcript-api",
        },
        {
            "module": "requests",
            "name": "requests",
            "install": "python3 -m pip install requests",
        },
    ]
    for dep in python_deps:
        try:
            importlib.import_module(dep["module"])
        except ImportError:
            missing.append(
                {
                    "name": dep["name"],
                    "type": "python",
                    "install": dep["install"],
                }
            )

    # System binaries
    if not shutil.which("yt-dlp"):
        missing.append(
            {
                "name": "yt-dlp",
                "type": "system",
                "install": "brew install yt-dlp  # or: python3 -m pip install yt-dlp",
                "optional": True,
            }
        )

    return missing


def print_dependency_report(missing: list[dict]) -> None:
    """Print a clear, actionable dependency report."""
    required = [d for d in missing if not d.get("optional")]
    optional = [d for d in missing if d.get("optional")]

    if required:
        print("\n╔══════════════════════════════════════════════════╗", file=sys.stderr)
        print("║       Missing Required Dependencies              ║", file=sys.stderr)
        print("╚══════════════════════════════════════════════════╝\n", file=sys.stderr)
        for dep in required:
            print(f"  ✗ {dep['name']}", file=sys.stderr)
            print(f"    Install: {dep['install']}\n", file=sys.stderr)
        print("  ── Quick install all ──", file=sys.stderr)
        installs = " ".join(d["name"] for d in required if d["type"] == "python")
        if installs:
            print(f"  python3 -m pip install {installs}\n", file=sys.stderr)

    if optional:
        label = "\n" if required else ""
        print(f"{label}  ⚠ Optional (recommended):", file=sys.stderr)
        for dep in optional:
            print(f"    ○ {dep['name']} — {dep['install']}", file=sys.stderr)
            if dep["name"] == "yt-dlp":
                print(
                    "      (Without yt-dlp: no video description, chapters, or duration)\n",
                    file=sys.stderr,
                )


# ── Duplicate detection ────────────────────────────────────────────────────
def find_existing_transcript(video_id: str, transcripts_dir: Path) -> Path | None:
    """Find an existing transcript for this video_id.

    Fast path: match *_[VIDEO_ID].md filenames without reading file contents.
    Fallback: scan file contents for backwards compatibility with older files.
    """
    if not transcripts_dir.exists():
        return None

    # Fast path: video_id encoded in filename
    # Match literal brackets — glob() treats [] as character classes.
    matches = sorted(transcripts_dir.glob(f"*_[[]{video_id}[]].md"))
    if matches:
        return matches[0]

    # Fallback: scan frontmatter for older files without ID in filename
    for md_file in sorted(transcripts_dir.glob("*.md")):
        try:
            # Bound vault reads and match a field only inside frontmatter.
            with md_file.open(encoding="utf-8", errors="ignore") as f:
                head = f.read(16384)
            match = re.match(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", head, re.S)
            if match and re.search(
                rf"^video_id:\s*['\"]?{re.escape(video_id)}['\"]?\s*$",
                match.group(1),
                re.M,
            ):
                return md_file
        except OSError:
            continue
    return None


def get_existing_transcript_date(filepath: Path) -> str:
    """Extract the fetched date from an existing transcript's frontmatter."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r'fetched:\s*"(\d{4}-\d{2}-\d{2})"', content)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown date"


# ── Core helpers ───────────────────────────────────────────────────────────
def extract_video_id(url_or_id: str) -> str:
    """Extract a video ID from supported YouTube URLs or a raw ID.

    Hostnames are allow-listed so lookalike domains such as
    ``youtube.com.example.org`` are rejected.
    """
    candidate = url_or_id.strip()
    if VIDEO_ID_PATTERN.fullmatch(candidate):
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Could not extract a YouTube video ID from '{url_or_id}'")

    host = parsed.hostname.lower().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = None

    if host in SHORT_YOUTUBE_HOSTS and path_parts:
        video_id = path_parts[0]
    elif host in YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            video_id = values[0] if values else None
        elif len(path_parts) >= 2 and path_parts[0].lower() in {
            "embed",
            "live",
            "shorts",
            "v",
        }:
            video_id = path_parts[1]

    if video_id and VIDEO_ID_PATTERN.fullmatch(video_id):
        return video_id
    raise ValueError(f"Could not extract a YouTube video ID from '{url_or_id}'")


def resolve_output_directory(
    output: str | None,
    output_dir: str | None,
    environ: dict[str, str] | None = None,
) -> Path:
    """Resolve output directory using file, flag, environment, then default."""
    if output:
        return Path(output).expanduser().parent
    if output_dir:
        return Path(output_dir).expanduser()

    env = os.environ if environ is None else environ
    env_dir = env.get(OUTPUT_DIR_ENV, "").strip() or env.get(LEGACY_OUTPUT_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / DEFAULT_OUTPUT_DIRNAME


def yaml_quote(value) -> str:
    """Return a JSON-compatible double-quoted YAML scalar."""
    return json.dumps(str(value), ensure_ascii=False)


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    # Limit bytes, not code points: CJK titles otherwise exceed filesystem limits.
    return (
        text.strip("-")
        .encode("utf-8")[:80]
        .decode("utf-8", errors="ignore")
        .rstrip("-")
        or "video"
    )


def format_duration(seconds: int) -> str:
    """Convert seconds to human-readable duration."""
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def empty_metadata() -> dict:
    return {
        "title": "Untitled",
        "channel": "Unknown",
        "description": "",
        "duration": 0,
        "upload_date": "",
        "chapters": [],
        "metadata_source": "unavailable",
    }


def create_http_session(timeout: float):
    """Apply a connect/read timeout to every request made by the caption library."""
    from requests import Session

    class TimeoutSession(Session):
        def request(self, method, url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = timeout
            return super().request(method, url, **kwargs)

    return TimeoutSession()


def fetch_video_metadata(video_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch full video metadata via yt-dlp (title, channel, description, etc.)."""
    if not shutil.which("yt-dlp"):
        return _fetch_metadata_oembed(video_id, timeout)

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--ignore-config",
                "--no-playlist",
                "--no-cache-dir",
                "--skip-download",
                "--dump-single-json",
                "--no-warnings",
                "--socket-timeout",
                str(timeout),
                "--retries",
                "0",
                "--extractor-retries",
                "0",
                "--",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout * 2,
        )
        if result.returncode != 0:
            print("Warning: yt-dlp failed, falling back to oEmbed", file=sys.stderr)
            return _fetch_metadata_oembed(video_id, timeout)

        data = json.loads(result.stdout)
        return {
            "title": data.get("title") or "Untitled",
            "channel": data.get("channel") or data.get("uploader") or "Unknown",
            "description": data.get("description") or "",
            "duration": int(data.get("duration") or 0),
            "upload_date": _format_upload_date(data.get("upload_date", "")),
            "chapters": data.get("chapters") or [],
            "metadata_source": "yt-dlp",
        }
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        print("Warning: yt-dlp metadata failed; trying oEmbed.", file=sys.stderr)
        return _fetch_metadata_oembed(video_id, timeout)


def _fetch_metadata_oembed(video_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fallback: fetch basic metadata via YouTube oEmbed API."""
    import requests

    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title") or "Untitled",
            "channel": data.get("author_name") or "Unknown",
            "description": "",
            "duration": 0,
            "upload_date": "",
            "chapters": [],
            "metadata_source": "oembed",
        }
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        print(
            "Warning: video metadata is unavailable; saving the captions with their source URL.",
            file=sys.stderr,
        )
        return empty_metadata()


def _format_upload_date(raw: str) -> str:
    """Convert yt-dlp date format (YYYYMMDD) to readable (YYYY-MM-DD)."""
    if raw and len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def select_transcript(transcript_list, requested_language: str, strict: bool = False):
    """Prefer requested languages, then regional variants, then English if allowed."""
    tracks = list(transcript_list)
    requested_languages = [
        part.strip().lower() for part in requested_language.split(",")
    ]
    if not all(requested_languages) or (
        "auto" in requested_languages and len(requested_languages) > 1
    ):
        raise ValueError(
            "Use a language code, comma-separated preferences, or 'auto' on its own."
        )

    def match_language(code):
        exact = [t for t in tracks if t.language_code.lower() == code]
        variants = [t for t in tracks if t.language_code.lower().startswith(code + "-")]
        matches = exact or variants
        return min(matches, key=lambda t: t.is_generated) if matches else None

    selected = None
    if requested_languages == ["auto"]:
        # The API does not expose the video's original audio language here.
        # Prefer a manual caption track, retaining provider order for ties.
        selected = min(tracks, key=lambda t: t.is_generated) if tracks else None
    else:
        for language in requested_languages:
            selected = match_language(language)
            if selected is not None:
                break
        if selected is None and not strict:
            selected = match_language("en")
    if selected is None:
        available = ", ".join(dict.fromkeys(t.language_code for t in tracks)) or "none"
        raise ValueError(
            f"No captions match '{requested_language}'"
            f"{' (English fallback disabled)' if strict else ' or English'}. "
            f"Available: {available}. Use --list, --lang CODE, or --lang auto."
        )
    actual_language = selected.language_code
    actual_lower = actual_language.lower()
    caption_type = "auto-generated" if selected.is_generated else "manual"
    print(f"Selected captions: {actual_language} ({caption_type}).", file=sys.stderr)
    if requested_languages != ["auto"] and not any(
        actual_lower == lang or actual_lower.startswith(lang + "-")
        for lang in requested_languages
    ):
        print(
            f"Warning: requested captions '{requested_language}' were unavailable; "
            f"using '{actual_language}' instead.",
            file=sys.stderr,
        )

    return selected, actual_language, caption_type


def describe_fetch_error(error: Exception) -> str:
    """Actionable errors without dumping request bodies, proxy URLs, or credentials."""
    from requests import RequestException, Timeout
    from youtube_transcript_api import _errors

    if isinstance(error, (_errors.RequestBlocked, _errors.IpBlocked)):
        return "YouTube blocked requests from this network. Stop retrying; try later from a permitted local network. No file was saved."
    if isinstance(error, _errors.TranscriptsDisabled):
        return "YouTube did not expose accessible captions for this video. Supply a caption file or try another video."
    if isinstance(
        error,
        (_errors.AgeRestricted, _errors.VideoUnavailable, _errors.VideoUnplayable),
    ):
        return "This video is unavailable or access-restricted. Check that it is publicly playable and exposes captions."
    if isinstance(
        error, (_errors.NotTranslatable, _errors.TranslationLanguageNotAvailable)
    ):
        return "YouTube cannot translate this caption track to that language. Use --list to inspect translation targets, or omit --translate."
    if isinstance(error, Timeout):
        return "YouTube request timed out. Check your connection or choose a longer --timeout; no file was saved."
    if isinstance(error, RequestException):
        return "YouTube network request failed. Check connectivity and your network configuration; no file was saved."
    if isinstance(error, ValueError):
        return str(error)
    return f"Caption retrieval failed ({type(error).__name__}). Check the video and installed youtube-transcript-api version; no file was saved."


class ExistingOutputError(FileExistsError):
    """The destination itself exists, rather than a parent-directory error."""


def _write_exclusive(path: Path, content: str) -> None:
    """Fallback for filesystems without hard links; never open an old file for writing."""
    try:
        handle = path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as error:
        raise ExistingOutputError(str(path)) from error
    identity = os.fstat(handle.fileno())
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Remove only our incomplete file, not a different file moved into its place.
        try:
            if os.path.samestat(identity, path.lstat()):
                path.unlink()
        except OSError:
            pass
        raise


def write_output(path: Path, content: str, force: bool = False) -> None:
    """Save UTF-8 safely; prefer atomic publication and preserve existing modes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"Refusing to replace a symbolic link: {path}")
    existing_mode = (
        stat.S_IMODE(path.stat().st_mode) if force and path.exists() else None
    )
    temporary = None
    try:
        candidate = path.parent / f".youtube-fetcher-{uuid.uuid4().hex}.tmp"
        # Exclusive creation applies the user's normal umask, unlike mkstemp's 0600.
        with candidate.open("x", encoding="utf-8", newline="\n") as handle:
            temporary = candidate
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        if force:
            os.replace(temporary, path)
        else:
            # Unlike exists()+replace(), a hard link cannot clobber a concurrent writer.
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                raise ExistingOutputError(str(path)) from error
            except OSError as error:
                unsupported = {
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                    errno.EXDEV,
                    errno.ENOSYS,
                    errno.EPERM,
                }
                if error.errno not in unsupported and getattr(
                    error, "winerror", None
                ) not in {1, 50}:
                    raise
                _write_exclusive(path, content)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def sanitize_table_value(text: str) -> str:
    """Keep dynamic text inside one Markdown table cell."""
    return sanitize_inline_text(text).replace("|", "\\|")


def sanitize_inline_text(text) -> str:
    """Collapse dynamic text to one readable Markdown line."""
    return " ".join(str(text).replace("\x00", "").splitlines()).strip()


def timestamp_link(seconds: float, video_id: str) -> str:
    return f"[{format_timestamp(seconds)}](https://www.youtube.com/watch?v={video_id}&t={max(0, int(seconds))}s)"


def build_description_section(
    description: str, chapters: list, video_id: str = ""
) -> str:
    """Build the Video Description section from yt-dlp data."""
    if not description and not chapters:
        return ""

    parts = ["\n## Video Description\n"]

    if description:
        # Prevent stray --- lines from being parsed as frontmatter/thematic breaks
        safe_desc = re.sub(
            r"^-{3,}$",
            "\\---",
            str(description).replace("\x00", ""),
            flags=re.MULTILINE,
        )
        parts.append(safe_desc)

    if chapters:
        parts.append("\n### Chapters\n")
        for ch in chapters:
            start = ch.get("start_time", 0)
            ts = (
                timestamp_link(start, video_id)
                if video_id
                else f"`{format_timestamp(start)}`"
            )
            title = sanitize_inline_text(ch.get("title", ""))
            parts.append(f"- {ts} {title}")

    return "\n".join(parts)


def build_markdown(
    title: str,
    channel: str,
    video_id: str,
    fetched_date: str,
    source_project: str,
    language: str,
    caption_type: str,
    description_section: str,
    transcript_text: str,
    duration: int = 0,
    upload_date: str = "",
    requested_language: str = "",
    source_language: str = "",
    translated: bool = False,
    metadata_source: str = "",
) -> str:
    """Build the full Markdown file content with frontmatter and transcript."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    safe_heading = sanitize_inline_text(title) or "Untitled"
    safe_title = yaml_quote(title)
    safe_channel = yaml_quote(channel)
    safe_source = yaml_quote(source_project)
    safe_language = yaml_quote(language)
    safe_caption_type = yaml_quote(caption_type)

    # Build optional frontmatter fields
    extra_frontmatter = ""
    if duration:
        extra_frontmatter += f"\nduration: {yaml_quote(format_duration(duration))}"
    if upload_date:
        extra_frontmatter += f"\nupload_date: {yaml_quote(upload_date)}"
    if requested_language:
        extra_frontmatter += f"\nrequested_language: {yaml_quote(requested_language)}"
    if source_language:
        extra_frontmatter += f"\nsource_language: {yaml_quote(source_language)}"
        extra_frontmatter += f"\ntranslated: {'true' if translated else 'false'}"
    if translated:
        extra_frontmatter += '\ntranslation_provider: "YouTube"'
    if metadata_source:
        extra_frontmatter += f"\nmetadata_source: {yaml_quote(metadata_source)}"

    # Build optional table rows
    extra_rows = ""
    if duration:
        extra_rows += f"\n| Duration | {format_duration(duration)} |"
    if upload_date:
        extra_rows += f"\n| Uploaded | {sanitize_table_value(upload_date)} |"
    if translated:
        extra_rows += f"\n| Translation | YouTube machine translation from {sanitize_table_value(source_language)}; {sanitize_table_value(caption_type)} source captions |"

    language_label = (
        f"{language} ({'machine-translated' if translated else caption_type})"
    )

    return f"""---
title: {safe_title}
channel: {safe_channel}
url: {yaml_quote(video_url)}
video_id: {yaml_quote(video_id)}
fetched: {yaml_quote(fetched_date)}
source_project: {safe_source}
language: {safe_language}
caption_type: {safe_caption_type}{extra_frontmatter}
tags:
  - yt-transcript
---

# {safe_heading}

## Video Details

| Field    | Value |
|----------|-------|
| URL      | {video_url} |
| Channel  | {sanitize_table_value(channel)} |{extra_rows}
| Fetched  | {fetched_date} |
| Source   | {sanitize_table_value(source_project)} |
| Language | {sanitize_table_value(language_label)} |
{description_section}

## Transcript

{transcript_text}
"""


# ── Main ───────────────────────────────────────────────────────────────────
def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number greater than zero"
        )
    return number


def run(args, parser) -> int:
    if args.check_deps:
        missing = check_dependencies()
        if missing:
            print_dependency_report(missing)
        else:
            print("All dependencies are installed.")
        return (
            EXIT_MISSING_DEPS
            if any(not d.get("optional") for d in missing)
            else EXIT_SUCCESS
        )

    if not args.video:
        parser.error("the following arguments are required: video")
    try:
        video_id = extract_video_id(args.video)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_ERROR

    # Known destinations are protected even offline or without dependencies.
    markdown = args.fmt in {"text", "markdown"}
    out_path = None
    output_dir = resolve_output_directory(args.output, args.output_dir)
    if not args.stdout and not args.list:
        if args.output:
            out_path = Path(args.output).expanduser()
        elif markdown:
            out_path = find_existing_transcript(video_id, output_dir)
        else:
            out_path = output_dir / f"{video_id}.{args.fmt}"
        if out_path is not None and out_path.is_dir():
            raise IsADirectoryError(f"Output must be a file: {out_path}")
        if (
            out_path is not None
            and (out_path.exists() or out_path.is_symlink())
            and not args.force
        ):
            print(
                f"Existing file preserved: {out_path.absolute()}. Use --force only to replace it.",
                file=sys.stderr,
            )
            return EXIT_DUPLICATE_SKIPPED

    missing = check_dependencies()
    required = [d for d in missing if not d.get("optional")]
    if required:
        print_dependency_report(required)
        return EXIT_MISSING_DEPS
    if markdown and not args.no_metadata and not args.list:
        optional = [d for d in missing if d.get("optional")]
        if optional:
            print_dependency_report(optional)

    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.formatters import (
        JSONFormatter,
        SRTFormatter,
        WebVTTFormatter,
    )

    try:
        with create_http_session(args.timeout) as session:
            transcript_list = YouTubeTranscriptApi(http_client=session).list(video_id)
            if args.list:
                for track in transcript_list:
                    kind = "auto-generated" if track.is_generated else "manual"
                    print(f"[{track.language_code}] {track.language} ({kind})")
                    targets = [
                        item["language_code"] for item in track.translation_languages
                    ]
                    if targets:
                        print("  Translation targets: " + ", ".join(targets))
                return EXIT_SUCCESS
            selected, source_language, caption_type = select_transcript(
                transcript_list, args.lang, args.strict_lang
            )
            actual_language = source_language
            translated = bool(
                args.translate and args.translate.lower() != source_language.lower()
            )
            if translated:
                selected = selected.translate(args.translate)
                actual_language = selected.language_code
                print(
                    f"YouTube machine translation: {source_language} → {actual_language} ({caption_type} source).",
                    file=sys.stderr,
                )
            transcript = selected.fetch()
            if not transcript or not any(
                snippet.text.strip() for snippet in transcript
            ):
                raise ValueError("YouTube returned empty captions. No file was saved.")
    except Exception as error:
        print(f"Error: {describe_fetch_error(error)}", file=sys.stderr)
        return EXIT_ERROR

    if args.fmt == "json":
        output = JSONFormatter().format_transcript(
            transcript, indent=2, ensure_ascii=False
        )
    elif args.fmt == "srt":
        output = SRTFormatter().format_transcript(transcript)
    elif args.fmt == "vtt":
        output = WebVTTFormatter().format_transcript(transcript)
    elif args.fmt == "txt":
        output = "\n".join(
            f"[{format_timestamp(s.start)}] {s.text}" if args.timestamps else s.text
            for s in transcript
        )
    else:
        transcript_text = "\n".join(
            f"{timestamp_link(s.start, video_id)} {s.text}"
            if args.timestamps
            else s.text
            for s in transcript
        )
        metadata = (
            empty_metadata()
            if args.no_metadata
            else fetch_video_metadata(video_id, args.timeout)
        )
        if args.no_metadata:
            metadata["metadata_source"] = "skipped"
        today = date.today().isoformat()
        description_section = (
            ""
            if args.no_description
            else build_description_section(
                metadata["description"], metadata["chapters"], video_id
            )
        )
        output = build_markdown(
            title=metadata["title"],
            channel=metadata["channel"],
            video_id=video_id,
            fetched_date=today,
            source_project=args.source or Path.cwd().name,
            language=actual_language,
            caption_type=caption_type,
            description_section=description_section,
            transcript_text=transcript_text,
            duration=metadata["duration"],
            upload_date=metadata["upload_date"],
            requested_language=args.lang,
            source_language=source_language,
            translated=translated,
            metadata_source=metadata["metadata_source"],
        )
        if out_path is None:
            out_path = (
                output_dir / f"{today}_{slugify(metadata['title'])}_[{video_id}].md"
            )

    if args.stdout:
        print(output)
        return EXIT_SUCCESS
    try:
        write_output(out_path, output, force=args.force)
    except ExistingOutputError:
        print(
            f"Existing file preserved: {out_path.absolute()}. Use --force only to replace it.",
            file=sys.stderr,
        )
        return EXIT_DUPLICATE_SKIPPED
    print(f"Saved to {out_path.absolute()}")
    return EXIT_SUCCESS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch YouTube captions as archival Markdown or subtitle exports",
        epilog="Exit codes: 0=success, 1=error, 2=usage/missing dependencies, 3=existing file preserved",
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="YouTube URL or 11-character ID (use -- before an ID starting with -)",
    )
    parser.add_argument("--output", "-o", help="Exact output file (highest precedence)")
    parser.add_argument(
        "--output-dir",
        help=f"Output directory (overrides ${OUTPUT_DIR_ENV}, ${LEGACY_OUTPUT_DIR_ENV} and ~/{DEFAULT_OUTPUT_DIRNAME}/)",
    )
    parser.add_argument(
        "--stdout", action="store_true", help="Print only the result; write no files"
    )
    parser.add_argument(
        "--format",
        "-f",
        dest="fmt",
        choices=["text", "markdown", "json", "srt", "vtt", "txt"],
        default="text",
        help="text/markdown=archival note (default); json/srt/vtt/txt=raw captions",
    )
    parser.add_argument(
        "--timestamps",
        "-t",
        action="store_true",
        help="Link Markdown timestamps to the video; add times in txt",
    )
    parser.add_argument(
        "--lang",
        "-l",
        default="en",
        help="Caption code, ordered comma-separated codes, or auto (default: en; fallback: en)",
    )
    parser.add_argument(
        "--strict-lang",
        action="store_true",
        help="Disable English fallback; regional variants still match",
    )
    parser.add_argument(
        "--translate",
        type=lambda value: value.strip().lower(),
        help="Explicit YouTube machine translation to this language code",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List caption tracks and translation targets without saving",
    )
    parser.add_argument(
        "--source",
        "-s",
        help="Capture-project name (defaults to current directory name)",
    )
    parser.add_argument(
        "--no-description",
        action="store_true",
        help="Omit the description and chapters from Markdown",
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip yt-dlp and oEmbed; capture captions and source URL only",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=DEFAULT_TIMEOUT,
        help="HTTP connect/read timeout in seconds (default: 15; metadata subprocess: twice this)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the chosen file; refresh an existing default note in place",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Report dependencies without fetching a video",
    )
    args = parser.parse_args(argv)
    if args.translate is not None and not args.translate:
        parser.error("--translate needs a language code")
    try:
        return run(args, parser)
    except OSError as error:
        print(
            f"Error: could not read or save output ({error}). Check the path and permissions.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    # Keep redirected exports portable, including Windows legacy console encodings.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
