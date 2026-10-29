// 👥 팀 보기(팀 통합, docs/designs/team-unify.md 2026-10-13): 그룹이 없어지고 팀이 관제·알림 + 일 장부를 함께 갖는다.
// 2026-10-15 정리: 팀 칸을 펼치면 구성원만(세션 줄처럼 동작), 관제·작업 장부(일감·기록·경력)·이름·알림·삭제는 ⋯ 메뉴.
// 팀 머리는 세션 목록의 폴더 머리(.ghead)와 같은 모양, 새 팀은 이름 + 세션 고르기, 보관 대신 삭제.
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

test("팀 목록: 🧑‍💼 개인비서 맨 위 · 머리는 폴더 머리 모양(▶ · 👥 이름 · 상태 줄 · 구성원 수 · ⋯) · 보관 팀은 접힌 묶음 · ⋯ 메뉴", async () => {
  const ctx = await boot();
  assert.equal(JSON.stringify(ev(ctx, "__calls[0]")), JSON.stringify(["GET", "/api/owner/teams", { query: { archived: 1 } }]));
  const t = texts(ctx);
  assert.equal(ev(ctx, "__w.children[0].dataset.code"), "ASSIST");
  assert.ok(t.includes("🧑‍💼 개인비서") && t.includes("꺼져 있음 · 사장님 프로필"));
  const head = ev(ctx, "__w.children[1].children[0]");
  assert.equal(head.className, "ghead grp tgh", "세션 목록 폴더 머리(.ghead)와 같은 틀");
  assert.equal(head.dataset.gid, "g1");
  assert.ok(t.includes("👥 Web") && t.includes("▶"));
  assert.equal(ev(ctx, `${FLAT}.find(n => n.className === 'gcount').textContent`), "3", "오른쪽 숫자 = 구성원 수");
  assert.ok(t.includes("작업 중 1") && t.includes("실행 1/3"), "일감 수 대신 구성원 상태를 요약한다");
  assert.ok(!t.some((x) => x.includes("제출 1")), "작업 장부 숫자는 팀 칸에 없다(메뉴 안으로)");
  assert.ok(t.includes("＋ 새 팀") && t.includes("▸ 🗄 보관된 팀 1"));
  assert.ok(!t.some((x) => x === "🗄 Old"), "보관 팀은 접혀 있다");
  click(ctx, "n => n.textContent === '▸ 🗄 보관된 팀 1'");
  assert.ok(texts(ctx).includes("🗄 Old") && texts(ctx).includes("보관됨"));
  // 관제는 ⋯ 메뉴의 「관제 열기」로 — 팀 칸 머리엔 🎬 버튼이 없다
  assert.ok(!t.includes("🎬"));
  const items = JSON.parse(ev(ctx, "JSON.stringify(teamMenuItems(TEAMS[1]).map(x => x[1]))"));
  assert.deepEqual(items, ["관제 열기", "작업 장부", "이름 바꾸기", "알림 설정", "구성원 추가", "이 팀 삭제"]);
  ev(ctx, "teamMenuItems(TEAMS[1])[0][3]()");
  assert.equal(ev(ctx, "__opened.at(-1)"), "mon:g1");
  const g = ev(ctx, "teamAsGroup(TEAMS[1])");
  assert.equal(JSON.stringify(g), JSON.stringify({ id: "g1", name: "Web", manager: "s-m", subs: ["s-f", "s-gone"],
    labels: { "s-m": "관리", "s-f": "프론트 · UI", "s-gone": "옛워커" } }));
});

