# ClewPath 개발 원칙

## 불가침 원칙: claude 자체의 동작에 영향을 주지 않는다 (2026-08-13, 사장님 확정)

ClewPath 는 claude 를 **관찰·중계·실행**하는 도구이지, claude 의 상태를 바꾸는
도구가 아니다.

1. **claude 가 사용하는 파일(`~/.claude` 아래 세션 jsonl, 설정, 잡 등록부 등)을
   임의로 수정하지 않는다.** 수정이 꼭 필요한 기능이라면, 반드시 사용자에게
   위험을 먼저 고지하고 **명시 승인을 받은 뒤에만** 수정한다(1회성 확인 포함).
2. **직접 수정이 없어도 claude 의 의도치 않은 동작을 유발할 위험이 있는 기능은
   아예 만들지 않는다.** 개발 중 그런 지점이 포착되면 구현 전에 **사장님과 먼저
   협의**한다.

전례(둘 다 원칙 위반으로 제거, 회귀 가드 테스트 존재):
- '은닉 유지'(피커 미표시 세션 재개 후 ai-title 레코드 자동 제거) → 대체 방식 =
  **재개 전 고지 + 사용자 승인**. 가드: `test_lifecycle.py::test_no_silent_modification_of_claude_files`
- 웹 재개 프롬프트의 `~/.claude/history.jsonl` 자동 append(터미널 ↑/↓ 이력 연동) →
  claude 소유 파일 무단 수정이라 제거. 가드: `test_webapi_history.py::test_no_silent_claude_history_append`

승인된 예외(2026-08-13 사장님 승인 — 모두 사용자의 명시 동작 경유, 삭제는 확인창 필수):
- 세션 삭제 → 확인창 → 휴지통 이동(원위치 복구 가능, 30일 보관)
- 이름 변경 → jsonl 에 custom-title 레코드 append (claude 네이티브 형식 준수)
- 폴더 변경(change_cwd) → jsonl 의 cwd 필드 재작성
- 세션 가져오기 → 새 jsonl 파일 생성

이 목록에 없는 claude 파일 접근을 추가할 때는 위 1·2 를 적용할 것.
