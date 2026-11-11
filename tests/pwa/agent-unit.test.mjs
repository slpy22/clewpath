// 에이전트 묶기(docs/designs/agent-unit.md '화면 확정'·'0.37.0 대조'): 칩·줄 순서·아래 묶음·메뉴·상태·늦은 응답.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev, stubEl, INDEX } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const A = "aaaaaaaa-0000-0000-0000-000000000001", B = "bbbbbbbb-0000-0000-0000-000000000002";
const C = "cccccccc-0000-0000-0000-000000000003", D = "dddddddd-0000-0000-0000-000000000004";
const cls = (e) => String(e.className);
const txt = (w) => w.children.filter((k) => cls(k).startsWith("ghead")).map((k) => k.children.map((c) => c.text ?? c.textContent).join("|"));
const walk = (n, out = []) => { for (const k of n.children || []) { out.push(k); walk(k, out); } return out; };
const rows = (n) => walk(n).filter((k) => cls(k).includes("agrow"));
const rowName = (r) => walk(r).find((k) => cls(k) === "sname").textContent;
const rowText = (r) => walk(r).map((k) => k.textContent).join("|");

const AG = (id, name, sid, x = {}) => ({ id, uid: "u-" + id, name, role: "", tags: [], status: "active", rev: 1, created: 1, session_id: sid, sessions: sid ? 1 : 0, teams: [], assistant: false, ...x });

async function setup() {
  const ctx = await load();
  const now = Date.now() / 1000, iso = (ago) => new Date(Date.now() - ago * 1000).toISOString();
  ev(ctx, `CLOCK_SKEW = 0; ACTIVE_TAG = ''; COLLAPSED.clear(); AG_FOLD.clear(); AG_SEC.clear(); POP = null;
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

test("목록(A1·A3): 세션 탭처럼 폴더 아래 에이전트 한 줄씩 — 폴더 안은 권한 대기 먼저, 지금 세션 없는 에이전트는 마지막 세션 폴더에 흐리게, 쉬는 에이전트는 아래 묶음", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '')`);
  const w = ev(ctx, "__w");
  const heads = txt(w);
  assert.ok(heads[0].includes("📁 q") && heads[1].includes("📁 p"), "최근 활동 폴더 먼저: " + heads.join(" / "));
  assert.ok(heads[2].includes("💤 쉬는 에이전트 1"), heads[2]);
  const lists = w.children.filter((k) => cls(k) === "glist");
  same(rows(lists[1]).map(rowName), ["🪪 나", "🪪 가"]);
  const q = rows(lists[0]);
  same(q.map(rowName), ["🪪 빈손"]);
  assert.ok(cls(q[0]).includes("dim") && rowText(q[0]).includes("지금 세션 없음"));
  assert.ok(!walk(q[0]).some((k) => cls(k).includes("resume")), "지금 세션이 없으면 여는 단추도 없다");
  assert.ok(!walk(w).some((k) => k.dataset && k.dataset.sid), "목록에 세션 줄은 그리지 않는다(세션은 상세에서)");
  assert.ok(walk(w.children.filter((k) => cls(k) === "ghead")[1]).some((k) => k.getAttribute && k.getAttribute("aria-label") === "작업 중 0 · 열림 0 · 에이전트 2"), "폴더 수는 에이전트 기준");
});

