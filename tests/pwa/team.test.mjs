// 👥 팀 보기(팀 통합, docs/designs/team-unify.md 2026-10-13): 그룹이 없어지고 팀이 관제·알림 + 일 장부를 함께 갖는다.
// 팝업 없이 목록 칸에서 펼쳐 보고 관리 — 구성원(세션 열기·경력 펼침·추가/빼기) · 일감(타임라인·승인/반려) · 기록 · 설정.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const MEMBERS = [
  { agent_id: "ag_m", alias: "관리", member_role: "manager", role: "", live: true, session_id: "s-m" },
  { agent_id: "ag_f", alias: "프론트", member_role: "worker", role: "UI", live: false, session_id: "s-f" },
  { agent_id: "ag_x", alias: "옛워커", member_role: "worker", role: "", live: false, session_id: "s-gone" }];
const DATA = {
  "GET /api/owner/teams": { teams: [
    { id: "tm_a", code: "ASSIST", name: "비서", kind: "portfolio", members: [{ agent_id: "ag_a", alias: "비서", member_role: "manager", live: false }], tasks: {} },
    { id: "tm_1", code: "WEB", name: "Web", kind: "team", monitor_group_id: "g1", members: MEMBERS, tasks: { assigned: 2, submitted: 1 },
      notify: { manager_stop: true, sub_stop: true, sub_start: false, error: true } },
    { id: "tm_2", code: "OLD", name: "Old", kind: "team", archived: true, monitor_group_id: null, members: [], tasks: {} }] },
  "GET /api/owner/teams/WEB": { id: "tm_1", code: "WEB", name: "Web", members: MEMBERS, monitor_group_id: "g1",
    notify: { manager_stop: true, sub_stop: false, sub_start: false, error: true },
    tasks: [{ id: "WEB-T1", status: "submitted", goal: "client.md 작성", owner: "ag_f", assignment_ver: 2, created: 1 },
            { id: "WEB-T2", status: "accepted", goal: "끝난 일", owner: "ag_f", assignment_ver: 1, created: 1, closed: 2 }],
    usage: { total: { total: 12345 }, by_agent: [{ agent_id: "ag_f", alias: "프론트", total: 2500 }] },
    violations: [{ id: 9, t: 1, text: "답 없는 질문 30분", task: "WEB-T1", who: ["프론트", "백엔드"], notified: true }] },
  "GET /api/owner/teams/WEB/tasks/WEB-T1/timeline": { id: "WEB-T1", goal: "client.md 작성", status: "submitted", assignment_ver: 2,
    tokens_estimate: { total: 4000 }, events: [
      { id: 1, t: 10, kind: "assigned", agent: "프론트", payload: { goal: "client.md 작성" } },
      { id: 3, t: 12, kind: "msg", agent: "프론트", to: "백엔드", payload: { body: "결론: 단위?\n본문", cw: { type: "ASK", thread: "T1-a" } } }] },
  "GET /api/owner/teams/WEB/members/ag_f/history": { agent_id: "ag_f", alias: "프론트",
    counts: { messages_sent: 2, submitted: 1, accepted: 1, rejected: 0, human_inputs: 3 }, tokens: { total: 2500 },
    sessions: [{ session_id: "s-ffffffff", reason: "created", started: 1, ended: null }],
    tasks_owned: [{ id: "WEB-T1", status: "submitted", goal: "client.md 작성" }] },
  "GET /api/owner/teams/WEB/inputs": { items: [{ id: 5, t: 1, agent: "프론트", task: "WEB-T1", text: "만료 처리 써 줘" }], next_before: null },
};
const tick = () => new Promise((r) => setTimeout(r, 0));

