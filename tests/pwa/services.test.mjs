// 서비스 대장 S4 화면: 설정 「🔌 외부 서비스」(목록·오늘 사용량·정지·재발급·폐기)와 등록 승인 시트(검사 경고·원본 가드레일).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const SVC = { id: "nklaw", name: "북한법", status: "active", bases: { chat: "d5b86b3b-bad5-4386" }, folder: "E:/004/nklaw_chatbot",
              token_hint: "cps_nklaw_…a91f", limits: { daily_calls: 200, daily_usd: 5, max_call_usd: 1 }, today: { calls: 12, usd: 0.4 } };

async function boot(local, data) {
  const ctx = await load(local ? { location: LOCAL_LOC } : {});
  ctx.__DATA = data || {};
  ev(ctx, "globalThis.__calls = []; globalThis.__modals = []; globalThis.__toasts = []; toast = (m) => globalThis.__toasts.push(m);"
    + "openModal = (title, body, foot) => { globalThis.__modals.push({ title, body, foot }); }; closeModal = () => {};"
    + "T = { api: async (verb, path, o) => { globalThis.__calls.push([verb, path, o]); const d = globalThis.__DATA[verb + ' ' + path];"
    + "  if (d && d.__throw) throw new Error(d.__throw); if (d === undefined) throw new Error('404 ' + path); return JSON.parse(JSON.stringify(d)); } };");
  return ctx;
}
const modals = (ctx) => ev(ctx, "globalThis.__modals");

function findButtons(node, out = []) {
  if (!node) return out;
  if (node.tagName === "BUTTON" || node.onclick) out.push(node);
  (node.children || []).forEach((c) => findButtons(c, out));
  return out;
}

test("외부 서비스 목록: 이름·상태·원본·오늘 사용량·토큰 끝자리", async () => {
  const ctx = await boot(true, { "GET /api/owner/services": { services: [SVC] } });
  await ev(ctx, "showServices()");
  const t = modals(ctx).at(-1).body.text;
  assert.ok(t.includes("북한법 (nklaw)") && t.includes("사용 중"));
  assert.ok(t.includes("chat d5b86b3b") && t.includes("오늘 12/200회") && t.includes("$0.40/5") && t.includes("a91f"));
});

test("정지 → 상태 POST, 재발급 → 확인 뒤 새 토큰을 한 번 보여 준다", async () => {
  const ctx = await boot(true, { "GET /api/owner/services": { services: [SVC] },
    "POST /api/owner/services/nklaw": { service: { ...SVC, status: "suspended" } },
    "POST /api/owner/services/nklaw/rotate": { service: SVC, token: "cps_nklaw_" + "ab".repeat(32) } });
  await ev(ctx, "showServices()");
  const bs = findButtons(modals(ctx).at(-1).body).filter((b) => ["정지", "재발급", "폐기"].includes(b.textContent));
  assert.equal(bs.map((b) => b.textContent).join(","), "정지,재발급,폐기");
  await bs[0].onclick();
  const st = ev(ctx, "globalThis.__calls.find(c => c[0]==='POST' && c[1]==='/api/owner/services/nklaw')");
  assert.equal(st[2].body.status, "suspended");
  const p = bs[1].onclick();
  await new Promise((r) => setTimeout(r, 0));
  const sheet = modals(ctx).at(-1);
  assert.ok(sheet.body.text.includes("옛 토큰은 지금 바로 막힙니다"));
  sheet.foot[1].disabled = false;            // 스텁 엘리먼트는 안 쓴 속성을 함수로 돌려준다
  await sheet.foot[1].onclick();
  await p;
  const tokSheet = modals(ctx).at(-1);
  assert.equal(tokSheet.title, "🔑 새 토큰");
  assert.ok(tokSheet.body.text.includes("cps_nklaw_abab"));
});

test("폰에서는 관리하지 않는다(안내만, API 호출 없음)", async () => {
  const ctx = await boot(false, {});
  await ev(ctx, "showServices()");
  assert.ok(modals(ctx).at(-1).body.text.includes("이 PC 화면에서만"));
  assert.equal(ev(ctx, "globalThis.__calls.length"), 0);
});

