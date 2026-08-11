// pwa/ → app/www 번들 복사. 로컬 번들 원칙(원격 URL 로딩 금지 — 스토어 4.2 대응)이라
// 앱은 항상 이 스크립트가 만든 스냅샷을 담는다. 웹 코어가 곧 앱 코어(재작성 없음).
// 이 환경(Node 24.13/Windows)에서 fs 의 rmSync·cpSync({recursive}) 가 0xC0000409 로
// 크래시한다(실측). readdir 기반 수동 재귀(삭제·복사)로만 구현한다.
import { existsSync, mkdirSync, readdirSync, copyFileSync, statSync, unlinkSync, rmdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const pwa = join(here, '..', '..', 'pwa');
const www = join(here, '..', 'www');

function copyAny(src, dst) {
  if (statSync(src).isDirectory()) {
    mkdirSync(dst, { recursive: true });
    for (const e of readdirSync(src)) copyAny(join(src, e), join(dst, e));
  } else {
    copyFileSync(src, dst);
  }
}

function rmAny(p) {
  if (!existsSync(p)) return;
  if (statSync(p).isDirectory()) {
    for (const e of readdirSync(p)) rmAny(join(p, e));
    rmdirSync(p);
  } else {
    unlinkSync(p);
  }
}

rmAny(www);
mkdirSync(www, { recursive: true });
// sw.js 는 제외 — 앱은 네이티브 푸시(M3)를 쓰고, 서비스워커 푸시 경로는 웹 전용.
for (const f of ['index.html', 'native-bridge.js', 'app.webmanifest', 'vendor']) {
  copyAny(join(pwa, f), join(www, f));
}
console.log('www bundle ready');
