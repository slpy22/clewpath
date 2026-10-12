// 📁 파일 보기(VS Code 사용감, 읽기 전용) — docs/designs/file-browser.md §9. Monaco 는 스텁.
// 트리 첫 층 = 작업 폴더(같은 폴더 세션은 하나로), 펼치면 그 안(2026-10-12 사장님).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
const texts = (n) => flat(n).map((x) => String(x.textContent || ""));
const RK = "f:\\p\\proj";                                  // 뿌리 키 = cwd 소문자
const id = (rel) => RK + "|" + rel;

// 가짜 Host: /files·/file 응답을 경로별로. 호출 기록은 __calls
const DIRS = {
  "": { root: "F:\\p\\proj", root_name: "proj", path: "", truncated: false, entries: [
    { name: "docs", type: "dir", mtime: 1 }, { name: "node_modules", type: "dir", heavy: true, mtime: 1 },
    { name: ".env", type: "file", size: 8, blocked: true, mtime: 1 }, { name: "app.py", type: "file", size: 20, mtime: 1 }] },
  docs: { root: "F:\\p\\proj", root_name: "proj", path: "docs", truncated: false, entries: [
    { name: "a.md", type: "file", size: 5, mtime: 1 }, { name: "b.txt", type: "file", size: 5, mtime: 1 }] },
};
async function boot() {
  const ctx = await load();
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  ctx.__DIRS = DIRS;
  ev(ctx, `globalThis.__calls = [];
    SESSIONS = [{session_id:'s1', title:'첫 세션', cwd:'F:\\\\p\\\\proj', mtime:10, live_terminal:true},
                {session_id:'s2', title:'같은 폴더', cwd:'F:\\\\p\\\\proj', mtime:5},
                {session_id:'s3', title:'다른 폴더', cwd:'F:\\\\q', mtime:20}];
    T = { api: async (v, p, o) => { __calls.push([v, p, o]);
      if (p.endsWith('/files')) { const d = __DIRS[(o.query||{}).path || '']; if (!d) { const e = new Error('HTTP 404'); e.status = 404; e.json = {error:'not_dir'}; throw e; } return JSON.parse(JSON.stringify(d)); }
      if (p.endsWith('/file')) { const q = o.query || {};
        if (q.path === 'big.log') { const e = new Error('HTTP 413'); e.status = 413; e.json = {error:'too_large', size: 5*1024*1024, limit: 2*1024*1024}; throw e; }
        return {path:q.path, name:q.path.split('/').pop(), size:5, mtime:1, kind:'text', encoding:'utf-8', text:'x = 1'}; }
      throw new Error('unexpected ' + p); } };
    loadMonaco = async () => ({ editor: {
      createModel: (text, lang) => ({ text, lang, dispose(){ this.disposed = true; } }),
      create: (host, o) => { globalThis.__edOpts = o; return { model: o.model, setModel(m){ this.model = m; }, dispose(){},
        saveViewState(){ return {top: 7}; }, restoreViewState(v){ globalThis.__restored = v; }, updateOptions(){}, focus(){}, getAction(){ return null; } }; } } });
    globalThis.__w = el('div');`);
  return { ctx, pd };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const rowsOf = (ctx) => flat(ev(ctx, "FB.tree")).filter((n) => String(n.className).split(" ").includes("fbrow"));
const kids = (r, cls) => r.children.filter((c) => String(c.className).split(" ").includes(cls));

test("목록 보기 전환: 세 번째 칸 📁 파일, 기기에 기억", async () => {
  const ctx = await load();
  ev(ctx, "setListView('files')");
  assert.equal(ev(ctx, "LIST_VIEW"), "files");
  assert.equal(ev(ctx, "LS.getItem('sm_list_view')"), "files");
  ev(ctx, "setListView('nope')");
  assert.equal(ev(ctx, "LIST_VIEW"), "sessions");
});

test("트리 첫 층 = 작업 폴더: 같은 폴더 세션은 하나로(세션 수 배지), 최근 순, 실행 중 표시", async () => {
  const { ctx } = await boot();
  await ev(ctx, "renderFbExplorer(__w)");
  const roots = rowsOf(ctx);
  assert.deepEqual(roots.map((r) => r.dataset.path), ["f:\\q|", id("")], "최근(mtime 20) 폴더가 먼저");
  const proj = roots[1];
  assert.equal(kids(proj, "rcount")[0].textContent, "2", "이 폴더를 쓰는 세션 2개");
  assert.equal(kids(proj, "rlive").length, 1, "실행 중인 세션이 있으면 초록 점");
  assert.equal(kids(roots[0], "rcount").length, 0, "세션 1개면 배지 없음");
  assert.ok(texts(ev(ctx, "__w")).includes("2개 폴더"));
  assert.equal(ev(ctx, "__calls.length"), 0, "접혀 있으면 아무것도 받지 않는다");
});

test("폴더 펼치기: 그 폴더의 가장 최근 세션으로 지연 로딩, 펼친 상태는 기기에 기억", async () => {
  const { ctx } = await boot();
  await ev(ctx, "renderFbExplorer(__w)");
  await rowsOf(ctx)[1].onclick();                                   // proj 펼치기
  const paths = () => rowsOf(ctx).map((r) => r.dataset.path);
  assert.deepEqual(paths(), ["f:\\q|", id(""), id("docs"), id("node_modules"), id(".env"), id("app.py")]);
  await rowsOf(ctx).find((r) => r.dataset.path === id("docs")).onclick();
  assert.deepEqual(paths().slice(2, 5), [id("docs"), id("docs/a.md"), id("docs/b.txt")]);
  assert.equal(ev(ctx, "JSON.stringify(__calls.map(c => [c[1], c[2].query.path]))"),
    JSON.stringify([["/api/sessions/s1/files", ""], ["/api/sessions/s1/files", "docs"]]), "s1(가장 최근)으로 읽는다");
  const saved = JSON.parse(ev(ctx, "LS.getItem('sm_fb_state2')"));
  assert.deepEqual(saved.open.sort(), [id(""), id("docs")].sort());
  const byPath = Object.fromEntries(rowsOf(ctx).map((r) => [r.dataset.path, r]));
  assert.ok(String(byPath[id("node_modules")].className).includes("heavy"));
  assert.ok(String(byPath[id(".env")].className).includes("blocked") && texts(byPath[id(".env")]).includes("🔒"));
});

test("트리 모양: 깊이만큼 안내선, 폴더는 화살표+폴더 아이콘(펼치면 open), 파일은 종류 아이콘", async () => {
  const { ctx } = await boot();
  await ev(ctx, `FB.restored = true; FB.open = new Set(${JSON.stringify([id(""), id("docs")])}); renderFbExplorer(__w)`);
  const byPath = Object.fromEntries(rowsOf(ctx).map((r) => [r.dataset.path, r]));
  assert.equal(kids(byPath[id("")], "ig").length, 0);
  assert.equal(kids(byPath[id("docs")], "ig").length, 1);
  assert.equal(kids(byPath[id("docs/a.md")], "ig").length, 2);
  assert.ok(String(kids(byPath[id("docs")], "tw")[0].className).includes("open"));
  assert.ok(!String(kids(byPath[id("node_modules")], "tw")[0].className).includes("open"));
  assert.equal(kids(byPath[id("docs")], "fi").length, 1, "폴더 아이콘");
  assert.equal(kids(byPath[id("docs/a.md")], "ic")[0].textContent, "M↓");
  assert.equal(kids(byPath[id("app.py")], "ic")[0].textContent, "py");
});

test("찾기 칸: 폴더 경로나 세션 이름으로 첫 층을 거른다", async () => {
  const { ctx } = await boot();
  await ev(ctx, "renderFbExplorer(__w)");
  await ev(ctx, "FB.q = '같은 폴더'; fbRenderTree()");
  assert.deepEqual(rowsOf(ctx).map((r) => r.dataset.path), [id("")]);
  await ev(ctx, "FB.q = 'zzz'; fbRenderTree()");
  assert.ok(texts(ev(ctx, "FB.tree")).some((t) => t.includes("맞는 폴더가 없습니다")));
});

test("세션 상세 📂 파일 보기 → 그 세션 폴더를 펼치고 선택", async () => {
  const { ctx } = await boot();
  ev(ctx, "setListView = () => {}; loadList = async () => {}; openFilesFor(SESSIONS[1]);");
  assert.equal(ev(ctx, `FB.open.has(${JSON.stringify(id(""))})`), true);
  assert.equal(ev(ctx, "FB.sel"), id(""));
  assert.equal(ev(ctx, "FB.autoShow"), true);
});

test("탭 규칙(VS Code): 한 번 = 미리보기 탭이 교체, 두 번 = 고정, 고정 탭은 남는다", async () => {
  const { ctx, pd } = await boot();
  ev(ctx, "FB.tree = null;");
  const tabs = () => JSON.parse(ev(ctx, "JSON.stringify(FB.tabs.map(t => [t.path, !!t.pinned]))"));
  const open = (rel, pin) => ev(ctx, `fbOpen(${JSON.stringify(id(rel))}${pin ? ", {pin:true}" : ""})`);
  open("docs/a.md"); await tick();
  open("docs/b.txt"); await tick();
  assert.deepEqual(tabs(), [["docs/b.txt", false]], "미리보기 탭 자리를 다음 파일이 쓴다");
  open("docs/b.txt", true); await tick();
  open("app.py"); await tick();
  assert.deepEqual(tabs(), [["docs/b.txt", true], ["app.py", false]]);
  assert.equal(ev(ctx, "FB.active"), id("app.py"));
  const tabEls = ev(ctx, "FB.tabsEl.children");
  assert.ok(String(tabEls[1].className).includes("preview"));
  ev(ctx, `fbCloseTab(${JSON.stringify(id("app.py"))})`); await tick();
  assert.equal(ev(ctx, "FB.active"), id("docs/b.txt"));
  assert.ok(pd.classList.contains("fb-mode"));
  assert.equal(ev(ctx, "__calls.filter(c => c[1].endsWith('/file')).every(c => c[1] === '/api/sessions/s1/file')"), true);
});

test("Monaco: 읽기 전용(키보드 안 뜸)·언어 추정·탭 전환 시 스크롤 위치 복원", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.tree=null; FB.ed=null; innerWidth=1280;");
  ev(ctx, `fbOpen(${JSON.stringify(id("app.py"))}, {pin:true})`); await tick(); await tick();
  const o = ev(ctx, "__edOpts");
  assert.equal(o.readOnly, true); assert.equal(o.domReadOnly, true);
  assert.equal(ev(ctx, "FB.tabs[0].model.lang"), "python");
  ev(ctx, `fbOpen(${JSON.stringify(id("docs/b.txt"))}, {pin:true})`); await tick(); await tick();
  ev(ctx, `fbActivate(${JSON.stringify(id("app.py"))})`); await tick(); await tick();
  assert.deepEqual(JSON.parse(ev(ctx, "JSON.stringify(__restored)")), { top: 7 });
  assert.equal(ev(ctx, "fbLang('Dockerfile')"), "dockerfile");
  assert.equal(ev(ctx, "fbLang('x.unknown')"), "plaintext");
  assert.equal(ev(ctx, "fbLang('a.PS1')"), "powershell");
});

