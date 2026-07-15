"""Build a small plaintext blog.

Usage:
    python build.py

Layout:
    posts/    Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
    static/   files copied verbatim into the build (style.css lives here)
    docs/     generated output — deploy this directory anywhere static

Post format: if the first line is an H1 ("# Title") it becomes the
post title; otherwise the first line is taken as the title verbatim.
The rest is standard Markdown (fenced code blocks and tables enabled).
If the LAST line consists only of hashtags ("#magic #geomancy"), they
become the post's tags: rendered as links on the post page, indexed
under docs/tags/<tag>.html, with an overview at docs/tags/index.html.

Requires: pip install markdown
"""

import html
import re
import shutil

import markdown
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
STATIC = ROOT / "static"
DOCS = ROOT / "docs"

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
<link rel="preload" href="{root}fonts/EBGaramond.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{root}style.css">
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
</script>
</body>
</html>
"""

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
TAG_LINE_RE = re.compile(r"^\s*#[\w-]+(?:\s+#[\w-]+)*\s*$")     # a line consisting only of hashtags: "#placebo  #tree-of-life #philosophy"


def parse_post(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body_lines = lines[1:]

    # peel a trailing hashtag line off BEFORE markdown sees it (python-markdown
    # would happily read "#magic" as an <h1>)
    tags: list[str] = []
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if body_lines and TAG_LINE_RE.match(body_lines[-1]):
        tags = sorted({t.lstrip("#").lower() for t in body_lines.pop().split()})

    body = "\n".join(body_lines).strip()

    m = DATE_RE.match(path.stem)
    if m:
        d = date.fromisoformat(m.group(1))
        slug = path.stem[len(m.group(0)):]
    else:
        d = datetime.fromtimestamp(path.stat().st_mtime).date()
        slug = path.stem

    md = markdown.Markdown(extensions=["fenced_code", "tables", "toc"])
    rendered = md.convert(body)

    toc_tokens = getattr(md, "toc_tokens", [])
    sections = [t for t in toc_tokens if t["level"] == 2]
    guide = ""
    if len(sections) >= 3:
        items = "\n".join(
            f'<li><a href="#{t["id"]}">{t["name"]}</a></li>' for t in sections
        )
        guide = f'<nav class="guide"><h3>Contents</h3><ul>\n{items}\n</ul></nav>\n'

    # return {"title": title, "date": d, "slug": slug, "tags": tags, "html": guide + rendered}
    return {"title": title, "date": d, "slug": slug, "tags": tags, "html": rendered}


def render(title: str, root: str, body: str) -> str:
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


def main():
    if DOCS.exists():
        shutil.rmtree(DOCS)
    (DOCS / "posts").mkdir(parents=True)
    (DOCS / "tags").mkdir()

    # copy static assets (style.css, fonts/, ...) verbatim, recursively
    shutil.copytree(STATIC, DOCS, dirs_exist_ok=True)

    posts = sorted(
        (
            parse_post(p)
            for p in POSTS.glob("*.md")
            # if not p.name.endswith("WIP.md")
        ),
        key=lambda p: p["date"],
        reverse=True,
    )

    # ---- index ----
    (DOCS / "index.html").write_text(
        render(
            SITE_TITLE,
            "",
            '<ul class="toc">\n' + post_list_items(posts, "posts/") + "\n</ul>",
        ),
        encoding="utf-8",
    )

    # ---- posts ----
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
        (DOCS / "posts" / f"{p['slug']}.html").write_text(
            render(f"{p['title']} — {SITE_TITLE}", "../", body),
            encoding="utf-8",
        )

    # ---- tag pages ----
    by_tag: dict[str, list] = {}
    for p in posts:
        for t in p["tags"]:
            by_tag.setdefault(t, []).append(p)  # posts already date-sorted

    for t, tagged in by_tag.items():
        body = (
            f'<h2 class="tag-title">#{html.escape(t)}</h2>\n'
            '<ul class="toc">\n' + post_list_items(tagged, "../posts/") + "\n</ul>\n"
            '<nav class="back"><a href="index.html">&larr; all tags</a></nav>'
        )
        (DOCS / "tags" / f"{t}.html").write_text(
            render(f"#{t} — {SITE_TITLE}", "../", body),
            encoding="utf-8",
        )

    # ---- tag directory ----
    tag_items = "\n".join(
        '  <li><a href="{t}.html">#{t}</a>'
        '<span class="leader"></span>'
        '<span class="count">{n} post{s}</span></li>'.format(
            t=html.escape(t), n=len(ps), s="" if len(ps) == 1 else "s"
        )
        for t, ps in sorted(by_tag.items())
    )
    (DOCS / "tags" / "index.html").write_text(
        render(
            f"Tags — {SITE_TITLE}",
            "../",
            f'<ul class="toc">\n{tag_items}\n</ul>\n'
            '<nav class="back"><a href="../index.html">&larr; all posts</a></nav>',
        ),
        encoding="utf-8",
    )

    print(f"built {len(posts)} post(s), {len(by_tag)} tag(s) → {DOCS}/")


if __name__ == "__main__":
    main()

