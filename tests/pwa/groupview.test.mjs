// 목록 보기 전환(세션 | 👥 팀 | 파일) — 키 'groups' 는 옛 이름 그대로(기기에 기억된 값 호환). 팀 칸은 team.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];

test("setListView: 기기에 기억, 이상한 값은 세션 보기", async () => {
  const ctx = await load();
  ev(ctx, "setListView('groups')");
  assert.equal(ev(ctx, "LIST_VIEW"), "groups");
  assert.equal(ev(ctx, "LS.getItem('sm_list_view')"), "groups");
  ev(ctx, "setListView('x')");
  assert.equal(ev(ctx, "LIST_VIEW"), "sessions");
});

test("보기 전환·검색은 한 머리(.lhead)에 묶여 스크롤해도 위에 남는다", async () => {
  const ctx = await load();
  const pl = stubEl("div"), pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneList" ? pl : s === "#paneDetail" ? pd : stubEl());
  ev(ctx, "T = Object.assign({}, T, { list: async () => ({sessions: []}) }); setListView('sessions')");
  await ev(ctx, "loadList()");
  const head = pl.children[0];
  assert.equal(String(head.className), "lhead");
  assert.equal(JSON.stringify(head.children.map((k) => String(k.className))), JSON.stringify(["lview", "search"]));
});
