"""Build a small plaintext blog.

Usage:
    python build.py

Layout:
    posts/    Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
    static/   files copied verbatim into the build (style.css lives here)
    site/     generated output — deploy this directory anywhere static

Post format: if the first line is an H1 ("# Title") it becomes the
post title; otherwise the first line is taken as the title verbatim.
The rest is standard Markdown (with extensions enabled).
If the LAST line consists only of hashtags ("#magic #geomancy"), they
become the post's tags: rendered as links on the post page, indexed
under site/tags/<tag>.html, with an overview at site/tags/index.html.
"""

import html
import json
import os
import re
import shutil

from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
import markdown


load_dotenv()
IS_LOCAL = os.getenv("ENVIRONMENT") == "LOCAL"

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
OLD = POSTS / "old"
STATIC = ROOT / "static"
SITE = ROOT / "site"

SITE_TITLE = "Sublunary Musings"
SITE_SUBTITLE = "philosophy, magic, and other errata"
DATE_FMT = "%B %-d, %Y"

EXTRA_NOINDEX = '<meta name="robots" content="noindex">'
EXTRA_MATH = (
    '<link rel="stylesheet" href="{site_root}static/vendor/katex/katex.min.css">\n'
    '<script defer src="{site_root}static/vendor/katex/katex.min.js"></script>\n'
    '<script defer src="{site_root}static/vendor/katex/contrib/auto-render.min.js"></script>\n'
    '<script defer src="{site_root}static/math.js"></script>'
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
<link rel="preload" href="{site_root}static/fonts/EBGaramond.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{site_root}static/style.css">
<link rel="apple-touch-icon" sizes="180x180" href="{site_root}static/favicon/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="{site_root}static/favicon/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="{site_root}static/favicon/favicon-16x16.png">
<link rel="manifest" href="{site_root}static/favicon/site.webmanifest">
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
  <h1><a href="{site_root}index.html">{site_title}</a></h1>
  <p>{site_subtitle}</p>
</header>
{post_body}
<script src="{site_root}static/theme.js"></script>
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
    if "{site_root}" in text:
        raise ValueError(f"{path.name}: contains literal '{{site_root}}' — rename or escape it")
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body_lines = lines[1:]

    options = extract_options(body_lines)   # NOTE: mutates body_lines: pops the OPTIONS line
    tags = extract_tags(body_lines)         # NOTE: mutates body_lines: pops the tag line
    body = "\n".join(body_lines).strip()
    d, slug = date_and_slug(path)
    rendered = render_markdown(body, options)

    return {"title": title, "date": d, "slug": slug, "tags": tags, "html": rendered, "options": options}


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
    """
    options = {}
    if body_lines:
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
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
            "pymdownx.arithmatex"
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

    return rendered


# --------------------------------------------------------------------------
# html fragments
# --------------------------------------------------------------------------

def render(title: str, root: str, body: str, extras: list[str] | None = None) -> str:
    """Wrap a body fragment in the full page shell."""
    if not IS_LOCAL:
        root = "/"
    ext = "\n".join(h.format(site_root=root) for h in (extras or ["<!-- no extras -->"]))
    body = body.replace("{site_root}", root)
    return PAGE.format(
        site_root=root,
        site_title=SITE_TITLE,
        site_subtitle=SITE_SUBTITLE,
        post_title=title,
        post_body=body,
        head_extras=ext,
    )


def post_list_items(posts, slug_prefix: str) -> str:
    """The dotted-leader <li> rows used by the index and by tag pages."""
    return "\n".join(
        '<li><a href="{prefix}{slug}.html">{title}</a>'
        '<span class="leader"></span>'
        '{badge}'
        '<time datetime="{iso}">{nice}</time></li>'.format(
            prefix=slug_prefix,
            slug=p["slug"],
            title=html.escape(p["title"]),
            iso=p["date"].isoformat(),
            nice=p["date"].strftime(DATE_FMT),
            badge='<span class="badge badge-pin">PINNED</span>&nbsp;' if p["options"].get("pin") else "",
        )
        for p in posts
    )



def tag_footer(tags: list[str]) -> str:
    if not tags:
        return ""
    links = " ".join(
        f'<a href="{{site_root}}tags/{t}.html" rel="tag">#{html.escape(t)}</a>'
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
    extras = []
    if post["options"].get("noindex", False):
        extras.append(EXTRA_NOINDEX)
    if post["options"].get("math", False):
        extras.append(EXTRA_MATH)
    return extras


def post_article_classes(post: dict) -> list[str]:
    """CSS classes for the <article> wrapper"""
    classes = []
    if not post["options"].get("dropcap", True) : classes.append("no-dropcap")
    return classes


def post_badges(post: dict) -> list[str]:
    """Badges displayed on posts"""
    badges = []
    if post["options"].get("old", False)      : badges.append('<span class="badge badge-old">OLD</span>')
    if post["options"].get("draft", False)    : badges.append('<span class="badge badge-draft">DRAFT</span>')
    if post["options"].get("unlisted", False) : badges.append('<span class="badge badge-unlisted">UNLISTED</span>')
    if post["options"].get("pin", 0) > 0      : badges.append('<span class="badge badge-pin">PINNED</span>')
    return badges


def render_article(post: dict, *, footer: str = "") -> str:
    """The <article> block shared by post pages and old version pages:
    header (title + badges), date, rendered HTML, tag footer, then a caller
    -supplied footer nav appended after </article>."""
    classes = post_article_classes(post)
    class_attr = f' class="{" ".join(classes)}"' if classes else ""

    badges = post_badges(post)
    badge_wrapper = (f'<div class="post-badges">{"".join(badges)}</div>' if badges else "")

    return (
        f'<article{class_attr}>\n'
        '<header class="post">\n'
        f'<h2>{html.escape(post["title"])}{badge_wrapper}</h2>\n'
        f'<time datetime="{post["date"].isoformat()}">{post["date"].strftime(DATE_FMT)}</time>\n'
        '</header>\n'
        f'{post["html"]}\n'
        f'{tag_footer(post["tags"])}'
        '</article>\n'
        f'{footer}'
    )


# --------------------------------------------------------------------------
# writers — each renders one part of site/
# --------------------------------------------------------------------------

def write_page(dest: Path, title: str, body: str, extras=None) -> None:
    """Render body into the page shell and write it to dest (under SITE).
    root is derived from dest's depth locally, and is "/" in production."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if IS_LOCAL:
        depth = len(dest.relative_to(SITE).parts) - 1
        root = "../" * depth
    else:
        root = "/"
    dest.write_text(render(title, root, body, extras), encoding="utf-8")


def write_index(posts) -> None:
    body = '<ul class="toc">\n' + post_list_items(posts, "posts/") + "\n</ul>"
    write_page(
        SITE / "index.html",
        SITE_TITLE,
        body
    )


def write_posts(posts) -> None:
    for p in posts:
        back = '<a href="../index.html">&larr; all posts</a>'
        hist = ""
        if p["history"]:
            n = len(p["history"])
            hist = (f'<a href="{{site_root}}posts/old/{p["slug"]}/index.html">'
                    f'{n} earlier version{"" if n == 1 else "s"} &rarr;</a>')
        footer = f'<nav class="post-foot">{back}{hist}</nav>'

        body = render_article(p, footer=footer)
        write_page(
            SITE / "posts" / f"{p['slug']}.html",
            f"{p['title']} — {SITE_TITLE}",
            body,
            extras=post_head_extras(p)
        )


def write_old_posts(posts: list[dict], old_posts: dict[str, list[dict]]) -> None:
    """For each slug with old versions, render every old version and an
    index linking to them by date. Version pages mirror regular post pages
    (via render_article) but carry an OLD badge, are always noindexed,
    and link back to the version list instead of the post index."""
    live = {p["slug"]: p for p in posts}
    for slug, versions in old_posts.items():
        old_dir = SITE / "posts" / "old" / slug

        # prefer the live post's current title; fall back to the latest old post
        canonical_title = live[slug]["title"] if slug in live else versions[0]["title"]

        # each old version as its own page
        for v in versions:
            stamp = v["date"].isoformat()
            footer = '<nav class="back"><a href="index.html">&larr; all versions</a></nav>'
            body = render_article(v, footer=footer)

            extras = post_head_extras(v)
            if EXTRA_NOINDEX not in extras:
                extras.append(EXTRA_NOINDEX)

            write_page(
                old_dir / f"{stamp}.html",
                f'{v["title"]} ({stamp}) — {SITE_TITLE}',
                body,
                extras=extras
            )

        # index of versions for this slug
        items = "\n".join(
            '  <li><a href="{stamp}.html">{nice}</a></li>'.format(
                stamp=v["date"].isoformat(), nice=v["date"].strftime(DATE_FMT)
            )
            for v in versions
        )
        foot = ""
        if slug in live:
            foot = (f'<nav class="post-foot">'
                    f'<a href="../../{slug}.html">&larr; current version</a>'
                    f'</nav>')

        old_badge = (f'<div class="post-badges"><span class="badge badge-old">OLD</span></div>')
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
            '<nav class="back"><a href="index.html">&larr; all tags</a></nav>'
        )
        write_page(
            SITE / "tags" / f"{t}.html",
            f"#{t} — {SITE_TITLE}",
            body,
            extras=[EXTRA_NOINDEX]
        )


def write_tag_index(by_tag) -> None:
    tag_items = "\n".join(
        '  <li><a href="{t}.html">#{t}</a>'
        '<span class="leader"></span>'
        '<span class="count">{n} post{s}</span></li>'.format(
            t=html.escape(t), n=len(ps), s="" if len(ps) == 1 else "s"
        )
        for t, ps in sorted(by_tag.items())
    )
    body = (f'<ul class="toc">\n{tag_items}\n</ul>\n'
        '<nav class="back"><a href="../index.html">&larr; all posts</a></nav>')
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
        '<p><a href="/index.html">Return to the index</a>, or <a href="/tags/index.html">wander the tags</a>.</p>\n'
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

    old = sum(len(v) for v in old_posts.values())
    slugs_with_history = len(old_posts)
    print(
        f'built {len(posts)} {"post" if len(posts) == 1 else "posts"}, '
        f'{old} old {"version" if old == 1 else "versions"} '
        f'across {slugs_with_history} {"slug" if slugs_with_history == 1 else "slugs"}, '
        f'{len(by_tag)} {"tag" if len(by_tag) == 1 else "tags"}: {SITE}/'
    )


if __name__ == "__main__":
    main()