async function boot() {
  const ctx = await load();                     // 폰(릴레이) — 팀 화면은 폰에서도 된다(U4)
  ctx.__DATA = JSON.parse(JSON.stringify(DATA));
  ev(ctx, "globalThis.__calls = []; globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
    + "globalThis.__req = []; requestApproval = async (k, a, done) => { globalThis.__req.push([k, a]); globalThis.__done = done; };"
    + "globalThis.__opened = []; loadDetail = (s) => globalThis.__opened.push(s.session_id); openMonitorGroup = (g, o) => globalThis.__opened.push('mon:' + g.id);"
    + "T = { api: async (verb, path, o) => { globalThis.__calls.push([verb, path, o]); const d = globalThis.__DATA[verb + ' ' + path];"
    + "  if (verb === 'POST' && d === undefined) return { ok: true, notify: { error: false } }; if (d === undefined) throw new Error('404 ' + path); return JSON.parse(JSON.stringify(d)); } };"
    + "SESSIONS = [{session_id:'s-m', title:'관리 세션', runtime:{phase:'thinking', thinking_at: Date.now()/1000 - 5}}, {session_id:'s-f', title:'프론트 세션'},"
    + " {session_id:'s-new', title:'새 워커', cwd:'F:/p/new', mtime: 9}, {session_id:'s-old', title:'오래된', cwd:'F:/p/old', mtime: 1}, {session_id:'s-h', title:'자동', headless:true}];"
    + "CLOCK_SKEW = 0; globalThis.__w = el('div'); LIST_CTX = { rerender(){ renderTeamRows(globalThis.__w); } };");
  await ev(ctx, "fetchTeams().then(t => { TEAMS = t; renderTeamRows(__w); })");
  return ctx;
}
const FLAT = "(n => { const f = x => [x, ...(x.children || []).flatMap(f)]; return f(n); })(__w)";
const nodes = (ctx) => ev(ctx, FLAT);
const texts = (ctx) => nodes(ctx).map((n) => String(n.textContent || ""));
const click = (ctx, pred) => ev(ctx, `${FLAT}.find(${pred}).onclick()`);

test("팀 목록: 🧑‍💼 개인비서 맨 위 · 팀 칸 요약(구성원·실행 중·진행·제출⚠) · 보관 팀은 접힌 묶음 · 보관 포함으로 받는다", async () => {
  const ctx = await boot();
  assert.equal(JSON.stringify(ev(ctx, "__calls[0]")), JSON.stringify(["GET", "/api/owner/teams", { query: { archived: 1 } }]));
  const t = texts(ctx);
  assert.equal(ev(ctx, "__w.children[0].dataset.code"), "ASSIST");
  assert.ok(t.includes("🧑‍💼 개인비서") && t.includes("꺼져 있음 · 사장님 프로필"));
  assert.ok(t.includes("구성원 3 · 🟢 1 · 진행 2 · 제출 1 ⚠"));
  assert.ok(nodes(ctx).some((n) => String(n.className) === "gpath warn"), "제출된 일감이 있으면 강조");
  assert.ok(t.includes("＋ 새 팀") && t.includes("▸ 🗄 보관된 팀 1"));
  assert.ok(!t.some((x) => x.includes("Old")), "보관 팀은 접혀 있다");
  click(ctx, "n => n.textContent === '▸ 🗄 보관된 팀 1'");
  assert.ok(texts(ctx).some((x) => x.includes("🗄 Old")));
  // 🎬 = 그 팀의 관제(관리 + 구성원)
  click(ctx, "n => n.textContent === '🎬'");
  assert.equal(ev(ctx, "__opened.at(-1)"), "mon:g1");
  const g = ev(ctx, "teamAsGroup(TEAMS[1])");
  assert.equal(JSON.stringify(g), JSON.stringify({ id: "g1", name: "Web", manager: "s-m", subs: ["s-f", "s-gone"],
    labels: { "s-m": "관리", "s-f": "프론트 · UI", "s-gone": "옛워커" } }));
});

test("펼치기: 기기에 기억 · 구성원 탭(상태 점·관리·역할·지금 세션) → 누르면 그 세션 · 경력은 그 줄 아래", async () => {
  const ctx = await boot();
  click(ctx, "n => n.className === 'gtext' && (n.text||'').includes('Web')");
  assert.equal(ev(ctx, "LS.getItem('sm_team_open')"), '["WEB"]');
  await tick(); await tick();
  const t = texts(ctx);
  assert.ok(t.includes("구성원 3") && t.includes("일감 3") && t.includes("기록") && t.includes("⚙ 설정"));
  assert.ok(t.includes("UI · 프론트 세션") && t.includes("세션 s-gone · 목록에 없음"));
  assert.ok(t.includes("관리"));
  click(ctx, "n => n.className === 'sinfo' && (n.text||'').includes('프론트')");
  assert.equal(ev(ctx, "__opened.at(-1)"), "s-f");
  ev(ctx, "toggleCareer(TEAMS[1], TEAMS[1].members[1])");
  await tick(); await tick();
  const c = texts(ctx);
  assert.ok(c.some((x) => x.includes("토큰 2.5k · 메시지 2 · 제출 1 · 승인 1 · 반려 0 · 사장님 입력 3")));
  assert.ok(c.includes("WEB-T1 · 제출됨 · client.md 작성") && c.includes("이 팀에서 빼기"));
});

