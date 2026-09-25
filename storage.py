"""Durable SQLite state storage with field-level secret encryption.

The application still works with one in-memory dictionary, but persistence is a
single SQLite document. This keeps the existing route layer compatible while
giving deployments WAL journaling, atomic commits and portable database
backups. Sensitive values are encrypted before they enter SQLite.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


logger = logging.getLogger(__name__)
_SECRET_PREFIX = "enc:v1:"
_SECRET_KEYS = {
    "password",
    "private_key",
    "token",
    "secret",
    "api_key",
    "remnawave_api_key",
    "socks5_password",
    "cert_text",
    "key_text",
}


class StorageError(RuntimeError):
    """Raised when persistent panel state cannot be read safely."""


class SQLiteStateStore:
    """Persist the panel state in one encrypted SQLite record.

    Keeping the document shape avoids a risky big-bang ORM migration while all
    existing routes are moved incrementally to repository methods. SQLite's
    transaction and WAL semantics still prevent torn writes and make backups
    consistent.
    """

    def __init__(self, database_path: str, legacy_json_path: str, master_key: str = "", require_encryption: bool = False):
        self.database_path = Path(database_path)
        self.legacy_json_path = Path(legacy_json_path)
        self.require_encryption = require_encryption
        self._lock = threading.RLock()
        self._initialised = False
        self._key = hashlib.sha256(master_key.encode("utf-8")).digest() if master_key else None
        self.key_fingerprint = hashlib.sha256(self._key).hexdigest()[:16] if self._key else ""
        if require_encryption and not self._key:
            raise StorageError("PANEL_MASTER_KEY is required when PANEL_REQUIRE_ENCRYPTION is enabled")
        if not self._key:
            logger.warning("PANEL_MASTER_KEY is not set; secrets are not encrypted at rest")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _initialise(self) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS panel_state ("
                    "id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                row = connection.execute("SELECT payload FROM panel_state WHERE id = 1").fetchone()
                if row is None:
                    legacy_data = self._read_legacy_data()
                    payload = self._seal_value(legacy_data)
                    connection.execute(
                        "INSERT INTO panel_state (id, payload, updated_at) VALUES (1, ?, ?)",
                        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), self._now()),
                    )
                    if legacy_data:
                        self._preserve_encrypted_legacy_copy(payload)
                if self.key_fingerprint:
                    connection.execute(
                        "INSERT INTO metadata (key, value) VALUES ('master_key_fingerprint', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (self.key_fingerprint,),
                    )
                connection.commit()
            finally:
                connection.close()
            self._initialised = True

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _read_legacy_data(self) -> dict[str, Any]:
        if not self.legacy_json_path.exists() or not self.legacy_json_path.stat().st_size:
            return {}
        try:
            with self.legacy_json_path.open("r", encoding="utf-8") as source:
                data = json.load(source)
            if not isinstance(data, dict):
                raise StorageError("Legacy data.json must contain an object")
            return data
        except json.JSONDecodeError as error:
            raise StorageError(f"Cannot migrate invalid legacy JSON: {error}") from error

    def _preserve_encrypted_legacy_copy(self, sealed_data: dict[str, Any]) -> None:
        """Keep a migration snapshot without leaving plaintext credentials behind."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.database_path.parent / f"data.json.pre-sqlite-{stamp}.encrypted.json"
        content = json.dumps(sealed_data, ensure_ascii=False, indent=2).encode("utf-8")
        self._atomic_write(backup, content)
        # The legacy file can be a Docker bind-mounted single file. Linux does
        # not allow replacing that mount with os.replace(), so fall back to an
        # in-place, fsynced rewrite while retaining atomic writes everywhere
        # else.
        try:
            self._atomic_write(self.legacy_json_path, content)
        except OSError as error:
            if error.errno != 16:  # EBUSY
                raise
            with self.legacy_json_path.open("wb") as legacy:
                legacy.write(content)
                legacy.flush()
                os.fsync(legacy.fileno())
            try:
                os.chmod(self.legacy_json_path, 0o600)
            except OSError:
                pass

    def _encrypt(self, value: str) -> str:
        if not self._key:
            return value
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, value.encode("utf-8"), b"amnezia-web-panel:v1")
        return _SECRET_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value.startswith(_SECRET_PREFIX):
            return value
        if not self._key:
            raise StorageError("Encrypted secrets require PANEL_MASTER_KEY")
        try:
            raw = base64.urlsafe_b64decode(value[len(_SECRET_PREFIX):].encode("ascii"))
            return AESGCM(self._key).decrypt(raw[:12], raw[12:], b"amnezia-web-panel:v1").decode("utf-8")
        except Exception as error:
            raise StorageError("Could not decrypt stored panel secrets; check PANEL_MASTER_KEY") from error

    def _seal_value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {item_key: self._seal_value(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [self._seal_value(item, key) for item in value]
        if isinstance(value, str) and key in _SECRET_KEYS and value and not value.startswith(_SECRET_PREFIX):
            return self._encrypt(value)
        return value

    def _unseal_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {item_key: self._unseal_value(item_value) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [self._unseal_value(item) for item in value]
        if isinstance(value, str) and value.startswith(_SECRET_PREFIX):
            return self._decrypt(value)
        return value

    def load(self) -> dict[str, Any]:
        self._initialise()
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("SELECT payload FROM panel_state WHERE id = 1").fetchone()
                if not row:
                    return {}
                payload = json.loads(row[0])
                data = self._unseal_value(payload)
                if not isinstance(data, dict):
                    raise StorageError("Stored panel state must be an object")
                return data
            finally:
                connection.close()

    def save(self, data: dict[str, Any]) -> None:
        self._initialise()
        sealed = self._seal_value(data)
        payload = json.dumps(sealed, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE panel_state SET payload = ?, updated_at = ? WHERE id = 1",
                    (payload, self._now()),
                )
                connection.commit()
            finally:
                connection.close()

    def _plaintext_secret_paths(self, value: Any, path: str = "", key: str = "") -> list[str]:
        if isinstance(value, dict):
            return [
                found
                for item_key, item_value in value.items()
                for found in self._plaintext_secret_paths(item_value, f"{path}.{item_key}" if path else item_key, item_key)
            ]
        if isinstance(value, list):
            return [found for index, item in enumerate(value) for found in self._plaintext_secret_paths(item, f"{path}[{index}]", key)]
        if isinstance(value, str) and key in _SECRET_KEYS and value and not value.startswith(_SECRET_PREFIX):
            return [path]
        return []

    def plaintext_secret_paths(self) -> list[str]:
        """Paths of secret fields stored unencrypted in the persisted payload.

        A database created before PANEL_MASTER_KEY was configured keeps its old
        plaintext values until they are rewritten, and export_database() copies
        the payload verbatim.
        """
        self._initialise()
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute("SELECT payload FROM panel_state WHERE id = 1").fetchone()
            finally:
                connection.close()
        return self._plaintext_secret_paths(json.loads(row[0])) if row else []

    def reseal(self) -> None:
        """Rewrite the stored state so every secret field is encrypted."""
        if not self._key:
            raise StorageError("PANEL_MASTER_KEY is required to encrypt stored secrets")
        with self._lock:
            self.save(self.load())

    def export_database(self, compact: bool = False) -> bytes:
        """Return a consistent SQLite snapshot, including WAL changes.

        compact=True rebuilds the copy with VACUUM so free pages that still
        hold earlier payload versions (e.g. pre-encryption plaintext) are not
        shipped along with the current state.
        """
        self._initialise()
        with self._lock:
            fd, snapshot_path = tempfile.mkstemp(prefix="amnezia-panel-backup-", suffix=".db")
            os.close(fd)
            try:
                source = self._connect()
                destination = sqlite3.connect(snapshot_path)
                try:
                    source.backup(destination)
                    if compact:
                        destination.execute("VACUUM")
                finally:
                    destination.close()
                    source.close()
                with open(snapshot_path, "rb") as snapshot:
                    return snapshot.read()
            finally:
                if os.path.exists(snapshot_path):
                    os.unlink(snapshot_path)

    def restore_database(self, content: bytes) -> None:
        """Validate and atomically replace the active database from a backup."""
        self._initialise()
        with self._lock:
            fd, temporary_path = tempfile.mkstemp(prefix="panel-restore-", suffix=".db", dir=self.database_path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                candidate = sqlite3.connect(temporary_path)
                try:
                    integrity = candidate.execute("PRAGMA integrity_check").fetchone()
                    if not integrity or integrity[0] != "ok":
                        raise StorageError("Backup database integrity check failed")
                    row = candidate.execute("SELECT payload FROM panel_state WHERE id = 1").fetchone()
                    key_row = candidate.execute("SELECT value FROM metadata WHERE key = 'master_key_fingerprint'").fetchone()
                    if not row:
                        raise StorageError("Backup does not contain panel state")
                    if key_row and self.key_fingerprint and key_row[0] != self.key_fingerprint:
                        raise StorageError("Backup was encrypted with a different PANEL_MASTER_KEY")
                    self._unseal_value(json.loads(row[0]))
                finally:
                    candidate.close()
                for suffix in ("-wal", "-shm"):
                    stale_path = str(self.database_path) + suffix
                    if os.path.exists(stale_path):
                        os.unlink(stale_path)
                os.replace(temporary_path, self.database_path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)

    @staticmethod
    def is_sqlite(content: bytes) -> bool:
        return content.startswith(b"SQLite format 3\x00")

    def backup_current_database(self) -> Path:
        """Create a filesystem rollback snapshot before replacing panel state."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        destination = self.database_path.parent / f"panel-before-restore-{stamp}.db"
        self._atomic_write(destination, self.export_database())
        return destination
