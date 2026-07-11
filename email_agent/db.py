"""
PostgreSQL 저장 계층 (Docker의 db 컨테이너).
DATABASE_URL 환경변수가 있을 때만 동작한다(없으면 조용히 건너뜀 → 로컬 실행 안 깨짐).
emails / events 두 테이블에 누적 저장(중복은 무시). 웹페이지는 여전히 JSON 으로 그리고,
DB 는 쿼리·분석용 저장소로 함께 쓴다.
"""

import os
import time

DATABASE_URL = os.environ.get("DATABASE_URL")

EMAILS_DDL = """CREATE TABLE IF NOT EXISTS emails (
    id            TEXT PRIMARY KEY,
    ts            BIGINT,
    received_date DATE,
    label         TEXT,
    category      TEXT,
    is_newsletter BOOLEAN,
    topic         TEXT,
    en_title      TEXT,
    en_summary    TEXT,
    ko_title      TEXT,
    ko_summary    TEXT,
    subject       TEXT,
    sender        TEXT,
    body          TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
)"""

EVENTS_DDL = """CREATE TABLE IF NOT EXISTS events (
    gcal_id    TEXT PRIMARY KEY,
    email_id   TEXT,
    title      TEXT,
    start_date TEXT,
    all_day    BOOLEAN,
    kind       TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
)"""

INSERT_EMAIL = """INSERT INTO emails
    (id, ts, received_date, label, category, is_newsletter, topic,
     en_title, en_summary, ko_title, ko_summary, subject, sender, body)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (id) DO NOTHING"""

INSERT_EVENT = """INSERT INTO events
    (gcal_id, email_id, title, start_date, all_day, kind, notes)
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (gcal_id) DO NOTHING"""


def enabled() -> bool:
    return bool(DATABASE_URL)


def _connect(retries=12, delay=3):
    """DB 연결. 컨테이너 기동 직후엔 DB가 아직 준비 중일 수 있어 잠깐 재시도."""
    import psycopg  # 지연 임포트 → psycopg 미설치 로컬에서도 db.py 임포트는 안전
    last = None
    for _ in range(retries):
        try:
            return psycopg.connect(DATABASE_URL)
        except Exception as e:
            last = e
            time.sleep(delay)
    raise last


def init():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(EMAILS_DDL)
            cur.execute(EVENTS_DDL)
        conn.commit()


def _email_row(e: dict):
    return (
        e.get("id"), e.get("ts", 0), (e.get("date") or None),
        e.get("label"), e.get("category"), bool(e.get("is_newsletter")), e.get("topic"),
        e.get("en_title"), e.get("en_summary"), e.get("ko_title"), e.get("ko_summary"),
        e.get("subject"), e.get("from"), e.get("body"),
    )


def upsert_emails(entries) -> int:
    if not entries:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT_EMAIL, [_email_row(e) for e in entries])
        conn.commit()
    return len(entries)


def upsert_events(rows) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(INSERT_EVENT, [
                (r.get("gcal_id"), r.get("email_id"), r.get("title"), r.get("start"),
                 bool(r.get("all_day")), r.get("kind"), r.get("notes"))
                for r in rows
            ])
        conn.commit()
    return len(rows)
