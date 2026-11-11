// 에이전트 탭·상세의 세션 관리·지난 대화 찾기(docs/designs/agent-unit.md '에이전트 탭·세션 관리·기록 보관' H6~H8, 판정 K11~K18).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { load, ev, stubEl, INDEX } from "./harness.mjs";

const A = "aaaaaaaa-0000-0000-0000-000000000001", B = "bbbbbbbb-0000-0000-0000-000000000002", C = "cccccccc-0000-0000-0000-000000000003";
const walk = (n, out = []) => { for (const k of n.children || []) { out.push(k); walk(k, out); } return out; };
const label = (k) => String(k.text ?? k.textContent ?? "");
const find = (root, t) => walk(root).filter((k) => label(k).startsWith(t)).pop();
const has = (root, t) => walk(root).some((k) => label(k).includes(t));
const pass = () => new Promise((r) => setImmediate(r));
const AG = { id: "ag1", uid: "u1", name: "가", role: "", tags: [], status: "active", rev: 1, created: 1, session_id: A, sessions: 3, teams: [], assistant: false };
const DETAIL = {
  ...AG, history: [
    { session_id: A, started: 3, ended: null, reason: "replaced", detached: false },
    { session_id: B, started: 2, ended: 3, reason: "replaced", detached: false },
    { session_id: C, started: 1, ended: 2, reason: "created", detached: false },
  ], last_handoff: null,
  archive: { count: 2, bytes: 3 * 1024 * 1024, sessions: { [A]: { archived: true, bytes: 1, original: true }, [B]: { archived: true, bytes: 1, original: true },
                                                              [C]: { archived: false, bytes: 0, original: false } } },
};

async function setup(routes = {}, opt) {
  const ctx = await load(opt);
  const pd = stubEl("div"), app = stubEl("div");
  ctx.document._register("paneDetail", pd); ctx.document._register("app", app); ctx.document._register("head", stubEl("span"));
  const iso = (ago) => new Date(Date.now() - ago * 1000).toISOString();
  ev(ctx, `CLOCK_SKEW = 0; ACTIVE_TAG = ''; AG_FOLD.clear(); AG_SEC.clear(); POP = null; HANDOFFS = [];
    SESSIONS = [{session_id:${JSON.stringify(A)}, title:'지금', project_folder:'p', cwd:'F:/p', ended_at:${JSON.stringify(iso(120))}, message_count:1, size_bytes:1},
                {session_id:${JSON.stringify(B)}, title:'지난', project_folder:'p', cwd:'F:/p', ended_at:${JSON.stringify(iso(9000))}, message_count:1, size_bytes:1}];
    AGENTS = [${JSON.stringify(AG)}, ${JSON.stringify({ ...AG, id: "ag2", name: "기록없는", session_id: "zzzz" })}]; AGENTS_ERR = false;
    AGENTS_BY = {[${JSON.stringify(A)}]:{agent_id:'ag1',name:'가',current:true,detached:false},[${JSON.stringify(B)}]:{agent_id:'ag1',name:'가',current:false,detached:false}};
    globalThis.__calls = []; globalThis.__routes = ${JSON.stringify(routes)}; globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    globalThis.__modal = null; openModal = (t, body, foot) => { __modal = {t, body, foot}; }; closeModal = () => { __modal = null; };
    globalThis.__sheet = null; confirmSheet = async (o) => { __sheet = o; return true; };
    globalThis.__pop = null; openPop = (a, box) => { __pop = box; };
    T = Object.assign({}, T, { api: async (v, p, o) => { const body = o && o.body; __calls.push({v, p, body});
      let h = __routes[v + ' ' + p]; if (Array.isArray(h)) h = h.length > 1 ? h.shift() : h[0];
      if (h === undefined) throw new Error('no route ' + v + ' ' + p);
      if (h && h.__err) { const e = new Error('HTTP ' + h.__err); e.status = h.__err; e.json = h.json; throw e; }
      return h; } });`);
  return { ctx, pd };
}
const calls = (ctx, v, p) => JSON.parse(ev(ctx, "JSON.stringify(__calls)")).filter((c) => c.v === v && (!p || c.p === p));
const DET_P = "GET /api/owner/agents/ag1";

