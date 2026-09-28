// 미검증 항목 소화(2026-09-28): 탭 배지 주기 재렌더 · 2FA OTP 프롬프트 경로 · 로컬 모드 즉시 전환.
// 실기기 대신 하네스로 "코드 경로가 그렇게 동작하는가"를 고정한다.
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const A = "aaaaaaaa-1111-2222-3333-444444444444";
const B = "bbbbbbbb-1111-2222-3333-444444444444";
const sA = { session_id: A, title: "A" }, sB = { session_id: B, title: "B" };

// $('#paneDetail') 이 매번 새 스텁을 주면 termView.mounted(main) 이 절대 참이 안 돼 fast-path 를 못 탄다 →
// 그 id 만 고정 요소로 돌려준다.
function pinPaneDetail(ctx) {
  const pd = stubEl("div");
  ctx.document.querySelector = (s) => (s === "#paneDetail" ? pd : stubEl());
  return pd;
}
function stubTerminal(ctx) {
  ev(ctx, "globalThis.__tcb = null; globalThis.__tcOpts = null; globalThis.__sends = 0;"
        + "T.terminal = (sid, cb, opts) => { globalThis.__tcb = cb; globalThis.__tcOpts = opts; return { close() {}, send() { globalThis.__sends++; } }; }");
}

// ---------------------------------------------------------------- 1) 탭 배지 20초 주기 재렌더
test("주기 재렌더: 스트립 스크롤 안 되돌리고 xterm·스트림·포커스 무접촉, 300ms 코얼레싱", async () => {
  const ctx = await load();
  const pd = pinPaneDetail(ctx);
  stubTerminal(ctx);
  ctx.ensurePriv = async () => ({});
  ev(ctx, "TABS.list = []; TABS.active = null");
  await ctx.loadTerminal(sA, false);
  assert.ok(ctx.__tcb && pd.classList.contains("term-mode"), "터미널 뷰가 떠야 한다");
  // 활성 칩 scrollIntoView 감시: 활성 탭이 바뀐 렌더에서만 불려야 한다
  let scrolls = 0;
  const strip = ev(ctx, "termView.strip");
  strip.querySelector = () => ({ scrollIntoView() { scrolls++; } });
  await ctx.loadTerminal(sB, false);                                   // fast-path(attach) + renderTabBar
  assert.equal(ev(ctx, "termView.mounted(document.querySelector('#paneDetail'))"), true, "재구축 없이 fast-path");
  assert.equal(scrolls, 1, "활성 탭 변경 → 1회 스크롤");
  const keyB = ev(ctx, "TABS.active");
  const termB = ev(ctx, `termView.tabs[${JSON.stringify(keyB)}].term`);
  const tcB = ev(ctx, `termView.tabs[${JSON.stringify(keyB)}].tc`);
  await new Promise((r) => setTimeout(r, 400));                        // attach 의 resize 타이머(80/300ms) 소진
  ev(ctx, "globalThis.__sends = 0");
  let focus = 0; termB.focus = () => { focus++; };
  let syncs = 0; ev(ctx, "tabBadge.sync = () => { globalThis.__syncs = (globalThis.__syncs||0) + 1; }");
  // 20초 틱(목록 자동 갱신)을 하네스에서 직접 실행 — 목록 재조회 → 목록 재렌더 → renderTabBarSoon
  const tick = ctx.__intervals.find((i) => i.ms === 20000);
  assert.ok(tick, "20초 목록 갱신 interval 이 등록돼 있어야 한다");
  ev(ctx, "T.list = async () => ({ sessions: [" + JSON.stringify(sA) + "," + JSON.stringify(sB) + "], now: Date.now()/1000 });"
        + "LIST_CTX = { rerender() { globalThis.__rr = (globalThis.__rr||0) + 1; } }");
  ctx.document.hidden = false;
  await tick.f(); await tick.f(); await tick.f();                       // 연속 3틱
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(ev(ctx, "globalThis.__rr"), 3, "목록은 틱마다 재렌더");
  syncs = ev(ctx, "globalThis.__syncs");
  assert.equal(syncs, 1, "탭 스트립은 300ms 로 모아 1회만 재렌더");
  assert.equal(scrolls, 1, "주기 재렌더는 사용자가 넘겨 둔 스트립 스크롤을 되돌리지 않는다");
  assert.equal(focus, 0, "주기 재렌더가 xterm 포커스를 훔치지 않는다(입력 방해 없음)");
  assert.equal(ev(ctx, `termView.tabs[${JSON.stringify(keyB)}].term`), termB, "xterm 인스턴스 유지");
  assert.equal(ev(ctx, `termView.tabs[${JSON.stringify(keyB)}].tc`), tcB, "라이브 스트림 유지(재접속 없음)");
  assert.equal(ev(ctx, "globalThis.__sends"), 0, "스트림으로 아무것도 안 보낸다");
  assert.equal(strip.children.length, 3, "칩 2 + '+ 탭' 버튼");
  assert.equal(strip.children.filter((c) => c.classList.contains("sel")).length, 1);
});

