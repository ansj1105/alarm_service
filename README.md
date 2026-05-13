# alarm_service

Fox Coin 외부 감시용 텔레그램 알림 서비스입니다.

감시 대상
- `https://korion.io.kr/health`
- `https://api.korion.io.kr/health`
- primary PostgreSQL 상태
- standby PostgreSQL 상태
- synchronous replication 상태
- 핵심 DB probe (`coin_prices`, `pg_statistic`)
- 선택형 Foxya SSH 런타임 상태
  - `foxya-coin-api`, `foxya-api-2`, `foxya-db-proxy`, PostgreSQL, Redis 컨테이너 상태
  - 최근 로그의 DB proxy/Redis/DNS/DB connection 치명 패턴
  - 예: `ClosedConnectionException`, `Failed to read any response from the server`, `Connection is closed`, `Connection refused: db-proxy`, `connect ECONNREFUSED`, `Connection terminated unexpectedly`, `UnknownHostException`, `Failed to resolve 'redis'`, `postgres.*NOSRV`

배포
```bash
cp .env.example .env
docker compose up -d --build
```

Foxya SSH 런타임 감시를 켤 경우:
```bash
FOXYA_RUNTIME_CHECK_ENABLED=true
FOXYA_DOCKER_CHECK_MODE=socket
```

알람서비스가 Foxya 호스트 밖에서 실행되어 SSH로 점검해야 하는 경우:
```bash
FOXYA_RUNTIME_CHECK_ENABLED=true
FOXYA_DOCKER_CHECK_MODE=ssh
FOXYA_SSH_KEY_PATH=/run/secrets/korion.pem
```

`socket` 모드는 `/var/run/docker.sock` 마운트가 필요합니다. `ssh` 모드는 컨테이너에서 SSH 키를 읽을 수 있어야 하고, 원격 `ubuntu` 계정이 `sudo docker ...`를 실행할 수 있어야 합니다.

운영 기본 경로
- 서버: `/var/www/alarm_service`
