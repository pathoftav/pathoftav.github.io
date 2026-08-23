# Path of Tav

A static site generator for *Sublunary Musings*. Markdown in, plain HTML out —
no runtime, no framework, deployable anywhere that serves files.

```sh
python build.py            # build site/
python build.py --serve    # build, serve, and rebuild on change
python build.py --serve --port 3000
```

## Layout

```
posts/                          Markdown posts (YYYY-MM-DD-slug.md; date prefix optional)
posts/old/                      earlier versions of a post, same slug with different date
static/                         copied verbatim into the build
static/components/<slug>/       HTML fragments spliced in with <include>
static/media/<slug>/            images and video for one post
static/<type>/<slug>/<date>/    per-version snapshots for an old post version
site/                           generated output — deploy this
```

## Environment

Create `.env` at the root:

```sh
ENVIRONMENT=LOCAL           # local dev build; omit in production
SEALED_PASSWORD=secret      # the phrase that opens sealed posts
```

`ENVIRONMENT=LOCAL` changes four things: drafts are built, CSS and JS are not
minified, page links carry `.html` so `site/` can be browsed straight off the
filesystem, and the seal salt is fixed rather than random so a rebuild doesn't
invalidate an open session.

`SEALED_PASSWORD` is only read when a post is `sealed`. A sealed post with no
password set is a fatal build error, not a warning — publishing the body in the
clear is worse than not publishing.

## Post format

The first line is the title, with a leading `#` stripped if present. If the last
line is nothing but hashtags it becomes the post's tags. If the line below that
is an `[OPTIONS]` comment it configures the post. Everything between is Markdown.

```markdown
# Post Title

Body text.

#magic #philosophy

<!-- [OPTIONS]: { "math": true, "toc": true } -->
```

The date and slug come from the filename — a `YYYY-MM-DD-` prefix wins, and
without one the file's mtime is used and the whole stem is the slug.

### Options

| Option | Type | Effect |
| --- | --- | --- |
| `pin` | int | Pin to the top of listings; higher ranks higher |
| `draft` | bool | Skipped entirely in production builds |
| `unlisted` | bool | Omitted from the index and tag pages; still reachable by link |
| `noindex` | bool | Emit `robots: noindex` |
| `dropcap` | bool | Default true; set false to suppress the drop cap |
| `toc` | bool | Prepend a Contents panel |
| `math` | bool | Load KaTeX and render LaTeX |
| `locked` | str | ISO date (optionally with time) before which the body is sealed |
| `sealed` | bool | Encrypt the body against `SEALED_PASSWORD` |

`toc` only produces a panel when the post has at least three `##` sections —
below that a Contents list is noise, so it is skipped silently.

A `locked` timestamp with no timezone is read as `America/New_York`. An
unparseable one warns and publishes unlocked.

`sealed` implies `"unlisted": true` unless you set it false explicitly, in which
case the post appears in the index. If a SEALED post is unlisted (default), the
unlisted badge is hidden. Setting both `locked` and `sealed` warns and keeps the
seal — the phrase is the stronger claim, so the clock is dropped.

## Path tokens

Two placeholders are substituted during the build.

`{site_root}` becomes the path back to the site root, resolved when a page is
written because it depends on that page's depth — `.` for the index, `..` for a
post, `../../..` for an old-version page, and `/` in production or under
`--serve`. Use it in any URL that must work from any page.

`{slug}` becomes the post's slug, substituted at parse time. It works anywhere in
the body, including inside `<include>` paths and spliced fragments.

```markdown
[the sigil]({site_root}/static/media/{slug}/sigil.png)
```

## Include tag

Splices an HTML fragment into the post before Markdown runs, so a figure can live
in its own file instead of several hundred lines inline.

```html
<include source="vecfig.html"
    prompt="what is an LLM?"
    reply="a labyrinth of vectors collapsing into the next token">
```

The tag stands alone on its own line, though it may wrap across several. A bare
`source` resolves under `static/components/<slug>/`; anything containing a slash
resolves against the project root. `{site_root}/` and `{slug}` are substituted
first, so both spellings below reach the same file:

```html
<include source="vecfig.html">
<include source="{site_root}/static/components/{slug}/vecfig.html">
```

Every attribute other than `source` fills a matching `{{slot}}` in the fragment.
Double braces, so single-brace `{site_root}` survives for the page writer to
resolve later:

```html
<div class="vecfig" data-prompt="{{prompt}}" data-reply="{{reply}}">
```

Slot values are escaped for attribute position. That is the only context they are
safe in — script bodies are not HTML-decoded, so a fragment that wants a value in
JavaScript should read it back with `getAttribute` rather than interpolate a slot
into the script.

Expansion runs after options and tags are peeled, so a trailing `[OPTIONS]` or
hashtag line inside a fragment cannot reconfigure the post that pulled it in, and
before Markdown, so a fragment parses exactly as if it had been pasted in.
Includes do not nest. A missing file or an unfilled slot fails the build.

