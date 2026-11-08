// Only active inside the AviationLog desktop app (pywebview window); a normal browser ignores it.
// - links marked data-download (CSV export, backups, original photos) are saved with a save dialog
// - links to other websites open in the normal browser
// - links with target="_blank" within AviationLog open in a second app window
(function () {
  function api() { return window.pywebview && window.pywebview.api; }
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || !api()) return;
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var url = new URL(a.getAttribute("href"), location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    if (url.origin !== location.origin) {
      e.preventDefault();
      api().open_external(url.href);
    } else if (a.hasAttribute("data-download")) {
      e.preventDefault();
      api().download(url.pathname + url.search, a.getAttribute("data-download") || "").then(function (r) {
        if (r && r.error) alert(r.error);
      });
    } else if (a.target === "_blank") {
      e.preventDefault();
      api().open_window(url.pathname + url.search);
    }
  });
})();