test("Markdown 은 미리보기로 열리고 정화(DOMPurify)를 거친다", async () => {
  const { ctx } = await boot();
  ev(ctx, `globalThis.marked = { parse: (s) => '<h1>' + s + '</h1><img src=x onerror=alert(1)>' };
    globalThis.DOMPurify = { sanitize: (h, o) => { globalThis.__san = o; return h.replace(/ onerror=[^>]*/, ''); } };
    FB.tree=null;`);
  ev(ctx, `fbOpen(${JSON.stringify(id("docs/a.md"))})`); await tick(); await tick(); await tick();
  assert.equal(ev(ctx, "FB.tabs[0].view"), "preview");
  const html = ev(ctx, "FB.alt.children[0].innerHTML");
  assert.ok(html.includes("<h1>x = 1</h1>") && !html.includes("onerror"));
  const o = ev(ctx, "__san");
  assert.ok(o.FORBID_TAGS.includes("style") && o.FORBID_TAGS.includes("iframe") && o.FORBID_ATTR.includes("style"));
  assert.ok(flat(ev(ctx, "FB.crumbEl")).find((n) => n.textContent === "원문"), "미리보기 ↔ 원문 전환");
  assert.equal(flat(ev(ctx, "FB.crumbEl")).find((n) => String(n.className) === "croot").textContent, "proj", "이동 경로 첫 칸 = 폴더 이름");
});

