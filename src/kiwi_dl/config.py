"""Application configuration: paths, defaults, persisted settings."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .utils.atomic import atomic_write_text
from .utils.corruption import existing_backup_for, preserve_corrupt_file
from .utils.filelock import FileLock, FileLockError

APP_NAME = "kiwi-dl"

log = logging.getLogger(__name__)


class ConfigLockError(Exception):
    """Raised when the config file lock cannot be acquired in time."""


class ConfigCorruptionError(Exception):
    """Raised when the config file is malformed or has an invalid root.

    A corrupt config is preserved byte-for-byte (never overwritten) and every
    mutation is blocked until the operator resolves it with
    `kiwi-dl config recover --reset` or by restoring a good copy.
    """


# Nullable (None-default) fields accept an explicit "null"/"none" to unset.
_NULL_TOKENS = {"null", "none"}

# Settings that existed but never worked; keeping them silently would lie to
# users, so setting them is rejected with the reason and old persisted values
# are ignored with a warning.
DEPRECATED_CONFIG_KEYS = {
    "chunk_size_kb": "MEGA chunk sizes are protocol-defined; this setting never had an effect "
    "and was removed.",
    "smart_proxy_autorefresh_minutes": "not implemented; the proxy pool is reloaded on every "
    "command run.",
    "smart_proxy_timeout_seconds": "not implemented.",
    "smart_proxy_random": "not implemented; the pool always picks randomly, weighted by health.",
}

# Deterministic warn-once bookkeeping: old config files written by other
# tools may carry deprecated or unknown keys on every load;
# each key is warned about at most once per process, never per access.
_warned_config_keys: set[str] = set()

# Fields whose dataclass default is None still have an expected value type.
_OPTIONAL_STR_KEYS = {
    "smart_proxy_url",
    "run_command",
    "all_finished_command",
}

# Values of these keys may contain secrets and are never echoed in warnings
# or `config show`/`config get`. (`smart_proxy_url` can carry credentials too;
# `display_value` scrubs every string.)
_SECRET_CONFIG_KEYS: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned_config_keys:
        _warned_config_keys.add(key)
        log.warning("%s", message)


@dataclass
class Config:
    """User-tunable settings."""

    download_path: str = ""  # Defaults to <project>/Output
    max_workers: int = 8
    max_parallel_downloads: int = 6  # Number of files downloading simultaneously
    speed_limit_kbps: float = 0  # Aggregate per-command download cap; 0 = unlimited
    verify_integrity: bool = True
    smart_proxy_enabled: bool = False
    smart_proxy_url: str | None = None
    # How downloads use the pool once smart proxy is enabled (MegaBasterd's
    # SmartProxy, see proxy/quota_router.py):
    #   "on-509" - direct first; a chunk 509 sends every download's chunks
    #              through the pool for `smart_proxy_window_seconds`, then
    #              direct is tried again;
    #   "always" - every chunk through the pool from the first byte;
    #   "off"    - never use the pool.
    smart_proxy_mode: str = "on-509"
    smart_proxy_window_seconds: int = 3600
    smart_proxy_ban_seconds: int = 300  # a failing proxy's ban; 0 = for the rest of the run
    timeout_seconds: int = 60
    auto_resume: bool = True
    keep_state_files_on_error: bool = True
    log_level: str = "WARNING"
    log_to_file: bool = True
    log_max_bytes: int = 5_000_000
    log_backups: int = 5
    user_agent: str = ""  # Empty = "kiwi-dl/<version>"

    # Quota recovery: wait this many seconds when MEGA returns EOVERQUOTA/-17
    quota_wait_seconds: int = 3600
    quota_max_wait_loops: int = 24  # ~24h with the default
    # A chunk 509 that no proxy can take over may be waited out up to this
    # many seconds in total per command; 0 stops at once with the reset time.
    quota_budget_seconds: int = 0

    # Post-transfer hook: run this command (with the downloaded path appended
    # as exactly one argument) after every successful download.
    run_command: str | None = None

    # Batch-completion hook: run this command ONCE per download command, after
    # every item has been attempted, with `download <succeeded> <failed>`
    # appended as three arguments.
    all_finished_command: str | None = None

    # Smart proxy extras
    force_smart_proxy: bool = False  # Refuse direct connections


# Range/format rules applied on load and on `config set`. Each entry maps the
# key to (predicate, human-readable requirement).
_VALIDATORS: dict[str, tuple] = {
    "max_workers": (lambda v: 1 <= v <= 64, "an integer between 1 and 64"),
    "max_parallel_downloads": (lambda v: 1 <= v <= 32, "an integer between 1 and 32"),
    "speed_limit_kbps": (lambda v: v >= 0, "a non-negative number"),
    "timeout_seconds": (lambda v: v > 0, "a positive integer"),
    "quota_wait_seconds": (lambda v: v >= 0, "a non-negative integer"),
    "quota_max_wait_loops": (lambda v: v >= 0, "a non-negative integer"),
    "quota_budget_seconds": (lambda v: v >= 0, "a non-negative integer"),
    "smart_proxy_mode": (lambda v: v in ("off", "on-509", "always"), "one of off/on-509/always"),
    "smart_proxy_window_seconds": (
        lambda v: 60 <= v <= 86400,
        "an integer between 60 and 86400",
    ),
    "smart_proxy_ban_seconds": (lambda v: v >= 0, "a non-negative integer (0 = rest of the run)"),
    "log_max_bytes": (lambda v: v >= 0, "a non-negative integer"),
    "log_backups": (lambda v: v >= 0, "a non-negative integer"),
    "log_level": (
        lambda v: str(v).upper() in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
        "one of DEBUG/INFO/WARNING/ERROR/CRITICAL",
    ),
}


def validate_config_value(key: str, value) -> None:
    """Raise ValueError when `value` is invalid for `key`.

    Secret-carrying keys are validated without echoing the value.
    """
    if key in _OPTIONAL_STR_KEYS and value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null (got type {type(value).__name__})")
    rule = _VALIDATORS.get(key)
    if rule is None:
        return
    predicate, requirement = rule
    try:
        ok = bool(predicate(value))
    except TypeError:
        ok = False
    if not ok:
        shown = "<redacted>" if key in _SECRET_CONFIG_KEYS else repr(value)
        raise ValueError(f"{key} must be {requirement} (got {shown})")


def validate_config(cfg: Config) -> list[str]:
    """Validate a loaded Config in place; invalid values fall back to defaults.

    Returns human-readable warnings (also logged) instead of crashing deep in
    runtime code when a hand-edited config file holds a bad value.
    """
    warnings: list[str] = []
    defaults = Config()
    for f in fields(Config):
        current = getattr(cfg, f.name)
        default = getattr(defaults, f.name)
        # Optional (None-default) fields still have an expected type; never
        # echo their values (they may carry secrets).
        if f.name in _OPTIONAL_STR_KEYS:
            if current is not None and not isinstance(current, str):
                warnings.append(
                    f"config: {f.name} must be a string or null "
                    f"(got type {type(current).__name__}); using default"
                )
                setattr(cfg, f.name, default)
            continue
        # Type check against the default's runtime type (None-able keys skip).
        if default is not None and current is not None:
            expected = type(default)
            if expected is float and isinstance(current, int) and not isinstance(current, bool):
                current = float(current)
                setattr(cfg, f.name, current)
            elif not isinstance(current, expected) or (
                expected in (int, float) and isinstance(current, bool)
            ):
                warnings.append(
                    f"config: {f.name} has invalid type {type(current).__name__}; "
                    f"using default {default!r}"
                )
                setattr(cfg, f.name, default)
                continue
        try:
            validate_config_value(f.name, getattr(cfg, f.name))
        except ValueError as exc:
            warnings.append(f"config: {exc}; using default {default!r}")
            setattr(cfg, f.name, default)
    for message in warnings:
        log.warning("%s", message)
    return warnings


def display_value(key: str, value):
    """Redact secret config values for `config show` / `config get`.

    Every string goes through the shared sanitizer rather than being printed
    verbatim, so `smart_proxy_url` never shows its `scheme://user:pass@host` -
    the same value `proxy list` redacts one command over.
    """
    from .utils.redaction import redact_text

    if value is None:
        return None
    if isinstance(value, str):
        return redact_text(value)
    return value


def config_dir() -> Path:
    return user_dir() / "Config"


def data_dir() -> Path:
    return user_dir() / "Data"


def log_dir() -> Path:
    env_log = os.environ.get("KIWI_DL_LOG_DIR")
    if env_log:
        return Path(env_log).expanduser().resolve()
    return user_dir() / "Logs"


def config_file() -> Path:
    return config_dir() / "config.json"


def project_root() -> Path:
    """Return the source project root when the script launcher provides it."""
    env_root = os.environ.get("MEGABASTERD_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "Run.ps1").is_file():
            return parent
    return Path.cwd().resolve()


def user_dir() -> Path:
    """Directory for local user data owned by the source checkout."""
    env_user = os.environ.get("KIWI_DL_HOME")
    if env_user:
        return Path(env_user).expanduser().resolve()
    return project_root() / "User"


def default_download_dir() -> Path:
    """Default download directory used when no config override is present."""
    return project_root() / "Output"


class ConfigStore:
    """Load and save the Config dataclass from JSON on disk.

    Concurrency contract (mirrors QueueManager): targeted mutations
    (`set`/`unset`) run under an instance mutex plus a cross-process file
    lock, reload the newest on-disk config first, apply only the requested
    change, and persist atomically via a unique fsync'd temp file. This
    prevents two processes from losing each other's updates or
    colliding on a shared temp file.
    """

    def __init__(self, path: Path | None = None, lock_timeout: float = 10.0):
        self.path = path or config_file()
        self._config: Config | None = None
        self.lock_timeout = lock_timeout
        self._mutex = threading.RLock()
        self._file_lock = FileLock(
            self.path.parent / (self.path.name + ".lock"),
            message=(
                f"Could not lock the config file within {lock_timeout:.0f}s; "
                "another process is updating it. Retry shortly."
            ),
        )
        self._lock_depth = 0
        # Set by `load()` when the on-disk config is malformed/invalid-root.
        self._corrupt = False
        self._corrupt_reason = ""
        self._corrupt_backup: Path | None = None

    @contextlib.contextmanager
    def _locked(self):
        """Hold the instance mutex plus the cross-process file lock (once)."""
        self._mutex.acquire()
        try:
            if self._lock_depth == 0:
                try:
                    self._file_lock.acquire(timeout=self.lock_timeout)
                except FileLockError as exc:
                    raise ConfigLockError(str(exc)) from None
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    self._file_lock.release()
        finally:
            self._mutex.release()

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = self.load()
        return self._config

    @property
    def is_corrupt(self) -> bool:
        return self._corrupt

    @property
    def corruption_reason(self) -> str:
        return self._corrupt_reason

    def _ensure_writable(self) -> None:
        """Block mutations while the on-disk config is corrupt."""
        if self._corrupt:
            raise ConfigCorruptionError(self._corrupt_reason)

    def _defaults(self) -> Config:
        cfg = Config()
        cfg.download_path = str(default_download_dir())
        return cfg

    @property
    def corrupt_backup(self) -> Path | None:
        """The backup for the CURRENT corruption episode, when one exists.

        Deliberately not "the newest .corrupt.* file": after a previous
        recovery an older, unrelated backup would otherwise be reported as if
        it held the bytes just preserved.
        """
        return self._corrupt_backup

    def _mark_corrupt(self, reason: str, data: bytes | None) -> None:
        """Flag the on-disk config as corrupt, preserve it, and back it up once.

        The original is NEVER overwritten. A single timestamped copy is made
        the first time corruption is seen, under the cross-process file lock so
        two processes cannot duplicate the backup. The reason is sanitized: it
        must never carry a value from the corrupt file.
        """
        from .utils.redaction import redact_text

        self._corrupt = True
        self._corrupt_backup = None
        self._corrupt_reason = redact_text(
            f"The config file is corrupt and was preserved untouched: {reason}. "
            f"A backup was saved next to {self.path.name}; run "
            "`kiwi-dl config recover --reset` to start from defaults, or restore a good copy."
        )
        if data is None:
            self._corrupt_backup = None
            log.warning("%s", self._corrupt_reason)
            return
        try:
            with self._locked():
                # Under the same lock used for recovery, so a concurrent
                # detector cannot duplicate or lose this episode's backup.
                self._corrupt_backup = preserve_corrupt_file(self.path, data)
        except ConfigLockError:
            self._corrupt_backup = existing_backup_for(self.path, data)
            log.warning("Could not lock the config file to back up the corrupt copy.")
        if self._corrupt_backup is not None:
            self._corrupt_reason = redact_text(
                f"The config file is corrupt and was preserved untouched: {reason}. "
                f"A backup was saved as {self._corrupt_backup.name}; run "
                "`kiwi-dl config recover --reset` to start from defaults, or restore a good copy."
            )
        else:
            # Never claim a backup that was not written.
            self._corrupt_reason = redact_text(
                f"The config file is corrupt and was preserved untouched: {reason}. "
                "A backup could NOT be written; fix the permissions or move the file "
                "aside, then run `kiwi-dl config recover --reset`."
            )
        log.warning("%s", self._corrupt_reason)

    def load(self) -> Config:
        self._corrupt = False
        self._corrupt_reason = ""
        self._corrupt_backup = None
        if not self.path.exists():
            return self._defaults()

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self._mark_corrupt(f"unreadable ({type(exc).__name__})", None)
            return self._defaults()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._mark_corrupt(f"not valid UTF-8 JSON ({type(exc).__name__})", raw)
            return self._defaults()
        if not isinstance(data, dict):
            self._mark_corrupt(f"root is {type(data).__name__}, expected an object", raw)
            return self._defaults()

        cfg = Config()
        for key, value in data.items():
            if key in DEPRECATED_CONFIG_KEYS:
                _warn_once(
                    key,
                    f"config: ignoring deprecated key {key} ({DEPRECATED_CONFIG_KEYS[key]}); "
                    "run `kiwi-dl config migrate` to remove it",
                )
                continue
            if hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                _warn_once(key, f"config: ignoring unknown key {key}")
        validate_config(cfg)
        if cfg.user_agent == "MegaBasterd-CLI/1.0":
            # Old persisted default carried a hard-coded version; empty means
            # "derive from the installed package version".
            cfg.user_agent = ""
        if not cfg.download_path:
            cfg.download_path = str(default_download_dir())
        return cfg

    def save(self) -> None:
        """Persist the in-memory config, refusing to clobber a corrupt file.

        The on-disk file is re-read HERE, under the lock, before the writable
        guard. A freshly constructed store has never loaded, so `_corrupt`
        would still be False and reading `self.config` would lazily produce
        defaults that then overwrote the corrupt original. Re-reading inside
        the same lock hold that performs the replace also closes the window
        where another process corrupts or updates the file after validation.

        `load()` deliberately does not touch `self._config`, so a pending
        mutation from set/unset/reset survives this refresh.
        """
        with self._locked():
            self.load()
            self._ensure_writable()
            self._write_locked()

    def _write_locked(self) -> None:
        """Serialize and atomically replace the file. Caller holds the lock.

        Split out of `save()` so explicit recovery can write valid defaults
        over a file that is still flagged corrupt, without giving any other
        path a way to skip the guard.
        """
        # Serialize BEFORE touching the filesystem so a serialization failure
        # leaves the original file untouched and drops no temp file.
        payload = json.dumps(asdict(self.config), indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The unique temp file, fsync, bounded PermissionError retry (antivirus
        # holding config.json open on Windows) and the temp cleanup that keeps
        # a failed `config set` from orphaning config.json.*.tmp all live in the
        # shared helper.
        atomic_write_text(self.path, payload)

    def _coerce(self, key: str, value):
        """Turn a CLI string into the field's typed value; parse null/none."""
        if key in DEPRECATED_CONFIG_KEYS:
            raise ValueError(f"{key} is deprecated: {DEPRECATED_CONFIG_KEYS[key]}")
        if not hasattr(self.config, key):
            raise KeyError(f"Unknown config key: {key}")
        # Nullable fields: an explicit null/none unsets to JSON null; any other
        # string is kept verbatim (so "null-value" or a URL stays a string).
        if key in _OPTIONAL_STR_KEYS and isinstance(value, str):
            if value.strip().lower() in _NULL_TOKENS:
                return None
            return value
        current = getattr(self.config, key)
        if isinstance(current, bool):
            lowered = str(value).strip().lower()
            if lowered in ("1", "true", "yes", "y", "on"):
                return True
            if lowered in ("0", "false", "no", "n", "off"):
                return False
            raise ValueError("Expected a boolean value: true/false, yes/no, on/off, or 1/0")
        if isinstance(current, dict):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("Expected a JSON object")
            return parsed
        if isinstance(current, int):
            return int(value)
        if isinstance(current, float):
            return float(value)
        return value

    def set(self, key: str, value) -> None:
        with self._locked():
            self._config = self.load()  # reload newest before mutating
            typed = self._coerce(key, value)
            validate_config_value(key, typed)
            setattr(self._config, key, typed)
            self.save()

    def unset(self, key: str) -> None:
        """Reset a nullable field to JSON null (fails for non-nullable keys)."""
        with self._locked():
            self._config = self.load()
            if key in DEPRECATED_CONFIG_KEYS:
                raise ValueError(f"{key} is deprecated: {DEPRECATED_CONFIG_KEYS[key]}")
            if not hasattr(self._config, key):
                raise KeyError(f"Unknown config key: {key}")
            if key not in _OPTIONAL_STR_KEYS:
                raise ValueError(
                    f"{key} is not a nullable field; use `config reset` to restore defaults"
                )
            setattr(self._config, key, None)
            self.save()

    def raw_keys(self) -> frozenset[str]:
        """Keys present in the persisted JSON file (empty when absent)."""
        if not self.path.exists():
            return frozenset()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return frozenset()
        return frozenset(data.keys()) if isinstance(data, dict) else frozenset()

    def migrate(self) -> list[str]:
        """Normalize the persisted config: drop deprecated/unknown keys.

        Returns the removed keys so callers can report them.
        Values of known keys are validated/kept by the normal load path.
        """
        with self._locked():
            self._config = self.load()  # detects corruption before any rewrite
            self._ensure_writable()
            stale = sorted(
                key
                for key in self.raw_keys()
                if key in DEPRECATED_CONFIG_KEYS or not hasattr(Config(), key)
            )
            self.save()
            return stale

    def reset(self) -> None:
        with self._locked():
            # Load first: a corrupt file must be detected (and preserved)
            # BEFORE reset would otherwise overwrite it with defaults.
            self.load()
            self._ensure_writable()
            self._config = self._defaults()
            self.save()

    def recover(self) -> Path | None:
        """Explicit recovery from corruption: write a fresh default config.

        The corrupt original is preserved as a backup (made on load); this only
        clears the corruption flag and writes valid defaults, so it must be
        driven by a deliberate user action. Returns the preserved backup path.
        """
        with self._locked():
            self.load()  # ensures the corrupt original is backed up first
            backup = self.corrupt_backup
            self._corrupt = False
            self._corrupt_reason = ""
            self._config = self._defaults()
            # Write directly: `save()` would re-read and re-flag the corruption
            # we are deliberately recovering from, still under this same lock.
            self._write_locked()
            return backup
