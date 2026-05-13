from __future__ import annotations

import os
import http.client
import json
import re
import shlex
import socket
import subprocess
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


def env_list(name: str, default: str) -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


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
        self.primary_db_target = self._build_target("PRIMARY_DB")
        self.standby_conninfo = self._build_optional_conninfo("STANDBY_DB")
        self.standby_db_target = self._build_target("STANDBY_DB") if self.standby_conninfo else "disabled"
        self.web_health_url = env("WEB_HEALTH_URL", "https://korion.io.kr/health")
        self.api_health_url = env("API_HEALTH_URL", "https://api.korion.io.kr/health")
        self.startup_message_enabled = env("STARTUP_MESSAGE_ENABLED", "true").lower() == "true"
        self.standby_check_enabled = env("STANDBY_CHECK_ENABLED", "true").lower() == "true"
        self.foxya_runtime_check_enabled = os.getenv(
            "FOXYA_RUNTIME_CHECK_ENABLED",
            os.getenv("FOXYA_SSH_CHECK_ENABLED", "false"),
        ).lower() == "true"
        self.foxya_docker_check_mode = env("FOXYA_DOCKER_CHECK_MODE", "ssh").lower()
        self.foxya_docker_socket_path = env("FOXYA_DOCKER_SOCKET_PATH", "/var/run/docker.sock")
        self.foxya_ssh_host = env("FOXYA_SSH_HOST", "52.200.97.155")
        self.foxya_ssh_user = env("FOXYA_SSH_USER", "ubuntu")
        self.foxya_ssh_port = env_int("FOXYA_SSH_PORT", 22)
        self.foxya_ssh_key_path = env("FOXYA_SSH_KEY_PATH", "")
        self.foxya_ssh_timeout_seconds = env_int("FOXYA_SSH_TIMEOUT_SECONDS", 15)
        self.foxya_docker_containers = env_list(
            "FOXYA_DOCKER_CONTAINERS",
            "foxya-coin-api,foxya-api-2,foxya-db-proxy,foxya-coin-postgres,foxya-coin-redis",
        )
        self.foxya_log_containers = env_list(
            "FOXYA_LOG_CONTAINERS",
            "foxya-coin-api,foxya-api-2,foxya-db-proxy",
        )
        self.foxya_log_lookback_minutes = env_int("FOXYA_LOG_LOOKBACK_MINUTES", 10)
        self.foxya_critical_log_patterns = env_list(
            "FOXYA_CRITICAL_LOG_PATTERNS",
            ",".join([
                "ClosedConnectionException",
                "Failed to read any response from the server",
                "Connection is closed",
                "Connection refused: db-proxy",
                "Connection refused: db-proxy/.*5432",
                "connect ECONNREFUSED",
                "Connection terminated unexpectedly",
                "UnknownHostException",
                "Failed to resolve 'redis'",
                "Failed to connect to Redis",
                "Failed to start MainVerticle",
                "backend-unresolved",
                "runtime-conflict",
                "status:restarting",
                "postgres.*NOSRV",
                "could not resolve address.*postgres",
                "server postgres/primary is DOWN",
            ]),
        )
        self.foxya_ignored_log_patterns = env_list(
            "FOXYA_IGNORED_LOG_PATTERNS",
            "Unauthorized,프로필 이미지를 찾을 수 없습니다",
        )
        self.offline_pay_runtime_check_enabled = env(
            "OFFLINE_PAY_RUNTIME_CHECK_ENABLED",
            "false",
        ).lower() == "true"
        self.offline_pay_ssh_host = env("OFFLINE_PAY_SSH_HOST", "98.91.96.182")
        self.offline_pay_ssh_user = env("OFFLINE_PAY_SSH_USER", "ubuntu")
        self.offline_pay_ssh_port = env_int("OFFLINE_PAY_SSH_PORT", 22)
        self.offline_pay_ssh_key_path = env("OFFLINE_PAY_SSH_KEY_PATH", "")
        self.offline_pay_ssh_timeout_seconds = env_int("OFFLINE_PAY_SSH_TIMEOUT_SECONDS", 15)
        self.offline_pay_log_containers = env_list(
            "OFFLINE_PAY_LOG_CONTAINERS",
            "korion_offline-app-api-1,korion_offline-app-worker-1",
        )
        self.offline_pay_log_lookback_minutes = env_int("OFFLINE_PAY_LOG_LOOKBACK_MINUTES", 10)
        self.offline_pay_critical_log_patterns = env_list(
            "OFFLINE_PAY_CRITICAL_LOG_PATTERNS",
            ",".join([
                "Offline Pay Settlement Dead Letter",
                "offline_pay\\.collateral\\.dead_letter",
                "COLLATERAL_LOCK_FAIL",
                "INSUFFICIENT_BALANCE",
                "Failed to request settlement",
                "HISTORY_SYNC_FAIL",
                "RECEIVER_HISTORY_SYNC_REQUESTED",
                "value too long for type character varying",
                "circuit opened",
                "coin_manage collateral request failed",
                "foxya.*status 5[0-9][0-9]",
                "coin_manage.*status 5[0-9][0-9]",
            ]),
        )
        self.offline_pay_ignored_log_patterns = env_list(
            "OFFLINE_PAY_IGNORED_LOG_PATTERNS",
            "",
        )

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

    def _build_optional_conninfo(self, prefix: str) -> Optional[str]:
        host = os.getenv(f"{prefix}_HOST", "").strip()
        if not host:
            return None
        return self._build_conninfo(prefix)

    def _build_target(self, prefix: str) -> str:
        host = env(f"{prefix}_HOST")
        port = env(f"{prefix}_PORT")
        return f"{host}:{port}"

    def run(self) -> None:
        if self.startup_message_enabled:
            self.notifier.send(
                "[KORION] External Monitor Started",
                "\n".join([
                    "service=alarm_service",
                    f"webHealthUrl={self.web_health_url}",
                    f"apiHealthUrl={self.api_health_url}",
                    f"primaryDb={self.primary_db_target}",
                    f"standbyDb={self.standby_db_target}",
                    f"criticalProbeSql={self.db_critical_probe_sql}",
                    f"catalogProbeSql={self.db_catalog_probe_sql}",
                    f"foxyaRuntimeCheckEnabled={self.foxya_runtime_check_enabled}",
                    f"foxyaDockerCheckMode={self.foxya_docker_check_mode}",
                    f"foxyaSshHost={self.foxya_ssh_host}",
                    f"offlinePayRuntimeCheckEnabled={self.offline_pay_runtime_check_enabled}",
                    f"offlinePaySshHost={self.offline_pay_ssh_host}",
                ]),
            )

        checks: Dict[str, Callable[[], CheckResult]] = {
            "web_health": self.check_web_health,
            "api_health": self.check_api_health,
            "primary_db": self.check_primary_db,
        }
        if self.standby_conninfo and self.standby_check_enabled:
            checks["standby_db"] = self.check_standby_db
        if self.foxya_runtime_check_enabled:
            checks["foxya_runtime"] = self.check_foxya_runtime
            checks["foxya_critical_logs"] = self.check_foxya_critical_logs
        if self.offline_pay_runtime_check_enabled:
            checks["offline_pay_critical_logs"] = self.check_offline_pay_critical_logs

        while True:
            for key, check in checks.items():
                self.process_result(key, check())
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
        try:
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
        except Exception as exc:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Primary DB Failed",
                body="\n".join([
                    f"target={self.primary_db_target}",
                    f"error={type(exc).__name__}: {exc}",
                ]),
                recovery_title="[KORION] External Recovered - Primary DB Failed",
                recovery_body=f"target={self.primary_db_target}\nstatus=ok",
            )

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
        if not self.standby_conninfo or not self.standby_check_enabled:
            return CheckResult(
                ok=True,
                title="[KORION] External Alert - Standby DB Failed",
                body="status=disabled",
                recovery_title="[KORION] External Recovered - Standby DB Failed",
                recovery_body="status=disabled",
            )
        try:
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
        except Exception as exc:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Standby DB Failed",
                body="\n".join([
                    f"target={self.standby_db_target}",
                    f"error={type(exc).__name__}: {exc}",
                ]),
                recovery_title="[KORION] External Recovered - Standby DB Failed",
                recovery_body=f"target={self.standby_db_target}\nstatus=ok",
            )

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

    def _ssh_base_command(self) -> list[str]:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.foxya_ssh_timeout_seconds}",
            "-p",
            str(self.foxya_ssh_port),
        ]
        if self.foxya_ssh_key_path:
            command.extend(["-i", self.foxya_ssh_key_path])
        command.append(f"{self.foxya_ssh_user}@{self.foxya_ssh_host}")
        return command

    def _run_foxya_ssh(self, remote_command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._ssh_base_command() + [remote_command],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.foxya_ssh_timeout_seconds + 10,
        )

    def _run_foxya_command(self, remote_command: str) -> subprocess.CompletedProcess[str]:
        if self.foxya_docker_check_mode == "local":
            return subprocess.run(
                ["bash", "-lc", remote_command],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.foxya_ssh_timeout_seconds + 10,
            )
        return self._run_foxya_ssh(remote_command)

    def _ssh_command(
        self,
        *,
        user: str,
        host: str,
        port: int,
        key_path: str,
        timeout_seconds: int,
        remote_command: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={timeout_seconds}",
            "-p",
            str(port),
        ]
        if key_path:
            command.extend(["-i", key_path])
        command.extend([f"{user}@{host}", remote_command])
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 10,
        )

    def _docker_socket_request(self, path: str) -> tuple[int, bytes]:
        class UnixSocketHTTPConnection(http.client.HTTPConnection):
            def __init__(self, socket_path: str, timeout: int) -> None:
                super().__init__("localhost", timeout=timeout)
                self.socket_path = socket_path

            def connect(self) -> None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(self.socket_path)

        connection = UnixSocketHTTPConnection(self.foxya_docker_socket_path, self.foxya_ssh_timeout_seconds)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def _docker_socket_container_status(self, name: str) -> tuple[str, str]:
        status, body = self._docker_socket_request(f"/containers/{name}/json")
        if status == 404:
            return "missing", "missing"
        if status < 200 or status >= 300:
            return f"http_{status}", "unknown"
        payload = json.loads(body.decode("utf-8"))
        state = payload.get("State") or {}
        health = (state.get("Health") or {}).get("Status") or "no-healthcheck"
        return str(state.get("Status") or "unknown"), str(health)

    def _docker_socket_container_logs(self, name: str) -> str:
        since = max(0, int(time.time()) - self.foxya_log_lookback_minutes * 60)
        status, body = self._docker_socket_request(
            f"/containers/{name}/logs?stdout=1&stderr=1&timestamps=1&since={since}"
        )
        if status < 200 or status >= 300:
            return f"docker_socket_log_error status={status}"
        return body.decode("utf-8", errors="replace")

    def check_foxya_runtime(self) -> CheckResult:
        if self.foxya_docker_check_mode == "socket":
            lines = []
            failures = []
            try:
                for name in self.foxya_docker_containers:
                    status, health = self._docker_socket_container_status(name)
                    line = f"{name} {status} {health}"
                    lines.append(line)
                    if status != "running":
                        failures.append(f"{name}=status:{status}")
                    elif health not in ("healthy", "no-healthcheck"):
                        failures.append(f"{name}=health:{health}")
            except Exception as exc:
                return CheckResult(
                    ok=False,
                    title="[KORION] External Alert - Foxya Runtime Failed",
                    body=f"dockerSocket={self.foxya_docker_socket_path}\nerror={type(exc).__name__}: {exc}",
                    recovery_title="[KORION] External Recovered - Foxya Runtime Failed",
                    recovery_body=f"dockerSocket={self.foxya_docker_socket_path}\nstatus=ok",
                )

            body = "\n".join([
                f"dockerSocket={self.foxya_docker_socket_path}",
                f"containers={','.join(self.foxya_docker_containers)}",
                *lines,
                *failures,
            ])
            return CheckResult(
                ok=not failures,
                title="[KORION] External Alert - Foxya Runtime Failed",
                body=body,
                recovery_title="[KORION] External Recovered - Foxya Runtime Failed",
                recovery_body="\n".join([
                    f"dockerSocket={self.foxya_docker_socket_path}",
                    "status=ok",
                    *lines,
                ]),
            )

        quoted_names = " ".join(shlex.quote(name) for name in self.foxya_docker_containers)
        remote_command = (
            "for name in " + quoted_names + "; do "
            "status=$(sudo docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' \"$name\" 2>/dev/null) || "
            "status=missing; "
            "echo \"$name $status\"; "
            "done"
        )
        try:
            result = self._run_foxya_command(remote_command)
        except Exception as exc:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Foxya Runtime Failed",
                body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nerror={type(exc).__name__}: {exc}",
                recovery_title="[KORION] External Recovered - Foxya Runtime Failed",
                recovery_body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nstatus=ok",
            )

        if result.returncode != 0:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Foxya Runtime Failed",
                body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nssh_exit={result.returncode}\nstderr={result.stderr.strip()}",
                recovery_title="[KORION] External Recovered - Foxya Runtime Failed",
                recovery_body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nstatus=ok",
            )

        failures = []
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        for line in lines:
            parts = line.split()
            if len(parts) < 2:
                failures.append(f"unparseable={line}")
                continue
            name, status = parts[0], parts[1]
            health = parts[2] if len(parts) > 2 else "no-healthcheck"
            if status != "running":
                failures.append(f"{name}=status:{status}")
            elif health not in ("healthy", "no-healthcheck"):
                failures.append(f"{name}=health:{health}")

        body = "\n".join([
            f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}",
            f"containers={','.join(self.foxya_docker_containers)}",
            *lines,
            *failures,
        ])
        return CheckResult(
            ok=not failures,
            title="[KORION] External Alert - Foxya Runtime Failed",
            body=body,
            recovery_title="[KORION] External Recovered - Foxya Runtime Failed",
            recovery_body="\n".join([
                f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}",
                "status=ok",
                *lines,
            ]),
        )

    def check_foxya_critical_logs(self) -> CheckResult:
        if not self.foxya_critical_log_patterns:
            return CheckResult(
                ok=True,
                title="[KORION] External Alert - Foxya Critical Logs",
                body="status=disabled",
                recovery_title="[KORION] External Recovered - Foxya Critical Logs",
                recovery_body="status=disabled",
            )

        pattern = "|".join(f"({item})" for item in self.foxya_critical_log_patterns)
        ignored_pattern = "|".join(f"({item})" for item in self.foxya_ignored_log_patterns)
        if self.foxya_docker_check_mode == "socket":
            try:
                compiled_pattern = re.compile(pattern, re.IGNORECASE)
                compiled_ignored = re.compile(ignored_pattern, re.IGNORECASE) if ignored_pattern else None
                matched_lines = []
                for name in self.foxya_log_containers:
                    for line in self._docker_socket_container_logs(name).splitlines():
                        if not compiled_pattern.search(line):
                            continue
                        if compiled_ignored and compiled_ignored.search(line):
                            continue
                        matched_lines.append(f"[{name}] {line}")
            except Exception as exc:
                return CheckResult(
                    ok=False,
                    title="[KORION] External Alert - Foxya Critical Logs",
                    body=f"dockerSocket={self.foxya_docker_socket_path}\nerror={type(exc).__name__}: {exc}",
                    recovery_title="[KORION] External Recovered - Foxya Critical Logs",
                    recovery_body=f"dockerSocket={self.foxya_docker_socket_path}\nstatus=ok",
                )

            body_lines = [
                f"dockerSocket={self.foxya_docker_socket_path}",
                f"lookbackMinutes={self.foxya_log_lookback_minutes}",
                f"containers={','.join(self.foxya_log_containers)}",
            ] + matched_lines[-50:]
            return CheckResult(
                ok=not matched_lines,
                title="[KORION] External Alert - Foxya Critical Logs",
                body="\n".join(body_lines),
                recovery_title="[KORION] External Recovered - Foxya Critical Logs",
                recovery_body="\n".join(body_lines[:3] + ["status=no recent critical log pattern"]),
            )

        quoted_names = " ".join(shlex.quote(name) for name in self.foxya_log_containers)
        remote_command = (
            "for name in " + quoted_names + "; do "
            f"sudo docker logs --since {self.foxya_log_lookback_minutes}m \"$name\" 2>&1 "
            f"| grep -E -i {shlex.quote(pattern)} "
        )
        if ignored_pattern:
            remote_command += f"| grep -E -i -v {shlex.quote(ignored_pattern)} "
        remote_command += "| tail -n 20 | sed \"s/^/[$name] /\"; done"

        try:
            result = self._run_foxya_command(remote_command)
        except Exception as exc:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Foxya Critical Logs",
                body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nerror={type(exc).__name__}: {exc}",
                recovery_title="[KORION] External Recovered - Foxya Critical Logs",
                recovery_body=f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}\nstatus=ok",
            )

        matched_lines = [line for line in result.stdout.splitlines() if line.strip()]
        body_lines = [
            f"target={self.foxya_ssh_user}@{self.foxya_ssh_host}",
            f"lookbackMinutes={self.foxya_log_lookback_minutes}",
            f"containers={','.join(self.foxya_log_containers)}",
        ] + matched_lines[-50:]
        return CheckResult(
            ok=not matched_lines,
            title="[KORION] External Alert - Foxya Critical Logs",
            body="\n".join(body_lines),
            recovery_title="[KORION] External Recovered - Foxya Critical Logs",
            recovery_body="\n".join(body_lines[:3] + ["status=no recent critical log pattern"]),
        )

    def check_offline_pay_critical_logs(self) -> CheckResult:
        if not self.offline_pay_critical_log_patterns:
            return CheckResult(
                ok=True,
                title="[KORION] External Alert - Offline Pay Critical Logs",
                body="status=disabled",
                recovery_title="[KORION] External Recovered - Offline Pay Critical Logs",
                recovery_body="status=disabled",
            )

        pattern = "|".join(f"({item})" for item in self.offline_pay_critical_log_patterns)
        ignored_pattern = "|".join(f"({item})" for item in self.offline_pay_ignored_log_patterns)
        quoted_names = " ".join(shlex.quote(name) for name in self.offline_pay_log_containers)
        remote_command = (
            "for name in " + quoted_names + "; do "
            f"sudo docker logs --since {self.offline_pay_log_lookback_minutes}m \"$name\" 2>&1 "
            f"| grep -E -i {shlex.quote(pattern)} "
        )
        if ignored_pattern:
            remote_command += f"| grep -E -i -v {shlex.quote(ignored_pattern)} "
        remote_command += "| tail -n 20 | sed \"s/^/[$name] /\"; done"

        try:
            result = self._ssh_command(
                user=self.offline_pay_ssh_user,
                host=self.offline_pay_ssh_host,
                port=self.offline_pay_ssh_port,
                key_path=self.offline_pay_ssh_key_path,
                timeout_seconds=self.offline_pay_ssh_timeout_seconds,
                remote_command=remote_command,
            )
        except Exception as exc:
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Offline Pay Critical Logs",
                body=f"target={self.offline_pay_ssh_user}@{self.offline_pay_ssh_host}\nerror={type(exc).__name__}: {exc}",
                recovery_title="[KORION] External Recovered - Offline Pay Critical Logs",
                recovery_body=f"target={self.offline_pay_ssh_user}@{self.offline_pay_ssh_host}\nstatus=ok",
            )

        if result.returncode not in (0, 1):
            return CheckResult(
                ok=False,
                title="[KORION] External Alert - Offline Pay Critical Logs",
                body="\n".join([
                    f"target={self.offline_pay_ssh_user}@{self.offline_pay_ssh_host}",
                    f"ssh_exit={result.returncode}",
                    f"stderr={result.stderr.strip()}",
                ]),
                recovery_title="[KORION] External Recovered - Offline Pay Critical Logs",
                recovery_body=f"target={self.offline_pay_ssh_user}@{self.offline_pay_ssh_host}\nstatus=ok",
            )

        matched_lines = [line for line in result.stdout.splitlines() if line.strip()]
        body_lines = [
            f"target={self.offline_pay_ssh_user}@{self.offline_pay_ssh_host}",
            f"lookbackMinutes={self.offline_pay_log_lookback_minutes}",
            f"containers={','.join(self.offline_pay_log_containers)}",
        ] + matched_lines[-50:]
        return CheckResult(
            ok=not matched_lines,
            title="[KORION] External Alert - Offline Pay Critical Logs",
            body="\n".join(body_lines),
            recovery_title="[KORION] External Recovered - Offline Pay Critical Logs",
            recovery_body="\n".join(body_lines[:3] + ["status=no recent critical log pattern"]),
        )


if __name__ == "__main__":
    Monitor().run()
