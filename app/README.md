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
```

Android CLI 빌드(이 PC 실측 기준):

```powershell
cd android
$env:JAVA_HOME="C:\Program Files\Java\jdk-21.0.10"   # Gradle 8.2 는 Java 25 미지원(major 69)
$env:ANDROID_HOME="$env:LOCALAPPDATA\Android\Sdk"    # 사용자 SDK 루트(아래 참고)
.\gradlew.bat assembleDebug
# → app/build/outputs/apk/debug/app-debug.apk
```

이 PC 의 빌드 환경 함정(실측):
- SDK 본체는 `C:\Program Files (x86)\Android\android-sdk`(쓰기 불가) —
  `%LOCALAPPDATA%\Android\Sdk` 에 junction + 실폴더(licenses, build-tools 추가분)로
  사용자 SDK 루트를 구성해 두었다. licenses/새 build-tools 설치가 여기로 들어간다.
- 한글 프로젝트 경로: `gradle.properties` 의 `android.overridePathCheck=true` 로 허용
  (aapt2 문제 재발 시 ASCII junction 경로로 이전).
- iOS: Mac + Xcode 필요(`ios/`). Windows 에서는 스캐폴드만 관리.
- 아이콘/스플래시 재생성: `npx @capacitor/assets generate --iconBackgroundColor '#109098' --splashBackgroundColor '#109098'` (원본은 `assets/`).

## 프로토콜 버전(pv)

앱은 심사 때문에 웹·Host 보다 늦게 갱신된다. `auth` 에서 pv 를 교환해
Host 가 더 새 규약이면 업데이트 안내를 띄운다(`index.html` PV 상수,
`connector.py` PROTOCOL_VERSION). 규약을 깨는 변경 시에만 올린다.
