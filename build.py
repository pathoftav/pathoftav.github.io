"""Build a small plaintext blog.

Usage:
    python build.py

Layout:
    posts/    Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
    static/   files copied verbatim into the build
    site/     generated output — deploy this directory anywhere static

Post format: if the first line is an H1 ("# Title") it becomes the
post title; otherwise the first line is taken as the title verbatim.
The rest is standard Markdown (with extensions enabled).
If the LAST line consists only of hashtags ("#magic #geomancy"), they
become the post's tags: rendered as links on the post page, indexed
under site/tags/<tag>.html, with an overview at site/tags/index.html.
"""

import argparse
import base64
import hashlib
import html
import json
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

SITE_TITLE = "Sublunary Musings"
SITE_SUBTITLE = "philosophy, magic, and other errata"
DATE_FMT = "%B %-d, %Y"

SITE_TZ = ZoneInfo("America/New_York")
UNLOCK_FMT = "%B %-d, %Y at %-I:%M %p %Z"
BUILD_TIME = datetime.now(timezone.utc)

SEALED_PASSWORD = os.getenv("SEALED_PASSWORD")
SEAL_ROUNDS = 310_000       # PBKDF2 rounds — the cost of one guess at the phrase.
HANDLE_ROUNDS = 310_000     # the same as above, one stage earlier. Rides in data-handle-rounds.
HANDLE_SALT = ""            # derived per build
SEALED_HANDLE = None        # derived per build

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

SERVE = False                   # True when running under --serve
BUILD_ID = "0"                  # bumped each rebuild; the reload poller watches this
BUILD_LOCK = threading.Lock()   # held while site/ is mid-rebuild and briefly empty
LIVE_RELOAD = """<script>
(function () {
  var seen = null;
  setInterval(function () {
    fetch("/__build", { cache: "no-cache" })
      .then(function (r) { return r.text(); })
      .then(function (v) {
        if (seen === null) { seen = v; }
        else if (v !== seen) { location.reload(); }
      })
      .catch(function () {});
  }, 700);
})();
</script>"""

EXTRA_NOINDEX = '<meta name="robots" content="noindex">'
EXTRA_LOCK = (
    '<link rel="stylesheet" href="{site_root}/static/styles/locked.css">\n'
    '<style>html.lock-pending [data-unlock] {{ visibility: hidden; }}</style>\n'
    '<script>document.documentElement.classList.add("lock-pending");</script>\n'
    '<script defer src="{site_root}/static/scripts/lock.js"></script>'
)
EXTRA_SEAL = (
    '<link rel="stylesheet" href="{site_root}/static/styles/sealed.css">\n'
    '<style>html.seal-pending [data-seal] {{ visibility: hidden; }}</style>\n'
    '<script>try {{ if (sessionStorage.getItem("seal-handle")) '
    'document.documentElement.classList.add("seal-pending"); }} catch (e) {{}}</script>\n'
    '<script defer src="{site_root}/static/scripts/sealed.js"></script>'
)
def EXTRA_SEAL_BADGE() -> str:
    return (
        '<link rel="stylesheet" href="{site_root}/static/styles/sealed.css">\n'
        '<script>try {{ if (sessionStorage.getItem("seal-salt") === "'
        + HANDLE_SALT
        + '") document.documentElement.classList.add("seal-open"); }} catch (e) {{}}</script>'
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


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_post(path: Path) -> dict:
    """Read one .md file into a post dict: title, date, slug, tags, html, options."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"{path}: post is empty")
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body_lines = lines[1:]

    options = extract_options(body_lines)       # NOTE: mutates body_lines: pops the OPTIONS line
    tags = extract_tags(body_lines)             # NOTE: mutates body_lines: pops the tag line
    body = "\n".join(body_lines).strip()
    d, slug = date_and_slug(path)
    parse_sealed_option(options, path.name)     # NOTE: mutates options: replaces "sealed" with "is_sealed", overrides locked_options and removes them if present
    parse_locked_option(options, path.name)     # NOTE: mutates options: replaces "locked" with "is_locked" and "unlock_time"
    rendered = render_markdown(body, options)

    return {
        "title": title,
        "date": d,
        "slug": slug,
        "tags": tags,
        "html": rendered,
        "options": options,
    }


def extract_options(body_lines: list[str]) -> dict:
    """Peel a trailing OPTIONS line off body_lines (in place) and return the
    list of options as a dictionary. Done BEFORE tags or markdown.

    The options line must be in a specific format at the very bottom of the file:
    <!-- [OPTIONS]: { "draft": true, "math": true } -->

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
        "sealed":   bool   encrypt the body against SEALED_PASSWORD; listed,
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
    otherwise fall back to the file's mtime."""
    m = DATE_RE.match(path.stem)
    if m:
        return date.fromisoformat(m.group(1)), path.stem[len(m.group(0)):]
    return datetime.fromtimestamp(path.stat().st_mtime).date(), path.stem


def render_markdown(body: str, options: dict) -> str:
    """Convert post body to HTML, prepending a Contents panel when the post
    has at least three top-level (##) sections."""
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
            MediaExtension(),
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

        "is_sealed": bool   body encrypted against SEALED_PASSWORD

    Unlike "locked", a missing secret is fatal. Warning and publishing anyway
    would put the plaintext of a post the author meant to hide on the open
    web, and no build is worth that."""
    if not options.pop("sealed", False):
        return
    if not SEALED_PASSWORD:
        raise RuntimeError(
            f'{source}: post is "sealed" but SEALED_PASSWORD is unset. '
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
    already captured stays readable, but nothing published afterwards does."""
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(salt_hex),
        iterations=HANDLE_ROUNDS,
    ).derive(phrase.encode()).hex()


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


