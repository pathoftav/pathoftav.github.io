"""Markdown extension for media written with image syntax.

    ![alt]({site_root}/static/media/{slug}/waterfall.webm?w=50&align=center "Crabtree Falls, October")
    ![alt](sigil.png?dark=sigil-dark.png&link=1&align=center)

Every option is a query parameter:

    align=<dir>   left | right | center | left-inline | right-inline.
                  Emitted as a class on whichever element ends up
                  outermost, and styled entirely in CSS. An unrecognised
                  value is ignored rather than passed through, and so is
                  center — it is the default, so it emits no class.
    w=<1-100>     width as a percentage of the containing block. A fixed
                  size, like h — media smaller than the slot IS stretched
                  up to fill it. Use it to make something bigger than it
                  really is; omit it to keep natural dimensions.
    mw=<1-100>    max-width as a percentage; a cap rather than a size.
                  On floated media it overrides the default 45% cap.
    h=<px>        height in pixels
    ar=<W>x<H>    aspect-ratio, reserves the box and prevents layout shift
    dark=<path>   images only: alternate source for the site's dark theme,
                  following the light/dark toggle rather than just the OS.
                  Emitted as a --dark custom property and swapped in CSS
                  with content: var(--dark).
                  A bare filename resolves next to the main image; anything
                  containing '/' is used as given.
    link=<url>    wrap in an anchor; link=1 links to the media itself
    loop=1        silent decorative loop: autoplays, repeats, no controls

A bare media filename resolves under {site_root}/static/media/<slug>/, so
a post's own media needs no path; anything containing '/' is used as
given. The same rule applies to ?dark= and ?link= siblings, which resolve
against the main path once it has been resolved.

Without loop=1 a video is an ordinary player — controls, sound, and the
reader presses play.

Sizing is natural by default: media renders at its own dimensions and is
capped at the column width, captioned and linked media included. ?w=
forces it wider than it really is, ?mw= caps it narrower. A <video> has no
dimensions until its metadata arrives, so a captioned video with neither
?w= nor ?ar= starts at the browser's 300x150 default and resizes on load —
a box around a video therefore takes the column rather than shrink-wrapping;
add ?ar= to reserve its height too.

The query never reaches the browser: it is consumed here and the emitted
src is the bare path. Alignment used to ride along as a '#dir' fragment so
that img[src$='#dir'] could match it in CSS; it is a class now, so the
fragment is gone and the CSS matches img.dir instead.

Whichever element ends up outermost carries the alignment class, since a
wrapping <a> is inline and would otherwise shrink-wrap the media and
defeat centering. When a caption is present its container — a <figure>,
or a <span> for media mid-sentence — takes over both the width and the
alignment, so the caption tracks the media rather than the full column.
"""

import re

from posixpath import relpath
from xml.etree import ElementTree as ET

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor


# path / ?query / #fragment. Hand-rolled rather than urlsplit because the
# {site_root} placeholder contains braces, which aren't legal URL chars.
# The fragment is still split off so a stray one can't end up inside path.
SRC_RE = re.compile(r"^(?P<path>[^?#]*)(?:\?(?P<query>[^#]*))?(?:#(?P<frag>.*))?$")
VIDEO_RE = re.compile(r"\.(?:webm|mp4)$", re.IGNORECASE)
AR_RE = re.compile(r"^(?P<w>\d+)[x:](?P<h>\d+)$")

# every accepted ?align= value
ALIGNS = {"left", "right", "center", "left-inline", "right-inline"}

# accepted, but the default already does it, so no class is emitted and the
# media keeps the plain-image fast path. Writing align=center is a note to
# the reader of the post, not an instruction to the browser. If centre ever
# needs a rule of its own, drop it from here and add the rule to media.css.
IMPLICIT_ALIGNS = {"center"}

# alignments that take the media out of normal flow
FLOAT_ALIGNS = {"left-inline", "right-inline"}


def parse_query(query: str | None) -> dict[str, str]:
    if not query:
        return {}
    out: dict[str, str] = {}
    for pair in query.split("&"):
        k, _, v = pair.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def clean(url: str) -> str:
    """Strip any query off a path given as a parameter value."""
    m = SRC_RE.match(url)
    return m["path"] if m else url


def sibling(path: str, name: str) -> str:
    """Resolve a parameter path. A bare filename is taken as a sibling of
    the main media; anything with a '/' is used as given."""
    name = clean(name)
    if "/" in name:
        return name
    base = path.rsplit("/", 1)[0] if "/" in path else ""
    return f"{base}/{name}" if base else name


def media_path(path: str, slug: str) -> str:
    """A bare filename resolves under the post's own media directory;
    anything containing '/' is used as given."""
    if not path or not slug or "/" in path:
        return path
    return f"{{site_root}}/static/media/{slug}/{path}"


