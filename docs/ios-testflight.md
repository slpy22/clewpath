# iOS TestFlight 배포 가이드 (원격 맥 + 수중의 아이폰)

맥과 아이폰이 물리적으로 떨어져 있을 때의 경로: 맥에서 **Archive → App Store Connect
업로드 → TestFlight 내부 테스트** → 아이폰은 TestFlight 앱으로 설치(USB 불필요).
내부 테스트는 **심사 없이** 바로 배포된다(외부 테스터부터 베타 심사 대상).

## 0. 1회 준비 (맥)

```bash
# Xcode 는 App Store 에서 설치돼 있어야 함. 처음이면 라이선스 동의:
sudo xcodebuild -license accept
# Homebrew 없으면: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install node cocoapods
```

Xcode 메뉴 → Settings → Accounts → **Apple Developer 계정 로그인**.

## 1. 1회 준비 (App Store Connect 웹)

https://appstoreconnect.apple.com → 나의 앱 → **＋ 신규 앱**:

| 항목 | 값 |
|---|---|
| 플랫폼 | iOS |
| 이름 | ClewPath |
| 기본 언어 | 한국어 |
| 번들 ID | `com.pyongso.clewpath` (목록에 없으면 developer.apple.com → Identifiers 에서 App ID 먼저 등록 — Xcode 자동 서명을 한 번 돌리면 자동 생성되기도 함) |
| SKU | clewpath-ios (임의 문자열) |

## 2. 빌드·업로드 (맥, 반복 실행 단위)

```bash
git clone https://github.com/slpy22/clewpath.git   # 이후부터는 git pull
cd clewpath/app
npm install
npm run bundle          # pwa/ → www 스냅샷
npx cap sync ios        # 네이티브 반영 + pod install
npx cap open ios        # Xcode 열림
```

Xcode 에서:
1. 좌측 **App** 프로젝트 → App 타깃 → **Signing & Capabilities** → Automatically manage signing 체크 + **Team** 선택 (1회)
2. 상단 실행 대상에서 **Any iOS Device (arm64)** 선택
3. 메뉴 **Product → Archive** (수 분 소요)
4. 완료되면 Organizer 창 → **Distribute App → App Store Connect → Upload** → 기본값으로 Next 연타 → Upload

## 3. TestFlight 배포 (App Store Connect 웹)

1. 나의 앱 → ClewPath → **TestFlight** 탭 — 업로드된 빌드가 "처리 중"으로 뜬다(수 분~30분)
2. 처리 완료 후 **내부 테스트** → ＋ 그룹 생성 → 테스터에 본인 Apple ID 추가 → 빌드 선택
3. (암호화 질문이 뜨면) "표준 암호화만 사용" — Info.plist 에 선언돼 있어 보통 안 뜬다

## 4. 아이폰에서

1. App Store 에서 **TestFlight** 앱 설치
2. 초대 알림(메일 또는 TestFlight 앱 내 표시) 수락 → **설치**
3. 앱 실행 → PC ClewPath 의 📱 새 기기 추가 링크 붙여넣기 → 접속

이후 코드가 바뀌면 **2번만 반복**(pull → bundle → sync → Archive → Upload)하면
TestFlight 에 새 빌드가 자동으로 올라가고 아이폰에 업데이트 알림이 온다.
빌드 번호는 Xcode 가 자동 증가시키지 않으므로, 재업로드 전 App 타깃 →
General → **Build 숫자를 +1** 해야 한다(같은 번호는 업로드 거부).

## 자주 걸리는 것

- **"No profiles for com.pyongso.clewpath"**: Signing 탭에서 Team 선택이 안 된 상태 — 로그인·팀 선택 후 Try Again
- **CocoaPods 없음/버전 오류**: `sudo gem install cocoapods` 또는 `brew upgrade cocoapods` 후 `npx cap sync ios` 재실행
- **Archive 메뉴 비활성**: 실행 대상이 시뮬레이터로 돼 있음 — Any iOS Device 로 변경