test("펼치기: 기기에 기억 · 구성원만(탭 없음) · 이름은 세션 이름(별칭은 보조) · 누르면 세션 줄처럼 · 관제는 안 열림 · 빼기는 구성원 메뉴", async () => {
  const ctx = await boot();
  click(ctx, "n => n.className === 'gtext' && (n.text||'').includes('Web')");
  assert.equal(ev(ctx, "LS.getItem('sm_team_open')"), '["WEB"]');
  await tick(); await tick();
  const t = texts(ctx);
  assert.ok(!t.some((x) => /^(일감|기록|⚙ 설정)/.test(x)), "일감·기록·설정 탭은 없다");
  assert.ok(t.includes("관리 세션") && t.includes("프론트 세션") && t.includes("UI · 별칭 프론트"));
  assert.ok(t.includes("옛워커") && t.includes("세션 s-gone · 지워짐(목록에 없음)"));
  assert.equal(ev(ctx, `${FLAT}.filter(n => String(n.className).startsWith('srow tm'))[0].dataset.agent`), "ag_m", "관리가 맨 위");
  assert.ok(ev(ctx, `${FLAT}.some(n => n.className === 'glist tmembers')`), "구성원 = 세션 목록의 줄 묶음(.glist)");
  assert.ok(texts(ctx).includes("▼"));
  assert.ok(!(ev(ctx, "__opened").some((x) => String(x).startsWith("mon:"))), "펼쳐도 관제는 열리지 않는다");
  click(ctx, "n => n.className === 'sinfo' && (n.text||'').includes('프론트')");
  assert.equal(ev(ctx, "__opened.at(-1)"), "s-f", "실행 중이 아니면 상세(세션 줄과 같은 규칙)");
  ev(ctx, "confirm = () => true");
  await ev(ctx, "memberLeave(TEAMS[1], TEAMS[1].members[1])");
  assert.equal(ev(ctx, "__calls.find(c => c[1].endsWith('/leave'))[1]"), "/api/owner/teams/WEB/members/ag_f/leave");
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

test("📒 작업 장부(오른쪽 칸): 일감(필터·타임라인·승인/반려) · 기록(토큰·위반·입력 검색) · 구성원 경력 · 보존본 지우기", async () => {
  const ctx = await boot();
  ev(ctx, "document._register('paneDetail', globalThis.__pd = el('div')); TEAM_DET.WEB = " + JSON.stringify(DATA["GET /api/owner/teams/WEB"]));
  ev(ctx, "teamMenuItems(TEAMS[1]).find(x => x[1] === '작업 장부')[3]()");
  assert.equal(ev(ctx, "LEDGER.code"), "WEB");
  const PD = "(n => { const f = x => [x, ...(x.children || []).flatMap(f)]; return f(n); })(__pd)";
  const pt = () => ev(ctx, PD).map((n) => String(n.textContent || ""));
  let t = pt();
  assert.ok(t.includes("일감 · 진행 3") && t.includes("기록") && t.includes("구성원 경력") && t.includes("정리"));
  assert.ok(t.includes("client.md 작성") && !t.includes("끝난 일"), "기본은 진행 중(제출 포함)");
  ev(ctx, `${PD}.find(n => n.textContent === '전체').onclick()`);
  assert.ok(pt().includes("끝난 일"));
  await ev(ctx, `${PD}.find(n => n.textContent === '✔ 승인').onclick()`);
  assert.equal(JSON.stringify(ev(ctx, "__req[0]")), JSON.stringify(["task_decide", { team: "WEB", task: "WEB-T1", action: "accept", assignment_ver: 2 }]));
  ev(ctx, `${PD}.find(n => n.className === 'sinfo' && (n.text||'').includes('WEB-T1')).onclick()`);
  await tick(); await tick();
  t = pt();
  assert.ok(t.some((x) => x.includes("제출됨 · 배정 v2 · 토큰(추정) 4.0k")) && t.includes("💬 메시지 · 프론트 → 백엔드"));
  assert.ok(t.includes("🧮 토큰 12.3k") && t.includes("프론트 · 2.5k") && t.includes("⚠ 규칙 위반 1"));
  ev(ctx, "TEAM_UI.WEB.q = '만료'");
  await ev(ctx, `${PD}.find(n => n.textContent === '검색').onclick()`);
  assert.ok(pt().includes("만료 처리 써 줘"));
  // 경력: 구성원 이름(세션 이름)을 누르면 그 아래
  ev(ctx, `${PD}.find(n => String(n.textContent).startsWith('▸ 프론트 세션')).onclick()`);
  await tick(); await tick();
  assert.ok(pt().some((x) => x.includes("토큰 2.5k · 메시지 2 · 제출 1 · 승인 1 · 반려 0 · 사장님 입력 3")));
  ev(ctx, `${PD}.find(n => n.className === 'setrow' && (n.text||'').includes('보존본 지우기')).onclick()`);
  assert.equal(JSON.stringify(ev(ctx, "__req.at(-1)")), JSON.stringify(["team_purge", { team: "WEB" }]));
});

test("알림 설정은 메뉴에서 작은 창 — 토글은 바로 저장 · 삭제는 승인 요청(team_delete) · 예전 보관 팀은 보관 풀기", async () => {
  const ctx = await boot();
  ev(ctx, "openModal = (t, b) => { globalThis.__mb = b; globalThis.__mt = t; }");
  ev(ctx, "teamMenuItems(TEAMS[1]).find(x => x[1] === '알림 설정')[3]()");
  assert.equal(ev(ctx, "__mt"), "🔔 Web 알림");
  const MB = "(n => { const f = x => [x, ...(x.children || []).flatMap(f)]; return f(n); })(__mb)";
  const cbs = ev(ctx, `${MB}.filter(n => n.type === 'checkbox')`);
  assert.equal(JSON.stringify(cbs.map((c) => c.checked)), "[true,true,false,true]");
  ev(ctx, `(() => { const c = ${MB}.filter(n => n.type === 'checkbox')[3]; c.checked = false; return c.onchange(); })()`);
  await tick();
  const post = ev(ctx, "__calls.find(c => c[1] === '/api/owner/teams/WEB/notify')");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ notify: { error: false } }));
  ev(ctx, "TEAM_OPEN.add('WEB'); teamMenuItems(TEAMS[1]).find(x => x[1] === '이 팀 삭제')[3]()");
  assert.equal(JSON.stringify(ev(ctx, "__req.at(-1)")), JSON.stringify(["team_delete", { team: "WEB" }]));
  ev(ctx, "__done({deleted:true})");
  assert.ok(!ev(ctx, "TEAM_OPEN.has('WEB')") && ev(ctx, "__toasts.at(-1)") === "팀을 삭제했습니다");
  ev(ctx, "teamMenuItems(TEAMS[2]).find(x => x[1] === '보관 풀기')[3]()");
  assert.equal(JSON.stringify(ev(ctx, "__req.at(-1)")), JSON.stringify(["team_archive", { team: "OLD", on: false }]));
});

