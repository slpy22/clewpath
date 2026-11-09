// 갈아타기 화면(docs/designs/agent-unit.md '화면 확정' U5~U11·'재검토 확정'): 막힌 이유·비용 확인·초안 충돌·2FA·실패·옛 세션·알림 진입.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-0000-0000-0000-000000000001", B = "bbbbbbbb-0000-0000-0000-000000000002";
const AG = { id: "ag1", uid: "u1", name: "가", role: "", tags: [], status: "active", rev: 1, created: 1, session_id: B, sessions: 2, teams: [], assistant: false };
const ST = (x = {}) => ({ id: "ho1", agent_id: "ag1", agent_name: "가", status: "ready", from_session: B, to_session: null, draft: "본문", draft_rev: 1, body_max: 10, snapshot_token: "tk1", job_running: false, error: null, pending: null, attempts: [], spent_usd: 0.5, state_at: 1, needs_trust: false, cost_confirm_usd: 5, ...x });
const PRE = (x = {}) => ({ agent_id: "ag1", name: "가", session_id: B, blocked: null, est_usd: 0.8, cost_confirm_usd: 5, body_max: 10, cwd: "F:/p", model: "m1", permission_mode: null, stored: {}, active: null, trusted: true, ...x });
const PRE_P = "GET /api/owner/agents/ag1/handoff", ST_P = "GET /api/owner/handoffs/ho1", START_P = "POST /api/owner/agents/ag1/handoff";

async function setup(routes = {}, opt) {
  const ctx = await load(opt);
  ev(ctx, `CLOCK_SKEW = 0; POP = null; HANDOFFS = [];
    SESSIONS = [{session_id:${JSON.stringify(A)}, title:'옛', cwd:'F:/p', project_folder:'p', ended_at:new Date().toISOString(), message_count:1, size_bytes:1},
                {session_id:${JSON.stringify(B)}, title:'새', cwd:'F:/p', project_folder:'p', ended_at:new Date().toISOString(), message_count:1, size_bytes:1, live_terminal:true}];
    AGENTS = [${JSON.stringify(AG)}]; AGENTS_ERR = false;
    AGENTS_BY = {[${JSON.stringify(A)}]:{agent_id:'ag1',name:'가',current:false,detached:false,replaced_by:${JSON.stringify(B)}},[${JSON.stringify(B)}]:{agent_id:'ag1',name:'가',current:true,detached:false}};
    globalThis.__t = []; setTimeout = (f) => { __t.push(f); return __t.length; }; clearTimeout = () => {};
    globalThis.__calls = []; globalThis.__routes = ${JSON.stringify(routes)};
    globalThis.__toasts = []; toast = (m) => __toasts.push(m);
    loadAgentDetail = (id) => { paneEnter('empty'); __calls.push({v:'DETAIL', p:id}); };
    T = Object.assign({}, T, { api: async (v, p, o) => { const body = o && o.body; __calls.push({v, p, body, grace: o && o.grace});
      let h = __routes[v + ' ' + p]; if (Array.isArray(h)) h = h.length > 1 ? h.shift() : h[0];
      if (h === undefined) throw new Error('no route ' + v + ' ' + p);
      if (h && h.__err) { const e = new Error('HTTP ' + h.__err); e.status = h.__err; e.json = h.json; throw e; }
      return h; } });`);
  return ctx;
}
const route = (ctx, k, v) => ev(ctx, `__routes[${JSON.stringify(k)}] = ${JSON.stringify(v)}`);
const walk = (n, out = []) => { for (const k of n.children || []) { out.push(k); walk(k, out); } return out; };
const label = (k) => String(k.text ?? k.textContent ?? "");
const btn = (ctx, t) => walk(ev(ctx, "HO_BOX")).filter((k) => label(k).startsWith(t)).pop();     // 묶음의 글자는 자식을 합친 값이라 가장 안쪽을 고른다
const has = (ctx, t) => walk(ev(ctx, "HO_BOX")).some((k) => label(k).includes(t));
const calls = (ctx, v, p) => JSON.parse(ev(ctx, "JSON.stringify(__calls)")).filter((c) => c.v === v && (!p || c.p === p));
const pass = () => new Promise((r) => setImmediate(r));
async function review(ctx, st = ST(), pre = PRE()) { route(ctx, ST_P, st); route(ctx, PRE_P, pre); await ev(ctx, "openHandoff('ho1')"); await pass(); }
const area = (ctx) => walk(ev(ctx, "HO_BOX")).find((k) => String(k.className).includes("hota"));
const type = (ctx, v) => { const t = area(ctx); t.value = v; t.oninput(); };