test("에이전트 줄(A2): 본문 = 상세, 큰 단추 = 지금 세션 열기, 지금 세션 이름이 다르면 보조로", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, ''); globalThis.__seen = []; loadAgentDetail = (id) => __seen.push('detail:'+id); showResumeChooser = (s) => __seen.push('open:'+s.session_id);`);
  const by = (n) => rows(ev(ctx, "__w")).find((r) => rowName(r) === n), na = by("🪪 나"), ga = by("🪪 가");
  assert.ok(rowText(na).includes("지금 · 세션나") && rowText(na).includes("분석") && rowText(na).includes("대백과"));
  assert.ok(!rowText(ga).includes("지금 ·"), "이름이 같으면 보조 없음");
  na.children[0].onclick();
  walk(ga).find((k) => cls(k) === "sbtn resume").onclick();
  same(ev(ctx, "__seen"), ["detail:ag2", "open:" + A]);
});

test("폴더 접기는 에이전트 탭 따로(세션 탭 접힘과 안 섞임)", async () => {
  const ctx = await setup();
  ev(ctx, `LIST_CTX = {rerender(){}}; globalThis.__w = el('div'); renderAgentRows(__w, '')`);
  ev(ctx, "__w").children.find((k) => cls(k) === "ghead").onclick();
  assert.equal(ev(ctx, "AG_FOLD.size"), 1);
  assert.equal(ev(ctx, "COLLAPSED.size"), 0);
  ev(ctx, `renderAgentRows(__w, '')`);
  same(rows(ev(ctx, "__w")).map(rowName), ["🪪 나", "🪪 가"], "접은 폴더(q)의 줄은 안 그린다");
});

test("비서(A4): 따로 있던 비서 탭·단추는 없고, 비서가 없으면 에이전트 탭 맨 위에 만들기 줄", async () => {
  const ctx = await setup();
  const html = readFileSync(INDEX, "utf8");
  assert.ok(!/data-v="assist"/.test(html), "폰 아래 탭 막대에 비서 칸 없음");
  assert.ok(!ev(ctx, "String(loadList)").includes("openAssistant"), "목록 칸 보기 전환에 비서 단추 없음");
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, ''); globalThis.__opened = 0; openAssistant = () => { __opened++; };`);
  const first = ev(ctx, "__w").children[0];
  assert.equal(cls(first), "agnoast");
  first.onclick();
  assert.equal(ev(ctx, "__opened"), 1);
});

test("⋯ 메뉴(A5): 열기·상세·갈아타기·이름·지금 세션 메뉴 → 같은 자리에 세션 메뉴", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__box = null; globalThis.__rm = null; openPop = (a, box) => { __box = box; }; rowMenu = (s) => { __rm = s.session_id; };
    agentMenu(AGENTS[0], SESSIONS[0], el('button'));`);
  const items = walk(ev(ctx, "__box")).map((k) => k.text ?? k.textContent).join("|");
  for (const t of ["지금 세션 열기", "에이전트 상세", "새 세션으로 갈아타기", "에이전트 이름", "지금 세션 메뉴", "지금 세션 떼기", "쉬게 하기"]) assert.ok(items.includes(t), t);
  const it = walk(ev(ctx, "__box")).find((k) => (k.text ?? "").includes("지금 세션 메뉴"));
  await it.onclick();
  assert.equal(ev(ctx, "__rm"), A);
});

test("검색·빈 상태·오류(U4)", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '분석')`);
  same(rows(ev(ctx, "__w")).map(rowName), ["🪪 나"]);
  ev(ctx, `renderAgentRows(__w, '없는말')`);
  assert.ok(ev(ctx, "__w").children.some((k) => cls(k) === "empty" && k.textContent.includes("에 맞는 에이전트가 없습니다")));
  ev(ctx, `AGENTS = []; AGENTS_BY = {}; renderAgentRows(__w, '')`);
  assert.ok(ev(ctx, "__w").children.some((k) => cls(k).includes("agempty")));
  ev(ctx, `AGENTS = null; AGENTS_ERR = true; renderAgentRows(__w, '')`);
  assert.ok(ev(ctx, "__w").children[0].children[0].textContent.includes("못 받았습니다"), "에이전트만 실패하면 안내 + 세션 탭으로");
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

test("개인비서: 맨 위 고정, 쉬게 하기·떼기 메뉴 없음(U16)", async () => {
  const ctx = await setup();
  ev(ctx, `AGENTS.push(${JSON.stringify(AG("ast", "개인비서", C, { assistant: true }))}); AGENTS_BY[${JSON.stringify(C)}] = {agent_id:'ast', name:'개인비서', current:true, detached:false};
    globalThis.__w = el('div'); renderAgentRows(__w, ''); globalThis.__box = null; openPop = (a, box) => { __box = box; };
    agentMenu(AGENTS.find(a=>a.assistant), SESSIONS[2], el('button'));`);
  const top = ev(ctx, "__w").children[0];
  assert.ok(cls(top).includes("agtop") && rowName(rows(top)[0]) === "🧑‍💼 개인비서", "맨 위 고정 줄");
  assert.equal(rows(ev(ctx, "__w")).filter((r) => rowName(r).includes("개인비서")).length, 1, "폴더 안에 다시 나오지 않는다");
  assert.ok(!walk(ev(ctx, "__w")).some((k) => cls(k) === "agnoast"));
  const items = ev(ctx, "__box").children.map((k) => k.text ?? k.textContent);
  assert.ok(!items.some((t) => t.includes("떼기") || t.includes("쉬게")), items.join());
});
