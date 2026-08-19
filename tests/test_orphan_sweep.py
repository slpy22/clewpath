"""고아 웹재개 스트림 판정 - '우리 서명 + 부모 사망'만 잡는다(임의 claude 불가침).

실사고(2026-08-19): 릴레이 경유 웹재개에서 폰이 소리 없이 사라지면 -p 스트림
claude 가 고아로 남았다(5일 생존 2건). 협의된 예외: ClewPath 자신이 낳은
프로세스 한정으로 기동 시 정리한다.
"""
from session_manager.webapi import find_orphan_stream_pids, _our_stream_signature

OURS = 'claude.EXE -p --resume abcd1234 --input-format stream-json --output-format stream-json'


def _p(pid, ppid, cmd=""):
    return {"pid": pid, "ppid": ppid, "cmdline": cmd}


def test_signature_matches_only_our_shape():
    assert _our_stream_signature(OURS)
    assert _our_stream_signature('claude -p "질문" --resume x --fork-session --output-format stream-json')
    # 임의의 claude 는 절대 아님
    assert not _our_stream_signature('claude.exe --resume --dangerously-skip-permissions')  # 대화형
    assert not _our_stream_signature('claude.exe --resume abcd')                            # 스트림 아님
    assert not _our_stream_signature('python -m session_manager.server')


def test_orphan_requires_dead_parent():
    procs = [
        _p(100, 1),            # (죽은 부모의) 시스템 프로세스 - 서명 없음 → 무시
        _p(200, 300, OURS),    # 우리 서명 + 부모(300) 생존 → 살려둠 (현역)
        _p(300, 1),            # 부모 격인 Host
        _p(400, 999, OURS),    # 우리 서명 + 부모(999) 사망 → 고아
        _p(500, 999, 'claude.exe --resume --dangerously-skip-permissions'),  # 부모 사망이어도 대화형은 불가침
    ]
    assert find_orphan_stream_pids(procs) == [400]
