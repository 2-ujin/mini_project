"""
메일에서 '날짜가 있는 할 일/이벤트'를 추출해 Google 캘린더에 자동 등록.
 - 예약/부킹/초대 등 확정된 이벤트
 - uni 관련: 과제 마감, 시험, 수업/수강신청 등 '언제까지 무엇을' 액션
이미 등록한 메일은 processed_events.json 에 기록해 중복 등록을 막는다.
여러 메일을 한 번의 요청으로 묶어 처리(무료 한도 절약).
"""

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from google import genai
from google.genai import errors
from googleapiclient.discovery import build
from pydantic import BaseModel

import classify as C  # Gmail 로그인/메일 읽기 재사용

sys.stdout.reconfigure(encoding="utf-8")

MODEL = "gemini-2.5-flash"
MAX_EMAILS = 15
TIMEZONE = "Australia/Brisbane"      # QUT/브리즈번 기준
STATE_DIR = Path("state")
STATE = STATE_DIR / "processed_events.json"   # 중복 방지 기록 (Docker 볼륨에 매핑)
OLD_STATE = Path("processed_events.json")      # 예전 위치(하위 호환 읽기)


def load_processed() -> dict:
    """캘린더 중복 방지 기록을 읽는다. 예전 위치 파일이 있으면 그것도 인정."""
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    if OLD_STATE.exists():
        return json.loads(OLD_STATE.read_text(encoding="utf-8"))
    return {}


def save_processed(processed: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")

GUIDE = f"""Today is {date.today().isoformat()} (timezone {TIMEZONE}).
You are given several emails, numbered. Return one item PER email, in order, with matching index.

Set has_event=true ONLY when the email contains a concrete calendar-worthy item with a date:
 - a booking / reservation / appointment / confirmed event, OR
 - a university action with a deadline: assignment due date, exam/quiz date,
   class/tutorial time, enrolment / class-registration deadline, "action required by <date>".

Fields:
 - title: short calendar title (English or Korean).
 - start: ISO 8601. "YYYY-MM-DD" if no specific time (then all_day=true),
          or "YYYY-MM-DDTHH:MM:SS" if a time is given (then all_day=false).
 - all_day: true/false accordingly.
 - notes: 1-2 lines in Korean describing what action is needed.

Resolve relative dates (e.g. "next Friday") using today's date above.
If there is NO concrete dated event/deadline, set has_event=false."""


class EventInfo(BaseModel):
    index: int
    has_event: bool
    title: str
    start: str      # ISO date or datetime
    all_day: bool
    notes: str


def get_calendar_service():
    return build("calendar", "v3", credentials=C.get_credentials())


def analyze(client, emails, retries=4):
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=C.build_batch_prompt(emails),
                config={
                    "system_instruction": GUIDE,
                    "response_mime_type": "application/json",
                    "response_schema": list[EventInfo],
                },
            )
            return resp.parsed or []
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429 and attempt < retries - 1:
                print("  (무료 한도 대기 — 25초 후 재시도...)")
                time.sleep(25)
                continue
            raise


def create_event(service, info: EventInfo) -> dict:
    """EventInfo를 Google 캘린더 이벤트로 생성."""
    if info.all_day:
        d = info.start[:10]
        end = (date.fromisoformat(d) + timedelta(days=1)).isoformat()  # 종일 이벤트 end는 다음날
        body = {
            "summary": info.title,
            "description": info.notes,
            "start": {"date": d},
            "end": {"date": end},
        }
    else:
        try:
            sdt = datetime.fromisoformat(info.start)
            end = (sdt + timedelta(hours=1)).isoformat()
        except ValueError:
            end = info.start
        body = {
            "summary": info.title,
            "description": info.notes,
            "start": {"dateTime": info.start, "timeZone": TIMEZONE},
            "end": {"dateTime": end, "timeZone": TIMEZONE},
        }
    return service.events().insert(calendarId="primary", body=body).execute()


def main():
    client = genai.Client()
    gmail = C.get_gmail_service()
    cal = get_calendar_service()
    emails = C.get_recent_emails(gmail, max_results=MAX_EMAILS)

    processed = load_processed()

    print(f"최근 메일 {len(emails)}개에서 일정 추출 중...")
    items = analyze(client, emails)

    created = 0
    for info in items:
        idx = info.index - 1
        if not info.has_event or not (0 <= idx < len(emails)):
            continue
        e = emails[idx]
        if e["id"] in processed:
            print(f"⏭️  이미 등록됨: {info.title}")
            continue
        ev = create_event(cal, info)
        processed[e["id"]] = ev.get("id")
        created += 1
        when = info.start + (" (종일)" if info.all_day else "")
        print(f"📅 등록: {info.title}  [{when}]")
        print(f"    메일: {e['subject']}")
        print(f"    노트: {info.notes}")

    save_processed(processed)
    print(f"\n✅ 새 일정 {created}개 등록 완료. Google 캘린더를 확인하세요.")


if __name__ == "__main__":
    main()
