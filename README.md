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
  - `foxya-api`, `foxya-api-2`, `foxya-db-proxy`, PostgreSQL, Redis 컨테이너 상태
  - 최근 로그의 DB proxy/Redis/DNS/DB connection 치명 패턴
- 예: `ClosedConnectionException`, `Failed to read any response from the server`, `Connection refused: db-proxy`, `connect ECONNREFUSED`, `Connection terminated unexpectedly`, `UnknownHostException`, `Failed to resolve 'redis'`, `backend-unresolved`, `runtime-conflict`, `postgres.*NOSRV`
- 선택형 `offline_pay` SSH 로그 상태
  - settlement/collateral dead-letter, 담보 부족, receiver history sync, Foxya/coin_manage 5xx 연동 실패 패턴
  - 예: `Offline Pay Settlement Dead Letter`, `offline_pay.collateral.dead_letter`, `COLLATERAL_LOCK_FAIL`, `INSUFFICIENT_BALANCE`, `Failed to request settlement`, `HISTORY_SYNC_FAIL`
- 선택형 `coin_manage` SSH 런타임/로그 상태
  - `korion-app-api-1`, `korion-app-ops-1`, `korion-app-signer-1`, `korion-app-withdraw-worker-1`, PostgreSQL, Redis, `ledger-signer` 상태
  - ledger/settlement/collateral/withdraw/deposit 실패, DB/Redis 연결 실패, dead-letter 패턴
- 선택형 `coin_csms` SSH 런타임/로그 상태
  - `csms-api` 상태 및 admin bridge/DB/Redis/Foxya/coin_manage 연동 실패 패턴

배포
```bash
cp .env.example .env
docker compose up -d --build
```

Foxya SSH 런타임 감시를 켤 경우:
```bash
FOXYA_RUNTIME_CHECK_ENABLED=true
FOXYA_DOCKER_CHECK_MODE=socket
FOXYA_DOCKER_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy,foxya-postgres,foxya-redis
FOXYA_LOG_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy
```

알람서비스가 Foxya 호스트 밖에서 실행되어 SSH로 점검해야 하는 경우:
```bash
FOXYA_RUNTIME_CHECK_ENABLED=true
FOXYA_DOCKER_CHECK_MODE=ssh
FOXYA_SSH_KEY_PATH=/run/secrets/korion.pem
FOXYA_DOCKER_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy,foxya-postgres,foxya-redis
FOXYA_LOG_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy
```

`socket` 모드는 `/var/run/docker.sock` 마운트가 필요합니다. `ssh` 모드는 컨테이너에서 SSH 키를 읽을 수 있어야 하고, 원격 `ubuntu` 계정이 `sudo docker ...`를 실행할 수 있어야 합니다.

`offline_pay` 로그 감시를 켤 경우:
```bash
OFFLINE_PAY_RUNTIME_CHECK_ENABLED=true
OFFLINE_PAY_SSH_HOST=98.91.96.182
OFFLINE_PAY_SSH_KEY_SOURCE=/home/ubuntu/.ssh/korion.pem
OFFLINE_PAY_SSH_KEY_PATH=/run/secrets/korion.pem
OFFLINE_PAY_LOG_CONTAINERS=korion_offline-app-api-1,korion_offline-app-worker-1
```

`coin_manage` 감시를 켤 경우:
```bash
COIN_MANAGE_RUNTIME_CHECK_ENABLED=true
COIN_MANAGE_SSH_HOST=54.83.183.123
COIN_MANAGE_SSH_KEY_PATH=/run/secrets/korion.pem
COIN_MANAGE_DOCKER_CONTAINERS=korion-app-api-1,korion-app-ops-1,korion-app-signer-1,korion-app-withdraw-worker-1,korion-postgres,korion-redis,ledger-signer
COIN_MANAGE_LOG_CONTAINERS=korion-app-api-1,korion-app-ops-1,korion-app-signer-1,korion-app-withdraw-worker-1,ledger-signer
```

`coin_csms` 감시를 켤 경우:
```bash
COIN_CSMS_RUNTIME_CHECK_ENABLED=true
COIN_CSMS_SSH_HOST=52.200.97.155
COIN_CSMS_SSH_KEY_PATH=/run/secrets/korion.pem
COIN_CSMS_DOCKER_CONTAINERS=csms-api
COIN_CSMS_LOG_CONTAINERS=csms-api
```

운영 기본 경로
- 서버: `/var/www/alarm_service`