test("등록 승인 시트: 서비스·폴더·한도·원본 가드레일·검사 경고를 읽을 수 있게", async () => {
  const ctx = await boot(false, {});
  const AP = { id: "ap_2", kind: "service_register", status: "pending", created: 1, expires: 2, requested_by: "s-1",
    summary: "외부 서비스 등록: 북한법(nklaw)",
    args: { service: "nklaw", name: "북한법", purpose: "법령 챗봇", folder: "E:/004/nklaw_chatbot",
            limits: { daily_calls: 200, daily_usd: 5, max_call_usd: 1 },
            bases: { chat: { new: { system_prompt: "북한 법령만", allowed_tools: ["ToolSearch"], permission_mode: "dontAsk" } },
                     gen: { adopt: "1ca057f1-c655" } },
            warnings: ["비밀값으로 보이는 파일: .env"] } };
  ev(ctx, "showApprovalSheet(" + JSON.stringify(AP) + ")");
  const t = modals(ctx).at(-1).body.text;
  assert.ok(t.includes("외부 서비스 등록") && t.includes("폴더: E:/004/nklaw_chatbot") && t.includes("하루 200회"));
  assert.ok(t.includes("원본 chat: 새로 만들기 · 권한 dontAsk · 허용 도구 ToolSearch") && t.includes("북한 법령만"));
  assert.ok(t.includes("원본 gen: 기존 세션 입양 1ca057f1"));
  assert.ok(t.includes("검사 경고 1개") && t.includes("⚠ 비밀값으로 보이는 파일: .env"));
  assert.ok(!t.includes('"service": "nklaw"'), "JSON 덤프 대신 읽기 좋게");
});

test("세션 이동 승인 시트: 옮길 곳·대상 전부·기록만 옮긴다는 안내", async () => {
  const ctx = await boot(false, {});
  const AP = { id: "ap_3", kind: "sessions_move", status: "pending", created: 1, expires: 2, requested_by: "s-1",
    summary: "세션 2개 이동 → E:/200_nk_law_center: 남북비교, 법령 수집",
    args: { new_cwd: "E:/200_nk_law_center", items: [{ session_id: "aaaa1111-x", title: "남북비교", cwd: "E:/004" }, { session_id: "bbbb2222-x", title: null, cwd: null }] } };
  ev(ctx, "showApprovalSheet(" + JSON.stringify(AP) + ")");
  const t = modals(ctx).at(-1).body.text;
  assert.ok(t.includes("세션 이동") && t.includes("옮길 곳: E:/200_nk_law_center") && t.includes("대상 세션 2개"));
  assert.ok(t.includes("남북비교 · E:/004") && t.includes("bbbb2222 · (폴더 없음)") && t.includes("기록만 옮깁니다"));
  assert.ok(!t.includes('"new_cwd"'), "JSON 덤프 대신 읽기 좋게");
});

test("세션 삭제 승인 시트: 대상 전부와 복구 안내", async () => {
  const ctx = await boot(false, {});
  const items = Array.from({ length: 12 }, (_, i) => ({ session_id: "s" + i, title: "세션" + i, cwd: "F:/w" }));
  ev(ctx, "showApprovalSheet(" + JSON.stringify({ id: "ap_4", kind: "sessions_delete", status: "pending", created: 1, expires: 2, summary: "세션 12개 휴지통으로", args: { items } }) + ")");
  const t = modals(ctx).at(-1).body.text;
  assert.ok(t.includes("대상 세션 12개") && t.includes("세션11 · F:/w") && t.includes("30일 안 복구"), "요약이 잘려도 시트엔 전부");
});

