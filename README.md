# alarm_service

Fox Coin 외부 감시용 텔레그램 알림 서비스입니다.

감시 대상
- `https://korion.io.kr/health`
- `https://api.korion.io.kr/health`
- primary PostgreSQL 상태
- standby PostgreSQL 상태
- synchronous replication 상태
- 핵심 DB probe (`coin_prices`, `pg_statistic`)

배포
```bash
cp .env.example .env
docker compose up -d --build
```

운영 기본 경로
- 서버: `/var/www/alarm_service`

