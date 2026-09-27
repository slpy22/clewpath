# Crosscheck Session
Started: 2026-08-11 15:00

---

## [Context Summary]
(없음)

---

## Q1
**Question**: ClewPath 모바일 앱(iOS+Android)을 어떤 언어/프레임워크로 개발할지 결정. 후보: Capacitor 래핑 / Expo·RN / Flutter / KMP·네이티브. 기준: 1인+AI 생산성, 기존 바닐라JS PWA(xterm·WebCrypto E2EE) 재사용, 푸시 품질, 심사 리스크, 장기 확장.

**Gemini** (`gemini-3.5-flash`): Capacitor 강력 추천 — xterm+WebCrypto 가 핵심이라 웹뷰 유지가 정답, 1인 기준 "일주일 공수 vs 수개월". 7일 플랜(푸시 브리지→safe-area→@capacitor/assets→심사용 데모 모드), 네이티브 QR 스캐너 옵션.
**Codex** (config 참조): Capacitor 추천 — "로컬 번들 웹앱 + 얇은 네이티브 브리지". RN 가도 xterm 은 결국 WebView 라 재작성 이득 없음. 6단계 경로: 셸 생성→native-bridge.js 분기→푸시 토큰 경로 교체(웹 VAPID 유지+네이티브 APNs/FCM)→알림 액션 플러그인(Approve 는 보안상 foreground 확인 후, 추후 FaceID)→심사 대비(로컬 번들+네이티브 기능+Review Notes)→데스크톱은 Tauri 재사용.
**Copilot**: 오류(프롬프트 전달 실패 2회 — 콘솔 인코딩)
**Claude** (`claude-opus-4-8`): Capacitor — 기존 자산 보존이 1인 체제의 유일한 합리적 선택. 비용은 FCM 발송 경로 추가(Host push.py 확장)와 4.2 심사 리스크(네이티브 푸시+실기능으로 완화).

**Consensus**: 3/3 Capacitor 만장일치. Expo/RN 은 xterm·E2EE 재구현 비용으로 기각(Happier 는 RN 출발이라 합리적이었을 뿐). 핵심 실행 합의: ①로컬 번들(원격 URL 금지—심사) ②native-bridge 분기(웹=VAPID 유지, 앱=FCM/APNs) ③알림 액션은 Swift/Kotlin 소플러그인, Approve 는 초기엔 foreground 확인 ④심사 대비 데모 모드 ⑤데스크톱은 추후 같은 코어를 Tauri 로.

## Q2
**Question**: 터미널 네이티브급 UX(스크롤·보조키바·키보드) + 향후 음성 비서 진화(상시청취·한국어 STT/TTS·백그라운드 오디오)를 감안해도 Capacitor 가 최선인지 재검증.

**Gemini** (`gemini-3.5-flash`): Capacitor 유지. 3단계 하이브리드 — P1 Capacitor(터미널 튜닝+웹 PTT+자체 Whisper 서버 STT+웹/플러그인 TTS), P2 백그라운드 오디오만 Swift/Kotlin 커스텀 플러그인(AVAudioSession), P3 전환 재평가(완전 오프라인 온디바이스 모델 또는 wake-word 필요 시에만 RN). "그 전까진 99% 충족".
**Codex** (`gpt-5.5`): Capacitor 유지 — 단 "그냥 감싸기"가 아니라 터미널 UX Bridge + Voice Native Plugin 인터페이스(startPTT/stopPTT/streamToWhisper/speak/setAudioSession)를 처음부터 분리. 기술 디테일: 스크롤=웹뷰 스크롤 금지, 터치 제스처→xterm scrollLines 매핑 / 키=IME 우회하고 이스케이프 시퀀스 직접 전송 / 핀치=fontSize+fit 재계산 / 상시청취는 Capacitor 문제가 아니라 OS 정책 문제(RN 가도 동일). 전환 기준: ①음성이 주 UI ②wake-word·VAD·오디오라우팅이 핵심품질 ③온디바이스 모델 ④커스텀 플러그인 3~5개+ 중 2개 이상. "리스크는 Capacitor 선택이 아니라 네이티브가 필요한 부분까지 웹으로 버티는 것".
**Copilot** (`gpt-5.3-codex`, 영문 프롬프트로 성공): Capacitor 권장. 경고: 고주기 대량 출력 시 프레임 드롭 가능(→WebGL 렌더러/성능 데이터 수집), 백그라운드 상시청취는 네이티브 모듈 필수. "Capacitor 시작, 성능 데이터 보고 필요한 부분만 네이티브화".
**Claude** (`claude-opus-4-8`): Capacitor 유지 — 웹뷰 불편의 본질은 '남의 브라우저'였고 Capacitor 는 우리가 통제하는 웹뷰. 음성 1차는 보유한 Whisper 서버 활용이 최단.

