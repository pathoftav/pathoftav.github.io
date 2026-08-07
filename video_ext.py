"""Markdown extension: render video files written with image syntax as
looping, muted, inline <video> elements.

    ![alt]({site_root}/static/images/waterfall.webm#center)

Any trailing #fragment becomes a CSS class, mirroring the
img[src$='#center'] convention used for still images.
"""

import re

from xml.etree import ElementTree as ET

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor


VIDEO_SRC_RE = re.compile(r"^(?P<src>.*\.(?:webm|mp4))(?:#(?P<frag>[\w-]+))?$", re.IGNORECASE)


class VideoTreeprocessor(Treeprocessor):
    def run(self, root: ET.Element) -> ET.Element:
        for parent in root.iter():
            for i, child in enumerate(list(parent)):
                if child.tag != "img":
                    continue
                m = VIDEO_SRC_RE.match(child.get("src", ""))
                if m is None:
                    continue

                video = ET.Element("video")
                video.set("src", m["src"])

                frag = m["frag"]
                video.set("class", f"video {frag}" if frag else "video")

                # muted is REQUIRED — every browser blocks autoplay of
                # unmuted media, so without it the loop never starts.
                # playsinline stops iOS going fullscreen.
                for attr in ("autoplay", "loop", "muted", "playsinline"):
                    video.set(attr, attr)

                alt = child.get("alt")
                if alt:
                    video.set("aria-label", alt)

                # `tail` is the text between this element's closing tag and
                # the next sibling. It belongs to the node being replaced,
                # so it has to be carried across or the prose after an
                # inline video is silently dropped.
                video.tail = child.tail

                parent[i] = video
        return root


class VideoExtension(Extension):
    def extendMarkdown(self, md: Markdown) -> None:
        md.treeprocessors.register(
            VideoTreeprocessor(md),
            "video_embed",
            7,  # after inline processing has built the <img> nodes
        )

