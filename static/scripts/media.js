/* Optional: honor prefers-reduced-motion for looping videos.
   CSS can't stop playback, so pause autoplaying videos and hand the
   reader controls instead. Load it like theme.js, at the end of <body>. */
(function () {
  var q = matchMedia("(prefers-reduced-motion: reduce)");

  function apply() {
    document.querySelectorAll("article video[autoplay]").forEach(function (v) {
      if (q.matches) {
        v.pause();
        v.controls = true;
        v.removeAttribute("loop");
      } else {
        v.controls = false;
        v.setAttribute("loop", "loop");
        v.play().catch(function () {
          /* autoplay refused (rare when muted); leave the poster frame up */
          v.controls = true;
        });
      }
    });
  }

  apply();
  q.addEventListener("change", apply);
})();

