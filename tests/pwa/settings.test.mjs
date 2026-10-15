// ⚙ 설정 재기획(2026-10-15): 설정 창 안에서 하위 화면으로 넘어가고 ‹ 로 돌아온다(SETNAV),
// 첫 화면에 항목별 지금 상태를 보여 준다. openModal/closeModal 은 스텁 없이 진짜를 쓴다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const DATA = {
  "GET /api/owner/2fa/status": { required: false, has_secret: false },
  "GET /api/owner/devices": { active: 2, total: 3, devices: [] },
  "GET /api/owner/housekeeping": { idle_hours: 4, jobs: [{ id: "idle_sessions", enabled: true }, { id: "models", enabled: true }] },
  "GET /api/owner/trash": { trash: [{ bucket: "b1", session_id: "s1", items: 1 }] },
  "GET /api/owner/skills/workers": { installed: true, up_to_date: false, path: "p" },
  "GET /api/owner/skills/assistant": { installed: true, up_to_date: true, path: "p" },
  "GET /api/owner/ptyd": { enabled: true, running: true, sessions: 3 },
};
async function boot() {
  const ctx = await load({ location: LOCAL_LOC });
  ctx.__DATA = JSON.parse(JSON.stringify(DATA));
  ev(ctx, "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m); pushSupported = () => false; VERSTATE = { cur: '0.22.5' };"
    + "T = Object.assign({}, T, { api: async (verb, path) => { const d = globalThis.__DATA[verb + ' ' + path];"
    + "  if (d === undefined) throw new Error('404 ' + path); return JSON.parse(JSON.stringify(d)); } });");
  return ctx;
}
const titles = (ctx) => ev(ctx, "SETNAV ? JSON.stringify(SETNAV.stack.map(p => p.title)) : 'null'");
const rowOf = (ctx, name) => `SETNAV.stack[0].body.children.find(n => n.className.startsWith('setrow') && n.text.includes(${JSON.stringify(name)}))`;
const tick = () => new Promise((r) => setTimeout(r, 0));

test("설정 항목을 누르면 설정 창 안에서 넘어가고, 뒤로(closeModal)는 설정 첫 화면을 새로 그린다 · ✕ 는 전부 닫기", async () => {
  const ctx = await boot();
  ev(ctx, "showSettings()");
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정"]));
  const root0 = ev(ctx, "SETNAV.stack[0].body");
  await ev(ctx, `${rowOf(ctx, "휴지통")}.onclick()`);
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정", "🗑 휴지통"]));
  ev(ctx, "closeModal()");                                   // ‹ 뒤로
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정"]));
  assert.notEqual(ev(ctx, "SETNAV.stack[0].body"), root0, "첫 화면은 다시 그린다(상태 새로 읽기)");
  // 같은 화면을 다시 열면(2FA 켜기 뒤 showSecurity 재호출) 쌓지 않고 갈아 끼운다
  await ev(ctx, `${rowOf(ctx, "2차 인증")}.onclick()`);
  await ev(ctx, "showSecurity()");
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정", "🔒 보안 · 2차 인증"]));
  // 하위 화면 안의 확인창도 한 겹 — 취소하면 그 화면으로
  ev(ctx, "confirmSheet({title:'확인 테스트', main:'x'})");
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정", "🔒 보안 · 2차 인증", "확인 테스트"]));
  ev(ctx, "closeModal()");
  assert.equal(titles(ctx), JSON.stringify(["⚙ 설정", "🔒 보안 · 2차 인증"]));
  ev(ctx, "closeAllModals()");                               // ✕
  assert.equal(titles(ctx), "null");
});

test("설정 첫 화면: 항목별 지금 상태 · 손볼 것은 주황(warn) · 고급 접기와 팀 줄은 없다", async () => {
  const ctx = await boot();
  ev(ctx, "showSettings()");
  await tick(); await tick(); await tick();
  const txt = (n) => ev(ctx, `${rowOf(ctx, n)}.text`);
  const warn = (n) => ev(ctx, `${rowOf(ctx, n)}.classList.contains('warn')`);
  assert.ok(txt("2차 인증").includes("꺼짐") && warn("2차 인증"));
  assert.ok(txt("외부 접속 기기").includes("활성 2대 · 해제 1"));
  assert.ok(txt("정기 작업").includes("쉬는 세션 4시간 뒤 정리") && txt("정기 작업").includes("2/2"));
  assert.ok(txt("휴지통").includes("세션 1개"));
  assert.ok(txt("워커 스킬").includes("새 내용 있음") && warn("워커 스킬"));
  assert.ok(txt("비서 스킬").includes("최신") && !warn("비서 스킬"));
  assert.ok(txt("터미널 관리 프로세스").includes("터미널 3개"));
  const all = ev(ctx, "SETNAV.stack[0].body.text");
  assert.ok(!all.includes("고급") && !all.includes("팀 보기로"));
  // 상태를 못 읽으면 기본 설명을 그대로 둔다
  delete ctx.__DATA["GET /api/owner/trash"];
  ev(ctx, "showSettings()"); await tick(); await tick();
  assert.ok(txt("휴지통").includes("30일 보관"));
});

test("설정 밖의 창은 예전 그대로 — 닫으면 끝, 쌓지 않는다", async () => {
  const ctx = await boot();
  ev(ctx, "openModal('다른 창', el('div'), [])");
  assert.equal(titles(ctx), "null");
  ev(ctx, "openModal('또 다른 창', el('div'), [])");
  ev(ctx, "closeModal()");
  assert.equal(titles(ctx), "null");
});
