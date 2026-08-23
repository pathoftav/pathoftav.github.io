/* Decrypt the media belonging to a locked or sealed post.
 *
 * Both lock.js and sealed.js need this and neither loads the other, so it
 * lives on its own rather than twice over. Not in media.js, which ships on
 * every page: this is only ever wanted by a post that was shut in.
 *
 * The body arrives as one ciphertext; its media does not. Inlining the bytes
 * would cost about 78% over the wire and decrypt in full before the page
 * could paint, so each file is published separately under static/sealed/ and
 * fetched once the body is open.
 *
 * The content key lives inside the shut-in body, so by the time this runs the
 * caller has already proved it could open that body. lock.js and sealed.js
 * call decryptMedia(container, keyHex) while the markup is still detached.
 */
(function () {
	"use strict";

	function unhex(hex) {
		var out = new Uint8Array(hex.length / 2);
		for (var i = 0; i < out.length; i++) {
			out[i] = parseInt(hex.substr(i * 2, 2), 16);
		}
		return out;
	}

	/* iv || ciphertext || GCM tag, as publish_asset() laid it out */
	async function fetchPlain(key, url) {
		var res = await fetch(url, { cache: "no-store" });
		if (!res.ok) throw new Error(url + " -> " + res.status);
		var blob = new Uint8Array(await res.arrayBuffer());
		return crypto.subtle.decrypt(
			{ name: "AES-GCM", iv: blob.subarray(0, 12) },
			key,
			blob.subarray(12)
		);
	}

	/* never revoked: a revoked URL fails if the resource is decoded again —
	   printing, a re-render, a theme toggle re-reading --dark. They go when
	   the page does. */
	function objectUrl(buf, type) {
		return URL.createObjectURL(new Blob([buf], { type: type || "" }));
	}

	window.decryptMedia = async function (container, keyHex) {
		if (!keyHex || !container) return;

		var key = await crypto.subtle.importKey(
			"raw", unhex(keyHex), "AES-GCM", false, ["decrypt"]
		);

		var jobs = [];

		container.querySelectorAll("[data-enc]").forEach(function (el) {
			jobs.push(fetchPlain(key, el.dataset.enc).then(function (buf) {
				el.src = objectUrl(buf, el.dataset.encType);
				el.removeAttribute("data-enc");
				el.removeAttribute("data-enc-type");
			}));
		});

		container.querySelectorAll("[data-enc-dark]").forEach(function (el) {
			jobs.push(fetchPlain(key, el.dataset.encDark).then(function (buf) {
				el.style.setProperty(
					"--dark", "url(" + objectUrl(buf, el.dataset.encDarkType) + ")"
				);
				el.removeAttribute("data-enc-dark");
				el.removeAttribute("data-enc-dark-type");
			}));
		});

		/* allSettled: one missing file should not blank the rest of the post */
		(await Promise.allSettled(jobs)).forEach(function (r) {
			if (r.status === "rejected") console.warn("media decrypt failed:", r.reason);
		});
	};
})();

