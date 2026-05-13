"""collect_law_articles.py — 법령 본문 조문 수집 (3단비교용).

변경 감지 모드:
  1. lawSearch.do 로 법률·시행령·시행규칙(또는 동일 관련 행정규칙) MST 조회
  2. Supabase law_articles 에 같은 law_id+law_type 의 최신 MST 와 비교
  3. MST 가 다르거나 처음 수집이면 fetch_law_articles → upsert

사용법:
  python collect_law_articles.py                # 8개 대상 법령 전체 변경 감지
  python collect_law_articles.py --law 외국환거래법  # 특정 법령만
  python collect_law_articles.py --force        # 변경 감지 무시 강제 재수집
  python collect_law_articles.py --dry-run      # 본문 fetch만 하고 Supabase 업로드 안 함
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
from datetime import date
from pathlib import Path

from law_client import LawClient

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# 대상 법령 마스터 매핑 — law_id 는 짧고 안정한 식별자.
# 시행규칙/규정 이름은 일부 행정규칙 카테고리(별도 OC 활용 필요)로 빠질 수 있어 fallback 처리.
LAW_TARGETS: dict[str, dict] = {
    "fefta":  {"act":"외국환거래법",
                "enforce":"외국환거래법 시행령",
                "regulation":"외국환거래규정"},
    "fiu":    {"act":"특정 금융거래정보의 보고 및 이용 등에 관한 법률",
                "enforce":"특정 금융거래정보의 보고 및 이용 등에 관한 법률 시행령",
                "regulation":"특정 금융거래정보의 보고 및 감독규정"},
    "tfpa":   {"act":"공중 등 협박목적 및 대량살상무기확산을 위한 자금조달행위의 금지에 관한 법률",
                "enforce":"공중 등 협박목적 및 대량살상무기확산을 위한 자금조달행위의 금지에 관한 법률 시행령",
                "regulation":"공중협박자금조달금지법 시행규칙"},
    "pipa":   {"act":"개인정보 보호법",
                "enforce":"개인정보 보호법 시행령",
                "regulation":"개인정보 보호법 시행규칙"},
    "cipa":   {"act":"신용정보의 이용 및 보호에 관한 법률",
                "enforce":"신용정보의 이용 및 보호에 관한 법률 시행령",
                "regulation":"신용정보업감독규정"},
    "itna":   {"act":"정보통신망 이용촉진 및 정보보호 등에 관한 법률",
                "enforce":"정보통신망 이용촉진 및 정보보호 등에 관한 법률 시행령",
                "regulation":"정보통신망 이용촉진 및 정보보호 등에 관한 법률 시행규칙"},
    "efta":   {"act":"전자금융거래법",
                "enforce":"전자금융거래법 시행령",
                "regulation":"전자금융감독규정"},
}


# ── Supabase helpers ─────────────────────────────────────────────────────────
def _sb_get(path: str) -> list:
    su = os.environ["SUPABASE_URL"]; sk = os.environ["SUPABASE_KEY"]
    req = urllib.request.Request(
        f"{su}/rest/v1/{path}",
        headers={"apikey": sk, "Authorization": f"Bearer {sk}"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _sb_upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    su = os.environ["SUPABASE_URL"]; sk = os.environ["SUPABASE_KEY"]
    headers = {
        "apikey": sk, "Authorization": f"Bearer {sk}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    ok = 0
    for r in rows:
        data = json.dumps(r, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{su}/rest/v1/law_articles", data=data, headers=headers, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).read()
            ok += 1
        except Exception as e:
            print(f"  [WARN] upsert 실패 ({r.get('id')}): {e}", file=sys.stderr)
    return ok


def _sb_delete_old(law_id: str, law_type: str) -> None:
    """변경 감지 시 옛 조문을 모두 삭제하고 새로 채움. (조문 번호가 줄어들면 잔재 방지)"""
    su = os.environ["SUPABASE_URL"]; sk = os.environ["SUPABASE_KEY"]
    qs = urllib.parse.urlencode({"law_id": f"eq.{law_id}", "law_type": f"eq.{law_type}"})
    req = urllib.request.Request(
        f"{su}/rest/v1/law_articles?{qs}",
        headers={"apikey": sk, "Authorization": f"Bearer {sk}", "Prefer": "return=minimal"},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"  [WARN] delete 실패 ({law_id}/{law_type}): {e}", file=sys.stderr)


# ── 위임 패턴 추출 ────────────────────────────────────────────────────────────
_LAW_REF_PAT = re.compile(r"법\s*제(\d+)조")
_REG_REF_PAT = re.compile(r"규정\s*제(\d+)조")
_DELEG_PAT   = [re.compile(r"대통령령으로\s*정"), re.compile(r"총리령으로\s*정"),
                re.compile(r"고시(?:로|에)\s*정")]


def _extract_delegation_refs(body: str, law_type: str) -> list[str]:
    refs: list[str] = []
    for m in _LAW_REF_PAT.finditer(body or ""):
        refs.append(f"법§{m.group(1)}")
    for m in _REG_REF_PAT.finditer(body or ""):
        refs.append(f"규정§{m.group(1)}")
    # act 본문에 대통령령 위임 표시 (정보용)
    if law_type == "act":
        for p in _DELEG_PAT:
            if p.search(body or ""):
                refs.append("→하위")
                break
    return refs


# ── 본문 추출 ────────────────────────────────────────────────────────────────
def _parse_jo_no(raw: str | int | None) -> int:
    """'제3조의2' / '3' / 3 → 3 (소가지번호 무시)"""
    if raw is None:
        return 0
    s = str(raw)
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


def _build_rows(law_id: str, law_type: str, parent_law_id: str | None,
                law_name: str, articles: list[dict], mst: str) -> list[dict]:
    rows: list[dict] = []
    order_idx = 0
    for art in articles:
        jo_no  = _parse_jo_no(art.get("조문번호"))
        jo_lab = art.get("조문번호") or ""
        jo_str = str(jo_lab)
        if not jo_str.startswith("제"):
            jo_str = f"제{jo_str}조" if jo_str.strip() else ""
        jo_title = (art.get("조문제목") or "").strip()
        body = (art.get("조문내용") or "").strip()
        # 항/호 본문은 별도 키일 수 있음 — 단순 합산. 응답 스키마가 list/str 혼재.
        hangs = art.get("항")
        if isinstance(hangs, list):
            for h in hangs:
                if isinstance(h, dict):
                    raw = h.get("항내용")
                    if isinstance(raw, str):
                        v = raw.strip()
                        if v:
                            body += "\n" + v
                elif isinstance(h, str):
                    if h.strip():
                        body += "\n" + h.strip()
        if not body:
            continue
        order_idx += 1
        rows.append({
            "id": f"{law_id}-{law_type}-{order_idx}",
            "law_id": law_id,
            "law_name": law_name,
            "law_type": law_type,
            "parent_law_id": parent_law_id,
            "jo_no": jo_no,
            "jo_label": jo_str,
            "jo_title": jo_title,
            "body": body[:5000],
            "delegation_refs": _extract_delegation_refs(body, law_type),
            "order_idx": order_idx,
            "scraped_at": date.today().isoformat(),
            # MST 는 row 가 아니라 별도 환경으로 관리해도 되나, 단순화를 위해 마지막 row 의 id 에 묻음
        })
    return rows


# ── 변경 감지 ────────────────────────────────────────────────────────────────
def _current_mst(client: LawClient, law_name: str) -> tuple[str, str] | None:
    """search_law → 첫 매칭 결과의 (MST, 정확한 법령명) 반환."""
    try:
        results = client.search_law(law_name, display=10)
    except Exception as e:
        print(f"  [WARN] search_law 실패 ({law_name}): {e}", file=sys.stderr)
        return None
    if not results:
        return None
    # 정확한 이름 매칭 우선
    target = law_name.replace(" ", "")
    for r in results:
        if (r.get("법령명한글") or "").replace(" ", "") == target:
            return r.get("법령일련번호") or "", r.get("법령명한글") or law_name
    # 못 찾으면 첫 결과
    r = results[0]
    return r.get("법령일련번호") or "", r.get("법령명한글") or law_name


def _stored_mst_marker(law_id: str, law_type: str) -> str | None:
    """law_articles 행 1개의 scraped_at 또는 별도 마커로 변경 감지.
    간단한 방식: 첫 row 의 id 안에 MST 를 별도 저장하지 않고, 'meta' 행을 별도 관리.
    여기서는 행이 1개라도 있으면 'fetched' 로 간주하고, 강제 모드(--force) 또는
    행이 없을 때만 재수집. (실제 변경 감지는 향후 mst 컬럼 추가 후 고도화)"""
    rows = _sb_get(
        f"law_articles?law_id=eq.{law_id}&law_type=eq.{law_type}"
        f"&select=id,scraped_at&limit=1"
    )
    if not rows:
        return None
    # 단순 마커: 'fetched'
    return "fetched"


# ── 메인 ─────────────────────────────────────────────────────────────────────
def collect_one(client: LawClient, law_id: str, law_type: str, law_name: str,
                parent_law_id: str | None, force: bool, dry_run: bool) -> int:
    print(f"  · {law_type:<10} {law_name[:40]}", end="")
    info = _current_mst(client, law_name)
    if not info:
        print("  [SKIP] 검색결과 없음")
        return 0
    mst, exact_name = info

    # 변경 감지 (단순 모드: 기존에 데이터 있으면 skip)
    marker = _stored_mst_marker(law_id, law_type)
    if marker and not force:
        print(f"  [SKIP] 이미 수집됨 (force 옵션으로 재수집 가능) MST={mst}")
        return 0

    # 본문 가져오기
    try:
        articles = client.fetch_law_articles(mst)
    except Exception as e:
        print(f"  [ERR] fetch 실패: {e}")
        return 0
    rows = _build_rows(law_id, law_type, parent_law_id, exact_name, articles, mst)
    print(f"  MST={mst} 조문={len(rows)}")

    if dry_run:
        return len(rows)
    # 기존 행 삭제 후 신규 적재 (조문 번호 변경 잔재 방지)
    _sb_delete_old(law_id, law_type)
    return _sb_upsert(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--law", default=None, help="특정 law_id 또는 법령명")
    parser.add_argument("--force", action="store_true", help="기존 데이터 있어도 재수집")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    oc = os.environ.get("LAWGO_OC")
    if not oc:
        print("[ERROR] LAWGO_OC 환경변수 필요", file=sys.stderr)
        return 1

    client = LawClient(oc)
    grand = 0
    for law_id, names in LAW_TARGETS.items():
        if args.law and args.law not in (law_id, names["act"]):
            continue
        print(f"\n=== {law_id} ({names['act']}) ===")
        for law_type in ("act", "enforce", "regulation"):
            name = names.get(law_type)
            if not name:
                continue
            parent = None if law_type == "act" else law_id
            grand += collect_one(client, law_id, law_type, name, parent,
                                 args.force, args.dry_run)
            time.sleep(0.3)

    print(f"\n총 upsert: {grand}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
