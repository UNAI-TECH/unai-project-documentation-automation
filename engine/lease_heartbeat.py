"""Worker lease heartbeat configuration and coordinated background renewal.

Maintains an active attempt lease via periodic short-lived pooled transactions
(`renew_attempt_lease`), signals immediate worker abort on definitive lease
loss or sustained DB outage past the self-fence deadline, and shuts down
cleanly with worker completion or failure.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import threading
import time
from typing import Any, Callable, Mapping

from engine.database import (
    TRANSIENT_ERRORS,
    CommitOutcomeUnknownError,
    DatabaseUnavailableError,
    IdempotencyIdentityMismatchError,
    StaleAttemptError,
)
from engine.db_config import DatabaseConfigError, validate_project_id
from engine.sanitization import configure_sanitized_logger, sanitize_log_text

DEFAULT_LOCK_TTL_SECONDS = 180
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
MAX_DEFAULT_SELF_FENCE_MARGIN_SECONDS = 10.0


class LeaseConfigurationError(ValueError):
    """Raised when lease TTL or heartbeat interval configuration is unsafe."""


class LeaseLostError(RuntimeError):
    """Raised when a worker definitively loses or self-fences its lease."""

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str = "",
        attempt_id: str = "",
        cause: BaseException | None = None,
    ) -> None:
        self.idempotency_key = idempotency_key
        self.attempt_id = attempt_id
        self.cause = cause
        super().__init__(sanitize_log_text(message))


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _parse_int_env(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise LeaseConfigurationError(f"{name} must be an integer.") from exc


def _parse_float_env(raw: str | None, default: float | None, name: str) -> float | None:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise LeaseConfigurationError(f"{name} must be a finite number.") from exc


@dataclass(frozen=True)
class LeaseConfig:
    """Validated lease TTL, renewal interval, and self-fencing safety margins."""

    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    self_fence_margin_seconds: float | None = None

    def __post_init__(self) -> None:
        if _is_bool(self.lock_ttl_seconds) or not isinstance(self.lock_ttl_seconds, int):
            raise LeaseConfigurationError("lock_ttl_seconds must be a positive integer.")
        if self.lock_ttl_seconds < 1:
            raise LeaseConfigurationError("lock_ttl_seconds must be >= 1 second.")

        if _is_bool(self.heartbeat_interval_seconds) or not isinstance(
            self.heartbeat_interval_seconds, (int, float)
        ):
            raise LeaseConfigurationError("heartbeat_interval_seconds must be a positive number.")
        interval = float(self.heartbeat_interval_seconds)
        if not math.isfinite(interval) or interval <= 0.0:
            raise LeaseConfigurationError("heartbeat_interval_seconds must be > 0.")
        max_safe_interval = self.lock_ttl_seconds / 2.0
        if interval >= max_safe_interval:
            raise LeaseConfigurationError(
                f"heartbeat_interval_seconds ({interval}) must be strictly less than "
                f"half of lock_ttl_seconds ({max_safe_interval}) so renewals occur before expiry."
            )

        margin = self.effective_self_fence_margin_seconds
        if _is_bool(margin) or not isinstance(margin, (int, float)) or not math.isfinite(margin):
            raise LeaseConfigurationError("self_fence_margin_seconds must be a finite number.")
        if margin < 0.0 or margin >= float(self.lock_ttl_seconds):
            raise LeaseConfigurationError(
                "self_fence_margin_seconds must satisfy 0 <= margin < lock_ttl_seconds."
            )
        if interval >= float(self.lock_ttl_seconds) - float(margin):
            raise LeaseConfigurationError(
                "heartbeat_interval_seconds must be strictly less than "
                "(lock_ttl_seconds - self_fence_margin_seconds)."
            )

    @property
    def effective_self_fence_margin_seconds(self) -> float:
        if self.self_fence_margin_seconds is None:
            return min(MAX_DEFAULT_SELF_FENCE_MARGIN_SECONDS, self.lock_ttl_seconds * 0.25)
        return float(self.self_fence_margin_seconds)

    @property
    def self_fence_deadline_seconds(self) -> float:
        return float(self.lock_ttl_seconds) - self.effective_self_fence_margin_seconds

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LeaseConfig":
        """Build validated LeaseConfig from UTDA_* environment variables."""
        env = os.environ if environ is None else environ
        ttl = _parse_int_env(
            env.get("UTDA_LOCK_TTL_SECONDS"),
            DEFAULT_LOCK_TTL_SECONDS,
            "UTDA_LOCK_TTL_SECONDS",
        )
        raw_interval = env.get("UTDA_HEARTBEAT_INTERVAL_SECONDS")
        default_interval = round(ttl / 3.0, 3)
        interval = _parse_float_env(
            raw_interval, default_interval, "UTDA_HEARTBEAT_INTERVAL_SECONDS"
        )
        assert interval is not None
        margin = _parse_float_env(
            env.get("UTDA_LEASE_SELF_FENCE_MARGIN_SECONDS"),
            None,
            "UTDA_LEASE_SELF_FENCE_MARGIN_SECONDS",
        )
        return cls(
            lock_ttl_seconds=ttl,
            heartbeat_interval_seconds=interval,
            self_fence_margin_seconds=margin,
        )


class LeaseHeartbeat:
    """Coordinated background lease keeper using independent short DB transactions.

    Never shares a database connection with the worker thread. Immediately sets
    `abort_event` if lease ownership is definitively lost (`StaleAttemptError`,
    project mismatch) or if transient DB errors prevent renewal before the
    self-fence deadline ahead of `expires_at`.
    """

    def __init__(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        attempt_id: str,
        expected_fence_version: int | None,
        renew_fn: Callable[..., Mapping[str, Any]],
        config: LeaseConfig | None = None,
        on_lease_lost: Callable[[BaseException], None] | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.project_id = validate_project_id(project_id)
        if not idempotency_key or not str(idempotency_key).strip():
            raise ValueError("idempotency_key is required for LeaseHeartbeat.")
        if not attempt_id or not str(attempt_id).strip():
            raise ValueError("attempt_id is required for LeaseHeartbeat.")
        if expected_fence_version is not None:
            if _is_bool(expected_fence_version) or int(expected_fence_version) < 0:
                raise ValueError("expected_fence_version must be a non-negative integer.")

        self.idempotency_key = str(idempotency_key)
        self.attempt_id = str(attempt_id)
        self.expected_fence_version = (
            None if expected_fence_version is None else int(expected_fence_version)
        )
        self._renew_fn = renew_fn
        self.config = config or LeaseConfig()
        self._on_lease_lost = on_lease_lost
        self._clock = clock_fn
        self.logger = configure_sanitized_logger("utda.lease_heartbeat")

        self._stop_event = threading.Event()
        self._abort_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self._loss_reason: BaseException | None = None
        self._renewal_count = 0
        self._transient_error_count = 0
        self._last_renewed_at = self._clock()

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    @property
    def abort_event(self) -> threading.Event:
        return self._abort_event

    @property
    def lost(self) -> bool:
        return self._abort_event.is_set()

    @property
    def loss_reason(self) -> BaseException | None:
        with self._lock:
            return self._loss_reason

    @property
    def renewal_count(self) -> int:
        with self._lock:
            return self._renewal_count

    @property
    def transient_error_count(self) -> int:
        with self._lock:
            return self._transient_error_count

    @property
    def last_renewed_at(self) -> float:
        with self._lock:
            return self._last_renewed_at

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> "LeaseHeartbeat":
        """Start the background heartbeat thread safely (idempotent)."""
        with self._lock:
            if self._stopped:
                raise RuntimeError("Cannot restart a stopped LeaseHeartbeat.")
            if self._started:
                return self
            self._last_renewed_at = self._clock()
            self._thread = threading.Thread(
                target=self._run,
                name=f"utda-heartbeat-{self.attempt_id[:8]}",
                daemon=True,
            )
            self._started = True
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Signal heartbeat shutdown and wait cleanly for thread termination."""
        self._stop_event.set()
        with self._lock:
            self._stopped = True
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.logger.warning(
                    "LeaseHeartbeat thread for attempt '%s' did not exit within %.2fs.",
                    sanitize_log_text(self.attempt_id),
                    timeout,
                )

    def __enter__(self) -> "LeaseHeartbeat":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    def raise_if_lost(self) -> None:
        """Raise LeaseLostError immediately if lease ownership was lost or self-fenced."""
        if not self._abort_event.is_set():
            return
        reason = self.loss_reason
        if isinstance(reason, LeaseLostError):
            raise reason
        msg = (
            f"Worker lost lease for project '{self.project_id}', attempt '{self.attempt_id}': "
            f"{sanitize_log_text(str(reason))}"
        )
        raise LeaseLostError(
            msg,
            idempotency_key=self.idempotency_key,
            attempt_id=self.attempt_id,
            cause=reason,
        ) from reason

    def wait_or_raise(self, seconds: float) -> None:
        """Sleep up to `seconds`, aborting immediately if lease is lost."""
        if seconds <= 0:
            self.raise_if_lost()
            return
        self.raise_if_lost()
        if self._abort_event.wait(timeout=seconds):
            self.raise_if_lost()

    def _mark_lost(self, exc: BaseException) -> None:
        callback: Callable[[BaseException], None] | None = None
        first_loss = False
        with self._lock:
            if not self._abort_event.is_set():
                first_loss = True
                self._loss_reason = exc
                self._abort_event.set()
                callback = self._on_lease_lost
        if first_loss:
            self.logger.error(
                "HEARTBEAT_LEASE_LOST project=%s attempt=%s reason=%s",
                self.project_id,
                sanitize_log_text(self.attempt_id),
                sanitize_log_text(str(exc)),
            )
            if callback is not None:
                try:
                    callback(exc)
                except Exception as cb_err:
                    self.logger.warning(
                        "on_lease_lost callback raised: %s",
                        sanitize_log_text(str(cb_err)),
                    )

    def _run(self) -> None:
        in_transient_backoff = False
        backoff_attempt = 0
        while not self._stop_event.is_set():
            if in_transient_backoff:
                with self._lock:
                    last_ok = self._last_renewed_at
                remaining = (last_ok + self.config.self_fence_deadline_seconds) - self._clock()
                if remaining <= 0.0:
                    self._mark_lost(
                        LeaseLostError(
                            f"Lease self-fenced after DB outage exceeded deadline ({self.config.self_fence_deadline_seconds:.2f}s).",
                            idempotency_key=self.idempotency_key,
                            attempt_id=self.attempt_id,
                        )
                    )
                    break
                retry_delay = min(
                    0.05 * (2**backoff_attempt),
                    max(0.01, remaining * 0.5),
                    self.config.heartbeat_interval_seconds,
                )
                wait_seconds = retry_delay
            else:
                wait_seconds = self.config.heartbeat_interval_seconds

            if self._stop_event.wait(timeout=wait_seconds):
                break

            try:
                res = self._renew_fn(
                    project_id=self.project_id,
                    idempotency_key=self.idempotency_key,
                    attempt_id=self.attempt_id,
                    expected_fence_version=self.expected_fence_version,
                    extend_ttl_seconds=self.config.lock_ttl_seconds,
                )
                if not isinstance(res, Mapping) or not res.get("renewed"):
                    raise StaleAttemptError(
                        f"Lease renewal did not confirm active ownership for attempt '{self.attempt_id}'."
                    )
                with self._lock:
                    self._last_renewed_at = self._clock()
                    self._renewal_count += 1
                in_transient_backoff = False
                backoff_attempt = 0
            except (
                StaleAttemptError,
                IdempotencyIdentityMismatchError,
                DatabaseConfigError,
                ValueError,
            ) as exc:
                self._mark_lost(exc)
                break
            except TRANSIENT_ERRORS + (DatabaseUnavailableError, CommitOutcomeUnknownError) as exc:
                with self._lock:
                    self._transient_error_count += 1
                    last_ok = self._last_renewed_at
                elapsed = self._clock() - last_ok
                if elapsed >= self.config.self_fence_deadline_seconds:
                    self._mark_lost(
                        LeaseLostError(
                            f"Transient DB errors persisted for {elapsed:.2f}s "
                            f"(self-fence deadline {self.config.self_fence_deadline_seconds:.2f}s): "
                            f"{sanitize_log_text(str(exc))}",
                            idempotency_key=self.idempotency_key,
                            attempt_id=self.attempt_id,
                            cause=exc,
                        )
                    )
                    break
                in_transient_backoff = True
                backoff_attempt = min(backoff_attempt + 1, 6)
                self.logger.warning(
                    "Transient lease renewal failure for attempt '%s' (%.2fs elapsed): %s",
                    sanitize_log_text(self.attempt_id),
                    elapsed,
                    sanitize_log_text(str(exc)),
                )
            except Exception as exc:
                # Fail closed on unexpected renewal errors rather than continuing blindly.
                self._mark_lost(
                    LeaseLostError(
                        f"Unexpected lease renewal error for attempt '{self.attempt_id}': "
                        f"{sanitize_log_text(str(exc))}",
                        idempotency_key=self.idempotency_key,
                        attempt_id=self.attempt_id,
                        cause=exc,
                    )
                )
                break
