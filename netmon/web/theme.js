/* applied before first paint so there's no flash of the wrong theme */
(function () {
  var t = null;
  try { t = localStorage.getItem("nm-theme"); } catch (e) {}
  if (!t) t = window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", t);
  try { window.NM_UNITS = localStorage.getItem("nm-units") || "bits"; } catch (e) { window.NM_UNITS = "bits"; }
})();