// ---- 목록·🔔
test("머리 칩: 진행 상태 글자로 보이고 누르면 그 화면, 없으면 칩 없음", async () => {
  const ctx = await setup();
  ev(ctx, `HANDOFFS = [{id:'ho1',agent_id:'ag1',agent_name:'가',status:'ready',at:1,session_id:${JSON.stringify(B)}}]; globalThis.__w = el('div'); agentBlock(__w, AGENTS[0], '');`);
  const chips = walk(ev(ctx, "__w")).filter((k) => String(k.className).includes("agho"));
  assert.equal(label(chips[0]), "🔁 검토 대기");
  ev(ctx, `HANDOFFS = []; __w = el('div'); agentBlock(__w, AGENTS[0], '');`);
  assert.equal(walk(ev(ctx, "__w")).filter((k) => String(k.className).includes("agho")).length, 0);
});

test("🔔: 사람이 할 일(검토 대기·실패)만 센다, 준비 중·기동 중은 안 센다", async () => {
  const ctx = await setup();
  const base = ev(ctx, "todoCount()");
  ev(ctx, `HANDOFFS = ['ready','failed','preparing','starting'].map((status,i)=>({id:'h'+i,agent_id:'a'+i,agent_name:'x',status,at:1}));`);
  assert.equal(ev(ctx, "todoCount()"), base + 2);
});

