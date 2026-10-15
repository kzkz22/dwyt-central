// Runner settings page (round 7d): fields shown only with another setting, the mode buttons, and the keys
// encrypted in the browser with the runner's public key (RSA-OAEP, SHA-256) before the form is sent.
// The plain key inputs have no name attribute: only the ciphertext ever leaves the browser.
(function () {
  "use strict";
  var form = document.querySelector("form[data-runner-settings]");
  if (!form) return;

  var control = function (key) { return form.querySelector('[name="v_' + key + '"]'); };
  var valueOf = function (key) {
    var el = control(key);
    if (!el) return undefined;
    return el.type === "checkbox" ? el.checked : el.value;
  };
  var update = function () {
    form.querySelectorAll("[data-when]").forEach(function (field) {
      var rules = JSON.parse(field.getAttribute("data-when"));
      var ok = rules.some(function (r) { return valueOf(r.key) === r.value; });
      field.classList.toggle("hidden", !ok);
    });
  };
  form.addEventListener("change", update);
  update();

  // a mode fills in its settings (saved only with the Save button)
  form.querySelectorAll("[data-mode]").forEach(function (button) {
    button.addEventListener("click", function () {
      var values = JSON.parse(button.getAttribute("data-values") || "{}");
      Object.keys(values).forEach(function (key) {
        var el = control(key);
        if (!el || el.disabled) return;
        if (el.type === "checkbox") el.checked = ["igen", "true", "be", "1"].indexOf(String(values[key])) >= 0;
        else el.value = values[key];
      });
      form.querySelectorAll("[data-mode]").forEach(function (b) { b.classList.toggle("on", b === button); });
      update();
    });
  });

  var secrets = form.querySelectorAll("[data-secret]");
  var pem = form.getAttribute("data-public-key") || "";
  var subtle = window.crypto && window.crypto.subtle;
  if (!subtle || !pem) {
    secrets.forEach(function (input) { input.disabled = true; });
    var note = form.querySelector("[data-crypto-missing]");
    if (note && secrets.length) note.hidden = false;
    return;
  }

  var toBase64 = function (buffer) {
    var bytes = new Uint8Array(buffer), text = "";
    for (var i = 0; i < bytes.length; i++) text += String.fromCharCode(bytes[i]);
    return btoa(text);
  };
  var keyPromise = null;
  var publicKey = function () {
    if (!keyPromise) {
      var body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
      var der = Uint8Array.from(atob(body), function (c) { return c.charCodeAt(0); });
      keyPromise = subtle.importKey("spki", der.buffer, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"]);
    }
    return keyPromise;
  };

  var sending = false;
  form.addEventListener("submit", function (event) {
    if (sending) return;
    var filled = Array.prototype.filter.call(secrets, function (input) { return input.value.trim() !== ""; });
    if (!filled.length) return;
    event.preventDefault();
    publicKey().then(function (key) {
      return Promise.all(filled.map(function (input) {
        var data = new TextEncoder().encode(input.value.trim());
        return subtle.encrypt({ name: "RSA-OAEP" }, key, data).then(function (cipher) {
          form.querySelector('[name="enc_' + input.getAttribute("data-secret") + '"]').value = toBase64(cipher);
          input.value = "";
        });
      }));
    }).then(function () {
      sending = true;
      form.submit();
    }).catch(function () {
      var note = form.querySelector("[data-crypto-missing]");
      if (note) { note.textContent = "A kulcsot nem sikerült titkosítani; töltsd újra az oldalt."; note.hidden = false; }
    });
  });
})();
