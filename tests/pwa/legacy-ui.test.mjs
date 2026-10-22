// 옛 화면(/legacy = session_manager/static/index.html) — 맥 실기 3b-2 이슈 A: /legacy 로 열면 API 를 /legacy/api/… 로 불러 404,
// 재개 창이 404 본문으로 열려 cd 없는 명령과 '새 cmd 창에서 실행' 단추가 나왔다. 화면 파일에서 해당 코드만 떼어 돌린다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../../session_manager/static/index.html", import.meta.url), "utf8");
const pick = (re, what) => {
  const m = html.match(re);
  assert.ok(m, `${what} 를 찾지 못함 — 화면 코드가 바뀌었으면 이 시험도 맞춘다`);
  return m[0];
};
const BASE_LINE = pick(/^const BASE=.*$/m, "BASE 줄");
const RESUME = pick(/^let _resume=null;[\s\S]*?^function renderResumeCmd\(\)[^\n]*$/m, "재개 함수들");

function resumeCtx(status, body) {
  const ctx = vm.createContext({
    BASE: "", toasts: [], modals: [], urls: [],
    fetch: async (url) => {
      ctx.urls.push(url);
      return { ok: status >= 200 && status < 300, status, json: async () => body };
    },
    toast: (m, k) => ctx.toasts.push([m, k]),
    openModal: (title, b, foot) => ctx.modals.push({ title, foot }),
    esc: (s) => String(s ?? ""),
    document: { getElementById: () => null },
  });
  vm.runInContext(RESUME, ctx);
  return ctx;
}

test("BASE: 끝의 /legacy 를 떼고 nginx 접두어만 남긴다", () => {
  for (const [path, want] of [["/legacy", ""], ["/legacy/", ""], ["/session_mgr/legacy", "/session_mgr"],
                              ["/session_mgr/legacy/", "/session_mgr"], ["/", ""], ["/session_mgr/", "/session_mgr"]]) {
    const ctx = vm.createContext({ location: { pathname: path } });
    vm.runInContext(`${BASE_LINE}\nglobalThis.__b = BASE;`, ctx);
    assert.equal(ctx.__b, want, path);
  }
});

test("재개 창: 응답이 404 면 창을 열지 않고 알린다(옛 동작: 빈 cwd 로 열림)", async () => {
  const ctx = resumeCtx(404, { detail: "Not Found" });
  await vm.runInContext("resumeSession('s1')", ctx);
  assert.equal(ctx.modals.length, 0);
  assert.equal(ctx.toasts.length, 1);
  assert.match(ctx.toasts[0][0], /HTTP 404/);
  assert.equal(ctx.toasts[0][1], "err");
  const ctx2 = resumeCtx(200, { error: "세션 없음" });
  await vm.runInContext("resumeSession('s1')", ctx2);
  assert.deepEqual([ctx2.modals.length, ctx2.toasts[0][0]], [0, "세션 없음"]);
});

test("재개 창: 맥(sh)은 cd '…' 에 단추 없음, Windows(cmd)는 cd /d 에 단추 있음", async () => {
  const mac = resumeCtx(200, { cwd: "/Users/a b/it's", shell: "sh", can_launch: false, note: "" });
  await vm.runInContext("resumeSession('s1')", mac);
  assert.equal(mac.urls[0], "/api/sessions/s1/resume-command");
  assert.equal(mac.modals.length, 1);
  assert.ok(!mac.modals[0].foot.includes("새 cmd 창에서 실행"));
  assert.equal(vm.runInContext("_resumeCmd()", mac), "cd '/Users/a b/it'\\''s' && claude --resume s1 --dangerously-skip-permissions");
  const win = resumeCtx(200, { cwd: "C:\\work dir", shell: "cmd", can_launch: true, note: "" });
  await vm.runInContext("resumeSession('s2')", win);
  assert.ok(win.modals[0].foot.includes("새 cmd 창에서 실행"));
  assert.equal(vm.runInContext("_resumeCmd()", win), 'cd /d "C:\\work dir" && claude --resume s2 --dangerously-skip-permissions');
});