def css_dark_path(path: str, name: str) -> str:
    """Resolve a dark-image path and express it relative to
    static/styles/media.css."""
    name = clean(name)

    # Normalize the main image path to site-relative.
    site_path = path.removeprefix("{site_root}/")

    # Resolve bare filenames alongside the main image.
    if "/" not in name:
        media_dir = site_path.rsplit("/", 1)[0]
        dark_path = f"{media_dir}/{name}"
    else:
        # Normalize explicit {site_root}/ paths too.
        dark_path = name.removeprefix("{site_root}/")

    return relpath(dark_path, "static/styles")


class MediaTreeprocessor(Treeprocessor):
    def __init__(self, md: Markdown, slug: str = "") -> None:
        super().__init__(md)
        self.slug = slug

    def run(self, root: ET.Element) -> ET.Element:
        for parent in root.iter():
            for child in list(parent):
                if child.tag != "img":
                    continue
                m = SRC_RE.match(child.get("src", ""))
                if m is None:
                    continue

                path = media_path(m["path"], self.slug)
                params = parse_query(m["query"])
                caption = child.get("title")
                is_video = VIDEO_RE.search(path) is not None

                # unknown values are dropped rather than emitted as a class,
                # and so is centre, which is what the CSS does anyway
                align = params.get("align")
                if align not in ALIGNS or align in IMPLICIT_ALIGNS:
                    align = None

                if not params and not caption and not is_video:
                    if path != m["path"]:
                        child.set("src", path)   # bare name still needs resolving
                    continue  # otherwise a plain image: leave it entirely alone

                if is_video:
                    media = self.build_video(child, path, align, params)
                else:
                    media = child
                    media.attrib.pop("title", None)
                    media.set("src", path)
                    if align:
                        self.add_class(media, align)

                styles = self.styles_for(params)
                # width and max-width size the outermost box; height and
                # aspect-ratio describe the frame and stay on the media
                box_style = ";".join(
                    f"{k}:{styles.pop(k)}"
                    for k in ("width", "max-width")
                    if k in styles
                )
                if styles:
                    self.merge_style(
                        media, ";".join(f"{k}:{v}" for k, v in styles.items())
                    )

                dark = params.get("dark")
                if dark and not is_video:
                    self.add_dark_variant(media, css_dark_path(path, dark))

                # the only remaining wrapper is the anchor
                node = media
                link = params.get("link")
                if link:
                    href = path if link == "1" else sibling(path, link)
                    node = self.wrap_link(node, href)

                # the outermost box owns the sizing and alignment, because a
                # wrapping <a> is inline and would otherwise shrink-wrap the
                # media and defeat both
                if node is not media:
                    node.set("class", f"media {align}" if align else "media")
                    if align:
                        # leaving the class on the inner element too would
                        # float it *inside* its own wrapper
                        self.remove_class(media, align)

                if node is not child:
                    # `tail` is the text after the closing tag; it belongs
                    # to the node being replaced.
                    node.tail = child.tail
                    # look the position up now rather than trusting an index
                    # captured earlier: drop_following_br removes siblings,
                    # so a cached index goes stale after the first match.
                    parent[list(parent).index(child)] = node

                if caption:
                    node = self.add_caption(parent, node, caption, align, box_style)
                elif box_style:
                    self.merge_style(node, box_style)

                # a caption/link box shrink-wraps its contents, which a
                # <video> cannot supply until its metadata arrives — see
                # media.css. Mark the box so CSS can give it the column.
                if is_video and node is not media:
                    self.add_class(node, "media-video")

                if align in FLOAT_ALIGNS:
                    self.drop_following_br(parent, node)
        return root

    @staticmethod
    def drop_following_br(parent: ET.Element, node: ET.Element) -> None:
        """Remove a <br> sitting immediately after floated media.

        nl2br turns the newline after a media line into a <br>. The float
        itself is out of flow, but that <br> still occupies a line box
        beside it, so the wrapping text starts one line too low.
        """
        children = list(parent)
        if node not in children:
            # the media was alone in its paragraph, so add_caption retagged
            # that <p> as the <figure> — node IS parent here, and there is
            # no sibling <br> to strip
            return
        i = children.index(node)
        if i + 1 >= len(children):
            return
        following = children[i + 1]
        if following.tag != "br":
            return
        node.tail = ((node.tail or "") + (following.tail or "")) or None
        parent.remove(following)

    @staticmethod
    def merge_style(el: ET.Element, extra: str) -> None:
        existing = el.get("style")
        el.set("style", f"{existing};{extra}" if existing else extra)

    @staticmethod
    def add_class(el: ET.Element, name: str) -> None:
        existing = el.get("class")
        el.set("class", f"{existing} {name}" if existing else name)

    @staticmethod
    def remove_class(el: ET.Element, name: str) -> None:
        rest = [c for c in (el.get("class") or "").split() if c != name]
        if rest:
            el.set("class", " ".join(rest))
        else:
            el.attrib.pop("class", None)

    @staticmethod
    def styles_for(params: dict[str, str]) -> dict[str, str]:
        styles: dict[str, str] = {}
        w = params.get("w", "")
        if w.isdigit() and 1 <= int(w) <= 100:
            styles["width"] = f"{w}%"
        mw = params.get("mw", "")
        if mw.isdigit() and 1 <= int(mw) <= 100:
            styles["max-width"] = f"{mw}%"
        h = params.get("h", "")
        if h.isdigit():
            styles["height"] = f"{h}px"
        ar = AR_RE.match(params.get("ar", ""))
        if ar:
            styles["aspect-ratio"] = f"{ar['w']}/{ar['h']}"
        return styles

    @classmethod
    def build_video(
        cls, img: ET.Element, path: str, align: str | None, params: dict[str, str]
    ) -> ET.Element:
        video = ET.Element("video")
        video.set("src", path)
        video.set("class", f"video {align}" if align else "video")

        if params.get("loop") == "1":
            # decorative loop: plays itself, silently, forever, with no controls.
            # muted is REQUIRED — browsers refuse to autoplay unmuted media, so
            # without it playback never starts.
            for attr in ("autoplay", "loop", "muted"):
                video.set(attr, attr)
        else:
            # an ordinary video the reader starts, with sound
            video.set("controls", "controls")
        video.set("playsinline", "playsinline")  # keeps iOS from going fullscreen

        alt = img.get("alt")
        if alt:
            video.set("aria-label", alt)
        return video

    @classmethod
    def add_dark_variant(cls, img: ET.Element, dark_src: str) -> None:
        """Attach the dark-theme source as a custom property.

        CSS swaps it with `content: var(--dark)`, which follows the site's
        :root[data-theme] toggle. A <picture> with a prefers-color-scheme
        media query could only ever see the OS setting.
        """
        cls.add_class(img, "theme-aware")
        cls.merge_style(img, f"--dark:url({dark_src})")

    @staticmethod
    def wrap_link(node: ET.Element, href: str) -> ET.Element:
        anchor = ET.Element("a")
        anchor.set("href", href)
        anchor.append(node)
        return anchor

    @classmethod
    def add_caption(
        cls,
        parent: ET.Element,
        node: ET.Element,
        caption: str,
        align: str | None,
        box_style: str,
    ) -> ET.Element:
        """Attach a visible caption, using whichever container is legal here.

        When the media is alone in its paragraph, the <p> is retagged as a
        <figure>. <figure> is flow content and cannot sit inside <p> — the
        HTML parser closes an open <p> on seeing one, which would split the
        surrounding sentence — so inline media instead gets a <span>
        wrapper, which is phrasing content and legal mid-paragraph.

        Either way the container takes the width and alignment class so it
        shrinks to the media's box; otherwise a centred caption drifts away
        from a narrow or floated image.
        """
        alone = (
            parent.tag == "p"
            and len(parent) == 1
            and not (parent.text or "").strip()
            and not (node.tail or "").strip()
        )

        if alone:
            # the paragraph itself becomes the figure, so its own tail —
            # the whitespace separating it from the next block — stays put
            box = parent
            tail = box.tail
            box.tag = "figure"
            node.tail = None
            classes = "media"
        else:
            # splice a <span> in where the media currently sits
            index = list(parent).index(node)
            tail = node.tail
            box = ET.Element("span")
            box.set("role", "figure")
            node.tail = None
            parent.remove(node)
            box.append(node)
            parent.insert(index, box)
            classes = "media media-inline"

        box.set("class", f"{classes} {align}" if align else classes)
        if box_style:
            box.set("style", box_style)

        # the box now owns the alignment; leaving the same class on the
        # inner element would float it *inside* its own caption box
        if align:
            cls.remove_class(node, align)

        figcaption = ET.SubElement(box, "figcaption" if alone else "span")
        if not alone:
            figcaption.set("class", "figcaption")
        figcaption.text = caption.replace("\\n", "\n")
        box.tail = tail
        return box


class MediaExtension(Extension):
    def __init__(self, slug: str = "", **kwargs) -> None:
        self.slug = slug
        super().__init__(**kwargs)

    def extendMarkdown(self, md: Markdown) -> None:
        md.treeprocessors.register(
            MediaTreeprocessor(md, self.slug),
            "media_embed",
            7,  # after inline processing has built the <img> nodes
        )

