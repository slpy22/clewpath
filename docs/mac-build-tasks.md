# 맥 측 작업 지시 — iOS 빌드·시뮬레이터·TestFlight (2026-08-12)

> 이 문서는 Windows 측(주 개발)에서 맥 측 Claude 에게 전달하는 작업 지시서다.
> 위에서부터 순서대로 실행하면 된다. 완료 후 결과를 사용자에게 보고할 것.

## ★★★★★ 업데이트 5 (다음 배포 = 1.6(6) — 앱 안 QR 스캔 입구, 2026-09-29)

사장님 실기: 폰 카메라 앱으로 PC 의 QR 을 찍으면 Safari(웹 릴레이 앱)가 열려 **네이티브 앱에는 PC 를
추가할 방법이 없었다**. 앱의 네이티브 스캐너는 첫 실행 오버레이에만 있었다 → 이번 커밋으로
**페어링 화면(`📷 QR 스캔으로 연결`)과 ⚙ 설정 → PC 전환·관리(`📷 QR 스캔으로 PC 추가`)** 에도 입구를 냈다
(브리지 `pairFromUrl` 공개, 웹 화면은 무변경). pbxproj 는 **1.6 / 6** 으로 고정 커밋됨 — Xcode 에서 버전을 손대지 말 것.

```bash
git checkout -- ios/App/App.xcodeproj/project.pbxproj   # 맥 로컬의 버전 수정 폐기
git pull                                                 # feat/app-qr-entry 병합 커밋 이후
cd app && npm install && npm run bundle && npx cap sync ios
grep -n CapacitorBarcodeScanner ios/App/Podfile           # ★ 반드시 1줄 이상 나와야 함(없으면 스캔 버튼이 무반응)
```

`Podfile` 에 `pod 'CapacitorBarcodeScanner'` 가 생겼는지 확인하고(저장소의 Podfile 은 sync 이전 상태라
없다 — sync 가 넣는다), 생긴 Podfile 은 **커밋해서 push** 해 달라. `Info.plist` 의 `NSCameraUsageDescription` 은 이미 있다.

시뮬레이터 확인(카메라가 없어 스캔 자체는 실기기에서):
- [ ] 페어링 화면(연결 안 된 상태 또는 설정 → 연결 해제 뒤)에 **📷 QR 스캔으로 연결** 버튼이 링크 입력창 위에 있다
- [ ] ⚙ 설정 → PC 전환·관리 모달 하단에 **📷 QR 스캔으로 PC 추가** 버튼 + "…QR 을 비추세요" 문구
- [ ] 버튼을 누르면 스캐너 화면이 뜨려다 카메라 없음으로 닫힘(오류 없이 조용히) — 앱이 죽지 않아야 함

실기기(TestFlight 1.6(6) 설치 후, 사장님): PC 로컬 웹 → 📱 → ＋새 기기 추가 QR → 앱의 📷 버튼으로 스캔 →
리로드 뒤 세션 목록 + 설정의 PC 목록에 추가돼 있어야 한다(기존 PC 유지).

**같이 태우는 것 — Universal Links(카메라 앱으로 QR 을 찍어도 앱이 열리게)**: 저장소에
`ios/App/App/App.entitlements`(`applinks:clewpath.pyongso.com`) + pbxproj `CODE_SIGN_ENTITLEMENTS` 를 커밋해 뒀다.
- [ ] Xcode → App 타깃 → Signing & Capabilities 에 **Associated Domains: applinks:clewpath.pyongso.com** 이 보이는지
      (안 보이면 + Capability 로 추가 — entitlements 파일과 같은 값)
- [ ] 같은 화면의 **Team** 옆 10자리 **Team ID 를 사용자에게 보고**(예: `Team: 홍길동 (AB12CD34EF)`) — 서버 AASA 파일에 넣어야
      카메라 스캔이 앱으로 연결된다. Team ID 가 서버에 들어가기 전엔 이 기능은 조용히 비활성(Safari 폴백)이고 나머지는 정상.
- Archive 시 "Provisioning profile doesn't include the associated-domains entitlement" 가 나오면 Xcode 의
  자동 서명이 프로필을 재생성하도록 Signing 의 Team 을 한 번 껐다 켠다.

통과 시 Archive → Upload **1.6(6)**.

## ★★★★ 업데이트 4 (다음 배포 = 1.5(5), 버전은 저장소에 고정됨)