test("메뉴(U10): 🔁 는 지금 세션이 있는 일반 에이전트에게만, 비서는 없음", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__box = null; openPop = (a, box) => { __box = box; }; agentMenu(AGENTS[0], SESSIONS[1], el('button'));`);
  assert.ok(walk(ev(ctx, "__box")).some((k) => label(k).includes("갈아타기")));
  ev(ctx, `agentMenu({...AGENTS[0], assistant:true}, SESSIONS[1], el('button'));`);
  assert.ok(!walk(ev(ctx, "__box")).some((k) => label(k).includes("갈아타기")));
});

// ---- ① 시작
test("①: 막혔으면 이유만 보이고 돈 드는 단추가 없다", async () => {
  const ctx = await setup({ [PRE_P]: PRE({ blocked: "live_elsewhere" }) });
  await ev(ctx, "openHandoffStart('ag1')");
  assert.ok(has(ctx, "바깥 터미널")); assert.equal(btn(ctx, "인수인계서 만들기"), undefined); assert.equal(btn(ctx, "직접 쓰기"), undefined);
});

test("①: 5달러 넘는 어림은 체크해야 켜지고, 그 금액으로 묶어 보낸다", async () => {
  const ctx = await setup({ [PRE_P]: PRE({ est_usd: 6.5 }), [START_P]: ST({ status: "preparing" }) });
  await ev(ctx, "openHandoffStart('ag1')");
  const go = btn(ctx, "인수인계서 만들기"); assert.equal(go.disabled, true); assert.ok(has(ctx, "어림 — 상한 아님"));
  const ack = walk(ev(ctx, "HO_BOX")).find((k) => k.type === "checkbox"); ack.checked = true; ack.onchange();
  assert.equal(go.disabled, false);
  await go.onclick();
  assert.equal(calls(ctx, "POST", "/api/owner/agents/ag1/handoff")[0].body.confirm_usd, 6.5);
  assert.ok(has(ctx, "만드는 중"));
});

test("①: 어림을 못 하면 -1 로 확인, 직접 쓰기는 비용 확인 없이", async () => {
  const ctx = await setup({ [PRE_P]: PRE({ est_usd: null }), [START_P]: ST() });
  await ev(ctx, "openHandoffStart('ag1')");
  assert.equal(btn(ctx, "인수인계서 만들기").disabled, true);
  await btn(ctx, "직접 쓰기").onclick();
  assert.equal(calls(ctx, "POST", "/api/owner/agents/ag1/handoff")[0].body.mode, "manual");
});

test("진행 중이면 ①을 건너뛰고 그 화면으로", async () => {
  const ctx = await setup({ [PRE_P]: PRE({ active: "ho1" }), [ST_P]: ST({ status: "preparing" }) });
  await ev(ctx, "openHandoffStart('ag1')"); await pass();
  assert.ok(has(ctx, "닫아도 계속"));
});

// ---- 비용 고리
test("hoPaid: 서버가 금액 확인을 요구하면 사람에게 묻고 그 금액으로 다시, 거절하면 null", async () => {
  const ctx = await setup({ "POST /x": [{ __err: 409, json: { error: "cost_confirm_required", cumulative_usd: 5.22 } }, { ok: 1 }] });
  ev(ctx, `globalThis.__ok = true; confirmSheet = async () => __ok;`);
  assert.equal((await ev(ctx, "hoPaid('/x',{})")).ok, 1);
  assert.equal(calls(ctx, "POST", "/x")[1].body.confirm_usd, 5.22);
  ev(ctx, `__ok = false; __routes['POST /x'] = {__err:409, json:{error:'cost_confirm_required', cumulative_usd:9}};`);
  assert.equal(await ev(ctx, "hoPaid('/x',{})"), null);
});

test("hoPaid: 폰 하루 기준을 넘으면 2차 인증 경로로, 그 값을 싣는다", async () => {
  const ctx = await setup({ "POST /x": { __err: 403, json: { error: "phone_daily_limit" } }, "POST /x-2fa": { ok: 2 } });
  ev(ctx, `confirmSheet = async () => true; ensurePriv = async () => ({grace:'g1'});`);
  assert.equal((await ev(ctx, "hoPaid('/x',{})")).ok, 2);
  assert.equal(calls(ctx, "POST", "/x-2fa")[0].grace, "g1");
});

// ---- ③ 검토
test("③: 상한을 넘으면 꺼지고 얼마 넘었는지 알려 준다(자르지 않는다), 줄이면 켜진다", async () => {
  const ctx = await setup(); await review(ctx);
  type(ctx, "가".repeat(14));
  assert.equal(btn(ctx, "갈아타기").disabled, true); assert.ok(has(ctx, "4자 넘음")); assert.equal(area(ctx).value.length, 14);
  type(ctx, "가".repeat(10));
  assert.equal(btn(ctx, "갈아타기").disabled, false);
});

test("③: 다른 기기가 먼저 고쳤으면 입력을 지우지 않고 고르게 한다", async () => {
  const ctx = await setup(); await review(ctx);
  route(ctx, "POST /api/owner/handoffs/ho1/draft", { __err: 409, json: { error: "draft_rev_conflict", current: ST({ draft: "저쪽", draft_rev: 5, snapshot_token: "tk5" }) } });
  type(ctx, "내 글");
  await ev(ctx, "HO_FLUSH()");
  assert.equal(area(ctx).value, "내 글"); assert.ok(has(ctx, "먼저 고쳤습니다")); assert.equal(btn(ctx, "갈아타기").disabled, true);
  btn(ctx, "저쪽 글 받기").onclick();
  assert.equal(area(ctx).value, "저쪽"); assert.equal(btn(ctx, "갈아타기").disabled, false);
});

test("③: [갈아타기]는 저장 뒤 같은 rev·토큰으로, 폰은 2차 인증을 싣는다", async () => {
  const ctx = await setup({ "POST /api/owner/handoffs/ho1/draft": ST({ draft: "새글", draft_rev: 2, snapshot_token: "tk2" }), "POST /api/owner/handoffs/ho1/confirm-handoff": ST({ status: "starting", to_session: A }) });
  await review(ctx);
  ev(ctx, `ensurePriv = async () => ({grace:'g9'});`);
  type(ctx, "새글");
  await btn(ctx, "갈아타기").onclick();
  const c = calls(ctx, "POST", "/api/owner/handoffs/ho1/confirm-handoff")[0];
  assert.equal(c.body.draft_rev, 2); assert.equal(c.body.snapshot_token, "tk2"); assert.equal(c.grace, "g9");
  assert.ok(has(ctx, "띄우는 중"));
});

test("③: 2차 인증을 취소하면 아무것도 안 보내고 초안 그대로(U9)", async () => {
  const ctx = await setup(); await review(ctx);
  ev(ctx, `ensurePriv = async () => null;`);
  await btn(ctx, "갈아타기").onclick();
  assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/confirm-handoff").length, 0);
  assert.ok(has(ctx, "2차 인증이 필요합니다")); assert.equal(area(ctx).value, "본문"); assert.equal(btn(ctx, "갈아타기").disabled, false);
});

test("③: 지금 세션이 바뀌면 무엇이 바뀌었는지 묻고, 새 토큰으로 다시", async () => {
  const ctx = await setup({ "POST /api/owner/handoffs/ho1/confirm-handoff": [{ __err: 409, json: { error: "target_changed", diff: ["end_size"], snapshot_token: "tk7" } }, ST({ status: "starting", to_session: A })] });
  await review(ctx);
  ev(ctx, `globalThis.__sheet = null; confirmSheet = async (o) => { __sheet = o; return true; }; ensurePriv = async () => ({});`);
  await btn(ctx, "갈아타기").onclick();
  assert.ok(ev(ctx, "__sheet.main").includes("대화가 더 진행됨"));
  const cs = calls(ctx, "POST", "/api/owner/handoffs/ho1/confirm-handoff");
  assert.equal(cs.length, 2); assert.equal(cs[1].body.snapshot_token, "tk7");
});

test("③: 늦게 온 새 요약은 덮지 않고 바꿀지 묻는다", async () => {
  const ctx = await setup({ "POST /api/owner/handoffs/ho1/pending": ST({ draft: "새 요약", draft_rev: 3, snapshot_token: "t3" }) });
  await review(ctx, ST({ pending: "새 요약" }));
  assert.equal(area(ctx).value, "본문"); assert.ok(has(ctx, "새 요약이 도착"));
  await btn(ctx, "바꾸기").onclick(); await pass();
  assert.equal(area(ctx).value, "새 요약");
  assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/pending")[0].body.accept, true);
});

test("③: Ctrl+Enter 는 [갈아타기]에 포커스만, Esc 는 저장 뒤 닫기", async () => {
  const ctx = await setup({ "POST /api/owner/handoffs/ho1/draft": ST({ draft: "x", draft_rev: 2, snapshot_token: "t2" }) });
  await review(ctx);
  let focused = false; const go = btn(ctx, "갈아타기"); go.focus = () => { focused = true; };
  ev(ctx, "HO_BOX").onkeydown({ key: "Enter", ctrlKey: true });
  assert.equal(focused, true); assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/confirm-handoff").length, 0);
  type(ctx, "x");
  ev(ctx, "HO_BOX").onkeydown({ key: "Escape" }); await pass(); await pass();
  assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/draft").length, 1); assert.equal(calls(ctx, "DETAIL").length, 1);
});

test("③: 다른 화면으로 가면 고치던 글을 저장하고 폴링을 멈춘다", async () => {
  const ctx = await setup({ "POST /api/owner/handoffs/ho1/draft": ST({ draft: "x", draft_rev: 2, snapshot_token: "t2" }) });
  await review(ctx);
  type(ctx, "x");
  ev(ctx, "paneEnter('empty')"); await pass();
  assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/draft").length, 1); assert.equal(ev(ctx, "HO_BOX"), null);
  const n = calls(ctx, "GET", "/api/owner/handoffs/ho1").length;
  await ev(ctx, "Promise.all(__t.splice(0).map(f=>f()))");
  assert.equal(calls(ctx, "GET", "/api/owner/handoffs/ho1").length, n);
});

test("③: 폴더를 바꾸면 메모리 안내, 권한을 넓히면 경고", async () => {
  const ctx = await setup(); await review(ctx); await pass();
  const fields = walk(ev(ctx, "HO_BOX")).filter((k) => k.tagName === "INPUT" || k.tagName === "SELECT" || k.tag === "input" || k.tag === "select");
  const [cwd, , perm] = fields; assert.equal(cwd.value, "F:/p");
  cwd.value = "F:/q"; cwd.oninput();
  assert.ok(has(ctx, "메모리는 따라오지"));
  perm.value = "bypassPermissions"; perm.onchange();
  assert.ok(has(ctx, "권한이 넓어집니다"));
});

// ---- ④·⑤·실패
test("④: 신뢰 질문이면 그냥 Enter 가 종료라는 것까지 알린다(X6)", async () => {
  const ctx = await setup({ [ST_P]: ST({ status: "starting", to_session: A, needs_trust: true }) });
  await ev(ctx, "openHandoff('ho1')");
  assert.ok(has(ctx, "그냥 Enter 는 종료")); assert.ok(btn(ctx, "🖥 새 세션 터미널 열기"));
});

test("④: 신뢰한 폴더로 판정돼도 15초 넘게 기록이 없으면 터미널 확인을 권한다(판정은 틀릴 수 있다, 실측)", async () => {
  const now = Math.floor(Date.now() / 1000), SKEW = -3600;   // PC 시계가 폰보다 1시간 늦다 — 폰 시계로 재면 처음부터 지난 것으로 보인다
  const ctx = await setup({ [ST_P]: ST({ status: "starting", to_session: A, state_at: now + SKEW }) });
  ev(ctx, `CLOCK_SKEW = ${SKEW};`);
  const tick = async () => { await ev(ctx, "Promise.all(__t.splice(0).map((f) => f()))"); await pass(); };
  await ev(ctx, "openHandoff('ho1')");
  assert.ok(!has(ctx, "신뢰 질문"));
  route(ctx, ST_P, ST({ status: "starting", to_session: A, state_at: now + SKEW - 16 }));
  await tick();
  assert.ok(has(ctx, "터미널을 열어")); assert.ok(has(ctx, "그냥 Enter 는 종료")); assert.ok(btn(ctx, "🖥 새 세션 터미널 열기"));
  route(ctx, ST_P, ST({ status: "starting", to_session: A, state_at: now + SKEW, needs_trust: true }));
  await tick();
  assert.ok(has(ctx, "처음 쓰는 폴더라")); assert.equal(walk(ev(ctx, "HO_BOX")).filter((k) => String(k.className).includes("warn")).length, 1);
});

test("⑤: 완료 화면에 머물고(폰에서 목록으로 밀려나지 않음) 목록은 뒤에서 새로 받는다", async () => {
  const ctx = await setup({ [ST_P]: ST({ status: "done", to_session: A }) });
  const app = stubEl("div"); ctx.document._register("app", app); app.setAttribute("data-view", "list");
  ev(ctx, `fetchAgents = async () => false; LIST_CTX = { rerender() { __calls.push({v:'RERENDER'}); } };
    T.list = async () => { __calls.push({v:'LIST'}); return { sessions: SESSIONS, handoffs: [] }; };`);
  await ev(ctx, "openHandoff('ho1')"); await pass();
  assert.equal(app.getAttribute("data-view"), "detail");
  assert.ok(has(ctx, "갈아탔습니다")); assert.ok(btn(ctx, "🖥 새 세션 터미널 열기"));
  assert.equal(calls(ctx, "LIST").length, 1); assert.ok(calls(ctx, "RERENDER").length >= 1);
});

test("실패: 단계·이유를 보이고 [닫기]는 🔔 에서 뺀다, [다시]는 ①로", async () => {
  const ctx = await setup({ [ST_P]: ST({ status: "failed", to_session: A, error: "session_exited" }), "POST /api/owner/handoffs/ho1/cancel": ST({ status: "cancelled" }), [PRE_P]: PRE() });
  await ev(ctx, "openHandoff('ho1')");
  assert.ok(has(ctx, "새 세션 띄우기")); assert.ok(has(ctx, "신뢰 질문")); assert.equal(ev(ctx, "hoTodo().length"), 1);
  await btn(ctx, "닫기").onclick();
  assert.equal(calls(ctx, "POST", "/api/owner/handoffs/ho1/cancel").length, 1); assert.equal(ev(ctx, "hoTodo().length"), 0); assert.equal(calls(ctx, "DETAIL").length, 1);
  await ev(ctx, "openHandoff('ho1')");
  await btn(ctx, "다시").onclick(); await pass();
  assert.ok(has(ctx, "인수인계서 만들기"));
});

// ---- 옛 세션 ⏭(U11)
test("⏭: 옛 세션 칩은 지금 세션으로, 흐린 줄", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__d = null; loadDetail = (s) => { __d = s.session_id; };`);
  const c = ev(ctx, "agentChip(SESSIONS[0])");
  assert.equal(label(c), "⏭ 갈아탐"); c.onclick({ stopPropagation() {} });
  assert.equal(ev(ctx, "__d"), B);
  assert.ok(ev(ctx, "sessRowEl(SESSIONS[0],'F:/p',{}).classList.contains('dim')"));
  assert.ok(!ev(ctx, "sessRowEl(SESSIONS[1],'F:/p',{}).classList.contains('dim')"));
});

