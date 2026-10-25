#!/bin/sh
# ClewPath Host — 원라인 설치 부트스트랩(맥·리눅스). get.ps1 의 짝 — docs/designs/posix-install.md 3.1
#
#   curl -fsSL https://clewpath.pyongso.com/get.sh | sh
#   curl -fsSL https://clewpath.pyongso.com/get.sh | sh -s -- --port 5123             옵션
#   curl -fsSL https://clewpath.pyongso.com/get.sh | CLEWPATH_CLIENT_TOKEN=… sh       토큰은 env 로(인자는 ps 에 보인다)
#
# 하는 일: 최신 릴리스 매니페스트를 읽고 → zip 을 받아 SHA-256·크기를 대조하고 → 항목 이름을 검사해 설치 폴더 옆 임시
#   stage 에 풀고 → stage 의 install.sh(--from-stage)로 넘긴다(exec). 실제 설치는 전부 zip 안의 install.sh 가 한다.
#
# 신뢰 모델(get.ps1 과 같다, 정직하게):
#  - 첫 설치의 신뢰 근거는 TLS 다. 이 스크립트 자체가 서버에서 오므로, 서버가 뚫리면 서명 검증을 여기 넣어 봐야
#    스크립트째 바뀐다. 그래서 매니페스트의 서명은 여기서 검증하지 않는다.
#  - 대신 zip 은 매니페스트의 SHA-256·크기와 대조한다(전송 오류·불일치 차단). 설치가 끝난 뒤의 자동 업데이트부터는
#    코드에 박힌 공개키로 오프라인 서명을 검증한다 — 서버 침해 창은 첫 설치 순간뿐이다.
#
# 옵션: --cp-url URL --dir DIR --port N --relay-url URL --app-url URL --no-autostart --no-provision
#       --allow-http --allow-root                    (시험 전용)
#       --client-token T                             (받기는 하지만 경고하고 env 로 옮긴다)
# env:  CLEWPATH_CLIENT_TOKEN·CLEWPATH_AGENT_TOKEN 토큰 / CLEWPATH_HOME·XDG_DATA_HOME 설치 위치(기본 <홈>/app)
#
# 지키는 것:
#  - 본문 전체가 main() 안에 있고 맨 끝 줄이 main "$@" 다 — curl | sh 가 중간에 끊겨도 반쪽 스크립트가 돌지 않는다.
#    맨 위에는 함수 정의와 값 대입뿐이다(test_posix_scripts 가 묶는다)
#  - https 만(리다이렉트로 http 로 내려가지 않게, TLS 1.2 이상). jq·venv 가 아직 없으므로 매니페스트(압축된 ASCII JSON)는
#    sed·awk 로 읽고 키가 한 번씩인지·값의 모양까지 본다. zip 은 Info-ZIP unzip 으로, [linux] 에 그것이 없을 때만(busybox
#    unzip 은 목록 모드 -Z 가 없다) python3 -I 의 zipfile 로 푼다 — [mac] 은 python3 를 부르지 않는다(CLT 설치 대화상자)
#  - 항목 이름(절대 경로·..·역슬래시·제어문자·점으로 시작하는 최상위·런처 이름·심볼릭 링크·같은 이름)을 풀기 전에 보고,
#    푼 뒤에 디스크에서 다시 본다. 받는 동안 설치 폴더는 건드리지 않는다(옮기기·Host 정지는 install.sh 가)
#  - root 로는 설치하지 않는다. 토큰은 출력하지 않는다. set -x·env 덤프 금지. umask·env 는 그대로 install.sh 로 간다
# 종료코드: 1 실패 / 2 인자 오류. 성공하면 install.sh 로 exec 하므로 그 종료코드가 그대로 사용자에게 간다

set -u

# zipfile 대역 — [linux] 에 Info-ZIP unzip 이 없을 때만. list: 이름을 한 줄에 하나(제어문자 3, 심볼릭 링크 4), extract: 푼다
ZIP_PY='
import stat, sys, zipfile
with zipfile.ZipFile(sys.argv[2]) as z:
    infos = z.infolist()
    if sys.argv[1] == "list":
        for i in infos:
            if any(ord(c) < 32 or ord(c) == 127 for c in i.filename):
                sys.exit(3)
            if stat.S_ISLNK(i.external_attr >> 16):
                sys.exit(4)
        sys.stdout.write("".join(i.filename + "\n" for i in infos))
    else:
        z.extractall(sys.argv[3])
'

# ---------------------------------------------------------------- 출력·검사

