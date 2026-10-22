"""PC 사이 세션 통신(pclink) — 짝지은 두 Host 사이의 조회 전용 원격 묻기.

선 위 약속의 정본은 docs/designs/pclink-protocol-v0.md 다. 층:
- protocol: 봉투·봉인·만료·중복 id 장부·결과 보관·짝짓기 암호(전송·암호 층)
- link: 상대 Host 의 릴레이 방에 client 로 붙는 짝 채널(전송 층)
- policy: 짝 목록·PC 등급 기기·세션별 허용·첫 연락 승인·한도·끄기·감사(정책 층)
- routes: 로컬 API 와 받는 쪽 처리(실행은 webapi.ephemeral_ask(isolated=True))
"""
