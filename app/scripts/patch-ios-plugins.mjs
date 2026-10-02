// iOS 플러그인 등록 목록(packageClassList) 보정 — `npx cap sync`/`cap copy` 뒤에 자동 실행(package.json 의
// capacitor:sync:after · capacitor:copy:after 훅).
//
// 왜: @capacitor/barcode-scanner 1.0.4(Capacitor 6 용 마지막 버전)는 iOS 플러그인을 옛 방식(Objective-C
// CAP_PLUGIN 매크로)으로 등록한다. Capacitor 6 CLI 는 CAPBridgedPlugin 을 선언한 Swift 클래스만
// packageClassList 에 넣고, 앱은 그 목록에 있는 플러그인만 등록한다 → 스캐너가 빠져 'QR 스캔' 버튼이
// 아무 반응 없이 실패했다(2026-10-02 사장님 아이폰 실기). 클래스 이름(@objc(CapacitorBarcodeScannerPlugin))을
// 직접 더해 주면 NSClassFromString 으로 찾아 등록된다(CAP_PLUGIN 매크로가 CAPBridgedPlugin 적합성을 붙인다).
// Capacitor 7 + barcode-scanner 2.x 로 올리면 이 스크립트는 필요 없다.
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const cfgPath = join(here, '..', 'ios', 'App', 'App', 'capacitor.config.json');
const REQUIRED = ['CapacitorBarcodeScannerPlugin'];

export function patch(json) {
  const cfg = JSON.parse(json);
  const list = Array.isArray(cfg.packageClassList) ? cfg.packageClassList : [];
  let added = 0;
  for (const name of REQUIRED) if (!list.includes(name)) { list.push(name); added++; }
  cfg.packageClassList = list;
  return { text: JSON.stringify(cfg, null, '\t') + '\n', added };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  if (!existsSync(cfgPath)) {
    console.log('[patch-ios-plugins] ios 프로젝트 없음 — 건너뜀');
  } else {
    const { text, added } = patch(readFileSync(cfgPath, 'utf8'));
    if (added) writeFileSync(cfgPath, text);
    console.log(`[patch-ios-plugins] packageClassList ${added ? '보정(+' + added + ')' : '이미 정상'}: ${REQUIRED.join(', ')}`);
  }
}
