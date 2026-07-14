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

FONTS = """\
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400&display=swap">"""

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
/* critical palette, inlined: paints the canvas correctly before style.css
   arrives; must mirror the values in static/style.css */
:root {{ color-scheme: light dark; background: light-dark(#f1ecdf, #17141f); }}
:root[data-theme="light"] {{ color-scheme: light; }}
:root[data-theme="dark"]  {{ color-scheme: dark; }}
</style>
{fonts}
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


def parse_post(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    first = lines[0].strip()
    title = first.lstrip("#").strip() if first.startswith("#") else first
    body = "\n".join(lines[1:]).strip()

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

    # return {"title": title, "date": d, "slug": slug, "html": guide + rendered}
    return {"title": title, "date": d, "slug": slug, "html": rendered}


def render(title: str, root: str, body: str) -> str:
    return PAGE.format(
        title=title,
        fonts=FONTS,
        root=root,
        site_title=SITE_TITLE,
        subtitle=SITE_SUBTITLE,
        body=body,
    )


def main():
    if DOCS.exists():
        shutil.rmtree(DOCS)
    (DOCS / "posts").mkdir(parents=True)

    for f in STATIC.iterdir():
        if f.is_file():
            shutil.copy2(f, DOCS / f.name)

    posts = sorted(
        (parse_post(p) for p in POSTS.glob("*.md")),
        key=lambda p: p["date"],
        reverse=True,
    )

    items = "\n".join(
        '  <li><a href="posts/{slug}.html">{title}</a>'
        '<span class="leader"></span>'
        '<time datetime="{iso}">{nice}</time></li>'.format(
            slug=p["slug"],
            title=html.escape(p["title"]),
            iso=p["date"].isoformat(),
            nice=p["date"].strftime(DATE_FMT),
        )
        for p in posts
    )
    (DOCS / "index.html").write_text(
        render(SITE_TITLE, "", f'<ul class="toc">\n{items}\n</ul>'),
        encoding="utf-8",
    )

    for p in posts:
        body = (
            "<article>\n"
            '<header class="post">\n'
            f"<h2>{html.escape(p['title'])}</h2>\n"
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime(DATE_FMT)}</time>\n'
            "</header>\n"
            f"{p['html']}\n"
            "</article>\n"
            '<nav class="back"><a href="../index.html">&larr; all posts</a></nav>'
        )
        (DOCS / "posts" / f"{p['slug']}.html").write_text(
            render(f"{p['title']} — {SITE_TITLE}", "../", body),
            encoding="utf-8",
        )

    print(f"built {len(posts)} post(s) → {DOCS}/")


if __name__ == "__main__":
    main()

