"""Build a small plaintext blog.

Usage:
    python build.py

Layout:
    posts/    Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
    static/   files copied verbatim into the build (style.css lives here)
    site/     generated output — deploy this directory anywhere static

Post format: if the first line is an H1 ("# Title") it becomes the
post title; otherwise the first line is taken as the title verbatim.
The rest is standard Markdown (fenced code blocks and tables enabled).
If the LAST line consists only of hashtags ("#magic #geomancy"), they
become the post's tags: rendered as links on the post page, indexed
under site/tags/<tag>.html, with an overview at site/tags/index.html.

Requires: pip install markdown
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

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
STATIC = ROOT / "static"
SITE = ROOT / "site"

SITE_TITLE = "Sublunary Musings"
SITE_SUBTITLE = "philosophy, magic, and other errata"
DATE_FMT = "%B %-d, %Y"

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f1ecdf">
<meta name="theme-color" media="(prefers-color-scheme: dark)"  content="#17141f">
<title>{title}</title>
<style>
/* OS Preference */
:root {{ color-scheme: light dark; background: #f1ecdf; }}
@media (prefers-color-scheme: dark) {{ :root {{ background: #17141f; }} }}
/* User Preference */
:root[data-theme="light"] {{ color-scheme: light; background: #f1ecdf; }}
:root[data-theme="dark"]  {{ color-scheme: dark;  background: #17141f; }}
</style>
<link rel="preload" href="{root}static/fonts/EBGaramond.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{root}static/style.css">
<link rel="apple-touch-icon" sizes="180x180" href="{root}static/favicon/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="{root}static/favicon/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="{root}static/favicon/favicon-16x16.png">
<link rel="manifest" href="{root}static/favicon/site.webmanifest">
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
<button class="theme" aria-label="Toggle light/dark mode" title="Toggle light/dark mode"></button>
<header class="site">
  <h1><a href="{root}index.html">{site_title}</a></h1>
  <p>{subtitle}</p>
</header>
{body}
<script src="{root}static/theme.js"></script>
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

    options = extract_options(body_lines)   # NOTE: mutates body_lines: pops the OPTIONS line
    tags = extract_tags(body_lines)         # NOTE: mutates body_lines: pops the tag line
    body = "\n".join(body_lines).strip()
    d, slug = date_and_slug(path)
    rendered = render_markdown(body, options.get("toc", False))

    return {"title": title, "date": d, "slug": slug, "tags": tags, "html": rendered, "options": options}


def extract_options(body_lines: list[str]) -> dict:
    """Peel a trailing OPTIONS line off body_lines (in place) and return the
    list of options as a dictionary. Done BEFORE tags or markdown.

    The options line must be in a specific format at the very bottom of the file:
    <!-- [OPTIONS]: {"toc": true, "Foo": "bar"} -->

    List of valid options
        "toc":   bool   enable table of contents
        "draft": bool   draft posts are hidden in production
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


def render_markdown(body: str, toc: bool) -> str:
    """Convert post body to HTML, prepending a Contents panel when the post
    has at least three top-level (##) sections."""
    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc"])
    rendered = md.convert(body)
    if toc:
        toc_tokens = getattr(md, "toc_tokens", [])
        sections = [t for t in toc_tokens if t["level"] == 2]
        if len(sections) >= 3:
            items = "\n".join(
                f'<li><a href="#{t["id"]}">{t["name"]}</a></li>' for t in sections
            )
            guide = f'<nav class="guide"><h3>Contents</h3><ul>\n{items}\n</ul></nav>\n'
            return guide + rendered
    return rendered


# --------------------------------------------------------------------------
# html fragments
# --------------------------------------------------------------------------

def render(title: str, root: str, body: str) -> str:
    """Wrap a body fragment in the full page shell."""
    return PAGE.format(
        title=title,
        root=root,
        site_title=SITE_TITLE,
        subtitle=SITE_SUBTITLE,
        body=body,
    )


def post_list_items(posts, slug_prefix: str) -> str:
    """The dotted-leader <li> rows used by the index and by tag pages."""
    return "\n".join(
        '  <li><a href="{prefix}{slug}.html">{title}</a>'
        '<span class="leader"></span>'
        '<time datetime="{iso}">{nice}</time></li>'.format(
            prefix=slug_prefix,
            slug=p["slug"],
            title=html.escape(p["title"]),
            iso=p["date"].isoformat(),
            nice=p["date"].strftime(DATE_FMT),
        )
        for p in posts
    )


