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
<title>{title}</title>
<style>
/* critical palette, inlined: paints the canvas correctly before style.css
   arrives; must mirror the values in static/style.css */
:root {{ color-scheme: light dark; background: light-dark(#f1ecdf, #17141f); }}
:root[data-theme="light"] {{ color-scheme: light; }}
:root[data-theme="dark"]  {{ color-scheme: dark; }}
</style>
<link rel="preload" href="{root}static/fonts/EBGaramond.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{root}static/style.css">
<link rel="apple-touch-icon" sizes="180x180" href="{root}static/favicon/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="{root}static/favicon/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="{root}static/favicon/favicon-16x16.png">
<link rel="manifest" href="/site.webmanifest">
<script>
/* restore saved choice before first paint to avoid a flash;
   if the save now matches the OS, it's redundant — drop it and follow the OS */
try {{
  var t = localStorage.getItem("theme");
  var os = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
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
<script>
var mq = matchMedia("(prefers-color-scheme: dark)");

/* run a mutation inside a view transition (cross-fade) when supported;
   otherwise fade with a temporary transition class */
function withTransition(fn) {{
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {{
    fn();
  }} else if (document.startViewTransition) {{
    document.startViewTransition(fn);
  }} else {{
    var root = document.documentElement;
    root.classList.add("theme-fade");
    fn();
    setTimeout(function () {{ root.classList.remove("theme-fade"); }}, 400);
  }}
}}

document.querySelector("button.theme").addEventListener("click", function () {{
  withTransition(function () {{
    var root = document.documentElement;
    var os = mq.matches ? "dark" : "light";
    var current = root.dataset.theme || os;
    var next = current === "dark" ? "light" : "dark";
    if (next === os) {{
      /* choice matches the OS — drop the override and follow the OS again */
      delete root.dataset.theme;
      try {{ localStorage.removeItem("theme"); }} catch (e) {{}}
    }} else {{
      root.dataset.theme = next;
      try {{ localStorage.setItem("theme", next); }} catch (e) {{}}
    }}
  }});
}});

/* if the OS theme changes while the page is open and now agrees with the
   saved override, the override is redundant — drop it so later OS changes
   are followed */
mq.addEventListener("change", function (e) {{
  var os = e.matches ? "dark" : "light";
  if (document.documentElement.dataset.theme === os) {{
    delete document.documentElement.dataset.theme;
    try {{ localStorage.removeItem("theme"); }} catch (err) {{}}
  }}
}});

/* a link back to the page you're already on: nothing to animate, so a
   cross-fade of the page into itself only produces shimmer — skip it */
window.addEventListener("pageswap", function (e) {{
  if (e.viewTransition && e.activation &&
      e.activation.from && e.activation.entry &&
      e.activation.from.url === e.activation.entry.url) {{
    e.viewTransition.skipTransition();
  }}
}});
</script>
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
    e.g. <!-- [OPTIONS]: {"toc": true, "Foo": "bar"} -->
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
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if body_lines and TAG_LINE_RE.match(body_lines[-1]):
        return sorted({t.lstrip("#").lower() for t in body_lines.pop().split()})
    return []


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
        body = (
            "<article>\n"
            '<header class="post">\n'
            f"<h2>{html.escape(p['title'])}</h2>\n"
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
        '<article>\n'
        '<header class="post">\n'
        '<h2>Lost in the sublunary</h2>\n'
        '</header>\n'
        '<p>There is no page at this address. The path you followed may be '
        'broken, or the writing may have been unmade.</p>\n'
        '<p><a href="/index.html">Return to the index</a>, or '
        '<a href="/tags/index.html">wander the tags</a>.</p>\n'
        '</article>'
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
    # copy static assets (style.css, fonts/, ...) verbatim, recursively
    shutil.copytree(STATIC, SITE / STATIC.name, dirs_exist_ok=True)


def load_posts() -> list[dict]:
    """Parse every post, newest first."""
    return sorted(
        (
            parse_post(p)
            for p in POSTS.glob("*.md")
            if not p.name.endswith("WIP.md") or os.getenv("ENVIRONMENT") == "LOCAL"
        ),
        key=lambda p: p["date"],
        reverse=True,
    )


def main() -> None:
    prepare_output()
    posts = load_posts()
    by_tag = group_by_tag(posts)

    write_index(posts)
    write_posts(posts)
    write_tag_pages(by_tag)
    write_tag_index(by_tag)
    write_404()

    print(f"built {len(posts)} post(s), {len(by_tag)} tag(s) → {SITE}/")


if __name__ == "__main__":
    load_dotenv()
    main()

