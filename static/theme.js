// Theme toggle: cross-fades light/dark, persists an override in
// localStorage only when it disagrees with the OS preference.
var mq = matchMedia("(prefers-color-scheme: dark)");


/* run a mutation inside a view transition (cross-fade) when supported;
   otherwise fade with a temporary transition class */
function withTransition(fn) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    fn();
  } else if (document.startViewTransition) {
    document.startViewTransition(fn);
  } else {
    var root = document.documentElement;
    root.classList.add("theme-fade");
    fn();
    setTimeout(function () { root.classList.remove("theme-fade"); }, 400);
  }
}


document.querySelector("button.theme").addEventListener("click", function () {
  withTransition(function () {
    var root = document.documentElement;
    var os = mq.matches ? "dark" : "light";
    var current = root.dataset.theme || os;
    var next = current === "dark" ? "light" : "dark";
    if (next === os) {
      /* choice matches the OS — drop the override and follow the OS again */
      delete root.dataset.theme;
      try { localStorage.removeItem("theme"); } catch (e) {}
    } else {
      root.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) {}
    }
  });
});


/* if the OS theme changes while the page is open and now agrees with the
   saved override, the override is redundant — drop it so later OS changes
   are followed */
mq.addEventListener("change", function (e) {
  var os = e.matches ? "dark" : "light";
  if (document.documentElement.dataset.theme === os) {
    delete document.documentElement.dataset.theme;
    try { localStorage.removeItem("theme"); } catch (err) {}
  }
});


/* a link back to the page you're already on: nothing to animate, so a
   cross-fade of the page into itself only produces shimmer — skip it */
window.addEventListener("pageswap", function (e) {
  if (e.viewTransition && e.activation &&
      e.activation.from && e.activation.entry &&
      e.activation.from.url === e.activation.entry.url) {
    e.viewTransition.skipTransition();
  }
});

