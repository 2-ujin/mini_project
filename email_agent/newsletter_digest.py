"""
뉴스레터 요약 웹페이지 생성기 (누적 아카이브 버전).
받은 메일 중 '뉴스레터'만 골라 → 영어 요약(기본) + 한국어 요약(보조) → state/results.html.

특징:
  - 누적 보관: state/newsletters.json 에 계속 쌓음 (id로 중복 제거). 실행마다 사라지지 않음.
  - 날짜순 정렬(최신순).
  - 카드를 누르면 전체 메일 내용을 펼쳐볼 수 있음.
  - 언어: 영어가 기본, 한국어는 카드 아래에 가볍게.
build_html / TOPICS / TOPIC_COLOR / 아카이브 헬퍼는 run_all.py 도 공유한다.
"""

import html
import json
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import errors
from pydantic import BaseModel

import classify as C  # Gmail 로그인/메일 읽기 + build_batch_prompt 재사용

sys.stdout.reconfigure(encoding="utf-8")

MODEL = "gemini-2.5-flash"
MAX_EMAILS = 15
OUTPUT = Path("state/results.html")
ARCHIVE = Path("state/newsletters.json")   # 누적 보관 파일 (Docker 볼륨에 매핑)

TOPICS = ["Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other"]
TOPIC_COLOR = {
    "Uni": "#7c3aed", "AI": "#0891b2", "Career": "#ea580c", "Business": "#2563eb",
    "Tech": "#0d9488", "Finance": "#16a34a", "News": "#dc2626",
    "Lifestyle": "#db2777", "Other": "#64748b",
}

# 발신자 도메인 기반 하드 제외: 여기 해당하면 AI가 뉴스레터라고 해도 results.html 에 넣지 않음.
EXCLUDED_NEWSLETTER_SENDERS = (
    "linkedin.com",
    "seek.com.au",
    "seek.com",
)

GUIDE = """You are given several emails, numbered. Return one item PER email, in order, with matching index.
- is_newsletter: true if it's a newsletter / informational digest / promotional info blast.
  false for personal mail, receipts, e-signing requests, bank/payment alerts, one-to-one agent messages.
  IMPORTANT: job-alert / job-recommendation digests from LinkedIn or Seek (seek.com.au) are NOT
  newsletters → is_newsletter=false.
- topic: best fit — Uni (university/study), AI, Career (jobs/recruiting), Business, Tech, Finance,
  News (general news), Lifestyle, Other.
- english_title: a short, natural English title.
- english_summary: 2-3 sentence summary in natural English (the main content).
- korean_title: a short, natural Korean title.
- korean_summary: 1-2 sentence brief summary in Korean."""


class DigestItem(BaseModel):
    index: int          # 1-based, matches the email number in the prompt
    is_newsletter: bool
    topic: Literal["Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other"]
    english_title: str
    english_summary: str
    korean_title: str
    korean_summary: str