다음 TestFlight 배포는 **1.5(5)** 로 확정(사장님 결정). pbxproj 에
MARKETING_VERSION=1.5 / CURRENT_PROJECT_VERSION=5 를 커밋해 뒀으니
**Xcode 에서 버전을 손대지 말고** 아래만 수행:

```bash
git checkout -- ios/App/App.xcodeproj/project.pbxproj   # 맥 로컬의 버전 수정 폐기
git pull                                                 # bd0df30+ (PgUp/PgDn 수정 포함)
cd app && npm run bundle && npx cap sync ios
```

포함된 웹 수정: TUI 스와이프 = PageUp/PageDown 변환(화살표 오작동 수정, iPhone 실기기
발견 건) + 보조키바 PgUp/PgDn 버튼. 시뮬레이터 확인 후 Archive → Upload (1.5(5)).
pull 충돌 시: 맥 로컬 변경을 버리는 방향으로(모든 확정 수정은 원격 main 에 있다).

## ★★★ 업데이트 3 (CORS 서버 반영 완료 + doPair fragment 수정 — 2026-08-13)

맥 측이 확정한 두 건 모두 처리됐다:
1. **CP CORS**: 서버(control_plane)에 CORSMiddleware 추가 + **운영 반영 완료**
   (util-session-cp 재시작, 프리플라이트 200 + `ACAO: capacitor://localhost` 실측 확인).
   → 앱의 JWT 교환이 이제 통과한다. 서버 대기 없이 바로 재시도 가능.
