"""Packaging checks for the distributed skill bundle."""

from __future__ import annotations

import os
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class SkillBundleTests(unittest.TestCase):
    def test_required_bundle_files_exist(self):
        required = [
            "SKILL.md",
            "README.md",
            "LICENSE",
            "requirements.txt",
            "scripts/fetch_transcript.py",
            "scripts/fetch_media.py",
            "scripts/browser_media_links.js",
            "assets/banner.png",
            "agents/openai.yaml",
            ".scripts/verify-isolated-install.sh",
        ]
        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((REPO_ROOT / relative_path).is_file())

    def test_skill_frontmatter_is_minimal_and_discoverable(self):
        text = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        keys = [
            line.split(":", 1)[0]
            for line in frontmatter.splitlines()
            if line and not line[0].isspace()
        ]
        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: youtube-fetcher", frontmatter)
        for trigger in ("Obsidian", "knowledge-base", "transcript", "Markdown"):
            self.assertIn(trigger, frontmatter)

    def test_openai_metadata_matches_skill(self):
        metadata = (REPO_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "YouTube Fetcher"', metadata)
        self.assertIn("$youtube-fetcher", metadata)

    def test_installer_verifier_is_executable(self):
        verifier = REPO_ROOT / ".scripts" / "verify-isolated-install.sh"
        self.assertTrue(os.access(verifier, os.X_OK))


if __name__ == "__main__":
    unittest.main()
