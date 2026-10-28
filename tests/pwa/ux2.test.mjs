// 화면 재배치 2단계(2026-10-12): 목록 줄 ⋯ 메뉴 · 실행 중이면 바로 터미널 · 🤖 자동 실행 기록 접기 · 🔔 할 일 · 폰 아래 탭 막대.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m);
const cls = (e) => String(e.className);
const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const H1 = "h1h1h1h1-1111-2222-3333-444444444444";
const H2 = "h2h2h2h2-1111-2222-3333-444444444444";
const H3 = "h3h3h3h3-1111-2222-3333-444444444444";
const iso = (secAgo) => new Date(Date.now() - secAgo * 1000).toISOString();

function seed(ctx, extra = "") {
  ev(ctx, `CLOCK_SKEW = 0; SESSIONS = [
    {session_id:${JSON.stringify(A)}, title:'알파', project_folder:'E--p', cwd:'E:/p', ended_at:${JSON.stringify(iso(600))}, live_terminal:true},
    {session_id:${JSON.stringify(B)}, title:'베타', project_folder:'E--p', cwd:'E:/p', ended_at:${JSON.stringify(iso(900))}},
    {session_id:${JSON.stringify(H1)}, project_folder:'E--p', cwd:'E:/p', ended_at:${JSON.stringify(iso(5))}, headless:true, picker_hidden:true},
    {session_id:${JSON.stringify(H2)}, project_folder:'E--p-cache', cwd:'E:/p/cache', ended_at:${JSON.stringify(iso(1))}, headless:true, picker_hidden:true},
    {session_id:${JSON.stringify(H3)}, project_folder:'E--p-cache', cwd:'E:/p/cache', ended_at:${JSON.stringify(iso(2))}, headless:true, picker_hidden:true}
  ]; SELECTED_ID=null; ACTIVE_LABEL=''; COLLAPSED.clear(); ${extra}`);
}
const flat = (pop) => [pop.children[0], ...pop.children[1].children];   // 팝 = [제목, 내용]
const deep = (n) => [n, ...((n && n.children) || []).flatMap(deep)];      // 제목 없는 팝(🔔 할 일)은 통째로 훑는다
const render = (ctx, q = "") => { const wrap = stubEl("div"); ctx.__wrap = wrap; ev(ctx, `renderRows(globalThis.__wrap, ${JSON.stringify(q)})`); return wrap; };
const rowsOf = (glist) => glist.children.filter((k) => cls(k).startsWith("srow"));

test("renderRows: (Host 가 빼고 남은) 지금 돌고 있는 claude -p 는 🤖 자동 실행 칩·흐리게, 묶음 접기 없음", async () => {
  const ctx = await load();
  seed(ctx);
  const wrap = render(ctx);
  assert.ok(!wrap.children.some((g) => (g.children || []).some((k) => cls(k) === "sauto")), "접기 줄 없음");
  const all = wrap.children.filter((k) => cls(k) === "glist").flatMap(rowsOf);
  assert.equal(all.length, 5);
  const h = all.find((r) => r.dataset.sid === H1);
  assert.ok(h.classList.contains("dim"));
  same(h.children[0].children.find((k) => cls(k) === "sside").children.find((k) => cls(k) === "schips").children.filter((k) => cls(k).startsWith("schip")).map((k) => k.textContent), ["🤖 자동 실행"]);
});

test("목록 줄 클릭: 실행 중인 클루패스 터미널이면 바로 터미널, 아니면 상세 — 열린 탭이어도 프로세스가 꺼졌으면 탭을 닫고 상세", async () => {
  const ctx = await load();
  seed(ctx, "globalThis.__c=[]; loadTerminal=(s)=>__c.push(['term',s.session_id]); loadDetail=(s)=>__c.push(['detail',s.session_id]); switchTab=(k)=>__c.push(['tab',k]);");
  const wrap = render(ctx);
  const [a, b] = rowsOf(wrap.children.filter((k) => cls(k) === "glist").find((g) => rowsOf(g).some((r) => r.dataset.sid === A)));
  a.children[0].onclick(); b.children[0].onclick();
  ev(ctx, `TABS.list=[{key:'k-b', s:{session_id:${JSON.stringify(B)}}}]`);
  await b.children[0].onclick();                          // 꺼진 것으로 보이면 판정 전에 목록을 한 번 새로 받는다(받지 못하면 지금 목록으로)
  same(ev(ctx, "__c"), [["term", A], ["detail", B], ["detail", B]]);
  same(ev(ctx, "TABS.list.length"), 0);
});