// ---- 탭(H6·K11)
test("탭: 목록 칸·폰 아래 탭 막대에 🪪 에이전트, 세션 탭의 묶기 단추는 없다", async () => {
  const { ctx } = await setup();
  assert.ok(ev(ctx, "LIST_VIEWS.includes('agents')"));
  const html = readFileSync(INDEX, "utf8");
  assert.ok(/<button data-v="agents">/.test(html), "폰 아래 탭 막대");
  assert.ok(!ev(ctx, "String(loadList)").includes("aggroup"), "🪪 묶기 단추는 없앴다");
  assert.ok(ev(ctx, "String(loadList)").includes("'🪪 에이전트'"));
});

test("옛 🪪 묶기 사용자는 에이전트 탭으로 열리고, 그 탭을 그린 뒤에 기억을 지운다", async () => {
  const { ctx } = await setup({}, { setup: (sb) => sb.localStorage.setItem("sm_list_group", "agent") });
  assert.equal(ev(ctx, "LIST_VIEW"), "agents");
  assert.equal(ev(ctx, "LS.getItem('sm_list_group')"), "agent", "그리기 전엔 남아 있다");
  ev(ctx, `renderAgentRows(el('div'), '')`);
  assert.equal(ev(ctx, "LS.getItem('sm_list_group')"), null);
});

test("에이전트 줄: 마지막 활동, 지금 세션 기록이 없으면 '기록 없음', '에이전트 아닌 세션' 절은 없다", async () => {
  const { ctx } = await setup();
  ev(ctx, `globalThis.__w = el('div'); renderAgentRows(__w, '')`);
  const w = ev(ctx, "__w");
  assert.ok(walk(w).some((k) => String(k.className) === "aglast" && label(k) === "2분 전"));
  assert.ok(walk(w).some((k) => String(k.className) === "aglast" && label(k) === "기록 없음"));
  assert.ok(!has(w, "에이전트 아닌 세션"));
});

// ---- 상세의 세션 관리(H7·K12·K13·K18)
test("상세: 보관 합계, 줄마다 ⋯, 원본 없는 줄 표시", async () => {
  const { ctx, pd } = await setup({ [DET_P]: DETAIL });
  await ev(ctx, "loadAgentDetail('ag1')");
  assert.ok(has(pd, "보관 2개 · 3.0MB"));
  assert.ok(has(pd, "원본·보관본 없음"), "원본도 보관본도 없으면 장부 요약만");
  assert.equal(walk(pd).filter((k) => String(k.className).includes("aghmore")).length, 3);
});

test("⋯: 지난 세션 = 상세·이어서 열기·휴지통, 지금 세션 = 휴지통 없음, 보관본 지우기는 없다(K12)", async () => {
  const { ctx, pd } = await setup({ [DET_P]: DETAIL });
  await ev(ctx, "loadAgentDetail('ag1')");
  const mores = walk(pd).filter((k) => String(k.className).includes("aghmore"));
  mores[1].onclick({ stopPropagation() {} });
  const items = walk(ev(ctx, "__pop")).map(label).join("|");
  assert.ok(items.includes("상세") && items.includes("이어서 열기") && items.includes("휴지통"), items);
  assert.ok(!items.includes("보관본 지우기"));
  mores[0].onclick({ stopPropagation() {} });
  assert.ok(!walk(ev(ctx, "__pop")).map(label).join("|").includes("휴지통"), "지금 세션은 지우지 않는다");
});

