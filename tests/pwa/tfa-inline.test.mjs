// 2차 인증이 꺼진 PC(2026-10-26, TODOS pclink 「PC 화면 승인은 OTP 뿐이라 2FA 미설정이면 짝짓기 불가(화면 경고만)」):
// 승인은 사람 증명(A-2)이 있어야 하고 pclink 는 폰에서도 2차 인증(D11)이라, 2FA 가 없으면 짝짓기·첫 연락을 승인할 길이 없다.
// 보안 규칙은 그대로 두고, 막히는 그 자리(PC 짝 화면·승인 시트)에서 바로 2차 인증을 설정하고 돌아오게 한다(로컬 PC 화면만 — 설정은 로컬 전용).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev } from "./harness.mjs";

const LOCAL_LOC = { pathname: "/app", href: "http://127.0.0.1:5100/app", origin: "http://127.0.0.1:5100", protocol: "http:", host: "127.0.0.1:5100" };
const NOW = Math.floor(Date.now() / 1000);
const STATUS = { host_id: "abcdef0123456789abcdef0123456789", name: "데스크톱", paused: false, room_ready: true, pairs: [], invites: [], sessions: [] };
const OFF = { required: false, has_secret: false };
const AP = { id: "ap_p", kind: "pclink_pair", args: { action: "invite" }, status: "pending", created: NOW, expires: NOW + 1800,
  requested_by: "owner", summary: "PC 짝짓기 초대 만들기" };
const clone = (o) => JSON.parse(JSON.stringify(o));

async function boot(local, data) {
  const ctx = await load(local ? { location: LOCAL_LOC } : {});
  ctx.__DATA = clone({ "GET /api/owner/pclink": STATUS, "GET /api/owner/pclink/audit": { events: [] },
    "GET /api/owner/2fa/status": OFF,
    "POST /api/owner/2fa/provision": { secret: "JBSWY3DPEHPK3PXP", otpauth: "otpauth://totp/x", qr_svg: "data:image/svg+xml;base64,AA==" },
    ...(data || {}) });
  ev(ctx, `globalThis.__calls = []; globalThis.__modals = []; globalThis.__toasts = [];
    toast = (m) => __toasts.push(m);
    openModal = (title, body, foot) => { __modals.push({ title, body, foot }); }; closeModal = () => {};
    T = { api: async (verb, path, o) => { __calls.push([verb, path, o]); const d = globalThis.__DATA[verb + ' ' + path];
        if (d === undefined) throw new Error('404 ' + path);
        return JSON.parse(JSON.stringify(d)); } };`);
  return ctx;
}
const tick = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };
const last = (ctx) => ev(ctx, "globalThis.__modals.at(-1)");
const calls = (ctx, verb, path) => ev(ctx, "globalThis.__calls").filter((c) => c[0] === verb && c[1] === path);
const findBtn = (node, label) => {
  if (!node) return null;
  if (node.tagName === "BUTTON" && String(node.textContent || "").includes(label)) return node;
  for (const c of node.children || []) { const b = findBtn(c, label); if (b) return b; }
  return null;
};
const footBtn = (m, label) => (m.foot || []).find((b) => String(b.textContent || "").includes(label));

test("PC 짝 화면(로컬, 2FA 비밀 없음): 경고 옆 「🔒 지금 2차 인증 설정」 → 설정(QR) → 「↩ PC 짝으로 돌아가기」 가 짝 화면을 다시 연다", async () => {
  const ctx = await boot(true);
  await ev(ctx, "showPclink()"); await tick();
  const m = last(ctx);
  assert.ok(m.body.text.includes("2차 인증이 꺼져 있어 짝짓기·첫 연락을 승인할 수 없습니다"));
  const b = findBtn(m.body, "지금 2차 인증 설정");
  assert.ok(b, "경고 자리에서 바로 설정할 단추");
  await b.onclick(); await tick();
  const sec = last(ctx);
  assert.equal(sec.title, "🔒 보안 · 2차 인증");
  await footBtn(sec, "2FA 설정").onclick(); await tick();
  assert.equal(calls(ctx, "POST", "/api/owner/2fa/provision").length, 1);
  assert.ok(sec.body.text.includes("2차 인증이 켜졌습니다"));
  const back = findBtn(sec.body, "PC 짝으로 돌아가기");
  assert.ok(back, "설정이 끝나면 하던 화면으로 돌아가는 단추");
  const before = calls(ctx, "GET", "/api/owner/pclink").length;
  await back.onclick(); await tick();
  assert.equal(calls(ctx, "GET", "/api/owner/pclink").length, before + 1);
  assert.equal(last(ctx).title.includes("PC 짝"), true);
});

