"""JSON-file state store for tests that seed ``panel.DATA_FILE`` directly.

The panel keeps its state in an encrypted SQLite database (``storage.py``),
while many tests describe their fixtures as a plain ``data.json`` file. This
test double keeps that fixture style working: it reads and writes whatever
path ``DATA_FILE`` currently points to, so tests stay isolated in their
temporary directory and never touch the real ``data/panel.db``.
"""
import json
import os
import tempfile


class JsonFileStore:
    key_fingerprint = ''

    def __init__(self, path):
        self._path = path if callable(path) else (lambda: path)

    def load(self):
        path = self._path()
        try:
            with open(path, encoding='utf-8') as handle:
                raw = handle.read()
        except FileNotFoundError:
            return {}
        return json.loads(raw) if raw.strip() else {}

    def save(self, data):
        path = self._path()
        directory = os.path.dirname(path) or '.'
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def plaintext_secret_paths(self):
        return []


def install(case, panel):
    """Route ``panel.load_data``/``save_data`` to ``panel.DATA_FILE`` for one test."""
    original = panel.STATE_STORE
    panel.STATE_STORE = JsonFileStore(lambda: panel.DATA_FILE)
    case.addCleanup(setattr, panel, 'STATE_STORE', original)
