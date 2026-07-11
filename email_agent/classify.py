"""
Email Agent — MVP (Step 1)  [Google Gemini 무료 API 버전]
Gmail의 최근 메일을 읽어 Gemini로 분류하고 결과를 화면에 출력합니다.
아직 캘린더 등록이나 라벨 부착은 하지 않습니다. (분류 정확도부터 확인)
"""

import base64
import os
import sys
import time
from datetime import datetime
from typing import Literal

# Windows 터미널에서 한글이 깨지지 않도록 UTF-8 출력 강제
sys.stdout.reconfigure(encoding="utf-8")

from google import genai
from google.genai import errors
from pydantic import BaseModel
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Gmail 라벨 수정 + Google 캘린더 이벤트 생성 권한 (메일 삭제는 불가능한 안전한 권한)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]
MODEL = "gemini-2.5-flash"  # 이 키에서 무료 quota가 있는 모델(분당 5건)
MAX_EMAILS = 5   # 무료 한도에 맞춰 우선 5개 (나중에 늘려도 됨)
REQUEST_DELAY = 13  # 요청 간격(초) — 분당 5건 한도를 넘지 않도록

CATEGORY_GUIDE = """You classify emails.

FIRST pick exactly one `category`:
- uni: anything from the university (e.g. QUT / connect.qut.edu.au) — study, campus, admin
- news: newsletters, news digests, media subscriptions
- real_estate: correspondence about a place you actually rent / applied for / your own lease or bond
- finance: bank, invoices, bills, payments, tax, signing your OWN forms (e.g. bond/lease forms)
- shopping: orders, shipping, receipts, promotions from stores
- message: a direct message / greeting / notification addressed to you personally by a person OR a
           service (e.g. "Greetings from ...", account notices, a real reply, a provider asking YOU a
           question). This is a real one-to-you message, NOT an automated marketing/recommendation blast.
- personal: messages from real people you personally know (friends, family)
- junk: spam, phishing, AND automated recommendation / alert blasts you did not ask for —
        * job-alert / job-recommendation digests: "N new jobs", "remember to apply", recommended
          positions or job postings pushed by job boards (LinkedIn/Seek/Indeed/company recruiters).
        * property recommendation / listing blasts: "just came on the market", "potential properties
          to consider", "more inspection times available", "register to inspect".
        If it is a bulk recommendation/listing you didn't personally request, it is junk.
- other: anything that fits none of the above

THEN, ONLY IF category is "uni", pick a `uni_subtype` (otherwise use "not_uni"):
- study: about her actual studying — assignments, submission deadlines, exams/quizzes,
         lecture/tutorial/class schedules or changes, course content. (Most important.)
- announcement: important campus/course announcements or urgent notices
- newsletter: general university newsletters, event promos, info digests
- admin: enrolment, fees, results/grades, timetables admin, IT/library accounts
If unsure among uni subtypes, prefer "study" when a date/deadline/action is involved,
otherwise "announcement"."""


# 여러 메일을 한 번의 요청으로 묶을 때 프롬프트 뒤에 붙이는 안내
BATCH_NOTE = """

You are given several emails, numbered (=== EMAIL n ===).
Return ONE item PER email, in order, each with its matching `index` (1-based)."""


class Classification(BaseModel):
    category: Literal[
        "uni", "news", "real_estate", "finance",
        "shopping", "message", "personal", "junk", "other",
    ]
    uni_subtype: Literal[
        "study", "announcement", "newsletter", "admin", "not_uni",
    ]
    reason: str      # why this category (one short sentence)
    summary: str     # 1-line summary of the email


class BatchClassification(BaseModel):
    """배치(한 번의 요청) 분류용 — index로 원본 메일과 짝지음."""
    index: int       # 1-based, 프롬프트의 EMAIL 번호와 일치
    category: Literal[
        "uni", "news", "real_estate", "finance",
        "shopping", "message", "personal", "junk", "other",
    ]
    uni_subtype: Literal[
        "study", "announcement", "newsletter", "admin", "not_uni",
    ]
    topic: Literal[   # message 카테고리의 세부 라벨(message/<topic>)에 사용
        "Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other",
    ]
    reason: str
    summary: str