test("오류 안내: 413(너무 큼) · 🔒 보호 파일은 내려받기 버튼도 없음 · 사라진 폴더", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.tree=null;");
  ev(ctx, `fbOpen(${JSON.stringify(id("big.log"))})`); await tick(); await tick();
  let t = texts(ev(ctx, "FB.alt"));
  assert.ok(t.some((x) => x.includes("파일이 커서 바로 볼 수 없습니다(5.0MB · 상한 2.0MB)")));
  assert.ok(t.some((x) => x.startsWith("⬇ 내려받기")));
  ev(ctx, "fbShowInfo({path:'.env', err:{error:'protected'}}, fbErrText({error:'protected'}))");
  t = texts(ev(ctx, "FB.alt"));
  assert.ok(t.some((x) => x.startsWith("🔒 보호 파일")));
  assert.ok(!t.some((x) => x.startsWith("⬇")), "보호 파일은 내려받기 없음");
  ev(ctx, "fbOpen('f:\\\\gone|x.txt')"); await tick(); await tick();
  assert.ok(texts(ev(ctx, "FB.alt")).some((x) => x.includes("세션을 찾지 못했습니다")), "세션이 없어진 폴더의 탭");
});

test("내려받기: 릴레이는 2차 인증 자격을 실어 보낸다(거절되면 받지 않음)", async () => {
  const { ctx } = await boot();
  ev(ctx, `fbRoots(); ensurePriv = async () => ({grace:'G1'}); globalThis.__dl = null;
    dlBlob = (b, n) => { globalThis.__dl = n; };
    T.api = async (v, p, o) => { __calls.push([v, p, o]); return {name:'app.py', mime:'text/plain', size:3, b64:'eD0x'}; };`);
  await ev(ctx, `fbDownload({root:${JSON.stringify(RK)}, path:'app.py', name:'app.py'})`);
  const c = ev(ctx, "__calls[__calls.length-1]");
  assert.equal(c[0], "POST"); assert.equal(c[1], "/api/sessions/s1/file/download"); assert.equal(c[2].grace, "G1");
  assert.equal(ev(ctx, "__dl"), "app.py");
  ev(ctx, "ensurePriv = async () => null; __dl = null;");
  await ev(ctx, `fbDownload({root:${JSON.stringify(RK)}, path:'app.py', name:'app.py'})`);
  assert.equal(ev(ctx, "__dl"), null, "인증 취소면 받지 않는다");
});

