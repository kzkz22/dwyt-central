// Reading a summary aloud with the browser's own voice (decision 19.7). The text comes from the page as short plain
// pieces (render.speech_chunks), spoken one after the other: long utterances stop half-way in some browsers, and
// pausing is done by stopping and starting again at the same piece, because pause() is unreliable on phones.
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
  const play = q("[data-speak-play]"), label = q("[data-speak-label]"), stopButton = q("[data-speak-stop]");
  const rateSel = q("[data-speak-rate]"), voiceSel = q("[data-speak-voice]"), voicesBox = q("[data-speak-voices]");
  const status = q("[data-speak-status]"), noVoice = q("[data-speak-novoice]");
  const load = (k) => { try { return localStorage.getItem(k); } catch (err) { return null; } };
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch (err) { /* blocked */ } };

  let state = "idle";  // idle | speaking | paused
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
    label.textContent = state === "speaking" ? "Szünet" : state === "paused" ? "Folytatás" : "Felolvasás";
    play.setAttribute("aria-pressed", String(state === "speaking"));
    stopButton.hidden = state === "idle";
    status.textContent = state === "idle" ? "" : `${index + 1} / ${chunks.length}`;
  }

  function speakFrom(i) {
    const mine = ++generation;
    synth.cancel();
    index = i;
    if (index >= chunks.length) { state = "idle"; index = 0; show(); return; }
    const u = new SpeechSynthesisUtterance(chunks[index]);
    u.lang = "hu-HU";
    const v = voice();
    if (v) u.voice = v;
    u.rate = parseFloat(rateSel.value) || 1;
    u.onend = () => { if (mine === generation && state === "speaking") speakFrom(index + 1); };
    u.onerror = (e) => {
      if (mine !== generation || e.error === "interrupted" || e.error === "canceled") return;
      state = "idle";
      show();
      status.textContent = "A felolvasás megszakadt.";
    };
    state = "speaking";
    show();
    synth.speak(u);
  }

  function stop() {
    generation++;
    synth.cancel();
    state = "idle";
    index = 0;
    show();
  }

  play.addEventListener("click", () => {
    if (state === "speaking") { generation++; synth.cancel(); state = "paused"; show(); }
    else speakFrom(state === "paused" ? index : 0);
  });
  stopButton.addEventListener("click", stop);
  rateSel.addEventListener("change", () => { save("dwyt-rate", rateSel.value); if (state === "speaking") speakFrom(index); });
  voiceSel.addEventListener("change", () => { save("dwyt-voice", voiceSel.value); if (state === "speaking") speakFrom(index); });
  window.addEventListener("pagehide", () => { generation++; synth.cancel(); });

  fillVoices();
  if (typeof synth.addEventListener === "function") synth.addEventListener("voiceschanged", fillVoices);
  else synth.onvoiceschanged = fillVoices;
  box.hidden = false;
  show();
})();
