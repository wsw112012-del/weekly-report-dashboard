"""daily_monitor_post.py — 매일 수집 직후 Flow에 AML 데일리 뉴스 모니터링 게시글 1건.

파이프라인:
  1. Supabase에서 오늘 AML 보도/언론기사 로드
  2. priority.get_priority() == "상" 1차 컷
  3. 노이즈 키워드 (오늘의 일정·사설·간담회 등) 제외
  4. risk_analyze.analyze_article() (Gemini) 로 평가
  5. risk_grade ∈ {"상", "중"} 통과
  6. 국내/해외 분리 + 입법동향(오늘 일자 단계만) 추가
  7. v1 Bot API 게시글 1건으로 발송

환경변수:
  FLOW_API_KEY      Flow v1 Bot API 키 (x-flow-api-key)
  FLOW_BOT_ID       게시 봇 식별자 (예: biz@coocon.net)
  FLOW_PROJECT_ID   게시 대상 프로젝트 ID
  SUPABASE_URL/KEY  Supabase REST 접근
  GEMINI_API_KEY    risk_analyze.analyze_article() 용

옵션:
  --dry-run    API 호출 없이 본문만 stdout 출력
  --allow-empty  항목 0건이어도 게시
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from flow_bot import FlowBot
from priority import get_priority
from risk_analyze import analyze_article

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# ── 분류·필터 상수 ────────────────────────────────────────────────────────────

# 노이즈 컷 — 일정/사설/형식적 보도자료
_NOISE_RE = re.compile(
    r"\[오늘의|오늘의\s*(일정|국회|주요)|국회일정|주요일정|"
    r"\[사설\]|사설\s*[:\]]|방문\s*간담회|취임\s*인사|기념\s*행사|보도참고\]"
)

# 해외 동향 판별 — 해외 정부/규제기관 또는 명백한 해외 정책 흐름만.
# 단순 외국 회사명(코인베이스/바이낸스 등)은 국내 보도에도 자주 등장하므로 제외.
_OVERSEAS_RE = re.compile(
    r"美\s|미국\s|중국\s|中\s|일본\s|日\s|영국|英\s|EU\b|유럽연합|독일|프랑스|"
    r"싱가포르|홍콩|대만|베트남|인도|호주|캐나다|러시아|두바이|UAE|"
    r"FATF|G7|G20|IMF|BIS|OECD|백악관|연준|미\s*연준|Fed\b|"
    r"美\s*SEC|미\s*SEC|美\s*CFTC|미\s*CFTC|FinCEN|FCA|MAS|"
    r"트럼프|바이든|시진핑|기시다|클래리티\s*법안|GENIUS\s*Act|MiCA"
)
# 한국 컨텍스트 우선 — 위 키워드가 있어도 한국이 주체이면 국내로 분류.
_KR_CONTEXT_RE = re.compile(
    r"한국판|국내(?!\s*외)|한국(?:의|이|에서|에서는|에선)|"
    r"국회\s*(?:통과|의결|상정|가결)|금융위(원회)?|FIU|금감원|"
    r"한은|한국은행|기재부|기획재정부|금융정보분석원"
)

LEG_STATUS_KEYWORDS = ("공포", "국무회의", "시행", "가결")
_DATE_RE = re.compile(r"(\d{4})[\.\s\-]+(\d{1,2})[\.\s\-]+(\d{1,2})")


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(s) -> str:
    """HTML 엔티티 디코드 + 양끝 공백 제거."""
    return html.unescape(str(s or "")).strip()


def _is_noise(article: dict) -> bool:
    text = (article.get("제목") or "") + " " + (article.get("내용") or "")[:500]
    return bool(_NOISE_RE.search(text))


def _is_overseas(article: dict) -> bool:
    """해외 키워드가 있어도 한국 컨텍스트(한국판/국내/금융위 등)가 더 강하면 국내."""
    text = (article.get("제목") or "") + " " + (article.get("내용") or "")[:400]
    if not _OVERSEAS_RE.search(text):
        return False
    return not _KR_CONTEXT_RE.search(text)


def _key_points(item: dict) -> list[str]:
    kp = item.get("key_points") or []
    if isinstance(kp, list):
        return [_clean(x) for x in kp if x][:3]
    if isinstance(kp, str):
        return [_clean(kp)]
    return []


def _event_date(r: dict) -> str | None:
    """입법현황 row → '실제 단계 발생일' (status 괄호 1순위, propose_date 2순위)."""
    m = re.search(r"\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", r.get("status") or "")
    if not m:
        m = _DATE_RE.search(r.get("propose_date") or "")
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _sb_get(path: str):
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": os.environ["SUPABASE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 데이터 수집 ────────────────────────────────────────────────────────────────

def _fetch_aml_articles(today_str: str) -> list[dict]:
    """오늘 AML 기사 → 1차 priority 컷 → 노이즈 제거 → risk 평가 → 상/중 통과."""
    rows = _sb_get("articles?type=eq.AML&select=data&limit=1")
    if not rows:
        return []
    raw = rows[0].get("data") or []
    today = [a for a in raw if (a.get("날짜") or "")[:10] == today_str]
    candidates = [a for a in today if get_priority(a) == "상" and not _is_noise(a)]

    results: list[dict] = []
    for a in candidates:
        try:
            r = analyze_article(a)
        except Exception as e:
            print(f"[WARN] risk_analyze 실패 ({(a.get('제목') or '')[:40]}): {e}",
                  file=sys.stderr)
            continue
        if r.get("risk_grade") not in ("상", "중"):
            continue
        results.append({**a, **r, "_overseas": _is_overseas(a)})

    # 상 먼저 — 같은 등급 안에서 국내 먼저
    results.sort(key=lambda x: (
        0 if x.get("risk_grade") == "상" else 1,
        1 if x["_overseas"] else 0,
    ))
    return results


def _fetch_legislation(today_str: str) -> list[dict]:
    """AML 카테고리 입법현황 중 status/propose_date 가 오늘인 항목."""
    rows = _sb_get(f"legislation_status?category=eq.AML"
                   f"&select=bill_title,ministry,status,link,target_law,propose_date"
                   f"&limit=1000")
    seen: set[str] = set()
    result: list[dict] = []
    for r in rows:
        if not any(kw in (r.get("status") or "") for kw in LEG_STATUS_KEYWORDS):
            continue
        if _event_date(r) != today_str:
            continue
        key = (r.get("target_law") or "") + "|" + (r.get("bill_title") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


# ── 본문 조립 (시안 A: 컴팩트 카드) ────────────────────────────────────────────

_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _format_article_card(idx: int, x: dict) -> str:
    grade = x.get("risk_grade", "")
    rtype = x.get("risk_type", "")
    emoji = "🔴" if grade == "상" else "🟡"
    lines = [
        _SEP,
        f"{emoji} [{grade}·{rtype}]  #{idx}",
        _clean(x.get("제목")),
        f"· 기관: {_clean(x.get('기관') or '-')}",
    ]
    kps = _key_points(x)
    if kps:
        lines.append("· 핵심:")
        for k in kps:
            lines.append(f"   - {k}")
    impact = x.get("impact_area") or x.get("영향영역")
    if impact:
        lines.append(f"· 영향: {_clean(impact)}")
    action = x.get("recommendation") or x.get("권장액션")
    if action:
        lines.append(f"· 액션: {_clean(action)[:140]}")
    link = x.get("링크") or ""
    if link:
        lines.append(f"· 원문: {link}")
    return "\n".join(lines)


def _format_leg_card(idx: int, r: dict) -> str:
    title = _clean(r.get("bill_title"))
    ministry = _clean(r.get("ministry") or "-")
    status = _clean(r.get("status") or "-")
    link = r.get("link") or ""
    lines = [
        _SEP,
        f"⚖️ [입법]  #{idx}",
        title,
        f"· 부처: {ministry}",
        f"· 단계: {status}",
    ]
    if link:
        lines.append(f"· 원문: {link}")
    return "\n".join(lines)


def build_contents(today_str: str) -> tuple[str, int]:
    articles = _fetch_aml_articles(today_str)
    legs = _fetch_legislation(today_str)

    domestic = [x for x in articles if not x["_overseas"]]
    overseas = [x for x in articles if x["_overseas"]]

    total = len(articles) + len(legs)
    header = [
        f"📊 AML 일일 모니터링 — {today_str.replace('-', '.')}",
        f"국내 {len(domestic)}건 · 해외 {len(overseas)}건 · 입법 {len(legs)}건",
    ]
    sections: list[str] = []
    idx = 0
    if domestic:
        sections.append(f"\n🇰🇷 국내 동향 ({len(domestic)}건)")
        for x in domestic:
            idx += 1
            sections.append(_format_article_card(idx, x))
    if overseas:
        sections.append(f"\n🌍 해외 동향 ({len(overseas)}건)")
        for x in overseas:
            idx += 1
            sections.append(_format_article_card(idx, x))
    if legs:
        sections.append(f"\n⚖️ 입법동향 ({len(legs)}건)")
        for r in legs:
            idx += 1
            sections.append(_format_leg_card(idx, r))

    if total == 0:
        sections.append("\n오늘 신규 의미있는 AML 동향이 없습니다.")
    else:
        sections.append(_SEP)

    return "\n".join(header + sections), total


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="API 호출 없이 본문만 stdout 출력")
    parser.add_argument("--allow-empty", action="store_true",
                        help="항목 0건이어도 게시")
    args = parser.parse_args()

    missing = [k for k in ("FLOW_API_KEY", "FLOW_BOT_ID", "FLOW_PROJECT_ID",
                            "SUPABASE_URL", "SUPABASE_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"[ERROR] 환경변수 누락: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not os.environ.get("GEMINI_API_KEY"):
        print("[WARN] GEMINI_API_KEY 없음 — risk_analyze 가 룰 기반 폴백으로만 동작",
              file=sys.stderr)

    today_str = date.today().isoformat()
    contents, total = build_contents(today_str)
    title = f"📊 AML 일일 모니터링 — {today_str.replace('-', '.')}"

    print(f"[INFO] today={today_str} total={total}건")
    print(f"[INFO] title={title}")
    print("---- contents ----")
    print(contents)
    print("---- /contents ----")

    if args.dry_run:
        print("[INFO] --dry-run, API 호출 생략")
        return 0
    if total == 0 and not args.allow_empty:
        print("[INFO] 항목 0건 — 게시 생략 (--allow-empty 로 강제 게시 가능)")
        return 0

    bot = FlowBot(os.environ["FLOW_API_KEY"])
    res = bot.create_post(
        bot_id=os.environ["FLOW_BOT_ID"],
        project_id=os.environ["FLOW_PROJECT_ID"],
        title=title,
        contents=contents,
    )
    print(f"[OK] response: {json.dumps(res, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