// ---------------------------------------------------------------- 2) 2FA on: OTP 프롬프트 경로
test("ensurePriv: 2FA 필수면 OTP 1회 → grace 캐시, 취소/실패는 null, 만료 오류는 grace 소거", async () => {
  const ctx = await load();
  ev(ctx, "T.api = async (v, p) => (p === '/api/owner/2fa/status' ? { required: true } : {});"
        + "T.grant = async (otp) => { globalThis.__otp = otp; if (otp === '000000') throw new Error('2fa_invalid');"
        + "  return { grace: 'g-' + otp, exp: Math.floor(Date.now()/1000) + 3600 }; };"
        + "globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);");
  let prompts = 0;
  ctx.prompt = () => { prompts++; return null; };                       // 취소
  assert.equal(await ctx.ensurePriv(), null);
  assert.equal(prompts, 1);
  ctx.prompt = () => { prompts++; return " 123456 "; };                 // 공백은 정리
  assert.equal(JSON.stringify(await ctx.ensurePriv()), JSON.stringify({ grace: "g-123456" }));
  assert.equal(ev(ctx, "globalThis.__otp"), "123456");
  assert.equal(JSON.parse(ctx.localStorage.getItem("sm_grace")).grace, "g-123456");
  assert.equal(JSON.stringify(await ctx.ensurePriv()), JSON.stringify({ grace: "g-123456" }));
  assert.equal(prompts, 2, "유효 grace 있으면 다시 묻지 않는다");
  ctx.clearGrace();
  ctx.prompt = () => { prompts++; return "000000"; };                   // 코드 불일치
  assert.equal(await ctx.ensurePriv(), null);
  assert.ok(ev(ctx, "globalThis.__toasts").at(-1).includes("2차 인증 실패"));
  assert.equal(ctx.localStorage.getItem("sm_grace"), null);
  // 원격 세션 종료: grace 만료(2fa_invalid) → grace 소거 + false, 성공 → true
  ctx.localStorage.setItem("sm_grace", JSON.stringify({ grace: "stale", exp: Math.floor(Date.now() / 1000) + 3600 }));
  ev(ctx, "T.api = async (v, p, o) => { if (p === '/api/owner/2fa/status') return { required: true };"
        + "  globalThis.__stopCred = o; throw new Error('2fa_invalid'); }");
  assert.equal(await ctx.stopSessionTerminal(sA), false);
  assert.equal(ev(ctx, "globalThis.__stopCred.grace"), "stale", "stop 은 grace 를 동봉한다");
  assert.equal(ctx.localStorage.getItem("sm_grace"), null, "만료 오류 → 다음 특권 동작은 다시 묻는다");
  ev(ctx, "T.api = async (v, p) => (p === '/api/owner/2fa/status' ? { required: true } : { ok: true })");
  ctx.localStorage.setItem("sm_grace", JSON.stringify({ grace: "g2", exp: Math.floor(Date.now() / 1000) + 3600 }));
  assert.equal(await ctx.stopSessionTerminal(sA), true);
  assert.equal(prompts, 3, "grace 있으면 stop 도 프롬프트 없음");
});