A version out of `posts/old/` looks first in `static/components/<slug>/<date>/`
and falls back to the shared directory, so a figure can be frozen for the
versions that need it. See [Old versions](#old-versions).

## Media

Images and video are written with ordinary image syntax; options ride in the
query string and never reach the browser.

```markdown
![alt](waterfall.webm?w=50&align=center "Crabtree Falls, October")
![alt](sigil.png?dark=sigil-dark.png&link=1)
```

A bare filename resolves under `{site_root}/static/media/<slug>/`, so a post's
own media needs no path. Anything containing a slash is used as given.

| Parameter | Effect |
| --- | --- |
| `align=<dir>` | `left`, `right`, `center`, `left-inline`, `right-inline` |
| `w=<1-100>` | Width as a percentage of the containing block |
| `mw=<1-100>` | Max-width as a percentage; a cap rather than a size |
| `h=<px>` | Height in pixels |
| `ar=<W>x<H>` | Aspect ratio; reserves the box and prevents layout shift |
| `dark=<path>` | Images only: alternate source for the dark theme |
| `link=<url>` | Wrap in an anchor; `link=1` links to the media itself |
| `loop=1` | Silent decorative loop: autoplays, repeats, no controls |

A `.webm` or `.mp4` becomes a `<video>`. Without `loop=1` it is an ordinary
player with controls and sound that the reader starts.

Sizing is natural by default: media renders at its own dimensions and is capped
at the column width. `w=` forces it wider than it really is; `mw=` caps it
narrower. The `-inline` alignments float the media and cap it at 45% unless `mw=`
says otherwise.

A title string becomes a visible caption. The container takes over the width and
alignment so the caption tracks the media rather than the column — a `<figure>`
when the media is alone in its paragraph, a `<span>` when it sits mid-sentence,
since `<figure>` would otherwise split the surrounding text.

`dark=` follows the site's own light/dark toggle rather than the OS setting, which
is why it is emitted as a `--dark` custom property and swapped in CSS instead of
using `<picture>`. Bare `dark=` and `link=` filenames resolve next to the main
image, after the main path itself has been resolved.

An image with no parameters, no caption and no dark variant is left completely
alone apart from resolving its path.

Bare filenames in an old version resolve the same way as `<include>` sources:
`static/media/<slug>/<date>/` first, the shared directory otherwise.

## Markdown extensions

Enabled for every post: `tables`, `fenced_code`, `md_in_html`, `toc`,
`footnotes`, `nl2br`, `pymdownx.arithmatex` (generic mode), and the custom media
extension. Links inside footnotes are rewritten to open in a new tab.

## Locked and sealed posts

Both replace the body with ciphertext and a notice, so the text is absent from
view-source, unfindable with Ctrl+F, and uncaptured by crawlers and archives.

**`locked`** is deterrence, not access control. The key derives from the slug and
the unlock time, both of which ship with the page, so a determined reader can
open it early. It buys absence from the served HTML until the clock runs out, at
which point `lock.js` decrypts in place — including the badge on listing pages,
which clears itself without waiting for a rebuild.

**`sealed`** is real access control. The body is AES-GCM encrypted under a key
stretched from the reader's phrase through two PBKDF2 stages of 310,000 rounds
each, so a dictionary attack pays both. The salt is fresh per post per build. The
page shows only a field of runes: no prompt, no input, no hint that typing does
anything. There is no separate correctness check by design — the GCM tag is the
check, so a wrong phrase is indistinguishable from noise.

Because the phrase salt rotates per build, a handle lifted from a reader's
session opens that build and no other.

Sealed and locked bodies are encrypted *after* `{site_root}` is resolved, so links
inside them still work. One consequence worth knowing: an `<include>` fragment
under `static/` is also published as a standalone file at a predictable URL, so it
is served in the clear regardless of the seal. Fine for a figure; keep prose you
mean to hide out of `static/`.

## Old versions

Drop an earlier copy in `posts/old/` with the same slug and it is published under
`site/posts/old/<slug>/`, indexed by date, noindexed, badged OLD, and linked from
the live post's footer.

Old versions run through the same parser and share the live post's slug, so a
bare `<include>` source or media filename would otherwise follow those assets as
they stand today — rewrite a figure and every archived version silently shows the
new one.

To prevent that, a version prefers a snapshot named for its own date and falls
back to the shared directory when there isn't one:

## Dev server

`--serve` builds, serves `site/` on `127.0.0.1:8000`, and polls for changes every
0.4s across `build.py`, `media_ext.py`, `posts/` and `static/`. Pages carry a
small poller that hits `/__build` and reloads when the build id changes.

URLs are extensionless while serving, matching production. A build error prints
the traceback and keeps the server up, so you can fix the post and save again. A
build lock keeps requests from landing while `site/` is mid-rebuild and briefly
empty.

## Production notes

A non-`LOCAL` build minifies `static/styles/*.css` and `static/scripts/*.js`.
Both globs are shallow — a script in a subdirectory such as
`static/scripts/figures/` ships unminified.

`site/404.html` uses absolute asset paths, since GitHub Pages serves it for any
unmatched URL at any depth.

