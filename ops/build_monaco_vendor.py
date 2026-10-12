"""파일 보기(📁)용 Monaco 를 pwa/vendor 의 '평평한 파일 2개'로 만든다.

왜 한 파일로 묶나: 릴레이(007)의 /vendor/* 는 하위 폴더 없는 파일 이름만 서빙한다(경로 탈출 방지).
Monaco 의 AMD 모듈들은 이름 있는 define 이라 이어 붙여도 로더가 그대로 찾는다 — 릴레이 코드를 안 바꾸고
Host 릴리스만으로(pwa 폴더 bind mount) 원격에도 반영된다.

입력: npm pack 으로 받은 패키지 폴더(고정 버전)
  monaco-editor@0.52.2, marked@12.0.2, dompurify@3.1.6
사용:
  python ops/build_monaco_vendor.py --monaco <monaco-editor/package> --marked <marked/package> --purify <dompurify/package>
출력: pwa/vendor/monaco.js, pwa/vendor/monaco.css, pwa/vendor/monaco.LICENSE.txt
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pwa" / "vendor"

# 문법 색을 넣을 언어(basic-languages). PWA 의 확장자 표(FB_LANG)와 맞춘다. 여기 없는 언어는 일반 텍스트.
LANGS = ["python", "javascript", "typescript", "markdown", "html", "css", "scss", "less", "xml", "yaml",
         "shell", "powershell", "bat", "sql", "mysql", "pgsql", "java", "kotlin", "cpp", "csharp", "go",
         "rust", "ruby", "php", "swift", "dockerfile", "ini", "lua", "r", "perl", "graphql", "scala",
         "dart", "hcl", "protobuf"]

# 언어 서비스(워커) 모듈 자리표시 — 우리는 읽기 전용 문법 색만 쓴다. 없으면 로더가 404 를 받으러 간다.
_DISPOSABLE = "{dispose(){}}"
STUBS = f"""
define("vs/language/typescript/tsMode",[],function(){{return{{setupTypeScript:function(){{return {_DISPOSABLE}}},setupJavaScript:function(){{return {_DISPOSABLE}}}}}}});
define("vs/language/json/jsonMode",[],function(){{return{{setupMode:function(){{return {_DISPOSABLE}}}}}}});
define("vs/language/css/cssMode",[],function(){{return{{setupMode:function(){{return {_DISPOSABLE}}}}}}});
define("vs/language/html/htmlMode",[],function(){{return{{setupMode:function(){{return {_DISPOSABLE}}},setupMode1:function(){{return {_DISPOSABLE}}}}}}});
"""


def _strip_map(js: str) -> str:
    return re.sub(r"//# sourceMappingURL=\S+", "", js)


def _nls_iife(js: str) -> str:
    """nls.messages.ko.js 는 이름 없는 define 이라 이어 붙이면 로더가 거부한다 → 즉시 실행 함수로 바꾼다.
    본문은 globalThis._VSCODE_NLS_MESSAGES/LANGUAGE 를 채울 뿐이다."""
    js = _strip_map(js).strip()
    head = "define([], function () {"
    if not js.startswith(head) or not js.endswith("});"):
        raise SystemExit("nls.messages.ko.js 형식이 예상과 다릅니다(define 래퍼)")
    return "(function () {" + js[len(head):-3] + "})();"


def build(monaco: Path, marked: Path, purify: Path) -> dict:
    vs = monaco / "min" / "vs"
    version = json.loads((monaco / "package.json").read_text(encoding="utf-8"))["version"]
    parts = [
        f"/* ClewPath vendor bundle: monaco-editor {version} + marked + DOMPurify. "
        "생성: ops/build_monaco_vendor.py — 직접 고치지 말 것. 라이선스: monaco.LICENSE.txt */",
        # marked·DOMPurify 는 UMD 라 AMD 로더보다 먼저 와야 전역(window.marked/DOMPurify)에 붙는다.
        _strip_map((marked / "marked.min.js").read_text(encoding="utf-8")),
        _strip_map((purify / "dist" / "purify.min.js").read_text(encoding="utf-8")),
        _nls_iife((vs / "nls.messages.ko.js").read_text(encoding="utf-8")),
        _strip_map((vs / "loader.js").read_text(encoding="utf-8")),
        # 설정은 define 들보다 먼저: 로더가 define 시점에 의존성(vs/css!…)을 받으러 가기 때문(실측 —
        # 나중에 config 하면 /vs/editor/editor.main.css 404 로 영영 안 뜬다). CSS 는 monaco.css 로 따로 싣는다.
        "require.config({paths:{vs:'__monaco_missing__'},'vs/css':{disabled:true}});",
        _strip_map((vs / "editor" / "editor.main.js").read_text(encoding="utf-8")),
        STUBS,
    ]
    for lang in LANGS:
        f = vs / "basic-languages" / lang / f"{lang}.js"
        if not f.is_file():
            raise SystemExit(f"언어 파일 없음: {f}")
        parts.append(_strip_map(f.read_text(encoding="utf-8")))
    js = "\n;\n".join(parts) + "\n"

    css = (vs / "editor" / "editor.main.css").read_text(encoding="utf-8")
    ttf = base64.b64encode((vs / "base/browser/ui/codicons/codicon/codicon.ttf").read_bytes()).decode()
    font_ref = "../base/browser/ui/codicons/codicon/codicon.ttf"
    if font_ref not in css:
        raise SystemExit("editor.main.css 의 아이콘 글꼴 경로가 예상과 다릅니다")
    css = _strip_map(css).replace(font_ref, f"data:font/ttf;base64,{ttf}")
    css = re.sub(r"/\*# sourceMappingURL=\S+ \*/", "", css)

    lic = [f"monaco-editor {version} — MIT", (monaco / "LICENSE").read_text(encoding="utf-8"),
           "marked — MIT", (marked / "LICENSE.md").read_text(encoding="utf-8"),
           "DOMPurify — Apache-2.0 OR MPL-2.0", (purify / "LICENSE").read_text(encoding="utf-8")]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "monaco.js").write_text(js, encoding="utf-8", newline="\n")
    (OUT / "monaco.css").write_text(css, encoding="utf-8", newline="\n")
    (OUT / "monaco.LICENSE.txt").write_text("\n\n".join(lic), encoding="utf-8", newline="\n")
    return {"version": version, "js_bytes": len(js.encode()), "css_bytes": len(css.encode()), "langs": len(LANGS)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--monaco", required=True, type=Path)
    ap.add_argument("--marked", required=True, type=Path)
    ap.add_argument("--purify", required=True, type=Path)
    a = ap.parse_args()
    print(build(a.monaco, a.marked, a.purify))
