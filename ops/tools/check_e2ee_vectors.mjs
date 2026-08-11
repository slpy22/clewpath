// E2EE 골든 벡터 교차검증 (JS 측) - PWA 가 쓸 WebCrypto AES-GCM 이
// Python(cryptography)과 같은 바이트를 내는지 확인한다.
//   실행: node ops/tools/check_e2ee_vectors.mjs
// Python <-> JS 바이트 불일치는 초기에 안 잡으면 디버깅에 며칠을 태운다(분석 결론).
import { readFileSync } from "node:fs";
import { webcrypto as crypto } from "node:crypto";

const b64u = (buf) => Buffer.from(buf).toString("base64url");
const unb64u = (s) => Buffer.from(s, "base64url");

const vec = JSON.parse(readFileSync(new URL("../../tests/vectors/e2ee_vectors.json", import.meta.url), "utf8"));
const key = await crypto.subtle.importKey("raw", unb64u(vec.key_b64u), "AES-GCM", false, ["encrypt", "decrypt"]);

let fail = 0;
for (const c of vec.cases) {
  const nonce = unb64u(c.nonce_b64u);
  // PWA 와 동일한 직렬화: JSON.stringify (Python 측은 separators=(",",":") 로 맞춤)
  const pt = new TextEncoder().encode(JSON.stringify(c.frame));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, key, pt));
  const sealed = b64u(Buffer.concat([Buffer.from([1]), nonce, ct]));
  const encOk = sealed === c.sealed;

  // 복호 방향: Python 이 만든 sealed 를 JS 가 연다
  const raw = unb64u(c.sealed);
  const pt2 = new Uint8Array(await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: raw.subarray(1, 13) }, key, raw.subarray(13)));
  const decOk = JSON.stringify(JSON.parse(new TextDecoder().decode(pt2))) === JSON.stringify(c.frame);

  console.log(`${c.name}: encrypt ${encOk ? "OK" : "MISMATCH"} / decrypt ${decOk ? "OK" : "MISMATCH"}`);
  if (!encOk || !decOk) fail++;
}
// 변조 거부: 마지막 바이트 뒤집기 -> 반드시 실패해야
try {
  const raw = unb64u(vec.cases[0].sealed);
  raw[raw.length - 1] ^= 0xff;
  await crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.subarray(1, 13) }, key, raw.subarray(13));
  console.log("tamper: NOT REJECTED (문제!)"); fail++;
} catch { console.log("tamper: rejected OK"); }

process.exit(fail ? 1 : 0);
