"""
restore_propose_date.py — 로컬 legislation_status.json의 enrich 데이터를 Supabase로 복구.

오늘 재수집으로 Supabase의 propose_date/summary/reason 등이 빈 값으로 덮어써진 상태를
로컬 JSON에 남아 있는 마지막 enrich 값으로 일괄 복원한다.
"""
import json
import urllib.parse
import urllib.request
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERROR] SUPABASE_URL/KEY 환경변수 없음")
    sys.exit(1)

LOCAL = Path(__file__).parent / "legislation_status.json"
items = json.loads(LOCAL.read_text(encoding="utf-8"))
print(f"로컬 JSON: {len(items)}건")

PRESERVE = ("propose_date", "status", "summary", "reason", "propose_info", "committee_review")
patched = 0
skipped = 0
errors = 0

for it in items:
    link = it.get("link")
    if not link:
        skipped += 1
        continue
    # 복구 대상 필드가 모두 비어있으면 건너뜀
    payload = {k: it[k] for k in PRESERVE if it.get(k)}
    if not payload:
        skipped += 1
        continue

    encoded = urllib.parse.quote(link, safe="")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/legislation_status?link=eq.{encoded}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            patched += 1
            if patched % 50 == 0:
                print(f"  진행 {patched}/{len(items)}")
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f"  [WARN] {link[:60]}: {e}")

print(f"\n복구 완료: PATCH {patched}건 / 스킵 {skipped}건 / 오류 {errors}건")
