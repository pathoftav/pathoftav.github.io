"""Build a small plaintext blog.

Usage:
    python build.py

Layout:
    posts/    Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
    static/   files copied verbatim into the build
    site/     generated output — deploy this directory anywhere static

A subdirectory of posts/ is a group: the posts inside it are collected
onto a page of their own instead of appearing individually in the index,
and the index carries one row for the group. The directory is named like
a post (YYYY-MM-DD-slug), and an optional index.md inside it supplies the
group's title, body and OPTIONS line — so a group can be pinned, sealed
or locked exactly as a post can. Sealing a group says nothing about its
members: they are sealed only if they say so themselves.

A post's own assets live under static/components/<slug>/ and
static/media/<slug>/, where a bare filename in an <include> or an image
finds them without a path. An old version out of posts/old/ shares the
live slug, so it first looks in a <slug>/<date>/ subdirectory and falls
back to the shared one — keep a snapshot there only for the versions whose
assets have since been rewritten. Old versions of a grouped post live in
posts/old/<group-slug>/, so slugs only have to be unique within a group.

Post format: if the first line is an H1 ("# Title") it becomes the
post title; otherwise the first line is taken as the title verbatim.
The rest is standard Markdown (with extensions enabled).
If the LAST line consists only of hashtags ("#magic #geomancy"), they
become the post's tags: rendered as links on the post page, indexed
under site/tags/<tag>.html, with an overview at site/tags/index.html.

Every build writes every page. Under --serve the work that does NOT change
between rebuilds is skipped instead: only the static files the watcher saw
move are copied, unchanged posts come out of a parse cache, and the phrase
KDF runs at a local round count. Every page is still rendered from a post
parsed or re-parsed this build, so nothing can be written from a stale
cache and there is no dependency graph to get wrong.
"""

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback

from datetime import date, datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from posixpath import join as urljoin, normpath as urlnorm
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv
import markdown
import rcssmin
import rjsmin

from media_ext import MediaExtension


load_dotenv()
IS_LOCAL = os.getenv("ENVIRONMENT") == "LOCAL"

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
OLD = POSTS / "old"
STATIC = ROOT / "static"
SITE = ROOT / "site"
INDEX = "index.html" if IS_LOCAL else ""    # set to "" in serve() if --serve
EXT = ".html" if IS_LOCAL else ""           # set to "" in serve() if --serve

GROUP_META = "index.md"     # optional title/body/OPTIONS for a group directory

SITE_TITLE = "Sublunary Musings"
SITE_SUBTITLE = "philosophy, magic, and other errata"
DATE_FMT = "%B %-d, %Y"

SITE_TZ = ZoneInfo("America/New_York")
UNLOCK_FMT = "%B %-d, %Y at %-I:%M %p %Z"
BUILD_TIME = datetime.now(timezone.utc)

SEAL_PASSWORD = os.getenv("SEAL_PASSWORD") if not IS_LOCAL else os.getenv("SEAL_PASSWORD", "secret")
SEAL_ROUNDS = 1_000 if IS_LOCAL else 310_000
HANDLE_ROUNDS = 1_000 if IS_LOCAL else 310_000
HANDLE_SALT = ""            # derived per build
SEALED_HANDLE = None        # derived per build
SITE_HAS_SEALED = False     # whether this build sealed anything at all

# Decoys. With this on, every listing carries a payload whether or not it is
# withholding anything, and every payload is padded to a multiple of the block
# below. A page that hides rows is then indistinguishable from one that does
# not: same script, same order of magnitude of base64. Costs about one block
# per listing page for every visitor. Set the block to 0 to seal unpadded.
SEAL_DECOY_LISTINGS = True
SEAL_PAD_BLOCK = 4096


# The glyph pool the rune field draws from, shipped to sealed.js in a data
# attribute so the two never drift apart. Rows are ordered by how reliably
# fonts carry them: futhark and the planets are near-universal, the alchemical
# block wants Noto Sans Symbols 2 or Segoe UI Symbol.
SEAL_RUNES = (
    "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚻᚾᛁᛃᛇᛈᛉᛊᛋᛏᛒᛖᛗᛚᛜᛝᛞᛟ"
    "☉☽☿♀♁♂♃♄♅♆♇☊☋"
    "♈♉♊♋♌♍♎♏♐♑♒♓"
    "☤☥☧☩☬☸⚕⚖⚗⚘⚚⚛⚜"
    "🜁🜂🜃🜄🜍🜔🜚🜛🜞🜠🜫🝆🝊🝳"
)
SEAL_RUNE_ROWS = 4
SEAL_RUNE_COLS = 22

SERVE = False                       # True when running under --serve
BUILD_ID = "0"                      # bumped each rebuild; open reload streams watch this
RELOAD_HINT = "{}"                  # what the last rebuild touched; read by the client
BUILD_LOCK = threading.Lock()       # held while site/ is mid-rebuild and briefly inconsistent
BUILD_DONE = threading.Condition()  # notified once site/ is whole again

# Server-sent events rather than a poll: one connection per tab for as long as
# the tab is open, instead of a request every fraction of a second cluttering
# the network tab. EventSource reconnects on its own if the stream drops, so
# there is no retry logic to write here.
#
# The payload names what the rebuild touched. A build that changed no markup
# and only restyled swaps the <link> hrefs in place — a full reload would lose
# scroll position and every open <details> to repaint the same document.
LIVE_RELOAD = """<script>
(function () {
  var events = new EventSource("/__reload");
  events.onmessage = function (e) {
    var hint = null;
    try { hint = JSON.parse(e.data); } catch (err) {}
    if (hint && hint.css && hint.css.length) {
      hint.css.forEach(function (href) {
        document.querySelectorAll('link[rel="stylesheet"]').forEach(function (link) {
          var u = new URL(link.href, location.href);
          if (u.pathname === href) {
            u.searchParams.set("v", hint.build);
            link.href = u.href;
          }
        });
      });
      return;
    }
    location.reload();
  };
  // let the server drop the connection now rather than discover it on the
  // next write, and keep a bfcached page from holding a second stream open
  window.addEventListener("pagehide", function () { events.close(); });
})();
</script>"""

# Media belonging to a shut-in post. Encrypted per (slug, kind) under a key
# that ships inside the payload; see seal_assets().
#
# ASSET_KEYS and ASSET_NAMES persist across rebuilds under --serve, where site/
# is not emptied: rotating a key every rebuild would re-encrypt every video on
# every keystroke. The two sets below are a single build's verdict and are
# cleared each time.
ASSET_KEYS: dict[tuple[str, str], bytes] = {}                # (slug, kind) -> content key
ASSET_NAMES: dict[tuple[bytes, Path], tuple[str, int]] = {}  # (key, source) -> (.enc name, mtime)
SEALED_ASSETS: set[Path] = set()
PUBLIC_ASSETS: set[Path] = set()
LIVE_ENC: set[str] = set()          # .enc filenames this build still points at
WITHDRAWN: set[Path] = set()        # sources whose plaintext is currently pulled
WRITTEN_PAGES: set[Path] = set()    # every page this build wrote
PREV_PAGES: set[Path] = set()       # every page the previous build wrote

SEALED_DIR = "static/sealed"
ASSET_ROOTS = ("static/media/",)    # trees a post's assets may be sealed out of