test("정리: 고른 것만, 보관을 먼저 확인하고 보관된 것만 휴지통으로, 줄별 결과", async () => {
  const D2 = { ...DETAIL, history: [...DETAIL.history, { session_id: "dddddddd-0000-0000-0000-000000000004", started: 0, ended: 1, reason: "created", detached: false }] };
  const { ctx, pd } = await setup({ [DET_P]: D2,
    "POST /api/owner/agents/ag1/archive-now": { items: { [B]: { ok: true, status: "same" } } },
    [`POST /api/sessions/${B}/delete`]: { recoverable: true } });
  ev(ctx, `SESSIONS.push({session_id:'dddddddd-0000-0000-0000-000000000004', title:'옛', project_folder:'p', cwd:'F:/p', ended_at:new Date().toISOString(), message_count:1, size_bytes:1})`);
  await ev(ctx, "loadAgentDetail('ag1')");
  await find(pd, "🧹 지난 세션 정리").onclick();
  const m = ev(ctx, "__modal"); assert.ok(m && m.t.includes("지난 세션 정리"));
  assert.ok(has(m.body, "보관본은 남아"));
  const boxes = walk(m.body).filter((k) => k.type === "checkbox");
  assert.equal(boxes.length, 2, "끝난 지난 세션만(지금 세션 없음)"); assert.ok(boxes.every((b) => !b.checked), "기본 모두 해제");
  boxes[0].checked = true; boxes[0].onchange && boxes[0].onchange();
  boxes[1].checked = true; boxes[1].onchange && boxes[1].onchange();
  const go = m.foot.find((b) => label(b).startsWith("휴지통으로"));
  await go.onclick(); await pass();
  const arc = calls(ctx, "POST", "/api/owner/agents/ag1/archive-now");
  assert.equal(arc.length, 1); assert.equal(arc[0].body.session_ids.length, 2);
  assert.equal(calls(ctx, "POST", `/api/sessions/${B}/delete`).length, 1);
  assert.equal(calls(ctx, "POST", "/api/sessions/dddddddd-0000-0000-0000-000000000004/delete").length, 0, "보관 확인이 안 된 것은 지우지 않는다");
  assert.ok(ev(ctx, "__toasts.join('|')").includes("1개") && ev(ctx, "__toasts.join('|')").includes("못 함 1"));
});

// ---- 지난 대화 찾기(H8·K16)
test("찾기: 본문으로 보내고 결과를 누르면 그 자리 앞뒤를 읽는다", async () => {
  const hit = { ref: B + ":4", session_id: B, n: 4, ts: 1762700000, who: "사장님", source: "archive", at: 3, snippet: "…배포 규칙…" };
  const { ctx, pd } = await setup({ [DET_P]: DETAIL,
    "POST /api/owner/agents/ag1/history/search": { items: [hit], sessions: [] },
    "POST /api/owner/agents/ag1/history/read": { items: [{ ref: B + ":3", who: "Claude", ts: 1762700000, text: "앞 글", target: false },
                                                        { ref: B + ":4", who: "사장님", ts: 1762700001, text: "배포 규칙 원문", target: true }],
                                                 prev: B + ":2", next: null, source: "archive", truncated: false } });
  await ev(ctx, "loadAgentDetail('ag1')");
  const input = walk(pd).find((k) => String(k.className).includes("agq"));
  input.value = "배포 규칙";
  await find(pd, "찾기").onclick(); await pass();
  assert.deepEqual(calls(ctx, "POST", "/api/owner/agents/ag1/history/search")[0].body, { q: "배포 규칙" });
  assert.ok(has(pd, "…배포 규칙…") && has(pd, "보관본"));
  await walk(pd).find((k) => String(k.className).includes("aghit")).onclick(); await pass();
  assert.deepEqual(calls(ctx, "POST", "/api/owner/agents/ag1/history/read")[0].body, { ref: B + ":4", at: 3, before: 3, after: 3 });
  const m = ev(ctx, "__modal");
  assert.ok(m.t.includes("지난 대화") && has(m.body, "배포 규칙 원문"));
  assert.ok(walk(m.body).some((k) => String(k.className).includes("aghmsg on")), "찾은 글 강조");
  assert.ok(m.foot.some((b) => label(b).includes("앞")) && !m.foot.some((b) => label(b).includes("뒤 ▶")));
});