2. **doPair fragment 파싱**: 커밋 023e5c7 — fragment(#room=) 우선 파싱 + E2EE 키(rk)
   저장 누락도 함께 수정(수동 페어링 시 평문 접속 되던 문제).

맥 작업: `git pull`(ad0fc7b - 탭바 뷰포트 보정 포함) → `cd app && npm run bundle && npx cap sync ios`
→ 시뮬레이터에서 페어링→세션 목록 진입(4401 없이) 확인
→ **빌드 번호 +1 (1.3(4)) → Archive → TestFlight 업로드**.

## ★★ 업데이트 2 (cpBase 4401 버그 — 맥 측 발견, 6601198 반영됨)

맥 측이 확정한 cpBase() origin 유도 버그(앱에서 JWT 미발급 → 릴레이 4401)는
**커밋 6601198 로 반영됐다** (wsBaseOverride 에서 CP 유도 + 비/relay 배포 형태 보강).
로컬에 미커밋 수정본이 있으면 폐기 후 pull:

```bash
git checkout -- pwa/index.html && git pull    # 6601198 수신
cd app && npm run bundle && npx cap sync ios
```

시뮬레이터에서 페어링 → 세션 목록 접속(4401 없이)까지 확인 후,
**빌드 번호 올려서(1.3(4)) Archive → TestFlight 재업로드**.

## ★ 업데이트 (registerPlugin 크래시 — 맥 측 발견 반영됨)

맥 측이 찾은 `cap.registerPlugin is not a function` 크래시는 정확한 진단이었고,
동일 수정이 **커밋 6fae801 로 본 저장소에 반영됐다** (Plugins 프록시 폴백 + 주석).
맥 로컬에 커밋 전 수정본이 남아 있다면 충돌 방지를 위해 버리고 pull 할 것:

```bash
git checkout -- pwa/native-bridge.js   # 로컬 미커밋 수정 폐기(내용 동일)
git pull                                # 6fae801 수신
```

이후 아래 ①(번들·동기화)부터 재실행 → ② 체크리스트 검증 → 통과 시 ③ TestFlight.

## 0. 왜 시뮬레이터에 과거 버전이 뜨는가 (필독)

`app/www` 와 `ios/App/App/public` 은 **생성물**이다. `git pull` 은 원본(`pwa/`)만
갱신하므로, 아래 ①번의 번들·동기화를 돌리지 않으면 Xcode 는 계속 예전 스냅샷을
빌드한다. **웹 코어(pwa/)가 바뀐 커밋을 받을 때마다 ①은 필수다.**

## 1. 최신화 + 번들 (필수, 순서대로)

```bash
cd <repo>/clewpath          # 저장소 루트
git pull                     # 최신 main (9ae4f27 이후)
cd app
npm install                  # ★ 이번에 네이티브 플러그인 추가됨 - 반드시 실행
npm run bundle               # pwa/ -> www 스냅샷 (구버전 문제의 해결 지점)
npx cap sync ios             # www -> ios 반영 + pod install (플러그인 등록)
```

`npx cap sync ios` 출력에 `@capacitor/barcode-scanner@1.0.4` 가 보여야 정상.
pod 관련 오류가 나면: `cd ios/App && pod install --repo-update` 후 재시도.

## 2. 시뮬레이터 확인 (Xcode)

Xcode 에서 `ios/App/App.xcworkspace` 열기 (**.xcodeproj 아님 — pods 포함 워크스페이스**).
이미 열려 있었다면 **Product → Clean Build Folder (⇧⌘K)** 한 번 실행(캐시 제거).
기기 = iPhone 시뮬레이터 선택 → ▶ Run.

최신판이 맞는지 판별 기준(전부 이번 업데이트에서 바뀐 것):

- [ ] 첫 페어링 화면에 **3단계 안내 문구**와 **"📷 QR 스캔으로 연결" 버튼**이 있다
      (구버전은 입력창+연결 버튼뿐)
- [ ] 페어링 링크(https) 붙여넣기 → "연결 중…" → 세션 목록으로 전환된다
      (구버전은 눌러도 무반응 — 이번에 고친 버그)
- [ ] 세션 화면 제목 옆 PC 선택 드롭다운에 **🖥 localhost 항목이 없다**
- [ ] 하단 탭 4개(세션/터미널/알림/설정) 표시
- [ ] 잘못된 링크를 넣으면 사유별 오류 문구가 나온다
      (예: PC 로컬 주소(127.0.0.1)를 넣으면 "외부 접속(https) 링크를 붙여넣어 주세요")

시뮬레이터 한계(정상 동작이니 버그로 보고하지 말 것):
- QR 스캔 버튼은 **시뮬레이터에 카메라가 없어 동작 안 함** — 실기기 전용
- 터치 관성 스크롤·핀치·한글 IME 판정은 실기기에서만 유효

## 3. TestFlight 업로드 (시뮬레이터 확인 통과 후)

1. Xcode: App 타깃 → General → **Build 번호 +1** (같은 번호는 업로드 거부됨)
2. 기기 = **Any iOS Device (arm64)** 로 변경
3. Product → Archive → Organizer → Distribute App → App Store Connect → Upload
4. App Store Connect 의 TestFlight 처리 완료 후, 내부 그룹에 빌드가 자동/수동 배정되는지 확인

## 4. 이번 업데이트 변경 내역 (c21b97b → 9ae4f27)

| 커밋 | 내용 |
|---|---|
| 444b8ac | M2 스파이크 — 모바일 터미널 UX: 한손가락 스크롤(scrollLines+관성), TUI(alt버퍼)=화살표 변환, 두손가락 핀치(fontSize 8~22 기억), "↓ 최신" 버튼, 보조키바(Esc/Tab/Ctrl토글/화살표/1·2·3/Enter), WakeLock |
| e039c23 | TestFlight 가이드 + Info.plist 수출규정 exempt(`ITSAppUsesNonExemptEncryption=false`) |
| 16e3bff | 최소 지원 버전 iOS 13 → **15.0** (App Store 2027 요건. pbxproj 4곳+Podfile — pod install 재실행 필요 사유) |
| 0d698e5 | **페어링 버그 수정**: location.href+reload 경합으로 fragment 유실 → 해시 갱신 방식. http 링크 거부(iOS 는 평문 ws:// 차단), 오버레이 3단계 안내+오류 문구 |
| 7c21f8a | **QR 스캔 페어링**(@capacitor/barcode-scanner 네이티브, `npm install` 필요) + Info.plist `NSCameraUsageDescription` + **localhost 항목 숨김**(앱/모바일 브라우저) |
| 9ae4f27 | (Android 전용 빌드 설정 — 맥 작업과 무관) |

## 5. 문제 발생 시

- 서명 오류("No profiles"): Signing & Capabilities 에서 Team 재선택
- 빌드는 되는데 여전히 구버전: ①을 다시 돌렸는지, Clean Build Folder 했는지 확인.
  그래도면 `ls -la ios/App/App/public/native-bridge.js` 수정 시각으로 번들 신선도 확인
- 해결 안 되는 오류는 오류 전문을 사용자에게 보고
