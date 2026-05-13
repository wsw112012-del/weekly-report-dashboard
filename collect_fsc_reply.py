"""collect_fsc_reply.py — better.fsc.go.kr 회신사례 수집 → Supabase precedent_db.

수집 대상:
  - 법령해석 (lawreq, source='fsc_reply', 약 2,568건)
  - 비조치의견서 (opinion, source='fsc_nonact', 약 1,671건)
현장건의 과제는 제외.

매핑 (FscClient detail → precedent_db):
  id          = '{source}-{idx}'
  source      = 'fsc_reply' or 'fsc_nonact'
  target_law  = ''  (사용자 검색에서 본문 매칭으로 보완)
  title       = detail.title
  agency      = '금융위원회' + detail.department (있으면)
  case_no     = list row 의 lawreqNumber / opinionNumber
  decided_at  = detail.replied_at (YYYY-MM-DD → YYYY.MM.DD.)
  summary     = detail.question (질의요지)
  body        = detail.answer + '\\n\\n[이유]\\n' + detail.reason
  ref_laws    = list row 의 category (예: '보험', '전자금융')
  link        = detail.link

CLI:
  --kind {all|law|opinion}    수집 대상
  --max N                     각 카테고리 최대 N건
  --start N                   목록 시작 오프셋 (증분 수집)
  --dry-run                   Supabase upsert 생략
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

from fsc_client import FscClient

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


def _supabase_upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    su = os.environ.get("SUPABASE_URL")
    sk = os.environ.get("SUPABASE_KEY")
    if not su or not sk:
        print("[WARN] SUPABASE_URL/KEY 없음 — upsert 생략", file=sys.stderr)
        return 0
    headers = {
        "apikey": sk, "Authorization": f"Bearer {sk}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    ok = 0
    for r in rows:
        data = json.dumps(r, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{su}/rest/v1/precedent_db",
            data=data, headers=headers, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
            ok += 1
        except Exception as e:
            print(f"  [WARN] upsert 실패 ({r.get('id')}): {e}", file=sys.stderr)
    return ok


def _norm_date(s: str) -> str:
    """'2026-04-02' or '2026.04.02' → '2026.04.02.'"""
    if not s:
        return ""
    s = s.strip().replace("-", ".")
    if not s.endswith("."):
        s += "."
    return s


def collect(kind: str, max_items: int | None, start: int, dry_run: bool) -> int:
    client = FscClient()
    cfg = client.KINDS[kind]
    idx_key = cfg["idx_key"]
    num_key = cfg["number_key"]
    src     = cfg["source"]

    rows: list[dict] = []
    skip = start
    seen = 0
    for row in client.list_all(kind, max_items=(max_items + start if max_items else None),
                                page_size=100):
        if skip > 0:
            skip -= 1
            continue
        idx = row.get(idx_key)
        if not idx:
            continue
        try:
            d = client.fetch_detail(kind, idx)
        except Exception as e:
            print(f"  [WARN] detail 실패 ({kind}/{idx}): {e}", file=sys.stderr)
            continue
        rows.append({
            "id":         f"{src}-{idx}",
            "source":     src,
            "target_law": "",
            "title":      d.get("title") or row.get("title") or "",
            "agency":     "금융위원회" + (f" {d['department']}" if d.get("department") else ""),
            "case_no":    row.get(num_key) or "",
            "decided_at": _norm_date(d.get("replied_at") or ""),
            "summary":    d.get("question") or "",
            "body":       (d.get("answer") or "") +
                          (("\n\n[이유]\n" + d["reason"]) if d.get("reason") else ""),
            "ref_laws":   row.get("category") or "",
            "link":       d.get("link") or "",
            "scraped_at": date.today().isoformat(),
        })
        seen += 1
        if max_items and seen >= max_items:
            break
        # rate limit — 너무 빠르면 일부 502
        time.sleep(0.15)

    print(f"  {kind}: {len(rows)}건 수집")
    if dry_run:
        for r in rows[:2]:
            print(f"    [{r['source']}] {r['title'][:60]} (case={r['case_no']}, {r['decided_at']})")
        return len(rows)
    return _supabase_upsert(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", default="all", choices=["all", "law", "opinion"])
    parser.add_argument("--max", type=int, default=None)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    kinds = ["law", "opinion"] if args.kind == "all" else [args.kind]
    grand = 0
    for k in kinds:
        print(f"\n=== {k} ===")
        grand += collect(k, args.max, args.start, args.dry_run)
    print(f"\n총 upsert: {grand}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
