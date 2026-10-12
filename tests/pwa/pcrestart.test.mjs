// PC(Host) 재기동 뒤 터미널 자동 재연결 — 릴레이는 살아 있고 PC 만 다시 뜬 경우(2026-10-12: 화면 복귀 때까지 끊겨 있었음).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const tick = () => new Promise((r) => setTimeout(r, 0));
function fakeWs(ctx) {
  const all = [];
  ctx.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; all.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close(code) { this.readyState = 3; this.onclose && this.onclose({ code: code || 1000 }); }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    push(m) { this.onmessage({ data: JSON.stringify(m) }); }
  };
  return all;
}

test("agent_online 이 오면 끊긴 전경 터미널을 다시 붙인다", async () => {
  const ctx = await load();
  const sockets = fakeWs(ctx);
  const p = ev(ctx, "conn.connect('room1', 'tok')"); await tick();
  const ws = sockets.at(-1); ws.open(); ws.push({ type: "hello", peer_present: true, cid: "c" }); await p; await tick();
  ev(ctx, "globalThis.__rt = 0; reattachForegroundTab = () => { globalThis.__rt++; }; refreshVer = () => {};");
  ws.push({ type: "peer", event: "agent_offline" });
  ws.push({ type: "peer", event: "agent_online" });
  assert.equal(ev(ctx, "__rt"), 0, "PC 가 준비될 틈을 두고");
  await new Promise((r) => setTimeout(r, 1700));
  assert.equal(ev(ctx, "__rt"), 1);
  ev(ctx, "conn._stopped = true"); ws.close();
});

test("reattachForegroundTab: claude 가 끝난(ended) 탭은 자동으로 새로 띄우지 않는다", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__sw = []; switchTab = (k) => globalThis.__sw.push(k);"
    + "TABS.list = [{key:'k1', s:{session_id:'a'}}]; TABS.active = 'k1'; termView.disconnected = true;"
    + "termView.tabs = { k1: { ended: true } };");
  ev(ctx, "reattachForegroundTab()");
  assert.equal(ev(ctx, "__sw.length"), 0);
  ev(ctx, "termView.tabs = { k1: { ended: false } }; reattachForegroundTab()");
  assert.equal(ev(ctx, "__sw.length"), 1);
  ev(ctx, "termView.disconnected = false; termView.tabs = {}");
});