test("⏭: 옛 세션 재개는 확인창을 거치고, 지금 세션은 그대로 연다", async () => {
  const ctx = await setup();
  ev(ctx, `globalThis.__sheet = null; confirmSheet = (o) => { __sheet = o; return new Promise(()=>{}); }; globalThis.__modal = 0; openModal = () => { __modal++; };`);
  ev(ctx, "showResumeChooser(SESSIONS[0])");
  assert.ok(ev(ctx, "__sheet.sub").includes("같은 이름이 둘")); assert.equal(ev(ctx, "__sheet.alt.label"), "지금 세션 열기"); assert.equal(ev(ctx, "__modal"), 0);
  ev(ctx, "showResumeChooser(SESSIONS[0], true)");
  assert.equal(ev(ctx, "__modal"), 1);
});

// ---- 알림 진입(E9)
test("알림 진입: #handoff= 를 기억하고 해시를 지운다, room 은 알림 형식일 때만 읽는다", async () => {
  const c1 = await load({ hash: "#handoff=ho_7&room=r2" });
  assert.equal(ev(c1, "PENDING_HANDOFF"), "ho_7"); assert.equal(ev(c1, "PUSH_ROOM"), "r2");
  const c2 = await load({ hash: "#room=r2" });
  assert.equal(ev(c2, "PUSH_ROOM"), "", "페어링 링크의 room 과 섞이면 안 된다");
});