test("구성원 추가: 그 자리 폼(팀에 없는 세션만·자동 실행 기록 제외·최근 순) → 별칭 자동 → 사장님 API 로 바로", async () => {
  const ctx = await boot();
  ev(ctx, "TEAM_OPEN.add('WEB'); TEAM_DET.WEB = " + JSON.stringify(DATA["GET /api/owner/teams/WEB"]) + "; renderTeamRows(__w)");
  click(ctx, "n => n.textContent === '＋ 구성원 추가'");
  const sel = ev(ctx, `${FLAT}.find(n => n.className === 'tsel')`);
  assert.equal(JSON.stringify(sel.children.map((o) => o.value)), JSON.stringify(["", "s-new", "s-old"]), "팀에 없는 세션, 최근 순, 자동 실행 기록 빼고");
  assert.ok(ev(ctx, "teamEditing()"), "폼이 열려 있으면 20초 갱신이 갈아엎지 않는다");
  ev(ctx, `${FLAT}.find(n => n.className === 'tsel').value = 's-new'`);
  ev(ctx, `${FLAT}.find(n => n.className === 'tsel').onchange()`);
  assert.equal(ev(ctx, "TEAM_UI.WEB.add.alias"), "새 워커");
  await click(ctx, "n => n.textContent === '추가'");
  const post = ev(ctx, "__calls.find(c => c[0] === 'POST')");
  assert.equal(post[1], "/api/owner/teams/WEB/members");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ session_id: "s-new", alias: "새 워커", role: "" }));
  assert.equal(ev(ctx, "TEAM_UI.WEB.adding"), false);
});

test("일감 탭: 진행 중/제출됨/전체 · 줄을 누르면 그 아래 타임라인 · 제출된 일감은 승인/반려 = 승인 요청(task_decide)", async () => {
  const ctx = await boot();
  ev(ctx, "TEAM_OPEN.add('WEB'); TEAM_TAB.WEB = 'tasks'; TEAM_DET.WEB = " + JSON.stringify(DATA["GET /api/owner/teams/WEB"]) + "; renderTeamRows(__w)");
  let t = texts(ctx);
  assert.ok(t.includes("client.md 작성") && !t.includes("끝난 일"), "기본은 진행 중(제출 포함)");
  click(ctx, "n => n.textContent === '전체'");
  assert.ok(texts(ctx).includes("끝난 일"));
  await click(ctx, "n => n.textContent === '✔ 승인'");
  assert.equal(JSON.stringify(ev(ctx, "__req[0]")), JSON.stringify(["task_decide", { team: "WEB", task: "WEB-T1", action: "accept", assignment_ver: 2 }]));
  ev(ctx, "prompt = () => '단위 테스트가 없음'");
  await click(ctx, "n => n.textContent === '✖ 반려'");
  assert.equal(ev(ctx, "__req[1][1].note"), "단위 테스트가 없음");
  click(ctx, "n => n.className === 'sinfo' && (n.text||'').includes('WEB-T1')");
  await tick(); await tick();
  t = texts(ctx);
  assert.ok(t.some((x) => x.includes("제출됨 · 배정 v2 · 토큰(추정) 4.0k")));
  assert.ok(t.includes("💬 메시지 · 프론트 → 백엔드"));
});

test("기록 탭: 토큰·위반 · 사장님 입력 검색은 그 자리에 결과", async () => {
  const ctx = await boot();
  ev(ctx, "TEAM_OPEN.add('WEB'); TEAM_TAB.WEB = 'records'; TEAM_DET.WEB = " + JSON.stringify(DATA["GET /api/owner/teams/WEB"]) + "; renderTeamRows(__w)");
  let t = texts(ctx);
  assert.ok(t.includes("🧮 토큰 12.3k") && t.includes("프론트 · 2.5k") && t.includes("⚠ 규칙 위반 1"));
  ev(ctx, "TEAM_UI.WEB.q = '만료'");
  await click(ctx, "n => n.textContent === '검색'");
  const call = ev(ctx, "__calls.find(c => c[1].endsWith('/inputs'))");
  assert.equal(JSON.stringify(call[2]), JSON.stringify({ query: { limit: 30, q: "만료" } }));
  assert.ok(texts(ctx).includes("만료 처리 써 줘"));
});