say() { printf '%s\n' "$*"; }
ok() { printf '  [OK]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; }
die() { fail "$*"; exit 1; }
usage_die() { fail "$*"; say "         도움말: curl -fsSL https://clewpath.pyongso.com/get.sh | sh -s -- --help"; exit 2; }

usage() {
  say "쓰는 법: curl -fsSL https://clewpath.pyongso.com/get.sh | sh -s -- [옵션]"
  say "  --dir DIR        설치 폴더(기본: 맥 ~/Library/Application Support/ClewPath/app,"
  say "                   리눅스 \${XDG_DATA_HOME:-~/.local/share}/clewpath/app — CLEWPATH_HOME 으로 바꿀 수 있다)"
  say "  --port N         이 PC 의 웹 포트(기본 5100)"
  say "  --cp-url URL     컨트롤플레인(기본 https://clewpath.pyongso.com/cp)"
  say "  --relay-url URL  --app-url URL  --no-autostart  --no-provision   (install.sh 로 넘긴다)"
  say "  토큰: curl -fsSL https://clewpath.pyongso.com/get.sh | CLEWPATH_CLIENT_TOKEN=… sh"
}

exists() { [ -e "$1" ] || [ -L "$1" ]; }

path_ok() {   # 절대 경로 — 제어문자·. ·.. 조각이 없다
  case "$1" in /*) ;; *) return 1 ;; esac
  case "$1" in *[[:cntrl:]]*) return 1 ;; esac
  case "$1/" in */./*|*/../*) return 1 ;; esac
  return 0
}

# 물리 경로 — /bin/pwd -P 는 맥 APFS 에서 디스크의 대소문자를 준다(셸 내장 pwd -P 는 친 글자 그대로). 다섯 스크립트 같은 본문
phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }

trim_slash() {   # 끝의 / 를 걷어 낸 값 → T_OUT("/" 는 그대로)
  T_OUT=$1
  while :; do
    case "$T_OUT" in /|'') return 0 ;; */) T_OUT=${T_OUT%/} ;; *) return 0 ;; esac
  done
}

check_url() {   # $1=값 $2=안전한 스킴(https|wss) $3=인자 이름 — 시험용 http·ws 는 --allow-http 일 때만
  case "$1" in *[[:cntrl:]]*|*[[:space:]]*|*'"'*|*"'"*|*'\'*) usage_die "$3 에 쓸 수 없는 글자가 있습니다" ;; esac
  case "$1" in "$2"://?*) return 0 ;; esac
  if [ "$ALLOW_HTTP" = 1 ]; then
    case "$2" in https) _cu=http ;; *) _cu=ws ;; esac
    case "$1" in "$_cu"://?*) HTTP_USED=1; return 0 ;; esac
  fi
  usage_die "$3 는 $2:// 주소여야 합니다(시험용 http 는 --allow-http): $1"
}

is_clewpath_tree() {   # $1=폴더 — ClewPath 설치본인가(설치 기록, 또는 session_manager + 이름이 clewpath-host 인 pyproject)
  [ -f "$1/.clewpath-install" ] && return 0
  [ -d "$1/session_manager" ] && grep -q '^name[[:space:]]*=[[:space:]]*"clewpath-host"' "$1/pyproject.toml" 2>/dev/null
}

