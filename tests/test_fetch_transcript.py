"""Deterministic contract tests for the Video Fetcher to Markdown runtime (YouTube captions script)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "fetch_transcript.py"
SPEC = importlib.util.spec_from_file_location("youtube_fetcher", SCRIPT_PATH)
youtube_fetcher = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(youtube_fetcher)

VIDEO_ID = "dQw4w9WgXcQ"


class StubTranscript:
    def __init__(self, language_code="en", is_generated=False):
        self.language_code = language_code
        self.is_generated = is_generated


class StubTranscriptList(list):
    def __init__(self, selected):
        super().__init__([selected])
        self.selected = selected


class ExtractVideoIdTests(unittest.TestCase):
    def test_accepts_supported_url_shapes_and_raw_id(self):
        cases = {
            VIDEO_ID: VIDEO_ID,
            f"https://www.youtube.com/watch?v={VIDEO_ID}": VIDEO_ID,
            f"https://youtube.com/watch?feature=share&v={VIDEO_ID}&t=42": VIDEO_ID,
            f"https://m.youtube.com/watch?list=abc&v={VIDEO_ID}": VIDEO_ID,
            f"https://music.youtube.com/watch?v={VIDEO_ID}": VIDEO_ID,
            f"https://youtu.be/{VIDEO_ID}?si=abc": VIDEO_ID,
            f"https://youtube.com/embed/{VIDEO_ID}": VIDEO_ID,
            f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}": VIDEO_ID,
            f"https://youtube.com/shorts/{VIDEO_ID}?feature=share": VIDEO_ID,
            f"https://youtube.com/live/{VIDEO_ID}": VIDEO_ID,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(youtube_fetcher.extract_video_id(value), expected)

    def test_rejects_lookalike_hosts_and_invalid_ids(self):
        cases = [
            f"https://youtube.com.example.org/watch?v={VIDEO_ID}",
            f"https://notyoutube.com/watch?v={VIDEO_ID}",
            "https://youtube.com/watch?feature=share",
            "https://youtu.be/too-short",
            "not a video",
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                youtube_fetcher.extract_video_id(value)


class OutputDirectoryTests(unittest.TestCase):
    def test_new_setting_wins_and_the_old_name_still_works(self):
        new, old = youtube_fetcher.OUTPUT_DIR_ENV, youtube_fetcher.LEGACY_OUTPUT_DIR_ENV
        self.assertEqual((new, old), ("VIDEO_FETCHER_DIR", "YOUTUBE_FETCHER_DIR"))
        self.assertEqual(youtube_fetcher.resolve_output_directory(None, None, {old: "/old"}), Path("/old"))
        self.assertEqual(youtube_fetcher.resolve_output_directory(None, None, {old: "/old", new: "/new"}), Path("/new"))
        self.assertEqual(youtube_fetcher.resolve_output_directory(None, None, {new: " "}), Path.home() / "yt_transcripts")

    def test_output_precedence(self):
        env = {youtube_fetcher.OUTPUT_DIR_ENV: "/env/notes"}
        self.assertEqual(
            youtube_fetcher.resolve_output_directory(
                "/file/explicit.md", "/cli/notes", env
            ),
            Path("/file"),
        )
        self.assertEqual(
            youtube_fetcher.resolve_output_directory(None, "/cli/notes", env),
            Path("/cli/notes"),
        )
        self.assertEqual(
            youtube_fetcher.resolve_output_directory(None, None, env),
            Path("/env/notes"),
        )
        self.assertEqual(
            youtube_fetcher.resolve_output_directory(None, None, {}),
            Path.home() / "yt_transcripts",
        )

    def test_duplicate_detection_uses_resolved_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            expected = output_dir / f"2026-08-03_example_[{VIDEO_ID}].md"
            expected.write_text("existing", encoding="utf-8")
            self.assertEqual(
                youtube_fetcher.find_existing_transcript(VIDEO_ID, output_dir),
                expected,
            )

    def test_duplicate_detection_supports_legacy_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            expected = output_dir / "legacy.md"
            expected.write_text(f'---\nvideo_id: "{VIDEO_ID}"\n---\n', encoding="utf-8")
            self.assertEqual(
                youtube_fetcher.find_existing_transcript(VIDEO_ID, output_dir),
                expected,
            )


class TranscriptSelectionTests(unittest.TestCase):
    def test_records_actual_fallback_language_and_caption_type(self):
        transcript_list = StubTranscriptList(
            StubTranscript(language_code="en", is_generated=True)
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            selected, language, caption_type = youtube_fetcher.select_transcript(
                transcript_list, "es"
            )

        self.assertIs(selected, transcript_list.selected)
        self.assertEqual(language, "en")
        self.assertEqual(caption_type, "auto-generated")
        self.assertIn("requested captions 'es' were unavailable", stderr.getvalue())

    def test_language_variant_does_not_emit_false_fallback_warning(self):
        transcript_list = StubTranscriptList(
            StubTranscript(language_code="es-MX", is_generated=False)
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _, language, caption_type = youtube_fetcher.select_transcript(
                transcript_list, "es"
            )

        self.assertEqual(language, "es-MX")
        self.assertEqual(caption_type, "manual")
        self.assertNotIn("Warning:", stderr.getvalue())
        self.assertIn("Selected captions: es-MX (manual)", stderr.getvalue())

    def test_specific_english_request_can_fall_back_to_generic_english(self):
        transcript_list = StubTranscriptList(
            StubTranscript(language_code="en", is_generated=False)
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            _, language, _ = youtube_fetcher.select_transcript(transcript_list, "en-US")

        self.assertEqual(language, "en")
        self.assertIn("using 'en' instead", stderr.getvalue())


class MarkdownSafetyTests(unittest.TestCase):
    def test_dynamic_frontmatter_is_safely_quoted(self):
        title = 'A "quoted" title\nwith a second line and \\ slash'
        markdown = youtube_fetcher.build_markdown(
            title=title,
            channel="Channel | Name",
            video_id=VIDEO_ID,
            fetched_date="2026-08-03",
            source_project="project\nname",
            language="en",
            caption_type="manual",
            description_section="",
            transcript_text="A real transcript line.",
        )

        frontmatter = markdown.split("---", 2)[1]
        fields = {}
        for line in frontmatter.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value

        self.assertEqual(json.loads(fields["title"]), title)
        self.assertEqual(json.loads(fields["source_project"]), "project\nname")
        self.assertIn('# A "quoted" title with a second line and \\ slash', markdown)
        self.assertIn("| Channel  | Channel \\| Name |", markdown)
        self.assertIn("| Source   | project name |", markdown)
        self.assertIn("## Transcript\n\nA real transcript line.", markdown)

    def test_chapter_titles_stay_on_one_list_line(self):
        section = youtube_fetcher.build_description_section(
            "Description\n---\nMore",
            [{"start_time": 65, "title": "Chapter\nInjected"}],
        )
        self.assertIn("\\---", section)
        self.assertIn("- `01:05` Chapter Injected", section)


if __name__ == "__main__":
    unittest.main()