def post_list_items(posts, slug_prefix: str) -> str:
    """The dotted-leader <li> rows used by the index and by tag pages.

    A sealed row carries data-unlock so lock.js can drop the badge without
    waiting for the next build."""
    def item(p: dict) -> str:
        locked = p["options"].get("is_locked", False)
        unlock = f' data-unlock="{iso_utc(p["options"]["unlock_time"])}"' if locked else ""
        badges = ""
        if p["options"].get("pin"):
            badges += badge("pin", "PINNED") + "&nbsp;"
        if locked:
            badges += badge("locked") + "&nbsp;"
        if p["options"].get("is_sealed", False):
            badges += badge("sealed") + "&nbsp;"
        return (
            f'<li{unlock}><a href="{slug_prefix}{p["slug"]}{EXT}">{html.escape(p["title"])}</a>'
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


def render_article(post: dict, *, footer: str = "", root: str = "") -> str:
    """The <article> block shared by post pages and old version pages:
    header (title + badges), date, rendered HTML, tag footer, then a caller
    -supplied footer nav appended after </article>.

    A sealed post emits its unlock notice and an inert base64 payload in
    place of the body. <script> with a non-JS type is neither executed nor
    parsed as markup, so nothing of the post can render before lock.js
    decrypts it.

    A phrase-sealed post does the same, swapping the notice for a field of
    runes and the clock-derived key for a PBKDF2 one; sealed.js takes it
    from there."""
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
        # tags stay inside the payload: they leak the subject matter.
        # {site_root} must be resolved BEFORE sealing — render() substitutes
        # it on the body string, by which point a sealed body is base64 and
        # every link inside it would survive unreplaced.
        content = content.replace("{site_root}", root.rstrip("/"))

    if sealed:
        if SEALED_HANDLE is None:
            raise RuntimeError(
                f'{post["slug"]}: sealed post reached render with no handle. '
                "SEALED_PASSWORD must be set before main() derives one."
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
        f'{footer}'
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


def listing_extras(posts) -> list[str]:
    """Any listing holding a locked row needs lock.js, so the LOCKED badge
    clears itself when the moment arrives. A sealed row needs far less:
    nothing on the page can be opened, so its badge only has to reflect
    whether this session already holds a handle for this build."""
    extras = []
    if any(p["options"].get("is_locked", False) for p in posts):
        extras.append(EXTRA_LOCK)
    if any(p["options"].get("is_sealed", False) for p in posts):
        extras.append(EXTRA_SEAL_BADGE())
    return extras


def write_index(posts) -> None:
    body = '<ul class="toc">\n' + post_list_items(posts, "posts/") + "\n</ul>"
    write_page(
        SITE / "index.html",
        SITE_TITLE,
        body,
        extras=listing_extras(posts)
    )


def write_posts(posts) -> None:
    for p in posts:
        back = f'<a href="../{INDEX}">&larr; all posts</a>'
        hist = ""
        if p["history"]:
            n = len(p["history"])
            hist = (f'<a href="{{site_root}}/posts/old/{p["slug"]}/{INDEX}">'
                    f'{n} earlier version{"" if n == 1 else "s"} &rarr;</a>')
        footer = f'<nav class="post-foot">{back}{hist}</nav>'

        dest = SITE / "posts" / f"{p['slug']}.html"
        body = render_article(p, footer=footer, root=root_for(dest))
        write_page(
            dest,
            f"{p['title']} — {SITE_TITLE}",
            body,
            extras=post_head_extras(p)
        )


def write_old_posts(posts: list[dict], old_posts: dict[str, list[dict]]) -> None:
    """For each slug with old versions, render every old version and an
    index linking to them by date. Version pages mirror regular post pages
    (via render_article) but carry an OLD badge, are noindexed via their
    "old" option, and link back to the version list instead of the post
    index."""
    live = {p["slug"]: p for p in posts}
    for slug, versions in old_posts.items():
        old_dir = SITE / "posts" / "old" / slug

        # prefer the live post's current title; fall back to the latest old post
        canonical_title = live[slug]["title"] if slug in live else versions[0]["title"]

        # each old version as its own page
        for v in versions:
            stamp = v["date"].isoformat()
            footer = f'<nav class="back"><a href="./{INDEX}">&larr; all versions</a></nav>'
            dest = old_dir / f"{stamp}.html"
            body = render_article(v, footer=footer, root=root_for(dest))

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
        if slug in live:
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


def write_tag_pages(by_tag) -> None:
    for t, tagged in by_tag.items():
        body = (
            f'<h2 class="tag-title">#{html.escape(t)}</h2>\n'
            '<ul class="toc">\n' + post_list_items(tagged, "../posts/") + "\n</ul>\n"
            f'<nav class="back"><a href="./{INDEX}">&larr; all tags</a></nav>'
        )
        write_page(
            SITE / "tags" / f"{t}.html",
            f"#{t} — {SITE_TITLE}",
            body,
            extras=[EXTRA_NOINDEX] + listing_extras(tagged)
        )


def write_tag_index(by_tag) -> None:
    tag_items = "\n".join(
        f'  <li><a href="{{t}}{EXT}">#{{t}}</a>'
        '<span class="leader"></span>'
        '<span class="count">{n} post{s}</span></li>'.format(
            t=html.escape(t), n=len(ps), s="" if len(ps) == 1 else "s"
        )
        for t, ps in sorted(by_tag.items())
    )
    body = (f'<ul class="toc">\n{tag_items}\n</ul>\n'
        f'<nav class="back"><a href="../{INDEX}">&larr; all posts</a></nav>')
    write_page(
        SITE / "tags" / "index.html",
        f"Tags — {SITE_TITLE}",
        body,
        extras=[EXTRA_NOINDEX]
    )


def write_404() -> None:
    """A site-wide 404. Served from the site root by GitHub Pages for any
    unmatched URL, so it uses ABSOLUTE asset paths (site_root="/") — relative
    ones would break for deep URLs like /posts/x that don't exist."""
    body = (
        '<style>\n'
        '@view-transition { navigation: none; }\n'
        '</style>\n'
        '<article>\n'
        '<header class="post">\n'
        '<h2>Lost in the sublunary</h2>\n'
        '</header>\n'
        '<p>There is no page at this address. The path you followed may be broken, or the writing may have been unmade.</p>\n'
        f'<p><a href="/{INDEX}">Return to the index</a>, or <a href="/tags/{INDEX}">wander the tags</a>.</p>\n'
        '</article>\n'
        '<script>\n'
        '  // Fake a view transition out of the 404 page\n'
        '  document.addEventListener("click", function(e) {\n'
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
    (SITE / "404.html").write_text(
        render(
            f"Not found — {SITE_TITLE}",
            "/",
            body,
            extras=[EXTRA_NOINDEX]
        ),
        encoding="utf-8",
    )


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


def prepare_output() -> None:
    """Wipe site/ and copy static assets in."""
    clear_dir(SITE)
    shutil.copytree(STATIC, SITE / STATIC.name, dirs_exist_ok=True)

    if not IS_LOCAL:
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


def load_posts() -> list[dict]:
    """Parse every post, newest first."""
    valid_posts = []

    for p in POSTS.glob("*.md"):
        post = parse_post(p)
        if post["options"].get("draft", False) and not IS_LOCAL:
            continue
        valid_posts.append(post)

    return sorted(
        valid_posts,
        key=lambda p: (p["options"].get("pin", 0), p["date"]),
        reverse=True
    )


def load_old_versions() -> dict[str, list[dict]]:
    """Map slug -> its old versions in posts/old/, newest first.
    A slug appears here only if at least one old version exists."""
    by_slug: dict[str, list[dict]] = {}
    if not OLD.exists():
        return by_slug
    for p in OLD.glob("*.md"):
        post = parse_post(p)
        if post["options"].get("draft", False) and not IS_LOCAL:
            continue
        post["options"]["old"] = True
        by_slug.setdefault(post["slug"], []).append(post)
    for versions in by_slug.values():
        versions.sort(key=lambda v: v["date"], reverse=True)
    return by_slug


def main() -> None:
    global HANDLE_SALT, SEALED_HANDLE, BUILD_TIME, BUILD_ID
    HANDLE_SALT = "10ca1" * 6 + "77" if IS_LOCAL else os.urandom(16).hex()
    SEALED_HANDLE = derive_handle(SEALED_PASSWORD, HANDLE_SALT) if SEALED_PASSWORD else None
    BUILD_TIME = datetime.now(timezone.utc)
    with BUILD_LOCK:
        prepare_output()
        posts = load_posts()
        old_posts = load_old_versions()
        for p in posts:
            p["history"] = old_posts.get(p["slug"], [])

        listed = [p for p in posts if not p["options"].get("unlisted", False)]
        by_tag = group_by_tag(listed)

        write_index(listed)
        write_posts(posts)
        write_old_posts(posts, old_posts)
        write_tag_pages(by_tag)
        write_tag_index(by_tag)
        write_404()
    BUILD_ID = str(time.time_ns())

    locked = sum(1 for p in posts if p["options"].get("is_locked", False))
    sealed = sum(1 for p in posts if p["options"].get("is_sealed", False))
    shut = ", ".join(
        part for part in (
            f"{locked} locked" if locked else "",
            f"{sealed} sealed" if sealed else "",
        ) if part
    )
    old = sum(len(v) for v in old_posts.values())
    slugs_with_history = len(old_posts)
    print(
        f'built {len(posts)} {"post" if len(posts) == 1 else "posts"}'
        f'{f" ({shut})" if shut else ""}, '
        f'{old} old {"version" if old == 1 else "versions"} '
        f'across {slugs_with_history} {"slug" if slugs_with_history == 1 else "slugs"}, '
        f'{len(by_tag)} {"tag" if len(by_tag) == 1 else "tags"}: {SITE}/'
    )


# --------------------------------------------------------------------------
# dev server
# --------------------------------------------------------------------------
class DevServer(ThreadingHTTPServer):
    """Suppresses the traceback socketserver logs when a client disconnects
    mid-response. Only the logging changes: the request still fails and the
    socket is still closed."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1],
                      (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class DevHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/__build":
            body = BUILD_ID.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        with BUILD_LOCK:
            super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)

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


def watched_files():
    yield ROOT / "build.py"
    yield ROOT / "media_ext.py"
    for base in (POSTS, STATIC):
        if base.exists():
            yield from (p for p in base.rglob("*") if p.is_file())


def snapshot() -> dict:
    return {str(p): p.stat().st_mtime_ns for p in watched_files() if p.exists()}


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
            if cur == prev:
                continue
            prev = cur
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] rebuilding...")
            try:
                main()
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