test("swOpenHere: 같은 PC 면 그대로, 아는 다른 PC 면 그 PC 로 다시 열기, 모르는 PC 면 안내", async () => {
  const ctx = await setup();
  ev(ctx, `LS.setItem('sm_room','r1'); pcsLoad = () => [{room:'r1'},{room:'r2'}]; globalThis.__rl = 0; location.reload = () => { __rl++; };`);
  assert.equal(ev(ctx, "swOpenHere({type:'open-handoff',id:'h',room:'r1'})"), true);
  assert.equal(ev(ctx, "swOpenHere({type:'open-handoff',id:'h',room:'r2'})"), false);
  assert.equal(ev(ctx, "location.hash"), "#handoff=h&room=r2"); assert.equal(ev(ctx, "__rl"), 1);
  assert.equal(ev(ctx, "swOpenHere({type:'open-session',sid:'s',room:'r9'})"), false);
  assert.ok(ev(ctx, "__toasts[0]").includes("짝지어지지 않은"));
});

test("알림 진입: 아는 다른 PC 의 알림이면 부팅 때 그 PC 로 바꾼다", async () => {
  const ctx = await load({ hash: "#handoff=ho_7&room=r2", setup: (sb) => { sb.localStorage.setItem("sm_pcs", JSON.stringify([{ room: "r1" }, { room: "r2" }])); sb.localStorage.setItem("sm_room", "r1"); } });
  assert.equal(ev(ctx, "LS.getItem('sm_room')"), "r2");
});
