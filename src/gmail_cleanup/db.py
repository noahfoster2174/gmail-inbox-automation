import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS emails (
                    gmail_id TEXT PRIMARY KEY,
                    thread_id TEXT,
                    sender TEXT,
                    sender_domain TEXT,
                    subject TEXT,
                    date TEXT,
                    is_read INTEGER DEFAULT 0,
                    has_attachments INTEGER DEFAULT 0,
                    body_preview TEXT,
                    list_unsubscribe TEXT,
                    precedence TEXT,
                    classification TEXT,
                    category TEXT,
                    review_flag INTEGER DEFAULT 0,
                    account TEXT
                );
                CREATE TABLE IF NOT EXISTS senders (
                    sender TEXT PRIMARY KEY,
                    sender_domain TEXT,
                    total_count INTEGER DEFAULT 0,
                    read_count INTEGER DEFAULT 0,
                    reply_count INTEGER DEFAULT 0,
                    read_rate REAL DEFAULT 0.0,
                    is_newsletter INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    gmail_id TEXT PRIMARY KEY,
                    action TEXT,
                    category TEXT,
                    decided_by TEXT,
                    timestamp TEXT DEFAULT (datetime('now'))
                );
            """)

    def upsert_email(self, email: dict):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO emails (
                    gmail_id, thread_id, sender, sender_domain, subject,
                    date, is_read, has_attachments, body_preview,
                    list_unsubscribe, precedence, account
                ) VALUES (
                    :gmail_id, :thread_id, :sender, :sender_domain, :subject,
                    :date, :is_read, :has_attachments, :body_preview,
                    :list_unsubscribe, :precedence, :account
                ) ON CONFLICT(gmail_id) DO NOTHING
            """, email)

    def get_email(self, gmail_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM emails WHERE gmail_id = ?", (gmail_id,)
            ).fetchone()

    def count_emails(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def get_all_unclassified(self) -> list:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM emails WHERE classification IS NULL"
            ).fetchall()

    def get_flagged(self) -> list:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM emails WHERE classification = 'FLAG' ORDER BY date DESC"
            ).fetchall()

    def get_by_classification(self, classification: str) -> list:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM emails WHERE classification = ?", (classification,)
            ).fetchall()

    def set_decision(self, gmail_id: str, action: str, category: str | None, decided_by: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE emails SET classification = ?, category = ? WHERE gmail_id = ?",
                (action, category, gmail_id)
            )
            conn.execute("""
                INSERT INTO decisions (gmail_id, action, category, decided_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(gmail_id) DO UPDATE SET
                    action = excluded.action,
                    category = excluded.category,
                    decided_by = excluded.decided_by,
                    timestamp = datetime('now')
            """, (gmail_id, action, category, decided_by))

    def compute_sender_stats(self):
        # Auto-archived mail is excluded from read_count: the weekly archive
        # pass marks messages read in Gmail, which would otherwise count
        # robot-archived mail as user engagement and inflate read_rate.
        with self._connect() as conn:
            conn.execute("DELETE FROM senders")
            conn.execute("""
                INSERT INTO senders (sender, sender_domain, total_count, read_count, read_rate)
                SELECT
                    e.sender, e.sender_domain,
                    COUNT(*) as total_count,
                    SUM(CASE WHEN e.is_read = 1 AND d.gmail_id IS NULL THEN 1 ELSE 0 END) as read_count,
                    CAST(SUM(CASE WHEN e.is_read = 1 AND d.gmail_id IS NULL THEN 1 ELSE 0 END) AS REAL)
                        / COUNT(*) as read_rate
                FROM emails e
                LEFT JOIN decisions d
                    ON d.gmail_id = e.gmail_id
                    AND d.action IN ('AUTO_ARCHIVE', 'AUTO_TRASH')
                GROUP BY e.sender
            """)

    def get_sender(self, sender: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM senders WHERE sender = ?", (sender,)
            ).fetchone()

    def record_auto_archive(self, gmail_ids: list, action: str = "AUTO_ARCHIVE"):
        """Record that the weekly archive pass moved these messages out of the inbox
        (action AUTO_ARCHIVE, or AUTO_TRASH for block-listed senders).

        Written to `decisions` (not emails.is_read) so tool actions never count
        as user engagement in compute_sender_stats.
        """
        with self._connect() as conn:
            conn.executemany("""
                INSERT INTO decisions (gmail_id, action, category, decided_by)
                VALUES (?, ?, NULL, 'weekly_archive')
                ON CONFLICT(gmail_id) DO UPDATE SET
                    action = excluded.action,
                    decided_by = 'weekly_archive',
                    timestamp = datetime('now')
            """, [(gid, action) for gid in gmail_ids])

    def bulk_update_read_status(self, gmail_ids: list, is_read: bool):
        """Mark a batch of emails as read or unread in the local DB."""
        with self._connect() as conn:
            conn.executemany(
                "UPDATE emails SET is_read = ? WHERE gmail_id = ?",
                [(1 if is_read else 0, gid) for gid in gmail_ids]
            )