is_loopback() {   # $1=URL — 이 PC(127.0.0.1·localhost·[::1])인가
  _lh=${1#*://}
  _lh=${_lh%%/*}
  case "$_lh" in
    127.0.0.1|127.0.0.1:*|localhost|localhost:*|'[::1]'|'[::1]:'*) return 0 ;;
  esac
  return 1
}

err_line() {   # $1=curl 표준오류 파일 → 첫 줄(200자까지, 'curl: ' 머리 없이)
  awk 'NR == 1 { sub(/^curl: /, ""); print substr($0, 1, 200); exit }' "$1" 2>/dev/null
}

# ---------------------------------------------------------------- 내려받기·무결성

g_curl() {   # $1=URL, 나머지=curl 옵션 — https 만(시험용 --allow-http 면 http 도), 리다이렉트로 내려가지 않게, TLS 1.2+.
  _gu=$1     # 프록시는 사용자 설정을 따르되 이 PC(시험용 CP)로 가는 요청만 건너뛴다(사내 프록시가 127.0.0.1 을 받지 않는다)
  shift
  if [ "$ALLOW_HTTP" = 1 ]; then _gp='=http,https'; else _gp='=https'; fi
  if is_loopback "$_gu"; then set -- --noproxy '*' "$@"; fi
  curl --proto "$_gp" --proto-redir "$_gp" --tlsv1.2 "$@" "$_gu" </dev/null
}

sha256_of() {   # $1=파일 → 소문자 64자(도구가 없거나 실패하면 빈 출력). 표준 입력으로 넘긴다 — 파일 이름이 출력에 안 섞이게
  if command -v sha256sum >/dev/null 2>&1; then
    _sh=$(sha256sum <"$1" 2>/dev/null) || _sh=
  elif command -v shasum >/dev/null 2>&1; then
    _sh=$(shasum -a 256 <"$1" 2>/dev/null) || _sh=
  elif command -v openssl >/dev/null 2>&1; then
    _sh=$(openssl dgst -sha256 -r <"$1" 2>/dev/null) || _sh=
  else
    _sh=
  fi
  _sh=$(printf '%s' "${_sh%% *}" | tr 'A-F' 'a-f')
  case "$_sh" in *[!0-9a-f]*) _sh= ;; esac
  [ "${#_sh}" = 64 ] && printf '%s' "$_sh"
  return 0
}

b64_decode() {   # 표준 입력 → 표준 출력. 도구는 pick_b64 가 고른다(GNU base64 -d · 옛 맥 base64 -D · openssl)
  case "$B64_TOOL" in
    d) base64 -d ;;
    D) base64 -D ;;
    openssl) openssl base64 -d -A ;;
    *) return 1 ;;
  esac
}

pick_b64() {   # 알려진 값을 실제로 풀어 보고 고른다 → B64_TOOL
  for B64_TOOL in d D openssl; do
    [ "$(printf '%s' 'Y2xld3BhdGg=' | b64_decode 2>/dev/null)" = clewpath ] && return 0
  done
  B64_TOOL=
  return 1
}

# ---------------------------------------------------------------- 매니페스트(서명된 JWT — 내용만 읽는다)

jwt_json() {   # $1=JWT 가운데 조각(base64url) → 표준 출력: 푼 내용. 길이가 4로 나눠 1 남으면 base64 가 아니다
  case $(( ${#1} % 4 )) in
    0) JJ_PAD= ;;
    2) JJ_PAD='==' ;;
    3) JJ_PAD='=' ;;
    *) return 1 ;;
  esac
  printf '%s%s' "$1" "$JJ_PAD" | tr '_-' '/+' | b64_decode
}

json_count() {   # $1=키 $2=JSON 파일 → 값 자리(앞이 { 나 ,)의 "키": 가 몇 번. 문자열 안의 \"키\": 는 세지 않는다
  awk -v k="$1" 'BEGIN { n = 0 } { n += gsub("[{,]\"" k "\":", "&") } END { print n }' "$2"
}

json_str() {   # $1=키 $2=JSON 파일 → "키":"값" 의 값. 따옴표·역슬래시가 든 값은 빈 출력(이스케이프를 풀지 않는다)
  sed -n 's/.*[{,]"'"$1"'":"\([^"\\]*\)".*/\1/p' "$2"
}

json_num() {   # $1=키 $2=JSON 파일 → "키":숫자 의 숫자
  sed -n 's/.*[{,]"'"$1"'":\([0-9][0-9]*\)[,}].*/\1/p' "$2"
}

is_ver() {   # X.Y.Z(숫자 셋)
  case "$1" in ''|*[!0-9.]*) return 1 ;; esac
  _va=${1%%.*}
  _vr=${1#*.}
  _vb=${_vr%%.*}
  _vc=${_vr#*.}
  [ "$_vr" != "$1" ] && [ "$_vc" != "$_vr" ] && [ -n "$_va" ] && [ -n "$_vb" ] && [ -n "$_vc" ] || return 1
  case "$_vc" in *.*) return 1 ;; esac
  return 0
}

read_platforms() {   # $1=JSON 파일 → M_PLATS(공백으로 구분). 클레임이 없으면 win(0.28 까지의 릴리스), 모양이 이상하면 빈 값
  case "$(json_count platforms "$1")" in
    0) M_PLATS=win; return 0 ;;
    1) ;;
    *) M_PLATS=; return 0 ;;
  esac
  M_PLATS=
  [ -n "$(sed -n '/[{,]"platforms":\[[^]]*]/p' "$1")" ] || return 0     # 목록이 아니다 — 아무 OS 도 받지 않는다(updater 와 같다)
  RP_REST=$(sed -n 's/.*[{,]"platforms":\[\([^]]*\)].*/\1/p' "$1")
  case "$RP_REST" in ,*|*,|*,,*) return 0 ;; esac
  RP_ALL=
  while [ -n "$RP_REST" ]; do
    RP_ITEM=${RP_REST%%,*}
    case "$RP_REST" in *,*) RP_REST=${RP_REST#*,} ;; *) RP_REST= ;; esac
    case "$RP_ITEM" in '"'?*'"') ;; *) return 0 ;; esac
    RP_NAME=${RP_ITEM#\"}
    RP_NAME=${RP_NAME%\"}
    case "$RP_NAME" in *[!a-z0-9_-]*) return 0 ;; esac
    RP_ALL="$RP_ALL $RP_NAME"
  done
  M_PLATS=${RP_ALL# }
  return 0
}

read_manifest() {   # $1=받은 매니페스트 파일 $2=풀어 쓸 파일 → M_VER M_SHA M_SIZE M_URL M_PLATS. 틀리면 RM_WHY 를 두고 1
  RM_WHY=
  RM_LEN=$(wc -c <"$1" | tr -d ' ')
  case "$RM_LEN" in ''|*[!0-9]*) RM_WHY="읽지 못했습니다"; return 1 ;; esac
  [ "$RM_LEN" -le 65536 ] || { RM_WHY="너무 큽니다"; return 1; }
  RM_TOK=$(cat "$1")
  case "$RM_TOK" in ''|*[!A-Za-z0-9_.-]*) RM_WHY="JWT 모양이 아닙니다"; return 1 ;; esac
  RM_H=${RM_TOK%%.*}                             # 점으로 나뉜 세 조각(머리.내용.서명), 셋 다 비어 있지 않다
  RM_R=${RM_TOK#*.}
  RM_P=${RM_R%%.*}
  RM_S=${RM_R#*.}
  if [ "$RM_R" = "$RM_TOK" ] || [ "$RM_S" = "$RM_R" ] || [ -z "$RM_H" ] || [ -z "$RM_P" ] || [ -z "$RM_S" ]; then
    RM_WHY="JWT 모양이 아닙니다(세 조각)"
    return 1
  fi
  case "$RM_S" in *.*) RM_WHY="JWT 모양이 아닙니다(세 조각)"; return 1 ;; esac
  jwt_json "$RM_P" >"$2" 2>/dev/null || { RM_WHY="내용(base64url)을 풀지 못했습니다"; return 1; }
  [ -s "$2" ] || { RM_WHY="내용이 비었습니다"; return 1; }
  # 한 줄짜리 ASCII 만(서명 도구는 압축된 ASCII JSON 을 쓴다 — 한글 notes 는 \uXXXX). 줄바꿈·제어문자·8비트가 있으면 거부
  RM_BAD=$(LC_ALL=C tr -d ' -~' <"$2" | wc -c | tr -d ' ')
  [ "$RM_BAD" = 0 ] || { RM_WHY="내용이 한 줄 ASCII JSON 이 아닙니다"; return 1; }
  case "$(cat "$2")" in '{'*'}') ;; *) RM_WHY="내용이 JSON 객체가 아닙니다"; return 1 ;; esac
  for RM_K in ver sha256 size url; do
    [ "$(json_count "$RM_K" "$2")" = 1 ] || { RM_WHY="\"$RM_K\" 가 정확히 한 번 있어야 합니다"; return 1; }
  done
  M_VER=$(json_str ver "$2")
  M_SHA=$(json_str sha256 "$2")
  M_SIZE=$(json_num size "$2")
  M_URL=$(json_str url "$2")
  is_ver "$M_VER" || { RM_WHY="ver 가 X.Y.Z 가 아닙니다"; return 1; }
  case "$M_SHA" in *[!0-9a-f]*) M_SHA= ;; esac
  [ "${#M_SHA}" = 64 ] || { RM_WHY="sha256 이 64자리 소문자 16진수가 아닙니다"; return 1; }
  case "$M_SIZE" in ''|0*|*[!0-9]*) RM_WHY="size 가 양의 정수가 아닙니다"; return 1 ;; esac
  [ "${#M_SIZE}" -le 9 ] || { RM_WHY="size 가 너무 큽니다"; return 1; }
  case "$M_URL" in *[[:space:][:cntrl:]]*|*'"'*|*"'"*|*'\'*) RM_WHY="url 에 쓸 수 없는 글자가 있습니다"; return 1 ;; esac
  case "$M_URL" in
    https://?*) ;;
    http://?*) [ "$ALLOW_HTTP" = 1 ] || { RM_WHY="url 이 https 가 아닙니다"; return 1; } ;;
    *) RM_WHY="url 이 https 가 아닙니다"; return 1 ;;
  esac
  read_platforms "$2"
  return 0
}