def _tidy_body(text: str) -> str:
    """전체 메일 표시용으로 본문을 정리: 앞뒤 공백 제거, 과한 빈 줄(3줄+)을 2줄로,
    줄 끝 공백 제거. (읽기 편하게)"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)   # 빈 줄 연속 → 최대 한 줄
    return text


def is_excluded_newsletter_sender(from_field: str) -> bool:
    """발신자가 하드 제외 목록(LinkedIn/Seek 등)에 해당하면 True."""
    f = (from_field or "").lower()
    return any(dom in f for dom in EXCLUDED_NEWSLETTER_SENDERS)


def select_newsletters(items, emails):
    """AI 결과(items)에서 뉴스레터만, 그리고 제외 발신자를 뺀 (item, email) 목록을 만든다."""
    out = []
    for item in items:
        idx = item.index - 1
        if not (0 <= idx < len(emails)):
            continue
        if not item.is_newsletter:
            continue
        email = emails[idx]
        if is_excluded_newsletter_sender(email.get("from", "")):
            continue  # LinkedIn/Seek 채용 다이제스트 → 제외
        out.append((item, email))
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  누적 아카이브 (state/newsletters.json)
# ─────────────────────────────────────────────────────────────────────────────
def _to_entry(item, email) -> dict:
    """(AI 결과 item, 원본 email) → 아카이브에 저장할 dict. item 은 english_* 필드가 없어도 동작."""
    return {
        "id": email.get("id", ""),
        "ts": email.get("ts", 0),
        "date": email.get("date", ""),
        "topic": getattr(item, "topic", "Other"),
        "en_title": getattr(item, "english_title", "") or getattr(item, "korean_title", ""),
        "en_summary": getattr(item, "english_summary", "") or getattr(item, "korean_summary", ""),
        "ko_title": getattr(item, "korean_title", ""),
        "ko_summary": getattr(item, "korean_summary", ""),
        "subject": email.get("subject", ""),
        "from": email.get("from", ""),
        "body": email.get("body", ""),
    }


def load_archive() -> list:
    if ARCHIVE.exists():
        try:
            return json.loads(ARCHIVE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_archive(archive: list):
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_into_archive(archive: list, pairs) -> list:
    """새 뉴스레터(item,email) 목록을 아카이브에 누적. id로 중복 제거."""
    seen = {e.get("id") for e in archive}
    for item, email in pairs:
        eid = email.get("id", "")
        if eid and eid not in seen:
            archive.append(_to_entry(item, email))
            seen.add(eid)
    return archive


def analyze(client, emails, retries=4):
    """한 번의 요청으로 모든 메일을 분석. 429면 잠깐 대기 후 재시도."""
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=C.build_batch_prompt(emails),
                config={
                    "system_instruction": GUIDE,
                    "response_mime_type": "application/json",
                    "response_schema": list[DigestItem],
                },
            )
            return resp.parsed or []
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429 and attempt < retries - 1:
                print("  (무료 한도 대기 — 25초 후 재시도...)")
                time.sleep(25)
                continue
            raise


# ─────────────────────────────────────────────────────────────────────────────
#  HTML 생성 — 날짜순 플랫 리스트, 영어 기본 + 한국어 보조, 전체 메일 펼치기, 주제 필터
#  run_all.py 와 newsletter_digest.py 가 공유한다. 인자는 아카이브 entry(dict) 리스트.
# ─────────────────────────────────────────────────────────────────────────────
_PAGE_CSS = """
  :root{
    --bg:#f4f5f7; --panel:#ffffff; --panel-2:#f7f8fa; --border:#e5e7eb;
    --text:#111827; --text-2:#4b5563; --text-3:#9ca3af;
    --ko-bg:#f8fafc; --ko-border:#eef2f7;
    --shadow:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.04);
    --shadow-hover:0 4px 12px rgba(16,24,40,.10);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0b1220; --panel:#141c2b; --panel-2:#0f1626; --border:#273244;
      --text:#e8edf5; --text-2:#aeb9c9; --text-3:#6b7688;
      --ko-bg:#0f1626; --ko-border:#22304a;
      --shadow:0 1px 2px rgba(0,0,0,.4); --shadow-hover:0 6px 18px rgba(0,0,0,.5);
    }
  }
  *{ box-sizing:border-box; }
  html{ -webkit-text-size-adjust:100%; }
  body{
    font-family:system-ui,-apple-system,'Segoe UI','Malgun Gothic','맑은 고딕','Apple SD Gothic Neo',sans-serif;
    margin:0; background:var(--bg); color:var(--text); line-height:1.6;
    font-size:15px; -webkit-font-smoothing:antialiased;
  }
  header{ background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%); color:#fff; padding:26px 20px 20px; }
  .head-inner{ max-width:820px; margin:0 auto; }
  header h1{ margin:0; font-size:21px; font-weight:700; letter-spacing:-.2px; }
  header .sub{ margin:6px 0 0; color:#94a3b8; font-size:13px; }
  .stats{ display:flex; gap:16px; margin-top:14px; flex-wrap:wrap; }
  .stat{ font-size:13px; color:#cbd5e1; } .stat b{ color:#fff; font-size:15px; }
  .filters{ position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--border); padding:12px 20px; }
  .filters-inner{ max-width:820px; margin:0 auto; display:flex; gap:8px; overflow-x:auto; -webkit-overflow-scrolling:touch; padding-bottom:2px; }
  .filters-inner::-webkit-scrollbar{ height:0; }
  .chip{
    flex:0 0 auto; cursor:pointer; border:1.5px solid var(--border); background:var(--panel);
    color:var(--text-2); border-radius:999px; padding:6px 13px; font-size:13px; font-weight:600;
    font-family:inherit; display:inline-flex; align-items:center; gap:7px; white-space:nowrap;
    transition:all .15s ease; user-select:none;
  }
  .chip:hover{ border-color:var(--text-3); }
  .chip .cdot{ width:9px; height:9px; border-radius:50%; flex:0 0 auto; }
  .chip .cnum{ font-size:11px; color:var(--text-3); font-weight:700; }
  .chip[aria-pressed="true"]{ color:#fff; border-color:transparent; }
  .chip[aria-pressed="true"] .cnum{ color:rgba(255,255,255,.85); }
  .chip[aria-pressed="true"] .cdot{ background:#fff !important; }
  main{ max-width:820px; margin:0 auto; padding:14px 20px 60px; }
  .item{
    background:var(--panel); border:1px solid var(--border); border-left:4px solid var(--border);
    border-radius:12px; padding:15px 17px; margin-bottom:11px; box-shadow:var(--shadow);
    transition:box-shadow .15s;
  }
  .item:hover{ box-shadow:var(--shadow-hover); }
  .item-top{ display:flex; align-items:center; gap:8px; margin-bottom:7px; flex-wrap:wrap; }
  .badge{ display:inline-block; color:#fff; font-size:11px; font-weight:700; padding:2px 9px; border-radius:999px; letter-spacing:.2px; }
  .date{ font-size:12px; color:var(--text-3); font-weight:600; margin-left:auto; }
  .item h3{ margin:0; font-size:16.5px; font-weight:700; line-height:1.45; letter-spacing:-.2px; color:var(--text); }
  .summary{ margin:6px 0 10px; color:var(--text-2); font-size:14.5px; }
  .ko{ background:var(--ko-bg); border:1px solid var(--ko-border); border-radius:9px; padding:9px 11px; margin:0 0 10px; }
  .ko-title{ font-size:12.5px; font-weight:700; color:var(--text-2); margin-bottom:2px; }
  .ko-summary{ font-size:12.5px; color:var(--text-3); line-height:1.55; }
  details.full{ margin:0 0 10px; }
  details.full > summary{
    cursor:pointer; font-size:12.5px; font-weight:600; color:var(--text-2);
    list-style:none; display:inline-flex; align-items:center; gap:6px;
    padding:5px 10px; border:1px solid var(--border); border-radius:8px; background:var(--panel-2);
    user-select:none;
  }
  details.full > summary::-webkit-details-marker{ display:none; }
  details.full > summary:hover{ border-color:var(--text-3); }
  details.full[open] > summary{ margin-bottom:8px; }
  .body{
    white-space:pre-wrap; word-wrap:break-word; overflow-wrap:anywhere; max-height:360px; overflow:auto;
    font-family:inherit; font-size:13.5px; line-height:1.7;
    color:var(--text-2); background:var(--panel-2); border:1px solid var(--border);
    border-radius:8px; padding:13px 15px; margin:0;
  }
  .body a{ color:#2563eb; word-break:break-all; }
  .meta{ display:flex; flex-direction:column; gap:3px; font-size:12px; color:var(--text-3); border-top:1px dashed var(--border); padding-top:9px; }
  .meta .row{ display:flex; gap:6px; align-items:baseline; min-width:0; }
  .meta .lbl{ flex:0 0 auto; opacity:.75; }
  .meta .val{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .empty{ text-align:center; color:var(--text-3); padding:60px 20px; background:var(--panel); border:1px dashed var(--border); border-radius:14px; margin-top:24px; }
  .empty .emo{ font-size:34px; display:block; margin-bottom:10px; }
  .no-match{ text-align:center; color:var(--text-3); padding:40px; display:none; }
  footer{ text-align:center; color:var(--text-3); font-size:12px; padding:0 20px 34px; }
  @media (max-width:520px){
    header h1{ font-size:18px; } .item h3{ font-size:15.5px; }
    main{ padding:12px 14px 50px; } .filters{ padding:10px 14px; } header{ padding:20px 16px 16px; }
  }
"""

_PAGE_SCRIPT = """
  var chips = document.querySelectorAll('.chip');
  var items = document.querySelectorAll('.item');
  var noMatch = document.getElementById('noMatch');
  function applyFilter(f){
    var shown = 0;
    items.forEach(function(it){
      var on = (f === 'all' || it.getAttribute('data-topic') === f);
      it.style.display = on ? '' : 'none';
      if(on) shown++;
    });
    if(noMatch) noMatch.style.display = shown ? 'none' : 'block';
  }
  chips.forEach(function(chip){
    chip.addEventListener('click', function(){
      chips.forEach(function(c){
        c.setAttribute('aria-pressed','false');
        if(c.getAttribute('data-filter') !== 'all') c.style.background='';
      });
      chip.setAttribute('aria-pressed','true');
      var c = chip.style.getPropertyValue('--c');
      if(c) chip.style.background = c;
      applyFilter(chip.getAttribute('data-filter'));
    });
  });
"""


def build_html(entries: list) -> str:
    """아카이브 entry(dict) 리스트로 완성된 HTML 생성. 날짜(ts) 최신순 정렬, 영어 기본 + 한국어 보조."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entries = sorted(entries, key=lambda e: e.get("ts", 0), reverse=True)  # 최신순
    total = len(entries)

    # 등장한 주제(개수 세기, TOPICS 순서로 칩 표시)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.get("topic", "Other")] = counts.get(e.get("topic", "Other"), 0) + 1
    present_topics = [t for t in TOPICS if t in counts]

    if total == 0:
        main_html = '<div class="empty"><span class="emo">📭</span>No newsletters yet. 아직 뉴스레터가 없습니다.</div>'
        chips_html = '<button class="chip" data-filter="all" aria-pressed="true" style="background:#334155;">All <span class="cnum">0</span></button>'
    else:
        chips = ['<button class="chip" data-filter="all" aria-pressed="true" '
                 f'style="background:#334155;">All <span class="cnum">{total}</span></button>']
        for t in present_topics:
            color = TOPIC_COLOR.get(t, "#64748b")
            chips.append(
                f'<button class="chip" data-filter="{html.escape(t)}" aria-pressed="false" '
                f'style="--c:{color};"><span class="cdot" style="background:{color}"></span>'
                f'{html.escape(t)} <span class="cnum">{counts[t]}</span></button>'
            )
        chips_html = "\n    ".join(chips)

        cards = []
        for e in entries:
            t = e.get("topic", "Other")
            color = TOPIC_COLOR.get(t, "#64748b")
            en_title = html.escape(e.get("en_title", "") or "(no title)")
            en_summary = html.escape(e.get("en_summary", ""))
            ko_title = html.escape(e.get("ko_title", ""))
            ko_summary = html.escape(e.get("ko_summary", ""))
            date = html.escape(e.get("date", ""))
            subject = html.escape(e.get("subject", ""))
            sender = html.escape(e.get("from", ""))
            body = html.escape(_tidy_body(e.get("body", "")) or "(본문을 가져오지 못했습니다.)")
            ko_block = ""
            if ko_title or ko_summary:
                ko_block = (f'      <div class="ko"><div class="ko-title">🇰🇷 {ko_title}</div>'
                            f'<div class="ko-summary">{ko_summary}</div></div>\n')
            cards.append(f"""    <article class="item" data-topic="{html.escape(t)}" style="border-left-color:{color}">
      <div class="item-top">
        <span class="badge" style="background:{color}">{html.escape(t)}</span>
        <span class="date">{date}</span>
      </div>
      <h3>{en_title}</h3>
      <p class="summary">{en_summary}</p>
{ko_block}      <details class="full"><summary>📄 Read full email · 전체 메일 보기</summary><div class="body">{body}</div></details>
      <div class="meta">
        <div class="row"><span class="lbl">📧</span><span class="val">{subject}</span></div>
        <div class="row"><span class="lbl">✉️</span><span class="val">{sender}</span></div>
      </div>
    </article>""")
        main_html = "\n".join(cards) + '\n  <p class="no-match" id="noMatch">No newsletters in this topic.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Newsletter Digest - {now}</title>
<style>{_PAGE_CSS}</style></head>
<body>
<header>
  <div class="head-inner">
    <h1>📰 Newsletter Digest</h1>
    <p class="sub">Updated: {now} · sorted by date · click a card to read the full email</p>
    <div class="stats">
      <span class="stat"><b>{total}</b> newsletters</span>
      <span class="stat"><b>{len(present_topics)}</b> topics</span>
    </div>
  </div>
</header>
<nav class="filters" aria-label="topic filter">
  <div class="filters-inner" id="chips">
    {chips_html}
  </div>
</nav>
<main id="list">
{main_html}
</main>
<footer>Tap a topic chip to filter · newsletters accumulate over time · opens as a local file</footer>
<script>{_PAGE_SCRIPT}</script>
</body></html>"""


def main():
    client = genai.Client()
    service = C.get_gmail_service()
    emails = C.get_recent_emails(service, max_results=MAX_EMAILS)

    print(f"최근 메일 {len(emails)}개를 한 번에 분석 중...")
    items = analyze(client, emails)

    pairs = select_newsletters(items, emails)   # 뉴스레터만 + 제외 발신자 걸러냄
    archive = merge_into_archive(load_archive(), pairs)  # 누적
    save_archive(archive)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(archive), encoding="utf-8")
    print(f"✅ 이번 +{len(pairs)}개, 누적 {len(archive)}개 → {OUTPUT.resolve()}")
    webbrowser.open(OUTPUT.resolve().as_uri())  # 브라우저로 열기


if __name__ == "__main__":
    main()
