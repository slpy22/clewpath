// 📁 파일 보기(VS Code 사용감, 읽기 전용) — docs/designs/file-browser.md §9. Monaco 는 스텁.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const flat = (n) => [n, ...(n.children || []).flatMap(flat)];
const texts = (n) => flat(n).map((x) => String(x.textContent || ""));

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
    SESSIONS = [{session_id:'s1', title:'첫 세션', cwd:'F:\\\\p\\\\proj', mtime:10},
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
        saveViewState(){ return {top: 7}; }, restoreViewState(v){ globalThis.__restored = v; }, updateOptions(){}, focus(){}, getAction(){ return null; } }; } } });`);
  return { ctx, pd };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

test("목록 보기 전환: 세 번째 칸 📁 파일, 기기에 기억", async () => {
  const ctx = await load();
  ev(ctx, "setListView('files')");
  assert.equal(ev(ctx, "LIST_VIEW"), "files");
  assert.equal(ev(ctx, "LS.getItem('sm_list_view')"), "files");
  ev(ctx, "setListView('nope')");
  assert.equal(ev(ctx, "LIST_VIEW"), "sessions");
});

test("세션이 없으면 안내 + 폴더별(같은 cwd 는 한 번) 최근 세션 고르기", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.sid = null; SELECTED_ID = null; globalThis.__w = el('div'); renderFbExplorer(__w);");
  const t = texts(ev(ctx, "__w"));
  assert.ok(t.some((x) => x.includes("어느 세션의 폴더를 볼지")));
  const picks = t.filter((x) => x.startsWith("📁 "));
  assert.deepEqual(picks, ["📁 q — 다른 폴더", "📁 proj — 첫 세션"], "같은 폴더 세션은 하나로, 최근 순");
});

test("탐색기: 지연 로딩·폴더 먼저·무거운 폴더 흐림·보호 파일 🔒, 펼친 상태는 폴더별로 기억", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.sid = 's1'; LS.setItem('sm_fb_sid','s1'); globalThis.__w = el('div');");
  await ev(ctx, "renderFbExplorer(__w)");
  const rows = () => flat(ev(ctx, "FB.tree")).filter((n) => String(n.className).includes("fbrow"));
  assert.deepEqual(rows().map((r) => r.dataset.path), ["docs", "node_modules", ".env", "app.py"]);
  assert.ok(String(rows()[1].className).includes("heavy"));
  const envRow = rows()[2];
  assert.ok(String(envRow.className).includes("blocked") && texts(envRow).includes("🔒"));
  await rows()[0].onclick();                                     // docs 펼치기 → 그 폴더만 받는다
  assert.deepEqual(rows().map((r) => r.dataset.path), ["docs", "docs/a.md", "docs/b.txt", "node_modules", ".env", "app.py"]);
  assert.equal(ev(ctx, "JSON.stringify(__calls.filter(c => c[1].endsWith('/files')).map(c => c[2].query.path))"), '["","docs"]');
  const saved = JSON.parse(ev(ctx, "LS.getItem('sm_fb_state:F:\\\\p\\\\proj')"));
  assert.deepEqual(saved.open, ["docs"]);
  // 다른 폴더 세션으로 바꿨다 돌아오면 펼친 상태가 돌아온다(같은 폴더 세션 s2 는 같은 탐색기)
  ev(ctx, "fbUseSession(SESSIONS[2]); fbUseSession(SESSIONS[1]);");
  assert.equal(ev(ctx, "FB.open.has('docs')"), true);
  assert.equal(ev(ctx, "FB.sid"), "s2");
});

test("탭 규칙(VS Code): 한 번 = 미리보기 탭이 교체, 두 번 = 고정, 고정 탭은 남는다", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.sid='s1'; FB.cwd='F:\\\\p\\\\proj'; FB.tabs=[]; FB.kids={}; FB.tree=null;");
  const tabs = () => JSON.parse(ev(ctx, "JSON.stringify(FB.tabs.map(t => [t.path, !!t.pinned]))"));
  ev(ctx, "fbOpen('docs/a.md')"); await tick();
  ev(ctx, "fbOpen('docs/b.txt')"); await tick();
  assert.deepEqual(tabs(), [["docs/b.txt", false]], "미리보기 탭 자리를 다음 파일이 쓴다");
  ev(ctx, "fbOpen('docs/b.txt', {pin:true})"); await tick();
  ev(ctx, "fbOpen('app.py')"); await tick();
  assert.deepEqual(tabs(), [["docs/b.txt", true], ["app.py", false]]);
  assert.equal(ev(ctx, "FB.active"), "app.py");
  const tabEls = ev(ctx, "FB.tabsEl.children");
  assert.ok(String(tabEls[1].className).includes("preview") && String(tabEls[0].className).includes("on") === false);
  // 탭 닫기 → 옆 탭이 활성
  ev(ctx, "fbCloseTab('app.py')"); await tick();
  assert.equal(ev(ctx, "FB.active"), "docs/b.txt");
  assert.ok(ev(ctx, "paneDetail().classList.contains('fb-mode')"));
});

test("Monaco: 읽기 전용(키보드 안 뜸)·언어 추정·탭 전환 시 스크롤 위치 복원", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.sid='s1'; FB.cwd='F:\\\\p\\\\proj'; FB.tabs=[]; FB.ed=null; FB.tree=null; innerWidth=1280;");
  ev(ctx, "fbOpen('app.py', {pin:true})"); await tick(); await tick();
  const o = ev(ctx, "__edOpts");
  assert.equal(o.readOnly, true); assert.equal(o.domReadOnly, true);
  assert.equal(ev(ctx, "FB.tabs[0].model.lang"), "python");
  ev(ctx, "fbOpen('docs/b.txt', {pin:true})"); await tick(); await tick();
  ev(ctx, "fbActivate('app.py')"); await tick(); await tick();
  assert.deepEqual(JSON.parse(ev(ctx, "JSON.stringify(__restored)")), { top: 7 });
  assert.equal(ev(ctx, "fbLang('Dockerfile')"), "dockerfile");
  assert.equal(ev(ctx, "fbLang('x.unknown')"), "plaintext");
  assert.equal(ev(ctx, "fbLang('a.PS1')"), "powershell");
});

test("Markdown 은 미리보기로 열리고 정화(DOMPurify)를 거친다", async () => {
  const { ctx } = await boot();
  ev(ctx, `globalThis.marked = { parse: (s) => '<h1>' + s + '</h1><img src=x onerror=alert(1)>' };
    globalThis.DOMPurify = { sanitize: (h, o) => { globalThis.__san = o; return h.replace(/ onerror=[^>]*/, ''); } };
    FB.sid='s1'; FB.cwd='F:\\\\p\\\\proj'; FB.tabs=[]; FB.tree=null;`);
  ev(ctx, "fbOpen('docs/a.md')"); await tick(); await tick(); await tick();
  assert.equal(ev(ctx, "FB.tabs[0].view"), "preview");
  const html = ev(ctx, "FB.alt.children[0].innerHTML");
  assert.ok(html.includes("<h1>x = 1</h1>") && !html.includes("onerror"));
  const o = ev(ctx, "__san");
  assert.ok(o.FORBID_TAGS.includes("style") && o.FORBID_TAGS.includes("iframe") && o.FORBID_ATTR.includes("style"));
  // 원문 전환 버튼
  const btn = flat(ev(ctx, "FB.crumbEl")).find((n) => n.textContent === "원문");
  assert.ok(btn, "미리보기 ↔ 원문 전환");
});

test("오류 안내: 413(너무 큼) · 🔒 보호 파일은 내려받기 버튼도 없음", async () => {
  const { ctx } = await boot();
  ev(ctx, "FB.sid='s1'; FB.cwd='F:\\\\p\\\\proj'; FB.tabs=[]; FB.tree=null;");
  ev(ctx, "fbOpen('big.log')"); await tick(); await tick();
  let t = texts(ev(ctx, "FB.alt"));
  assert.ok(t.some((x) => x.includes("파일이 커서 바로 볼 수 없습니다(5.0MB · 상한 2.0MB)")));
  assert.ok(t.some((x) => x.startsWith("⬇ 내려받기")));
  ev(ctx, "fbShowInfo({path:'.env', err:{error:'protected'}}, fbErrText({error:'protected'}))");
  t = texts(ev(ctx, "FB.alt"));
  assert.ok(t.some((x) => x.startsWith("🔒 보호 파일")));
  assert.ok(!t.some((x) => x.startsWith("⬇")), "보호 파일은 내려받기 없음");
  assert.equal(ev(ctx, "fbErrText({error:'root_is_drive'})").includes("드라이브 맨 위"), true);
});

test("내려받기: 릴레이는 2차 인증 자격을 실어 보낸다(거절되면 안내)", async () => {
  const { ctx } = await boot();
  ev(ctx, `FB.sid='s1'; ensurePriv = async () => ({grace:'G1'}); globalThis.__dl = null;
    dlBlob = (b, n) => { globalThis.__dl = n; };
    T.api = async (v, p, o) => { __calls.push([v, p, o]); return {name:'app.py', mime:'text/plain', size:3, b64:'eD0x'}; };`);
  await ev(ctx, "fbDownload({path:'app.py', name:'app.py'})");
  const c = ev(ctx, "__calls[__calls.length-1]");
  assert.equal(c[0], "POST"); assert.ok(c[1].endsWith("/file/download")); assert.equal(c[2].grace, "G1");
  assert.equal(ev(ctx, "__dl"), "app.py");
  ev(ctx, "ensurePriv = async () => null; __dl = null;");
  await ev(ctx, "fbDownload({path:'app.py', name:'app.py'})");
  assert.equal(ev(ctx, "__dl"), null, "인증 취소면 받지 않는다");
});

test("xterm 을 Monaco(AMD 로더) 뒤에 받아도 window.Terminal 로 붙게 define 을 잠시 숨긴다", async () => {
  const ctx = await load();
  ev(ctx, "globalThis.__amd = function(){}; __amd.amd = {}; window.define = __amd; globalThis.__seen = [];"
    + "const _ap = document.head.appendChild; document.head.appendChild = (c) => { __seen.push(typeof window.define); return _ap(c); };");
  await ev(ctx, "loadXterm()");
  assert.ok(ev(ctx, "__seen").every((x) => x === "undefined"), "스크립트를 붙이는 동안 define 숨김");
  assert.equal(ev(ctx, "window.define === __amd"), true, "끝나면 되돌린다");
});