# ---------------------------------------------------------------- zip 이름 검사·풀기

check_names() {   # $1=이름 목록(한 줄에 하나) — 하나라도 어긋나면 CN_WHY 를 두고 1
  CN_WHY=
  CN_N=0
  while IFS= read -r CN_NAME || [ -n "$CN_NAME" ]; do
    CN_N=$((CN_N + 1))
    # unzip 목록은 제어문자를 ^A·^J 로 보여 주고 풀 때는 그 글자를 지운다('\001../x' 가 '^A../x' 로 보이다가 '../x' 가 된다).
    # 그래서 ^ 가 든 이름은 제어문자와 같이 본다(배포 zip 에는 ^ 가 든 이름이 없다)
    case "$CN_NAME" in
      '') CN_WHY="빈 이름"; return 1 ;;
      /*) CN_WHY="절대 경로: $CN_NAME"; return 1 ;;
      *'\'*) CN_WHY="역슬래시가 든 이름: $CN_NAME"; return 1 ;;
      *[[:cntrl:]]*|*'^'*) CN_WHY="제어문자(^)가 든 이름"; return 1 ;;
    esac
    case "/$CN_NAME/" in */../*) CN_WHY="'..' 가 든 이름: $CN_NAME"; return 1 ;; esac
    case "${CN_NAME%%/*}" in
      .*) CN_WHY="점으로 시작하는 최상위 이름: ${CN_NAME%%/*}"; return 1 ;;
      start-connector.sh) CN_WHY="설치본이 만드는 이름(런처): start-connector.sh"; return 1 ;;
    esac
  done <"$1"
  [ "$CN_N" -gt 0 ] || { CN_WHY="빈 압축 파일"; return 1; }
  # 같은 이름이 두 번이면 도구마다 다른 내용을 볼 수 있다. 대소문자도 무시한다(맥 APFS 는 대소문자를 가리지 않는다)
  CN_DUP=$(LC_ALL=C tr 'A-Z' 'a-z' <"$1" | sed 's:/*$::' | LC_ALL=C sort | LC_ALL=C uniq -d | sed -n 1p)
  [ -z "$CN_DUP" ] || { CN_WHY="같은 이름이 두 번: $CN_DUP"; return 1; }
  return 0
}

zip_list() {   # $1=zip $2=이름 목록 파일 — 심볼릭 링크 항목·제어문자 이름이면 ZL_WHY 를 두고 1
  ZL_WHY=
  if [ "$ZIPT" = unzip ]; then
    # 상대 이름으로 — 목록 머리줄('Archive: …')에 경로 글자가 섞이지 않게
    ( cd "$(dirname "$1")" && unzip -Z1 "${1##*/}" ) </dev/null >"$2" 2>/dev/null \
      || { ZL_WHY="zip 목록을 읽지 못했습니다(손상)"; return 1; }
    ( cd "$(dirname "$1")" && unzip -Z "${1##*/}" ) </dev/null >"$2.long" 2>/dev/null \
      || { ZL_WHY="zip 목록을 읽지 못했습니다(손상)"; return 1; }
    [ -z "$(sed -n '/^l/p' "$2.long")" ] || { ZL_WHY="심볼릭 링크 항목이 있습니다"; return 1; }
    return 0
  fi
  python3 -I -c "$ZIP_PY" list "$1" </dev/null >"$2" 2>/dev/null
  case $? in
    0) return 0 ;;
    3) ZL_WHY="제어문자가 든 이름이 있습니다" ;;
    4) ZL_WHY="심볼릭 링크 항목이 있습니다" ;;
    *) ZL_WHY="zip 목록을 읽지 못했습니다(손상)" ;;
  esac
  return 1
}