test("터미널 스트림이 2fa 오류로 끝나면 grace 소거 + 안내 문구, 2FA off 면 자격 없이 붙는다", async () => {
  const ctx = await load();
  pinPaneDetail(ctx);
  stubTerminal(ctx);
  ev(ctx, "T.api = async (v, p) => (p === '/api/owner/2fa/status' ? { required: true } : {})");
  ctx.localStorage.setItem("sm_grace", JSON.stringify({ grace: "g-old", exp: Math.floor(Date.now() / 1000) + 3600 }));
  ev(ctx, "TABS.list = []; TABS.active = null");
  await ctx.loadTerminal(sA, false);
  assert.equal(ctx.__tcOpts.grace, "g-old", "터미널 스트림에 grace 동봉");
  const key = ev(ctx, "TABS.active");
  const term = ev(ctx, `termView.tabs[${JSON.stringify(key)}].term`);
  ctx.__tcb(null, true, "2fa_required");                                // 커넥터가 eof+error 로 거부
  assert.equal(ctx.localStorage.getItem("sm_grace"), null);
  assert.ok(term.written.at(-1).includes("2차 인증 미설정"), "otpErrMsg 로 원인을 화면에 쓴다");
  assert.equal(ev(ctx, "termView.disconnected"), true, "장애로 취급(재접속 대상)");
  // 2FA off → status required:false → 프롬프트 없이 빈 자격
  ev(ctx, "T.api = async () => ({ required: false })");
  let prompts = 0; ctx.prompt = () => { prompts++; return "x"; };
  assert.equal(JSON.stringify(await ctx.ensurePriv()), "{}");
  assert.equal(prompts, 0);
});

// ---------------------------------------------------------------- 3) 로컬 모드(/app) 즉시 전환
test("로컬 모드: 탭별 iframe 보유, 전환은 .on 토글만(리로드 0), 죽은 탭만 리로드, 닫으면 about:blank", async () => {
  const ctx = await load({ location: { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" } });
  assert.equal(ev(ctx, "MODE"), "local");
  const pd = pinPaneDetail(ctx);
  let prompts = 0; ctx.prompt = () => { prompts++; return null; };
  ev(ctx, "TABS.list = []; TABS.active = null");
  await ctx.loadTerminal(sA, false);
  const keyA = ev(ctx, "TABS.active");
  const tA = ev(ctx, `termView.tabs[${JSON.stringify(keyA)}]`);
  assert.equal(tA.term, null, "로컬은 xterm 없음");
  assert.equal(tA.frame.tagName, "IFRAME");
  assert.equal(tA.frame.src, `/terminal?id=${A}&skip=1`);
  assert.ok(tA.host.classList.contains("on"));
  // src 대입 횟수 감시(리로드 = 같은 값 재대입)
  let reloads = 0; let src = tA.frame.src;
  tA.frame = { tagName: "IFRAME", get src() { return src; }, set src(v) { src = v; reloads++; } };
  await ctx.loadTerminal(sB, false);                                    // fast-path
  assert.equal(ev(ctx, "termView.mounted(document.querySelector('#paneDetail'))"), true);
  const keyB = ev(ctx, "TABS.active");
  const tB = ev(ctx, `termView.tabs[${JSON.stringify(keyB)}]`);
  assert.ok(tB.host.classList.contains("on") && !tA.host.classList.contains("on"), "전경만 .on");
  assert.equal(reloads, 0, "배경으로 간 A 의 iframe 은 그대로(WS 유지)");
  ctx.switchTab(keyA);
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(tA.host.classList.contains("on") && !tB.host.classList.contains("on"));
  assert.equal(reloads, 0, "돌아와도 리로드 없음 = 즉시 전환");
  assert.equal(ev(ctx, `termView.tabs[${JSON.stringify(keyA)}]`), tA, "인스턴스 재사용");
  // 복원 시점에 죽어 있던 탭(dead) 을 열면 새 스폰 → iframe 리로드 1회
  ev(ctx, `TABS.list.find(t => t.key === ${JSON.stringify(keyA)}).dead = true`);
  await ctx.loadTerminal(sA, false);
  assert.equal(reloads, 1, "dead 탭만 src 재대입(새 프로세스 화면)");
  assert.equal(ev(ctx, `TABS.list.find(t => t.key === ${JSON.stringify(keyA)}).dead`), false);
  // 탭 닫기(화면만) → about:blank 로 WS 를 닫게 한다
  ctx.removeTab(keyB);
  assert.equal(tB.frame.src, "about:blank");
  assert.equal(ev(ctx, `termView.tabs[${JSON.stringify(keyB)}]`), undefined);
  assert.equal(prompts, 0, "로컬은 2FA 프롬프트 없음");
  assert.ok(pd.classList.contains("term-mode"));
});