test("⋯ 메뉴: 자주 쓰는 것만(재개/터미널·대화·이름·파일·상세), 위험 동작은 없다 · 항목을 누르면 닫히고 실행", async () => {
  const ctx = await load();
  seed(ctx, "globalThis.__c=[]; renameNative=(s)=>__c.push(['rename',s.session_id]); showResumeChooser=(s)=>__c.push(['resume',s.session_id]);"
    + "loadTerminal=(s)=>__c.push(['term',s.session_id]); loadViewer=(s)=>__c.push(['view',s.session_id]); loadDetail=(s)=>__c.push(['detail',s.session_id]);"
    + "openFilesFor=(s)=>__c.push(['files',s.session_id]); showApprove=(s)=>__c.push(['approve',s.session_id]); doDelete=(s)=>__c.push(['delete',s.session_id]);");
  const wrap = render(ctx);
  const [a, b] = rowsOf(wrap.children.filter((k) => cls(k) === "glist").find((g) => rowsOf(g).some((r) => r.dataset.sid === A)));
  const more = b.children.at(-1);
  assert.equal(more.textContent, "⋯"); assert.equal(more.getAttribute("aria-haspopup"), "menu");
  let stopped = false;
  const box = more.onclick({ stopPropagation: () => { stopped = true; } });
  assert.ok(stopped, "줄 클릭(상세 열기)으로 번지지 않는다");
  assert.ok(ev(ctx, "POP") !== null);
  const items = flat(box).filter((k) => cls(k).startsWith("popi"));
  const labels = items.map((k) => k.children[1].children[0].textContent);
  same(labels, ["재개", "대화 보기", "이름 바꾸기", "파일 보기", "상세 보기", "삭제"]);
  for (const bad of ["강제 종료", "되돌리기"]) assert.ok(!labels.some((l) => l.includes(bad)), "강제 종료·되돌리기는 상세 위험 구역에서만: " + bad);
  const kids = flat(box);
  assert.equal(cls(kids.at(-2)), "popsep", "삭제는 구분선 뒤로 떨어뜨린다");
  assert.equal(cls(kids.at(-1)), "popi danger");
  assert.ok(kids.at(-1).children[1].children[1].textContent.includes("30일"));
  assert.equal(box.children[0].textContent, "베타", "제목 = 세션 이름");
  items[2].onclick();
  assert.equal(ev(ctx, "POP"), null, "누르면 닫힌다");
  // 실행 중(우리 PTY) → 첫 항목이 터미널 열기 / 권한 대기면 맨 위 ✋
  ev(ctx, `SESSIONS[0].runtime={phase:'permission', permission_at:Date.now()/1000-5}`);
  const items2 = ev(ctx, "rowMenuItems(SESSIONS[0])");
  same(items2.map((x) => x[1]), ["원격 승인", "터미널 열기", "대화 보기", "이름 바꾸기", "파일 보기", "상세 보기", "삭제"]);
  assert.ok(items2.at(-1)[2].startsWith("실행 중"), "실행 중이면 바로 지우지 않고 상세(위험 구역)로");
  for (const x of items2) x[3]();
  same(ev(ctx, "__c"), [["rename", B], ["approve", A], ["term", A], ["view", A], ["rename", A], ["files", A], ["detail", A], ["detail", A]]);
  ev(ctx, "rowMenuItems(SESSIONS[1]).at(-1)[3]()");
  same(ev(ctx, "__c").at(-1), ["delete", B], "실행 중 아니면 삭제(확인창·휴지통)");
  // 다른 곳에서 실행 중(피어) → '재개…' + 출처
  const p = ev(ctx, "rowMenuItems({session_id:'x', cwd:'E:/q', peer:{origin:{origin:'terminal'}}})");
  assert.equal(p[0][1], "재개…"); assert.ok(p[0][2].startsWith("터미널에서 실행 중"));
  // Esc 로 닫힘
  ev(ctx, "rowMenu(SESSIONS[1], null)");
  ev(ctx, "POP.onKey({key:'Escape'})");
  assert.equal(ev(ctx, "POP"), null);
});

