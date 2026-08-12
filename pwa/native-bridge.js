/* ClewPath native-bridge — 앱(Capacitor) 전용 코드의 유일한 수용처.
 *
 * 캡슐화 규칙(기획서 §12-4): 앱 전용 코드는 이 파일 안에만 존재한다.
 * index.html 은 window.ClewBridge 의 함수만 호출하고, 웹 모드에서는 전부
 * no-op/null 을 돌려받는다. index.html 에 isApp 분기를 직접 쓰지 않는다.
 *
 * 브리지 정의(기획서 확정 5개):
 *  ① registerPush  — 앱 푸시 등록(FCM/APNs, M3)
 *  ② keyboard      — 키보드/보조키 제어(M2)
 *  ③ 딥링크        — clewpath:// 및 페어링 링크 수신(아래 handlePairingUrl)
 *  ④ voice         — 음성 인터페이스 예약(2차)
 *  ⑤ syncRoomKey   — E2EE 룸 키 네이티브 미러(M1=Preferences,
 *                     M3 에서 iOS App Group 키체인/Android Keystore 로 승격 — NSE 복호용)
 */
(function () {
  'use strict';
  var cap = window.Capacitor;
  var isApp = !!(cap && cap.isNativePlatform && cap.isNativePlatform());
  var noop = function () {};

  // ---- 웹 기본형: 전부 no-op / null (웹 코드는 이 존재만 알면 된다) ----
  var B = {
    isApp: function () { return isApp; },
    modeOverride: function () { return null; },    // index.html 의 MODE 계산이 우선 참조
    wsBaseOverride: function () { return null; },  // index.html 의 wsUrl() 이 우선 참조
    registerPush: noop,                            // ①
    keyboard: { setup: noop },                     // ②
    voice: { startPTT: noop, stopPTT: noop, streamToWhisper: noop,
             speak: noop, setAudioSession: noop }, // ④
    syncRoomKey: noop                              // ⑤
  };
  window.ClewBridge = B;
  if (!isApp) return;

  /* ================= 이하 앱 전용 ================= */
  var Prefs = cap.registerPlugin('Preferences');
  var App = cap.registerPlugin('App');

  // 앱은 항상 릴레이 클라이언트다(로컬 번들이라 경로 추론이 'local' 로 빠지는 것을 교정)
  B.modeOverride = function () { return 'relay'; };
  // 페어링 링크에서 저장한 릴레이 WS 주소. 없으면 null → 페어링 오버레이가 뜬다.
  B.wsBaseOverride = function () { return localStorage.getItem('sm_relay_ws') || null; };

  // ---- 저장소 미러(기획서 §12-3): sm_* 키를 네이티브 Preferences 에 복제 ----
  // iOS WKWebView 는 디스크 압박 시 localStorage 를 퇴거할 수 있다. 쓰기 시점에
  // 미러하고, 기동 시 localStorage 가 비어 있으면 복원 후 1회 리로드로 자가 치유.
  var origSet = Storage.prototype.setItem, origRem = Storage.prototype.removeItem;
  Storage.prototype.setItem = function (k, v) {
    origSet.call(this, k, v);
    if (this === window.localStorage && String(k).indexOf('sm_') === 0)
      Prefs.set({ key: k, value: String(v) }).catch(noop);
  };
  Storage.prototype.removeItem = function (k) {
    origRem.call(this, k);
    if (this === window.localStorage && String(k).indexOf('sm_') === 0)
      Prefs.remove({ key: k }).catch(noop);
  };

  // ⑤ E2EE 룸 키 미러. 지금은 Preferences(위 미러와 동일 저장소)지만, M3 에서
  // iOS 는 App Group 키체인으로 옮겨 NSE(알림 복호 프로세스)가 읽게 한다.
  B.syncRoomKey = function (room, key) {
    if (!room) return;
    Prefs.set({ key: 'sm_e2ee_' + room, value: String(key || '') }).catch(noop);
  };

  // 기동 복원: localStorage 에 페어링 흔적이 없는데 네이티브 미러에 있으면 되살린다.
  function restoreIfEvicted() {
    if (localStorage.getItem('sm_room') || localStorage.getItem('sm_pcs')) return; // 정상
    if (sessionStorage.getItem('cb_restored')) return;      // 이번 실행에서 이미 시도
    Prefs.keys().then(function (r) {
      var ks = (r && r.keys || []).filter(function (k) { return k.indexOf('sm_') === 0; });
      if (!ks.length) return;                               // 첫 실행(복원할 것 없음)
      return Promise.all(ks.map(function (k) {
        return Prefs.get({ key: k }).then(function (v) {
          if (v && v.value != null) origSet.call(localStorage, k, v.value);
        });
      })).then(function () {
        sessionStorage.setItem('cb_restored', '1');
        location.reload();
      });
    }).catch(noop);
  }

  // ---- ③ 딥링크/페어링 링크 ----
  // 수용 형태: PC QR 의 https://<릴레이>/relay/app#room=..&rk=..&cs=..&cp=..&dev=..
  //           또는 clewpath://pair#<같은 fragment>
  // 처리: 릴레이 WS 주소를 도출·저장하고, fragment 를 번들 페이지 해시로 옮겨
  //       리로드 — 이후는 index.html 의 기존 페어링 파싱 경로가 그대로 처리한다.
  function handlePairingUrl(raw) {
    var u;
    try { u = new URL(String(raw).trim()); } catch (e) { return 'bad_url'; }
    var frag = u.hash ? u.hash.slice(1) : (u.search ? u.search.slice(1) : '');
    if (u.protocol === 'https:' || u.protocol === 'http:') {
      // iOS(WKWebView)는 보안 컨텍스트에서 평문 ws:// 를 차단한다 — 외부(https) 링크만 수용.
      // (로컬 http://127.0.0.1 링크는 폰에서 자기 자신을 가리켜 어차피 무의미)
      if (u.protocol === 'http:') return 'need_https';
      // /relay/app → wss://host/relay/ws  (index.html wsUrl() 과 같은 유도 규칙)
      var base = u.pathname.replace(/\/(app|index\.html)\/?$/, '').replace(/\/$/, '');
      localStorage.setItem('sm_relay_ws', 'wss://' + u.host + base + '/ws');
    } else if (!localStorage.getItem('sm_relay_ws')) {
      return 'bad_url';                    // clewpath:// 는 릴레이 주소 기저장 필요
    }
    if (!frag) return 'no_frag';           // 페어링 정보(#room=..)가 없는 링크
    // 주의: location.href 로 경로를 바꾸면 뒤이은 reload() 가 내비게이션을 삼켜
    // fragment 가 유실된다(실기기 재현). 해시만 바꾸고 리로드 — 해시는 리로드에도
    // 유지되고, index.html 부트 파서가 location.hash 에서 rk/cs/dev 를 저장한다.
    location.hash = frag;
    location.reload();
    return 'ok';
  }
  App.addListener('appUrlOpen', function (ev) {
    if (ev && ev.url) handlePairingUrl(ev.url);
  });

  // ---- 페어링 오버레이(M1 최소): 릴레이 주소가 없으면 링크 붙여넣기 안내 ----
  // 정식 온보딩(QR 카메라·데모)은 M4. 내부 설치 테스트를 위한 최소 경로만 둔다.
  var PAIR_ERR = {
    bad_url: '링크 형식을 확인해 주세요 (https://… 전체를 붙여넣어야 합니다)',
    need_https: 'PC 화면(127.0.0.1) 주소가 아니라, 📱 새 기기 추가가 만든 외부 접속(https) 링크를 붙여넣어 주세요',
    no_frag: '이 링크에는 페어링 정보(#room=…)가 없습니다 - 📱 새 기기 추가에서 새 링크를 만들어 주세요'
  };
  function pairingOverlay() {
    if (localStorage.getItem('sm_relay_ws')) return;
    var ov = document.createElement('div');
    ov.id = 'cb-pair';
    ov.innerHTML =
      '<div class="cb-card"><h2>PC 연결</h2>' +
      '<ol style="margin:10px 0 4px 18px;padding:0;line-height:1.7;font-size:14px">' +
      '<li>PC 브라우저에서 ClewPath (<span style="font-family:monospace">127.0.0.1:5100</span>) 열기</li>' +
      '<li>상단 <b>📱</b> → <b>＋ 새 기기 추가</b></li>' +
      '<li>표시된 <b>외부 접속 링크(https)</b> 를 복사해 아래에 붙여넣기</li></ol>' +
      '<input type="url" id="cb-link" placeholder="https://…/relay/app#room=…" autocomplete="off">' +
      '<button id="cb-go">연결</button><div id="cb-err"></div></div>';
    document.body.appendChild(ov);
    ov.querySelector('#cb-go').addEventListener('click', function () {
      var v = ov.querySelector('#cb-link').value.trim();
      var err = ov.querySelector('#cb-err');
      if (!v) { err.textContent = PAIR_ERR.bad_url; return; }
      err.style.color = '';
      err.textContent = '연결 중…';
      var r = handlePairingUrl(v);          // 'ok' 면 이 아래는 리로드로 사라진다
      if (r !== 'ok') { err.style.color = '#e66'; err.textContent = PAIR_ERR[r] || PAIR_ERR.bad_url; }
    });
  }

  // ---- 하단 탭 골격(기획서 §4, M1 은 골격만) ----
  // 알림함(M3)·설정 집결(추후)은 자리만. 터미널 탭은 열려 있는 터미널로 복귀.
  function tabBar() {
    var bar = document.createElement('nav');
    bar.id = 'cb-tabs';
    var items = [
      ['세션', function () { window.showListPane && showListPane(); }],
      ['터미널', function () {
        var t = document.querySelector('#paneDetail.term-mode');
        if (t) { window.showDetailPane && showDetailPane(); }
        else if (window.toast) toast('실행 중인 터미널이 없습니다 — 세션에서 재개해 주세요');
      }],
      ['알림', function () { if (window.toast) toast('알림함은 다음 업데이트(M3)에서 열립니다'); }],
      ['설정', function () { window.showDevices && showDevices(); }]
    ];
    items.forEach(function (it) {
      var b = document.createElement('button');
      b.textContent = it[0];
      b.addEventListener('click', it[1]);
      bar.appendChild(b);
    });
    document.body.appendChild(bar);
    document.body.classList.add('cb-app');
  }

  // 앱 전용 스타일(탭바·오버레이). 웹 CSS 를 건드리지 않기 위해 여기서 주입.
  function styles() {
    var s = document.createElement('style');
    s.textContent =
      '#cb-tabs{position:fixed;left:0;right:0;bottom:0;display:flex;z-index:900;' +
      'background:var(--panel,#1b1f27);border-top:1px solid var(--line,#2a2f3a);' +
      'padding-bottom:env(safe-area-inset-bottom)}' +
      '#cb-tabs button{flex:1;padding:12px 0;background:none;border:0;' +
      'color:var(--fg,#dfe3ea);font-size:13px}' +
      'body.cb-app #app{padding-bottom:calc(46px + env(safe-area-inset-bottom))}' +
      '#cb-pair{position:fixed;inset:0;z-index:1000;background:var(--bg,#12151b);' +
      'display:flex;align-items:center;justify-content:center;padding:24px}' +
      '#cb-pair .cb-card{max-width:420px;width:100%}' +
      '#cb-pair input{width:100%;margin:12px 0;padding:10px;font-size:14px}' +
      '#cb-pair button{padding:10px 18px}' +
      '#cb-err{color:#e66;margin-top:8px;font-size:13px}';
    document.head.appendChild(s);
  }

  restoreIfEvicted();
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', function () { styles(); tabBar(); pairingOverlay(); });
  else { styles(); tabBar(); pairingOverlay(); }
})();
