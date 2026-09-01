import sqlite3
from contextlib import contextmanager
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
                CREATE TABLE IF NOT EXISTS ingest_job (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  chat_id INTEGER NOT NULL,
                  mode TEXT NOT NULL CHECK(mode IN ('new', 'append')),
                  target_series_id INTEGER,
                  series_id INTEGER,
                  title TEXT,
                  status TEXT NOT NULL DEFAULT 'collecting',
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  error TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ingest_file (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id INTEGER NOT NULL REFERENCES ingest_job(id) ON DELETE CASCADE,
                  source_file_id TEXT NOT NULL,
                  original_filename TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  position INTEGER,
                  stored_filename TEXT,
                  uploaded_file_id TEXT,
                  uploaded_message_id INTEGER,
                  video_id INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  error TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(job_id, source_file_id)
                );
                CREATE TABLE IF NOT EXISTS processed_update (
                  update_id INTEGER PRIMARY KEY,
                  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_ingest_job_user_status
                  ON ingest_job(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_ingest_file_job_status
                  ON ingest_file(job_id, status);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(video)")}
            if "job_file_id" not in columns:
                conn.execute("ALTER TABLE video ADD COLUMN job_file_id INTEGER")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_video_job_file ON video(job_file_id) WHERE job_file_id IS NOT NULL"
            )

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_series(self, title: str) -> int:
        with self.connect() as conn:
            return conn.execute("INSERT INTO series(title) VALUES (?)", (title,)).lastrowid

    def add_video(self, series_id: int, position: int, stored_filename: str, original_filename: str,
                  telegram_file_id: str, thumbnail_path: str | None, job_file_id: int | None = None) -> int:
        with self.connect() as conn:
            if job_file_id is not None:
                existing = conn.execute(
                    "SELECT id FROM video WHERE job_file_id = ?", (job_file_id,)
                ).fetchone()
                if existing:
                    return int(existing["id"])
            video_id = conn.execute(
                """INSERT INTO video(series_id, position, stored_filename, original_filename,
                                      telegram_file_id, thumbnail_path, job_file_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (series_id, position, stored_filename, original_filename, telegram_file_id, thumbnail_path, job_file_id),
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
            return conn.execute(
                "SELECT 1 FROM series WHERE id = ?", (series_id,)
            ).fetchone() is not None

    def next_position(self, series_id: int) -> int:
        """Return the next 1-based filename/order number in a series."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS value FROM video WHERE series_id = ?",
                (series_id,),
            ).fetchone()
            return int(row["value"])

    def thumbnail_for_video(self, video_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT thumbnail_path FROM video WHERE id = ?", (video_id,)).fetchone()

    def video_for_send(self, video_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT stored_filename, telegram_file_id FROM video WHERE id = ?", (video_id,)
            ).fetchone()

    def set_cover(self, series_id: int, position: int) -> bool:
        """Set the representative thumbnail to one of the series videos."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM video WHERE series_id = ? AND position = ?",
                (series_id, position),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "UPDATE series SET cover_video_id = ? WHERE id = ?",
                (row["id"], series_id),
            )
            return True

    def rename_series(self, series_id: int, title: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "UPDATE series SET title = ? WHERE id = ?", (title, series_id)
            ).rowcount == 1

    def delete_series(self, series_id: int) -> list[str]:
        """Delete one series and return its thumbnail paths for filesystem cleanup."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT thumbnail_path FROM video WHERE series_id = ?", (series_id,)
            ).fetchall()
            if not conn.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone():
                return []
            conn.execute("DELETE FROM video WHERE series_id = ?", (series_id,))
            conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
            return [row["thumbnail_path"] for row in rows if row["thumbnail_path"]]

    def active_job(self, user_id: int):
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM ingest_job
                   WHERE user_id = ? AND status IN ('collecting', 'waiting_title', 'queued', 'processing')
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()

    def get_or_create_collecting_job(self, user_id: int, chat_id: int, mode: str,
                                     target_series_id: int | None = None) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id, mode, target_series_id FROM ingest_job
                   WHERE user_id = ? AND status IN ('collecting', 'waiting_title')
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if row and row["mode"] == mode and row["target_series_id"] == target_series_id:
                return int(row["id"])
            if row:
                conn.execute(
                    "UPDATE ingest_job SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row["id"],),
                )
            return conn.execute(
                """INSERT INTO ingest_job(user_id, chat_id, mode, target_series_id)
                   VALUES (?, ?, ?, ?)""",
                (user_id, chat_id, mode, target_series_id),
            ).lastrowid

    def add_job_file(self, job_id: int, source_file_id: str, original_filename: str, kind: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO ingest_file(job_id, source_file_id, original_filename, kind)
                   VALUES (?, ?, ?, ?)""",
                (job_id, source_file_id, original_filename, kind),
            )
            conn.execute(
                "UPDATE ingest_job SET status = 'collecting', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (job_id,),
            )
            return cursor.rowcount == 1

    def get_job(self, job_id: int):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM ingest_job WHERE id = ?", (job_id,)).fetchone()

    def job_files(self, job_id: int):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM ingest_file WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()

    def set_job_status(self, job_id: int, status: str, error: str | None = None):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_job SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, error, job_id),
            )

    def set_job_title(self, job_id: int, title: str):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_job SET title = ?, status = 'queued', error = NULL,
                   cancel_requested = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (title, job_id),
            )

    def set_job_series(self, job_id: int, series_id: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE ingest_job SET series_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (series_id, job_id),
            )

    def assign_job_file(self, file_id: int, position: int, stored_filename: str):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET position = COALESCE(position, ?),
                   stored_filename = COALESCE(stored_filename, ?), updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (position, stored_filename, file_id),
            )

    def mark_file_processing(self, file_id: int):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET status = 'processing', attempts = attempts + 1,
                   error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (file_id,),
            )

    def mark_file_uploaded(self, file_id: int, telegram_file_id: str, message_id: int):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET uploaded_file_id = ?, uploaded_message_id = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (telegram_file_id, message_id, file_id),
            )

    def mark_file_completed(self, file_id: int, video_id: int):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET status = 'completed', video_id = ?, error = NULL,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (video_id, file_id),
            )

    def mark_file_failed(self, file_id: int, error: str):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET status = 'failed', error = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (error[:1000], file_id),
            )

    def request_cancel(self, user_id: int) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id, status FROM ingest_job WHERE user_id = ?
                   AND status IN ('collecting', 'waiting_title', 'queued', 'processing')
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            status = "cancelled" if row["status"] != "processing" else "processing"
            conn.execute(
                """UPDATE ingest_job SET cancel_requested = 1, status = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (status, row["id"]),
            )
            return int(row["id"])

    def latest_failed_job(self, user_id: int):
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM ingest_job WHERE user_id = ? AND status IN ('partial_failed', 'failed')
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()

    def prepare_retry(self, job_id: int) -> bool:
        with self.connect() as conn:
            job = conn.execute(
                "SELECT status FROM ingest_job WHERE id = ?", (job_id,)
            ).fetchone()
            if not job or job["status"] not in ("partial_failed", "failed"):
                return False
            conn.execute(
                """UPDATE ingest_file SET status = 'pending', error = NULL,
                   updated_at = CURRENT_TIMESTAMP WHERE job_id = ? AND status = 'failed'""",
                (job_id,),
            )
            conn.execute(
                """UPDATE ingest_job SET status = 'queued', cancel_requested = 0, error = NULL,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (job_id,),
            )
            return True

    def recover_jobs(self):
        with self.connect() as conn:
            conn.execute(
                """UPDATE ingest_file SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                   WHERE status = 'processing'"""
            )
            conn.execute(
                """UPDATE ingest_job SET status = 'queued', cancel_requested = 0,
                   updated_at = CURRENT_TIMESTAMP WHERE status = 'processing'"""
            )
            jobs = conn.execute(
                """SELECT * FROM ingest_job
                   WHERE status IN ('collecting', 'waiting_title', 'queued') ORDER BY id"""
            ).fetchall()
            for job in jobs:
                if job["status"] == "collecting":
                    new_status = "queued" if job["mode"] == "append" else "waiting_title"
                    conn.execute(
                        "UPDATE ingest_job SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_status, job["id"]),
                    )
            return conn.execute(
                """SELECT * FROM ingest_job
                   WHERE status IN ('waiting_title', 'queued') ORDER BY id"""
            ).fetchall()

    def is_update_processed(self, update_id: int) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM processed_update WHERE update_id = ?", (update_id,)
            ).fetchone() is not None

    def mark_update_processed(self, update_id: int):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_update(update_id) VALUES (?)", (update_id,)
            )
            conn.execute(
                """DELETE FROM processed_update WHERE update_id NOT IN
                   (SELECT update_id FROM processed_update ORDER BY update_id DESC LIMIT 5000)"""
            )
