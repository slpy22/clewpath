# 맥 측 개발 규칙 — 맥에서 직접 고치고 시험한다 (2026-10-22~)

> 맥 측 Claude 가 지킬 규칙이다. 3회차부터는 「보고만」이 아니라, 맥에서 재현 → 원인 → 수정 → 시험까지 하고 브랜치로 올린다.
> 그때그때의 일감은 `docs/mac-host-tasks.md` 맨 위에 있다. 저장소 `CLAUDE.md`(불가침 원칙)는 그대로 지킨다.

## 1. 나눠 맡기

| 맥 측 | Windows 측(주 개발) |
|---|---|
| 맥에서만 재현·확인되는 것: launchd, BSD 도구 차이(`ps`·`stat`·`sed`), psutil·/proc 없는 판정, 경로 대소문자, 맥에서 실패하는 시험, 실제 설치·로그인 항목·권한 창 | Windows 시험(1,000여 건)·리눅스 컨테이너 시험·posix e2e 를 돌리고 **main 에 합친다** |
| 원인 조사를 맥에서 끝까지 한다. 짐작으로 고치지 않는다 | 출시(`ops/release.ps1`)·서명·배포·버전 올리기 |

## 2. 브랜치

- 일감마다 최신 main 에서 `mac/<주제>` 브랜치를 만든다(예: `mac/conf-vanish`). 한 브랜치에 한 주제.
- **main 에 올리지 않는다. 합치지 않는다. force-push 하지 않는다.** (옛 `macos` 브랜치는 2026-10-27 부터 쓰지 않는다 — 없앨 예정)
- 올리기 전에 main 이 앞서 갔으면 자기 브랜치만 `git rebase origin/main` 으로 따라간다.
- 커밋은 작게, 메시지는 한국어로 기존 커밋과 같은 꼴(`fix(맥): …`, `test: …`, `docs: …`). Claude Code 가 붙이는 Co-Authored-By 줄은 그대로 둔다.

## 3. 고쳐도 되는 것

- **바로 고친다:** 시험(`tests/`), 맥 전용 분기(`OS=Darwin`·`sys.platform == "darwin"`), 맥 문서(`docs/mac-*`), 설치·런처 스크립트의 맥 부분.
- **고쳐도 되지만 보고서에 「공통 코드 변경」이라고 적는다:** 모든 OS 가 같이 쓰는 코드(`session_manager/*.py`, `static/`, `pwa/`, `install.sh`·`uninstall.sh`·런처 템플릿의 공통 부분). Windows 측이 Windows·리눅스 시험을 돌린 뒤에 합친다.
- **먼저 사장님께 묻는다:**
  - `~/.claude` 아래 claude 파일을 바꾸는 것(저장소 `CLAUDE.md` 불가침 원칙)
  - 버그 수정이 아닌 새 동작·기능
  - 인증·토큰·권한 같은 보안 경계
  - 설치가 사용자 시스템에 남기는 것(로그인 항목·권한 창·launchd 설정)의 변화
- **하지 않는다:** 출시·서명(`ops/release.ps1`, `ops/sign_release.py`, `ops/release-keys/`), 버전 번호, 운영 서버(clewpath.pyongso.com)와 컨트롤플레인·릴레이(이 저장소 밖).

## 4. 시험

1. 고칠 곳의 시험을 먼저 쓰고, **고치기 전 코드에서 실패하는지** 확인한 뒤 고친다.
2. 고치는 동안에는 관련 파일만 돌린다: `uv run --python 3.11 --extra dev python -m pytest -q -p no:cacheprovider tests/test_xxx.py`
3. **올리기 전에 맥 전체 시험 한 번** — 실패 0 이어야 올린다(약 12분):
   ```bash
   uv run --python 3.11 --extra dev python -m pytest -q -p no:cacheprovider -rfEs > /tmp/clewpath-pytest.log 2>&1; echo "종료코드 $?"
   tail -3 /tmp/clewpath-pytest.log; grep -E "^(FAILED|ERROR) " /tmp/clewpath-pytest.log
   ```
   - Windows 전용 시험(pwsh·`E:\` 경로)은 skip 으로 나온다. 정상이다.
   - 「시험이 진짜 데이터 폴더의 파일을 지웠습니다」 오류가 나오면 그 시험이 범인이다(conftest 가드). 고칠 일감으로 다룬다.
4. 시험이 진짜 claude 를 띄우거나 `~/.claude` 에 세션을 남기면 안 된다. 남은 것을 보면 지우지 말고 보고한다(사장님이 지운다).

## 5. 실제 설치(launchd)를 만질 때

- **맥 측 Claude 는 터미널 앱에서 돌린다.** ClewPath 웹 터미널 안에서 돌리면 제거·브로커 교체 때 자기 자신이 끝난다(3b-2 관찰 1).
- 진짜 설치본에서 확인해야 하면 `docs/mac-host-tasks.md` 2회차 R3 방법(저장소에서 zip → 제거 → 설치)으로 바꾼다. 내리는 동안 폰 연결이 잠깐 끊긴다 — 끝나면 설치된 상태로 돌려 둔다.
- `sudo` 를 쓰지 않는다. 샌드박스를 임의로 끄지 않는다. 막히면 사장님께 터미널 앱에서 직접 붙여 넣어 달라고 부탁한다.
- `sfltool dumpbtm` 은 쓰지 않는다 — 관리자 암호 창이 뜬다(3b-2 관찰 7). 로그인 항목 이름은 사장님께 화면으로 확인을 부탁한다.

## 6. 비밀

저장소(github.com/slpy22/clewpath)는 공개다. 토큰·자격증명·`clewpath.toml`·`cp_credentials.json`·`host.log` 원문·`~/.claude` 아래 파일 내용을 커밋하지 않는다. 보고서에는 필요한 줄만 옮기고 32자 이상 영숫자 값은 `<가림>` 으로 바꾼다.

## 7. 보고

1. 같은 브랜치에 `docs/mac-results/<주제>-YYYYMMDD.md` 하나:
   - 첫 줄 결론(고침/못 고침/사장님 결정 필요)
   - 원인(무엇이 왜 틀렸나 — 재현 방법 포함)
   - 고친 것(파일과 한 줄 요약), **공통 코드 변경 있음/없음**
   - 시험: 새 시험 이름, 고치기 전 실패 확인, 맥 전체 결과 줄(`N passed, M skipped`)
   - 짐작인 것과 남은 것
2. `git push -u origin mac/<주제>`
3. 사장님께 한 줄: "맥에서 <주제> 고쳐 mac/<주제> 브랜치에 올렸습니다 — 맥 시험 N 통과, 공통 코드 변경 있음/없음"

Windows 측은 브랜치를 받아 검토하고 Windows·리눅스·e2e 를 돌려 main 에 합친다. 합치지 못하면 그 이유를 같은 브랜치 보고서 아래 「Windows 측 검토」에 적어 올린다 — 맥 측은 `git fetch` 로 읽고 이어서 고친다.

## 8. 말

터미널 출력(중간 안내·명령 설명)과 보고는 한국어로 한다. 사장님이 진행을 직접 보신다.