test("＋ 새 팀: 이름 + 세션 고르기(★ = 관리, 처음 고른 세션이 기본) → 별칭은 세션 이름 → 승인 요청(team_create)", async () => {
  const ctx = await boot();
  click(ctx, "n => n.textContent === '＋ 새 팀'");
  await click(ctx, "n => n.textContent === '만들기(승인 요청)'");
  assert.equal(ev(ctx, "__toasts.at(-1)"), "팀 이름을 적으세요");
  ev(ctx, "TEAM_NEW.name = '포털'");
  await click(ctx, "n => n.textContent === '만들기(승인 요청)'");
  assert.equal(ev(ctx, "__toasts.at(-1)"), "세션을 하나 이상 고르세요");
  assert.ok(ev(ctx, "teamEditing()"), "폼이 열려 있으면 20초 갱신이 갈아엎지 않는다");
  const rows = () => ev(ctx, `${FLAT}.filter(n => n.className === 'tcheck tprow')`);
  assert.equal(JSON.stringify(rows().slice(0, 2).map((r) => r.children[1].children[0].textContent)), JSON.stringify(["새 워커", "오래된"]), "최근 순");
  assert.ok(!rows().some((r) => r.children[1].children[0].textContent === "자동"), "자동 실행 기록은 빠진다");
  const tick1 = (name) => ev(ctx, `(() => { const r = ${FLAT}.filter(n => n.className === 'tcheck tprow').find(r => r.children[1].children[0].textContent === '${name}'); const c = r.children[0]; c.checked = true; c.onchange(); })()`);
  tick1("오래된"); tick1("새 워커");
  assert.equal(ev(ctx, "TEAM_NEW.mgr"), "s-old", "처음 고른 세션이 관리");
  ev(ctx, `${FLAT}.filter(n => n.className === 'tpstar')[1].onclick()`);   // ☆ 새 워커 → 관리
  assert.equal(ev(ctx, "TEAM_NEW.mgr"), "s-new");
  // 찾기: 폴더 이름으로도
  ev(ctx, `(() => { const q = ${FLAT}.find(n => n.className === 'tpq'); q.value = 'old'; q.oninput(); })()`);
  assert.equal(rows().length, 1);
  await click(ctx, "n => n.textContent === '만들기(승인 요청)'");
  assert.equal(JSON.stringify(ev(ctx, "__req[0]")), JSON.stringify(["team_create",
    { name: "포털", manager_session: "s-new", manager_alias: "새 워커", members: [{ session_id: "s-old", alias: "오래된" }] }]));
  ev(ctx, "__done({code:'POR'})");
  assert.equal(ev(ctx, "TEAM_NEW"), null);
  assert.ok(ev(ctx, "TEAM_OPEN.has('POR')"));
  // 같은 이름 세션은 별칭 뒤에 번호
  ev(ctx, "SESSIONS.push({session_id:'s-dup', title:'새 워커'})");
  assert.equal(JSON.stringify(ev(ctx, "newTeamAliases(['s-new','s-dup'])")), JSON.stringify(["새 워커", "새 워커 2"]));
});

test("화면에서 낸 승인 요청은 사장님 경로(폰에서도) — /api/owner/approvals", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__calls = []; globalThis.__sheet = null; showApprovalSheet = (a, o) => { globalThis.__sheet = [a, o]; }; closeModal = () => {};"
    + "T = { api: async (verb, path, o) => { __calls.push([verb, path, o]); return {id:'ap_1', status:'pending'}; } };");
  await ev(ctx, "requestApproval('team_archive', {team:'WEB', on:true})");
  assert.equal(JSON.stringify(ev(ctx, "__calls[0]")), JSON.stringify(["POST", "/api/owner/approvals", { body: { kind: "team_archive", args: { team: "WEB", on: true } } }]));
  assert.equal(ev(ctx, "__sheet[1].execute"), true);
});

