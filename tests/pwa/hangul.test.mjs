// 모바일 한글 입력창 — iOS 에서 xterm 이 조합을 깨 자모가 낱낱이 PTY 로 가던 문제(2026-10-11 iPhone Chrome).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("isHangulJamo: 낱자 자모만 참, 완성형·영문·제어문자는 거짓", async () => {
  const ctx = await load();
  for (const d of ["ㅇ", "ㅏ", "ㅇㅏ", "ᄒ"]) assert.equal(ev(ctx, `isHangulJamo(${JSON.stringify(d)})`), true, d);
  for (const d of ["안", "a", "\r", "\x1b[A", "", "ㅇa"]) assert.equal(ev(ctx, `isHangulJamo(${JSON.stringify(d)})`), false, d);
});

test("termComposer: Enter 전송은 글자와 \\r 을 따로, '넣기'는 글자만, 빈 칸 Enter 는 \\r 만", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__s = []; globalThis.__c = termComposer({ send: (d) => globalThis.__s.push(d) });");
  ev(ctx, "__c.open(); __c.input.value = '안녕하세요'; __c.submit(true);");
  assert.equal(ev(ctx, "JSON.stringify(__s)"), JSON.stringify(["안녕하세요"]));           // \r 은 뒤따라 따로
  await wait(120);
  assert.equal(ev(ctx, "JSON.stringify(__s)"), JSON.stringify(["안녕하세요", "\r"]));
  assert.equal(ev(ctx, "__c.input.value"), "");
  ev(ctx, "__c.input.value = '한글'; __c.submit(false);");
  await wait(120);
  assert.equal(ev(ctx, "JSON.stringify(__s.slice(2))"), JSON.stringify(["한글"]));
  ev(ctx, "__c.submit(true);");
  await wait(20);
  assert.equal(ev(ctx, "JSON.stringify(__s.slice(3))"), JSON.stringify(["\r"]));
});

test("attachMobileTermUX: 자모 입력은 PTY 로 안 보내고 입력창을 열어 옮긴다, 상태는 기기에 기억", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__s = []; globalThis.__term = { options: { fontSize: 13 }, buffer: { active: { type: 'normal', viewportY: 0, baseY: 0 } },"
    + " onScroll: () => ({ dispose() {} }), focus() {}, scrollLines() {}, scrollToBottom() {} };"
    + "globalThis.__ux = attachMobileTermUX(el('div'), __term, { send: (d) => __s.push(d), onFont() {} });");
  assert.equal(ev(ctx, "__ux.composer.isOpen()"), false);
  assert.equal(ev(ctx, "__ux.intercept('a')"), false);       // 영문은 그대로 xterm 경로
  assert.equal(ev(ctx, "__ux.intercept('ㅇ')"), true);
  assert.equal(ev(ctx, "__ux.intercept('ㅏ')"), true);
  assert.equal(ev(ctx, "__ux.composer.isOpen()"), true);
  assert.equal(ev(ctx, "__ux.composer.input.value"), "ㅇㅏ");
  assert.equal(ev(ctx, "__s.length"), 0);
  assert.equal(ev(ctx, "LS.getItem('sm_term_compose')"), "1");
  ev(ctx, "__ux.composer.close()");
  assert.equal(ev(ctx, "LS.getItem('sm_term_compose')"), "0");
  // 입력창 행이 보조키 행 위에 있다
  assert.equal(ev(ctx, "__ux.bar.children[0] === __ux.composer.row"), true);
});
