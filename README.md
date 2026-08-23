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
site/static/sealed/             encrypted media belonging to shut-in posts
```

`static/components/` is a build-time input and is not copied into `site/` — a
fragment's contents already live in the post that spliced them in.

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
inside them still work.

### Media in a shut-in post

Encrypting a body is pointless if its images stay at a guessable URL beside the
ciphertext — crawlable precisely because nothing links to them. So they are
encrypted too.

Each referenced file under `static/media/` is AES-GCM encrypted into
`site/static/sealed/<hash>.enc` under a random content key, and that key is
written into the body *before* the body itself is encrypted. Opening the post
therefore yields the key; not opening it yields nothing. The `.enc` filename is a
hash of the key and path, so it gives up neither the post nor the original
filename, and it rotates whenever the key does.

In the page, `src` becomes a transparent placeholder and the real URL moves to
`data-enc`; a `?dark=` variant moves to `data-enc-dark` and its `--dark`
declaration is stripped, since one pointing at a deleted file would break on the
first theme toggle. `decrypt-media.js` fetches each `.enc`, decrypts it, and
hands back a `blob:` URL — including putting `--dark` back, so
`content: var(--dark)` keeps working. `lock.js` and `sealed.js` call it while the
markup is still detached, so images arrive already loaded and nothing flashes a
placeholder.

The plaintext original is then deleted from `site/`, and the build reports what
it withdrew:

```
sealed 3 media files, withdrew 2 from the clear
  still public (an open post uses it): static/media/secret-post/sigil.png
```

A file an open page also references is never deleted — that would break the open
page. Any gap between the two counts is named, so it is worth reading.

Locked and sealed posts never share a content key. A locked post's body key
derives from values that ship with the page, so a shared content key would let
anyone who can open a locked post reach a sealed one's media. Old versions do
share, since they share the slug — one set of `.enc` files serves the live post
and its whole history.

Two things to watch:

- **Withdrawal is per file, not per directory.** An image sitting in
  `static/media/<slug>/` that no post references is never encrypted and never
  withdrawn, and the directory name is guessable from the slug. Run
  `find site/static/media -type f` after a build to see what is still exposed.
- **An unsealed old version keeps the live post's media public.** Old versions
  share the media directory, so if the current post is sealed but an archived
  one is not, the archived page holds those files in the clear. The "still
  public" line above catches it.

Sealing covers the trees named in `ASSET_ROOTS`, `static/media/` by default. Add
a directory there if a post keeps assets elsewhere and they should travel with
it.

A `<video>` is a poor fit: `blob:` URLs cannot be range-requested and AES-GCM
authenticates the whole message, so the file downloads and decrypts in full
before the first frame. Fine for short decorative loops; for anything long, leave
it unsealed.

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

```
static/components/mathematics-of-faith/vecfig.html             # live, and the default
static/components/mathematics-of-faith/2025-11-02/vecfig.html  # frozen for that version
static/media/mathematics-of-faith/sigil.png
static/media/mathematics-of-faith/2025-11-02/sigil.png
```

The date is the version's own, taken from its filename in `posts/old/`. The check
is per file, so only keep a snapshot for assets that have actually been replaced
— everything else resolves to the shared copy with no duplication and no upkeep.

Explicit paths bypass this entirely, which is what you want for anything shared
across posts.

## Dev server

`--serve` builds, serves `site/` on `127.0.0.1:8000`, and watches for changes
every 0.4s across `build.py`, `media_ext.py`, `posts/` and `static/`.

Pages hold an `EventSource` on `/__reload` — one connection per tab for as long
as the tab is open, rather than a request every fraction of a second cluttering
the network tab. The handler emits an event when the build id changes, and a
comment every 15s to keep the connection honest; `EventSource` reconnects on its
own if the stream drops.

The server speaks HTTP/1.1 so browsers can reuse sockets. Under the stdlib
default of 1.0 every response closed its connection, and a speculatively reused
socket would occasionally land on a closed one as `ERR_CONNECTION_RESET`.

URLs are extensionless while serving, matching production. A build error prints
the traceback and keeps the server up, so you can fix the post and save again. A
build lock keeps requests from landing while `site/` is mid-rebuild and briefly
empty. Only failed requests are logged.

## Production notes

A non-`LOCAL` build minifies `static/styles/*.css` and `static/scripts/*.js`.
Both globs are shallow — a script in a subdirectory such as
`static/scripts/figures/` ships unminified.

`site/404.html` uses absolute asset paths, since GitHub Pages serves it for any
unmatched URL at any depth.

Content keys and the phrase salt rotate every build, so a reader holding a page
cached across a deploy will 404 on sealed media until they reload.