test("🔔 할 일: 배지 = 승인 요청 + 권한 대기 + 입력 대기, 시트에 묶음별로, 최근 완료는 참고(배지 제외)", async () => {
  const ctx = await load();
  const badge = stubEl("span"); badge.className = "tbadge hidden";
  const btn = stubEl("button"); btn.querySelector = () => badge;
  ctx.document._register("btn-todo", btn);
  seed(ctx, `globalThis.__c=[]; const n=Date.now()/1000;
    SESSIONS[0].runtime={phase:'permission', permission_at:n-5}; SESSIONS[1].runtime={phase:'waiting', waiting_at:n-5};
    SESSIONS.push({session_id:'r1', title:'끝난일', cwd:'E:/r', runtime:{phase:'ready', ready_at:n-30}});
    T.api = async (m,p) => { __c.push([m,p]); return {approvals:[{id:'ap1', kind:'delegate', summary:'일감 위임', status:'pending', expires:n+600}, {id:'ap2', status:'approved'}]}; };
    showApprovalSheet=(a)=>__c.push(['sheet',a.id]); showApprove=(s)=>__c.push(['approve',s.session_id]); jumpToSession=(s)=>__c.push(['jump',s.session_id]);`);
  await ev(ctx, "refreshTodo()");
  assert.equal(ev(ctx, "todoCount()"), 3);
  assert.equal(badge.textContent, "3"); assert.ok(!badge.classList.contains("hidden"));
  assert.ok(btn.title.startsWith("할 일 3건"));
  await ev(ctx, "showTodo()");
  const box = { children: deep(ev(ctx, "POP.box")) };
  const head = box.children.find((k) => cls(k) === "todo-head");
  same(head.children.map((k) => k.textContent), ["할 일", "기다리는 일 3건"]);
  const heads = box.children.filter((k) => cls(k).startsWith("todo-sec")).map((k) => k.textContent);
  same(heads, ["승인 요청", "권한 대기", "입력 대기", "최근 끝난 작업"]);
  const counts = box.children.filter((k) => cls(k) === "n").map((k) => k.textContent);
  same(counts, ["1", "1", "1"], "기다리는 묶음만 건수 표시(최근 완료는 참고)");
  const items = box.children.filter((k) => cls(k).startsWith("popi"));
  const metas = box.children.filter((k) => cls(k).startsWith("tm")).map((k) => k.textContent);
  same(metas, ["10분 남음", "방금", "방금", "방금"], "오른쪽 = 한국어 상대 시각(만료까지 / 상태가 된 뒤)");
  assert.ok(cls(items.at(-1)).includes("done"), "최근 끝난 작업은 작게");
  items.forEach((k) => k.onclick());
  same(ev(ctx, "__c").filter((x) => x[0] !== "GET"), [["sheet", "ap1"], ["approve", A], ["jump", B], ["jump", "r1"]]);
  assert.ok(ev(ctx, "__c").some((x) => x[1] === "/api/owner/approvals"));
  // 아무것도 없으면 배지 숨김 + 빈 안내
  ev(ctx, "SESSIONS=[]; T.api = async () => ({approvals:[]})");
  await ev(ctx, "showTodo()");
  assert.ok(badge.classList.contains("hidden"));
  assert.ok(deep(ev(ctx, "POP.box")).some((k) => cls(k) === "popempty"));
  assert.equal(deep(ev(ctx, "POP.box")).find((k) => cls(k) === "todo-head").children[1].textContent, "모두 처리됨");
  // 승인 목록 조회 실패해도 배지는 세션 기준으로 계속
  ev(ctx, "T.api = async () => { throw new Error('403'); }; TODO_APPROVALS=[]");
  await ev(ctx, "refreshTodo()");
  assert.equal(ev(ctx, "todoCount()"), 0);
});