def get_credentials():
    """OAuth 로그인 자격증명을 반환. 최초 1회(또는 권한 변경 시) 브라우저 승인."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


def get_gmail_service():
    """Gmail API 서비스 객체."""
    return build("gmail", "v1", credentials=get_credentials())


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_body(payload) -> str:
    """메일 본문(text/plain)을 재귀적으로 찾아 반환."""
    if payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode(part["body"]["data"])
    for part in payload.get("parts", []):
        found = _extract_body(part)
        if found:
            return found
    return ""


def get_recent_emails(service, max_results=MAX_EMAILS):
    resp = service.users().messages().list(
        userId="me", maxResults=max_results, q="in:inbox"
    ).execute()
    emails = []
    for m in resp.get("messages", []):
        full = service.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in full["payload"]["headers"]}
        body = _extract_body(full["payload"]) or full.get("snippet", "")
        ts = int(full.get("internalDate", "0"))  # 받은 시각(epoch ms) — 날짜 정렬용
        date_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
        emails.append({
            "id": m["id"],  # 라벨을 붙일 때 필요
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "date": date_str,   # 받은 날짜 (YYYY-MM-DD)
            "ts": ts,           # 정렬용 정수
            "body": body[:4000],  # 토큰 절약을 위해 앞부분만
        })
    return emails


def build_batch_prompt(emails) -> str:
    """여러 메일을 한 프롬프트로 묶는다. (모든 배치 스크립트가 공유)"""
    parts = []
    for i, e in enumerate(emails, 1):
        parts.append(
            f"=== EMAIL {i} ===\n"
            f"From: {e['from']}\n"
            f"Subject: {e['subject']}\n"
            f"Body:\n{e['body'][:2000]}"
        )
    return "\n\n".join(parts)


def load_labels(service) -> dict:
    """계정의 기존 라벨 이름→ID 사전을 만든다."""
    existing = service.users().labels().list(userId="me").execute().get("labels", [])
    return {lbl["name"]: lbl["id"] for lbl in existing}


def get_or_create_label(service, name: str, cache: dict) -> str:
    """라벨이 없으면 만들고 ID를 반환. 'uni/study'처럼 / 가 있으면 상위 라벨도 보장."""
    if name in cache:
        return cache[name]
    if "/" in name:  # 상위 라벨(uni) 먼저 보장
        get_or_create_label(service, name.rsplit("/", 1)[0], cache)
    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    cache[name] = created["id"]
    return created["id"]


def label_name_for(c: "Classification") -> str:
    """분류 결과를 Gmail 라벨 이름으로 변환. uni/message는 세부 태그까지 중첩."""
    if c.category == "uni" and c.uni_subtype in ("study", "announcement", "newsletter", "admin"):
        return f"uni/{c.uni_subtype}"
    if c.category == "message":  # 직접 온 메시지 → message/<주제>
        return f"message/{getattr(c, 'topic', None) or 'Other'}"
    return c.category


def apply_label(service, msg_id: str, label_name: str, cache: dict):
    label_id = get_or_create_label(service, label_name, cache)
    service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


def classify(client, email, retries=4) -> Classification:
    """단건 분류(디버그/호환용). 무료 한도 초과(429) 시 잠깐 기다렸다 자동 재시도."""
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=(
                    f"From: {email['from']}\n"
                    f"Subject: {email['subject']}\n\n"
                    f"Body:\n{email['body']}"
                ),
                config={
                    "system_instruction": CATEGORY_GUIDE,
                    "response_mime_type": "application/json",
                    "response_schema": Classification,
                },
            )
            return resp.parsed  # Classification 인스턴스
        except errors.ClientError as e:
            # 429 = 요청 한도 초과 → 잠시 대기 후 재시도
            if getattr(e, "code", None) == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  (한도 초과 — {wait}초 대기 후 재시도...)")
                time.sleep(wait)
                continue
            raise


def classify_batch(client, emails, retries=4):
    """여러 메일을 '한 번의 요청'으로 분류(무료 한도 절약). 429면 대기 후 재시도."""
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=build_batch_prompt(emails),
                config={
                    "system_instruction": CATEGORY_GUIDE + BATCH_NOTE,
                    "response_mime_type": "application/json",
                    "response_schema": list[BatchClassification],
                },
            )
            return resp.parsed or []
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  (한도 초과 — {wait}초 대기 후 재시도...)")
                time.sleep(wait)
                continue
            raise


def main():
    # GEMINI_API_KEY 환경변수를 자동으로 읽음
    client = genai.Client()
    service = get_gmail_service()
    label_cache = load_labels(service)  # 기존 라벨 목록 미리 로드
    emails = get_recent_emails(service)

    print(f"\n최근 이메일 {len(emails)}개 분류 + 라벨 부착 (한 번의 요청)\n" + "=" * 60)
    results = classify_batch(client, emails)  # 배치: 요청 1건
    for c in results:
        idx = c.index - 1
        if not (0 <= idx < len(emails)):
            continue
        e = emails[idx]
        label_name = label_name_for(c)
        apply_label(service, e["id"], label_name, label_cache)  # Gmail에 라벨 부착
        print(f"\n[{label_name}]  {e['subject']}")
        print(f"  보낸이: {e['from']}")
        print(f"  요약  : {c.summary}")
        print(f"  근거  : {c.reason}")
    print("\n" + "=" * 60)
    print("✅ 완료! Gmail을 새로고침하면 왼쪽 메뉴에 라벨이 보입니다.")


if __name__ == "__main__":
    main()
