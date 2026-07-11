"""
메일 요약 웹페이지 생성기.
 - 화면 목록: 뉴스레터만 (영어 요약 + 한국어, 날짜순, 카드 클릭 시 전체 본문).
 - 분석(전체 메일 기준, 누적): 카테고리 파이차트 + 카테고리별 키워드 차트.
데이터는 state/emails.json 에 '모든 메일'을 누적(id로 중복 제거) — 차트는 전체 기준, 목록은 뉴스레터만.
차트는 외부 라이브러리 없이 순수 SVG/CSS 로 그려 로컬 파일에서도 동작.
"""

import html
import json
import math
import re
import sys
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import errors
from pydantic import BaseModel

import classify as C

sys.stdout.reconfigure(encoding="utf-8")

MODEL = "gemini-2.5-flash"
MAX_EMAILS = 15
OUTPUT = Path("state/results.html")
INBOX = Path("state/emails.json")            # 전체 메일 누적(분석용)
OLD_NEWS = Path("state/newsletters.json")    # 예전 뉴스레터 파일(하위호환 마이그레이션)

TOPICS = ["Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other"]
TOPIC_COLOR = {
    "Uni": "#7c3aed", "AI": "#0891b2", "Career": "#ea580c", "Business": "#2563eb",
    "Tech": "#0d9488", "Finance": "#16a34a", "News": "#dc2626",
    "Lifestyle": "#db2777", "Other": "#64748b",
}
CATEGORY_COLOR = {
    "uni": "#7c3aed", "message": "#2563eb", "news": "#dc2626", "real_estate": "#0d9488",
    "finance": "#16a34a", "shopping": "#db2777", "personal": "#ea580c",
    "junk": "#64748b", "other": "#94a3b8",
}
CATEGORY_KO = {
    "uni": "학교", "message": "메시지", "news": "뉴스", "real_estate": "부동산",
    "finance": "금융", "shopping": "쇼핑", "personal": "개인", "junk": "정크", "other": "기타",
}
CATEGORY_ORDER = ["uni", "message", "news", "real_estate", "finance", "shopping", "personal", "junk", "other"]

EXCLUDED_NEWSLETTER_SENDERS = ("linkedin.com", "seek.com.au", "seek.com")

# 키워드 추출 시 제외할 흔한 단어
STOPWORDS = set("""the a an and or of to in for on at is are be been being was were do does did
you your yours we our us it its this that these those with from as by not no new now get got
have has had will would can could should may might must re fwd fw please thanks thank hi hello
dear regards best team via about out up off all any more most your you’re let’s
email mail message inbox notification update updates info information href http https www com
au your qut edu""".split())
KO_STOP = {"있습니다", "합니다", "입니다", "그리고", "위한", "대한", "관련", "안내", "그대로", "여기"}


def is_excluded_newsletter_sender(from_field: str) -> bool:
    f = (from_field or "").lower()
    return any(dom in f for dom in EXCLUDED_NEWSLETTER_SENDERS)


def _tidy_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text)


def _cat_of(label: str) -> str:
    return (label or "other").split("/", 1)[0]


class DigestItem(BaseModel):
    index: int
    is_newsletter: bool
    topic: Literal["Uni", "AI", "Career", "Business", "Tech", "Finance", "News", "Lifestyle", "Other"]
    english_title: str
    english_summary: str
    korean_title: str
    korean_summary: str


# ── 아카이브 (전체 메일) ──────────────────────────────────────────────────────
def make_entry(a, email, label: str) -> dict:
    return {
        "id": email.get("id", ""),
        "ts": email.get("ts", 0),
        "date": email.get("date", ""),
        "label": label,
        "category": getattr(a, "category", _cat_of(label)),
        "is_newsletter": bool(getattr(a, "is_newsletter", False)),
        "topic": getattr(a, "topic", "Other"),
        "en_title": getattr(a, "english_title", "") or getattr(a, "korean_title", ""),
        "en_summary": getattr(a, "english_summary", "") or getattr(a, "korean_summary", ""),
        "ko_title": getattr(a, "korean_title", ""),
        "ko_summary": getattr(a, "korean_summary", ""),
        "subject": email.get("subject", ""),
        "from": email.get("from", ""),
        "body": email.get("body", ""),
    }


