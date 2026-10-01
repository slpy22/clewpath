// 👥 팀 · 경력 화면(워커 P2P 단계 2, PC 로컬 전용): 팀 목록 → 팀 → 일감 타임라인 / 에이전트 경력 / 사람 입력 / 보존본 지우기.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };

const DATA = {
  "/api/v1/team": { teams: [{ id: "tm_1", code: "WEB", name: "Web", members: 3, open_tasks: 1, archived: false },
                            { id: "tm_2", code: "OLD", name: "Old", members: 1, open_tasks: 0, archived: true }] },
  "/api/v1/team/WEB": { id: "tm_1", code: "WEB", name: "Web", members: [
      { agent_id: "ag_m", alias: "관리", member_role: "manager", role: "", live: true, session_id: "s-mmmmmmmm" },
      { agent_id: "ag_f", alias: "프론트", member_role: "worker", role: "UI", live: false, session_id: "s-ffffffff" }] },
  "/api/v1/team/WEB/usage": { total: { total: 12345 }, by_agent: [{ agent_id: "ag_f", total: 2500 }], by_task_estimate: [] },
  "/api/v1/team/WEB/violations": { violations: [{ id: 9, t: 1, text: "답 없는 질문 30분", task: "WEB-T1", who: ["프론트", "백엔드"], thread: "T1-a", notified: true }] },
  "/api/v1/team/WEB/tasks": { tasks: [{ id: "WEB-T1", status: "submitted", goal: "client.md 작성" }] },
  "/api/v1/team/WEB/tasks/WEB-T1/timeline": { id: "WEB-T1", goal: "client.md 작성", status: "submitted", assignment_ver: 1,
    tokens_estimate: { total: 4000 }, events: [
      { id: 1, t: 10, kind: "assigned", agent: "프론트", payload: { goal: "client.md 작성" } },
      { id: 2, t: 11, kind: "human_input", agent: "프론트", payload: { text: "만료 처리 써 줘" } },
      { id: 3, t: 12, kind: "msg", agent: "프론트", to: "백엔드", payload: { body: "결론: 단위?\n[cw] task=WEB-T1", cw: { type: "ASK", thread: "T1-a" } } },
      { id: 4, t: 13, kind: "submitted", agent: "프론트", payload: { evidence: { artifacts: [{}, {}] }, note: "완료" } }] },
  "/api/v1/team/WEB/members/ag_f/history": { agent_id: "ag_f", alias: "프론트", role: "UI", tags: ["ts"],
    counts: { messages_sent: 2, submitted: 1, accepted: 1, rejected: 0, human_inputs: 3 }, tokens: { total: 2500 },
    sessions: [{ session_id: "s-ffffffff", reason: "created", started: 1, ended: null }],
    tasks_owned: [{ id: "WEB-T1", status: "accepted", goal: "client.md 작성" }], tasks_collab: [] },
  "/api/v1/team/agent/ag_f": { memberships: [{ code: "WEB", name: "Web", alias: "프론트", joined: 1, left_at: null }] },
  "/api/v1/team/WEB/inputs": { items: [{ id: 5, t: 1, agent: "프론트", task: "WEB-T1", text: "만료 처리 써 줘" }], next_before: null },
};

async function boot() {
  const ctx = await load({ location: LOCAL_LOC });
  ctx.__DATA = DATA;
  ev(ctx, "globalThis.__calls = []; globalThis.__modals = []; toast = (m) => { (globalThis.__toasts ||= []).push(m); };"
    + "openModal = (title, body) => { globalThis.__modals.push({ title, body }); }; closeModal = () => {};"
    + "T = { api: async (verb, path, o) => { globalThis.__calls.push([verb, path, o]); const d = globalThis.__DATA[path];"
    + "  if (verb === 'POST') return { ok: true }; if (!d) throw new Error('404 ' + path); return JSON.parse(JSON.stringify(d)); } };");
  return ctx;
}
const lastModal = (ctx) => ev(ctx, "globalThis.__modals.at(-1)");

test("showTeams: 보관 팀 포함 목록(archived=1), 행을 누르면 팀 화면", async () => {
  const ctx = await boot();
  await ev(ctx, "showTeams()");
  const m = lastModal(ctx);
  assert.equal(m.title, "👥 팀 · 경력");
  assert.ok(m.body.text.includes("Web (WEB)") && m.body.text.includes("보관됨"));
  assert.equal(JSON.stringify(ev(ctx, "globalThis.__calls[0][2]")), JSON.stringify({ query: { archived: 1 } }));
});

