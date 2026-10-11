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

  function uploadFile(form, file) {
    const status = form.querySelector("#upload-status");
    const bar = form.querySelector("#upload-progress");
    const hidden = form.querySelector("#staging-id");
    const button = form.querySelector("#submit-button");
    hidden.value = "";
    button.disabled = true;
    bar.hidden = false;
    bar.value = 0;
    status.textContent = "Feltöltés…";
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/feltoltes?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("X-CSRF-Token", csrf());
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.value = Math.round(100 * e.loaded / e.total); };
    xhr.onload = () => {
      button.disabled = false;
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
      if (xhr.status === 200 && body.staging_id) {
        hidden.value = body.staging_id;
        bar.value = 100;
        status.textContent = `Feltöltve: ${body.name}. Most már beküldheted.`;
      } else {
        bar.hidden = true;
        status.textContent = "A feltöltés nem sikerült: " + (body.error || xhr.status);
      }
    };
    xhr.onerror = () => { button.disabled = false; bar.hidden = true; status.textContent = "A feltöltés megszakadt."; };
    xhr.send(file);
  }

  function init() {
    document.querySelectorAll("form#submit-form").forEach((form) => {
      form.addEventListener("change", (e) => {
        if (e.target.name === "source") syncForm(form);
        if (e.target.id === "upload-file" && e.target.files.length) uploadFile(form, e.target.files[0]);
      });
      syncForm(form);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
