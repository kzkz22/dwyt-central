// Small progressive enhancements; every page also works as plain forms without them.
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (form.classList && form.classList.contains("confirm") && !window.confirm(form.dataset.confirm || "Biztos?")) {
      e.preventDefault();
    }
  });
})();
