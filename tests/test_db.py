import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_library_is_preserved_and_job_column_is_added(self):
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(
                """
                CREATE TABLE series (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  title TEXT NOT NULL,
                  cover_video_id INTEGER,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE video (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  series_id INTEGER NOT NULL,
                  position INTEGER NOT NULL,
                  stored_filename TEXT NOT NULL,
                  original_filename TEXT NOT NULL,
                  telegram_file_id TEXT NOT NULL,
                  thumbnail_path TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(series_id, position)
                );
                INSERT INTO series(title) VALUES ('기존 시리즈');
                """
            )
            conn.commit()
        finally:
            conn.close()
        database = Database(self.path)
        self.assertEqual(database.list_series()[0]["title"], "기존 시리즈")
        with database.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(video)")}
        self.assertIn("job_file_id", columns)

    def test_job_survives_recovery_and_failed_file_can_be_retried(self):
        database = Database(self.path)
        job_id = database.get_or_create_collecting_job(10, 20, "new")
        self.assertTrue(database.add_job_file(job_id, "file-a", "movie.mp4", "video"))
        self.assertFalse(database.add_job_file(job_id, "file-a", "movie.mp4", "video"))
        database.set_job_title(job_id, "테스트")
        database.set_job_status(job_id, "processing")
        file_id = int(database.job_files(job_id)[0]["id"])
        database.mark_file_processing(file_id)

        recovered = database.recover_jobs()
        self.assertEqual(recovered[0]["status"], "queued")
        self.assertEqual(database.job_files(job_id)[0]["status"], "pending")

        database.mark_file_failed(file_id, "temporary error")
        database.set_job_status(job_id, "partial_failed")
        self.assertTrue(database.prepare_retry(job_id))
        self.assertEqual(database.job_files(job_id)[0]["status"], "pending")

    def test_update_and_video_writes_are_idempotent(self):
        database = Database(self.path)
        database.mark_update_processed(123)
        database.mark_update_processed(123)
        self.assertTrue(database.is_update_processed(123))

        series_id = database.create_series("테스트")
        first = database.add_video(series_id, 1, "1.mp4", "a.mp4", "telegram-a", None, 99)
        second = database.add_video(series_id, 1, "1.mp4", "a.mp4", "telegram-a", None, 99)
        self.assertEqual(first, second)
        self.assertEqual(len(database.list_videos(series_id)), 1)

    def test_series_archives_can_be_replaced_and_invalidated(self):
        database = Database(self.path)
        series_id = database.create_series("ZIP 테스트")
        database.replace_archives(
            series_id,
            [{
                "part_number": 1,
                "filename": "ZIP 테스트.zip",
                "telegram_file_id": "archive-file",
                "size_bytes": 123,
            }],
        )
        self.assertEqual(database.list_archives(series_id)[0]["filename"], "ZIP 테스트.zip")
        database.rename_series(series_id, "새 제목")
        self.assertEqual(database.list_archives(series_id), [])


if __name__ == "__main__":
    unittest.main()
