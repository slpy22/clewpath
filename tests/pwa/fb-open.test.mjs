// 파일 탭 팝업 메뉴 — 이 PC 에서 열기(2026-11-11 사장님): 줄 오른쪽 클릭(또는 메뉴 키) → 탐색기에서 보기·기본 프로그램으로 열기.
// PC 로컬 화면에서만. 실행 파일·🔒 보호 파일은 기본 프로그램 열기를 보내지 않는다(서버도 거절).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
const RK = "f:\\p\\proj";
const id = (rel) => RK + "|" + rel;
const LOCAL = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const DIRS = {
  "": { root: "F:\\p\\proj", root_name: "proj", path: "", truncated: false, entries: [
    { name: "docs", type: "dir", mtime: 1 }, { name: ".env", type: "file", size: 8, blocked: true, mtime: 1 },
    { name: "setup.EXE", type: "file", size: 9, mtime: 1 }, { name: "report.xlsx", type: "file", size: 20, mtime: 1 }] },
};

async function boot(location) {
  const ctx = await load(location ? { location } : {});
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  ctx.__DIRS = DIRS;
  ev(ctx, `globalThis.__calls = []; globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    SESSIONS = [{session_id:'s1', title:'첫 세션', cwd:'F:\\\\p\\\\proj', mtime:10}];
    T = { api: async (v, p, o) => { __calls.push([v, p, o]);
      if (p.endsWith('/files')) return JSON.parse(JSON.stringify(__DIRS[(o.query||{}).path || '']));
      if (p.endsWith('/file/open-external')) { if (o.body.path === 'gone.txt') { const e = new Error('HTTP 404'); e.status = 404; e.json = {error:'not_found'}; throw e; } return {ok:true}; }
      throw new Error('unexpected ' + p); } };
    globalThis.__w = el('div');`);
  await ev(ctx, "renderFbExplorer(__w)");
  await ev(ctx, `fbToggle(${JSON.stringify(id(""))})`);
  return ctx;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const rowsOf = (ctx) => flat(ev(ctx, "FB.tree")).filter((n) => String(n.className).split(" ").includes("fbrow"));
const rowOf = (ctx, p) => rowsOf(ctx).find((r) => r.dataset.path === p);
const menuItems = (ctx) => flat(ev(ctx, "POP.box")).filter((n) => String(n.className).split(" ").includes("popi"));
const label = (b) => flat(b).map((x) => String(x.textContent || "")).join(" ");
const rclick = (row) => { let prevented = false; row.oncontextmenu({ clientX: 100, clientY: 50, preventDefault() { prevented = true; } }); return prevented; };
const opens = (ctx) => JSON.parse(JSON.stringify(ev(ctx, "__calls"))).filter((c) => c[1].endsWith("/file/open-external"));   // vm 밖으로 값만

test("PC 로컬: 파일 오른쪽 클릭 → 탐색기에서 보기·기본 프로그램으로 열기, 누르면 그 세션으로 요청", async () => {
  const ctx = await boot(LOCAL);
  assert.equal(ev(ctx, "MODE"), "local");
  const row = rowOf(ctx, id("report.xlsx"));
  assert.ok(rclick(row), "브라우저 기본 메뉴는 막는다");
  assert.equal(ev(ctx, "FB.sel"), id("report.xlsx"), "오른쪽 클릭한 줄이 선택된다");
  const items = menuItems(ctx);
  assert.deepEqual(items.map((b) => label(b).includes("탐색기에서 보기") || label(b).includes("기본 프로그램으로 열기")), [true, true]);
  items[1].onclick({ stopPropagation() {} }); await tick();
  assert.deepEqual(opens(ctx).at(-1).slice(0, 2), ["POST", "/api/sessions/s1/file/open-external"]);
  assert.deepEqual(opens(ctx).at(-1)[2].body, { path: "report.xlsx", action: "open" });
  rclick(rowOf(ctx, id("report.xlsx"))); menuItems(ctx)[0].onclick({ stopPropagation() {} }); await tick();
  assert.deepEqual(opens(ctx).at(-1)[2].body, { path: "report.xlsx", action: "reveal" });
});

test("PC 로컬: 폴더·작업 폴더(뿌리)는 '탐색기에서 열기' 하나", async () => {
  const ctx = await boot(LOCAL);
  rclick(rowOf(ctx, id("docs")));
  let items = menuItems(ctx);
  assert.equal(items.length, 1); assert.ok(label(items[0]).includes("탐색기에서 열기"));
  items[0].onclick({ stopPropagation() {} }); await tick();
  assert.deepEqual(opens(ctx).at(-1)[2].body, { path: "docs", action: "reveal" });
  rclick(rowOf(ctx, id("")));
  items = menuItems(ctx);
  assert.equal(items.length, 1);
  items[0].onclick({ stopPropagation() {} }); await tick();
  assert.deepEqual(opens(ctx).at(-1)[2].body, { path: "", action: "reveal" });
});

test("PC 로컬: 실행 파일·🔒 보호 파일은 기본 프로그램 열기를 보내지 않고 이유만 알린다, 실패는 문구로", async () => {
  const ctx = await boot(LOCAL);
  for (const [name, why] of [["setup.EXE", "실행 파일"], [".env", "보호 파일"]]) {
    rclick(rowOf(ctx, id(name)));
    const items = menuItems(ctx);
    assert.ok(label(items[1]).includes(why), name + " 메뉴에 이유 표시");
    const n = opens(ctx).length;
    items[1].onclick({ stopPropagation() {} }); await tick();
    assert.equal(opens(ctx).length, n, name + " 은 요청하지 않는다");
    assert.ok(ev(ctx, "__toasts").at(-1).includes(why));
  }
  await ev(ctx, `fbExternal(${JSON.stringify(id("gone.txt"))}, 'open')`);
  assert.ok(ev(ctx, "__toasts").at(-1).includes("파일이 없습니다"));
  assert.ok(ev(ctx, "fbErrText({error:'runnable'})").includes("실행 파일"));
  assert.ok(ev(ctx, "fbErrText({error:'local_only'})").includes("이 PC 화면에서만"));
});

test("PC 로컬: 메뉴 키(또는 Shift+F10)로도 선택한 줄의 메뉴가 열린다", async () => {
  const ctx = await boot(LOCAL);
  const tree = ev(ctx, "FB.tree"); tree.querySelectorAll = () => rowsOf(ctx);     // 시험 도구의 querySelectorAll 은 빈 배열
  ev(ctx, `fbSelect(${JSON.stringify(id("report.xlsx"))})`);
  let prevented = false;
  ev(ctx, "fbTreeKey")({ key: "ContextMenu", preventDefault() { prevented = true; } });
  assert.ok(prevented && ev(ctx, "POP") && menuItems(ctx).length === 2);
  ev(ctx, "closePop()");
  ev(ctx, "fbTreeKey")({ key: "F10", shiftKey: true, preventDefault() {} });
  assert.ok(ev(ctx, "POP"));
});

test("폰(릴레이): 오른쪽 클릭 메뉴가 없다 — PC 화면에 창을 띄우는 일이라", async () => {
  const ctx = await boot();
  assert.equal(ev(ctx, "MODE"), "relay");
  assert.equal(rclick(rowOf(ctx, id("report.xlsx"))), false, "브라우저 기본 동작도 막지 않는다");
  assert.ok(!ev(ctx, "POP"), "오른쪽 클릭해도 메뉴 없음");
  const tree = ev(ctx, "FB.tree"); tree.querySelectorAll = () => rowsOf(ctx);
  ev(ctx, `fbSelect(${JSON.stringify(id("report.xlsx"))})`);
  ev(ctx, "fbTreeKey")({ key: "ContextMenu", preventDefault() {} });
  assert.ok(!ev(ctx, "POP"));
  assert.equal(ev(ctx, `fbMenu(${JSON.stringify(id("report.xlsx"))}, null)`), null);
});
