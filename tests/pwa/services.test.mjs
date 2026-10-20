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