test("설정 탭: 알림 토글은 바로 저장 · 이름 바꾸기 · 보관·보존본 지우기는 승인 요청 · 보관 팀은 보관 풀기", async () => {
  const ctx = await boot();
  ev(ctx, "TEAM_OPEN.add('WEB'); TEAM_TAB.WEB = 'settings'; TEAM_DET.WEB = " + JSON.stringify(DATA["GET /api/owner/teams/WEB"]) + "; renderTeamRows(__w)");
  const cbs = ev(ctx, `${FLAT}.filter(n => n.type === 'checkbox')`);
  assert.equal(JSON.stringify(cbs.map((c) => c.checked)), "[true,false,false,true]", "상세의 실제 설정");
  ev(ctx, `(() => { const c = ${FLAT}.filter(n => n.type === 'checkbox')[3]; c.checked = false; return c.onchange(); })()`);
  await tick();
  const post = ev(ctx, "__calls.find(c => c[1] === '/api/owner/teams/WEB/notify')");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ notify: { error: false } }));
  click(ctx, "n => n.className === 'setrow' && (n.text||'').includes('이 팀 보관')");
  click(ctx, "n => n.className === 'setrow' && (n.text||'').includes('보존본 지우기')");
  assert.equal(JSON.stringify(ev(ctx, "__req")), JSON.stringify([["team_archive", { team: "WEB", on: true }], ["team_purge", { team: "WEB" }]]));
  ev(ctx, "TEAM_ARCH_OPEN = true; TEAM_OPEN.add('OLD'); TEAM_TAB.OLD = 'settings'; TEAM_DET.OLD = {id:'tm_2', code:'OLD', name:'Old', members:[], tasks:[]}; renderTeamRows(__w)");
  click(ctx, "n => n.className === 'setrow' && (n.text||'').includes('보관 풀기')");
  assert.equal(JSON.stringify(ev(ctx, "__req.at(-1)")), JSON.stringify(["team_archive", { team: "OLD", on: false }]));
});

test("＋ 새 팀: 그 자리 폼 → 이름·관리 세션 확인 → 승인 요청(team_create) → 만든 팀을 펼쳐 둔다", async () => {
  const ctx = await boot();
  click(ctx, "n => n.textContent === '＋ 새 팀'");
  await click(ctx, "n => n.textContent === '만들기(승인 요청)'");
  assert.equal(ev(ctx, "__toasts.at(-1)"), "팀 이름을 적으세요");
  ev(ctx, "TEAM_NEW.name = '포털'; TEAM_NEW.mgr = 's-new'; renderTeamRows(__w)");
  click(ctx, "n => n.textContent === '＋ 구성원'");
  ev(ctx, "TEAM_NEW.members[0].sid = 's-old'; TEAM_NEW.members[0].alias = '백엔드'; TEAM_NEW.members.push({sid:'', alias:'빈칸', role:''}); renderTeamRows(__w)");
  const sels = ev(ctx, `${FLAT}.filter(n => n.className === 'tsel')`);
  assert.ok(!sels[1].children.some((o) => o.value === "s-new"), "관리로 고른 세션은 구성원 고르기에서 빠진다");
  await click(ctx, "n => n.textContent === '만들기(승인 요청)'");
  assert.equal(JSON.stringify(ev(ctx, "__req[0]")), JSON.stringify(["team_create",
    { name: "포털", manager_session: "s-new", manager_alias: "관리", members: [{ session_id: "s-old", alias: "백엔드", role: "" }] }]));
  ev(ctx, "__done({code:'POR'})");
  assert.equal(ev(ctx, "TEAM_NEW"), null);
  assert.ok(ev(ctx, "TEAM_OPEN.has('POR')"));
});

test("화면에서 낸 승인 요청은 사장님 경로(폰에서도) — /api/owner/approvals", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__calls = []; globalThis.__sheet = null; showApprovalSheet = (a, o) => { globalThis.__sheet = [a, o]; }; closeModal = () => {};"
    + "T = { api: async (verb, path, o) => { __calls.push([verb, path, o]); return {id:'ap_1', status:'pending'}; } };");
  await ev(ctx, "requestApproval('team_archive', {team:'WEB', on:true})");
  assert.equal(JSON.stringify(ev(ctx, "__calls[0]")), JSON.stringify(["POST", "/api/owner/approvals", { body: { kind: "team_archive", args: { team: "WEB", on: true } } }]));
  assert.equal(ev(ctx, "__sheet[1].execute"), true);
});
