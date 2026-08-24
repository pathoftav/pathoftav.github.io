/* Sealed rows in a listing.
 *
 * The index and the tag pages are built twice over: once with the rows
 * anyone may see, and once with the rows a reader holding the phrase may
 * see. The second listing ships as one ciphertext under the same build
 * handle a sealed post uses, so the page gives up nothing — not a tag name,
 * not a count, not the fact that a particular tag has more behind it than
 * it shows.
 *
 * No phrase is typed here and none is asked for. sealed.js writes the handle
 * into sessionStorage when a post opens; this reads it back. Opening one
 * sealed post therefore lights up every listing for the rest of the tab's
 * life, and closing the tab puts them out again.
 *
 * A tag carried only by sealed posts has a page like any other — it simply
 * stands empty, and nothing links to it, until the overview's payload opens
 * and supplies the row that does.
 */
(function () {
	"use strict";

	var root = document.documentElement;
	var payload = document.querySelector("script.seal-listing");
	if (!payload) return;

	var STORE_KEY = "seal-handle";      /* sessionStorage: forgotten with the tab */
	var SALT_KEY = "seal-salt";         /* the build the handle belongs to */
	var DEFAULT_ROUNDS = 310000;        /* only used if data-rounds is missing */

	/* The public list is hidden by the head style while a handle is on file,
	   so a reader who unsealed a post never sees the shorter listing paint
	   first. Whatever happens below, it has to end up visible again. */
	function show() {
		var pending = document.querySelector("[data-seal-listing]");
		if (pending) pending.removeAttribute("data-seal-listing");
	}

	var handle = null;
	try {
		if (sessionStorage.getItem(SALT_KEY) === payload.dataset.handleSalt) {
			handle = sessionStorage.getItem(STORE_KEY);
		}
	} catch (e) {}

	if (!handle) {
		show();
		return;
	}


	/* ------------------------------------------------------------------
	 * the seal
	 * ---------------------------------------------------------------- */

	/* blob layout, matching build.py's seal_by_phrase():
	   [0:16] salt   [16:28] iv   [28:] ciphertext || GCM tag */
	async function unwrap() {
		var blob = Uint8Array.from(
			atob(payload.textContent.trim()),
			function (c) { return c.charCodeAt(0); }
		);

		var material = await crypto.subtle.importKey(
			"raw",
			new TextEncoder().encode(handle),
			"PBKDF2",
			false,
			["deriveKey"]
		);

		var key = await crypto.subtle.deriveKey(
			{
				name: "PBKDF2",
				salt: blob.slice(0, 16),
				iterations: Number(payload.dataset.rounds) || DEFAULT_ROUNDS,
				hash: "SHA-256"
			},
			material,
			{ name: "AES-GCM", length: 256 },
			false,
			["decrypt"]
		);

		var plain = await crypto.subtle.decrypt(
			{ name: "AES-GCM", iv: blob.slice(16, 28) },
			key,
			blob.slice(28)
		);

		return new TextDecoder().decode(plain);
	}


	/* ------------------------------------------------------------------
	 * the swap
	 * ---------------------------------------------------------------- */

	unwrap().then(function (markup) {
		var target = document.querySelector("[data-seal-listing]");
		if (!target) return;

		var holder = document.createElement("div");
		holder.innerHTML = markup;

		/* the replacements carry no data-seal-listing, so the head style
		   stops applying the moment the old element leaves */
		target.replaceWith.apply(
			target,
			Array.prototype.slice.call(holder.childNodes)
		);

		payload.remove();
		root.classList.add("seal-open");

		/* remove 404 fake view transition */
		document.getElementById("no-view-transition")?.remove();
		document.getElementById("fake-view-transition")?.remove();

		/* A page whose public form is the 404 cannot carry its own title
		   either — "#necromancy" in the <title> would name the tag to
		   anyone who read the markup. It rides in with the rows instead. */
		var titled = document.querySelector("[data-seal-title]");
		if (titled) document.title = titled.dataset.sealTitle;
	}).catch(function () {
		/* a handle from a build this page does not belong to, or none at all:
		   say nothing, show what any reader would see */
		show();
	});
})();

