"""画像メタデータと特徴量の SQLite キャッシュ。path + mtime が一致すれば再計算しない。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    folder     TEXT,
    taken_at   TEXT,          -- ISO8601 or NULL
    lat        REAL,
    lon        REAL,
    camera     TEXT,
    width      INTEGER,
    height     INTEGER,
    thumb_path TEXT,
    phash      TEXT,           -- 64bit を 16 桁 hex 文字列で保持(SQLite INTEGER の符号問題回避)
    sharpness  REAL,
    embedding  BLOB,          -- float32
    mean_r     REAL,           -- グレーワールド補正用の色計測(0〜1)
    mean_g     REAL,
    mean_b     REAL,
    mean_sat   REAL,
    composition REAL,          -- 黄金比/三分割の構図スコア(0〜1)
    indexed_at TEXT DEFAULT (datetime('now'))
);
"""

_EXTRA_COLUMNS = {
    "mean_r": "REAL", "mean_g": "REAL", "mean_b": "REAL", "mean_sat": "REAL",
    "composition": "REAL",
}


class Cache:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}
        for col, decl in _EXTRA_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE images ADD COLUMN {col} {decl}")

    def get(self, path: str, mtime: float) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM images WHERE path = ? AND mtime = ?", (path, mtime)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("embedding") is not None:
            d["embedding"] = np.frombuffer(d["embedding"], dtype=np.float32)
        if d.get("phash") is not None:
            d["phash"] = int(d["phash"], 16)
        return d

    def upsert(self, rec: dict) -> None:
        rec = dict(rec)
        emb = rec.get("embedding")
        if isinstance(emb, np.ndarray):
            rec["embedding"] = emb.astype(np.float32).tobytes()
        if isinstance(rec.get("phash"), int):
            rec["phash"] = f"{rec['phash'] & ((1 << 64) - 1):016x}"
        cols = [
            "path", "mtime", "folder", "taken_at", "lat", "lon", "camera",
            "width", "height", "thumb_path", "phash", "sharpness", "embedding",
            "mean_r", "mean_g", "mean_b", "mean_sat", "composition",
        ]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "path")
        self.conn.execute(
            f"INSERT INTO images ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}, indexed_at=datetime('now')",
            [rec.get(c) for c in cols],
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
