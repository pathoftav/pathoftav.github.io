(function () {
  var sealed = document.querySelectorAll("[data-unlock]");
  if (!sealed.length) return;

  /* The host's clock, from the Date header of a request to our own origin.
     no-store keeps a cached response from handing back a stale timestamp.
     Resolves null if the network is unavailable — callers fail closed. */
  var serverNow = null;
  function hostClock() {
    if (serverNow) return serverNow;
    serverNow = fetch(location.href, { method: "HEAD", cache: "no-store" })
      .then(function (r) {
        var header = r.headers.get("Date");
        return header ? Date.parse(header) : null;
      })
      .catch(function () { return null; });
    return serverNow;
  }

  async function unseal(el) {
    var payload = el.querySelector("script.lock-payload");
    if (payload) {
      var material = new TextEncoder().encode(
        el.dataset.slug + "|" + el.dataset.unlock
      );
      var raw = await crypto.subtle.digest("SHA-256", material);
      var key = await crypto.subtle.importKey(
        "raw", raw, "AES-GCM", false, ["decrypt"]
      );

      var blob = Uint8Array.from(atob(payload.textContent.trim()), function (c) {
        return c.charCodeAt(0);
      });
      var plain = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: blob.slice(0, 12) }, key, blob.slice(12)
      );

      var holder = document.createElement("div");
      holder.innerHTML = new TextDecoder().decode(plain);
      while (holder.firstChild) el.insertBefore(holder.firstChild, payload);
      payload.remove();
    }

    el.querySelectorAll(".badge-locked").forEach(function (b) { b.remove(); });
    var notice = el.querySelector(".lock-notice");
    if (notice) notice.remove();
    el.classList.remove("locked");
    el.removeAttribute("data-unlock");
  }

  async function consider(el) {
    var at = Date.parse(el.dataset.unlock);
    if (isNaN(at)) return;

    var remaining = at - Date.now();
    if (remaining > 0) {
      // the reader's own clock says not yet; no need to ask the host.
      // setTimeout saturates past ~24.8 days, so only arm it within a day.
      if (remaining < 86400000) {
        setTimeout(function () { consider(el); }, remaining);
      }
      return;
    }

    // the reader's clock claims it's open — verify against the host before
    // believing it, and stay sealed if we can't reach anyone
    var host = await hostClock();
    if (host === null || host < at) return;

    await unseal(el);
  }

  sealed.forEach(function (el) {
    consider(el).catch(function (err) {
      // leave the notice up rather than showing a half-broken post
      console.error("unseal failed", err);
    });
  });
})();