test("키보드: ↓↑ 이동, → 펼치기/첫 자식, ← 부모로, Enter 고정으로 열기", async () => {
  const { ctx } = await boot();
  await ev(ctx, "renderFbExplorer(__w)");
  ev(ctx, "FB.tree.querySelectorAll = () => FB.tree.children.filter(c => String(c.className).split(' ').includes('fbrow'));"
    + "globalThis.__k = (key) => fbTreeKey({key, preventDefault(){}});");
  ev(ctx, "FB.sel = ''; __k('ArrowDown'); __k('ArrowDown')");
  assert.equal(ev(ctx, "FB.sel"), id(""));
  ev(ctx, "__k('ArrowRight')"); await tick(); await tick();
  assert.equal(ev(ctx, `FB.open.has(${JSON.stringify(id(""))})`), true);
  ev(ctx, "__k('ArrowRight')");
  assert.equal(ev(ctx, "FB.sel"), id("docs"), "펼친 폴더에서 → 는 첫 자식");
  ev(ctx, "__k('ArrowLeft')");
  assert.equal(ev(ctx, "FB.sel"), id(""), "← 는 부모(작업 폴더)");
  ev(ctx, "__k('End')");
  assert.equal(ev(ctx, "FB.sel"), id("app.py"));
  ev(ctx, "__k('Enter')"); await tick();
  assert.equal(ev(ctx, "JSON.stringify(FB.tabs.map(t => [t.path, !!t.pinned]))"), '[["app.py",true]]');
});

test("xterm 을 Monaco(AMD 로더) 뒤에 받아도 window.Terminal 로 붙게 define 을 잠시 숨긴다", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__amd = function(){}; __amd.amd = {}; window.define = __amd; globalThis.__seen = [];"
    + "const _ap = document.head.appendChild; document.head.appendChild = (c) => { __seen.push(typeof window.define); return _ap(c); };");
  await ev(ctx, "loadXterm()");
  assert.ok(ev(ctx, "__seen").every((x) => x === "undefined"), "스크립트를 붙이는 동안 define 숨김");
  assert.equal(ev(ctx, "window.define === __amd"), true, "끝나면 되돌린다");
});
