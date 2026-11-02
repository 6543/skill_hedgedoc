// Browser runtime of a HedgeDoc offline package.
//
// The package HTML embeds the original HedgeDoc markdown and its media
// untouched (see offline.py). This script turns that source into
// something reveal.js renders like HedgeDoc's slide mode:
//   - strip the YAML front matter
//   - point media/<file> references at in-memory blob URLs
//   - turn HedgeDoc's image size syntax ![](src =WxH) into reveal.js element attributes
// The pure helpers are exported for node tests (offline_runtime.test.js).
"use strict";

(function (root) {
  const FRONT_MATTER = /^---[ \t]*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/;
  const FENCE = /^[ \t]{0,3}(`{3,}|~{3,})/;
  // ![alt](src "title" =WxH), title and either dimension optional (markdown-it-imsize)
  const SIZED_IMAGE = /!\[([^\]\n]*)\]\(\s*([^\s)]+)(?:\s+"([^"\n]*)")?\s+=(\d*)x(\d*)\s*\)/g;

  function stripFrontMatter(md) {
    return md.replace(FRONT_MATTER, "");
  }

  // Size as reveal.js element attributes: the image stays a markdown image inside its
  // paragraph, as in HedgeDoc. Width/height are digits only, so nothing needs escaping.
  function sizedImageToMarkdown(_match, alt, src, title, width, height) {
    const attrs = [width && `width="${width}"`, height && `height="${height}"`].filter(Boolean).join(" ");
    const titlePart = title === undefined ? "" : ` "${title}"`;
    return `![${alt}](${src}${titlePart})<!-- .element: ${attrs} -->`;
  }

  // Line based so fenced code blocks keep their literal content.
  function applyImageSize(md) {
    let fence = null;
    return md
      .split("\n")
      .map((line) => {
        const m = line.match(FENCE);
        if (m) {
          if (fence === null) fence = m[1];
          else if (m[1][0] === fence[0] && m[1].length >= fence.length) fence = null;
          return line;
        }
        return fence === null ? line.replace(SIZED_IMAGE, sizedImageToMarkdown) : line;
      })
      .join("\n");
  }

  function rewriteMedia(md, urlByPath) {
    let out = md;
    for (const [path, url] of Object.entries(urlByPath)) {
      out = out.split(path).join(url);
    }
    return out;
  }

  // reveal.js dedents slide markdown by the indentation of its first line,
  // so the text handed over must not start with whitespace.
  function prepareMarkdown(md, urlByPath) {
    const body = stripFrontMatter(md).replace(/^\s+/, "");
    return applyImageSize(rewriteMedia(body, urlByPath));
  }

  function base64ToBlobUrl(b64, type) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([bytes], { type }));
  }

  function boot() {
    const payload = JSON.parse(document.getElementById("hedgedoc-offline-source").textContent);
    const urlByPath = {};
    for (const item of payload.media) urlByPath[item.path] = base64ToBlobUrl(item.base64, item.type);

    const section = document.createElement("section");
    section.setAttribute("data-markdown", "");
    // HedgeDoc slide mode separators: blank line, then --- (horizontal) or ---- (vertical)
    section.setAttribute("data-separator", "^(\\r\\n?|\\n)---(\\r\\n?|\\n)$");
    section.setAttribute("data-separator-vertical", "^(\\r\\n?|\\n)----(\\r\\n?|\\n)$");
    section.setAttribute("data-separator-notes", "^\\s*notes?:");
    const template = document.createElement("textarea");
    template.setAttribute("data-template", "");
    template.textContent = prepareMarkdown(payload.markdown.text, urlByPath);
    section.appendChild(template);
    document.querySelector(".reveal .slides").appendChild(section);

    const render = payload.render || {};
    root.Reveal.initialize(
      Object.assign({}, render.reveal || {}, {
        markdown: { gfm: true, breaks: render.breaks !== false },
        plugins: [root.RevealMarkdown, root.RevealHighlight, root.RevealNotes],
      }),
    );
  }

  const api = { stripFrontMatter, applyImageSize, rewriteMedia, prepareMarkdown, boot };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HedgedocOffline = api;
})(typeof window !== "undefined" ? window : globalThis);
