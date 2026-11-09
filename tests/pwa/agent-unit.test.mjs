// 에이전트 묶기(docs/designs/agent-unit.md '화면 확정'·'0.37.0 대조'): 칩·줄 순서·아래 묶음·메뉴·상태·늦은 응답.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const A = "aaaaaaaa-0000-0000-0000-000000000001", B = "bbbbbbbb-0000-0000-0000-000000000002";
const C = "cccccccc-0000-0000-0000-000000000003", D = "dddddddd-0000-0000-0000-000000000004";
const cls = (e) => String(e.className);
const txt = (w) => w.children.filter((k) => cls(k).startsWith("ghead")).map((k) => k.children.map((c) => c.text ?? c.textContent).join("|"));

const AG = (id, name, sid, x = {}) => ({ id, uid: "u-" + id, name, role: "", tags: [], status: "active", rev: 1, created: 1, session_id: sid, sessions: sid ? 1 : 0, teams: [], assistant: false, ...x });

async function setup() {
  const ctx = await load();
  const now = Date.now() / 1000, iso = (ago) => new Date(Date.now() - ago * 1000).toISOString();
  ev(ctx, `CLOCK_SKEW = 0; ACTIVE_TAG = ''; COLLAPSED.clear(); AG_CLOSED.clear(); AG_PAST.clear(); POP = null;
    SESSIONS = [
      {session_id:${JSON.stringify(A)}, title:'가', project_folder:'p', cwd:'F:/p', ended_at:${JSON.stringify(iso(50))}, message_count:1, size_bytes:10},
      {session_id:${JSON.stringify(B)}, title:'세션나', project_folder:'p', cwd:'F:/p', ended_at:${JSON.stringify(iso(900))}, message_count:1, size_bytes:10, runtime:{phase:'permission', permission_at:${now - 5}}},
      {session_id:${JSON.stringify(C)}, title:'묶이지 않음', project_folder:'q', cwd:'F:/q', ended_at:${JSON.stringify(iso(10))}, message_count:1, size_bytes:10},
      {session_id:${JSON.stringify(D)}, title:'떼어 낸', project_folder:'q', cwd:'F:/q', ended_at:${JSON.stringify(iso(20))}, message_count:1, size_bytes:10},
    ];
    AGENTS = ${JSON.stringify([AG("ag1", "가", A), AG("ag2", "나", B, { role: "분석", teams: [{ code: "KP", name: "대백과" }] }), AG("ag3", "빈손", null, { sessions: 1 }), AG("ag4", "쉬는이", null, { status: "retired" })])};
    AGENTS_BY = ${JSON.stringify({ [A]: { agent_id: "ag1", name: "가", current: true, detached: false }, [B]: { agent_id: "ag2", name: "나", current: true, detached: false }, [D]: { agent_id: "ag3", name: "빈손", current: false, detached: true } })};
    AGENTS_ERR = false;`);
  return ctx;
}

test("칩(U13): 이름이 같으면 🪪 만, 다르면 🪪 이름, 없으면 없음, 떼어 낸 세션은 칩 없음", async () => {
  const ctx = await setup();
  assert.equal(ev(ctx, `agentChip(SESSIONS[0]).textContent`), "🪪");
  assert.equal(ev(ctx, `agentChip(SESSIONS[0]).getAttribute('aria-label')`) ?? "에이전트 가", "에이전트 가");
  assert.equal(ev(ctx, `agentChip(SESSIONS[1]).textContent`), "🪪 나");
  assert.equal(ev(ctx, `agentChip(SESSIONS[2])`), null);
  assert.equal(ev(ctx, `agentChip(SESSIONS[3])`), null);
});

test("용어(U1): 칩 문구가 새 이름으로", async () => {
  const ctx = await setup();
  const html = ev(ctx, `String(sessRowInfo)`);
  assert.ok(html.includes("점유 중") && html.includes("재개 목록 밖") && !html.includes("'⚙ 에이전트'"));
});

test("줄 순서(U3): 권한 대기가 먼저, 아래 묶음은 지금 세션 없음·쉬는·아닌 세션 순", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '')`);
  const heads = txt(ev(ctx, "__w"));
  assert.ok(heads[0].includes("나") && heads[0].includes("권한 대기"), heads[0]);
  assert.ok(heads[1].includes("가"), heads[1]);
  same(heads.slice(2).map((h) => h.replace(/^[▼▶]\|/, "")), ["지금 세션 없음 1", "쉬는 에이전트 1", "에이전트 아닌 세션 2"]);
});

test("지금 세션 줄: 이름 같으면 '지금 세션', 다르면 세션 이름 보조", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '')`);
  const lists = ev(ctx, "__w").children.filter((k) => cls(k) === "glist");
  const names = lists.slice(0, 2).map((l) => l.children[0].children[0].children[0].children.find((k) => cls(k) === "sname").textContent);
  same(names, ["지금 세션 · 세션나", "지금 세션"]);
});