test("폰 아래 탭 막대: 보기 전환 · 같은 탭은 맨 위로 · 비서는 비서 열기", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__c=[]; loadList=()=>__c.push(['list',LIST_VIEW]); openAssistant=()=>__c.push(['assist']); LIST_CTX={rerender(){}}; setListView('sessions');");
  const pl = stubEl("div"); pl.scrollTop = 500; ctx.document._register("paneList", pl);
  const app = stubEl("div"); app.setAttribute("data-view", "detail"); ctx.document._register("app", app);
  ev(ctx, "tabbarGo('groups')");
  assert.equal(ev(ctx, "LIST_VIEW"), "groups");
  assert.equal(app.getAttribute("data-view"), "list", "상세에서 눌러도 목록으로");
  ev(ctx, "tabbarGo('groups')");
  assert.equal(pl.scrollTop, 0, "보던 탭을 또 누르면 맨 위로");
  ev(ctx, "tabbarGo('assist')");
  same(ev(ctx, "__c"), [["list", "groups"], ["assist"]]);
  assert.equal(ev(ctx, "LIST_VIEW"), "groups", "비서는 보기를 바꾸지 않는다");
});

test("파일 탐색기 뿌리: 자동 실행 기록 폴더는 올리지 않는다", async () => {
  const ctx = await load();
  seed(ctx);
  same(ev(ctx, "fbRoots().map(r=>r.cwd)"), ["E:/p"]);
});

test("쉘·모니터: 목록 줄에 🐚 쉘 n · 📡 모니터 m 칩, phase background 는 '백그라운드'(완료 아님), #라벨 칩 없음", async () => {
  const ctx = await load();
  seed(ctx, `SESSIONS[1].bg={shells:2, monitors:1}; SESSIONS[1].labels=['북한법']; SESSIONS[1].runtime={phase:'background', ready_at:Date.now()/1000-5};
    SESSIONS[0].bg={shells:0, monitors:0};`);
  const wrap = render(ctx);
  const rs = rowsOf(wrap.children.filter((k) => cls(k) === "glist").find((g) => rowsOf(g).some((r) => r.dataset.sid === A)));
  // 칩 한 개 = 줄 목록(여러 줄이면 위아래로 쌓은 칩의 줄들, 2026-10-28)
  const chips = (r) => (r.children[0].children.find((k) => cls(k) === "sside")?.children.find((k) => cls(k) === "schips")?.children || []).filter((k) => cls(k).startsWith("schip"))
    .map((k) => (k.children.length ? k.children.map((x) => x.textContent) : [k.textContent]));
  const bChip = rs.find((r) => r.dataset.sid === B).children[0].children.find((k) => cls(k) === "sside").children.find((k) => cls(k) === "schips").children.find((k) => cls(k).startsWith("schip bg"));
  same(chips(rs.find((r) => r.dataset.sid === B)), [["🐚 2", "📡 1"]], "쉘·모니터는 위아래 두 줄, 아이콘+숫자만(뜻은 툴팁)");
  assert.equal(cls(bChip), "schip bg stack");
  assert.ok(bChip.title.includes("🐚 쉘 2 · 📡 모니터 1"));
  same(chips(rs.find((r) => r.dataset.sid === A)), [], "0개면 칩 없음");
  const b = ev(ctx, "sessionBadge(SESSIONS[1])");
  assert.equal(b.text, "백그라운드"); assert.equal(b.pulse, true);
  assert.equal(ev(ctx, "bgText({bg:{shells:0,monitors:3}})"), "📡 모니터 3");
  assert.equal(ev(ctx, "bgText({})"), "");
  // 서브에이전트도(2026-10-28): 한 종류만이면 한 줄 칩, 셋이면 세 줄
  same(ev(ctx, "bgLines({bg:{shells:1,monitors:0,agents:2}})"), ["🐚 쉘 1", "🤖 에이전트 2"]);
  same(ev(ctx, "bgLines({bg:{shells:1,monitors:0,agents:2}}, true)"), ["🐚 1", "🤖 2"]);
  seed(ctx, `SESSIONS[1].bg={shells:0, monitors:0, agents:1}; SESSIONS[1].runtime={phase:'background', ready_at:Date.now()/1000-5};
    SESSIONS[0].bg={shells:1, monitors:2, agents:3};`);
  const rs2 = rowsOf(render(ctx).children.filter((k) => cls(k) === "glist").find((g) => rowsOf(g).some((r) => r.dataset.sid === A)));
  same(chips(rs2.find((r) => r.dataset.sid === B)), [["🤖 1"]]);
  same(chips(rs2.find((r) => r.dataset.sid === A)), [["🐚 1", "📡 2", "🤖 3"]]);
  // 🔔 최근 끝난 작업에 안 들어간다
  assert.equal(ev(ctx, "todoSessions().ready.length"), 0);
});