test("찾기: 0건이면 서버 안내를 보인다", async () => {
  const { ctx, pd } = await setup({ [DET_P]: DETAIL, "POST /api/owner/agents/ag1/history/search": { items: [], hint: "낱말을 줄이거나 다시" } });
  await ev(ctx, "loadAgentDetail('ag1')");
  walk(pd).find((k) => String(k.className).includes("agq")).value = "없는말";
  await find(pd, "찾기").onclick(); await pass();
  assert.ok(has(pd, "낱말을 줄이거나"));
});

// ---- 거절 사유(409 등)는 응답 본문의 error 로 한국어 안내 — 'HTTP 409' 만 뜨던 결함(2026-11-11)
test("이름 바꾸기 409: 같은 이름이면 'HTTP 409' 가 아니라 안내 문구", async () => {
  const { ctx } = await setup({ "POST /api/owner/agents/ag1/rename": { __err: 409, json: { error: "agent_name_taken" } } });
  ev(ctx, "prompt = () => '나'");
  await ev(ctx, "agentRename(AGENTS[0])");
  const t = ev(ctx, "__toasts.join('|')");
  assert.ok(t.includes("같은 이름의 에이전트가 이미 있습니다"), t);
  assert.ok(!t.includes("HTTP"), t);
});

test("떼기·붙이기·만들기도 거절 사유를 안내 문구로", async () => {
  const { ctx } = await setup({ "POST /api/owner/agents/ag1/detach": { __err: 409, json: { error: "not_current_session" } },
                                "POST /api/owner/agents/ag1/attach": { __err: 409, json: { error: "agent_retired" } },
                                "POST /api/owner/agents": { __err: 409, json: { error: "agent_name_taken" } } });
  ev(ctx, "globalThis.__ag = AGENTS[0]; globalThis.__s1 = SESSIONS[1]");     // 떼기 뒤 다시 불러오기가 목록을 비워도 같은 대상으로
  await ev(ctx, "agentDetach(__ag)");
  const m = await ev(ctx, "__sheet.run().then(() => 'ok', (e) => e.message)");
  assert.equal(m, "지금 세션이 아닙니다");
  await ev(ctx, `agentAttach({...__ag, session_id: null}, __s1, null)`);
  assert.ok(ev(ctx, "__toasts.join('|')").includes("쉬는 에이전트입니다"));
  ev(ctx, "agentMakeSheet(__s1)");
  await ev(ctx, "__modal.foot[1].disabled = false, __modal.foot[1].onclick()");     // 가짜 DOM 은 disabled 기본값이 없다
  const texts = walk(ev(ctx, "__modal.body")).map(label).join("|");
  assert.ok(texts.includes("같은 이름의 에이전트가 이미 있습니다"), texts);
});

test("상세(A6): 지금 세션은 세션 탭과 같은 줄(그 ⋯ 는 세션 메뉴), 지난 세션은 이력 줄 그대로", async () => {
  const { ctx, pd } = await setup({ [DET_P]: DETAIL });
  await ev(ctx, "loadAgentDetail('ag1')");
  assert.ok(has(pd, "지금 세션"));
  const cur = walk(pd).filter((k) => k.dataset && k.dataset.sid);
  assert.equal(cur.length, 1);
  assert.equal(cur[0].dataset.sid, A);
  assert.equal(walk(pd).filter((k) => String(k.className).includes("aghmore")).length, 3, "이력 줄 ⋯ 는 그대로");
  assert.equal(ev(ctx, "AG_DETAIL"), "ag1");
  ev(ctx, "paneEnter('empty')");
  assert.equal(ev(ctx, "AG_DETAIL"), null, "상세 칸을 떠나면 에이전트 선택도 끝");
});

test("폰 머리 제목은 보고 있는 탭 이름", async () => {
  const { ctx } = await setup({}, { setup: (sb) => { sb.innerWidth = 400; } });
  ev(ctx, "PANE_NOW.kind = 'list'; setListView('agents'); headList()");
  assert.equal(ev(ctx, "$('#head').textContent"), "에이전트");
  ev(ctx, "setListView('sessions'); headList()");
  assert.equal(ev(ctx, "$('#head').textContent"), "세션");
});
