// Tests for offline_runtime.js (the in-browser part of the offline package).
// Run: node --test offline_runtime.test.js
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const rt = require("./offline_runtime.js");

test("stripFrontMatter removes leading YAML block only", () => {
  const md = "---\ntitle: X\ntype: slide\n---\n\n## Hi\n\n---\n\n## Next\n";
  assert.equal(rt.stripFrontMatter(md), "\n## Hi\n\n---\n\n## Next\n");
});

test("stripFrontMatter keeps markdown without front matter", () => {
  const md = "## Hi\n\n---\n\n## Next\n";
  assert.equal(rt.stripFrontMatter(md), md);
});

test("stripFrontMatter handles CRLF and '...' terminator", () => {
  assert.equal(rt.stripFrontMatter("---\r\na: 1\r\n...\r\nbody"), "body");
});

// Sizes become reveal.js element attributes, so the image stays a markdown image inside
// a paragraph like in HedgeDoc (a raw <img> starting a line would be an HTML block instead).
test("applyImageSize converts width and height", () => {
  assert.equal(
    rt.applyImageSize("![Logo](a.webp =63x130) <!-- x -->"),
    '![Logo](a.webp)<!-- .element: width="63" height="130" --> <!-- x -->',
  );
});

test("applyImageSize supports width only, height only and title", () => {
  assert.equal(rt.applyImageSize("![](a.png =150x)"), '![](a.png)<!-- .element: width="150" -->');
  assert.equal(rt.applyImageSize("![](a.png =x20)"), '![](a.png)<!-- .element: height="20" -->');
  assert.equal(
    rt.applyImageSize('![a "b"](a.png "T & t" =10x)'),
    '![a "b"](a.png "T & t")<!-- .element: width="10" -->',
  );
});

test("applyImageSize leaves plain images and code blocks alone", () => {
  const md = "![](a.svg)\n\n```md\n![](b.png =10x)\n```\n\n![](c.png =5x)";
  assert.equal(
    rt.applyImageSize(md),
    '![](a.svg)\n\n```md\n![](b.png =10x)\n```\n\n![](c.png)<!-- .element: width="5" -->',
  );
});

test("applyImageSize handles several images in one line (table row)", () => {
  assert.equal(
    rt.applyImageSize("| ![A](a.png =24x) | ![B](b.png =24x) |"),
    '| ![A](a.png)<!-- .element: width="24" --> | ![B](b.png)<!-- .element: width="24" --> |',
  );
});

test("rewriteMedia replaces every reference of known files", () => {
  const md = "![](media/a.svg) and ![x](media/a.svg =5x) ![](media/b.png)";
  const out = rt.rewriteMedia(md, { "media/a.svg": "blob:1" });
  assert.equal(out, "![](blob:1) and ![x](blob:1 =5x) ![](media/b.png)");
});

test("prepareMarkdown strips leading whitespace so reveal does not dedent", () => {
  // reveal's markdown plugin dedents by the first line's indentation;
  // leading blank lines after front matter would turn every space into a newline.
  const md = "---\ntitle: t\n---\n\n<style>a { b: c }</style>\n\n## A b\n";
  const out = rt.prepareMarkdown(md, {});
  assert.equal(out, "<style>a { b: c }</style>\n\n## A b\n");
});
