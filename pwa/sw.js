/* ClewPath 서비스워커 - 웹푸시 수신 전용.
   페이지 캐싱/오프라인은 하지 않는다(앱은 서버가 항상 최신을 서빙).
   페이로드는 Web Push 프로토콜로 이미 종단 암호화되어 도착한다. */
"use strict";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch (_) { p = { title: "ClewPath", body: "" }; }
  const title = p.title || "ClewPath";
  e.waitUntil(self.registration.showNotification(title, {
    body: p.body || "",
    tag: p.tag || undefined,          // 같은 (세션,종류)는 갱신 표시(중복 방지)
    renotify: false,
    icon: "vendor/icon-192.png",      // 없으면 브라우저 기본 아이콘
    badge: "vendor/icon-192.png",
    data: { sid: p.sid || "", kind: p.kind || "" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const sid = (e.notification.data && e.notification.data.sid) || "";
  // SW 파일 위치가 앱 루트다: 로컬=/sw.js -> /,  릴레이=/relay/sw.js -> /relay/app
  const base = self.location.pathname.replace(/sw\.js$/, "");
  const url = (base === "/" ? "/" : base + "app") + (sid ? "#open=" + encodeURIComponent(sid) : "");
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.indexOf(self.location.origin) === 0) {
        try { await w.focus(); w.postMessage({ type: "open-session", sid }); return; } catch (_) {}
      }
    }
    await self.clients.openWindow(url);
  })());
});
