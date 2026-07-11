"""
통합 오케스트레이터 (run_all).
Gmail 최근 메일을 '한 번의 Gemini 요청'으로 분류 + 뉴스레터 요약 + 캘린더 일정을 동시에 처리한다.

무료 한도(하루 ~20건) 보호 장치 2가지:
  1) state/processed.json  — 이미 처리한 메일 id 기록. 새 메일이 없으면 Gemini를 아예 호출하지 않는다.
  2) state/quota.json       — 오늘 Gemini 호출 횟수. MAX_GEMINI_CALLS_PER_DAY 이상이면 건너뛴다.

한 번 실행 = Gemini 요청 1건.
"""

import json
import os
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import errors
from pydantic import BaseModel

import classify as C            # 인증/메일읽기/라벨 + build_batch_prompt
import calendar_sync as CS       # 캘린더 생성 로직 + 중복 방지(state)
import newsletter_digest as ND   # build_html / 발신자 제외 / 뉴스레터 선별

sys.stdout.reconfigure(encoding="utf-8")

MODEL = "gemini-2.5-flash"
MAX_EMAILS = 15
TIMEZONE = "Australia/Brisbane"
MAX_GEMINI_CALLS_PER_DAY = int(os.environ.get("MAX_GEMINI_CALLS_PER_DAY", "15"))
HEADLESS = bool(os.environ.get("HEADLESS"))   # 값이 있으면(예: 1) 브라우저 자동 열기 금지

STATE_DIR = Path("state")
PROCESSED = STATE_DIR / "processed.json"   # 메일 id → 처리 완료 시각
QUOTA = STATE_DIR / "quota.json"           # {date, count}
RESULTS = STATE_DIR / "results.html"       # 뉴스레터 요약 페이지


# ─────────────────────────────────────────────────────────────────────────────
#  통합 Pydantic 모델 — 세 기능(분류/뉴스레터/캘린더)에 필요한 모든 필드
# ─────────────────────────────────────────────────────────────────────────────
class CalendarEvent(BaseModel):
    """메일 하나에서 나온 '날짜 있는 일정' 한 건. 한 메일에 여러 개 나올 수 있음."""
    title: str
    start: str        # "YYYY-MM-DD"(종일) 또는 "YYYY-MM-DDTHH:MM:SS"(시각)
    all_day: bool
    notes: str        # 무엇/언제까지 액션이 필요한지 (한국어)


class EmailAnalysis(BaseModel):
    index: int
    # ── 분류(classify.py 와 동일한 Literal) ──
    category: Literal[
        "uni", "news", "real_estate", "finance",
        "shopping", "message", "personal", "junk", "other",
    ]
    uni_subtype: Literal[
        "study", "announcement", "newsletter", "admin", "not_uni",
    ]
    # ── 뉴스레터(newsletter_digest.py 와 동일한 Literal) ──
    is_newsletter: bool
    topic: Literal["Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other"]
    english_title: str
    english_summary: str
    korean_title: str
    korean_summary: str
    # ── 캘린더 (한 메일에서 여러 일정이 나올 수 있음) ──
    events: list[CalendarEvent]


def unified_guide() -> str:
    """분류 + 뉴스레터 + 캘린더 세 가이드를 하나로 합친 system instruction."""
    today = date.today().isoformat()
    return f"""You analyze a batch of emails for a personal assistant, and produce ONE combined result per email.

Today is {today} (timezone {TIMEZONE}).
You are given several emails, numbered (=== EMAIL n ===).
Return ONE item PER email, in order, each with its matching `index` (1-based).

======== 1) CLASSIFICATION ========
{C.CATEGORY_GUIDE}

======== 2) NEWSLETTER ========
- is_newsletter: true if it's a newsletter / informational digest / promotional info blast.
  false for personal mail, receipts, e-signing requests, bank/payment alerts, one-to-one agent messages.
  IMPORTANT: job-alert / job-recommendation digests from LinkedIn or Seek (seek.com.au) are NOT
  newsletters → is_newsletter=false.
- topic: best fit — Uni (university/study), AI, Career (jobs/recruiting), Business, Tech, Finance,
  News (general news), Lifestyle, Other.
- english_title: a short, natural English title.
- english_summary: 2-3 sentence summary in natural English (the main content).
- korean_title: a short, natural Korean title.
- korean_summary: 1-2 sentence brief summary in Korean.
(title/summary/topic should be filled for every email; they are used when is_newsletter=true.)

======== 3) CALENDAR ========
Extract EVERY concrete dated, calendar-worthy item as a SEPARATE entry in `events` (a list).
ONE email — ESPECIALLY a newsletter — often contains MULTIPLE dated items; return one event per item.
Include:
 - bookings / reservations / appointments / confirmed events;
 - university dates & deadlines: assignment due dates, exam/quiz dates, class/tutorial times,
   enrolment or timetable-adjustment windows (use the CLOSING date), orientation / Welcome Week,
   event dates, "action required by <date>".
For each event object:
 - title: short calendar title (English or Korean).
 - start: "YYYY-MM-DD" if no specific time (then all_day=true),
          or "YYYY-MM-DDTHH:MM:SS" if a time is given (then all_day=false).
 - all_day: true/false accordingly.
 - notes: 1-2 lines in Korean describing what it is / what action is needed.
For a date RANGE (e.g. "21-25 July"), use the START date and mention the full range in notes.
Resolve relative dates (e.g. "next Friday", "Friday 20 July") using today's date above.
If there are NO dated items, return an empty list for events."""


