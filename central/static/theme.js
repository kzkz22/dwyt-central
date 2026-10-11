// Runs in <head>, before the first paint: the chosen theme and the collapsed sidebar, so the
// page does not flash. Stored per browser; without it the system theme is used.
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var theme = localStorage.getItem("dwyt-theme");
    if (theme === "dark" || theme === "light") root.dataset.theme = theme;
    if (localStorage.getItem("dwyt-sidebar") === "collapsed") root.classList.add("sb-collapsed");
  } catch (e) { /* storage blocked: defaults */ }
})();
