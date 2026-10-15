// Reading a summary aloud with the browser's own voice (decisions 19.7, 19.8). The text comes from the page as short
// plain pieces (render.speech_chunks), spoken one after the other: long utterances stop half-way in some browsers, and
// pausing is done by stopping and starting again at the same piece, because pause() is unreliable on phones.
// The slider and the previous / next buttons move between pieces; the place is remembered per summary.
(function () {
  "use strict";
  const box = document.querySelector("[data-speak]");
  const data = document.getElementById("speech-text");
  const synth = window.speechSynthesis;
  if (!box || !data || !synth || typeof SpeechSynthesisUtterance === "undefined") return;
  let chunks = [];
  try { chunks = JSON.parse(data.textContent); } catch (err) { return; }
  if (!chunks.length) return;

  const q = (sel) => box.querySelector(sel);
  const play = q("[data-speak-play]"), label = q("[data-speak-label]"), toStart = q("[data-speak-stop]");
  const prev = q("[data-speak-prev]"), next = q("[data-speak-next]"), seek = q("[data-speak-seek]");
  const rateSel = q("[data-speak-rate]"), voiceSel = q("[data-speak-voice]"), voicesBox = q("[data-speak-voices]");
  const status = q("[data-speak-status]"), now = q("[data-speak-now]"), noVoice = q("[data-speak-novoice]");
  const load = (k) => { try { return localStorage.getItem(k); } catch (err) { return null; } };
  const save = (k, v) => { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (err) { /* blocked */ } };
  const placeKey = "dwyt-speak-" + (box.dataset.speakKey || "");

  let state = "idle";  // idle (at the start) | speaking | paused (somewhere in the text)
  let index = 0;
  let generation = 0;  // a cancelled utterance's late "end" event must not move on
  let voices = [];

  // the best Hungarian voices first: the natural / online / enhanced ones sound much better than the old ones
  const score = (v) => (/natural|online|neural|enhanced|premium|google/i.test(v.name) ? 0 : 1) + (v.localService ? 0.5 : 0);
  function fillVoices() {
    voices = synth.getVoices().filter((v) => /^hu(\b|[-_])/i.test(v.lang)).sort((a, b) => score(a) - score(b));
    const saved = load("dwyt-voice");
    voiceSel.textContent = "";
    voices.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.name;
      opt.textContent = v.name.replace(/^Microsoft /, "").replace(/ - Hungarian \(Hungary\)/, "");
      if (v.name === saved) opt.selected = true;
      voiceSel.appendChild(opt);
    });
    voicesBox.hidden = voices.length < 2;
    noVoice.hidden = voices.length > 0 || !synth.getVoices().length;
  }
  const voice = () => voices.find((v) => v.name === voiceSel.value) || voices[0] || null;

  const savedRate = load("dwyt-rate");
  if (savedRate && rateSel.querySelector(`option[value="${savedRate}"]`)) rateSel.value = savedRate;

  function show() {
    const speaking = state === "speaking";
    label.textContent = speaking ? "Szünet" : state === "paused" ? "Folytatás" : "Felolvasás";
    play.setAttribute("aria-label", label.textContent);
    play.classList.toggle("playing", speaking);
    toStart.hidden = state === "idle";
    seek.value = String(index + 1);
    status.textContent = `${index + 1} / ${chunks.length}`;
    now.hidden = state === "idle";
    now.textContent = chunks[index] || "";
    prev.disabled = index === 0;
    next.disabled = index >= chunks.length - 1;
    save(placeKey, state === "idle" ? null : String(index));
  }

  function halt() { generation++; synth.cancel(); }

  function speakFrom(i) {
    halt();
    const mine = generation;
    index = Math.max(0, i);
    if (index >= chunks.length) { state = "idle"; index = 0; show(); return; }
    const u = new SpeechSynthesisUtterance(chunks[index]);
    u.lang = "hu-HU";
    const v = voice();
    if (v) u.voice = v;
    u.rate = parseFloat(rateSel.value) || 1;
    u.onend = () => { if (mine === generation && state === "speaking") speakFrom(index + 1); };
    u.onerror = (e) => {
      if (mine !== generation || e.error === "interrupted" || e.error === "canceled") return;
      state = "paused";
      show();
      status.textContent = "A felolvasás megszakadt.";
    };
    state = "speaking";
    show();
    synth.speak(u);
  }

  // jump to a piece: keep speaking if it was speaking, else just stand there (Play starts from it)
  function goTo(i) {
    const target = Math.min(chunks.length - 1, Math.max(0, i));
    if (state === "speaking") { speakFrom(target); return; }
    index = target;
    state = "paused";
    show();
  }

  play.addEventListener("click", () => {
    if (state === "speaking") { halt(); state = "paused"; show(); }
    else speakFrom(index);
  });
  toStart.addEventListener("click", () => { halt(); state = "idle"; index = 0; show(); });
  prev.addEventListener("click", () => goTo(index - 1));
  next.addEventListener("click", () => goTo(index + 1));
  seek.addEventListener("input", () => {  // while dragging: show where it would start
    status.textContent = `${seek.value} / ${chunks.length}`;
    now.hidden = false;
    now.textContent = chunks[parseInt(seek.value, 10) - 1] || "";
  });
  seek.addEventListener("change", () => goTo(parseInt(seek.value, 10) - 1));
  rateSel.addEventListener("change", () => { save("dwyt-rate", rateSel.value); if (state === "speaking") speakFrom(index); });
  voiceSel.addEventListener("change", () => { save("dwyt-voice", voiceSel.value); if (state === "speaking") speakFrom(index); });
  window.addEventListener("pagehide", () => {
    if (state === "speaking") { state = "paused"; show(); }
    halt();
  });

  // where the user stopped last time on this summary
  const place = parseInt(load(placeKey) || "", 10);
  if (place > 0 && place < chunks.length) { index = place; state = "paused"; }

  fillVoices();
  if (typeof synth.addEventListener === "function") synth.addEventListener("voiceschanged", fillVoices);
  else synth.onvoiceschanged = fillVoices;
  box.hidden = false;
  show();
})();