# ─────────────────────────────────────────────────────────────────────────────
#  상태 파일(state/*.json) 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _save_json(path: Path, data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_quota() -> dict:
    """오늘 날짜의 호출 횟수. 날짜가 바뀌면 0으로 리셋."""
    today = date.today().isoformat()
    q = _load_json(QUOTA, {})
    if q.get("date") != today:
        q = {"date": today, "count": 0}
    return q


def analyze_unified(client, emails, retries=4):
    """한 번의 요청으로 분류+뉴스레터+캘린더를 모두 분석. 429면 대기 후 재시도."""
    import time
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=C.build_batch_prompt(emails),
                config={
                    "system_instruction": unified_guide(),
                    "response_mime_type": "application/json",
                    "response_schema": list[EmailAnalysis],
                },
            )
            return resp.parsed or []
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  (무료 한도 초과 — {wait}초 대기 후 재시도...)")
                time.sleep(wait)
                continue
            raise


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] run_all 시작")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 일일 하드 캡 확인 (Gmail 접속 전에 먼저 확인해도 되지만, 메일 유무와 무관하게 보호)
    quota = load_quota()
    if quota["count"] >= MAX_GEMINI_CALLS_PER_DAY:
        print(f"오늘 Gemini 호출 {quota['count']}/{MAX_GEMINI_CALLS_PER_DAY} — 일일 한도 도달, 건너뜀.")
        return

    # 2) Gmail 최근 메일 1회 조회
    gmail = C.get_gmail_service()
    emails = C.get_recent_emails(gmail, max_results=MAX_EMAILS)

    # 3) 이미 처리한 메일 제외 → 새 메일만
    processed = _load_json(PROCESSED, {})
    new_emails = [e for e in emails if e["id"] not in processed]
    if not new_emails:
        print("새 메일 없음 — AI 호출 0건")
        return

    print(f"새 메일 {len(new_emails)}개 → Gemini 통합 분석 1건 호출 "
          f"(오늘 {quota['count']}/{MAX_GEMINI_CALLS_PER_DAY})")

    # 4) 단 한 번의 통합 요청
    client = genai.Client()
    items = analyze_unified(client, new_emails)

    # 호출 성공 → 오늘 카운트 +1 (실패 시 예외로 빠져나가 카운트 안 올라감)
    quota["count"] += 1
    _save_json(QUOTA, quota)

    # 5a) Gmail 라벨 부착
    label_cache = C.load_labels(gmail)
    for a in items:
        idx = a.index - 1
        if not (0 <= idx < len(new_emails)):
            continue
        e = new_emails[idx]
        label_name = C.label_name_for(a)   # EmailAnalysis 는 category/uni_subtype 를 가짐
        try:
            C.apply_label(gmail, e["id"], label_name, label_cache)
            print(f"  🏷️  [{label_name}] {e['subject']}")
        except Exception as ex:  # 라벨 하나 실패해도 전체는 진행
            print(f"  ⚠️  라벨 실패({e['subject']}): {ex}")

    # 5b) 뉴스레터 요약 HTML — 누적 아카이브에 새 뉴스레터를 더해 날짜순으로 다시 그림
    pairs = ND.select_newsletters(items, new_emails)   # LinkedIn/Seek 등 제외
    archive = ND.load_archive()
    before = len(archive)
    archive = ND.merge_into_archive(archive, pairs)
    ND.save_archive(archive)
    RESULTS.write_text(ND.build_html(archive), encoding="utf-8")
    print(f"  📰 뉴스레터 누적 {len(archive)}개 (이번 +{len(archive) - before}) → {RESULTS.resolve()}")

    # 5c) 캘린더 이벤트 생성 — 한 메일에서 나온 여러 일정을 각각 등록
    #     (calendar_sync 의 생성 로직 재사용, 일정별로 중복 방지 키 사용)
    cal_processed = CS.load_processed()
    cal = None
    created = 0
    for a in items:
        idx = a.index - 1
        if not (0 <= idx < len(new_emails)):
            continue
        e = new_emails[idx]
        for ev in a.events:
            key = f"{e['id']}|{ev.title}|{ev.start}"   # 같은 메일의 같은 일정은 한 번만
            if key in cal_processed:
                continue
            try:  # 오늘 이전(이미 지난) 일정은 등록하지 않음
                if date.fromisoformat(ev.start[:10]) < date.today():
                    continue
            except ValueError:
                pass
            if cal is None:
                cal = CS.get_calendar_service()
            info = CS.EventInfo(
                index=a.index, has_event=True, title=ev.title,
                start=ev.start, all_day=ev.all_day, notes=ev.notes,
            )
            try:
                gev = CS.create_event(cal, info)
                cal_processed[key] = gev.get("id")
                created += 1
                when = ev.start + (" (종일)" if ev.all_day else "")
                print(f"  📅 일정 등록: {ev.title} [{when}]")
            except Exception as ex:
                print(f"  ⚠️  일정 실패({ev.title}): {ex}")
    if created:
        CS.save_processed(cal_processed)
    print(f"  📅 새 일정 {created}개 등록")

    # 6) 처리한 메일 id 기록 (다음 실행부터 중복 분석 방지)
    stamp = datetime.now().isoformat(timespec="seconds")
    for e in new_emails:
        processed[e["id"]] = stamp
    _save_json(PROCESSED, processed)

    # 7) 브라우저 열기 (HEADLESS 아닐 때만; Docker 에서는 열지 않음)
    if not HEADLESS:
        webbrowser.open(RESULTS.resolve().as_uri())

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] run_all 완료 "
          f"(오늘 Gemini {quota['count']}/{MAX_GEMINI_CALLS_PER_DAY})")


if __name__ == "__main__":
    main()