test("권한을 넓히는 새 세션 승인 시트: 넓히는 항목·옵션·첫 지시", async () => {
  const ctx = await boot(false, {});
  const AP = { id: "ap_5", kind: "session_create_auto", status: "pending", created: 1, expires: 2, summary: "새 세션(permission_mode=bypassPermissions)",
    args: { cwd: "F:/w", prompt: "모든 파일을 고쳐라", widens: ["permission_mode=bypassPermissions", "allowed_tools"],
            options: { name: "일꾼", permission_mode: "bypassPermissions", allowed_tools: ["Bash", "Edit"] } } };
  ev(ctx, "showApprovalSheet(" + JSON.stringify(AP) + ")");
  const t = modals(ctx).at(-1).body.text;
  assert.ok(t.includes("폴더: F:/w") && t.includes("⚠ 넓히는 항목: permission_mode=bypassPermissions, allowed_tools"));
  assert.ok(t.includes("allowed_tools: Bash, Edit") && t.includes("모든 파일을 고쳐라"));
});

const PLUGIN = { want: "0.36.1", remove_commands: ["claude plugin uninstall clewpath@clewpath"], items: [
  { key: "sessions", title: "세션 제어", desc: "프롬프트로 세션 다루기", command: "/clewpath:sessions", installed: false },
  { key: "workers", title: "워커 규칙", desc: "팀 운영 규칙", command: "/clewpath-workers:workers", installed: true, version: "0.36.1", up_to_date: true },
  { key: "assistant", title: "비서 규칙", desc: "비서 세션 규칙", command: "/clewpath-assistant:assistant", installed: false, old_skill: "C:/u/.claude/skills/clewpath-assistant" }] };
function boxes(node, out = []) {
  if (!node) return out;
  if (node.tagName === "INPUT" && node.type === "checkbox") out.push(node);
  (node.children || []).forEach((c) => boxes(c, out));
  return out;
}

test("플러그인 시트: 항목을 설명하고, 열기만 해서는 바꾸지 않으며, 체크한 대로 적용한다", async () => {
  const ctx = await boot(true, { "GET /api/owner/plugin": PLUGIN, "POST /api/owner/plugin/install": { moved: { assistant: "x" } } });
  await ev(ctx, "showPluginSheet()");
  const m = modals(ctx).at(-1);
  const t = m.body.text;
  for (const w of ["세션 제어", "프롬프트로 세션 다루기", "/clewpath-workers:workers", "비서 세션 규칙", "Claude 설정", "claude plugin uninstall clewpath@clewpath"]) assert.ok(t.includes(w), w);
  assert.ok(t.includes("옛 방식 스킬") && t.includes("C:/u/.claude/skills/clewpath-assistant"));
  assert.equal(ev(ctx, "globalThis.__calls.filter(c=>c[0]==='POST').length"), 0, "열기만 해서는 바꾸지 않는다");
  const [ses, wrk, asst, mv] = boxes(m.body);
  assert.deepEqual([ses.checked, wrk.checked, asst.checked, mv.checked], [true, true, true, true], "설치됨·옛 스킬·세션 제어는 기본 체크");
  wrk.checked = false;
  asst.checked = false;
  await m.foot[0].onclick();
  const call = ev(ctx, "globalThis.__calls.find(c=>c[0]==='POST')");
  assert.equal(call[1], "/api/owner/plugin/install");
  assert.deepEqual(JSON.parse(JSON.stringify(call[2].body)), { items: ["sessions"], move_old: true });
});

test("플러그인 시트: 체크를 끄면 무엇이 제거되는지 적용 전에 보인다", async () => {
  const ctx = await boot(true, { "GET /api/owner/plugin": PLUGIN });
  await ev(ctx, "showPluginSheet()");
  const m = modals(ctx).at(-1);
  const [, wrk] = boxes(m.body);
  wrk.checked = false;
  wrk.onchange();
  assert.ok(m.body.text.includes("제거: 워커 규칙"));
});

test("플러그인 시트: 옛 스킬이 없으면 옮기기 칸이 없다", async () => {
  const items = PLUGIN.items.map((x) => ({ ...x, old_skill: null }));
  const ctx = await boot(true, { "GET /api/owner/plugin": { ...PLUGIN, items } });
  await ev(ctx, "showPluginSheet()");
  const m = modals(ctx).at(-1);
  assert.ok(!m.body.text.includes("옛 방식 스킬") && boxes(m.body).length === 3);
});
