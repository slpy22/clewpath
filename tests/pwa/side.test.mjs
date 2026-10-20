// 목록 칸 접기·펼치기(데스크톱, 2026-10-20): 헤더 ◧ 버튼 · 이 브라우저에 기억 · Ctrl+Shift+B(입력칸에선 무시).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev } from "./harness.mjs";

// 스텁 문서는 등록한 id 만 같은 엘리먼트를 돌려준다 — #app·#btn-side 를 부트 전에 고정
const fix = (pre) => (sb) => {
  for (const id of ["app", "btn-side"]) sb.document._register(id, sb.document.createElement(id === "app" ? "div" : "button"));
  if (pre) pre(sb);
};

test("◧ 누르면 접히고 기억되며, 다시 열면 접힌 채로 시작 · 다시 누르면 펼침", async () => {
  const ctx = await load({ setup: fix() });
  assert.equal(ev(ctx, "$('#app').classList.contains('side-off')"), false);
  ev(ctx, "$('#btn-side').onclick()");
  assert.equal(ev(ctx, "$('#app').classList.contains('side-off')"), true);
  assert.equal(ev(ctx, "localStorage.getItem('sm_side_off')"), "1");
  assert.equal(ev(ctx, "$('#btn-side').getAttribute('aria-pressed')"), "true");
  const again = await load({ setup: fix((sb) => sb.localStorage.setItem("sm_side_off", "1")) });
  assert.equal(ev(again, "$('#app').classList.contains('side-off')"), true, "새로 열어도 접힌 상태");
  ev(again, "$('#btn-side').onclick()");
  assert.equal(ev(again, "$('#app').classList.contains('side-off')"), false);
  assert.equal(ev(again, "localStorage.getItem('sm_side_off')"), null);
});

test("CSS: 접기는 데스크톱 2단에서만, 상세 칸이 전체 폭", () => {
  const html = readFileSync(new URL("../../pwa/index.html", import.meta.url), "utf-8");
  const wide = html.slice(html.indexOf("@media (min-width:880px){\n    body{background:var(--surface2)}"));
  // 숨긴 목록·구분선은 격자에서 빠진다 → 열을 하나만 둬야 상세 칸이 첫 열(폭 0)로 밀려 사라지지 않는다(0.27.2 버그)
  assert.ok(wide.includes("#app.side-off #main{grid-template-columns:minmax(0,1fr)}"));
  assert.ok(!wide.includes("grid-template-columns:0 0 1fr"));
  assert.ok(wide.includes("#app.side-off[data-view] #paneList,#app.side-off #resizer{display:none!important}"));
  assert.ok(html.includes("#btn-side{display:none}"), "폰에서는 버튼을 숨긴다");
});