test("상세 태그: 이름 아래 #태그 칩(× 로 빼기) + ＋ 태그 → 입력·기존 태그 골라 저장, 목록 필터도 갱신", async () => {
  const ctx = await load();
  seed(ctx, `SESSIONS[0].labels=['북한법']; SESSIONS[1].labels=['논문','북한법'];
    globalThis.__c=[]; T.api=async(m,p,o)=>{ __c.push([m,p,o&&o.body]); return {}; }; toast=()=>{};
    globalThis.__rr=0; LIST_CTX={rerender(){ __rr++; }}; setListView('sessions');
    globalThis.__modal=null; openModal=(t,b,f)=>{ __modal={t,b,f}; }; closeModal=()=>{};`);
  const wrap = stubEl("div"); ctx.__w = wrap;
  ev(ctx, "renderDetailTags(globalThis.__w, SESSIONS[1])");
  same(wrap.children.map((k) => k.children.length ? k.children[0].textContent : k.textContent), ["#논문", "#북한법", "＋ 태그"]);
  // × → 그 태그만 빼고 저장
  await wrap.children[0].children[1].onclick();
  same(ev(ctx, "__c").at(-1), ["POST", `/api/sessions/${B}/labels`, { labels: ["북한법"] }]);
  same(ev(ctx, "SESSIONS[1].labels"), ["북한법"]);
  assert.ok(ev(ctx, "__rr") >= 1, "목록(태그 필터) 다시 그림");
  same(wrap.children.map((k) => k.children.length ? k.children[0].textContent : k.textContent), ["#북한법", "＋ 태그"]);
  // ＋ 태그 → 입력 '#연구, 메모' + 기존 '북한법' 눌러 빼기 → 저장
  const ed = ev(ctx, "editSessionTags(SESSIONS[1])");
  assert.ok(ev(ctx, "__modal.t").startsWith("🏷 태그"));
  const picks = ed.box.children.map((k) => k.textContent);
  same(picks, ["#북한법"], "고를 수 있는 기존 태그");
  ed.box.children[0].onclick();
  ed.inp.value = "#연구, 메모";
  await ed.save.onclick();
  same(ev(ctx, "__c").at(-1)[2], { labels: ["연구", "메모"] });
  // 상세(loadDetail) 머리에 태그 줄이 들어간다
  const pd = stubEl("div"); ctx.document._register("paneDetail", pd);
  ev(ctx, "T.api=async()=>({})");
  await ctx.loadDetail(ev(ctx, "SESSIONS[1]"));
  const hd = pd.children[1].children[0];
  assert.equal(String(hd.children.at(-1).className), "dtags");
});