def load_inbox() -> list:
    if INBOX.exists():
        try:
            return json.loads(INBOX.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    if OLD_NEWS.exists():
        try:
            data = json.loads(OLD_NEWS.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        out = []
        for x in data:
            cat = "uni" if x.get("topic") == "Uni" else "news"
            x = dict(x)
            x.setdefault("is_newsletter", True)
            x["category"] = cat
            x["label"] = "uni/newsletter" if cat == "uni" else "news"
            out.append(x)
        return out
    return []


def save_inbox(inbox: list):
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_inbox(inbox: list, entries: list) -> list:
    seen = {e.get("id") for e in inbox}
    for e in entries:
        eid = e.get("id", "")
        if eid and eid not in seen:
            inbox.append(e)
            seen.add(eid)
    return inbox


def select_newsletters(items, emails):
    out = []
    for item in items:
        idx = item.index - 1
        if 0 <= idx < len(emails) and item.is_newsletter and not is_excluded_newsletter_sender(emails[idx].get("from", "")):
            out.append((item, emails[idx]))
    return out


def analyze(client, emails, retries=4):
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


GUIDE = """You are given several emails, numbered. Return one item PER email, in order, with matching index.
- is_newsletter: true if newsletter/informational digest/promo blast; false for personal, receipts,
  e-signing, bank alerts, one-to-one messages. LinkedIn/Seek job digests are NOT newsletters.
- topic: Uni, AI, Career, Business, Tech, Finance, News, Lifestyle, Other.
- english_title/english_summary + korean_title/korean_summary."""


# ── 분석 계산 ────────────────────────────────────────────────────────────────
def _keywords_by(entries, key_field, per=6) -> dict:
    """key_field(예: 'topic')별 상위 키워드 [(word,count),...]. 제목+영문요약에서 추출(영문/한글 토큰)."""
    buckets: dict[str, Counter] = {}
    for e in entries:
        k = e.get(key_field) or "Other"
        text = " ".join([e.get("subject", ""), e.get("en_title", ""), e.get("en_summary", "")]).lower()
        words = re.findall(r"[a-z][a-z0-9+#]{2,}|[가-힣]{2,}", text)
        cnt = buckets.setdefault(k, Counter())
        for w in words:
            if w in STOPWORDS or w in KO_STOP:
                continue
            cnt[w] += 1
    return {k: cnt.most_common(per) for k, cnt in buckets.items()}


def _pie_svg(counts: dict, color_map: dict, name_map: dict, order: list, unit: str = "items") -> str:
    """개수 dict → 파이차트 SVG + 범례 HTML (색/이름/순서를 인자로 받아 범용)."""
    total = sum(counts.values())
    if total == 0:
        return '<p class="muted">데이터가 없습니다.</p>'
    items = [(c, counts[c]) for c in order if c in counts]
    items += [(c, n) for c, n in counts.items() if c not in order]
    cx = cy = 100.0
    r = 92.0
    if len(items) == 1:
        c0 = items[0][0]
        slices = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color_map.get(c0, "#94a3b8")}"/>'
    else:
        ang = -90.0
        parts = []
        for cat, n in items:
            sweep = n / total * 360.0
            a2 = ang + sweep
            x1 = cx + r * math.cos(math.radians(ang))
            y1 = cy + r * math.sin(math.radians(ang))
            x2 = cx + r * math.cos(math.radians(a2))
            y2 = cy + r * math.sin(math.radians(a2))
            large = 1 if sweep > 180 else 0
            parts.append(
                f'<path d="M{cx:.1f} {cy:.1f} L{x1:.2f} {y1:.2f} '
                f'A{r} {r} 0 {large} 1 {x2:.2f} {y2:.2f} Z" fill="{color_map.get(cat, "#94a3b8")}"/>'
            )
            ang = a2
        slices = "".join(parts)
    svg = (f'<svg class="pie" viewBox="0 0 200 200" width="180" height="180" '
           f'role="img" aria-label="분포">{slices}'
           f'<circle cx="{cx}" cy="{cy}" r="46" fill="var(--panel)"/>'
           f'<text x="100" y="96" text-anchor="middle" class="pie-c1">{total}</text>'
           f'<text x="100" y="114" text-anchor="middle" class="pie-c2">{html.escape(unit)}</text></svg>')
    legend = []
    for cat, n in items:
        pct = round(n / total * 100)
        col = color_map.get(cat, "#94a3b8")
        legend.append(
            f'<div class="lg-row"><span class="lg-dot" style="background:{col}"></span>'
            f'<span class="lg-name">{html.escape(name_map.get(cat, cat))}</span>'
            f'<span class="lg-num">{n} · {pct}%</span></div>'
        )
    return f'<div class="pie-wrap">{svg}<div class="legend">{"".join(legend)}</div></div>'


def _keyword_blocks(entries, key_field: str, color_map: dict, name_map: dict, order: list) -> str:
    kw = _keywords_by(entries, key_field)
    keys = [k for k in order if k in kw] + [k for k in kw if k not in order]
    blocks = []
    for k in keys:
        pairs = kw.get(k, [])
        if not pairs:
            continue
        col = color_map.get(k, "#94a3b8")
        mx = max(c for _, c in pairs) or 1
        bars = []
        for word, cnt in pairs:
            w = round(cnt / mx * 100)
            bars.append(
                f'<div class="kw-row"><span class="kw-word">{html.escape(word)}</span>'
                f'<span class="kw-track"><span class="kw-fill" style="width:{w}%;background:{col}"></span></span>'
                f'<span class="kw-num">{cnt}</span></div>'
            )
        blocks.append(
            f'<div class="kw-cat"><div class="kw-head"><span class="lg-dot" style="background:{col}"></span>'
            f'{html.escape(name_map.get(k, k))}</div>{"".join(bars)}</div>'
        )
    return f'<div class="kw-grid">{"".join(blocks)}</div>' if blocks else '<p class="muted">키워드가 없습니다.</p>'


# ── HTML ────────────────────────────────────────────────────────────────────
_CSS = """
  :root{--bg:#f4f5f7;--panel:#fff;--panel-2:#f7f8fa;--border:#e5e7eb;--text:#111827;--text-2:#4b5563;
    --text-3:#9ca3af;--ko-bg:#f8fafc;--ko-border:#eef2f7;--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.04);--shadow-h:0 4px 12px rgba(16,24,40,.10);}
  @media (prefers-color-scheme:dark){:root{--bg:#0b1220;--panel:#141c2b;--panel-2:#0f1626;--border:#273244;
    --text:#e8edf5;--text-2:#aeb9c9;--text-3:#6b7688;--ko-bg:#0f1626;--ko-border:#22304a;--shadow:0 1px 2px rgba(0,0,0,.4);--shadow-h:0 6px 18px rgba(0,0,0,.5);}}
  *{box-sizing:border-box;} html{-webkit-text-size-adjust:100%;}
  body{font-family:system-ui,-apple-system,'Segoe UI','Malgun Gothic','맑은 고딕','Apple SD Gothic Neo',sans-serif;
    margin:0;background:var(--bg);color:var(--text);line-height:1.6;font-size:15px;-webkit-font-smoothing:antialiased;}
  header{background:linear-gradient(135deg,#1e293b,#0f172a);color:#fff;padding:24px 20px 20px;}
  .head-inner{max-width:860px;margin:0 auto;}
  header h1{margin:0;font-size:21px;font-weight:700;letter-spacing:-.2px;}
  header .sub{margin:6px 0 0;color:#94a3b8;font-size:13px;}
  main{max-width:860px;margin:0 auto;padding:18px 20px 60px;}
  .card-sec{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px 18px 16px;margin-bottom:18px;box-shadow:var(--shadow);}
  .sec-title{margin:0 0 14px;font-size:16px;font-weight:700;display:flex;align-items:baseline;gap:8px;}
  .sec-title small{font-size:12px;color:var(--text-3);font-weight:600;}
  .muted{color:var(--text-3);text-align:center;padding:20px;}
  /* pie */
  .pie-wrap{display:flex;gap:22px;align-items:center;flex-wrap:wrap;}
  .pie-c1{font-size:30px;font-weight:800;fill:var(--text);}
  .pie-c2{font-size:11px;fill:var(--text-3);letter-spacing:1px;}
  .legend{display:grid;grid-template-columns:1fr 1fr;gap:6px 18px;flex:1;min-width:220px;}
  .lg-row{display:flex;align-items:center;gap:8px;font-size:13px;}
  .lg-dot{width:11px;height:11px;border-radius:3px;flex:0 0 auto;}
  .lg-name{color:var(--text-2);} .lg-num{margin-left:auto;color:var(--text-3);font-weight:600;font-size:12px;}
  /* keywords */
  .kw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;}
  .kw-cat{border:1px solid var(--border);border-radius:11px;padding:11px 12px;background:var(--panel-2);}
  .kw-head{display:flex;align-items:center;gap:7px;font-size:13.5px;font-weight:700;margin-bottom:9px;color:var(--text-2);}
  .kw-row{display:flex;align-items:center;gap:8px;margin:5px 0;}
  .kw-word{flex:0 0 84px;font-size:12.5px;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .kw-track{flex:1;height:8px;background:var(--border);border-radius:99px;overflow:hidden;}
  .kw-fill{display:block;height:100%;border-radius:99px;}
  .kw-num{flex:0 0 auto;font-size:11.5px;color:var(--text-3);font-weight:700;width:20px;text-align:right;}
  /* newsletter filters + cards */
  .filters-inner{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;margin-bottom:6px;}
  .filters-inner::-webkit-scrollbar{height:0;}
  .chip{flex:0 0 auto;cursor:pointer;border:1.5px solid var(--border);background:var(--panel-2);color:var(--text-2);
    border-radius:999px;padding:5px 12px;font-size:13px;font-weight:600;font-family:inherit;display:inline-flex;
    align-items:center;gap:6px;white-space:nowrap;transition:all .15s;user-select:none;}
  .chip .cdot{width:9px;height:9px;border-radius:50%;} .chip .cnum{font-size:11px;color:var(--text-3);font-weight:700;}
  .chip[aria-pressed="true"]{color:#fff;border-color:transparent;}
  .chip[aria-pressed="true"] .cnum{color:rgba(255,255,255,.85);} .chip[aria-pressed="true"] .cdot{background:#fff !important;}
  .item{background:var(--panel);border:1px solid var(--border);border-left:4px solid var(--border);border-radius:12px;
    padding:14px 16px;margin-bottom:10px;box-shadow:var(--shadow);transition:box-shadow .15s;}
  .item:hover{box-shadow:var(--shadow-h);}
  .item-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}
  .badge{display:inline-block;color:#fff;font-size:11px;font-weight:700;padding:2px 9px;border-radius:999px;}
  .date{font-size:12px;color:var(--text-3);font-weight:600;margin-left:auto;}
  .item h3{margin:0;font-size:16px;font-weight:700;line-height:1.45;color:var(--text);}
  .summary{margin:5px 0 9px;color:var(--text-2);font-size:14px;}
  .ko{background:var(--ko-bg);border:1px solid var(--ko-border);border-radius:9px;padding:8px 11px;margin:0 0 9px;}
  .ko-title{font-size:12.5px;font-weight:700;color:var(--text-2);margin-bottom:2px;} .ko-summary{font-size:12.5px;color:var(--text-3);line-height:1.55;}
  details.full{margin:0 0 9px;}
  details.full>summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--text-2);list-style:none;display:inline-flex;
    align-items:center;gap:6px;padding:5px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel-2);}
  details.full>summary::-webkit-details-marker{display:none;} details.full[open]>summary{margin-bottom:8px;}
  .body{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;max-height:360px;overflow:auto;font-family:inherit;
    font-size:13.5px;line-height:1.7;color:var(--text-2);background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:13px 15px;margin:0;}
  .meta{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--text-3);border-top:1px dashed var(--border);padding-top:8px;}
  .meta .row{display:flex;gap:6px;align-items:baseline;min-width:0;} .meta .lbl{flex:0 0 auto;opacity:.75;}
  .meta .val{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .empty{text-align:center;color:var(--text-3);padding:40px 20px;} .empty .emo{font-size:32px;display:block;margin-bottom:10px;}
  .no-match{text-align:center;color:var(--text-3);padding:30px;display:none;}
  footer{text-align:center;color:var(--text-3);font-size:12px;padding:6px 20px 34px;}
  @media (max-width:520px){header h1{font-size:18px;} .legend{grid-template-columns:1fr;} main{padding:14px 14px 50px;}}
"""

_SCRIPT = """
  var bar=document.getElementById('nl-filter');
  if(bar){
    var chips=bar.querySelectorAll('.chip');
    var list=document.getElementById('nl-list');
    var noMatch=document.getElementById('nl-nomatch');
    chips.forEach(function(chip){
      chip.addEventListener('click',function(){
        chips.forEach(function(c){c.setAttribute('aria-pressed','false'); if(c.getAttribute('data-filter')!=='all') c.style.background='';});
        chip.setAttribute('aria-pressed','true');
        var col=chip.style.getPropertyValue('--c'); if(col) chip.style.background=col;
        var f=chip.getAttribute('data-filter'),shown=0;
        list.querySelectorAll('.item').forEach(function(it){
          var on=(f==='all'||it.getAttribute('data-topic')===f); it.style.display=on?'':'none'; if(on)shown++;
        });
        if(noMatch) noMatch.style.display=shown?'none':'block';
      });
    });
  }
"""


def _news_card(e: dict) -> str:
    t = e.get("topic", "Other")
    color = TOPIC_COLOR.get(t, "#64748b")
    en_title = html.escape(e.get("en_title", "") or "(no title)")
    en = html.escape(e.get("en_summary", ""))
    ko_title = html.escape(e.get("ko_title", ""))
    ko_sum = html.escape(e.get("ko_summary", ""))
    date = html.escape(e.get("date", ""))
    subject = html.escape(e.get("subject", ""))
    sender = html.escape(e.get("from", ""))
    body = html.escape(_tidy_body(e.get("body", "")) or "(본문을 가져오지 못했습니다.)")
    ko = ""
    if ko_title or ko_sum:
        ko = f'      <div class="ko"><div class="ko-title">🇰🇷 {ko_title}</div><div class="ko-summary">{ko_sum}</div></div>\n'
    return f"""    <article class="item" data-topic="{html.escape(t)}" style="border-left-color:{color}">
      <div class="item-top"><span class="badge" style="background:{color}">{html.escape(t)}</span><span class="date">{date}</span></div>
      <h3>{en_title}</h3>
      <p class="summary">{en}</p>
{ko}      <details class="full"><summary>📄 Read full email · 전체 메일 보기</summary><div class="body">{body}</div></details>
      <div class="meta"><div class="row"><span class="lbl">📧</span><span class="val">{subject}</span></div>
        <div class="row"><span class="lbl">✉️</span><span class="val">{sender}</span></div></div>
    </article>"""


def build_page(entries: list) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 분석·목록 모두 '뉴스레터만' 대상, 주제(topic)별
    news = sorted(
        [e for e in entries if e.get("is_newsletter") and not is_excluded_newsletter_sender(e.get("from", ""))],
        key=lambda e: e.get("ts", 0), reverse=True,
    )
    topic_name = {t: t for t in TOPICS}
    topic_counts = Counter(e.get("topic", "Other") for e in news)
    pie = _pie_svg(topic_counts, TOPIC_COLOR, topic_name, TOPICS, "newsletters")
    kw = _keyword_blocks(news, "topic", TOPIC_COLOR, topic_name, TOPICS)

    chips = ['<button class="chip" data-filter="all" aria-pressed="true" style="background:#334155;">All '
             f'<span class="cnum">{len(news)}</span></button>']
    for t in TOPICS:
        if t in topic_counts:
            col = TOPIC_COLOR.get(t, "#64748b")
            chips.append(f'<button class="chip" data-filter="{t}" aria-pressed="false" style="--c:{col};">'
                         f'<span class="cdot" style="background:{col}"></span>{t} <span class="cnum">{topic_counts[t]}</span></button>')
    if news:
        news_html = ("\n".join(_news_card(e) for e in news)
                     + '\n    <p class="no-match" id="nl-nomatch">이 주제의 뉴스레터가 없습니다.</p>')
        filter_html = f'<div class="filters-inner" id="nl-filter">{"".join(chips)}</div>'
    else:
        news_html = '<div class="empty"><span class="emo">📭</span>아직 뉴스레터가 없습니다.</div>'
        filter_html = ""

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>뉴스레터 분석 - {now}</title>
<style>{_CSS}</style></head>
<body>
<header><div class="head-inner">
  <h1>📰 뉴스레터 분석</h1>
  <p class="sub">업데이트: {now} · 뉴스레터 {len(news)}개 · 주제별 분석</p>
</div></header>
<main>
  <section class="card-sec">
    <h2 class="sec-title">📊 주제별 분포 <small>(뉴스레터 {len(news)}개)</small></h2>
    {pie}
  </section>
  <section class="card-sec">
    <h2 class="sec-title">🔑 주제별 키워드</h2>
    {kw}
  </section>
  <section class="card-sec">
    <h2 class="sec-title">📰 뉴스레터 목록 <small>({len(news)}개)</small></h2>
    {filter_html}
    <div id="nl-list">
{news_html}
    </div>
  </section>
</main>
<footer>뉴스레터 주제별 분석 · 로컬 파일로 열림 · 메일이 쌓일수록 정확해져요</footer>
<script>{_SCRIPT}</script>
</body></html>"""


def build_html(entries: list) -> str:  # 하위호환 별칭
    return build_page(entries)


def main():
    client = genai.Client()
    service = C.get_gmail_service()
    emails = C.get_recent_emails(service, max_results=MAX_EMAILS)
    print(f"최근 메일 {len(emails)}개를 한 번에 분석 중...")
    items = analyze(client, emails)

    entries = []
    for it in items:
        idx = it.index - 1
        if not (0 <= idx < len(emails)):
            continue
        label = "news"
        if it.is_newsletter and it.topic == "Uni":
            label = "uni/newsletter"
        entries.append(make_entry(it, emails[idx], label))
    inbox = merge_inbox(load_inbox(), entries)
    save_inbox(inbox)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_page(inbox), encoding="utf-8")
    print(f"✅ 누적 {len(inbox)}개(뉴스레터만 목록 표시) → {OUTPUT.resolve()}")
    webbrowser.open(OUTPUT.resolve().as_uri())


if __name__ == "__main__":
    main()
