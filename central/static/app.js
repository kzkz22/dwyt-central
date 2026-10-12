// Small progressive enhancements; every page also works as plain forms without them
// (the upload needs this script: the file goes up first, then the form refers to it).
(function () {
  "use strict";
  const csrf = () => document.body.dataset.csrf || "";

  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (form.classList && form.classList.contains("confirm") && !window.confirm(form.dataset.confirm || "Biztos?")) {
      e.preventDefault();
    }
  });

  // Show only the parts of the submit form that belong to the chosen source; an upload cannot
  // use YouTube captions.
  function syncForm(form) {
    const pick = (name) => (form.querySelector(`input[name="${name}"]:checked`) || {}).value;
    const source = pick("source");
    form.querySelectorAll("[data-for-source]").forEach((el) => { el.hidden = el.dataset.forSource !== source; });
    const captions = form.querySelector('input[name="mode"][value="captions"]');
    if (captions) {
      captions.disabled = source === "upload";
      if (captions.disabled && captions.checked) {
        const local = form.querySelector('input[name="mode"][value="local"]');
        if (local) local.checked = true;
      }
    }
  }

  // Resolves true once the file is staged (the hidden staging_id is set), false on failure.
  function uploadFile(form, file) {
    return new Promise((resolve) => {
      const status = form.querySelector("#upload-status");
      const bar = form.querySelector("#upload-progress");
      const hidden = form.querySelector("#staging-id");
      const button = form.querySelector("#submit-button");
      hidden.value = "";
      form.dataset.uploading = "1";
      button.disabled = true;
      bar.hidden = false;
      bar.value = 0;
      status.textContent = "Feltöltés…";
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", "/feltoltes?name=" + encodeURIComponent(file.name));
      xhr.setRequestHeader("X-CSRF-Token", csrf());
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = Math.round(100 * e.loaded / e.total); };
      const done = (ok) => { delete form.dataset.uploading; button.disabled = false; resolve(ok); };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
        if (xhr.status === 200 && body.staging_id) {
          hidden.value = body.staging_id;
          bar.value = 100;
          status.textContent = `Feltöltve: ${body.name}. Most már beküldheted.`;
          done(true);
        } else {
          bar.hidden = true;
          status.textContent = "A feltöltés nem sikerült: " + (body.error || xhr.status);
          done(false);
        }
      };
      xhr.onerror = () => { bar.hidden = true; status.textContent = "A feltöltés megszakadt."; done(false); };
      xhr.send(file);
    });
  }

  // Submitting an upload before the file is staged: upload first, then submit.
  function beforeSubmit(form, e) {
    const source = (form.querySelector('input[name="source"]:checked') || {}).value;
    const hidden = form.querySelector("#staging-id");
    if (source !== "upload" || hidden.value) return;
    e.preventDefault();
    const status = form.querySelector("#upload-status");
    if (form.dataset.uploading) { status.textContent = "Várd meg, amíg a feltöltés befejeződik."; return; }
    const input = form.querySelector("#upload-file");
    if (!input.files.length) { status.textContent = "Előbb válaszd ki a fájlt."; return; }
    uploadFile(form, input.files[0]).then((ok) => { if (ok) form.submit(); });
  }

  // ---- frame: collapsible sidebar (desktop), off-canvas menu (mobile), theme ----
  const root = document.documentElement;
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch (err) { /* blocked */ } };
  const mobile = () => window.matchMedia("(max-width: 899px)").matches;

  function syncCollapse() {
    const collapsed = root.classList.contains("sb-collapsed");
    document.querySelectorAll("[data-sidebar-collapse]").forEach((b) => {
      b.setAttribute("aria-expanded", String(!collapsed));
      b.setAttribute("aria-label", collapsed ? "Menü kinyitása" : "Menü becsukása");
    });
    // a collapsed sidebar shows icons only: the names become tooltips
    document.querySelectorAll(".sb-link, .sb-tools .icon-button").forEach((a) => {
      const label = a.querySelector(".sb-label");
      if (collapsed && label) a.setAttribute("title", label.textContent); else a.removeAttribute("title");
    });
  }

  let lastFocus = null;
  function openMenu() {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    lastFocus = document.activeElement;
    root.classList.add("sb-open");
    document.querySelectorAll(".sb-backdrop").forEach((b) => { b.hidden = false; });
    document.querySelectorAll("[data-sidebar-open]").forEach((b) => b.setAttribute("aria-expanded", "true"));
    sidebar.setAttribute("role", "dialog");
    sidebar.setAttribute("aria-modal", "true");
    const first = sidebar.querySelector("[aria-current]") || sidebar.querySelector("a, button");
    if (first) first.focus();
  }
  function closeMenu() {
    if (!root.classList.contains("sb-open")) return;
    const sidebar = document.getElementById("sidebar");
    root.classList.remove("sb-open");
    document.querySelectorAll(".sb-backdrop").forEach((b) => { b.hidden = true; });
    document.querySelectorAll("[data-sidebar-open]").forEach((b) => b.setAttribute("aria-expanded", "false"));
    sidebar.removeAttribute("role");
    sidebar.removeAttribute("aria-modal");
    if (lastFocus) lastFocus.focus();
  }

  document.addEventListener("click", function (e) {
    const el = e.target.closest ? e.target.closest("[data-sidebar-open], [data-sidebar-close], [data-sidebar-collapse], [data-theme-toggle]") : null;
    if (!el) return;
    if (el.hasAttribute("data-sidebar-open")) openMenu();
    else if (el.hasAttribute("data-sidebar-close")) closeMenu();
    else if (el.hasAttribute("data-sidebar-collapse")) {
      root.classList.add("sb-animate");
      root.classList.toggle("sb-collapsed");
      store("dwyt-sidebar", root.classList.contains("sb-collapsed") ? "collapsed" : "open");
      syncCollapse();
    } else if (el.hasAttribute("data-theme-toggle")) {
      const dark = root.dataset.theme ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      store("dwyt-theme", root.dataset.theme);
    }
  });
  document.addEventListener("keydown", function (e) {
    if (!root.classList.contains("sb-open")) return;
    if (e.key === "Escape") { closeMenu(); return; }
    if (e.key === "Tab") {  // keep the focus inside the open menu
      const items = Array.from(document.getElementById("sidebar").querySelectorAll("a, button")).filter((x) => x.offsetParent);
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener("resize", () => { if (!mobile()) closeMenu(); });

  // questions: Ctrl+Enter sends; after an accepted question the box is emptied (it is kept across
  // the panel's refreshes with hx-preserve)
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.id === "chat-q" && e.target.form) {
      e.preventDefault();
      e.target.form.requestSubmit();
    }
  });
  // model pickers: the line under the select says what runs behind the chosen name
  document.addEventListener("change", function (e) {
    const sel = e.target;
    if (!(sel instanceof HTMLSelectElement) || !sel.dataset.hintTarget) return;
    const box = document.getElementById(sel.dataset.hintTarget);
    const opt = sel.options[sel.selectedIndex];
    if (box && opt) (box.querySelector("span") || box).textContent = opt.dataset.hint || "";
  });
  document.addEventListener("htmx:afterSwap", function () {
    const panel = document.getElementById("kerdes");
    if (panel && panel.dataset.sent) {
      const box = document.getElementById("chat-q");
      if (box) box.value = "";
      delete panel.dataset.sent;
    }
  });

  function init() {
    syncCollapse();
    document.querySelectorAll("form#submit-form").forEach((form) => {
      form.addEventListener("change", (e) => {
        if (e.target.name === "source") syncForm(form);
        if (e.target.id === "upload-file" && e.target.files.length) uploadFile(form, e.target.files[0]);
      });
      form.addEventListener("submit", (e) => beforeSubmit(form, e));
      syncForm(form);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