zip_extract() {   # $1=zip $2=풀 폴더
  if [ "$ZIPT" = unzip ]; then
    unzip -q -o "$1" -d "$2" </dev/null >/dev/null 2>&1
  else
    python3 -I -c "$ZIP_PY" extract "$1" "$2" </dev/null >/dev/null 2>&1
  fi
}

check_tree() {   # $1=stage — 푼 결과를 디스크에서 다시 본다(목록이 놓친 것). 지원하지 않는 모양이면 CT_WHY=unsupported
  CT_WHY=
  [ -z "$(find "$1" -type l 2>/dev/null | sed -n 1p)" ] || { CT_WHY="심볼릭 링크가 있습니다"; return 1; }
  [ -z "$(find "$1" ! -type f ! -type d 2>/dev/null | sed -n 1p)" ] || { CT_WHY="파일·폴더가 아닌 항목이 있습니다"; return 1; }
  [ -z "$(find "$1" -name '*[[:cntrl:]]*' 2>/dev/null | sed -n 1p)" ] || { CT_WHY="제어문자가 든 이름이 있습니다"; return 1; }
  [ -z "$(find "$1" -name '*\\*' 2>/dev/null | sed -n 1p)" ] || { CT_WHY="역슬래시가 든 이름이 있습니다"; return 1; }
  for CT_P in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    exists "$CT_P" || continue
    case "${CT_P##*/}" in
      .*|start-connector.sh) CT_WHY="최상위에 둘 수 없는 이름: ${CT_P##*/}"; return 1 ;;
    esac
  done
  if [ ! -f "$1/session_manager/__init__.py" ] || [ ! -f "$1/install.sh" ]; then
    CT_WHY=unsupported
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- 정리·인자

g_cleanup() {   # 끝날 때(실패) — 내가 만든 임시 폴더만, 이름 모양을 다시 보고 지운다
  case "$G_W" in */clewpath-get.*) rm -rf "$G_W" ;; esac
  case "$G_TMP" in
    */.clewpath-get.*) chmod -R u+rwX "$G_TMP" 2>/dev/null; rm -rf "$G_TMP" ;;   # 권한이 이상한 항목(0 폴더)도 지우게
  esac
  return 0
}

