// 목록 보기 전환(세션 | 그룹) — 관제 그룹을 팝업이 아니라 목록에서 고른다(2026-10-11).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];

test("renderGroupRows: 그룹 헤더(이름·세션 수·진행 수) + 구성원(관리 표시·목록에 없음) + 새 그룹 버튼", async () => {
  const ctx = await load();
  const now = Date.now() / 1000;
  ev(ctx, `CLOCK_SKEW = 0; SESSIONS = [{session_id:'m1', title:'관리자', runtime:{phase:'thinking', thinking_at:${now - 5}}},`
    + "{session_id:'w1', title:'워커1'}];"
    + "MON_GROUPS = [{id:'g1', name:'논문팀', manager:'m1', subs:['w1','gone'], labels:{w1:'선행연구'}}];"
    + "globalThis.__w = el('div'); renderGroupRows(__w);");
  const nodes = flat(ev(ctx, "__w"));
  const texts = nodes.map((n) => String(n.textContent || ""));
  assert.ok(texts.includes("🎬 논문팀"));
  assert.ok(texts.includes("3개 세션 · 진행 1"));
  assert.ok(texts.includes("선행연구"), "저장된 라벨을 쓴다");
  assert.ok(texts.includes("관리"));
  assert.ok(texts.includes("목록에 없음"));
  assert.ok(texts.includes("＋ 새 관제 그룹"));
  // 헤더 탭 → 관전
  ev(ctx, "globalThis.__opened = null; openMonitorGroup = (g) => { globalThis.__opened = g.id; };");
  nodes.find((n) => n.textContent === "관전").onclick();
  assert.equal(ev(ctx, "__opened"), "g1");
});

test("renderGroupRows: 아직 못 받았으면 스피너, 없으면 안내", async () => {
  const ctx = await load();
  ev(ctx, "SESSIONS = []; MON_GROUPS = null; globalThis.__w = el('div'); renderGroupRows(__w);");
  assert.equal(ev(ctx, "__w.children[0].className"), "spin");
  ev(ctx, "MON_GROUPS = []; renderGroupRows(__w);");
  assert.ok(ev(ctx, "__w.children[0].textContent").includes("저장된 관제 그룹이 없습니다"));
});

test("setListView: 기기에 기억, 이상한 값은 세션 보기", async () => {
  const ctx = await load();
  ev(ctx, "setListView('groups')");
  assert.equal(ev(ctx, "LIST_VIEW"), "groups");
  assert.equal(ev(ctx, "LS.getItem('sm_list_view')"), "groups");
  ev(ctx, "setListView('x')");
  assert.equal(ev(ctx, "LIST_VIEW"), "sessions");
});

test("showMonitorPicker: 새 그룹 만들기 전용(저장된 그룹 목록은 없음)", async () => {
  const ctx = await load();
  ev(ctx, "SESSIONS = [{session_id:'a', title:'A'}]; globalThis.__fetched = 0;"
    + "fetchMonGroups = async () => { globalThis.__fetched++; return []; };"
    + "openModal = (t, b, f) => { globalThis.__t = t; };");
  ev(ctx, "showMonitorPicker()");
  assert.equal(ev(ctx, "__t"), "🎬 새 관제 그룹 — 세션 선택");
  assert.equal(ev(ctx, "__fetched"), 0);
});