ASSET_TAG_RE = re.compile(r"<(?:img|video|source)\b[^>]*>", re.IGNORECASE)
ASSET_SRC_RE = re.compile(r'\bsrc="([^"]+)"')
ASSET_DARK_RE = re.compile(r"--dark:url\(([^)]+)\);?")
ASSET_PLACEHOLDER = (      # 1x1 transparent GIF, so nothing flashes a broken icon
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

EXTRA_DECRYPT = '<script defer src="{site_root}/static/scripts/decrypt-media.js"></script>'

EXTRA_NOINDEX = '<meta name="robots" content="noindex">'
EXTRA_LOCK = (
    '<link rel="stylesheet" href="{site_root}/static/styles/locked.css">\n'
    '<style>html.lock-pending [data-unlock] {{ visibility: hidden; }}</style>\n'
    '<script>document.documentElement.classList.add("lock-pending");</script>\n'
    + EXTRA_DECRYPT + '\n'
    '<script defer src="{site_root}/static/scripts/lock.js"></script>'
)
EXTRA_SEAL = (
    '<link rel="stylesheet" href="{site_root}/static/styles/sealed.css">\n'
    '<style>html.seal-pending [data-seal] {{ visibility: hidden; }}</style>\n'
    '<script>try {{ if (sessionStorage.getItem("seal-handle")) '
    'document.documentElement.classList.add("seal-pending"); }} catch (e) {{}}</script>\n'
    + EXTRA_DECRYPT + '\n'
    '<script defer src="{site_root}/static/scripts/sealed.js"></script>'
)
def EXTRA_SEAL_BADGE() -> str:
    return (
        '<link rel="stylesheet" href="{site_root}/static/styles/sealed.css">\n'
        '<script>try {{ if (sessionStorage.getItem("seal-salt") === "'
        + HANDLE_SALT
        + '") document.documentElement.classList.add("seal-open"); }} catch (e) {{}}</script>'
    )
# A listing with a fuller form behind the phrase. The style hides the public
# list only for a reader whose session already holds a handle for this build;
# everyone else paints once, normally, and never sees a flicker.
EXTRA_SEAL_LISTING = (
    '<style>html.seal-open [data-seal-listing] {{ visibility: hidden; }}</style>\n'
    '<script defer src="{site_root}/static/scripts/seal-listing.js"></script>'
)
EXTRA_MATH = (
    '<link rel="stylesheet" href="{site_root}/static/vendor/katex/katex.min.css">\n'
    '<script defer src="{site_root}/static/vendor/katex/katex.min.js"></script>\n'
    '<script defer src="{site_root}/static/vendor/katex/contrib/auto-render.min.js"></script>\n'
    '<script defer src="{site_root}/static/scripts/math.js"></script>'
)

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f1ecdf">
<meta name="theme-color" media="(prefers-color-scheme: dark)"  content="#17141f">
<title>{post_title}</title>
<style>
/* OS Preference */
:root {{ color-scheme: light dark; background: #f1ecdf; }}
@media (prefers-color-scheme: dark) {{ :root {{ background: #17141f; }} }}
/* User Preference */
:root[data-theme="light"] {{ color-scheme: light; background: #f1ecdf; }}
:root[data-theme="dark"]  {{ color-scheme: dark;  background: #17141f; }}
</style>
<script>
/* Apply theme before first paint to prevent flashes.
   Syncs with OS preference or localStorage override. */
try {{
  var t = localStorage.getItem("theme");
  var os = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  var activeTheme = t || os;

  // Sync browser UI with active theme
  var metaTheme = document.createElement("meta");
  metaTheme.name = "theme-color";
  metaTheme.content = activeTheme === "dark" ? "#17141f" : "#f1ecdf";
  document.head.appendChild(metaTheme);

  // Apply data-theme if user override exists; clean up redundant storage
  if (t === os) {{
    localStorage.removeItem("theme");
  }} else if (t) {{
    document.documentElement.dataset.theme = t;
  }}
}} catch (e) {{}}
</script>
<link rel="preload" href="{site_root}/static/fonts/EBGaramond.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{site_root}/static/styles/theme.css">
<link rel="stylesheet" href="{site_root}/static/styles/base.css">
<link rel="stylesheet" href="{site_root}/static/styles/ui.css">
<link rel="stylesheet" href="{site_root}/static/styles/article.css">
<link rel="stylesheet" href="{site_root}/static/styles/media.css">
<link rel="apple-touch-icon" sizes="180x180" href="{site_root}/static/favicon/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="{site_root}/static/favicon/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="{site_root}/static/favicon/favicon-16x16.png">
<link rel="icon" href="{site_root}/favicon.ico">
<link rel="manifest" href="{site_root}/static/favicon/site.webmanifest">
{head_extras}
</head>
<body>
<header class="site">
  <button class="theme" aria-label="Toggle light/dark mode" title="Toggle light/dark mode"></button>
  <h1><a href="{site_root}/{index_file}">{site_title}</a></h1>
  <p>{site_subtitle}</p>
</header>
{post_body}
<script src="{site_root}/static/scripts/theme.js"></script>
<script src="{site_root}/static/scripts/media.js"></script>
</body>
</html>
"""

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
TAG_LINE_RE = re.compile(r"^\s*#[\w-]+(?:\s+#[\w-]+)*\s*$")     # a line consisting only of hashtags: "#placebo #tree-of-life #philosophy"

INCLUDE_RE = re.compile(r'^[ \t]*<include\s+(.+?)\s*/?>[ \t]*$', re.MULTILINE | re.DOTALL)
ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
SLOT_RE = re.compile(r'\{\{\s*(\w+)\s*\}\}')


# --------------------------------------------------------------------------
# parse cache
# --------------------------------------------------------------------------
# Only markdown rendering is cached, and only under --serve; everything
# downstream still runs every build. That is the whole point of stopping here:
# every page is written from a post parsed or re-parsed this build, so no page
# can go stale, and there is no graph of "which pages does this post appear on"
# to maintain.
#
# The key is the .md's mtime AND the mtimes of every file it reached outside
# itself. A bare <include source="fig.html"> resolves through the slug and,
# for an old version, through a snapshot directory; media resolves inside
# MediaExtension. The post's own mtime is silent about both.

PARSE_CACHE: dict[tuple, tuple[int, dict[str, int | None], dict]] = {}
_HANDLE_CACHE: dict[tuple[str, str], str] = {}


def stamp_of(path: Path) -> int | None:
    return path.stat().st_mtime_ns if path.exists() else None


def dep_stamps(deps: set[Path]) -> dict[str, int | None]:
    return {str(d): stamp_of(d) for d in deps}


def deps_unchanged(stamps: dict[str, int | None]) -> bool:
    return all(stamp_of(Path(d)) == s for d, s in stamps.items())


def clone_post(post: dict) -> dict:
    """A copy safe for the caller to annotate. Only options is mutated
    downstream (history, the "old" flag, the lock refresh, the group a post
    belongs to), so only options needs its own dict; html and tags are
    read-only after parsing."""
    clone = dict(post)
    clone["options"] = dict(post["options"])
    return clone


def refresh_lock(post: dict) -> None:
    """Re-answer "is this still locked?" against the current BUILD_TIME.

    A cached parse froze that verdict at the moment it ran. Nothing here makes
    a deadline trigger a rebuild on its own — lock.js clears the badge in the
    browser, as it always did — but a post whose moment passed while the server
    was up must not be written from a stale one."""
    unlock = post["options"].get("unlock_time")
    if unlock is not None:
        post["options"]["is_locked"] = unlock > BUILD_TIME


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_post(path: Path, old: bool = False,
               ident: tuple[date, str] | None = None) -> dict:
    """Read one .md file into a post dict: title, date, slug, tags, html,
    options, and the set of files outside the .md that the render depended on.

    `ident` overrides the (date, slug) the filename would give. A group's
    index.md needs it: the file is called index.md, but the group is named by
    its directory, and the slug decides where <include> and media resolve."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path}: post is empty")
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body_lines = lines[1:]

    deps: set[Path] = set()

    options = extract_options(body_lines)                   # NOTE: mutates body_lines: pops the OPTIONS line
    tags = extract_tags(body_lines)                         # NOTE: mutates body_lines: pops the tag line
    body = "\n".join(body_lines).strip()
    d, slug = ident if ident is not None else date_and_slug(path)
    version = d.isoformat() if old else ""                  # NOTE: names the snapshot dir an old version prefers
    body = expand_includes(body, path.name, slug, version, deps)  # NOTE: splices in <include> tags
    body = body.replace("{slug}", slug)
    parse_sealed_option(options, path.name)                 # NOTE: mutates options: replaces "sealed" with "is_sealed", overrides locked_options and removes them if present
    parse_locked_option(options, path.name)                 # NOTE: mutates options: replaces "locked" with "is_locked" and "unlock_time"
    rendered = render_markdown(body, slug, version, options)

    # media resolves inside MediaExtension, which has no way to report back;
    # the rendered HTML is the record of what it chose, and asset_deps() reads
    # it with the same resolver the sealing pass uses.
    deps |= asset_deps(rendered)

    return {
        "title": title,
        "date": d,
        "slug": slug,
        "tags": tags,
        "html": rendered,
        "options": options,
        "deps": deps,
    }


def parse_post_cached(path: Path, old: bool = False,
                      ident: tuple[date, str] | None = None) -> dict:
    """parse_post() memoised on the .md's mtime and its dependencies'.

    Bypassed entirely off --serve: a one-shot build has nothing to reuse."""
    if not SERVE:
        return parse_post(path, old, ident)

    key = (str(path), old, ident)
    stamp = path.stat().st_mtime_ns
    hit = PARSE_CACHE.get(key)
    if hit is not None and hit[0] == stamp and deps_unchanged(hit[1]):
        post = clone_post(hit[2])
    else:
        post = parse_post(path, old, ident)
        PARSE_CACHE[key] = (stamp, dep_stamps(post["deps"]), post)
        post = clone_post(post)
    refresh_lock(post)
    return post


def extract_options(body_lines: list[str]) -> dict:
    """Peel a trailing OPTIONS line off body_lines (in place) and return the
    list of options as a dictionary. Done BEFORE tags or markdown.

    The options line must be in a specific format at the very bottom of the file:
    <!-- [OPTIONS]: { "draft": true, "math": true } -->

    A group's index.md takes the same line, and every option below means for
    the group page what it means for a post page.

    List of options
        "pin":      int    pin post to top of listings; larger numbers rank higher
        "draft":    bool   mark post as a draft; not rendered in production
        "unlisted": bool   omit from index and tags; page still reachable by link
        "noindex":  bool   ask search engines not to index the post
        "dropcap":  bool   set false to suppress the drop cap (default true)
        "toc":      bool   enable table of contents
        "math":     bool   enable LaTeX rendering
        "locked":   str    ISO date (optionally with time) before which the
                           body is sealed; listed but unreadable until then
        "sealed":   bool   encrypt the body against SEAL_PASSWORD; listed,
                           but readable only once a reader types the phrase.
                           sealed implicitly adds "unlisted": true and also
                           hides the unlisted badge, unless "unlisted": false is
                           specified — in which case the sealed post will show
                           in the index.
    """
    options = {}
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if body_lines:
        last_line = body_lines[-1].removeprefix("<!--").removesuffix("-->").strip()
        if last_line.startswith("[OPTIONS]"):
            json_string = last_line.split("[OPTIONS]:", 1)[1]
            options = json.loads(json_string)
            body_lines.pop(-1)
    return options


def extract_tags(body_lines: list[str]) -> list[str]:
    """Peel a trailing hashtag line off body_lines (in place) and return its
    tags. Done BEFORE markdown sees the text — python-markdown would read
    "#magic" as an <h1>."""
    tags = []
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if body_lines and TAG_LINE_RE.match(body_lines[-1]):
        tag_line = body_lines.pop()
        tags = [t.lstrip("#").lower() for t in tag_line.split()]
    return tags


def date_and_slug(path: Path) -> tuple[date, str]:
    """Derive (date, slug) from the filename: a YYYY-MM-DD- prefix wins,
    otherwise fall back to the file's mtime.

    Works on a group directory too — a directory has no suffix, so its stem
    is its whole name."""
    m = DATE_RE.match(path.stem)
    if m:
        return date.fromisoformat(m.group(1)), path.stem[len(m.group(0)):]
    return datetime.fromtimestamp(path.stat().st_mtime).date(), path.stem


def expand_includes(text: str, source: str, slug: str, version: str,
                    deps: set[Path] | None = None) -> str:
    """Splice component fragments into a post body, in place of <include> tags.

        <include source="vecfig.html" prompt="what is an LLM?" reply="...">

    The tag stands alone on its own line, though it may wrap across several.
    A bare `source` resolves under static/components/<slug>/; anything with a
    slash resolves against ROOT. {site_root}/ and {slug} are substituted first.

    An old version passes its date as `version` and prefers a snapshot in
    static/components/<slug>/<version>/, falling back to the shared directory
    when there isn't one — so a figure can be frozen for the versions that
    need it without copying the ones that didn't change.

    Other attributes fill matching {{slot}}s in the fragment — double braces so
    single-brace {site_root} survives for render(). Values are escaped for
    attribute position, so a fragment wanting one in JS should read it back
    with getAttribute rather than interpolate it into a script.

    Every fragment actually opened is recorded in `deps` when one is passed:
    the resolution rules above mean the post's own mtime says nothing about
    which file a bare filename found, so the parse cache has to be told.

    Runs after options and tags are peeled, so a fragment cannot reconfigure
    its post, and before markdown, so it parses as if pasted in. Includes do
    not nest. A missing file or unfilled slot is fatal."""
    def swap(m: re.Match) -> str:
        attrs = dict(ATTR_RE.findall(m.group(1)))
        src = attrs.pop("source", "")
        rel = src.replace("{site_root}/", "").replace("{slug}", slug).lstrip("/")
        if not rel:
            raise ValueError(f"{source}: <include> with no source")

        # a bare filename belongs to this post; anything with a slash is a path
        if "/" in rel:
            path = ROOT / rel
        else:
            base = STATIC / "components" / slug
            snapshot = base / version / rel if version else None
            path = snapshot if snapshot and snapshot.is_file() else base / rel
        if not path.is_file():
            raise FileNotFoundError(f"{source}: no such include: {src!r} -> {path}")
        if deps is not None:
            deps.add(path)

        frag = path.read_text(encoding="utf-8").strip("\n")

        def fill(s: re.Match) -> str:
            key = s.group(1)
            if key not in attrs:
                raise KeyError(f"{source}: {rel} wants {key!r}; got {sorted(attrs)}")
            return html.escape(attrs[key], quote=True)

        return SLOT_RE.sub(fill, frag)

    return INCLUDE_RE.sub(swap, text)


def render_markdown(body: str, slug: str, version: str, options: dict) -> str:
    """Convert post body to HTML, prepending a Contents panel when the post
    has at least three top-level (##) sections.

    slug and version reach MediaExtension, which resolves bare media
    filenames under static/media/<slug>/ and lets an old version prefer a
    snapshot in <slug>/<version>/."""
    md = markdown.Markdown(
        extensions=[
            # Structural Block Parsers
            "tables",
            "fenced_code",
            "md_in_html",
            # Document-Level Navigation & Linking
            "toc",
            "footnotes",
            # Inline Text Formatting
            "nl2br",
            # Third-Party
            "pymdownx.arithmatex",
            # Custom
            MediaExtension(slug=slug, version=version),
        ],
        extension_configs={"pymdownx.arithmatex": {"generic": True}},
    )
    rendered = md.convert(body)

    if options.get("toc", False):
        toc_tokens = getattr(md, "toc_tokens", [])
        sections = [t for t in toc_tokens if t["level"] == 2]
        if len(sections) >= 3:
            items = "\n".join(
                f'<li><a href="#{t["id"]}">{t["name"]}</a></li>' for t in sections
            )
            guide = f'<nav class="guide"><h3>Contents</h3><ul>\n{items}\n</ul></nav>\n'
            rendered = guide + rendered

    # If footnotes exist, modify any links so they open in a new tab
    footnote_split = rendered.split('<div class="footnote">')
    if len(footnote_split) > 1:
        main_content = footnote_split[0]
        footnote_content = '<div class="footnote">' + footnote_split[1]
        # Regex to find hrefs in the footnote block, ignoring the backref arrows
        # It adds target="_blank" and rel="noopener noreferrer" to external links
        footnote_content = re.sub(
            r'(<a href="[^"]+")(?! class="footnote-backref")',
            r'\1 target="_blank" rel="noopener noreferrer"',
            footnote_content
        )
        rendered = main_content + footnote_content

    return rendered


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------

def parse_locked_option(options: dict, source: str) -> None:
    """Trade the authored "locked" option (in place) for the two derived
    keys the rest of the build reads:

        "unlock_time": datetime   the sealing deadline, as a UTC instant
        "is_locked":   bool       whether that deadline is still ahead
    """
    locked_timestamp = options.pop("locked", False)
    if not locked_timestamp:
        return
    try:
        dt = datetime.fromisoformat(str(locked_timestamp).strip())
    except ValueError:
        print(f'warning: {source}: unusable "locked" value {locked_timestamp!r}; publishing unlocked')
        return
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SITE_TZ)
    unlock_time = dt.astimezone(timezone.utc)

    options["unlock_time"] = unlock_time
    options["is_locked"]   = unlock_time > BUILD_TIME


def iso_utc(dt: datetime) -> str:
    """A UTC instant in the form JavaScript's Date.parse reads unambiguously."""
    return dt.isoformat().replace("+00:00", "Z")


def seal(body: str, slug: str, unlock: datetime) -> str:
    """AES-GCM the post body so the text is absent from the served HTML.

    Deterrence, not access control: every input to the key derivation ships
    with the page, so a determined reader can decrypt early. What it does
    buy is that the text is not in View Source, not findable with Ctrl+F,
    not indexed by crawlers and not captured by web archives.

    Plain SHA-256 rather than PBKDF2 — key stretching defends a secret the
    attacker does not have, and here they have all three inputs."""
    material = f"{slug}|{iso_utc(unlock)}".encode()
    key = hashlib.sha256(material).digest()
    iv = os.urandom(12)
    blob = iv + AESGCM(key).encrypt(iv, body.encode(), None)
    return base64.b64encode(blob).decode()


def lock_notice(unlock: datetime) -> str:
    nice = unlock.astimezone(SITE_TZ).strftime(UNLOCK_FMT)
    return (
        f'<p class="lock-notice">This writing is sealed until '
        f'<time datetime="{iso_utc(unlock)}">{nice}</time>.</p>\n'
    )


# --------------------------------------------------------------------------
# sealing by phrase
# --------------------------------------------------------------------------

def parse_sealed_option(options: dict, source: str) -> None:
    """Trade the authored "sealed" option (in place) for the one derived key
    the rest of the build reads:

        "is_sealed": bool   body encrypted against SEAL_PASSWORD

    Unlike "locked", a missing secret is fatal. Warning and publishing anyway
    would put the plaintext of a post the author meant to hide on the open
    web, and no build is worth that."""
    if not options.pop("sealed", False):
        return
    if not SEAL_PASSWORD:
        raise RuntimeError(
            f'{source}: post is "sealed" but SEAL_PASSWORD is unset. '
            f"Refusing to publish the body in the clear — set it in .env "
            f"(and in the deploy environment) and build again."
        )

    if options.pop("locked", False):
        # both would mean two keys for one body; the phrase is the stronger
        # claim, so it wins and the clock is dropped.
        print(f'warning: {source}: both "locked" and "sealed" set; sealed wins')

    # unlisted by default, unless explicitly set to false
    if "unlisted" not in options:
        options["unlisted"] = True

    options["is_sealed"] = True


def derive_handle(phrase: str, salt_hex: str) -> str:
    """The value a reader's session is given to hold, in place of the phrase.

    Sealed posts are keyed to this rather than to the phrase itself, so
    nothing capable of opening a post ever has to keep the phrase around: the
    handle is one-way, and a copy of it says nothing about what was typed.
    Stretched rather than plain-hashed for exactly that reason — a memorable
    phrase behind a bare SHA-256 falls to a dictionary in seconds, and the
    protection being bought here is precisely that a leaked handle does not
    hand over the phrase.

    Salted per build, so the handle rotates with the site. A handle lifted out
    of a reader's session opens that build's posts and no others; ciphertext
    already captured stays readable, but nothing published afterwards does.

    Memoised on (phrase, salt), which pays only under --serve: the local salt
    is a constant, so every rebuild would otherwise re-derive the same value."""
    hit = _HANDLE_CACHE.get((phrase, salt_hex))
    if hit is None:
        hit = _HANDLE_CACHE[(phrase, salt_hex)] = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=bytes.fromhex(salt_hex),
            iterations=HANDLE_ROUNDS,
        ).derive(phrase.encode()).hex()
    return hit


def seal_by_phrase(body: str, handle: str) -> str:
    """AES-GCM the post body under a key stretched from the build's handle.

    Real access control, unlike seal() above: the only input a reader is not
    handed is the phrase itself, so the ciphertext is worth no more than a
    guess at it. Hence PBKDF2 rather than a bare hash — here the stretching
    defends a secret the attacker genuinely lacks, and each round multiplies
    the cost of grinding through a dictionary. Note that the grind runs the
    full chain, phrase to handle to key, so both round counts are charged
    against every candidate.

    The salt is fresh per post per build, so two sealed posts never share a
    derived key and a rebuild never re-uses one. Layout of the blob:

        [0:16]  salt      [16:28]  iv      [28:]  ciphertext || GCM tag

    There is no separate "is this the right phrase?" check anywhere on the
    page, by design: the GCM tag is the check. A wrong phrase yields a key
    that fails authentication, which is indistinguishable from noise — so a
    reader learns nothing from a failed attempt except that it failed."""
    salt = os.urandom(16)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=SEAL_ROUNDS,
    ).derive(handle.encode())
    iv = os.urandom(12)
    blob = salt + iv + AESGCM(key).encrypt(iv, body.encode(), None)
    return base64.b64encode(blob).decode()


def seal_notice() -> str:
    """The rune field that stands in for a sealed body.

    Deliberately says nothing. There is no prompt, no field, no hint that
    typing would do anything at all — a reader who does not already know the
    phrase should see only that something has been closed. The glyphs are
    drawn fresh each build and carry no information about the post; sealed.js
    keeps transmuting them once it loads, and they are aria-hidden because
    read aloud they are gibberish."""
    rows = "\n".join(
        "<div class=\"seal-rune-row\">"
        + "".join(
            f'<span class="seal-rune">{r}</span>'
            for r in random.choices(SEAL_RUNES, k=SEAL_RUNE_COLS)
        )
        + "</div>"
        for _ in range(SEAL_RUNE_ROWS)
    )
    return (
        '<div class="seal-notice" role="note" aria-label="This writing is sealed.">\n'
        f'<div class="seal-runes" aria-hidden="true" data-runes="{html.escape(SEAL_RUNES, quote=True)}">\n'
        f'{rows}\n'
        '</div>\n'
        '</div>\n'
    )


def pad_fragment(fragment: str, block: int = 0) -> str:
    """Round a fragment up to a multiple of `block` bytes with a comment.

    AES-GCM is a stream mode: the ciphertext is exactly as long as the
    plaintext, so the only place a listing's true size can be concealed is
    here, before it is sealed. An HTML comment is the filler because it
    survives innerHTML as a comment node and renders as nothing.

    The dots are ASCII, so the byte count and the character count agree."""
    if block <= 0:
        return fragment
    room = -len(fragment.encode()) % block
    while room < 7:            # "<!---->" is the shortest comment there is
        room += block
    return fragment + "<!--" + "." * (room - 7) + "-->"


def carries_payload(public: list, full: list) -> bool:
    """Whether this listing ships a sealed second form.

    With decoys on, every listing on a site that sealed anything ships one,
    including the listings with nothing to hide — a page that withholds rows
    has to look like a page that does not, and the only way to arrange that
    is for both to carry a payload. With decoys off, only the listings that
    actually withhold something do, and their presence says so."""
    if not SITE_HAS_SEALED:
        return False
    return SEAL_DECOY_LISTINGS or has_sealed_rows(public, full)


def seal_listing(fragment: str, root: str) -> str:
    """The fuller form of a listing, encrypted under the build handle.

    Every listing is built twice: the public page carries the rows anyone may
    see, and this carries the rows a reader holding the phrase may see. Same
    handle a sealed post is keyed to, so opening one post opens all of them —
    the reader never types anything a second time. Where the two forms are
    identical this is a decoy, and opening it swaps the list for itself.

    Nothing about the withheld rows survives out here. Not a tag name, not a
    count, not which of the visible rows grew, and — once padded — not how
    much is behind it.

    {site_root} is resolved here for the reason render_article resolves it:
    render() substitutes on the body string, by which point this is base64
    and every link inside it would survive unreplaced."""
    if SEALED_HANDLE is None:
        raise RuntimeError(
            "sealed rows reached a listing with no handle. "
            "SEAL_PASSWORD must be set before main() derives one."
        )
    fragment = fragment.replace("{site_root}", root.rstrip("/"))
    blob = seal_by_phrase(pad_fragment(fragment, SEAL_PAD_BLOCK), SEALED_HANDLE)
    return (
        '<script type="application/octet-stream" class="seal-listing"'
        f' data-rounds="{SEAL_ROUNDS}"'
        f' data-handle-rounds="{HANDLE_ROUNDS}"'
        f' data-handle-salt="{HANDLE_SALT}">{blob}</script>'
    )


# --------------------------------------------------------------------------
# sealing media
# --------------------------------------------------------------------------

def resolve_asset(url: str) -> Path | None:
    """Map a URL in rendered HTML back to a file on disk, or None if it isn't
    a sealable asset.

    Media carries the {site_root} token; a ?dark= variant instead carries a
    path relative to static/styles, where the CSS that reads it lives. Both
    normalise to a repo-relative path, and anything outside ASSET_ROOTS is
    left alone."""
    u = url.strip().strip("\"'")
    if u.startswith("{site_root}/"):
        rel = u[len("{site_root}/"):]
    elif u.startswith(("http:", "https:", "data:", "//")):
        return None
    else:
        rel = urlnorm(urljoin("static/styles", u)).lstrip("/")
    rel = urlnorm(rel)
    if not rel.startswith(ASSET_ROOTS):
        return None
    path = ROOT / rel
    return path if path.is_file() else None


def asset_deps(content: str) -> set[Path]:
    """Every sealable file a rendered body points at.

    Serves two callers with one pass: the parse cache treats these as
    dependencies (media changing must re-render the post that shows it), and
    note_public_assets() treats them as files an open page serves in the
    clear."""
    found: set[Path] = set()
    for m in ASSET_TAG_RE.finditer(content):
        for pattern in (ASSET_SRC_RE, ASSET_DARK_RE):
            for hit in pattern.finditer(m.group(0)):
                source = resolve_asset(hit.group(1))
                if source is not None:
                    found.add(source)
    return found


def asset_key(slug: str, kind: str) -> bytes:
    """One content key per post per kind, minted on first use.

    Keyed by kind as well as slug because a locked post's body key derives
    from values that ship with the page: sharing a content key would let
    anyone who can open a locked post reach a sealed one's media. Old
    versions share the live slug, so they reuse the key and the files."""
    return ASSET_KEYS.setdefault((slug, kind), os.urandom(32))


def publish_asset(source: Path, key: bytes) -> str:
    """Encrypt one file into site/static/sealed/ and return its URL.

    Named from a hash of the key and the path, so the name gives up neither
    the post nor the original filename and rotates whenever the key does.
    Blob layout matches seal(): iv || ciphertext || GCM tag.

    The cache is keyed on the source's mtime as well: the name is a function
    of the key and the path, not the bytes, so under --serve — where keys
    outlive a build — an edited image would otherwise keep its old ciphertext
    under a name that looks up to date."""
    mtime = source.stat().st_mtime_ns
    cached = ASSET_NAMES.get((key, source))
    if cached is not None and cached[1] == mtime:
        LIVE_ENC.add(cached[0])
        return f"{{site_root}}/{SEALED_DIR}/{cached[0]}"

    rel = source.relative_to(ROOT).as_posix()
    name = hashlib.sha256(key + rel.encode()).hexdigest()[:32] + ".enc"
    iv = os.urandom(12)

    out = SITE / SEALED_DIR / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(iv + AESGCM(key).encrypt(iv, source.read_bytes(), None))

    ASSET_NAMES[(key, source)] = (name, mtime)
    LIVE_ENC.add(name)
    SEALED_ASSETS.add(source)
    return f"{{site_root}}/{SEALED_DIR}/{name}"


def seal_assets(content: str, slug: str, kind: str) -> tuple[str, bytes | None]:
    """Swap media URLs in a body for encrypted stand-ins, returning the
    content key so the caller can bury it in the payload.

    Runs before {site_root} is resolved, so the token is the anchor and the
    URLs emitted here are resolved by the same later pass.

    Rewritten a tag at a time rather than a URL at a time: an <img> can carry
    both a src and a ?dark= variant, and each needs its own attribute on that
    one element. src is replaced rather than dropped so a <video> does not
    refetch the page as its source."""
    used: list[bytes] = []

    def stash(url: str) -> tuple[str, str] | None:
        source = resolve_asset(url)
        if source is None:
            return None
        key = asset_key(slug, kind)
        used.append(key)
        SEALED_ASSETS.add(source)
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return publish_asset(source, key), mime

    def rewrite(m: re.Match) -> str:
        tag = m.group(0)
        extra = ""

        def swap_src(sm: re.Match) -> str:
            hit = stash(sm.group(1))
            if hit is None:
                return sm.group(0)
            return (f'src="{ASSET_PLACEHOLDER}"'
                    f' data-enc="{hit[0]}" data-enc-type="{hit[1]}"')

        def swap_dark(dm: re.Match) -> str:
            nonlocal extra
            hit = stash(dm.group(1))
            if hit is None:
                return dm.group(0)
            extra += f' data-enc-dark="{hit[0]}" data-enc-dark-type="{hit[1]}"'
            return ""      # a --dark left pointing at a deleted file breaks the toggle

        tag = ASSET_SRC_RE.sub(swap_src, tag)
        tag = ASSET_DARK_RE.sub(swap_dark, tag)
        if extra:
            tag = re.sub(r"^<\w+", lambda t: t.group(0) + extra, tag, count=1)
        return tag

    content = ASSET_TAG_RE.sub(rewrite, content)
    return content, used[0] if used else None


def note_public_assets(content: str) -> None:
    """Record media an open page serves in the clear, so a file it shares
    with a shut-in post survives that post's withdrawal."""
    PUBLIC_ASSETS.update(asset_deps(content))


def withdraw_sealed_assets() -> None:
    """Delete the plaintext of anything encrypted, unless an open page still
    points at it — and put back anything that has stopped being encrypted.

    Without the first half the encryption is theatre: the original would sit
    at a guessable URL beside the sealed page, crawlable precisely because
    nothing links to it. The .enc copies outlive it, so a locked post still
    opens on schedule with no rebuild.

    The second half exists because site/ survives between rebuilds and only
    the files the watcher saw move are re-copied. Unsealing a post changes
    the .md, not the image, so nothing would otherwise restore it.

    Every page is written each build, so both sets are the whole site's
    verdict and the unlink is a straight sweep rather than a diff."""
    global WITHDRAWN
    gone = SEALED_ASSETS - PUBLIC_ASSETS
    for source in sorted(gone):
        (SITE / source.relative_to(ROOT)).unlink(missing_ok=True)
    for source in sorted(WITHDRAWN - gone):
        dest = SITE / source.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    WITHDRAWN = gone

    for tree in ASSET_ROOTS:
        base = SITE / tree.rstrip("/")
        if not base.is_dir():
            continue
        for d in sorted(base.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

    # .enc files nothing points at any more. Only reachable under --serve,
    # where site/ survives between builds.
    sealed_dir = SITE / SEALED_DIR
    if sealed_dir.is_dir():
        for enc in sealed_dir.glob("*.enc"):
            if enc.name not in LIVE_ENC:
                enc.unlink()


# --------------------------------------------------------------------------
# html fragments
# --------------------------------------------------------------------------

def render(title: str, root: str, body: str, extras: list[str] | None = None) -> str:
    """Wrap a body fragment in the full page shell."""
    root = root.rstrip("/")
    ext = "\n".join(h.format(site_root=root) for h in (extras or ["<!-- no extras -->"]))
    body = body.replace("{site_root}", root)
    page = PAGE.format(
        index_file=INDEX,
        site_root=root,
        site_title=SITE_TITLE,
        site_subtitle=SITE_SUBTITLE,
        post_title=title,
        post_body=body,
        head_extras=ext,
    )
    if SERVE:
        page = page.replace("</body>", LIVE_RELOAD + "\n</body>")
    return page


def badge(kind: str, label: str | None = None) -> str:
    """One badge span; the label defaults to the kind, uppercased."""
    return f'<span class="badge badge-{kind}">{label or kind.upper()}</span>'


def entry_url(entry: dict) -> str:
    """Where an index row points, as a {site_root}-anchored path.

    A post is a file and takes EXT; a group is a directory and takes INDEX.
    Anchored rather than relative because the same row is emitted from the
    front page, from a tag page and from inside a group, and those sit at
    three different depths — {site_root} is resolved per page by render(),
    or by seal_listing() before the row is encrypted."""
    tail = f"/{INDEX}" if entry.get("is_group", False) else EXT
    return f'{{site_root}}/{entry["url"]}{tail}'


def history_url(group: str | None, slug: str) -> str:
    """Where a post's old versions are published.

    Kept beside the post rather than in one global pile, so that "../../<slug>"
    reaches the current version from either depth and so that a grouped post's
    slug only has to be unique within its group."""
    return f"posts/{group}/old/{slug}" if group else f"posts/old/{slug}"


def post_list_items(posts) -> str:
    """The dotted-leader <li> rows used by the index, by tag pages and by
    group pages.

    A sealed row carries data-unlock so lock.js can drop the badge without
    waiting for the next build."""
    def item(p: dict) -> str:
        locked = p["options"].get("is_locked", False)
        unlock = f' data-unlock="{iso_utc(p["options"]["unlock_time"])}"' if locked else ""
        badges = ""
        if p["options"].get("pin"):
            badges += badge("pin", "PINNED")
        if locked:
            badges += badge("locked")
        if p["options"].get("is_sealed", False):
            badges += badge("sealed")
        return (
            f'<li{unlock}><a href="{entry_url(p)}">{html.escape(p["title"])}</a>'
            '<span class="leader"></span>'
            f'{badges}'
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime(DATE_FMT)}</time></li>'
        )

    return "\n".join(item(p) for p in posts)


def tag_footer(tags: list[str]) -> str:
    if not tags:
        return ""
    links = " ".join(
        f'<a href="{{site_root}}/tags/{t}{EXT}" rel="tag">#{html.escape(t)}</a>'
        for t in tags
    )
    return f'<footer class="tags">{links}</footer>\n'


def group_by_tag(posts) -> dict[str, list]:
    """Map each tag to the (date-sorted) posts carrying it."""
    by_tag: dict[str, list] = {}
    for p in posts:
        for t in p["tags"]:
            by_tag.setdefault(t, []).append(p)   # posts already date-sorted
    return by_tag


def sort_posts(posts: list[dict]) -> list[dict]:
    """Pinned first, then newest first. The one ordering every listing uses."""
    return sorted(
        posts,
        key=lambda p: (p["options"].get("pin", 0), p["date"]),
        reverse=True
    )


def has_sealed_rows(public: list, full: list) -> bool:
    """Whether a listing's fuller form actually holds anything more."""
    return len(full) > len(public)


def public_rows(entries: list[dict]) -> list[dict]:
    """The rows anyone may see: everything not marked unlisted."""
    return [e for e in entries if not e["options"].get("unlisted", False)]


def withheld_rows(entries: list[dict]) -> list[dict]:
    """The rows the phrase puts back.

    Only those held back *because* they are sealed. A post marked unlisted on
    its own account stays unlisted, phrase or no."""
    return [e for e in entries
            if e["options"].get("is_sealed", False)
            and e["options"].get("unlisted", False)]


def tag_row(tag: str, n: int, sealed: bool = False) -> str:
    """One row of the tag overview.

    Every tag has a page of its own, the ones only sealed posts carry
    included — those pages stand empty until a phrase opens them. A row for
    such a tag ships only inside the payload, so the overview never links to
    a page it should not yet admit exists."""
    cls = ' class="sealed-tag"' if sealed else ""
    return (
        f'  <li{cls}><a href="{html.escape(tag)}{EXT}">#{html.escape(tag)}</a>'
        '<span class="leader"></span>'
        f'<span class="count">{n} post{"" if n == 1 else "s"}</span></li>'
    )


def post_head_extras(post: dict) -> list[str]:
    """Head-extras common to any post-like page"""
    opts = post["options"]
    extras = []
    if opts.get("noindex", False):
        extras.append(EXTRA_NOINDEX)
    if opts.get("old", False):
        extras.append(EXTRA_NOINDEX)
    if opts.get("unlisted", False):
        extras.append(EXTRA_NOINDEX)
    if opts.get("is_locked", False):
        extras.append(EXTRA_NOINDEX)
        extras.append(EXTRA_LOCK)
    if opts.get("is_sealed", False):
        extras.append(EXTRA_NOINDEX)
        extras.append(EXTRA_SEAL)
    if opts.get("math", False):
        extras.append(EXTRA_MATH)
    return list(dict.fromkeys(extras))


def post_article_classes(post: dict) -> list[str]:
    """CSS classes for the <article> wrapper"""
    classes = []
    if not post["options"].get("dropcap", True) : classes.append("no-dropcap")
    if post["options"].get("is_locked", False)  : classes.append("locked")
    if post["options"].get("is_sealed", False)  : classes.append("sealed")
    if post.get("is_group", False)              : classes.append("group")
    return classes


def post_badges(post: dict) -> list[str]:
    """Badges displayed on posts"""
    opts = post["options"]
    badges = []
    if opts.get("old", False)       : badges.append(badge("old"))
    if opts.get("draft", False)     : badges.append(badge("draft"))
    if opts.get("unlisted", False)  : badges.append(badge("unlisted"))
    if opts.get("pin", 0) > 0       : badges.append(badge("pin", "PINNED"))
    if opts.get("is_locked", False) : badges.append(badge("locked"))
    if opts.get("is_sealed", False) : badges.append(badge("sealed"))

    if opts.get("is_sealed", False) and opts.get("unlisted", False):
        badges.remove(badge("unlisted"))

    return badges


def render_article(post: dict, *, nav: str = "", root: str = "") -> str:
    """The <article> block shared by post pages, old version pages and group
    pages: header (title + badges), date, rendered HTML, tag footer, then a
    caller-supplied nav appended after </article>.

    The tag footer is a <footer> inside the article and is sealed along with
    the body; `nav` is a sibling of the article and stays in the clear.

    A sealed post emits its unlock notice and an inert base64 payload in
    place of the body. <script> with a non-JS type is neither executed nor
    parsed as markup, so nothing of the post can render before lock.js
    decrypts it.

    A phrase-sealed post does the same, swapping the notice for a field of
    runes and the clock-derived key for a PBKDF2 one; sealed.js takes it
    from there. A group page passes its member listing in as its body, so a
    sealed group is a rune field like any other sealed post and the list of
    members lives inside the payload."""
    classes = post_article_classes(post)
    class_attr = f' class="{" ".join(classes)}"' if classes else ""

    locked = post["options"].get("is_locked", False)
    lock_attr = (
        f' data-unlock="{iso_utc(post["options"]["unlock_time"])}" data-slug="{post["slug"]}"'
        if locked else ""
    )

    sealed = post["options"].get("is_sealed", False)
    if sealed:
        lock_attr = f' data-seal data-slug="{post["slug"]}"'

    badges = post_badges(post)
    badge_wrapper = (f'<div class="post-badges">{"".join(badges)}</div>' if badges else "")

    content = f'{post["html"]}\n{tag_footer(post["tags"])}'
    if locked or sealed:
        # media first, while {site_root} is still a token to match on
        content, cek = seal_assets(content, post["slug"], "sealed" if sealed else "locked")
        if cek is not None:
            content = (
                f'<script type="application/octet-stream" class="asset-key">'
                f'{cek.hex()}</script>\n' + content
            )
        # tags stay inside the payload: they leak the subject matter.
        # {site_root} must be resolved BEFORE sealing — render() substitutes
        # it on the body string, by which point a sealed body is base64 and
        # every link inside it would survive unreplaced.
        content = content.replace("{site_root}", root.rstrip("/"))
    else:
        note_public_assets(content)

    if sealed:
        if SEALED_HANDLE is None:
            raise RuntimeError(
                f'{post["slug"]}: sealed post reached render with no handle. '
                "SEAL_PASSWORD must be set before main() derives one."
            )
        payload = seal_by_phrase(content, SEALED_HANDLE)
        content = (
            seal_notice()
            + f'<script type="application/octet-stream" class="seal-payload"'
            f' data-rounds="{SEAL_ROUNDS}"'
            f' data-handle-rounds="{HANDLE_ROUNDS}"'
            f' data-handle-salt="{HANDLE_SALT}">{payload}</script>\n'
        )
    elif locked:
        payload = seal(content, post["slug"], post["options"]["unlock_time"])
        content = (
            lock_notice(post["options"]["unlock_time"])
            + f'<script type="application/octet-stream" class="lock-payload">{payload}</script>\n'
        )

    return (
        f'<article{class_attr}{lock_attr}>\n'
        '<header class="post">\n'
        f'<h2>{html.escape(post["title"])}{badge_wrapper}</h2>\n'
        f'<time datetime="{post["date"].isoformat()}">{post["date"].strftime(DATE_FMT)}</time>\n'
        '</header>\n'
        f'{content}'
        '</article>\n'
        f'{nav}'
    )


# --------------------------------------------------------------------------
# writers — each renders one part of site/
# --------------------------------------------------------------------------

def root_for(dest: Path) -> str:
    """The {site_root} replacement for a page at dest: derived from its
    depth locally, and "/" in production."""
    if SERVE or not IS_LOCAL:
        return "/"
    depth = len(dest.relative_to(SITE).parent.parts)
    return "../" * depth if depth else "."


def write_page(dest: Path, title: str, body: str, extras=None) -> None:
    """Render body into the page shell and write it to dest (under SITE)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(title, root_for(dest), body, extras), encoding="utf-8")
    WRITTEN_PAGES.add(dest)


def listing_extras(posts, payload: bool = False) -> list[str]:
    """Any listing holding a locked row needs lock.js, so the LOCKED badge
    clears itself when the moment arrives. A sealed row visible to everyone
    needs far less: nothing on the page can be opened, so its badge only has
    to reflect whether this session already holds a handle for this build.

    A listing carrying a payload needs both — the same early class, so the
    public list does not paint before it is replaced, and the script that
    replaces it. It gets them whether the payload withholds anything or is
    a decoy; a page that could be told apart by its <head> would defeat the
    decoy in the body."""
    extras = []
    if any(p["options"].get("is_locked", False) for p in posts):
        extras.append(EXTRA_LOCK)
    if payload or any(p["options"].get("is_sealed", False) for p in posts):
        extras.append(EXTRA_SEAL_BADGE())
    if payload:
        extras.append(EXTRA_SEAL_LISTING)
    return extras


def write_index(listed, revealed) -> None:
    """The front page, and behind the phrase the same page with the sealed
    posts folded back into their places by date.

    Rows here are top-level posts and groups. A grouped post is not one of
    them — it is reached through its group, which carries a row of its own."""
    dest = SITE / "index.html"
    payload = carries_payload(listed, revealed)
    attr = " data-seal-listing" if payload else ""

    body = f'<ul class="toc"{attr}>\n' + post_list_items(listed) + "\n</ul>\n"
    if payload:
        body += seal_listing(
            '<ul class="toc">\n' + post_list_items(revealed) + "\n</ul>\n",
            root_for(dest),
        )

    write_page(
        dest,
        SITE_TITLE,
        body,
        extras=listing_extras(listed, payload)
    )


def write_posts(posts) -> None:
    """Every post with a page of its own — top-level posts and group members
    alike. The two differ only in where the page lands and what the back link
    points at: a grouped post goes back to its group, a top-level one to the
    index."""
    for p in posts:
        if p.get("group"):
            back = (f'<a href="./{INDEX}">&larr; {html.escape(p["group_title"])}</a>')
        else:
            back = f'<a href="../{INDEX}">&larr; all posts</a>'
        hist = ""
        if p["history"]:
            n = len(p["history"])
            hist = (f'<a href="{{site_root}}/{history_url(p.get("group"), p["slug"])}/{INDEX}">'
                    f'{n} earlier version{"" if n == 1 else "s"} &rarr;</a>')
        nav = f'<nav class="post-foot">{back}{hist}</nav>'

        dest = SITE / f'{p["url"]}.html'
        body = render_article(p, nav=nav, root=root_for(dest))
        write_page(
            dest,
            f"{p['title']} — {SITE_TITLE}",
            body,
            extras=post_head_extras(p)
        )


def write_groups(groups) -> None:
    """A page per group: the index, narrowed to one directory.

    The group's own index.md body (if it has one) sits above the list, and
    everything a post page can do the group page can do — badges, tags, math,
    a lock, a seal. Sealing the group seals this page and the list with it;
    the members are untouched and stay exactly as sealed or as open as they
    each asked to be.

    Left unsealed, this is an ordinary listing: public rows in the clear and
    the fuller list behind the phrase, decoy included, so a group holding
    something back cannot be told from one that isn't."""
    for group in groups:
        dest = SITE / group["url"] / "index.html"
        root = root_for(dest)
        sealed = group["options"].get("is_sealed", False)

        members = group["members"]
        public = public_rows(members)
        full = sort_posts(public + withheld_rows(members))

        # a sealed group has no second form to hide: the whole page is already
        # behind the phrase, so the list it carries is the full one.
        payload = not sealed and carries_payload(public, full)
        attr = " data-seal-listing" if payload else ""
        shown = full if sealed else public

        listing = f'<ul class="toc"{attr}>\n{post_list_items(shown)}\n</ul>\n'
        if payload:
            listing += seal_listing(
                '<ul class="toc">\n' + post_list_items(full) + "\n</ul>\n",
                root,
            )

        page = clone_post(group)
        page["html"] = f'{group["html"]}\n{listing}' if group["html"] else listing

        nav = (f'<nav class="post-foot">'
               f'<a href="{{site_root}}/{INDEX}">&larr; all posts</a>'
               f'</nav>')
        body = render_article(page, nav=nav, root=root)

        write_page(
            dest,
            f'{group["title"]} — {SITE_TITLE}',
            body,
            extras=list(dict.fromkeys(
                post_head_extras(group) + listing_extras(shown, payload)
            ))
        )


def write_old_posts(posts: list[dict], old_posts: dict[tuple, list[dict]]) -> None:
    """For each post with old versions, render every old version and an
    index linking to them by date. Version pages mirror regular post pages
    (via render_article) but carry an OLD badge, are noindexed via their
    "old" option, and link back to the version list instead of the post
    index.

    Keyed by (group, slug) rather than slug alone: a grouped post's history
    lives beside its group, so two groups may each hold a "notes" without
    their archives colliding."""
    live = {(p.get("group"), p["slug"]): p for p in posts}
    for key, versions in old_posts.items():
        group, slug = key
        old_dir = SITE / history_url(group, slug)

        # prefer the live post's current title; fall back to the latest old post
        canonical_title = live[key]["title"] if key in live else versions[0]["title"]

        # each old version as its own page
        for v in versions:
            stamp = v["date"].isoformat()
            nav = f'<nav class="back"><a href="./{INDEX}">&larr; all versions</a></nav>'
            dest = old_dir / f"{stamp}.html"
            body = render_article(v, nav=nav, root=root_for(dest))

            write_page(
                dest,
                f'{v["title"]} ({stamp}) — {SITE_TITLE}',
                body,
                extras=post_head_extras(v)
            )

        # index of versions for this slug
        items = "\n".join(
            f'  <li><a href="{{stamp}}{EXT}">{{nice}}</a></li>'.format(
                stamp=v["date"].isoformat(), nice=v["date"].strftime(DATE_FMT)
            )
            for v in versions
        )
        foot = ""
        if key in live:
            foot = (f'<nav class="post-foot">'
                    f'<a href="../../{slug}{EXT}">&larr; current version</a>'
                    f'</nav>')

        old_badge = f'<div class="post-badges">{badge("old")}</div>'
        body = (
            f'<article><header class="post"><h2>{html.escape(canonical_title)}{old_badge}</h2></header></article>\n'
            '<ul class="toc">\n' + items + "\n</ul>\n"
            f'{foot}'
        )

        write_page(
            old_dir / "index.html",
            f'{canonical_title}: history — {SITE_TITLE}',
            body,
            extras=[EXTRA_NOINDEX]
        )


def write_tag_pages(by_tag, by_tag_all) -> None:
    """A page per tag, the ones only sealed posts carry included.

    Such a page exists and is named for its tag, which is the deliberate
    trade: a real URL to navigate to and reload, in exchange for the name
    being guessable by anyone who thinks to try it.

    What that guess buys is the 404 — same body, same title, same head, same
    payload as any other listing, so it cannot be told from a URL that was
    never a page at all. The tag name is not in the served markup anywhere;
    it arrives with the rows, and seal-listing.js puts the title back.

    Note that the status line still reads 200. A static host cannot be told
    to answer differently to a reader it cannot identify — for a genuine 404
    the file must not exist, which means naming it from the handle instead
    and giving up the readable URL.

    Tags are flat: a grouped post is listed here exactly as a top-level one
    is, and a group carrying tags of its own is listed alongside them."""
    for t in sorted(by_tag_all):
        tagged = by_tag.get(t, [])
        full = by_tag_all[t]
        dest = SITE / "tags" / f"{t}.html"
        payload = carries_payload(tagged, full)
        attr = " data-seal-listing" if payload else ""

        # everything a reader sees once the page opens, title included
        title = f"#{t} — {SITE_TITLE}"
        opened = (
            f'<h2 class="tag-title" data-seal-title="{html.escape(title)}">'
            f'#{html.escape(t)}</h2>\n'
            '<ul class="toc">\n' + post_list_items(full) + "\n</ul>\n"
            f'<nav class="back"><a href="./{INDEX}">&larr; all tags</a></nav>'
        )

        if tagged:
            body = (
                f'<h2 class="tag-title">#{html.escape(t)}</h2>\n'
                f'<ul class="toc"{attr}>\n'
                + post_list_items(tagged) + "\n</ul>\n"
                f'<nav class="back"><a href="./{INDEX}">&larr; all tags</a></nav>'
            )
            hidden = '<ul class="toc">\n' + post_list_items(full) + "\n</ul>"
        else:
            body = not_found_body(attr)
            hidden = opened
            title = f"Not found — {SITE_TITLE}"

        if payload:
            body += seal_listing(hidden, root_for(dest))

        write_page(
            dest,
            title,
            body,
            extras=[EXTRA_NOINDEX] + listing_extras(tagged, payload)
        )


def write_tag_index(by_tag, by_tag_all) -> None:
    """The tag overview, and behind the phrase the same overview with true
    counts and the tags no open post carries.

    A tag carried only by sealed posts is absent from the public list
    entirely, not shown greyed or teased: a reader without the phrase has no
    use for a row they cannot follow, and the name is the thing being kept
    back. Its page exists all the while, unlinked, waiting for the row that
    points at it."""
    dest = SITE / "tags" / "index.html"
    payload = carries_payload(list(by_tag), list(by_tag_all)) or any(
        len(by_tag_all[t]) > len(by_tag[t]) for t in by_tag
    )
    attr = " data-seal-listing" if payload else ""

    public = "\n".join(tag_row(t, len(ps)) for t, ps in sorted(by_tag.items()))
    body = (f'<ul class="toc"{attr}>\n{public}\n</ul>\n'
        f'<nav class="back"><a href="../{INDEX}">&larr; all posts</a></nav>')

    if payload:
        rows = "\n".join(
            tag_row(t, len(by_tag_all[t]), t not in by_tag)
            for t in sorted(by_tag_all)
        )
        body += seal_listing(f'<ul class="toc">\n{rows}\n</ul>', root_for(dest))

    write_page(
        dest,
        f"Tags — {SITE_TITLE}",
        body,
        extras=[EXTRA_NOINDEX] + listing_extras([], payload)
    )


def not_found_body(attr: str = "") -> str:
    """The whole of the 404 — the transition suppressant, the article, and
    the fade that stands in for one.

    All three, not just the article: a tag page only sealed posts carry wears
    this entire, and a disguise missing the neighbours' furniture is not a
    disguise. Two pages that differ by a <style> and a <script> are two pages
    that can be told apart by reading them.

    ABSOLUTE asset paths, because 404.html is served for any unmatched URL at
    any depth and relative ones would break.

    The fade stands down once the article is gone. On a tag page that opens,
    the links that replace it are ordinary links to real pages, and fading
    the body out before following them would be theatre borrowed from a page
    this no longer is."""
    return (
        f'<article class="notfound"{attr}>\n'
        '<header class="post">\n'
        '<h2>Lost in the sublunary</h2>\n'
        '</header>\n'
        '<p>There is no page at this address. The path you followed may be broken, or the writing may have been unmade.</p>\n'
        f'<p><a href="/{INDEX}">Return to the index</a>, or <a href="/tags/{INDEX}">wander the tags</a>.</p>\n'
        '</article>\n'
        '<style id="no-view-transition">@view-transition { navigation: none; }</style>\n'
        '<script id="fake-view-transition">\n'
        '  // Fake a view transition out of the 404 page\n'
        '  document.addEventListener("click", function(e) {\n'
        '    if (!document.querySelector("article.notfound")) return;\n'
        '    var link = e.target.closest("a");\n'
        '    if (link && link.host === window.location.host) {\n'
        '      e.preventDefault();\n'
        '      document.body.style.transition = "opacity 0.2s ease";\n'
        '      document.body.style.opacity = "0";\n'
        '      setTimeout(() => window.location.href = link.href, 200);\n'
        '    }\n'
        '  });\n'
        '</script>\n'
    )


def write_404() -> None:
    """A site-wide 404. Served from the site root by GitHub Pages for any
    unmatched URL, so it uses ABSOLUTE asset paths (site_root="/") — relative
    ones would break for deep URLs like /posts/x that don't exist.

    It carries a decoy of its own when the build has sealed anything. A tag
    page that only sealed posts carry wears this same body, and a reader who
    guesses that URL must not be able to tell the two apart by finding a
    payload on one and not the other."""
    decoy = carries_payload([], [])
    body = (
        not_found_body(" data-seal-listing" if decoy else "")
        + (seal_listing(not_found_body(), "/") if decoy else "")
    )
    dest = SITE / "404.html"
    dest.write_text(
        render(
            f"Not found — {SITE_TITLE}",
            "/",
            body,
            extras=[EXTRA_NOINDEX] + listing_extras([], decoy)
        ),
        encoding="utf-8",
    )
    WRITTEN_PAGES.add(dest)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def clear_dir(path: Path) -> None:
    """Empty a directory without removing the directory itself.

    rmtree()-ing site/ pulls the rug out from under any process whose cwd is
    inside it — a dev server started with `cd site` loses its working directory
    and fails every later request. Reusing the inode keeps that valid."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_favicons() -> None:
    """The favicon set, which is one directory in the source and another in
    the build depending on the environment."""
    shutil.copytree(
        STATIC / ("favicon" if not IS_LOCAL else "favicon_dev"),
        SITE / STATIC.name / "favicon",
        dirs_exist_ok=True,
    )
    shutil.copy(SITE / STATIC.name / "favicon" / "favicon.ico", SITE / "favicon.ico")


def minify_assets() -> None:
    """Minify .css and .js in place. Production only — a local build serves
    the sources so a stack trace points at a line that exists."""
    styles_dir = SITE / STATIC.name / "styles"
    if styles_dir.exists():
        for css_file in styles_dir.glob("*.css"):
            raw_css = css_file.read_text(encoding="utf-8")
            minified_css = rcssmin.cssmin(raw_css)
            css_file.write_text(minified_css, encoding="utf-8")

    scripts_dir = SITE / STATIC.name / "scripts"
    if scripts_dir.exists():
        for js_file in scripts_dir.glob("*.js"):
            raw_js = js_file.read_text(encoding="utf-8")
            minified_js = rjsmin.jsmin(raw_js)
            if isinstance(minified_js, bytes):
                minified_js = minified_js.decode("utf-8")
            else:
                minified_js = str(minified_js)
            js_file.write_text(minified_js, encoding="utf-8")


def prepare_output() -> None:
    """Wipe site/ and copy static assets in. Used off --serve."""
    clear_dir(SITE)

    # Copy static files
    shutil.copytree(
        STATIC,
        SITE / STATIC.name,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("components", "favicon", "favicon_dev"),
    )

    copy_favicons()

    if not IS_LOCAL:
        minify_assets()


def sync_static(changed: set[Path]) -> None:
    """Mirror just the static files the watcher saw move.

    Stands in for prepare_output() on a --serve rebuild. Walking the tree to
    work out what changed would cost more than everything else in the build
    put together — and would be the second walk, since the watcher has just
    stat()ed every one of these files and knows the answer. This takes that
    answer rather than re-deriving a worse version of it.

    site/ therefore survives between rebuilds, which has three consequences
    handled elsewhere: plaintext a sealed post withdrew is restored by
    withdraw_sealed_assets(); stale .enc files are swept there too; and pages
    for deleted posts are swept by prune_orphan_pages()."""
    for source in sorted(changed):
        try:
            rel = source.relative_to(STATIC)
        except ValueError:
            continue                       # not a static file
        top = rel.parts[0] if rel.parts else ""
        if top == "components":
            continue                       # spliced into posts, never copied
        if top in ("favicon", "favicon_dev"):
            copy_favicons()
            continue

        dest = SITE / STATIC.name / rel
        if not source.exists():
            dest.unlink(missing_ok=True)
            continue
        if source in WITHDRAWN:
            continue                       # a shut-in post has pulled this one
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)


def prune_orphan_pages() -> None:
    """Delete pages the previous build wrote and this one did not.

    site/ is not emptied under --serve, so a deleted post, version, group or
    tag would otherwise go on being served indefinitely. Every page is written
    every build, so the two sets are complete and their difference is exactly
    the orphans — no walk of site/ required."""
    orphans = PREV_PAGES - WRITTEN_PAGES
    if not orphans:
        return
    for page in sorted(orphans):
        page.unlink(missing_ok=True)

    for d in sorted({p.parent for p in orphans}, key=lambda q: len(q.parts), reverse=True):
        while d != SITE and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent


def load_posts() -> list[dict]:
    """Parse every top-level post, newest first. Grouped posts come from
    load_groups() instead."""
    valid_posts = []

    for p in POSTS.glob("*.md"):
        post = parse_post_cached(p)
        if post["options"].get("draft", False) and not IS_LOCAL:
            continue
        post["group"] = None
        post["url"] = f'posts/{post["slug"]}'
        valid_posts.append(post)

    return sort_posts(valid_posts)


def load_groups() -> list[dict]:
    """Parse every group directory under posts/, newest first, each carrying
    its members under "members".

    The directory is named like a post — a YYYY-MM-DD- prefix gives the date
    the group is filed under, and the remainder is its slug and the path its
    pages are written to. An index.md inside supplies title, body, tags and
    OPTIONS; without one, the slug is titled and the group is a bare list.

    Because the group is named by its directory, index.md is parsed with that
    (date, slug) forced: bare <include> and media filenames inside it then
    resolve under components/<group-slug>/ and media/<group-slug>/ rather
    than under an "index" that means nothing.

    Members are ordinary posts. They keep their own slug — so their assets
    resolve exactly as a top-level post's do — and gain the group they belong
    to, which decides where their page and their history are written."""
    groups: list[dict] = []
    if not POSTS.exists():
        return groups

    for d in sorted(POSTS.iterdir()):
        if not d.is_dir() or d.name == OLD.name:
            continue

        ident = date_and_slug(d)
        gdate, gslug = ident
        meta = d / GROUP_META
        if meta.is_file():
            group = parse_post_cached(meta, ident=ident)
        else:
            group = {
                "title": gslug.replace("-", " ").title(),
                "date": gdate,
                "slug": gslug,
                "tags": [],
                "html": "",
                "options": {},
                "deps": set(),
            }
        if group["options"].get("draft", False) and not IS_LOCAL:
            continue

        group["is_group"] = True
        group["group"] = None
        group["url"] = f"posts/{gslug}"
        group["history"] = []

        members = []
        for m in sorted(d.glob("*.md")):
            if m.name == GROUP_META:
                continue
            post = parse_post_cached(m)
            if post["options"].get("draft", False) and not IS_LOCAL:
                continue
            post["group"] = gslug
            post["group_title"] = group["title"]
            post["url"] = f'posts/{gslug}/{post["slug"]}'
            members.append(post)
        group["members"] = sort_posts(members)

        groups.append(group)

    return sort_posts(groups)


def load_old_versions() -> dict[tuple[str | None, str], list[dict]]:
    """Map (group, slug) -> that post's old versions, newest first.
    A post appears here only if at least one old version exists.

    posts/old/*.md are the histories of top-level posts;
    posts/old/<group-slug>/*.md are the histories of that group's members, so
    a slug only has to be unique inside its group.

    Parsed with old=True so bare component and media filenames can resolve
    to a per-version snapshot where one has been kept."""
    by_key: dict[tuple[str | None, str], list[dict]] = {}
    if not OLD.exists():
        return by_key

    sources: list[tuple[str | None, Path]] = [(None, p) for p in OLD.glob("*.md")]
    for d in sorted(OLD.iterdir()):
        if d.is_dir():
            sources += [(d.name, p) for p in sorted(d.glob("*.md"))]

    for group, p in sources:
        post = parse_post_cached(p, old=True)
        if post["options"].get("draft", False) and not IS_LOCAL:
            continue
        post["options"]["old"] = True
        post["group"] = group
        by_key.setdefault((group, post["slug"]), []).append(post)

    for versions in by_key.values():
        versions.sort(key=lambda v: v["date"], reverse=True)
    return by_key


def prune_parse_cache() -> None:
    """Drop entries for .md files that have since been deleted."""
    for key in [k for k in PARSE_CACHE if not Path(k[0]).exists()]:
        del PARSE_CACHE[key]


def main(changed: set[Path] | None = None) -> None:
    """Write the site. `changed` is the batch of paths a --serve watcher saw;
    None means start from scratch — wipe site/ and copy the static tree in.

    Every page is written either way. The only thing `changed` decides is how
    much of static/ has to be touched to get there."""
    global HANDLE_SALT, SEALED_HANDLE, SITE_HAS_SEALED, BUILD_TIME, BUILD_ID
    global RELOAD_HINT, PREV_PAGES
    full = changed is None or not SERVE
    HANDLE_SALT = "10ca1" * 6 + "77" if IS_LOCAL else os.urandom(16).hex()
    SEALED_HANDLE = derive_handle(SEAL_PASSWORD, HANDLE_SALT) if SEAL_PASSWORD else None
    BUILD_TIME = datetime.now(timezone.utc)
    started = time.perf_counter()
    PREV_PAGES = set() if full else set(WRITTEN_PAGES)
    for cache in (SEALED_ASSETS, PUBLIC_ASSETS, LIVE_ENC, WRITTEN_PAGES):
        cache.clear()
    if full:
        WITHDRAWN.clear()
        ASSET_KEYS.clear()
        ASSET_NAMES.clear()
    with BUILD_LOCK:
        if full:
            prepare_output()
        else:
            sync_static(changed)

        posts = load_posts()
        groups = load_groups()
        members = [m for g in groups for m in g["members"]]
        pages = posts + members        # everything with a page of its own
        old_posts = load_old_versions()
        prune_parse_cache()
        for p in pages:
            p["history"] = old_posts.get((p.get("group"), p["slug"]), [])

        clash = {g["slug"] for g in groups} & {p["slug"] for p in posts}
        if clash:
            print(f"warning: group and top-level post share a slug, so one "
                  f"buries the other's URL: {sorted(clash)}")

        # The index lists top-level posts and groups; a grouped post is
        # reached through its group and never appears here on its own.
        #
        # A sealed entry is unlisted by default, so it is absent from that
        # list. `revealed` puts those back — and only those: an entry marked
        # unlisted on its own account stays unlisted, phrase or no.
        entries = sort_posts(posts + groups)
        listed = public_rows(entries)
        revealed = sort_posts(listed + withheld_rows(entries))

        # Tags are flat and know nothing of groups: every post carries its own
        # tags wherever it lives, and a group carrying tags is indexed under
        # them alongside the posts.
        taggable = sort_posts(pages + groups)
        tag_public = public_rows(taggable)
        tag_all = sort_posts(tag_public + withheld_rows(taggable))

        # Decoys are pointless on a site with nothing sealed, and a payload on
        # every listing of one would be a claim about the site that is not
        # true. Set before any writer runs; carries_payload() reads it.
        SITE_HAS_SEALED = any(e["options"].get("is_sealed", False)
                              for e in pages + groups)

        by_tag = group_by_tag(tag_public)
        by_tag_all = group_by_tag(tag_all)

        write_index(listed, revealed)
        write_posts(pages)
        write_groups(groups)
        write_old_posts(pages, old_posts)
        write_tag_pages(by_tag, by_tag_all)
        write_tag_index(by_tag, by_tag_all)
        write_404()
        withdraw_sealed_assets()   # after every page has declared what it uses
        if not full:
            prune_orphan_pages()   # site/ was not emptied; catch what went away
    elapsed = (time.perf_counter() - started) * 1000
    BUILD_ID = str(time.time_ns())
    RELOAD_HINT = reload_hint(changed if not full else None)
    with BUILD_DONE:
        BUILD_DONE.notify_all()   # wake any open reload stream

    shut_set = pages + groups
    locked = sum(1 for p in shut_set if p["options"].get("is_locked", False))
    sealed = sum(1 for p in shut_set if p["options"].get("is_sealed", False))
    shut = ", ".join(
        part for part in (
            f"{locked} locked" if locked else "",
            f"{sealed} sealed" if sealed else "",
        ) if part
    )
    if SEALED_ASSETS:
        print(f"sealed {len(SEALED_ASSETS)} media "
              f'{"file" if len(SEALED_ASSETS) == 1 else "files"}, '
              f"withdrew {len(SEALED_ASSETS - PUBLIC_ASSETS)} from the clear")
        kept = SEALED_ASSETS & PUBLIC_ASSETS
        for source in sorted(kept):
            print(f"  still public (an open post uses it): "
                  f"{source.relative_to(ROOT)}")

    old = sum(len(v) for v in old_posts.values())
    slugs_with_history = len(old_posts)
    print(
        f'built {len(pages)} {"post" if len(pages) == 1 else "posts"}'
        f'{f" ({shut})" if shut else ""}, '
        f'{len(groups)} {"group" if len(groups) == 1 else "groups"} '
        f'holding {len(members)}, '
        f'{old} old {"version" if old == 1 else "versions"} '
        f'across {slugs_with_history} {"slug" if slugs_with_history == 1 else "slugs"}, '
        f'{len(by_tag_all)} {"tag" if len(by_tag_all) == 1 else "tags"}'
        f'{f" ({len(by_tag_all) - len(by_tag)} sealed)" if len(by_tag_all) > len(by_tag) else ""}'
        f': {SITE}/ ({elapsed:.0f} ms)'
    )


def reload_hint(changed: set[Path] | None) -> str:
    """What the client should do about this build.

    A rebuild in which nothing but stylesheets moved can be applied by
    swapping the <link> hrefs; anything else is a reload. Markup is rendered
    from the posts either way, so the test is only on what changed — a
    stylesheet edit cannot alter a page's HTML."""
    hint: dict[str, object] = {"build": BUILD_ID, "css": []}
    if changed:
        styles = STATIC / "styles"
        if all(p.suffix == ".css" and p.parent == styles and p.exists() for p in changed):
            hint["css"] = [f"/static/styles/{p.name}" for p in sorted(changed)]
    return json.dumps(hint)


# --------------------------------------------------------------------------
# dev server
# --------------------------------------------------------------------------
class DevServer(ThreadingHTTPServer):
    """Suppresses the traceback socketserver logs when a client disconnects
    mid-response. Only the logging changes: the request still fails and the
    socket is still closed."""

    request_queue_size = 64

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1],
                      (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class DevHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    PING_SECONDS = 15

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__reload":
            self.stream_reloads()
            return
        with BUILD_LOCK:
            super().do_GET()

    def do_HEAD(self):
        with BUILD_LOCK:
            super().do_HEAD()

    def stream_reloads(self) -> None:
        """Hold the connection open and emit an event on each rebuild.

        Each event carries an id, so a reconnecting EventSource announces the
        last build it saw in Last-Event-ID and is brought up to date on the
        spot. Without that, a stream that dropped while the server restarted
        (an edit to build.py re-execs it) would come back believing whatever
        was built while it was away was what the tab already had.

        Reads BUILD_ID without the lock. A str assignment is atomic under
        CPython, and the worst a torn read could do is delay a reload until
        the next wakeup. Cache-Control is added by end_headers() below."""
        seen = self.headers.get("Last-Event-ID") or BUILD_ID
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # in case a proxy ever sits in front
        self.end_headers()
        try:
            while True:
                if BUILD_ID != seen:
                    seen = BUILD_ID
                    self.wfile.write(f"id: {seen}\ndata: {RELOAD_HINT}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                with BUILD_DONE:
                    BUILD_DONE.wait(timeout=self.PING_SECONDS)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # the tab closed; the thread is free to end

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_request(self, code="-", size="-") -> None:
        """Log failures only. Overriding log_request rather than log_message
        because only this one is passed a status code — log_error goes through
        log_message too, with a different and shorter argument list."""
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if status >= 400:
            super().log_request(code, size)

    def log_error(self, format: str, *args: object) -> None:
        """Idle keep-alive connections are closed by `timeout` above, and the
        stdlib reports each one. That is the reaping working as intended, and
        a browser holds several sockets open at a time, so it would be steady
        noise for something that is not a fault."""
        if "timed out" in format:
            return
        super().log_error(format, *args)

    def translate_path(self, path):
        fs = super().translate_path(path)
        if not os.path.exists(fs) and not fs.endswith(os.sep):
            candidate = fs + ".html"
            if os.path.isfile(candidate):
                return candidate
        return fs

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            page = Path(self.directory) / "404.html"
            if page.is_file():
                body = page.read_bytes()
                self.send_response(404, message)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
        super().send_error(code, message, explain)


SOURCES = ("build.py", "media_ext.py")


def watched_files():
    for name in SOURCES:
        yield ROOT / name
    for base in (POSTS, STATIC):
        if base.exists():
            yield from (p for p in base.rglob("*") if p.is_file())


def snapshot() -> dict[Path, int]:
    return {p: p.stat().st_mtime_ns for p in watched_files() if p.exists()}


def diff(prev: dict[Path, int], cur: dict[Path, int]) -> set[Path]:
    """The paths that appeared, vanished or moved between two sweeps.

    The watcher has to stat every watched file anyway; this is the only extra
    line needed to keep what it learned, and it saves the build a second walk
    of the same tree."""
    return {p for p in prev.keys() | cur.keys() if prev.get(p) != cur.get(p)}


def restart() -> None:
    """Re-exec the interpreter after the generator itself changes.

    Rebuilding in place would run the module already in memory: every edit to
    build.py or media_ext.py would appear to take effect and none of them
    would. Open reload streams die with the process; their Last-Event-ID
    brings the tabs back on reconnect."""
    print("build source changed; restarting")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def serve(port: int) -> None:
    global SERVE, INDEX, EXT
    SERVE = True
    INDEX = ""
    EXT = ""
    main()

    handler = partial(DevHandler, directory=str(SITE))
    httpd = DevServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"serving http://127.0.0.1:{port}  (ctrl-c to stop)")

    prev = snapshot()
    try:
        while True:
            time.sleep(0.4)
            cur = snapshot()
            changed = diff(prev, cur)
            if not changed:
                continue
            prev = cur
            if any(p.name in SOURCES and p.parent == ROOT for p in changed):
                restart()
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] rebuilding...")
            try:
                main(changed)
            except Exception:
                traceback.print_exc()   # keep serving; fix the post and save again
    except KeyboardInterrupt:
        httpd.shutdown()



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="serve site/ and rebuild on change")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    serve(args.port) if args.serve else main()

