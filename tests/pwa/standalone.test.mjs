// 별도 프로젝트(2026-11-03 사장님): 다른 세션 폴더 안의 세션도 「별도 프로젝트로」 하면 상위 묶음에 넣지 않고 같은 높이로.
// 그런 폴더 구조의 세션에만 ⋯ 항목이 보인다. 📁 파일 보기도 같은 규칙 — 하위 폴더는 상위 폴더 트리 안에서만.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const eq = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);   // vm 안 배열은 프로토타입이 달라 deepEqual 이 못 씀
const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
const S = [
  { session_id: "s-top", title: "상위", cwd: "E:\\proj", project_folder: "E--proj", ended_at: "2026-11-03T04:00:00Z", mtime: 40 },
  { session_id: "s-sub", title: "하위", cwd: "E:\\proj\\sub", project_folder: "E--proj-sub", ended_at: "2026-11-03T03:00:00Z", mtime: 30 },
  { session_id: "s-deep", title: "깊은", cwd: "E:\\proj\\sub\\deep", project_folder: "E--proj-sub-deep", ended_at: "2026-11-03T02:00:00Z", mtime: 20 },
  { session_id: "s-oth", title: "다른", cwd: "E:\\other", project_folder: "E--other", ended_at: "2026-11-03T01:00:00Z", mtime: 10 }];

async function boot() {
  const ctx = await load();
  ev(ctx, "SESSIONS = " + JSON.stringify(S) + "; globalThis.__calls = []; T = Object.assign({}, T, { api: async (v, p, o) => { __calls.push([v, p, o && o.body]); return {}; } });");
  return ctx;
}
function groups(ctx) {
  const w = stubEl("div"); ctx.__w = w; ev(ctx, "renderRows(__w, '')");
  const heads = w.children.filter((n) => String(n.className) === "ghead").map((h) => flat(h).find((x) => String(x.className) === "gpath").textContent);
  const lists = w.children.filter((n) => String(n.className) === "glist").map((g) => g.children.map((r) => r.dataset.sid));
  return { heads, lists, w };
}
const item = (ctx, sid) => ev(ctx, `rowMenuItems(SESSIONS.find(s => s.session_id === '${sid}')).map(x => [x[1], x[2]])`)
  .find((x) => x[0] === "별도 프로젝트로" || x[0] === "상위 폴더 묶음에 넣기") || null;

test("⋯ 「별도 프로젝트로」: 다른 세션 폴더 안의 세션에만 · 누르면 그 폴더가 같은 높이 묶음, 그 아래는 그 묶음으로 · 되돌리기", async () => {
  const ctx = await boot();
  eq(groups(ctx).heads, ["E:\\proj", "E:\\other"]);
  eq(groups(ctx).lists[0], ["s-top", "s-sub", "s-deep"]);
  assert.equal(item(ctx, "s-top"), null, "다른 세션 폴더 안이 아니면 항목이 없다");
  assert.equal(item(ctx, "s-oth"), null);
  assert.equal(item(ctx, "s-sub")[0], "별도 프로젝트로");
  assert.ok(item(ctx, "s-sub")[1].startsWith("proj 묶음"));
  assert.ok(item(ctx, "s-deep")[1].startsWith("sub 묶음"), "가장 가까운 상위 세션 폴더");

  await ev(ctx, "rowMenuItems(SESSIONS[1]).find(x => x[1] === '별도 프로젝트로')[3]()");
  eq(ev(ctx, "__calls"), [["POST", "/api/sessions/s-sub/standalone", { on: true }]]);
  const g = groups(ctx);
  eq(g.heads, ["E:\\proj", "E:\\proj\\sub", "E:\\other"], "같은 높이로 섰다");
  eq(g.lists, [["s-top"], ["s-sub", "s-deep"], ["s-oth"]], "그 아래 폴더 세션은 가까운 묶음(별도 프로젝트)으로");
  const paths = flat(g.w.children.filter((n) => String(n.className) === "glist")[1]).filter((x) => String(x.className).startsWith("spath")).map((x) => x.textContent);
  eq(paths, ["↳ deep"], "상대경로는 그 별도 프로젝트 기준");
  assert.equal(item(ctx, "s-sub")[0], "상위 폴더 묶음에 넣기");

  await ev(ctx, "rowMenuItems(SESSIONS[1]).find(x => x[1] === '상위 폴더 묶음에 넣기')[3]()");
  eq(ev(ctx, "__calls").at(-1), ["POST", "/api/sessions/s-sub/standalone", { on: false }]);
  eq(groups(ctx).heads, ["E:\\proj", "E:\\other"]);
});

test("📁 파일 보기: 다른 세션 폴더 안의 폴더는 첫 층에 없고 상위 트리에서 찾기·📂 로 연다 · 별도 프로젝트는 첫 층", async () => {
  const ctx = await boot();
  const shown = () => ev(ctx, "fbShownRoots(fbRoots()).map(r => r.cwd)");
  eq(shown(), ["E:\\proj", "E:\\other"]);
  assert.ok(ev(ctx, "(() => { const r = fbShownRoots(fbRoots())[0]; return fbRootMatches(r, '깊은') && fbRootMatches(r, 'sub\\\\deep'); })()"), "하위 폴더 세션 이름·경로로도 상위 뿌리가 찾아진다");
  ev(ctx, "fbUseSession(SESSIONS[2])");
  assert.equal(ev(ctx, "FB.sel"), "e:\\proj|sub/deep", "상위 뿌리 트리 안의 그 폴더");
  for (const id of ["e:\\proj|", "e:\\proj|sub", "e:\\proj|sub/deep"]) assert.ok(ev(ctx, `FB.open.has(${JSON.stringify(id)})`), id + " 펼침");
  assert.ok(ev(ctx, "!!fbRoot('e:\\\\proj\\\\sub\\\\deep')"), "뿌리 기억(rootMap)은 그대로 — 그 폴더로 연 파일 탭이 계속 읽힌다");

  ev(ctx, "SESSIONS[1].standalone = true");
  eq(shown(), ["E:\\proj", "E:\\proj\\sub", "E:\\other"]);
  ev(ctx, "fbUseSession(SESSIONS[2])");
  assert.equal(ev(ctx, "FB.sel"), "e:\\proj\\sub|deep", "가장 가까운 첫 층 뿌리(별도 프로젝트) 안");
});
