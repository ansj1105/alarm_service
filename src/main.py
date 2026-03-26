from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import psycopg
import requests


def env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class AlertState:
    consecutive_failures: int = 0
    active: bool = False
    last_signature: Optional[str] = None


@dataclass
class CheckResult:
    ok: bool
    title: str
    body: str
    recovery_title: str
    recovery_body: str


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, title: str, body: str) -> None:
        if not self.enabled:
            print(f"[telegram-disabled] {title}\n{body}")
            return

        requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": f"{title}\n{body}".strip(),
                "disable_web_page_preview": True,
            },
            timeout=10,
        ).raise_for_status()


class Monitor:
    def __init__(self) -> None:
        self.poll_interval_seconds = env_int("POLL_INTERVAL_SECONDS", 30)
        self.http_timeout_seconds = env_int("HTTP_TIMEOUT_SECONDS", 5)
        self.db_timeout_seconds = env_int("DB_TIMEOUT_SECONDS", 5)
        self.consecutive_failures = env_int("CONSECUTIVE_FAILURES", 2)
        self.standby_lag_threshold = env_int("STANDBY_REPLAY_LAG_SECONDS_THRESHOLD", 120)
        self.db_critical_probe_sql = env("DB_CRITICAL_PROBE_SQL", "SELECT 1 FROM public.coin_prices LIMIT 1")
        self.db_catalog_probe_sql = env("DB_CATALOG_PROBE_SQL", "SELECT 1 FROM pg_catalog.pg_statistic LIMIT 1")
        self.notifier = TelegramNotifier(env("TELEGRAM_BOT_TOKEN", ""), env("TELEGRAM_CHAT_ID", ""))
        self.states: Dict[str, AlertState] = {}

        self.primary_conninfo = self._build_conninfo("PRIMARY_DB")
        self.standby_conninfo = self._build_conninfo("STANDBY_DB")
        self.web_health_url = env("WEB_HEALTH_URL", "https://korion.io.kr/health")
        self.api_health_url = env("API_HEALTH_URL", "https://api.korion.io.kr/health")

    def _build_conninfo(self, prefix: str) -> str:
        return " ".join([
            f"host={env(f'{prefix}_HOST')}",
            f"port={env(f'{prefix}_PORT')}",
            f"dbname={env(f'{prefix}_NAME')}",
            f"user={env(f'{prefix}_USER')}",
            f"password={env(f'{prefix}_PASSWORD', '')}",
            f"connect_timeout={self.db_timeout_seconds}",
            "sslmode=prefer",
        ])

    def run(self) -> None:
        checks: Dict[str, Callable[[], CheckResult]] = {
            "web_health": self.check_web_health,
            "api_health": self.check_api_health,
            "primary_db": self.check_primary_db,
            "standby_db": self.check_standby_db,
        }

        while True:
            for key, check in checks.items():
                try:
                    self.process_result(key, check())
                except Exception as exc:
                    self.process_result(
                        key,
                        CheckResult(
                            ok=False,
                            title=f"[KORION] External Monitor Failed - {key}",
                            body=f"error={type(exc).__name__}: {exc}",
                            recovery_title=f"[KORION] External Monitor Recovered - {key}",
                            recovery_body="status=ok",
                        ),
                    )
            time.sleep(self.poll_interval_seconds)

    def process_result(self, key: str, result: CheckResult) -> None:
        state = self.states.setdefault(key, AlertState())
        if result.ok:
            state.consecutive_failures = 0
            if state.active:
                self.notifier.send(result.recovery_title, result.recovery_body)
                state.active = False
                state.last_signature = None
            return

        state.consecutive_failures += 1
        if state.consecutive_failures < self.consecutive_failures:
            return

        signature = f"{result.title}\n{result.body}"
        if state.active and state.last_signature == signature:
            return

        self.notifier.send(result.title, result.body)
        state.active = True
        state.last_signature = signature

    def check_web_health(self) -> CheckResult:
        return self._check_http("Web Health", self.web_health_url)

    def check_api_health(self) -> CheckResult:
        return self._check_http("API Health", self.api_health_url)

    def _check_http(self, label: str, url: str) -> CheckResult:
        try:
            response = requests.get(url, timeout=self.http_timeout_seconds)
            ok = 200 <= response.status_code < 300
            body = f"url={url}\nstatus={response.status_code}"
            return CheckResult(
                ok=ok,
                title=f"[KORION] External Alert - {label} Failed",
                body=body,
                recovery_title=f"[KORION] External Recovered - {label} Failed",
                recovery_body=f"url={url}\nstatus=ok",
            )
        except Exception as exc:
            return CheckResult(
                ok=False,
                title=f"[KORION] External Alert - {label} Failed",
                body=f"url={url}\nerror={type(exc).__name__}: {exc}",
                recovery_title=f"[KORION] External Recovered - {label} Failed",
                recovery_body=f"url={url}\nstatus=ok",
            )

    def check_primary_db(self) -> CheckResult:
        with psycopg.connect(self.primary_conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      current_database(),
                      pg_is_in_recovery(),
                      current_setting('transaction_read_only'),
                      COALESCE(current_setting('synchronous_standby_names', true), ''),
                      COALESCE((SELECT COUNT(*) FROM pg_stat_replication WHERE state = 'streaming'), 0),
                      COALESCE((SELECT COUNT(*) FROM pg_stat_replication WHERE state = 'streaming' AND sync_state IN ('sync', 'quorum')), 0)
                    """
                )
                database_name, in_recovery, read_only, standby_names, streaming_replicas, healthy_sync = cur.fetchone()

                cur.execute(self.db_critical_probe_sql)
                cur.fetchone()
                cur.execute(self.db_catalog_probe_sql)
                cur.fetchone()

        failures = []
        if in_recovery:
            failures.append("pg_is_in_recovery=true")
        if read_only == "on":
            failures.append("transaction_read_only=on")
        if standby_names and healthy_sync < 1:
            failures.append(f"healthy_sync_replicas={healthy_sync}")

        ok = not failures
        details = "\n".join([
            f"database={database_name}",
            f"pg_is_in_recovery={in_recovery}",
            f"transaction_read_only={read_only}",
            f"synchronous_standby_names={standby_names or '-'}",
            f"streaming_replicas={streaming_replicas}",
            f"healthy_sync_replicas={healthy_sync}",
            f"critical_probe_sql={self.db_critical_probe_sql}",
            f"catalog_probe_sql={self.db_catalog_probe_sql}",
        ] + failures)

        return CheckResult(
            ok=ok,
            title="[KORION] External Alert - Primary DB Failed",
            body=details,
            recovery_title="[KORION] External Recovered - Primary DB Failed",
            recovery_body="\n".join([
                f"database={database_name}",
                f"streaming_replicas={streaming_replicas}",
                f"healthy_sync_replicas={healthy_sync}",
            ]),
        )

    def check_standby_db(self) -> CheckResult:
        with psycopg.connect(self.standby_conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      current_database(),
                      pg_is_in_recovery(),
                      current_setting('transaction_read_only'),
                      EXTRACT(EPOCH FROM COALESCE(now() - pg_last_xact_replay_timestamp(), interval '0 second'))::bigint
                    """
                )
                database_name, in_recovery, read_only, replay_lag_seconds = cur.fetchone()

        failures = []
        if not in_recovery:
            failures.append("pg_is_in_recovery=false")
        if read_only != "on":
            failures.append(f"transaction_read_only={read_only}")
        if replay_lag_seconds is not None and replay_lag_seconds > self.standby_lag_threshold:
            failures.append(f"replay_lag_seconds={replay_lag_seconds}")

        ok = not failures
        details = "\n".join([
            f"database={database_name}",
            f"pg_is_in_recovery={in_recovery}",
            f"transaction_read_only={read_only}",
            f"replay_lag_seconds={replay_lag_seconds}",
            f"replay_lag_threshold={self.standby_lag_threshold}",
        ] + failures)

        return CheckResult(
            ok=ok,
            title="[KORION] External Alert - Standby DB Failed",
            body=details,
            recovery_title="[KORION] External Recovered - Standby DB Failed",
            recovery_body="\n".join([
                f"database={database_name}",
                f"replay_lag_seconds={replay_lag_seconds}",
            ]),
        )


if __name__ == "__main__":
    Monitor().run()
