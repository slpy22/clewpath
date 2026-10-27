// 다른 PC(릴레이) 터미널이 PC 화면에서 위쪽 절반만 그려지던 일(사장님 2026-10-27).
// 탭 칸(.thost)은 CSS 로 터미널 영역을 꽉 채운다(position:absolute;inset:0). 모바일 터치 UX 가 그 칸에 position:relative 를
// 직접 넣어 CSS 를 덮어썼다 — 칸 높이가 xterm 기본 24줄에 멈추고 fit 도 24줄로 맞췄다(폰은 24줄이 화면과 비슷해 드러나지 않았다).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

test("릴레이 터미널의 탭 칸은 영역을 채우는 CSS 배치를 덮어쓰지 않는다", async () => {
  const ctx = await load();
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app);
  ctx.document._register("head", stubEl("span")); ctx.document._register("paneList", stubEl("div"));
  app.setAttribute("data-view", "list");
  ev(ctx, `innerWidth = 1400; globalThis.__tc = [];
    T.terminal = (sid, cb, opts) => { const h = { sid, cb, opts, close() {}, send() {} }; __tc.push(h); return h; };
    T.api = async () => ({});
    globalThis.MutationObserver = class { observe() {} disconnect() {} };
    SESSIONS = [{ session_id: '${A}', title: 'A', live_terminal: true, cwd: 'E:/p' }];`);
  ctx.ensurePriv = async () => ({});
  ctx.confirm = () => true;
  await ctx.loadTerminal(ev(ctx, "SESSIONS[0]"), false); await tick(350);
  const host = ev(ctx, `termView.tabs['${A}'].host`);
  assert.ok(host, "릴레이 탭 칸이 만들어졌다");
  assert.ok(ev(ctx, `!!termView.tabs['${A}'].ux`), "모바일 터치 UX 가 붙었다(이 경로가 칸 배치를 건드리던 곳)");
  assert.equal(String(host.className), "thost on");
  assert.equal(host.style.position || "", "", "칸에 직접 넣은 position 이 CSS 의 absolute·inset:0 을 덮어쓰면 높이가 24줄에 멈춘다");
});
