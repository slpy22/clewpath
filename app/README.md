# ClewPath 모바일 앱 (Capacitor 셸)

기존 `pwa/` 코어를 **로컬 번들**로 감싼 앱이다(원격 URL 로딩 금지 — 스토어 4.2 대응).
웹 코어가 곧 앱 코어이며, 재작성하지 않는다.

## 캡슐화 규칙 (eng-review 확정, 위반 금지)

> **앱 전용 코드는 `pwa/native-bridge.js` 안에만 존재한다.**
> `index.html` 은 `ClewBridge.<fn>()` 호출만 하고, 웹 모드에서는 전부 no-op/null 을
> 돌려받는다. `index.html` 에 `isApp` 류 분기를 직접 쓰지 않는다.

리뷰 체크: `index.html` 에서 `Capacitor`/`isApp` 직접 참조 0건이어야 한다.

## 브리지 정의 5개 (기획서 §12)

| # | 함수 | 상태 |
|---|---|---|
| ① | `registerPush()` | M3 (FCM/APNs) |
| ② | `keyboard.*` | M2 (보조키·키보드 제어) |
| ③ | 딥링크 `clewpath://` + 페어링 링크 | M1 구현 |
| ④ | `voice.*` (startPTT/stopPTT/streamToWhisper/speak/setAudioSession) | 예약(2차) |
| ⑤ | `syncRoomKey(room,key)` | M1=Preferences, M3 에서 iOS App Group 키체인 승격(NSE 복호용) |

추가로 M1 브리지가 하는 일: `MODE='relay'` 강제, 릴레이 WS 주소 오버라이드,
`sm_*` 키 네이티브 미러+퇴거 복원(iOS localStorage 소실 대비), 하단 탭 골격,
페어링 링크 붙여넣기 오버레이(정식 온보딩은 M4).

## 빌드

```
npm install
npm run bundle        # pwa/ → www 스냅샷
npx cap sync          # www → 네이티브 프로젝트 반영
npx cap open android  # Android Studio (SDK 필요)
```

- Android: Android Studio(SDK) 설치 후 `android/` 열어 APK 빌드.
- iOS: Mac + Xcode 필요(`ios/`). Windows 에서는 스캐폴드만 관리.
- 아이콘/스플래시 재생성: `npx @capacitor/assets generate --iconBackgroundColor '#109098' --splashBackgroundColor '#109098'` (원본은 `assets/`).

## 프로토콜 버전(pv)

앱은 심사 때문에 웹·Host 보다 늦게 갱신된다. `auth` 에서 pv 를 교환해
Host 가 더 새 규약이면 업데이트 안내를 띄운다(`index.html` PV 상수,
`connector.py` PROTOCOL_VERSION). 규약을 깨는 변경 시에만 올린다.