def tag_footer(tags: list[str]) -> str:
    if not tags:
        return ""
    links = " ".join(
        f'<a href="../tags/{t}.html" rel="tag">#{html.escape(t)}</a>' for t in tags
    )
    return f'<footer class="tags">{links}</footer>\n'


def group_by_tag(posts) -> dict[str, list]:
    """Map each tag to the (date-sorted) posts carrying it."""
    by_tag: dict[str, list] = {}
    for p in posts:
        for t in p["tags"]:
            by_tag.setdefault(t, []).append(p)   # posts already date-sorted
    return by_tag


# --------------------------------------------------------------------------
# writers — each renders one part of site/
# --------------------------------------------------------------------------

def write_index(posts) -> None:
    (SITE / "index.html").write_text(
        render(
            SITE_TITLE,
            "",
            '<ul class="toc">\n' + post_list_items(posts, "posts/") + "\n</ul>",
        ),
        encoding="utf-8",
    )


def write_posts(posts) -> None:
    for p in posts:
        badges = []

        if p["options"].get("draft", False):
            badges.append('<span class="draft-badge">DRAFT</span>')

        badge_wrapper = f'<div style="display: flex; gap: 0.5rem;">{"".join(badges)}</div>' if badges else ""

        body = (
            "<article>\n"
            '<header class="post">\n'
            f"<h2>{html.escape(p['title'])}{badge_wrapper}</h2>\n"
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime(DATE_FMT)}</time>\n'
            "</header>\n"
            f"{p['html']}\n"
            f"{tag_footer(p['tags'])}"
            "</article>\n"
            '<nav class="back"><a href="../index.html">&larr; all posts</a></nav>'
        )
        (SITE / "posts" / f"{p['slug']}.html").write_text(
            render(f"{p['title']} — {SITE_TITLE}", "../", body),
            encoding="utf-8",
        )


def write_tag_pages(by_tag) -> None:
    for t, tagged in by_tag.items():
        body = (
            f'<h2 class="tag-title">#{html.escape(t)}</h2>\n'
            '<ul class="toc">\n' + post_list_items(tagged, "../posts/") + "\n</ul>\n"
            '<nav class="back"><a href="index.html">&larr; all tags</a></nav>'
        )
        (SITE / "tags" / f"{t}.html").write_text(
            render(f"#{t} — {SITE_TITLE}", "../", body),
            encoding="utf-8",
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
    (SITE / "tags" / "index.html").write_text(
        render(
            f"Tags — {SITE_TITLE}",
            "../",
            f'<ul class="toc">\n{tag_items}\n</ul>\n'
            '<nav class="back"><a href="../index.html">&larr; all posts</a></nav>',
        ),
        encoding="utf-8",
    )


def write_404() -> None:
    """A site-wide 404. Served from the site root by GitHub Pages for any
    unmatched URL, so it uses ABSOLUTE asset paths (root="/") — relative
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
        render(f"Not found — {SITE_TITLE}", "/", body),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def prepare_output() -> None:
    """Wipe site/, recreate its subdirs, and copy static assets in."""
    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "posts").mkdir(parents=True)
    (SITE / "tags").mkdir()
    shutil.copytree(STATIC, SITE / STATIC.name, dirs_exist_ok=True)


def load_posts() -> list[dict]:
    """Parse every post, newest first."""
    is_local = os.getenv("ENVIRONMENT") == "LOCAL"
    valid_posts = []

    for p in POSTS.glob("*.md"):
        post = parse_post(p)
        if post["options"].get("draft", False) and not is_local:
            continue
        valid_posts.append(post)

    return sorted(valid_posts, key=lambda p: p["date"], reverse=True)


def main() -> None:
    prepare_output()
    posts = load_posts()
    by_tag = group_by_tag(posts)

    write_index(posts)
    write_posts(posts)
    write_tag_pages(by_tag)
    write_tag_index(by_tag)
    write_404()

    print(f'built {len(posts)} {"post" if len(posts) == 1 else "posts"}, {len(by_tag)} {"tag" if len(by_tag) == 1 else "tags"}: {SITE}/')


if __name__ == "__main__":
    load_dotenv()
    main()

