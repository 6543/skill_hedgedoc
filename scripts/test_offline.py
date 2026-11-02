"""Tests for the offline export (offline.py, extract_offline.py, hedgedoc.py export-offline).

Run: python3 -m unittest test_offline   (inside scripts/)
JS side: node --test offline_runtime.test.js
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import lzma
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import extract_offline
import hedgedoc
import offline

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAAABJRU5ErkJggg=="
)
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'

DECK = (
    "---\r\n"
    "title: Tést & <deck>\r\n"
    "type: slide\r\n"
    "slideOptions:\r\n"
    "  theme: black\r\n"
    "  transition: slide\r\n"
    "  slideNumber: true\r\n"
    "---\r\n"
    "\r\n"
    "## Hello ✓\r\n"
    "\r\n"
    "![](media/a.png =10x) ![](media/b.svg)\r\n"
    "\r\n"
    "```html\r\n"
    "<script>alert(1)</script> <!-- x -->\r\n"
    "```\r\n"
)

FAKE_REVEAL = {
    "reveal.js": "/*REVEAL*/ var s='</script>';",
    "reveal.css": "/*REVEALCSS*/",
    "plugin/markdown.js": "/*MARKDOWN*/",
    "plugin/highlight.js": "/*HIGHLIGHT*/",
    "plugin/notes.js": "/*NOTES*/",
    "plugin/highlight/zenburn.css": "/*ZENBURN*/",
    "theme/black.css": "/*THEME-black*/",
    "theme/white.css": "/*THEME-white*/",
}


def write_fake_reveal_dir(root: Path) -> Path:
    for rel, text in FAKE_REVEAL.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def write_fake_reveal_archive(path: Path) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, text in FAKE_REVEAL.items():
            data = text.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(lzma.compress(buf.getvalue()))
    return path


class OfflineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.reveal = offline.RevealSource(write_fake_reveal_dir(self.tmp / "reveal"))
        self.src = self.tmp / "src"
        (self.src / "media").mkdir(parents=True)
        (self.src / "media" / "a.png").write_bytes(PNG)
        (self.src / "media" / "b.svg").write_bytes(SVG)
        (self.src / "media" / "unused.png").write_bytes(PNG)
        self.md = self.src / "note.md"
        self.md.write_bytes(DECK.encode())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, **kwargs) -> str:
        return offline.build_html(self.md, self.reveal, **kwargs)

    def payload(self, **kwargs) -> dict:
        return extract_offline.read_payload(self.build(**kwargs))


class RoundTrip(OfflineTestCase):
    def test_extract_restores_markdown_and_media_byte_identical(self):
        html = self.tmp / "deck.html"
        html.write_text(self.build(source_url="https://pad.example.org/p/x"), encoding="utf-8")
        out = self.tmp / "out"
        extract_offline.extract(html, out)
        self.assertEqual((out / "note.md").read_bytes(), DECK.encode())
        self.assertEqual((out / "media" / "a.png").read_bytes(), PNG)
        self.assertEqual((out / "media" / "b.svg").read_bytes(), SVG)

    def test_only_referenced_media_is_packaged(self):
        media = self.payload()["media"]
        self.assertEqual([m["path"] for m in media], ["media/a.png", "media/b.svg"])
        self.assertEqual([m["type"] for m in media], ["image/png", "image/svg+xml"])

    def test_markup_in_source_cannot_break_out_of_the_payload(self):
        html = self.build()
        tag = re.search(r'<script type="application/json" id="hedgedoc-offline-source">(.*?)</script>', html, re.S)
        self.assertNotIn("<", tag.group(1))
        self.assertIn("</script>", extract_offline.read_payload(html)["markdown"]["text"])

    def test_inlined_assets_cannot_close_their_script_tag(self):
        self.assertIn("var s='<\\/script>';", self.build())

    def test_images_outside_media_are_an_error_because_they_would_break_offline(self):
        self.md.write_text(
            "## x\n\n![](https://pad.example.org/uploads/x.png)\n\n<img src='pics/y.png'>\n\n"
            "```md\n![](https://in.code/block.png)\n```\n"
        )
        with self.assertRaises(offline.OfflineError) as ctx:
            self.build()
        self.assertIn("https://pad.example.org/uploads/x.png", str(ctx.exception))
        self.assertIn("pics/y.png", str(ctx.exception))
        self.assertNotIn("in.code", str(ctx.exception))

    def test_missing_referenced_media_is_an_error(self):
        (self.src / "media" / "b.svg").unlink()
        with self.assertRaisesRegex(offline.OfflineError, "media/b.svg"):
            self.build()


class RenderSettings(OfflineTestCase):
    def test_slide_options_and_title_come_from_front_matter(self):
        html = self.build()
        render = extract_offline.read_payload(html)["render"]
        # HedgeDoc 1.x defaults, overridden by allowed slideOptions keys
        self.assertEqual(
            render["reveal"],
            {
                "controls": True,
                "progress": True,
                "slideNumber": True,
                "history": True,
                "center": True,
                "transition": "slide",
                "rtl": False,
            },
        )
        self.assertTrue(render["breaks"])
        self.assertIn("/*THEME-black*/", html)
        self.assertNotIn("/*THEME-white*/", html)
        self.assertIn("<title>Tést &amp; &lt;deck&gt;</title>", html)

    def test_hedgedoc_look_overrides_the_theme(self):
        html = self.build()
        theme = html.index("/*THEME-black*/")
        self.assertLess(theme, html.index("/*ZENBURN*/"))
        self.assertLess(theme, html.index("text-transform: none"))

    def test_breaks_false_is_respected(self):
        self.md.write_text("---\nbreaks: false\n---\n\n## x\n")
        self.assertFalse(self.payload()["render"]["breaks"])

    def test_default_theme_without_front_matter_is_black(self):
        self.md.write_text("## x\n")
        self.assertIn("/*THEME-black*/", self.build())

    def test_slide_options_outside_hedgedoc_allow_list_are_dropped(self):
        self.md.write_text("---\nslideOptions:\n  dependencies: [x]\n  loop: true\n---\n\n## x\n")
        reveal = self.payload()["render"]["reveal"]
        self.assertNotIn("dependencies", reveal)
        self.assertTrue(reveal["loop"])

    def test_lang_and_rtl_from_front_matter(self):
        self.md.write_text("---\nlang: de\ndir: rtl\n---\n\n## x\n")
        html = self.build()
        self.assertIn('<div class="reveal" lang="de">', html)
        self.assertTrue(extract_offline.read_payload(html)["render"]["reveal"]["rtl"])

    def test_unknown_theme_is_an_error(self):
        self.md.write_text("---\nslideOptions:\n  theme: nope\n---\n\n## x\n")
        with self.assertRaisesRegex(offline.OfflineError, "theme 'nope'"):
            self.build()

    def test_invalid_front_matter_is_an_error(self):
        self.md.write_text("---\ntitle: [unclosed\n---\n\n## x\n")
        with self.assertRaisesRegex(offline.OfflineError, "front matter"):
            self.build()


class RevealArchive(OfflineTestCase):
    def test_reads_assets_from_xz_archive_with_matching_checksum(self):
        archive = write_fake_reveal_archive(self.tmp / "reveal.tar.xz")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        html = offline.build_html(self.md, offline.RevealSource(archive, sha256=digest))
        self.assertIn("/*THEME-black*/", html)

    def test_rejects_archive_with_wrong_checksum(self):
        archive = write_fake_reveal_archive(self.tmp / "reveal.tar.xz")
        with self.assertRaisesRegex(offline.OfflineError, "sha256"):
            offline.RevealSource(archive, sha256="0" * 64).read("reveal.js")

    def test_bundled_reveal_matches_its_manifest(self):
        source = offline.bundled_reveal()
        self.assertIn("reveal.js", source.read("reveal.js"))
        self.assertTrue(source.read("theme/black.css"))


class Bundles(OfflineTestCase):
    def test_zip_output_bundles_html_extractor_and_readme(self):
        out = self.tmp / "deck.zip"
        offline.export(self.md, out, self.reveal, source_url="https://pad.example.org/p/x")
        with zipfile.ZipFile(out) as z:
            names = sorted(z.namelist())
            readme = z.read("deck/README.md").decode()
            z.extractall(self.tmp / "unzipped")
        self.assertEqual(names, ["deck/README.md", "deck/deck.html", "deck/extract_offline.py"])
        self.assertIn("https://pad.example.org/p/x", readme)
        # the shipped extractor works on its own, without the skill next to it
        restored = self.tmp / "restored"
        subprocess.run(
            [sys.executable, "-I", "extract_offline.py", "deck.html", "-o", str(restored)],
            cwd=self.tmp / "unzipped" / "deck",
            check=True,
            capture_output=True,
        )
        self.assertEqual((restored / "note.md").read_bytes(), DECK.encode())

    def test_html_output_is_a_single_file(self):
        out = self.tmp / "deck.html"
        offline.export(self.md, out, self.reveal)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.is_file()), ["deck.html"])

    def test_other_output_suffix_is_an_error(self):
        with self.assertRaisesRegex(offline.OfflineError, r"\.html or \.zip"):
            offline.export(self.md, self.tmp / "deck.pdf", self.reveal)


class Cli(OfflineTestCase):
    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hedgedoc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_export_local_markdown_then_extract(self):
        html = self.tmp / "deck.html"
        code, out, err = self.run_cli(
            "export-offline", str(self.md), "-o", str(html), "--reveal", str(self.reveal.path)
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["written"], str(html))
        code, out, err = self.run_cli("extract-offline", str(html), "-o", str(self.tmp / "x"))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.tmp / "x" / "note.md").read_bytes(), DECK.encode())

    def export_and_extract(self, *extra) -> tuple[int, str, Path]:
        html, out = self.tmp / "deck.html", self.tmp / "x"
        code, _, err = self.run_cli(
            "export-offline", str(self.md), "-o", str(html), "--reveal", str(self.reveal.path), *extra
        )
        if code == 0:
            self.run_cli("extract-offline", str(html), "-o", str(out))
        return code, err, out

    def test_local_images_outside_media_are_embedded_without_touching_the_deck(self):
        (self.src / "pics").mkdir()
        (self.src / "pics" / "y.png").write_bytes(PNG)
        deck = "## x\n\n![](media/a.png) ![y](pics/y.png =20x)\n"
        self.md.write_text(deck)
        code, err, out = self.export_and_extract()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.md.read_text(), deck)
        self.assertEqual((out / "note.md").read_text(), "## x\n\n![](media/a.png) ![y](media/y.png =20x)\n")
        self.assertEqual((out / "media" / "y.png").read_bytes(), PNG)

    def test_local_deck_web_images_need_external_flag(self):
        self.md.write_text("## x\n\n![](https://img.example.org/a/logo.png)\n")
        code, err, _ = self.export_and_extract()
        self.assertEqual(code, 2)
        self.assertIn("https://img.example.org/a/logo.png", err)
        self.assertIn("--external", err)

    def test_local_deck_web_images_are_downloaded_into_the_package_with_external(self):
        deck = '## x\n\n![](https://img.example.org/a/logo.png) <img src="https://cdn.example.net/b/logo.png">\n'
        self.md.write_text(deck)

        def fake_download(url, dest_dir, args):
            path = Path(dest_dir) / "logo.png"
            path.write_bytes(PNG if "img.example" in url else SVG)
            return str(path)

        with mock.patch.object(hedgedoc, "_download_image", side_effect=fake_download) as dl:
            code, err, out = self.export_and_extract("--external")
        self.assertEqual(code, 0, err)
        self.assertEqual(dl.call_count, 2)
        self.assertEqual(self.md.read_text(), deck)
        restored = (out / "note.md").read_text()
        self.assertIn("![](media/logo.png)", restored)
        self.assertIn('<img src="media/logo-2.png">', restored)
        self.assertEqual((out / "media" / "logo-2.png").read_bytes(), SVG)

    def test_extract_refuses_to_overwrite_without_force(self):
        html = self.tmp / "deck.html"
        self.run_cli("export-offline", str(self.md), "-o", str(html), "--reveal", str(self.reveal.path))
        self.run_cli("extract-offline", str(html), "-o", str(self.tmp / "x"))
        code, _, err = self.run_cli("extract-offline", str(html), "-o", str(self.tmp / "x"))
        self.assertEqual(code, 2)
        self.assertIn("--force", err)


class ExtractSafety(OfflineTestCase):
    def tampered(self, mutate) -> Path:
        payload = self.payload()
        mutate(payload)
        path = self.tmp / "tampered.html"
        path.write_text(offline.render_html(payload, "", "", "t"), encoding="utf-8")
        return path

    def test_rejects_paths_outside_media(self):
        path = self.tampered(lambda p: p["media"][0].update(path="media/../../evil.png"))
        with self.assertRaisesRegex(extract_offline.ExtractError, "unsafe path"):
            extract_offline.extract(path, self.tmp / "out")
        self.assertFalse((self.tmp / "evil.png").exists())

    def test_rejects_checksum_mismatch(self):
        path = self.tampered(lambda p: p["media"][0].update(base64=base64.b64encode(b"other").decode()))
        with self.assertRaisesRegex(extract_offline.ExtractError, "checksum"):
            extract_offline.extract(path, self.tmp / "out")

    def test_rejects_unknown_format_version(self):
        path = self.tampered(lambda p: p.update(version=99))
        with self.assertRaisesRegex(extract_offline.ExtractError, "version"):
            extract_offline.read_payload(path.read_text(encoding="utf-8"))

    def test_rejects_html_without_payload(self):
        with self.assertRaisesRegex(extract_offline.ExtractError, "no HedgeDoc offline payload"):
            extract_offline.read_payload("<html></html>")

    def test_does_not_overwrite_without_force(self):
        html = self.tmp / "deck.html"
        html.write_text(self.build(), encoding="utf-8")
        out = self.tmp / "out"
        out.mkdir()
        (out / "note.md").write_text("mine")
        with self.assertRaisesRegex(extract_offline.ExtractError, "exists"):
            extract_offline.extract(html, out)
        self.assertEqual((out / "note.md").read_text(), "mine")
        extract_offline.extract(html, out, force=True)
        self.assertEqual((out / "note.md").read_bytes(), DECK.encode())


class PayloadEncoding(unittest.TestCase):
    def test_script_safe_json_round_trips(self):
        data = {"t": "</script><!-- &     >"}
        encoded = offline.script_safe_json(data)
        self.assertNotRegex(encoded, "[<>&  ]")
        self.assertEqual(json.loads(encoded), data)


if __name__ == "__main__":
    unittest.main()