test("PC 짝 화면(로컬, 비밀은 있고 꺼짐): 「켜기」 단추 — 켠 뒤에도 돌아가기 단추가 남는다", async () => {
  const ctx = await boot(true, { "GET /api/owner/2fa/status": { required: false, has_secret: true },
    "POST /api/owner/2fa/toggle": { ok: true } });
  await ev(ctx, "showPclink()"); await tick();
  const m = last(ctx);
  assert.ok(m.body.text.includes("2차 인증이 꺼져 있어"));
  await findBtn(m.body, "지금 2차 인증 켜기").onclick(); await tick();
  ctx.__DATA["GET /api/owner/2fa/status"] = { required: true, has_secret: true };
  await footBtn(last(ctx), "2FA 켜기").onclick(); await tick();
  assert.equal(calls(ctx, "POST", "/api/owner/2fa/toggle").length, 1);
  assert.ok(findBtn(last(ctx).body, "PC 짝으로 돌아가기"), "켜기 뒤 다시 그린 보안 화면에도 돌아가기");
});

test("폰(릴레이): 설정 단추 없음(설정은 PC 로컬 전용) — 어디서 켜는지만 · 2FA 가 켜져 있으면 경고·단추 모두 없음", async () => {
  const phone = await boot(false);
  await ev(phone, "showPclink()"); await tick();
  const t = last(phone).body.text;
  assert.ok(t.includes("2차 인증이 꺼져 있어") && t.includes("PC 화면의 설정 → 🔒 보안"));
  assert.equal(findBtn(last(phone).body, "지금 2차 인증"), null);
  const ok = await boot(true, { "GET /api/owner/2fa/status": { required: true, has_secret: true } });
  await ev(ok, "showPclink()"); await tick();
  assert.ok(!last(ok).body.text.includes("2차 인증이 꺼져") && findBtn(last(ok).body, "지금 2차 인증") === null);
});

test("승인 시트(로컬, 2FA 꺼짐): 코드 칸 대신 막힌 이유 + 「🔒 지금 2차 인증 설정」 → 설정 뒤 「↩ 승인으로 돌아가기」 가 같은 승인 시트를 다시 연다", async () => {
  const ctx = await boot(true);
  ev(ctx, `showApprovalSheet(${JSON.stringify(AP)}, {})`); await tick();
  const sheet = last(ctx);
  assert.equal(sheet.title, "✅ 승인 요청");
  assert.ok(sheet.body.text.includes("2차 인증이 꺼져 있어 이 PC 화면에서는 승인할 수 없습니다"));
  await findBtn(sheet.body, "지금 2차 인증 설정").onclick(); await tick();
  await footBtn(last(ctx), "2FA 설정").onclick(); await tick();
  await findBtn(last(ctx).body, "승인으로 돌아가기").onclick(); await tick();
  const again = last(ctx);
  assert.equal(again.title, "✅ 승인 요청");
  assert.ok(again.body.text.includes("PC 짝짓기 초대 만들기"));
});

test("승인 시트(로컬, 2FA 켜짐): 지금처럼 코드 칸만 — 경고·설정 단추 없음 · 폰 승인 시트는 상태를 묻지 않는다", async () => {
  const ctx = await boot(true, { "GET /api/owner/2fa/status": { required: true, has_secret: true } });
  ev(ctx, `showApprovalSheet(${JSON.stringify(AP)}, {})`); await tick();
  const t = last(ctx).body.text;
  assert.ok(t.includes("2차 인증 코드로 승인") && !t.includes("2차 인증이 꺼져 있어"));
  const phone = await boot(false);
  ev(phone, `showApprovalSheet(${JSON.stringify({ ...AP, kind: "team_create" })}, {})`); await tick();
  assert.equal(calls(phone, "GET", "/api/owner/2fa/status").length, 0);
});