test("검색·빈 상태·오류(U4)", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '분석')`);
  assert.ok(txt(ev(ctx, "__w"))[0].includes("나"));
  ev(ctx, `renderAgentRows(__w, '없는말')`);
  assert.ok(ev(ctx, "__w").children.some((k) => cls(k) === "empty" && k.textContent.includes("에 맞는 에이전트가 없습니다")));
  ev(ctx, `AGENTS = []; AGENTS_BY = {}; renderAgentRows(__w, '')`);
  assert.ok(ev(ctx, "__w").children.some((k) => cls(k).includes("agempty")));
  ev(ctx, `AGENTS = null; AGENTS_ERR = true; renderAgentRows(__w, '')`);
  assert.ok(ev(ctx, "__w").children[0].children[0].textContent.includes("못 받았습니다"), "에이전트만 실패하면 안내 + 폴더로");
});

test("세션 메뉴(U14·E11): 무소속 한 항목, 소속 = 보기+위험 구역 떼기, 떼어 낸 세션 = 원래 에이전트로만, 자동 실행은 없음", async () => {
  const ctx = await setup();
  const m = (i) => ev(ctx, `JSON.stringify((({main,danger})=>({main:main.map(x=>x[1]),danger:danger.map(x=>x[1])}))(agentMenuItems(SESSIONS[${i}])))`);
  same(JSON.parse(m(2)), { main: ["에이전트로 만들기·붙이기…"], danger: [] });
  same(JSON.parse(m(0)), { main: ["에이전트 보기"], danger: ["에이전트에서 떼기"] });
  same(JSON.parse(m(3)), { main: ["「빈손」에 다시 붙이기"], danger: [] });
  ev(ctx, `SESSIONS[2].headless = true`);
  same(JSON.parse(m(2)), { main: [], danger: [] });
  ev(ctx, `SESSIONS[2].headless = false; AGENTS = null`);
  same(JSON.parse(m(2)), { main: [], danger: [] }, "옛 Host·못 받음 = 메뉴 없음");
});

test("떼기 항목은 삭제 바로 앞(위험 구역)", async () => {
  const ctx = await setup();
  const items = ev(ctx, `rowMenuItems(SESSIONS[0]).map(x=>x[1])`);
  assert.equal(items.indexOf("에이전트에서 떼기"), items.indexOf("삭제") - 1);
});

test("늦은 응답(W5)·PC 전환: 나중 요청의 답만 쓴다", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__res = []; T = Object.assign({}, T, { api: () => new Promise(r => __res.push(r)) }); AGENTS_PC = pcNow();
    globalThis.__p1 = fetchAgents(); globalThis.__p2 = fetchAgents();`);
  ev(ctx, `__res[1]({agents:[{id:'new'}], by_session:{}}); __res[0]({agents:[{id:'old'}], by_session:{}});`);
  await ev(ctx, "__p2"); await ev(ctx, "__p1");
  assert.equal(ev(ctx, "AGENTS[0].id"), "new");
});

test("묶기 기억은 보기 전환과 따로(sm_list_group)", async () => {
  const ctx = await setup();
  ev(ctx, `setAgGroup('agent')`);
  assert.equal(ev(ctx, "LS.getItem('sm_list_group')"), "agent");
  assert.equal(ev(ctx, "agGroupLabel()"), "🪪 에이전트");
  ev(ctx, `setAgGroup('x')`);
  assert.equal(ev(ctx, "AG_GROUP"), "folder");
});

test("개인비서: 맨 위 고정, 쉬게 하기·떼기 메뉴 없음(U16)", async () => {
  const ctx = await setup();
  ev(ctx, `AGENTS.push(${JSON.stringify(AG("ast", "개인비서", C, { assistant: true }))}); AGENTS_BY[${JSON.stringify(C)}] = {agent_id:'ast', name:'개인비서', current:true, detached:false};
    globalThis.__w = el('div'); renderAgentRows(__w, ''); globalThis.__box = null; openPop = (a, box) => { __box = box; };
    agentMenu(AGENTS.find(a=>a.assistant), SESSIONS[2], el('button'));`);
  assert.ok(txt(ev(ctx, "__w"))[0].includes("개인비서"));
  const items = ev(ctx, "__box").children.map((k) => k.text ?? k.textContent);
  assert.ok(!items.some((t) => t.includes("떼기") || t.includes("쉬게")), items.join());
});