dir_check() {   # 설치 폴더로 쓸 수 있나 — install.sh 서막과 같은 거절을 내려받기 전에(받은 뒤 거절하면 헛걸음)
  if exists "$DIR"; then
    [ -d "$DIR" ] || die "설치 폴더 자리에 폴더가 아닌 것이 있습니다: $DIR"
    if [ -e "$DIR/.git" ] || [ -d "$DIR/ops" ]; then die "개발 체크아웃(.git·ops) 위에는 설치하지 않습니다: $DIR"; fi
    if [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && ! is_clewpath_tree "$DIR"; then
      die "설치 폴더가 비어 있지 않고 ClewPath 설치본도 아닙니다(덮어쓰지 않습니다): $DIR"
    fi
  fi
  return 0
}

dir_guard() {   # $1=설치 폴더 $2=무엇(말) — 설치 폴더로 둘 수 없는 자리(다섯 스크립트 같은 규칙). 받기 전에, 물리 경로로 다시
  path_ok "$1" || usage_die "$2 는 절대 경로여야 합니다(제어문자·. ·.. 조각 없이): $1"
  if [ "$1" = / ] || [ "$1" = "$HOME" ]; then usage_die "$2 가 / 나 홈입니다: $1"; fi
  # 홈(물리 경로) 자신이나 그 조상도 — 링크로 이름만 다른 홈까지. 물리 경로를 못 얻으면 $HOME 글자로
  DG_HOME=$(CDPATH='' cd "$HOME" 2>/dev/null && phys_pwd) || DG_HOME=$HOME
  case "$DG_HOME/" in "$1"/*) usage_die "$2 가 / 나 홈(또는 홈을 품은 폴더)입니다: $1" ;; esac
  # [mac] ps 는 ASCII 밖 바이트를 이스케이프해 보여 준다 — 런처·러너가 명령줄로 Host 를 알아보지 못한다(리눅스는 그대로)
  if [ "$OS" = mac ] && [ "$(printf '%s' "$1" | LC_ALL=C tr -d ' -~' | wc -c | tr -d ' ')" != 0 ]; then
    usage_die "맥에서는 $2 에 ASCII 밖 글자(한글 등)를 쓸 수 없습니다 — 맥 ps 가 그 글자를 바꿔 보여 Host 를 알아보지 못합니다. --dir 나 CLEWPATH_HOME 으로 ASCII 경로를 고르세요: $1"
  fi
  return 0
}

zipinfo_ok() {   # PATH 의 unzip 이 Info-ZIP 의 목록 모드(-Z·-Z1)를 하나 — 인자 없는 -Z 는 ZipInfo 사용법을 낸다.
  case "$(unzip -Z </dev/null 2>&1)" in *ZipInfo*) return 0 ;; esac   # busybox unzip(Alpine 기본)은 -Z 를 모른다
  return 1
}

need_tools() {   # 없으면 무엇을 설치하라고 말하고 1 → ZIPT(unzip|python) B64_TOOL
  NT_MISS=
  for NT_T in curl mktemp tr sed awk wc find dirname sort uniq; do
    command -v "$NT_T" >/dev/null 2>&1 || NT_MISS="$NT_MISS $NT_T"
  done
  if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1 \
     && ! command -v openssl >/dev/null 2>&1; then
    NT_MISS="$NT_MISS sha256sum(coreutils)"
  fi
  pick_b64 || NT_MISS="$NT_MISS base64(coreutils)"
  ZIPT=
  NT_ZIP=
  if zipinfo_ok; then
    ZIPT=unzip
  elif [ "$OS" = linux ] && command -v python3 >/dev/null 2>&1; then
    ZIPT=python                                 # unzip 이 없거나, 있어도 목록 모드가 없다(busybox)
  elif command -v unzip >/dev/null 2>&1; then
    NT_MISS="$NT_MISS unzip(Info-ZIP)"          # 이 unzip 으로는 풀기 전에 이름·링크를 볼 수 없다('손상'이 아니다)
    NT_ZIP=1
  else
    NT_MISS="$NT_MISS unzip"
  fi
  [ -z "$NT_MISS" ] && return 0
  fail "필요한 도구가 없습니다:$NT_MISS"
  if [ "$NT_ZIP" = 1 ] && [ "$OS" = linux ]; then
    say "         PATH 의 unzip 은 목록 모드(-Z)가 없습니다(busybox 등) — Info-ZIP unzip 을 설치하세요(예: sudo apk add unzip)"
  elif [ "$NT_ZIP" = 1 ]; then
    say "         PATH 의 unzip 은 목록 모드(-Z)가 없습니다 — 맥 기본 /usr/bin/unzip(Info-ZIP)이 먼저 오게 하세요"
  fi
  if [ "$OS" = linux ]; then
    say "         먼저 설치하세요(예: sudo apt install curl unzip coreutils / sudo dnf install curl unzip)"
  else
    say "         맥 기본 도구입니다 — PATH 를 확인하세요"
  fi
  return 1
}

parse_args() {
  while [ $# -gt 0 ]; do
    PA_K=$1
    PA_V=
    PA_EQ=0
    case "$PA_K" in --*=*) PA_V=${PA_K#*=}; PA_K=${PA_K%%=*}; PA_EQ=1 ;; esac
    case "$PA_K" in
      --cp-url|--dir|--port|--relay-url|--app-url|--client-token)
        if [ "$PA_EQ" = 0 ]; then
          [ $# -ge 2 ] || usage_die "$PA_K 의 값이 없습니다"
          PA_V=$2
          shift
        fi ;;
      --no-autostart|--no-provision|--allow-http|--allow-root|-h|--help)
        [ "$PA_EQ" = 0 ] || usage_die "$PA_K 는 값을 받지 않습니다" ;;
      -*) usage_die "모르는 인자: $PA_K" ;;
      *) usage_die "모르는 인자(값은 보이지 않게 생략)" ;;   # 토큰을 잘못 붙였을 수 있다 — 화면에 찍지 않는다
    esac
    case "$PA_K" in
      --cp-url) CP_URL=$PA_V ;;
      --dir) DIR=$PA_V; DIR_GIVEN=1 ;;
      --port) PORT=$PA_V; PORT_GIVEN=1 ;;
      --relay-url) RELAY_URL=$PA_V ;;
      --app-url) APP_URL=$PA_V ;;
      --client-token) CLIENT_TOKEN=$PA_V; TOKEN_ARGV=1 ;;
      --no-autostart) NO_AUTOSTART=1 ;;
      --no-provision) NO_PROVISION=1 ;;
      --allow-http) ALLOW_HTTP=1 ;;
      --allow-root) ALLOW_ROOT=1 ;;
      -h|--help) usage; exit 0 ;;
    esac
    shift
  done
}

check_args() {
  while :; do                                   # 끝의 / 는 반복해서 걷어 낸다(get.ps1 과 같은 정규화)
    case "$CP_URL" in */) CP_URL=${CP_URL%/} ;; *) break ;; esac
  done
  check_url "$CP_URL" https --cp-url
  [ -z "$RELAY_URL" ] || check_url "$RELAY_URL" wss --relay-url
  [ -z "$APP_URL" ] || check_url "$APP_URL" https --app-url
  case "$PORT" in ''|0*|*[!0-9]*) usage_die "--port 는 1-65535 입니다: $PORT" ;; esac
  if [ "${#PORT}" -gt 5 ] || [ "$PORT" -gt 65535 ]; then usage_die "--port 는 1-65535 입니다: $PORT"; fi
  # agent 토큰은 env 로만 그대로 넘어간다 — install.sh 가 같이 보는 것을 여기서 먼저(받은 뒤 거절되면 stage 가 남는다)
  case "$CLIENT_TOKEN${CLEWPATH_AGENT_TOKEN:-}" in *[[:cntrl:]]*) usage_die "토큰에 줄바꿈·제어문자가 있습니다" ;; esac
  if [ "$DIR_GIVEN" = 1 ]; then CA_WHAT="--dir"; else CA_WHAT="설치 위치(CLEWPATH_HOME)"; fi
  trim_slash "$DIR"
  DIR=$T_OUT
  dir_guard "$DIR" "$CA_WHAT"
  # 클로드 폴더 — install.sh 가 정리를 걸기 전에 거절하는 값(claude_home_guess 와 같은 순서, path_ok)
  CA_CH=${SESSION_MANAGER_CLAUDE_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}
  path_ok "$CA_CH" || die "클로드 폴더가 절대 경로가 아닙니다(SESSION_MANAGER_CLAUDE_HOME·CLAUDE_CONFIG_DIR): $CA_CH"
}

