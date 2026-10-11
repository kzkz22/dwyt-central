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

  function init() {
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