**Consensus**: 4/4 Capacitor 유지(2라운드 연속). 하이브리드 전략 만장일치: 지금 Capacitor 출시 → 백그라운드 오디오만 커스텀 네이티브 플러그인 → wake-word/온디바이스 시점에 재평가. 음성 플러그인 인터페이스를 처음부터 정의(Codex)하고 성능 데이터 수집(Copilot).

## Q3
**Question**: ClewPath 다음 사이클 우선순위 — (A) Host 자기 회복 / (B) 세션 목록 API 성능(거대 세션 재파싱) / (C) 워커 분배 Phase 2 / (D) 화면 소유권 인계 UX. 첫 사이클 하나 추천.

**Gemini** (`gemini-3.5-flash`): B → A → D → C. B 는 FastAPI 이벤트 루프를 6초 블로킹하는 아키텍처 병목이라 SaaS 에서 "무거운 사용자 1명이 전체를 멈추는" 문제 → 최우선. A 는 가성비 최고지만 원인(OOM/segfault/커널 종료)을 덮을 위험 → 복구 알림에 종료 직전 로그 5~10줄을 얹는 '진단형 알림' 필요. B 의 리스크: 파일 축소/덮어쓰기 시 오프셋 정합성 → st_size 검증·전체 재파싱 폴백 필수. C 는 YAGNI.
**Codex** (`gpt-6-astra`): A → B → D → C. Host 장애는 관제·알림·복구 수단을 동시에 잃음. "B 는 매일, A 는 한 번" 반론에도 A 는 사용자가 알아채기 전엔 지속되는 구조라 우선. A 범위: health 짧은 타임아웃+기동 잠금(중복 방지), health 실패만으로 기존 프로세스를 죽이지 않기, 5분 반복(복구 목표 5분+기동), 실행 ID·정상 종료 표식 보존("비정상 종료 의심"으로 표현), 복구 알림과 유지보수 중지 구분. 한계: Host 가 못 뜨거나 PC 가 꺼지면 알림 불가 → SaaS 에선 릴레이 heartbeat 감지 필요. B: mtime 캐시는 활성 세션에서 매번 무효화 → 요약 메타 증분 갱신, health 경로가 스캔에 의존하면 B 지연이 A 오탐 유발. D: 이전 연결 입력 권한 회수 확인. C: 2FA 외 중복 기동·권한 범위·비용 제한.
**Copilot** (`-`): 오류 — 2회 모두 답변 없음(리포 작업으로 오해/빈 프롬프트 인식).
**Claude** (`claude-fable-5-1`): A → B → D → C. A 는 반나절, 검증 가능(강제 종료→5분 내 복구), 다른 모든 기능의 전제. B 는 바로 다음 사이클. Codex 의 범위 규정 + Gemini 의 진단형 알림 채택.
**Consensus**: A 와 B 가 최상위 2개(합의), 순서만 갈림(Gemini B 우선 / Codex·Claude A 우선). 결론: A 먼저(작고 검증 가능, 장애 시 전체 무력화) → B 연속 사이클. C 는 실사용 피드백 후, D 는 그 뒤.