# ---------------------------------------------------------------- 흐름

main() {
  CP_URL="https://clewpath.pyongso.com/cp"
  DIR=''
  DIR_GIVEN=0
  PORT=5100
  PORT_GIVEN=0
  RELAY_URL=''
  APP_URL=''
  NO_AUTOSTART=0
  NO_PROVISION=0
  ALLOW_HTTP=0
  ALLOW_ROOT=0
  HTTP_USED=0
  CLIENT_TOKEN=${CLEWPATH_CLIENT_TOKEN:-}
  TOKEN_ARGV=0
  OS=''
  ZIPT=''
  B64_TOOL=''
  G_W=''
  G_TMP=''
  parse_args "$@"

  # G0. 환경
  case "${HOME:-}" in
    /*) ;;
    *) die "HOME 이 절대 경로가 아닙니다: '${HOME:-}'" ;;
  esac
  case "$(uname -s)" in
    Darwin) OS=mac ;;
    Linux) OS=linux ;;
    *) die "지원하지 않는 OS 입니다: $(uname -s) (맥·리눅스용 — Windows 는 irm https://clewpath.pyongso.com/get | iex)" ;;
  esac
  # G1. 경로
  # >>> clewpath-home (get.sh·install.sh·uninstall.sh·update_runner.sh 공통 — 바꾸면 넷 다)
  case "$(uname -s)" in
    Darwin) CLEWPATH_HOME="${CLEWPATH_HOME:-$HOME/Library/Application Support/ClewPath}" ;;
    *)      _xdg="${XDG_DATA_HOME:-}"; case "$_xdg" in /*) ;; *) _xdg="$HOME/.local/share" ;; esac
            CLEWPATH_HOME="${CLEWPATH_HOME:-$_xdg/clewpath}" ;;
  esac
  # <<< clewpath-home
  [ -n "$DIR" ] || DIR="$CLEWPATH_HOME/app"
  check_args
  if [ "$(id -u)" = 0 ] && [ "$ALLOW_ROOT" != 1 ]; then
    die "root(관리자)로는 설치하지 않습니다 — 일반 사용자로 실행하세요(sudo 없이)"
  fi
  need_tools || exit 1

  say ""
  say "=== ClewPath Host 받기 ==="
  say "  설치 위치 : $DIR"
  [ "$OS" = linux ] && warn "리눅스는 시험용입니다(공식 지원 아님)"
  [ "$HTTP_USED" = 1 ] && warn "--allow-http — 암호화되지 않은 주소를 씁니다(시험 전용)"
  [ "$ALLOW_ROOT" = 1 ] && [ "$(id -u)" = 0 ] && warn "--allow-root — root 로 설치합니다(시험 전용)"
  if [ "$TOKEN_ARGV" = 1 ]; then
    warn "--client-token 은 같은 PC 의 다른 사용자에게 보입니다(ps) — 환경변수로 넘기세요:"
    say "         curl -fsSL https://clewpath.pyongso.com/get.sh | CLEWPATH_CLIENT_TOKEN=… sh"
  fi
  dir_check

  G_W=$(mktemp -d "${TMPDIR:-/tmp}/clewpath-get.XXXXXX") || die "임시 폴더를 만들 수 없습니다(TMPDIR)"
  trap 'g_cleanup' EXIT
  trap 'exit 1' INT TERM HUP

  # G2. 매니페스트 — 404 는 '게시된 릴리스가 없음'으로 따로 알린다
  G_CODE=$(g_curl "$CP_URL/updates/manifest" -sS --max-time 20 --max-filesize 65536 -o "$G_W/manifest" \
           -w '%{http_code}' 2>"$G_W/curl.err")
  G_RC=$?
  if [ "$G_RC" != 0 ]; then
    die "릴리스 서버에 닿지 못했습니다: $(err_line "$G_W/curl.err") (curl $G_RC)"
  fi
  case "$G_CODE" in
    200) ;;
    404) die "아직 게시된 릴리스가 없습니다. 운영자에게 문의하세요." ;;
    *) die "릴리스 서버에 닿지 못했습니다: HTTP $G_CODE" ;;
  esac
  # G3. 내용 — 서명은 보지 않는다(머리말의 신뢰 모델). 형식·키 1회·platforms
  read_manifest "$G_W/manifest" "$G_W/claims" || die "릴리스 정보(매니페스트)가 올바르지 않습니다: $RM_WHY"
  say "  최신 버전 : $M_VER"
  case " $M_PLATS " in
    *" $OS "*) ;;
    *) die "이 릴리스($M_VER)는 맥·리눅스 설치를 지원하지 않습니다." ;;
  esac

  # G5. 내려받기와 무결성(받은 zip 은 임시 폴더에만 — 설치 폴더 옆에는 검사를 통과한 뒤에야 만든다)
  say "  내려받는 중... ($(( (M_SIZE + 1023) / 1024 )) KB)"
  G_ZIP="$G_W/clewpath-host.zip"
  g_curl "$M_URL" -fsSL --max-time 300 --max-filesize "$M_SIZE" -o "$G_ZIP" 2>"$G_W/curl.err"
  G_RC=$?
  [ "$G_RC" = 63 ] && die "크기 불일치 - 다시 시도하세요."         # 매니페스트보다 크다(curl --max-filesize)
  [ "$G_RC" = 0 ] || die "내려받지 못했습니다: $(err_line "$G_W/curl.err") (curl $G_RC)"
  [ "$(sha256_of "$G_ZIP")" = "$M_SHA" ] || die "SHA-256 불일치 - 받은 파일을 버립니다. 다시 시도하세요."
  [ "$(wc -c <"$G_ZIP" | tr -d ' ')" = "$M_SIZE" ] || die "크기 불일치 - 다시 시도하세요."
  ok "무결성 확인 (SHA-256)"

  # G6. 이름 검사(풀기 전) → 설치 폴더 옆 임시 stage(같은 파일 시스템 — install.sh 의 mv 가 rename 이 되게) → 풀기 → 사후 확인
  zip_list "$G_ZIP" "$G_W/names" || die "받은 파일을 풀 수 없습니다: $ZL_WHY"
  check_names "$G_W/names" || die "받은 파일에 둘 수 없는 항목이 있습니다: $CN_WHY"
  G_PARENT=$(dirname "$DIR")
  mkdir -p "$G_PARENT" 2>/dev/null || die "폴더를 만들 수 없습니다: $G_PARENT"
  if [ -d "$DIR" ]; then                         # 물리 경로로 — install.sh 가 phys_pwd 로 맞춰 보는 자리와 같게
    DIR=$(CDPATH='' cd "$DIR" 2>/dev/null && phys_pwd) || die "설치 폴더를 찾지 못했습니다: $DIR"
    G_PARENT=$(dirname "$DIR")
  else
    G_PARENT=$(CDPATH='' cd "$G_PARENT" 2>/dev/null && phys_pwd) || die "폴더를 찾지 못했습니다: $G_PARENT"
    case "$G_PARENT" in /) DIR="/${DIR##*/}" ;; *) DIR="$G_PARENT/${DIR##*/}" ;; esac
  fi
  dir_guard "$DIR" "설치 폴더(물리 경로)"       # 링크 너머가 홈·[mac] ASCII 밖이면 — stage 를 만들기 전에
  G_TMP=$(mktemp -d "$G_PARENT/.clewpath-get.XXXXXX") || die "임시 폴더를 만들 수 없습니다: $G_PARENT"
  mkdir "$G_TMP/stage" || die "임시 폴더를 만들 수 없습니다: $G_TMP"
  zip_extract "$G_ZIP" "$G_TMP/stage" || die "압축을 풀지 못했습니다(디스크 공간·권한을 확인하세요)"
  # zip 에 유닉스 권한이 적혀 있으면 unzip 이 그대로 쓴다(배포 zip 은 Windows 에서 만들어 없다). 읽고 옮기고 지울 수 있게
  # 내 권한만 채운다(그룹·남은 그대로, 링크는 따라가지 않는다) — 사후 확인이 막힌 폴더를 건너뛰지 않게 확인보다 먼저
  chmod -R u+rwX "$G_TMP/stage" || die "받은 파일의 권한을 맞추지 못했습니다: $G_TMP/stage"
  if ! check_tree "$G_TMP/stage"; then
    [ "$CT_WHY" = unsupported ] && die "이 릴리스($M_VER)는 맥·리눅스 설치를 지원하지 않습니다."
    die "받은 파일에 둘 수 없는 항목이 있습니다: $CT_WHY"
  fi
  ok "압축 해제"

  # G8. 넘기기 — stage 는 install.sh 가 옮기고 지운다(실패해도 그쪽이 치운다). 토큰은 env 로만
  set -- --from-stage "$G_TMP/stage" --dir "$DIR" --cp-url "$CP_URL"
  # --port 는 준 값일 때만 — 기본값을 넘기면 재설치 때 설정 파일의 포트와 달라 install.sh 가 헛 경고를 낸다
  if [ "$PORT_GIVEN" = 1 ]; then set -- "$@" --port "$PORT"; fi
  if [ -n "$RELAY_URL" ]; then set -- "$@" --relay-url "$RELAY_URL"; fi
  if [ -n "$APP_URL" ]; then set -- "$@" --app-url "$APP_URL"; fi
  if [ "$NO_AUTOSTART" = 1 ]; then set -- "$@" --no-autostart; fi
  if [ "$NO_PROVISION" = 1 ]; then set -- "$@" --no-provision; fi
  if [ "$ALLOW_HTTP" = 1 ]; then set -- "$@" --allow-http; fi
  if [ "$ALLOW_ROOT" = 1 ]; then set -- "$@" --allow-root; fi
  if [ -n "$CLIENT_TOKEN" ]; then export CLEWPATH_CLIENT_TOKEN="$CLIENT_TOKEN"; fi
  rm -rf "$G_W"
  G_W=''
  trap - EXIT INT TERM HUP
  exec /bin/sh "$G_TMP/stage/install.sh" "$@" </dev/null
}

main "$@"
