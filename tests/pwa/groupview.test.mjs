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

// 검색 지우기 ✕(2026-10-29 사장님) — 글자가 있을 때만 보이고, 누르면 비우고 바로 다시 그린다
const byCls = (n, c) => flat(n).find((x) => String(x.className).split(" ").includes(c));
async function listBoot(view) {
  const ctx = await load();
  const pl = stubEl("div"), pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneList" ? pl : s === "#paneDetail" ? pd : stubEl());
  ev(ctx, `T = Object.assign({}, T, { list: async () => ({sessions: []}) }); setListView('${view}')`);
  return { ctx, pl };
}

test("세션 목록 검색 ✕: 글자가 있을 때만 보이고, 누르면 비운 뒤 바로 다시 그린다", async () => {
  const { ctx, pl } = await listBoot("sessions");
  await ev(ctx, "loadList('알파')");
  const head = pl.children[0], sb = byCls(head, "search");
  const inp = byCls(sb, "sbox").children[0], x = byCls(sb, "sclr");
  assert.equal(inp.value, "알파");
  assert.ok(!x.classList.contains("hidden"), "글자가 있으면 보인다");
  ev(ctx, "globalThis.__rows = []; renderRows = (w, q) => __rows.push(q)");
  x.onclick({ preventDefault() {} });
  assert.equal(inp.value, "");
  assert.ok(x.classList.contains("hidden"), "비우면 숨는다");
  assert.deepEqual([...ev(ctx, "__rows")], [""], "기다리지 않고 바로 다시 그린다");
  inp.value = "b"; inp.oninput({});
  assert.ok(!x.classList.contains("hidden"), "다시 치면 보인다");
});

test("파일 보기: 탐색기 머리·찾기 칸이 고정 머리(.lhead)에 실리고 ✕ 로 찾기를 지운다", async () => {
  const { ctx, pl } = await listBoot("files");
  ev(ctx, "FB.q = 'zzz'");
  await ev(ctx, "loadList()");
  await new Promise((r) => setTimeout(r, 0));
  const head = pl.children[0];
  assert.equal(String(head.className), "lhead");
  const box = byCls(head, "fbhead");
  assert.ok(box, "탐색기 머리 자리가 .lhead 안에 있다");
  assert.deepEqual(box.children.map((k) => String(k.className)), ["fbex", "fbfilter"]);
  const fw = box.children[1], x = byCls(fw, "sclr");
  assert.ok(!x.classList.contains("hidden"));
  x.onclick({ preventDefault() {} });
  assert.equal(ev(ctx, "FB.q"), "");
  assert.ok(x.classList.contains("hidden"));
  // 정렬을 바꿔 다시 그려도 머리는 같은 자리에서 새로 그려진다(겹쳐 쌓이지 않음)
  await ev(ctx, "renderFbExplorer(FB.wrap)");
  assert.deepEqual(box.children.map((k) => String(k.className)), ["fbex", "fbfilter"]);
});
