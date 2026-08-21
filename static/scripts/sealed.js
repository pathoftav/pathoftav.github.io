/* Phrase-sealed posts.
 *
 * The page shows a field of runes and nothing else — no prompt, no input,
 * no hint that typing does anything. A reader who knows the phrase types it
 * blind and presses Enter; the keystrokes never reach the document, so they
 * never surface in a find bar or a form field. On Enter the phrase is
 * stretched with PBKDF2 into the AES-GCM key that opens the payload. The
 * GCM tag is the only check there is: a wrong phrase produces a key that
 * fails authentication, and the page says nothing it did not already say.
 */
(function () {
	var root = document.documentElement;
	var sealed = Array.prototype.slice.call(document.querySelectorAll("[data-seal]"));

	if (!sealed.length) {
		root.classList.remove("seal-pending");
		return;
	}

	/* The phrase is never stored, and after the handle is derived it is never
		 held anywhere either. What sessionStorage keeps is the handle: one-way,
		 so a copy of it does not give up what was typed, and salted per build,
		 so it stops opening anything the next time the site is built. The salt
		 is kept beside it only to recognise a handle that has gone stale. */
	var STORE_KEY = "seal-handle";      /* sessionStorage: forgotten with the tab */
	var SALT_KEY = "seal-salt";         /* the build the handle belongs to */
	var MAX_PHRASE = 256;               /* cap the buffer so a lean on the keyboard can't grow it */
	var IDLE_MS = 5000;                 /* a pause this long abandons a half-typed phrase */
	var DEFAULT_ROUNDS = 310000;        /* only used if data-rounds is missing */
	var DEFAULT_HANDLE_ROUNDS = 310000; /* only used if data-handle-rounds is missing */

	var CALM = matchMedia("(prefers-reduced-motion: reduce)").matches;

	function still() {
		return sealed.filter(function (el) {
			return el.hasAttribute("data-seal");
		});
	}

	function wait(ms) {
		return new Promise(function (done) {
			setTimeout(done, ms);
		});
	}

	function rand(lo, hi) {
		return lo + Math.random() * (hi - lo);
	}


	/* ------------------------------------------------------------------
	 * the seal itself
	 * ---------------------------------------------------------------- */

	function hex(bytes) {
		return Array.prototype.map.call(new Uint8Array(bytes), function (b) {
			return b.toString(16).padStart(2, "0");
		}).join("");
	}

	function unhex(s) {
		return Uint8Array.from((s || "").match(/../g) || [], function (pair) {
			return parseInt(pair, 16);
		});
	}

	/* Any sealed post on the page will do: the handle salt and rounds describe
		 the build, not the post, which is what lets one handle open all of them. */
	function anyPayload() {
		return document.querySelector("script.seal-payload");
	}

	function buildSalt() {
		var payload = anyPayload();
		return payload ? (payload.dataset.handleSalt || "") : "";
	}

	/* The phrase goes in and does not come out. What comes out is the handle:
		 the only value the rest of this script sees and the only one ever
		 written down. Stretched, not plain-hashed, so that a handle read out of
		 sessionStorage is not a dictionary attack away from the phrase itself.
		 Matches build.py's derive_handle(). */
	async function handleFor(phrase, payload) {
		var material = await crypto.subtle.importKey(
			"raw",
			new TextEncoder().encode(phrase),
			"PBKDF2",
			false,
			["deriveBits"]
		);

		var bits = await crypto.subtle.deriveBits(
			{
				name: "PBKDF2",
				salt: unhex(payload.dataset.handleSalt),
				iterations: Number(payload.dataset.handleRounds) || DEFAULT_HANDLE_ROUNDS,
				hash: "SHA-256"
			},
			material,
			256
		);

		return hex(bits);
	}

	/* blob layout, matching build.py's seal_by_phrase():
		 [0:16] salt   [16:28] iv   [28:] ciphertext || GCM tag */
	async function unwrap(el, handle) {
		var payload = el.querySelector("script.seal-payload");
		if (!payload) return null;

		var blob = Uint8Array.from(
			atob(payload.textContent.trim()),
			function (c) {
				return c.charCodeAt(0);
			}
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

		try {
			var plain = await crypto.subtle.decrypt(
				{ name: "AES-GCM", iv: blob.slice(16, 28) },
				key,
				blob.slice(28)
			);
		} catch (e) {
			return null;      /* wrong handle — indistinguishable from noise */
		}

		return new TextDecoder().decode(plain);
	}


	/* ------------------------------------------------------------------
	 * the rune field
	 * ---------------------------------------------------------------- */

	/* Array.from, not split(""): the alchemical glyphs are astral and would
		 come back as broken surrogate halves. */
	function poolFor(field) {
		if (!field._pool) {
			field._pool = Array.from(field.dataset.runes || "");
		}
		return field._pool;
	}

	function styleTransmutation(rune) {
		var duration = rand(1600, 4000);

		/* Pick a color */
		var PALETTE = [
			{ name: "Yellow", pct: 55, hMin: 34,  hMax: 52 },
			{ name: "Purple", pct: 14, hMin: 260, hMax: 290 },
			{ name: "Blue",   pct: 11, hMin: 200, hMax: 240 },
			{ name: "Green",  pct: 11, hMin: 120, hMax: 150 },
			{ name: "White",  pct: 5,  sat: "0%", lit: "100%" },
			{ name: "Orange", pct: 3,  hMin: 20,  hMax: 32 },
			{ name: "Red",    pct: 1,  hMin: 0,   hMax: 15 }
		];
		var roll = rand(0, 100);
		var chosen = PALETTE[0];
		var count = 0;
		for (var i = 0; i < PALETTE.length; i++) {
			count += PALETTE[i].pct;
			if (roll <= count) { chosen = PALETTE[i]; break; }
		}

		/* Apply properties */
		rune.style.setProperty("--rune-duration", duration.toFixed(0) + "ms");
		rune.style.setProperty("--rune-x",   rand(-11, 11).toFixed(1) + "px");
		rune.style.setProperty("--rune-y",   rand(-15, 6).toFixed(1)  + "px");
		rune.style.setProperty("--rune-rot", rand(-38, 38).toFixed(1) + "deg");

		rune.style.setProperty("--rune-hue", (chosen.hMin !== undefined ? rand(chosen.hMin, chosen.hMax) : rand(0, 360)).toFixed(0));
		rune.style.setProperty("--rune-sat", chosen.sat || "80%");
		rune.style.setProperty("--rune-lit", chosen.lit || "70%");

		return duration;
	}

	function transmute(rune, pool) {
		if (rune.classList.contains("changing") || pool.length < 2) return;

		/* Set all custom properties and return the chosen duration */
		var duration = styleTransmutation(rune);

		rune.classList.add("changing");

		/* Swap glyph at 48% of the calculated duration */
		var swap = setTimeout(function () {
			var next = rune.textContent;
			while (next === rune.textContent) {
				next = pool[Math.floor(Math.random() * pool.length)];
			}
			rune.textContent = next;
		}, duration * 0.48);

		rune.addEventListener("animationend", function done() {
			clearTimeout(swap);
			rune.classList.remove("changing");
			rune.style.cssText = "";
		}, { once: true });
	}

	function stirRunes() {
		if (CALM) return;

		setInterval(function () {
			if (document.hidden) return;

			document.querySelectorAll(".seal-runes").forEach(function (field) {
				var runes = field.querySelectorAll(".seal-rune");
				if (!runes.length) return;

				var pool = poolFor(field);
				var n = Math.floor(Math.random() * 3) + 1;
				for (var i = 0; i < n; i++) {
					transmute(runes[Math.floor(Math.random() * runes.length)], pool);
				}
			});
		}, 2500);
	}


	/* ------------------------------------------------------------------
	 * the unsealing
	 * ---------------------------------------------------------------- */

	/* A <script> built by innerHTML is inert by spec: the fragment parser
		 marks it "already started", so it never runs no matter where it is
		 put. Everything a post ships for itself — an import map, a three.js
		 module, a small inline widget — therefore arrives dead. Swapping each
		 one for a freshly created element carrying the same attributes and
		 text is what brings it back.

		 Import maps are hoisted to the front of the queue whatever their
		 position in the post. A map registered after module resolution has
		 begun is at best ignored and at worst a console error, and the module
		 that needed it fails to resolve its bare specifiers. Array sort is
		 stable, so document order still holds within each group. */
	function revive(scope) {
		var dead = Array.prototype.slice.call(scope.querySelectorAll("script"));

		dead.sort(function (a, b) {
			return (b.type === "importmap") - (a.type === "importmap");
		});

		dead.forEach(function (old) {
			var live = document.createElement("script");

			for (var i = 0; i < old.attributes.length; i++) {
				live.setAttribute(old.attributes[i].name, old.attributes[i].value);
			}

			live.textContent = old.textContent;

			/* an inserted script defaults to async, which would let a later
				 one overtake an earlier one it depends on */
			if (!old.async) live.async = false;

			old.replaceWith(live);
		});
	}

	/* KaTeX ran at DOMContentLoaded, when the body was still ciphertext.
		 Same delimiters as math.js. */
	function typesetMath(el) {
		if (!window.renderMathInElement) return;
		try {
			renderMathInElement(el, {
				delimiters: [
					{ left: "\\(", right: "\\)", display: false },
					{ left: "\\[", right: "\\]", display: true }
				],
				throwOnError: false
			});
		} catch (e) {}
	}

	function reveal(el, markup, quiet) {
		var holder = document.createElement("div");
		holder.innerHTML = markup;

		var incoming = Array.prototype.slice.call(holder.childNodes);
		var blocks = incoming.filter(function (n) {
			return n.nodeType === 1;
		});

		function swap() {
			var notice = el.querySelector(".seal-notice");
			var payload = el.querySelector("script.seal-payload");
			var anchor = payload || notice;

			incoming.forEach(function (n) {
				el.insertBefore(n, anchor);
			});

			if (payload) payload.remove();
			if (notice) notice.remove();

			el.classList.remove("sealed");
			el.classList.add("is-unsealed");
			el.removeAttribute("data-seal");

			revive(el);         /* after the payload is gone, so it is not a candidate */
			typesetMath(el);
		}

		/* recalled from sessionStorage: the reader already earned the
			 theatre once, so this time the post is simply there */
		if (quiet || CALM) {
			swap();
			return Promise.resolve();
		}

		/* 1. the runes catch, flare white-gold, and scatter */
		var field = el.querySelector(".seal-runes");
		if (field) {
			field.querySelectorAll(".seal-rune").forEach(function (rune, i) {
				rune.classList.remove("changing");
				rune.style.cssText = "";
				rune.style.setProperty("--scatter-x", rand(-90, 90).toFixed(0) + "px");
				rune.style.setProperty("--scatter-y", rand(-170, -50).toFixed(0) + "px");
				rune.style.setProperty("--scatter-rot", rand(-140, 140).toFixed(0) + "deg");
				rune.style.setProperty("--scatter-delay", (i * 9 + rand(0, 90)).toFixed(0) + "ms");
			});
			el.classList.add("seal-breaking");
		}

		/* 2. the text condenses out of the light the runes left behind */
		return wait(field ? 1250 : 200).then(function () {
			swap();
			el.classList.remove("seal-breaking");
			el.classList.add("seal-unsealing");

			blocks.forEach(function (n, i) {
				n.style.setProperty("--reveal-delay", (i * 85).toFixed(0) + "ms");
				n.classList.add("seal-reveal-start");
			});

			void el.offsetWidth;    /* commit the start state before animating off it */

			blocks.forEach(function (n) {
				n.classList.add("just-unsealed");
				n.addEventListener("animationend", function done() {
					n.classList.remove("just-unsealed", "seal-reveal-start");
					n.style.removeProperty("--reveal-delay");
				}, { once: true });
			});

			return wait(2200).then(function () {
				el.classList.remove("seal-unsealing");
			});
		});
	}


	/* ------------------------------------------------------------------
	 * attempts
	 * ---------------------------------------------------------------- */

	function flash(cls, ms) {
		root.classList.remove(cls);
		void root.offsetWidth;
		root.classList.add(cls);
		setTimeout(function () {
			root.classList.remove(cls);
		}, ms);
	}

	/* the salt rides along so a handle from an earlier build can be spotted
		 without spending a PBKDF2 finding out */
	function remember(handle, salt) {
		try {
			sessionStorage.setItem(STORE_KEY, handle);
			sessionStorage.setItem(SALT_KEY, salt);
		} catch (e) {}
	}

	function forget() {
		try {
			sessionStorage.removeItem(STORE_KEY);
			sessionStorage.removeItem(SALT_KEY);
		} catch (e) {}
	}

	var busy = false;

	async function attempt(handle, quiet) {
		var targets = still();
		if (busy || !targets.length) return false;
		busy = true;

		try {
			var salt = buildSalt();     /* read before reveal() takes the payloads away */
			var opened = [];

			for (var i = 0; i < targets.length; i++) {
				var markup = await unwrap(targets[i], handle);
				if (markup !== null) opened.push([targets[i], markup]);
			}

			if (!opened.length) {
				if (quiet) forget();       /* a stale handle; stop trying it */
					else flash("seal-key-rejected", 460);
				return false;
			}

			remember(handle, salt);
			if (!quiet) flash("seal-key-accepted", 1250);

			await Promise.all(opened.map(function (pair) {
				return reveal(pair[0], pair[1], quiet);
			}));

			return true;
		} finally {
			busy = false;
		}
	}


	/* ------------------------------------------------------------------
	 * blind entry
	 * ---------------------------------------------------------------- */

	function listen() {
		var buffer = "";
		var idle = null;

		function clear() {
			buffer = "";
			clearTimeout(idle);
		}

		function touch() {
			clearTimeout(idle);
			idle = setTimeout(clear, IDLE_MS);
		}

		document.addEventListener("keydown", function (e) {
			if (!still().length) return;                 /* everything is open */
			if (e.ctrlKey || e.metaKey || e.altKey) return;

			var t = e.target;
			if (t && (t.isContentEditable ||
				/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;

			if (e.key === "Enter") {
				var typed = buffer;
				clear();                 /* before the await, not after */

				if (typed) {
					var payload = anyPayload();
					if (payload) {
						handleFor(typed, payload).then(function (h) {
							typed = "";      /* the phrase is done with */
							return attempt(h, false);
						}).catch(function (err) {
							console.error("unseal failed", err);
						});
					}
				}
				return;
			}

			if (e.key === "Escape") {
				clear();
				return;
			}

			if (e.key === "Backspace") {
				if (!buffer) return;
				buffer = buffer.slice(0, -1);
				e.preventDefault();
				touch();
				return;
			}

			if (Array.from(e.key).length !== 1) return;  /* Shift, arrows, F-keys */

			/* Swallowing the keystroke is the point: Firefox's quick-find
				 would otherwise print the phrase along the bottom of the
				 window. Space is left alone until a phrase is under way, so
				 an idle reader can still page down. */
			if (e.key !== " " || buffer) e.preventDefault();

			buffer = (buffer + e.key).slice(-MAX_PHRASE);
			touch();
		});
	}


	/* ------------------------------------------------------------------
	 * start
	 * ---------------------------------------------------------------- */

	/* A handle only opens the build it was derived under. Checking the salt
		 before spending a PBKDF2 on it means a reader arriving after a rebuild
		 is told the truth immediately, rather than after a wasted derivation. */
	var recalled = null;
	try {
		if (sessionStorage.getItem(SALT_KEY) === buildSalt()) {
			recalled = sessionStorage.getItem(STORE_KEY);
		} else {
			forget();
		}
	} catch (e) {}

	(recalled ? attempt(recalled, true) : Promise.resolve(false))
		.catch(function (err) {
			console.error("unseal failed", err);
		})
		.finally(function () {
			root.classList.remove("seal-pending");
			listen();
			stirRunes();
		});
})();