test("팀 ⋯ 메뉴: 이름 바꾸기(prompt → rename API, 취소는 무동작) · 구성원 추가는 펼쳐서 폼 · 예전 보관 팀/개인비서 메뉴", async () => {
  const ctx = await boot();
  ev(ctx, "teamMenuItems(TEAMS[1]).find(x => x[1] === '구성원 추가')[3]()");
  assert.equal(ev(ctx, "LS.getItem('sm_team_open')"), '["WEB"]');
  assert.equal(ev(ctx, "TEAM_UI.WEB.adding"), true);
  ev(ctx, "prompt = () => '  새 이름  '");
  await ev(ctx, "renameTeam(TEAMS[1])");
  const call = ev(ctx, "__calls.find(c => c[1].endsWith('/rename'))");
  assert.equal(call[1], "/api/owner/teams/WEB/rename");
  assert.equal(JSON.stringify(call[2].body), JSON.stringify({ name: "새 이름" }));
  ev(ctx, "prompt = () => null"); const n = ev(ctx, "__calls.length");
  await ev(ctx, "renameTeam(TEAMS[1])");
  assert.equal(ev(ctx, "__calls.length"), n, "취소하면 아무것도 안 보낸다");
  const old = JSON.parse(ev(ctx, "JSON.stringify(teamMenuItems(TEAMS[2]).map(x => x[1]))"));
  assert.deepEqual(old, ["작업 장부", "보관 풀기", "이 팀 삭제"]);
  const asst = JSON.parse(ev(ctx, "JSON.stringify(teamMenuItems(TEAMS[0], {assistant:true}).map(x => x[1]))"));
  assert.deepEqual(asst, ["비서 열기", "펼치기"]);
});

test("관리 세션 바꾸기·지워진 세션 빼기(2026-10-29 사장님): 일반 구성원 ⋯ = ⭐ 관리로 지정 + 빼기 · 지워진 구성원은 '지워짐' + 빼기(관리여도) · 살아 있는 관리는 빼기 대신 안내 · 관리가 지워졌으면 팀 칸에 경고", async () => {
  const ctx = await boot();
  ev(ctx, "globalThis.__pop = null; openPop = (a, box, title) => { globalThis.__pop = { title, box }; return box; };");
  const menu = (ref) => {
    ev(ctx, `(() => { const t = TEAMS[1]; const m = t.members.find(x => x.agent_id === '${ref}'); memberMenu(t, m, memberSess(m), el('button')); })()`);
    return ev(ctx, "__pop.box.children.map(c => c.className === 'popsep' ? '—' : c.className === 'cwd' ? '※' + c.textContent"
      + " : (c.children[1] && c.children[1].children[0] ? c.children[1].children[0].textContent : ''))");
  };
  const fe = menu("ag_f");
  assert.ok(fe.includes("관리 세션으로 지정") && fe.includes("이 팀에서 빼기"), JSON.stringify(fe));
  assert.deepEqual(menu("ag_x"), ["※이 구성원의 세션은 지워졌습니다(목록에 없음).", "—", "이 팀에서 빼기"], "지워진 세션엔 관리 지정 없이 빼기만");
  const mg = menu("ag_m");
  assert.ok(!mg.includes("이 팀에서 빼기") && mg.includes("※관리 세션을 바꾸려면 다른 구성원 ⋯ 에서 「⭐ 관리 세션으로 지정」"), JSON.stringify(mg));
  // 지정 → POST /manager
  ev(ctx, "confirm = () => true");
  await ev(ctx, "memberSetManager(TEAMS[1], TEAMS[1].members[1])");
  const post = ev(ctx, "__calls.find(c => c[1].endsWith('/manager'))");
  assert.equal(post[1], "/api/owner/teams/WEB/manager");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ agent_id: "ag_f" }));
  // 관리 세션이 지워진 팀: 경고 줄 + 관리 ⋯ 에 빼기
  ev(ctx, "LIST_READY = true; SESSIONS = SESSIONS.filter(s => s.session_id !== 's-m'); TEAM_OPEN.add('WEB'); renderTeamRows(__w)");
  assert.ok(texts(ctx).includes("관리 세션이 지워졌습니다 — 구성원 ⋯ 에서 「⭐ 관리 세션으로 지정」"));
  assert.deepEqual(menu("ag_m"), ["※이 구성원의 세션은 지워졌습니다(목록에 없음).", "—", "이 팀에서 빼기"]);
  assert.ok(texts(ctx).includes("세션 s-gone · 지워짐(목록에 없음)"));
});
