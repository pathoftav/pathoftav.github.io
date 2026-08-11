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

import base64
import hashlib
import html
import json
import os
import re
import shutil

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
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
INDEX = "index.html" if IS_LOCAL else ""
EXT = ".html" if IS_LOCAL else ""

SITE_TITLE = "Sublunary Musings"
SITE_SUBTITLE = "philosophy, magic, and other errata"
DATE_FMT = "%B %-d, %Y"

SITE_TZ = ZoneInfo("US/Eastern")
UNLOCK_FMT = "%B %-d, %Y at %-I:%M %p %Z"
BUILD_TIME = datetime.now(timezone.utc)

EXTRA_NOINDEX = '<meta name="robots" content="noindex">'
EXTRA_LOCK = (
    '<link rel="stylesheet" href="{site_root}/static/styles/locked.css">\n'
    '<style>html.lock-pending [data-unlock] {{ visibility: hidden; }}</style>\n'
    '<script>document.documentElement.classList.add("lock-pending");</script>\n'
    '<script defer src="{site_root}/static/scripts/lock.js"></script>'
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
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body_lines = lines[1:]

    options = extract_options(body_lines)       # NOTE: mutates body_lines: pops the OPTIONS line
    tags = extract_tags(body_lines)             # NOTE: mutates body_lines: pops the tag line
    body = "\n".join(body_lines).strip()
    d, slug = date_and_slug(path)
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
# html fragments
# --------------------------------------------------------------------------

def render(title: str, root: str, body: str, extras: list[str] | None = None) -> str:
    """Wrap a body fragment in the full page shell."""
    root = root.rstrip("/")
    ext = "\n".join(h.format(site_root=root) for h in (extras or ["<!-- no extras -->"]))
    body = body.replace("{site_root}", root)
    return PAGE.format(
        index_file=INDEX,
        site_root=root,
        site_title=SITE_TITLE,
        site_subtitle=SITE_SUBTITLE,
        post_title=title,
        post_body=body,
        head_extras=ext,
    )


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
    if opts.get("math", False):
        extras.append(EXTRA_MATH)
    return list(dict.fromkeys(extras))


def post_article_classes(post: dict) -> list[str]:
    """CSS classes for the <article> wrapper"""
    classes = []
    if not post["options"].get("dropcap", True) : classes.append("no-dropcap")
    if post["options"].get("is_locked", False)  : classes.append("locked")
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
    return badges


def render_article(post: dict, *, footer: str = "", root: str = "") -> str:
    """The <article> block shared by post pages and old version pages:
    header (title + badges), date, rendered HTML, tag footer, then a caller
    -supplied footer nav appended after </article>.

    A sealed post emits its unlock notice and an inert base64 payload in
    place of the body. <script> with a non-JS type is neither executed nor
    parsed as markup, so nothing of the post can render before lock.js
    decrypts it."""
    classes = post_article_classes(post)
    class_attr = f' class="{" ".join(classes)}"' if classes else ""

    locked = post["options"].get("is_locked", False)
    lock_attr = (
        f' data-unlock="{iso_utc(post["options"]["unlock_time"])}" data-slug="{post["slug"]}"'
        if locked else ""
    )

    badges = post_badges(post)
    badge_wrapper = (f'<div class="post-badges">{"".join(badges)}</div>' if badges else "")

    content = f'{post["html"]}\n{tag_footer(post["tags"])}'
    if locked:
        # tags stay inside the payload: they leak the subject matter.
        # {site_root} must be resolved BEFORE sealing — render() substitutes
        # it on the body string, by which point a sealed body is base64 and
        # every link inside it would survive unreplaced.
        content = content.replace("{site_root}", root.rstrip("/"))
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
    if not IS_LOCAL:
        return "/"
    depth = len(dest.relative_to(SITE).parts) - 1
    return "../" * depth if depth else "."


def write_page(dest: Path, title: str, body: str, extras=None) -> None:
    """Render body into the page shell and write it to dest (under SITE)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(title, root_for(dest), body, extras), encoding="utf-8")


def listing_extras(posts) -> list[str]:
    """Any listing holding a sealed row needs lock.js, so the LOCKED badge
    clears itself when the moment arrives."""
    return [EXTRA_LOCK] if any(p["options"].get("is_locked", False) for p in posts) else []


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

def prepare_output() -> None:
    """Wipe site/ and copy static assets in."""
    if SITE.exists():
        shutil.rmtree(SITE)
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

    sealed = sum(1 for p in posts if p["options"].get("is_locked", False))
    old = sum(len(v) for v in old_posts.values())
    slugs_with_history = len(old_posts)
    print(
        f'built {len(posts)} {"post" if len(posts) == 1 else "posts"}'
        f'{f" ({sealed} sealed)" if sealed else ""}, '
        f'{old} old {"version" if old == 1 else "versions"} '
        f'across {slugs_with_history} {"slug" if slugs_with_history == 1 else "slugs"}, '
        f'{len(by_tag)} {"tag" if len(by_tag) == 1 else "tags"}: {SITE}/'
    )


if __name__ == "__main__":
    main()

