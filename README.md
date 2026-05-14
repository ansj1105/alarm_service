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
  - Redis 메모리 사용률 임계치 초과 알림
- 예: `ClosedConnectionException`, `Failed to read any response from the server`, `Connection refused: db-proxy`, `connect ECONNREFUSED`, `Connection terminated unexpectedly`, `UnknownHostException`, `Failed to resolve 'redis'`, `backend-unresolved`, `runtime-conflict`, `postgres.*NOSRV`
- Foxya 가격 워커의 일시적 DB backoff 로그(`Coin price scheduled refresh ...`)는 기본 ignore 처리합니다. 다른 API 경로의 DB 응답 끊김은 계속 감지합니다.
- 선택형 `offline_pay` SSH 로그 상태
  - settlement/collateral dead-letter, 담보 부족, receiver history sync, Foxya/coin_manage 5xx 연동 실패 패턴
  - 예: `Offline Pay Settlement Dead Letter`, `offline_pay.collateral.dead_letter`, `COLLATERAL_LOCK_FAIL`, `INSUFFICIENT_BALANCE`, `Failed to request settlement`, `HISTORY_SYNC_FAIL`
  - Redis 메모리 사용률 임계치 초과 알림
- 선택형 `coin_manage` SSH 런타임/로그 상태
  - `korion-app-api-1`, `korion-app-ops-1`, `korion-app-signer-1`, `korion-app-withdraw-worker-1`, PostgreSQL, Redis, `ledger-signer` 상태
  - ledger/settlement/collateral/withdraw/deposit 실패, DB/Redis 연결 실패, dead-letter 패턴
  - Redis 메모리 사용률 임계치 초과 알림
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
FOXYA_REDIS_MEMORY_CHECK_ENABLED=true
FOXYA_REDIS_CONTAINERS=foxya-redis
```

알람서비스가 Foxya 호스트 밖에서 실행되어 SSH로 점검해야 하는 경우:
```bash
FOXYA_RUNTIME_CHECK_ENABLED=true
FOXYA_DOCKER_CHECK_MODE=ssh
FOXYA_SSH_KEY_PATH=/run/secrets/korion.pem
FOXYA_DOCKER_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy,foxya-postgres,foxya-redis
FOXYA_LOG_CONTAINERS=foxya-api,foxya-api-2,foxya-db-proxy
FOXYA_REDIS_MEMORY_CHECK_ENABLED=true
FOXYA_REDIS_CONTAINERS=foxya-redis
```

`socket` 모드는 `/var/run/docker.sock` 마운트가 필요합니다. Foxya Redis 메모리 체크도 socket 모드에서는 Docker socket exec로 Redis 정보를 읽습니다. `ssh` 모드는 컨테이너에서 SSH 키를 읽을 수 있어야 하고, 원격 `ubuntu` 계정이 `sudo docker ...`를 실행할 수 있어야 합니다.

`offline_pay` 로그 감시를 켤 경우:
```bash
OFFLINE_PAY_RUNTIME_CHECK_ENABLED=true
OFFLINE_PAY_SSH_HOST=98.91.96.182
OFFLINE_PAY_SSH_KEY_SOURCE=/home/ubuntu/.ssh/korion.pem
OFFLINE_PAY_SSH_KEY_PATH=/run/secrets/korion.pem
OFFLINE_PAY_LOG_CONTAINERS=korion_offline-app-api-1,korion_offline-app-worker-1
OFFLINE_PAY_REDIS_MEMORY_CHECK_ENABLED=true
OFFLINE_PAY_REDIS_CONTAINERS=korion_offline-redis-1
```

`coin_manage` 감시를 켤 경우:
```bash
COIN_MANAGE_RUNTIME_CHECK_ENABLED=true
COIN_MANAGE_SSH_HOST=54.83.183.123
COIN_MANAGE_SSH_KEY_PATH=/run/secrets/korion.pem
COIN_MANAGE_DOCKER_CONTAINERS=korion-app-api-1,korion-app-ops-1,korion-app-signer-1,korion-app-withdraw-worker-1,korion-postgres,korion-redis,ledger-signer
COIN_MANAGE_LOG_CONTAINERS=korion-app-api-1,korion-app-ops-1,korion-app-signer-1,korion-app-withdraw-worker-1,ledger-signer
COIN_MANAGE_REDIS_MEMORY_CHECK_ENABLED=true
COIN_MANAGE_REDIS_CONTAINERS=korion-redis
```

`coin_csms` 감시를 켤 경우:
```bash
COIN_CSMS_RUNTIME_CHECK_ENABLED=true
COIN_CSMS_SSH_HOST=52.200.97.155
COIN_CSMS_SSH_KEY_PATH=/run/secrets/korion.pem
COIN_CSMS_DOCKER_CONTAINERS=csms-api
COIN_CSMS_LOG_CONTAINERS=csms-api
```

Redis 메모리 임계치는 공통 환경변수로 조정합니다.
```bash
REDIS_MEMORY_USAGE_THRESHOLD_PERCENT=80
REDIS_MEMORY_FAIL_ON_UNBOUNDED=false
```

Redis `maxmemory`가 설정되어 있으면 그 값을 기준으로 사용률을 계산합니다. `maxmemory=0`이면 Docker 컨테이너 memory limit을 기준으로 사용하고, 그것도 없으면 원격 호스트 `MemTotal`을 기준으로 사용합니다. 세 값이 모두 없으면 `unbounded`로 보고 기본값에서는 알림 실패로 처리하지 않습니다.

운영 기본 경로
- 서버: `/var/www/alarm_service`
