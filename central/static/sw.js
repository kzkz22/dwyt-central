// dwyt's service worker (round 8, decision 19.2): it makes the site installable on the phone (so it can be a share
// target). It caches nothing: the library is private and always comes from the server. Only when the phone is
// offline does a page load get a short Hungarian note instead of the browser's error page.
"use strict";
const OFFLINE = '<!doctype html><html lang="hu"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<title>Nincs kapcsolat · dwyt</title><body style="font-family:system-ui,sans-serif;padding:24px;line-height:1.5">' +
  "<h1>Nincs kapcsolat</h1><p>A dwyt most nem érhető el. Ha VPN-en át használod, kapcsold be, majd próbáld újra.</p></body></html>";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, {status: 503, headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
