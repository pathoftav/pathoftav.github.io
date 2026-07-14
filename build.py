#!/usr/bin/env python3
"""Build a tiny plaintext blog.

Usage:
    python3 build.py

Layout:
    posts/    plain .txt posts (YYYY-MM-DD-slug.txt; first line = title)
    static/   files copied verbatim into the build (style.css lives here)
    docs/     generated output — deploy this directory anywhere static

Post format: first line is the title; everything after the first blank
line is the body. Paragraphs are separated by blank lines. No markup.
"""

import html
import re
import shutil
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
POSTS = ROOT / "posts"
STATIC = ROOT / "static"
DOCS = ROOT / "docs"

SITE_TITLE = "Sublunary Musings"  # change me
SITE_SUBTITLE = "philosophy, magic, and other errata"

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

document.querySelector("button.theme").addEventListener("click", function () {{
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
    title = lines[0].strip()
    body = "\n".join(lines[1:]).strip()

    m = DATE_RE.match(path.stem)
    if m:
        d = date.fromisoformat(m.group(1))
        slug = path.stem[len(m.group(0)):]
    else:
        d = datetime.fromtimestamp(path.stat().st_mtime).date()
        slug = path.stem

    paragraphs = [
        "<p>{}</p>".format(html.escape(p.strip()).replace("\n", "<br>"))
        for p in re.split(r"\n\s*\n", body)
        if p.strip()
    ]
    return {"title": title, "date": d, "slug": slug, "html": "\n".join(paragraphs)}


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

    # copy static assets (style.css and anything else) verbatim
    for f in STATIC.iterdir():
        if f.is_file():
            shutil.copy2(f, DOCS / f.name)

    posts = sorted(
        (parse_post(p) for p in POSTS.glob("*.txt")),
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
            nice=p["date"].strftime("%-d %B %Y"),
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
            f"<h2>{html.escape(p['title'])}</h2>\n"
            f'<time datetime="{p["date"].isoformat()}">{p["date"].strftime("%-d %B %Y")}</time>\n'
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

