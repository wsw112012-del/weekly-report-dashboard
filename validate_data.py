"""
validate_data.py
일배치 후 Supabase 수집 데이터 품질 검증

종료코드:
  0 — 정상 (경고만 있어도 0)
  1 — CRITICAL 오류 (오늘 수집 0건 등)
"""

import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TODAY      = date.today().isoformat()          # "2026-05-08"
YESTERDAY  = (date.today() - timedelta(days=1)).isoformat()
WEEK_AGO   = (date.today() - timedelta(days=7)).isoformat()

CRITICAL: list[str] = []
WARNINGS:  list[str] = []
INFO:      list[str] = []


# ── Supabase GET ───────────────────────────────────────────────────────────────
def _get(table: str, query: str = "") -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        WARNINGS.append(f"Supabase GET {table} 실패: {e}")
        return []


def _count(table: str, query: str = "") -> int:
    """count=exact 헤더로 건수만 조회"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return -1
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}&select=id"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
        "Prefer": "count=exact",
        "Range": "0-0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cr = resp.headers.get("Content-Range", "")  # "0-0/123"
            if "/" in cr:
                return int(cr.split("/")[-1])
            return len(json.loads(resp.read().decode("utf-8")))
    except Exception as e:
        WARNINGS.append(f"Supabase count {table} 실패: {e}")
        return -1


# ── 1. articles 검증 ───────────────────────────────────────────────────────────
def validate_articles():
    print("\n[1] articles (보도자료/뉴스) 검증")

    for cat in ["데이터", "페이먼트", "AML"]:
        encoded = urllib.parse.quote(cat, safe="")
        total = _count("articles", f"카테고리=eq.{encoded}")
        today_cnt = _count("articles", f"카테고리=eq.{encoded}&수집일=gte.{TODAY}")
        recent_cnt = _count("articles", f"카테고리=eq.{encoded}&수집일=gte.{WEEK_AGO}")

        if total < 0:
            WARNINGS.append(f"articles [{cat}] 건수 조회 실패")
            continue

        print(f"  [{cat}] 전체 {total}건 | 오늘 {today_cnt}건 | 최근7일 {recent_cnt}건")

        if today_cnt == 0:
            CRITICAL.append(f"articles [{cat}] 오늘({TODAY}) 수집 0건 — 수집 오류 가능성")
        elif today_cnt < 3:
            WARNINGS.append(f"articles [{cat}] 오늘 수집 {today_cnt}건 (기준: 3건 이상)")

        if total == 0:
            CRITICAL.append(f"articles [{cat}] 전체 데이터 없음")

    # 필수 필드 누락 체크
    missing_title = _count("articles", "제목=is.null")
    missing_link  = _count("articles", "링크=is.null")
    if missing_title > 0:
        WARNINGS.append(f"articles 제목 누락 {missing_title}건")
    if missing_link > 0:
        WARNINGS.append(f"articles 링크 누락 {missing_link}건")

    if missing_title == 0 and missing_link == 0:
        INFO.append("articles 필수 필드(제목/링크) 누락 없음 ✓")


# ── 2. legislation_status 검증 ─────────────────────────────────────────────────
def validate_legislation():
    print("\n[2] legislation_status (입법현황) 검증")

    total      = _count("legislation_status")
    gov_cnt    = _count("legislation_status", "source=eq.gov")
    asm_cnt    = _count("legislation_status", "source=eq.assembly")
    print(f"  전체 {total}건 | gov {gov_cnt}건 | assembly {asm_cnt}건")

    if total == 0:
        CRITICAL.append("legislation_status 전체 데이터 없음")
        return

    if gov_cnt < 10:
        WARNINGS.append(f"legislation_status gov {gov_cnt}건 (기준: 10건 이상)")
    if asm_cnt < 50:
        WARNINGS.append(f"legislation_status assembly {asm_cnt}건 (기준: 50건 이상)")

    # summary 없는 항목 비율
    no_summary_gov = _count("legislation_status", "source=eq.gov&summary=is.null")
    no_summary_asm = _count("legislation_status", "source=eq.assembly&summary=is.null")
    if gov_cnt > 0:
        ratio_g = round(no_summary_gov / gov_cnt * 100)
        msg = f"gov summary 미수집 {no_summary_gov}/{gov_cnt}건 ({ratio_g}%)"
        (WARNINGS if ratio_g > 50 else INFO).append(msg)
    if asm_cnt > 0:
        ratio_a = round(no_summary_asm / asm_cnt * 100)
        msg = f"assembly summary 미수집 {no_summary_asm}/{asm_cnt}건 ({ratio_a}%)"
        (WARNINGS if ratio_a > 50 else INFO).append(msg)

    # 필수 필드 누락
    no_bill_title = _count("legislation_status", "bill_title=is.null")
    no_target_law = _count("legislation_status", "target_law=is.null")
    no_link       = _count("legislation_status", "link=is.null")
    if no_bill_title > 0:
        WARNINGS.append(f"legislation_status bill_title 누락 {no_bill_title}건")
    if no_target_law > 0:
        WARNINGS.append(f"legislation_status target_law 누락 {no_target_law}건")
    if no_link > 0:
        WARNINGS.append(f"legislation_status link 누락 {no_link}건 (상세 수집 불가)")

    # 오늘 수집 여부 (scraped_at 기준)
    today_leg = _count("legislation_status", f"scraped_at=gte.{TODAY}")
    if today_leg == 0:
        WARNINGS.append(f"legislation_status 오늘({TODAY}) 수집된 항목 없음 — 배치 미실행 가능성")
    else:
        INFO.append(f"legislation_status 오늘 수집 {today_leg}건 ✓")

    # 카테고리별 현황
    for cat in ["데이터", "페이먼트", "AML"]:
        encoded = urllib.parse.quote(cat, safe="")
        cnt = _count("legislation_status", f"category=eq.{encoded}")
        print(f"  [{cat}] {cnt}건")
        if cnt == 0:
            WARNINGS.append(f"legislation_status [{cat}] 카테고리 0건")


# ── 3. assembly_press 검증 ────────────────────────────────────────────────────
def validate_assembly_press():
    print("\n[3] assembly_press (국회의원 보도자료) 검증")

    total      = _count("assembly_press")
    week_cnt   = _count("assembly_press", f"pub_date=gte.{WEEK_AGO}")
    print(f"  전체 {total}건 | 최근7일 {week_cnt}건")

    if total == 0:
        WARNINGS.append("assembly_press 전체 데이터 없음")
    if week_cnt == 0:
        WARNINGS.append(f"assembly_press 최근 7일({WEEK_AGO}~) 수집 0건")

    no_title = _count("assembly_press", "title=is.null")
    if no_title > 0:
        WARNINGS.append(f"assembly_press title 누락 {no_title}건")

    # 카테고리 분포
    for cat in ["데이터", "페이먼트", "AML"]:
        encoded = urllib.parse.quote(cat, safe="")
        cnt = _count("assembly_press", f"category=eq.{encoded}")
        print(f"  [{cat}] {cnt}건")


# ── 보고서 출력 ────────────────────────────────────────────────────────────────
def print_report():
    print("\n" + "=" * 60)
    print(f"  데이터 검증 보고서 — {TODAY}")
    print("=" * 60)

    if CRITICAL:
        print(f"\n🚨 CRITICAL ({len(CRITICAL)}건)")
        for c in CRITICAL:
            print(f"   ✗ {c}")

    if WARNINGS:
        print(f"\n⚠️  WARNING ({len(WARNINGS)}건)")
        for w in WARNINGS:
            print(f"   △ {w}")

    if INFO:
        print(f"\n✅ INFO ({len(INFO)}건)")
        for i in INFO:
            print(f"   ✓ {i}")

    if not CRITICAL and not WARNINGS:
        print("\n✅ 모든 검증 통과 — 데이터 품질 정상")

    print("=" * 60)


# ── 메인 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[WARN] SUPABASE_URL/KEY 없음 — 검증 생략")
        sys.exit(0)

    print(f"데이터 검증 시작 ({TODAY})")

    validate_articles()
    validate_legislation()
    validate_assembly_press()
    print_report()

    if CRITICAL:
        sys.exit(1)   # GitHub Actions 워크플로 실패 표시
    sys.exit(0)
