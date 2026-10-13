// 데스크톱 2단: 왼쪽 = 목록(그룹·탐색기), 오른쪽 = 그 내용(그룹 관제·파일) — 2026-10-12 사장님 요청.
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

async function boot(width) {
  const ctx = await load();
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  ev(ctx, `innerWidth = ${width}; globalThis.__closed = 0; globalThis.__bodyAdds = 0;
    T = Object.assign({}, T, { monitor: () => ({ close(){ __closed++; }, add(){}, remove(){} }), api: async () => ({}) });
    const _ba = document.body.appendChild; document.body.appendChild = (c) => { __bodyAdds++; return _ba(c); };
    MON_GROUPS = [{id:'g1', name:'팀1', manager:'m1', subs:['w1']}, {id:'g2', name:'팀2', manager:'m2', subs:[]}];
    TEAMS = [{code:'A', name:'팀1', monitor_group_id:'g1', members:[{member_role:'manager', session_id:'m1', alias:'관리'}, {member_role:'worker', session_id:'w1', alias:'w'}]},
             {code:'B', name:'팀2', monitor_group_id:'g2', members:[{member_role:'manager', session_id:'m2', alias:'관리'}]},
             {code:'C', name:'옛팀', archived:true, monitor_group_id:null, members:[]}];`);
  return { ctx, pd };
}

test("데스크톱: 그룹을 누르면 관제가 오른쪽 칸(상세)에 뜨고, 다른 화면이 칸을 가져가면 관제 연결을 닫는다", async () => {
  const { ctx, pd } = await boot(1400);
  ev(ctx, "openMonitorGroup(MON_GROUPS[0])");
  assert.ok(pd.classList.contains("mon-mode"));
  assert.equal(ev(ctx, "__bodyAdds"), 0, "전체 화면 오버레이가 아니다");
  assert.ok(String(pd.children[0].className).includes("mon-ov inline"));
  assert.equal(ev(ctx, "MON_INLINE.gid"), "g1");
  assert.equal(ev(ctx, "LS.getItem('sm_mon_last')"), "g1");
  // 같은 그룹을 다시 누르면 그대로(재연결 없음)
  ev(ctx, "openMonitorGroup(MON_GROUPS[0])");
  assert.equal(ev(ctx, "__closed"), 0);
  // 다른 그룹 → 이전 연결 닫고 교체
  ev(ctx, "openMonitorGroup(MON_GROUPS[1])");
  assert.equal(ev(ctx, "__closed"), 1);
  assert.equal(ev(ctx, "MON_INLINE.gid"), "g2");
  // 세션 상세 등 다른 화면 → 관제 연결 닫힘
  ev(ctx, "detailPlaceholder()");
  assert.equal(ev(ctx, "__closed"), 2);
  assert.equal(ev(ctx, "MON_INLINE"), null);
  assert.ok(!pd.classList.contains("mon-mode"));
});

test("폰: 그룹 관제는 지금처럼 전체 화면 오버레이", async () => {
  const { ctx, pd } = await boot(390);
  ev(ctx, "openMonitorGroup(MON_GROUPS[0])");
  assert.equal(ev(ctx, "__bodyAdds"), 1);
  assert.ok(!pd.classList.contains("mon-mode"));
  assert.equal(ev(ctx, "MON_INLINE"), null);
});

test("팀 보기로 들어오면(데스크톱) 마지막으로 본 팀의 관제를 오른쪽에 — 터미널 화면이면 건드리지 않음", async () => {
  const { ctx, pd } = await boot(1400);
  ev(ctx, "LS.setItem('sm_mon_last','g2'); monAutoOpen();");
  assert.equal(ev(ctx, "MON_INLINE.gid"), "g2");
  const r = await boot(1400);
  r.pd.classList.add("term-mode");
  ev(r.ctx, "monAutoOpen()");
  assert.equal(ev(r.ctx, "MON_INLINE"), null);
});

test("파일 보기로 들어오면(데스크톱) 열린 탭이 없어도 오른쪽은 파일 화면", async () => {
  const { ctx, pd } = await boot(1400);
  ev(ctx, `SESSIONS = [{session_id:'s1', title:'A', cwd:'F:\\\\p', mtime:1}];
    T.api = async () => ({root:'F:\\\\p', root_name:'p', path:'', entries:[], truncated:false});
    FB.autoShow = true; globalThis.__w = el('div');`);
  await ev(ctx, "renderFbExplorer(__w)");
  assert.ok(pd.classList.contains("fb-mode"));
  assert.ok(ev(ctx, "FB.alt.children[0].textContent").includes("파일을 고르세요"));
});
