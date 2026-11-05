// 데이터 저장 정리(2026-11-05, R1): 🧹 정기 작업 화면 아래 '저장소' 칸 — 상태 DB·마지막 백업·알림, 애매한 거울 보존본으로 되돌리기(D5).
import { test } from "node:test";
import assert from "node:assert/strict";
import { load, ev, stubEl } from "./harness.mjs";

const ST = {
  sqlite: "3.45.1", db: "team.db", schema: 6, last_backup: "state-20261105.db",
  notices: [{ at: 1, kind: "store", event: "mirror_ambiguous", doc: "labels" }],
  kept: ["labels.json.ambiguous.20261105-101010"],
};

test("저장소 칸: 파일·백업·알림을 보이고, 보존본 되돌리기는 확인 뒤 2FA 로 보낸다", async () => {
  const ctx = await load();   // 릴레이(폰)
  ev(ctx, `globalThis.__c=[]; globalThis.__t=[]; toast=(m)=>__t.push(m); ensurePriv=async()=>({grace:'g1'});
    globalThis.__ok=true; confirmSheet=async(o)=>{ if(!__ok) return false; await o.run(); return true; };
    T.api=async(m,p,o)=>{ __c.push([m,p,o&&o.body,o&&o.grace]); return {ok:true}; };`);
  const box = stubEl("div"); ctx.__b = box;
  ctx.__st = ST;
  ev(ctx, "renderStoreBox(globalThis.__b, globalThis.__st)");
  assert.ok(box.text.includes("team.db") && box.text.includes("3.45.1") && box.text.includes("state-20261105.db"));
  assert.ok(box.text.includes("두 값을 모두 보관"), "애매한 거울 알림 문구");
  const btn = box.children.flatMap(function deep(n) { return [n, ...((n && n.children) || []).flatMap(deep)]; })
    .find((k) => k.textContent === "옛 파일 값으로 되돌리기");
  assert.ok(btn, "되돌리기 단추");
  ev(ctx, "__ok=false");
  await btn.onclick();
  assert.equal(ev(ctx, "__c").length, 0, "확인 창에서 취소하면 보내지 않는다");
  ev(ctx, "__ok=true");
  await btn.onclick();
  const last = ev(ctx, "__c").at(-1);
  assert.equal(last[1], "/api/owner/store/restore-kept");
  assert.equal(JSON.stringify(last[2]), JSON.stringify({ kept: "labels.json.ambiguous.20261105-101010" }));
  assert.equal(last[3], "g1");
});

test("저장소 칸: 알림이 없으면 조용히 상태만", async () => {
  const ctx = await load();
  const box = stubEl("div"); ctx.__b = box;
  ctx.__st = { sqlite: "3.45.1", db: "team.db", schema: 6, last_backup: null, notices: [], kept: [] };
  ev(ctx, "renderStoreBox(globalThis.__b, globalThis.__st)");
  assert.ok(box.text.includes("아직 없음"));
  assert.ok(!box.text.includes("되돌리기"));
});