test("showTeam: 구성원(살아 있음·토큰)·일감·위반·기록 메뉴", async () => {
  const ctx = await boot();
  await ev(ctx, "showTeam('WEB')");
  const t = lastModal(ctx).body.text;
  for (const s of ["토큰 합 12.3k", "관리 (관리)", "프론트 · UI", "토큰 2.5k", "WEB-T1 · 제출됨", "규칙 위반 1", "답 없는 질문 30분",
                   "프론트 → 백엔드", "알림됨", "사람 입력 검색", "이 팀 보존본 지우기"]) assert.ok(t.includes(s), s);
});

test("purgeTeamArchive: 직접 지우지 않고 승인 요청(team_purge) → 승인 화면(PC 는 2FA 칸)", async () => {
  const ctx = await boot();
  ev(ctx, "globalThis.__sheets = []; showApprovalSheet = (a, o) => globalThis.__sheets.push([a, o]);");
  ctx.__DATA["/api/v1/team/approvals"] = { id: "ap_1", kind: "team_purge", args: { team: "WEB" }, status: "pending", summary: "s" };
  ev(ctx, "T.api = (orig => async (v, p, o) => { globalThis.__calls.push([v, p, o]); return p === '/api/v1/team/approvals' ? globalThis.__DATA[p] : orig(v, p, o); })(T.api)");
  await ev(ctx, "purgeTeamArchive('WEB', 'Web')");
  const post = ev(ctx, "globalThis.__calls.find(c => c[0] === 'POST')");
  assert.equal(post[1], "/api/v1/team/approvals");
  assert.equal(JSON.stringify(post[2].body), JSON.stringify({ kind: "team_purge", args: { team: "WEB" } }));
  assert.ok(!ev(ctx, "globalThis.__calls.some(c => String(c[1]).endsWith('/purge'))"), "직접 purge 호출 없음");
  assert.equal(ev(ctx, "globalThis.__sheets[0][1].execute"), true);
});

test("showTaskTimeline: 종류 라벨·보낸 이→받는 이·메시지 첫 줄·산출물 수·토큰 추정", async () => {
  const ctx = await boot();
  await ev(ctx, "showTaskTimeline('WEB', 'WEB-T1')");
  const m = lastModal(ctx);
  assert.equal(m.title, "📋 WEB-T1");
  for (const s of ["📌 배정", "🙋 사장님 입력", "만료 처리 써 줘", "💬 메시지 · 프론트 → 백엔드", "ASK [T1-a] 결론: 단위?",
                   "📤 제출", "산출물 2개 · 완료", "토큰(추정) 4.0k"]) assert.ok(m.body.text.includes(s), s);
});

test("showAgentCareer: 수치·전체 팀 소속 이력·세션 이력·맡은 일감", async () => {
  const ctx = await boot();
  await ev(ctx, "showAgentCareer('WEB', 'ag_f')");
  const m = lastModal(ctx);
  assert.ok(m.title.includes("프론트"));
  for (const s of ["토큰 2.5k", "메시지 2", "승인 1", "사장님 입력 3", "Web (WEB) · 프론트", "현재", "s-ffffff · created", "WEB-T1 · 승인"])
    assert.ok(m.body.text.includes(s), s);
});

test("showTeamInputs: 검색어를 쿼리로 넘기고 결과 전문을 보여 준다", async () => {
  const ctx = await boot();
  await ev(ctx, "showTeamInputs('WEB', '만료 처리')");
  const call = ev(ctx, "globalThis.__calls.at(-1)");
  assert.equal(call[1], "/api/v1/team/WEB/inputs");
  assert.equal(call[2].query.q, "만료 처리");
  assert.ok(lastModal(ctx).body.text.includes("만료 처리 써 줘"));
});

test("teamEventText: 종류별 요약(메시지 첫 줄·사람 입력 200자·위반 규칙)", async () => {
  const ctx = await boot();
  assert.equal(ev(ctx, "teamEventText({kind:'violation', payload:{rule:'deadlock', thread:'T1-a'}})"), "deadlock · T1-a");
  assert.equal(ev(ctx, "teamEventText({kind:'human_input', payload:{text:'가'.repeat(300)}})").length, 200);
  assert.equal(ev(ctx, "teamEventText({kind:'msg_in', payload:{body:'첫 줄\\n둘째'}})"), "첫 줄");
});
