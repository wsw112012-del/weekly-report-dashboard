"""collect_precedent.py — 대상 법령별 판례·법령해석례 자동수집.

흐름:
  1. LAW_TARGETS 8개 법령 순회
  2. search_precedent + search_expc 호출
  3. 상세 본문은 link 기반 ID 추출 후 fetch_precedent_detail / fetch_expc_detail
  4. precedent_db 스키마로 upsert

호출 한도 보호 — 법령별 search 최대 50건 + 상세는 7일 이내 신규만.

사용법:
  python collect_precedent.py [--law 외국환거래법] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from law_client import LawClient

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# 대상 법령 — collect_입법현황.LEGISLATION_TARGETS 와 단일 진실 원천 공유
LAW_TARGETS: list[str] = [
    "개인정보보호법",
    "신용정보의 이용 및 보호에 관한 법률",
    "정보통신망이용촉진및정보보호등에관한법률",
    "전자금융거래법",
    "외국환거래법",
    "특정 금융거래정보의 보고 및 이용 등에 관한 법률",
    "공중 등 협박목적 및 대량살상무기확산을 위한 자금조달행위의 금지에 관한 법률",
]


def _supabase_upsert(rows: list[dict]) -> int:
    if not rows or not os.environ.get("SUPABASE_URL"):
        return 0
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/precedent_db"
    headers = {
        "apikey": os.environ["SUPABASE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    ok = 0
    for row in rows:
        data = json.dumps(row, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
            ok += 1
        except Exception as e:
            print(f"  [WARN] upsert 실패 ({row.get('id')}): {e}", file=sys.stderr)
    return ok


def _normalize_date(s: str | None) -> str:
    """'2025.09.25' 또는 '20250925' 또는 '2025. 9. 25.' → '2025.09.25.'"""
    if not s:
        return ""
    s = str(s).strip()
    m = re.search(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})", s)
    if not m:
        return s
    return f"{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}."


def collect_prec_for_law(client: LawClient, law: str, max_items: int = 50) -> list[dict]:
    rows: list[dict] = []
    items = client.search_precedent(law, display=max_items)
    for it in items:
        prec_seq = it.get("판례일련번호")
        if not prec_seq:
            continue
        # 상세는 비용 절감을 위해 최근 90일만 fetch
        try:
            decided = _normalize_date(it.get("선고일자"))
            recent = False
            if decided:
                y, m, d = decided.rstrip(".").split(".")
                dt = date(int(y), int(m), int(d))
                recent = (date.today() - dt).days <= 90
            detail = client.fetch_precedent_detail(prec_seq) if recent else {}
        except Exception:
            detail = {}
        rows.append({
            "id":         f"prec-{prec_seq}",
            "source":     "prec",
            "target_law": law,
            "title":      (it.get("사건명") or "").strip(),
            "agency":     it.get("법원명") or "",
            "case_no":    it.get("사건번호") or "",
            "decided_at": _normalize_date(it.get("선고일자")),
            "summary":    (detail.get("판시사항") or "").strip(),
            "body":       (detail.get("판결요지") or "").strip(),
            "ref_laws":   (detail.get("참조조문") or "").strip(),
            "link":       LawClient.absolutize(it.get("판례상세링크") or ""),
            "scraped_at": date.today().isoformat(),
        })
        time.sleep(0.2)
    return rows


def collect_expc_for_law(client: LawClient, law: str, max_items: int = 50) -> list[dict]:
    rows: list[dict] = []
    items = client.search_expc(law, display=max_items)
    for it in items:
        expc_seq = it.get("법령해석례일련번호")
        if not expc_seq:
            continue
        try:
            detail = client.fetch_expc_detail(expc_seq)
        except Exception:
            detail = {}
        rows.append({
            "id":         f"expc-{expc_seq}",
            "source":     "expc",
            "target_law": law,
            "title":      (it.get("안건명") or "").strip(),
            "agency":     it.get("회신기관명") or "",
            "case_no":    it.get("안건번호") or "",
            "decided_at": _normalize_date(it.get("회신일자")),
            "summary":    (detail.get("질의요지") or "").strip(),
            "body":       (detail.get("회답") or detail.get("이유") or "").strip(),
            "ref_laws":   (detail.get("관련법령") or "").strip(),
            "link":       LawClient.absolutize(it.get("법령해석례상세링크") or ""),
            "scraped_at": date.today().isoformat(),
        })
        time.sleep(0.2)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--law", default=None, help="특정 법령만 수집")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max", type=int, default=50, help="법령당 최대 수집 건수")
    args = parser.parse_args()

    oc = os.environ.get("LAWGO_OC")
    if not oc:
        print("[ERROR] LAWGO_OC 환경변수 필요", file=sys.stderr)
        return 1

    client = LawClient(oc)
    targets = [args.law] if args.law else LAW_TARGETS
    grand_rows: list[dict] = []

    for law in targets:
        print(f"\n=== {law} ===")
        prec_rows = collect_prec_for_law(client, law, max_items=args.max)
        expc_rows = collect_expc_for_law(client, law, max_items=args.max)
        print(f"  prec {len(prec_rows)}건 / expc {len(expc_rows)}건")
        grand_rows.extend(prec_rows + expc_rows)

    print(f"\n총 {len(grand_rows)}건 수집")
    if args.dry_run:
        print("[INFO] --dry-run, Supabase 업로드 생략")
        for r in grand_rows[:5]:
            print(f"  - [{r['source']}/{r['target_law'][:15]}] {r['title'][:60]}")
        return 0

    ok = _supabase_upsert(grand_rows)
    print(f"Supabase upsert: {ok}/{len(grand_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