test("🧹 정기 작업 화면: 작업별 켜짐·마지막 결과·지금 실행, 기준 시간 칩, 끌 후보 · 원격은 2FA 로 바꾼다 · 📌 항상 켜 두기", async () => {
  const ctx = await load();   // 릴레이(폰)
  ev(ctx, `globalThis.__c=[]; toast=()=>{}; ensurePriv=async()=>({grace:'g1'});
    globalThis.__st={jobs:[{id:'idle_sessions',name:'쉬는 세션 정리',desc:'d',interval_s:900,enabled:true,last_run:0,last_ok:null,last_summary:null},
                          {id:'models',name:'모델 목록 갱신',desc:'m',interval_s:86400,enabled:false,last_run:Date.now()/1000-120,last_ok:true,last_summary:'7개'}],
      idle_hours:4, idle_hours_choices:[1,2,4,8], builtin:[{name:'업데이트 확인',every:'주기',desc:'x'}],
      idle_candidates:[{session_id:'a',title:'논문',idle_s:5*3600}]};
    T.api=async(m,p,o)=>{ __c.push([m,p,o&&o.body,o&&o.grace]); return m==='GET'?__st:Object.assign({},__st,{idle_hours:(o.body&&o.body.hours)||4}); };`);
  const body = stubEl("div"); ctx.__b = body;
  ev(ctx, "renderHousekeeping(globalThis.__b, __st)");
  const cards = body.children.filter((k) => String(k.className) === "hkcard");
  assert.equal(cards.length, 2);
  const txt = (k) => k.text;
  assert.ok(txt(cards[0]).includes("15분마다") && txt(cards[0]).includes("켜짐") && txt(cards[0]).includes("아직 안 돎"));
  assert.ok(txt(cards[0]).includes("끌 세션 1개: 논문(5시간)"));
  assert.ok(txt(cards[1]).includes("1일마다") && txt(cards[1]).includes("꺼짐") && txt(cards[1]).includes("7개"));
  assert.ok(body.text.includes("항상 도는 것"));
  // 모델 목록 켜기(원격 → grace 동봉)
  await cards[1].children[0].children.at(-1).onclick();
  const last = ev(ctx, "__c").at(-1);
  assert.equal(last[1], "/api/owner/housekeeping"); assert.equal(JSON.stringify(last[2]), JSON.stringify({ job: "models", on: true })); assert.equal(last[3], "g1");
  // 기준 8시간
  const hours = cards[0].children.find((k) => String(k.className) === "hkhours");
  await hours.children.find((k) => k.textContent === "8시간").onclick();
  assert.equal(JSON.stringify(ev(ctx, "__c").find((c) => c[2] && c[2].hours)[2]), JSON.stringify({ hours: 8 }));
  // 지금 실행
  await cards[1].children.at(-1).onclick();
  assert.ok(ev(ctx, "__c").some((c) => c[1] === "/api/owner/housekeeping/models/run"));
  // 📌 항상 켜 두기
  ev(ctx, "SESSIONS=[{session_id:'s1', keep:false}]");
  let drawn = 0; ctx.__redraw = () => drawn++;
  await ev(ctx, "toggleKeep(SESSIONS[0], globalThis.__redraw)");
  assert.equal(JSON.stringify(ev(ctx, "__c").at(-1).slice(0, 3)), JSON.stringify(["POST", "/api/sessions/s1/keep", { keep: true }]));
  assert.equal(ev(ctx, "SESSIONS[0].keep"), true); assert.equal(drawn, 1);
});

test("곧 지워질 세션(claude 보관 기간): 7일 안 = 목록 ⏳ 칩·🔔 묶음, 3일 안만 숫자·빨강", async () => {
  const ctx = await load();
  seed(ctx, `globalThis.__c=[]; const n=Date.now()/1000;
    SESSIONS[0].cleanup_at=n+2.5*86400; SESSIONS[1].cleanup_at=n+5.5*86400; SESSIONS[2].cleanup_at=n+3600;
    SESSIONS.push({session_id:'far', title:'먼일', cwd:'E:/f', cleanup_at:n+20*86400});
    T.api = async () => ({approvals:[]}); loadDetail=(s)=>__c.push(['detail',s.session_id]);`);
  same(ev(ctx, "todoSessions().gone.map(s=>s.session_id)"), [A, B], "자동 실행 기록(헤드리스)·먼 것은 빼고 가까운 순");
  assert.equal(ev(ctx, "todoCount()"), 1, "3일 안만 🔔 숫자");
  assert.equal(ev(ctx, "cleanupText(SESSIONS[0])"), "2일 뒤 지워짐");
  await ev(ctx, "showTodo()");
  const all = deep(ev(ctx, "POP.box"));
  assert.ok(all.some((k) => cls(k) === "todo-sec k-gone" && k.textContent === "곧 지워질 세션"));
  const items = all.filter((k) => cls(k).includes("k-gone") && cls(k).startsWith("popi"));
  assert.equal(items.length, 2);
  assert.ok(cls(items[0].children.at(-1)).includes("soon"), "3일 안은 강조");
  items[1].onclick();
  same(ev(ctx, "__c").at(-1), ["detail", B], "누르면 상세(경고·내보내기)");
  const rows = render(ctx).children.filter((k) => cls(k) === "glist").flatMap(rowsOf);
  const chips = deep({ children: rows }).filter((k) => cls(k).startsWith("schip gone")).map((k) => [cls(k), k.textContent]);
  assert.ok(chips.some(([c, t]) => c === "schip gone hot" && t === "⏳ 2일 뒤 지워짐"));
  assert.ok(chips.some(([c, t]) => c === "schip gone" && t === "⏳ 5일 뒤 지워짐"));
  assert.ok(!chips.some(([, t]) => t.includes("20일")), "7일 넘게 남으면 칩 없음");
});
