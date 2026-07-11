"""
PostgreSQL 요약 통계 조회.
  실행: docker compose exec email-agent python db_stats.py
DATABASE_URL 이 있어야 동작(Docker 안에서 실행).
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

import db


def main():
    if not db.enabled():
        print("DATABASE_URL 없음 — DB가 설정되지 않았습니다 (Docker 안에서 실행하세요).")
        return
    c = db._connect()
    total = c.execute("SELECT count(*) FROM emails").fetchone()[0]
    print(f"📬 총 메일: {total}건\n")

    print("[카테고리별]")
    for cat, n in c.execute(
            "SELECT category, count(*) FROM emails GROUP BY category ORDER BY 2 DESC"):
        print(f"  {cat:14} {n}")

    print("\n[뉴스레터 주제별]")
    rows = c.execute(
        "SELECT topic, count(*) FROM emails WHERE is_newsletter GROUP BY topic ORDER BY 2 DESC").fetchall()
    for t, n in rows:
        print(f"  {t:14} {n}")
    if not rows:
        print("  (아직 없음)")

    print("\n[캘린더 일정 종류별]")
    rows = c.execute("SELECT kind, count(*) FROM events GROUP BY kind ORDER BY 2 DESC").fetchall()
    for k, n in rows:
        print(f"  {k:14} {n}")
    if not rows:
        print("  (아직 없음)")

    c.close()


if __name__ == "__main__":
    main()
