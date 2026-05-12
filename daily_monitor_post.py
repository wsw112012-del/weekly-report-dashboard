"""daily_monitor_post.py — 매일 수집 직후 Flow에 데일리 뉴스 모니터링 게시글 1건 작성.

환경변수:
  FLOW_API_KEY      Flow v1 Bot API 키 (x-flow-api-key 헤더)
  FLOW_BOT_ID       게시 봇 식별자 (예: biz@coocon.net)
  FLOW_PROJECT_ID   게시 대상 프로젝트 ID
  SUPABASE_URL/KEY  Supabase REST 접근

옵션:
  --dry-run    API 호출 없이 본문만 stdout 출력
"""
import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from flow_bot import FlowBot
from priority import get_priority

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


CATEGORIES = ("AML",)  # Flow 게시 대상은 AML 한정
CAT_LABEL = {"AML": "AML(자금세탁방지)"}
LEG_STATUS_KEYWORDS = ("공포", "국무회의", "시행", "가결")


def _sb_get(path: str) -> list | dict:
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": os.environ["SUPABASE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_articles_top(today_str: str) -> dict[str, list[dict]]:
    """articles 테이블 3 카테고리 row에서 오늘 날짜 + 우선순위='상' 항목 추출.
    우선순위는 DB 미저장 → priority.get_priority() 로 인메모리 산정."""
    rows = _sb_get("articles?select=type,data")
    by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for r in rows:
        cat = r.get("type")
        if cat not in by_cat:
            continue
        for a in (r.get("data") or []):
            if (a.get("날짜") or "")[:10] != today_str:
                continue
            if get_priority(a) != "상":
                continue
            by_cat[cat].append(a)
    return by_cat


def _fetch_legislation_top(today_str: str) -> list[dict]:
    """legislation_status에서 오늘 scraped_at + category=AML + 주요 상태 항목 추출."""
    rows = _sb_get(f"legislation_status?scraped_at=eq.{today_str}"
                   f"&category=eq.AML"
                   f"&select=bill_title,ministry,status,link,target_law,category"
                   f"&limit=1000")
    seen: set[str] = set()
    result: list[dict] = []
    for r in rows:
        if not any(kw in (r.get("status") or "") for kw in LEG_STATUS_KEYWORDS):
            continue
        key = (r.get("target_law") or "") + "|" + (r.get("bill_title") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


def _clean(s: str) -> str:
    """HTML 엔티티(&quot; 등) 디코드 + 양끝 공백 제거."""
    return html.unescape((s or "")).strip()


def _format_article(a: dict) -> str:
    agency = _clean(a.get("기관") or "-")
    title = _clean(a.get("제목"))
    link = a.get("링크") or ""
    return f"  - [{agency}] {title}\n    {link}" if link else f"  - [{agency}] {title}"


def _format_leg(r: dict) -> str:
    title = _clean(r.get("bill_title"))
    ministry = _clean(r.get("ministry") or "-")
    status = _clean(r.get("status") or "-")
    link = r.get("link") or ""
    head = f"  - [{ministry}] {title} — {status}"
    return f"{head}\n    {link}" if link else head


def build_contents(today_str: str) -> tuple[str, int]:
    """본문 텍스트 + 총 항목 수 반환."""
    by_cat = _fetch_articles_top(today_str)
    legs = _fetch_legislation_top(today_str)
    total = sum(len(v) for v in by_cat.values()) + len(legs)

    lines = [f"📊 데일리 뉴스 모니터링 — {today_str.replace('-', '.')}", ""]
    for cat in CATEGORIES:
        items = by_cat[cat]
        if not items:
            continue
        lines.append(f"■ {CAT_LABEL[cat]} (상 등급 {len(items)}건)")
        for a in items:
            lines.append(_format_article(a))
        lines.append("")
    if legs:
        lines.append(f"■ 입법동향 ({'·'.join(LEG_STATUS_KEYWORDS)}) {len(legs)}건")
        for r in legs:
            lines.append(_format_leg(r))
        lines.append("")
    if total == 0:
        lines.append("오늘 신규 '상' 등급 항목 및 주요 입법동향이 없습니다.")
    return "\n".join(lines).rstrip(), total


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

    today_str = date.today().isoformat()
    contents, total = build_contents(today_str)
    title = f"📊 데일리 모니터링 — {today_str.replace('-', '.')}"

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
