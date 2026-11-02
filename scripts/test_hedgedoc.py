"""Offline tests for hedgedoc.py pure logic. Run: python3 -m unittest -v test_hedgedoc"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hedgedoc as hd  # noqa: E402

SERVER = "https://pad.example.org"


class VendoredDeps(unittest.TestCase):
    """Offline fallback: bundled pure-Python wheels used only when imports fail."""

    def setUp(self):
        import hashlib
        import json
        import uuid
        import zipfile

        self.tmp = tempfile.TemporaryDirectory()
        self.vendor = os.path.join(self.tmp.name, "vendor")
        os.makedirs(self.vendor)
        self.mod = "hdfake_" + uuid.uuid4().hex[:8]
        self.whl = os.path.join(self.vendor, f"{self.mod}-1.0-py3-none-any.whl")
        with zipfile.ZipFile(self.whl, "w") as z:
            z.writestr(f"{self.mod}/__init__.py", "VALUE = 42\n")
        digest = hashlib.sha256(open(self.whl, "rb").read()).hexdigest()
        with open(os.path.join(self.vendor, "MANIFEST.json"), "w") as fh:
            json.dump({os.path.basename(self.whl): digest}, fh)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.saved_path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.saved_path
        for m in [m for m in sys.modules if m.startswith(self.mod)]:
            del sys.modules[m]
        self.tmp.cleanup()

    def test_installed_modules_win(self):
        self.assertEqual(hd.ensure_deps(("json",), self.vendor, self.cache), "installed")
        self.assertFalse(os.path.exists(self.cache))

    def test_missing_module_loads_from_vendor(self):
        self.assertEqual(hd.ensure_deps((self.mod,), self.vendor, self.cache), "vendored")
        self.assertEqual(__import__(self.mod).VALUE, 42)

    def test_second_call_reuses_extraction(self):
        hd.ensure_deps((self.mod,), self.vendor, self.cache)
        marker = [os.path.join(r, f) for r, _, fs in os.walk(self.cache) for f in fs if f == ".complete"]
        self.assertEqual(len(marker), 1)
        mtime = os.path.getmtime(marker[0])
        del sys.modules[self.mod]
        hd.ensure_deps((self.mod,), self.vendor, self.cache)
        self.assertEqual(os.path.getmtime(marker[0]), mtime)

    def test_tampered_wheel_is_refused(self):
        with open(self.whl, "ab") as fh:
            fh.write(b"x")
        with self.assertRaises(hd.HedgeDocError) as cm:
            hd.ensure_deps((self.mod,), self.vendor, self.cache)
        self.assertIn("sha256", str(cm.exception))

    def test_unwritable_cache_imports_from_wheels(self):
        blocker = os.path.join(self.tmp.name, "file-not-dir")
        open(blocker, "w").close()
        self.assertEqual(hd.ensure_deps((self.mod,), self.vendor, blocker), "vendored")
        self.assertEqual(__import__(self.mod).VALUE, 42)

    def test_missing_vendor_dir_explains_pip(self):
        with self.assertRaises(hd.HedgeDocError) as cm:
            hd.ensure_deps((self.mod,), os.path.join(self.tmp.name, "nope"), self.cache)
        self.assertIn("pip install", str(cm.exception))

    def test_bundled_manifest_matches_wheels(self):
        vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
        import json

        manifest = json.load(open(os.path.join(vendor, "MANIFEST.json")))
        wheels = sorted(f for f in os.listdir(vendor) if f.endswith(".whl"))
        self.assertEqual(sorted(manifest), wheels)
        self.assertTrue(all(w.endswith("-none-any.whl") for w in wheels), "only pure-Python wheels")


class Utf16(unittest.TestCase):
    def test_bmp_counts_like_python(self):
        self.assertEqual(hd.utf16_len("Ünïcödé"), 7)

    def test_astral_counts_two_units(self):
        # regression: server said 139, python len() said 138 for a note with one emoji
        self.assertEqual(hd.utf16_len("a😀b"), 4)


class TextOperation(unittest.TestCase):
    def roundtrip(self, old, new):
        ops = hd.make_text_operation(old, new)
        self.assertEqual(hd.apply_text_operation(old, ops), new)
        return ops

    def test_identical_is_empty(self):
        self.assertEqual(hd.make_text_operation("same", "same"), [])

    def test_append_has_retain_then_insert(self):
        self.assertEqual(self.roundtrip("abc", "abcdef"), [3, "def"])

    def test_prepend(self):
        self.assertEqual(self.roundtrip("abc", "xabc"), ["x", 3])

    def test_middle_replace_keeps_prefix_and_suffix(self):
        self.assertEqual(self.roundtrip("hello world", "hello there world"), [6, "there ", 5])

    def test_delete_is_negative(self):
        self.assertEqual(self.roundtrip("abcdef", "abef"), [2, -2, 2])

    def test_replace_everything(self):
        self.assertEqual(self.roundtrip("old", "new!"), ["new!", -3])

    def test_empty_to_text_and_back(self):
        self.assertEqual(self.roundtrip("", "x"), ["x"])
        self.assertEqual(self.roundtrip("x", ""), [-1])

    def test_lengths_are_utf16_units(self):
        # base length must equal server document length (JS string length)
        ops = self.roundtrip("😀 a", "😀 ab")
        self.assertEqual(ops, [4, "b"])

    def test_never_splits_surrogate_pair(self):
        # 😀 (U+1F600) and 😁 (U+1F601) share the high surrogate; diff must not cut between
        ops = self.roundtrip("x😀y", "x😁y")
        for op in ops:
            if isinstance(op, str):
                op.encode("utf-8")  # raises on lone surrogate
        self.assertEqual(ops, [1, "😁", -2, 1])

    def test_base_length_matches(self):
        old, new = "a😀\nline2\n", "a😀\nline 2 changed\n"
        ops = hd.make_text_operation(old, new)
        base = sum(o if isinstance(o, int) and o > 0 else (-o if isinstance(o, int) else 0) for o in ops)
        self.assertEqual(base, hd.utf16_len(old))

    def test_apply_rejects_wrong_base_length(self):
        with self.assertRaises(ValueError):
            hd.apply_text_operation("abc", [2, "x"])


class NoteRef(unittest.TestCase):
    def test_full_url(self):
        self.assertEqual(
            hd.parse_note_ref("https://pad.example.org/yEHR8mQLREONMOf4gAWUxw"),
            ("https://pad.example.org", "yEHR8mQLREONMOf4gAWUxw"),
        )

    def test_url_with_mode_query_and_anchor(self):
        self.assertEqual(
            hd.parse_note_ref("https://pad.example.org/abc?both#Heading"), ("https://pad.example.org", "abc")
        )

    def test_publish_and_slide_urls(self):
        self.assertEqual(hd.parse_note_ref("https://pad.example.org/s/LC8JdSF43z")[1], "LC8JdSF43z")
        self.assertEqual(hd.parse_note_ref("https://pad.example.org/p/LC8JdSF43z")[1], "LC8JdSF43z")

    def test_action_suffix_stripped(self):
        self.assertEqual(hd.parse_note_ref("https://pad.example.org/abc/download")[1], "abc")
        self.assertEqual(hd.parse_note_ref("https://pad.example.org/s/xyz/download")[1], "xyz")

    def test_bare_id_needs_server(self):
        self.assertEqual(hd.parse_note_ref("abc", SERVER), (SERVER, "abc"))
        with self.assertRaises(ValueError):
            hd.parse_note_ref("abc")

    def test_subpath_server(self):
        self.assertEqual(
            hd.parse_note_ref("https://ex.org/pad/abc?view", "https://ex.org/pad/"), ("https://ex.org/pad", "abc")
        )

    def test_rejects_non_note_paths(self):
        for p in ("https://pad.example.org/", "https://pad.example.org/uploads/x.png"):
            with self.assertRaises(ValueError):
                hd.parse_note_ref(p)


class CreateRedirect(unittest.TestCase):
    def test_forbidden_redirect_goes_to_root(self):
        # 1.x answers anon-forbidden with 302 -> serverURL + '/'
        self.assertTrue(hd.is_forbidden_redirect(SERVER + "/", SERVER))
        self.assertTrue(hd.is_forbidden_redirect("/", SERVER))

    def test_note_redirect_is_success(self):
        self.assertFalse(hd.is_forbidden_redirect(SERVER + "/abc", SERVER))


class MediaLinks(unittest.TestCase):
    MD = (
        "![a](https://pad.example.org/uploads/1.png)\n"
        "![b](https://pad.example.org/uploads/2.jpg =200x200)\n"
        '<img src="https://pad.example.org/uploads/3.gif" width="50">\n'
        "[file](https://pad.example.org/uploads/4.svg)\n"
        "![ext](https://i.imgur.com/x.png)\n"
        "![dup](https://pad.example.org/uploads/1.png)\n"
    )

    def test_finds_server_uploads_once_in_order(self):
        self.assertEqual(
            hd.find_server_media(self.MD, SERVER),
            [
                SERVER + "/uploads/1.png",
                SERVER + "/uploads/2.jpg",
                SERVER + "/uploads/3.gif",
                SERVER + "/uploads/4.svg",
            ],
        )

    def test_all_remote_includes_external(self):
        self.assertIn("https://i.imgur.com/x.png", hd.find_server_media(self.MD, SERVER, include_external=True))


class LocalImages(unittest.TestCase):
    def test_finds_only_local_existing_files(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "img"))
            open(os.path.join(d, "img", "a b.png"), "wb").close()
            md = (
                "![x](img/a%20b.png)\n![y](<img/a b.png>)\n![r](https://e.org/z.png)\n"
                "![d](data:image/png;base64,AA)\n![m](img/missing.png)\n"
            )
            found = hd.find_local_media(md, d)
            self.assertEqual(
                found,
                {"img/a%20b.png": os.path.join(d, "img", "a b.png"), "img/a b.png": os.path.join(d, "img", "a b.png")},
            )

    def test_rewrite_replaces_targets_only(self):
        md = "![x](img/a.png =100x) and ![y](img/a.png)\n`img/a.png` stays"
        out = hd.rewrite_links(md, {"img/a.png": "https://pad/uploads/u.png"})
        self.assertEqual(
            out, "![x](https://pad/uploads/u.png =100x) and ![y](https://pad/uploads/u.png)\n`img/a.png` stays"
        )


class UploadType(unittest.TestCase):
    ALLOWED = ["image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp"]

    def test_allowed_by_extension(self):
        self.assertIsNone(hd.upload_problem("x.PNG", self.ALLOWED))
        self.assertIsNone(hd.upload_problem("x.jpeg", self.ALLOWED))

    def test_rejected_types_explain(self):
        self.assertIn("text/plain", hd.upload_problem("notes.txt", self.ALLOWED))
        self.assertIn("pdf", hd.upload_problem("doc.pdf", self.ALLOWED))

    def test_unknown_extension(self):
        self.assertIsNotNone(hd.upload_problem("noext", self.ALLOWED))


class Frontmatter(unittest.TestCase):
    def test_split(self):
        meta, body = hd.split_frontmatter("---\ntype: slide\n---\n# T\n")
        self.assertEqual(meta, "type: slide")
        self.assertEqual(body, "# T\n")

    def test_no_frontmatter(self):
        self.assertEqual(hd.split_frontmatter("# T\n---\nx"), (None, "# T\n---\nx"))


class Lint(unittest.TestCase):
    def codes(self, md):
        return [w.split(":")[0] for w in hd.lint_markdown(md)]

    def test_clean_slide_deck(self):
        md = "---\ntype: slide\nslideOptions:\n  theme: white\n---\n\n# A\n\n---\n\n## B\n\n----\n\n### B.1\n"
        self.assertEqual(hd.lint_markdown(md), [])

    def test_separator_without_blank_lines(self):
        md = "---\ntype: slide\n---\n\n# A\ntext\n---\n## B\n"
        self.assertIn("slide-separator", self.codes(md))

    def test_deck_without_type_slide(self):
        md = "# A\n\n---\n\n## B\n\n---\n\n## C\n"
        self.assertIn("missing-type-slide", self.codes(md))

    def test_tab_indent_in_yaml(self):
        md = "---\nslideOptions:\n\ttheme: white\n---\n# A\n"
        self.assertIn("yaml-indent", self.codes(md))

    def test_frontmatter_not_at_start(self):
        md = "\n---\ntitle: x\n---\n# A\n"
        self.assertIn("frontmatter-position", self.codes(md))

    def test_too_long(self):
        self.assertIn("too-long", self.codes("a" * 100001))

    def test_plain_doc_is_clean(self):
        self.assertEqual(hd.lint_markdown("---\ntitle: t\ntags: a, b\n---\n# Doc\n\n[TOC]\n"), [])


if __name__ == "__main__":
    unittest.main()
