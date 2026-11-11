// PC 웹 터미널(terminal.html) 글자 크기 조절 — 사장님 요청(2026-11-11 "글자가 너무 작아 눈이 아프다").
// 머리 줄 A−/A+ 단추와 터미널 위 Ctrl+휠로 8~28 사이에서 바꾸고, 기기(브라우저)별로 기억한다.
// Ctrl+- 같은 키는 가로채지 않는다(터미널에서 Ctrl+_ 와 같은 글자라 claude 의 되돌리기를 막는다).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import { stubEl, makeSandbox, extractScript } from "./harness.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const TERMINAL = join(here, "..", "..", "session_manager", "static", "terminal.html");
const A = "aaaaaaaa-1111-2222-3333-444444444444";

function boot({ saved } = {}) {
  const sandbox = makeSandbox();
  Object.assign(sandbox.location, { pathname: "/terminal", search: `?id=${A}&skip=1`,
    origin: "http://127.0.0.1:5100", host: "127.0.0.1:5100", protocol: "http:" });
  if (saved !== undefined) sandbox.localStorage.setItem("sm_term_fs", String(saved));
  const els = {};
  for (const k of ["sid", "dot", "status", "term", "delfork", "cwd", "fsUp", "fsDown", "fsVal"]) {
    els[k] = stubEl("div"); sandbox.document._register(k, els[k]);
  }
  // 터미널 칸의 휠 처리기를 잡아 두고 직접 부른다
  const wrap = stubEl("div"); const listeners = {};
  wrap.addEventListener = (t, f, o) => { (listeners[t] ||= []).push({ f, o }); };
  sandbox.document._register("termwrap", wrap); els.termwrap = wrap;
  let ws;
  sandbox.WebSocket = class { constructor(url) { this.url = url; this.readyState = 1; this.sent = []; ws = this; } send(s) { this.sent.push(s); } close() {} };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(extractScript(readFileSync(TERMINAL, "utf-8")), ctx, { filename: "terminal.html" });
  const term = vm.runInContext("term", ctx);
  const fs = () => vm.runInContext("fontSize", ctx);
  const wheel = (e) => {
    let prevented = false;
    const ev = { ctrlKey: false, deltaY: 0, deltaMode: 0, preventDefault() { prevented = true; }, stopPropagation() {}, ...e };
    for (const { f } of listeners.wheel || []) f(ev);
    return prevented;
  };
  return { sandbox, ctx, els, term, fs, wheel, listeners, ws: () => ws };
}

test("terminal.html: 저장한 글자 크기가 없으면 13, 있으면 그 크기로 시작하고 표시한다", () => {
  const a = boot();
  assert.equal(a.fs(), 13);
  assert.equal(a.els.fsVal.textContent, "13");
  const b = boot({ saved: 18 });
  assert.equal(b.fs(), 18);
  assert.equal(b.els.fsVal.textContent, "18");
  const c = boot({ saved: 99 });                     // 범위 밖 저장값은 무시
  assert.equal(c.fs(), 13);
});

test("terminal.html: A+/A− 단추는 한 칸씩 바꾸고 기억하며, 8~28 밖으로는 안 간다", () => {
  const t = boot();
  t.els.fsUp.onclick();
  assert.equal(t.fs(), 14);
  assert.equal(t.term.options.fontSize, 14);
  assert.equal(t.els.fsVal.textContent, "14");
  assert.equal(t.sandbox.localStorage.getItem("sm_term_fs"), "14");
  t.els.fsDown.onclick(); t.els.fsDown.onclick();
  assert.equal(t.fs(), 12);
  for (let i = 0; i < 30; i++) t.els.fsUp.onclick();
  assert.equal(t.fs(), 28);
  for (let i = 0; i < 40; i++) t.els.fsDown.onclick();
  assert.equal(t.fs(), 8);
  assert.equal(t.sandbox.localStorage.getItem("sm_term_fs"), "8");
});

test("terminal.html: 글자 크기가 바뀌면 칸 수를 다시 맞춰 claude 에 크기를 보낸다", () => {
  const t = boot();
  t.ws().onopen();
  const resizes = () => t.ws().sent.map((s) => JSON.parse(s)).filter((m) => m.type === "resize");
  const before = resizes().length;
  t.term.cols = 100; t.term.rows = 30;               // 글자가 커지면 fit 이 칸 수를 줄인 것처럼
  t.els.fsUp.onclick();
  assert.equal(resizes().length, before + 1);
  assert.deepEqual(resizes().at(-1), { type: "resize", cols: 100, rows: 30 });
});

test("terminal.html: 터미널 위 Ctrl+휠로 크기를 바꾸고 브라우저 확대는 막는다, Ctrl 없는 휠은 건드리지 않는다", () => {
  const t = boot();
  assert.ok((t.listeners.wheel || []).some(({ o }) => o && o.passive === false), "preventDefault 를 쓰려면 passive:false");
  assert.equal(t.wheel({ deltaY: -100 }), false, "Ctrl 없는 휠은 그대로 스크롤");
  assert.equal(t.fs(), 13);
  assert.equal(t.wheel({ ctrlKey: true, deltaY: -100 }), true);   // 마우스 휠 한 칸 위 = 크게
  assert.equal(t.fs(), 14);
  t.wheel({ ctrlKey: true, deltaY: 100 }); t.wheel({ ctrlKey: true, deltaY: 100 });
  assert.equal(t.fs(), 12);
  // 터치패드 핀치처럼 작은 값이 여러 번 오면 모아서 한 칸
  for (let i = 0; i < 4; i++) t.wheel({ ctrlKey: true, deltaY: -10 });
  assert.equal(t.fs(), 12, "40 만큼은 아직 한 칸이 안 됨");
  t.wheel({ ctrlKey: true, deltaY: -10 });
  assert.equal(t.fs(), 13);
  t.wheel({ ctrlKey: true, deltaY: 3, deltaMode: 1 });          // 줄 단위 휠(파이어폭스)
  assert.equal(t.fs(), 12);
});
