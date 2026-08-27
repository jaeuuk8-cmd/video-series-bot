import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS series (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT NOT NULL,
                  cover_video_id INTEGER,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS video (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  stored_filename TEXT NOT NULL,
                  original_filename TEXT NOT NULL,
                  telegram_file_id TEXT NOT NULL,
                  thumbnail_path TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(series_id, position)
                );
                """
            )

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_series(self, title: str) -> int:
        with self.connect() as conn:
            return conn.execute("INSERT INTO series(title) VALUES (?)", (title,)).lastrowid

    def add_video(self, series_id: int, position: int, stored_filename: str, original_filename: str,
                  telegram_file_id: str, thumbnail_path: str | None) -> int:
        with self.connect() as conn:
            video_id = conn.execute(
                """INSERT INTO video(series_id, position, stored_filename, original_filename, telegram_file_id, thumbnail_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (series_id, position, stored_filename, original_filename, telegram_file_id, thumbnail_path),
            ).lastrowid
            conn.execute("UPDATE series SET cover_video_id = COALESCE(cover_video_id, ?) WHERE id = ?", (video_id, series_id))
            return video_id

    def list_series(self):
        with self.connect() as conn:
            return conn.execute(
                """SELECT s.id, s.title, s.cover_video_id, s.created_at, COUNT(v.id) AS video_count, cv.thumbnail_path
                   FROM series s
                   LEFT JOIN video v ON v.series_id = s.id
                   LEFT JOIN video cv ON cv.id = s.cover_video_id
                   GROUP BY s.id ORDER BY s.id DESC"""
            ).fetchall()

    def list_videos(self, series_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, position, stored_filename, thumbnail_path FROM video WHERE series_id = ? ORDER BY position",
                (series_id,),
            ).fetchall()

    def series_exists(self, series_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone() is not None

    def next_position(self, series_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS value FROM video WHERE series_id = ?", (series_id,)
            ).fetchone()
            return int(row["value"])

    def thumbnail_for_video(self, video_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT thumbnail_path FROM video WHERE id = ?", (video_id,)).fetchone()

    def video_for_send(self, video_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT stored_filename, telegram_file_id FROM video WHERE id = ?", (video_id,)).fetchone()

    def set_cover(self, series_id: int, position: int) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM video WHERE series_id = ? AND position = ?", (series_id, position)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE series SET cover_video_id = ? WHERE id = ?", (row["id"], series_id))
            return True

    def rename_series(self, series_id: int, title: str) -> bool:
        with self.connect() as conn:
            return conn.execute("UPDATE series SET title = ? WHERE id = ?", (title, series_id)).rowcount == 1

    def delete_series(self, series_id: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT thumbnail_path FROM video WHERE series_id = ?", (series_id,)).fetchall()
            if not conn.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone():
                return []
            conn.execute("DELETE FROM video WHERE series_id = ?", (series_id,))
            conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
            return [row["thumbnail_path"] for row in rows if row["thumbnail_path"]]
