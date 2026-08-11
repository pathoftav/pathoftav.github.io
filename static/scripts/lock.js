(function () {
	var sealed = document.querySelectorAll("[data-unlock]");
	if (!sealed.length) {
		document.documentElement.classList.remove("lock-pending");
		return;
	}

	var serverNow = null;

	function hostClock(forceRefresh) {
		if (serverNow && !forceRefresh) return serverNow;

		serverNow = fetch(location.href, {
			method: "HEAD",
			cache: "no-store"
		})
			.then(function (r) {
				var header = r.headers.get("Date");
				return header ? Date.parse(header) : null;
			})
			.catch(function () {
				return null;
			});

		return serverNow;
	}

	async function unseal(el, animate) {
		var payload = el.querySelector("script.lock-payload");

		if (payload) {
			var material = new TextEncoder().encode(el.dataset.slug + "|" + el.dataset.unlock);
			var raw = await crypto.subtle.digest("SHA-256", material);
			var key = await crypto.subtle.importKey(
				"raw",
				raw,
				"AES-GCM",
				false,
				["decrypt"]
			);

			var blob = Uint8Array.from(
				atob(payload.textContent.trim()),
				function (c) {
					return c.charCodeAt(0);
				}
			);

			var plain = await crypto.subtle.decrypt(
				{ name: "AES-GCM", iv: blob.slice(0, 12) },
				key,
				blob.slice(12)
			);

			var holder = document.createElement("div");
			holder.innerHTML = new TextDecoder().decode(plain);

			function applyDOMUpdates() {
				while (holder.firstChild) {
					el.insertBefore(holder.firstChild, payload);
				}

				payload.remove();

				el.querySelectorAll(".badge-locked").forEach(function (b) {
					b.remove();
				});

				var notice = el.querySelector(".lock-notice");
				if (notice) notice.remove();

				el.classList.remove("locked");
				el.removeAttribute("data-unlock");

				if (animate) {
					el.classList.add("just-unlocked");
				}
			}

			if (animate && document.startViewTransition) {
				el.style.viewTransitionName = "unseal-" + el.dataset.slug;
				var transition = document.startViewTransition(applyDOMUpdates);

				transition.finished.finally(function() {
					el.style.viewTransitionName = "";
				});
			} else {
				applyDOMUpdates();
			}
		}
	}

	async function consider(el, isWait) {
		var at = Date.parse(el.dataset.unlock);
		if (isNaN(at)) return;

		var remaining = at - Date.now();
		if (remaining > 0) {
			if (remaining < 86400000) {
				setTimeout(function () {
					consider(el, true);
				}, remaining);
			}
			return;
		}

		var host = await hostClock(isWait);
		if (host === null || host < at) {
			setTimeout(function() {
				consider(el, true);
			}, 1000);
			return;
		}

		await unseal(el, isWait);
	}

	Promise.all(
		Array.from(sealed).map(async function (el) {
			try {
				return await consider(el);
			} catch (err) {
				console.error("unseal failed", err);
			}
		})
	).finally(function () {
		document.documentElement.classList.remove("lock-pending");
	});
})();

