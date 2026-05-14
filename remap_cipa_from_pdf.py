"""remap_cipa_from_pdf.py — 신용정보법(cipa) 감독규정 매핑을 PDF 3단 편람에서 추출.

PDF: 본인신용정보관리 법규 편람 (251p, 법률·시행령·감독규정 3단 정렬)
컬럼 경계: 법률 x<290, 시행령 290<x<540, 감독규정 x>540

흐름:
  1. pdfplumber 로 각 페이지 워드를 좌표 포함 추출
  2. x 좌표로 3컬럼 분류
  3. 각 컬럼에서 '제N조(...)' 헤더 위치 추적
  4. 같은 y 영역에서 법률 제N조 ↔ 감독규정 제M조 페어 수집
  5. DB 의 cipa regulation 행 (감독규정 제M조) 에 'law§N' 추가
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import pdfplumber

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PDF_PATH = "c:/Users/쿠콘_우승우/Desktop/업무/00. '26 쿠콘전략실/10. 기타/제본/본인신용정보관리 법규 편람(完)(시행, 25.02.05).pdf"

# 컬럼 x 경계 (페이지 너비 841 기준)
COL_BOUND_ACT_END = 290
COL_BOUND_ENF_END = 540

# 조문 헤더 정규식 (제N조 / 제N조의M / 제N-M조)
ART_RE = re.compile(r"제\s*(\d+)\s*(?:[-\s]\s*(\d+))?\s*(?:조의\s*(\d+))?\s*조")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def column_of(x: float) -> str:
    if x < COL_BOUND_ACT_END:
        return "act"
    if x < COL_BOUND_ENF_END:
        return "enforce"
    return "regulation"


def extract_mappings() -> dict[int, set[int]]:
    """PDF 파싱 결과 → {감독규정_jo_no: {법률_jo_no, ...}}"""
    reg_to_acts: dict[int, set[int]] = defaultdict(set)

    with pdfplumber.open(PDF_PATH) as pdf:
        # 마지막 조문 헤더를 컬럼별로 추적
        cur_act: int | None = None
        cur_reg: int | None = None
        for pidx, page in enumerate(pdf.pages):
            words = page.extract_words(use_text_flow=False)
            if not words:
                continue
            # y 별로 줄 묶기 (같은 줄 = y 차이 5 이하)
            words.sort(key=lambda w: (w["top"], w["x0"]))
            lines: list[list[dict]] = []
            for w in words:
                if lines and abs(w["top"] - lines[-1][0]["top"]) < 5:
                    lines[-1].append(w)
                else:
                    lines.append([w])

            for line in lines:
                # 컬럼별로 워드 그룹화
                cols: dict[str, list[str]] = {"act": [], "enforce": [], "regulation": []}
                for w in line:
                    col = column_of((w["x0"] + w["x1"]) / 2)
                    cols[col].append(w["text"])
                act_text = " ".join(cols["act"])
                reg_text = " ".join(cols["regulation"])

                # 법률 컬럼 조문 헤더
                m_act = ART_RE.match(act_text)
                if m_act:
                    n = int(m_act.group(1))
                    cur_act = n
                # 감독규정 컬럼 조문 헤더
                m_reg = ART_RE.match(reg_text)
                if m_reg:
                    n = int(m_reg.group(1))
                    cur_reg = n

                # 현재 줄에 양쪽 모두 텍스트가 있고, 헤더 페어가 잡혀 있으면 매핑 추가
                if cur_act and cur_reg and (cols["act"] or cols["regulation"]):
                    reg_to_acts[cur_reg].add(cur_act)

    return {k: v for k, v in reg_to_acts.items() if v}


def sb_get(path: str) -> list:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def sb_patch(row_id: str, data: dict) -> None:
    enc = urllib.parse.quote(row_id, safe="")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/law_articles?id=eq.{enc}",
        data=json.dumps(data).encode("utf-8"),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=20).read()


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    print(f"PDF 파싱 중: {PDF_PATH.split('/')[-1]}")
    mappings = extract_mappings()
    print(f"\n추출된 (감독규정 제M조 → 법률 제N조) 매핑: {len(mappings)}건")
    for reg_no, acts in sorted(mappings.items())[:10]:
        print(f"  감독규정 제{reg_no}조 → 법 제{sorted(acts)}조")
    if len(mappings) > 10:
        print(f"  ... 외 {len(mappings)-10}건")

    if dry_run:
        print("\n--dry-run: DB 업데이트 안 함")
        return 0

    # cipa 감독규정 행 fetch (law_id='cipa' regulation = 신용정보업감독규정)
    rows = sb_get(
        "law_articles?law_id=eq.cipa&law_type=eq.regulation"
        "&select=id,jo_no,jo_label,delegation_refs&limit=300"
    )
    print(f"\nDB cipa 감독규정 행 {len(rows)}건 확인")

    # jo_no 별 행 그룹 (가지번호 손실로 같은 jo_no 에 여러 행 있을 수 있음)
    by_jo = defaultdict(list)
    for r in rows:
        by_jo[r.get("jo_no") or 0].append(r)

    updated = 0
    for reg_no, act_nos in mappings.items():
        target_rows = by_jo.get(reg_no, [])
        if not target_rows:
            continue
        for r in target_rows:
            existing = r.get("delegation_refs") or []
            if isinstance(existing, str):
                try: existing = json.loads(existing)
                except Exception: existing = []
            new_refs = list(existing) + [f"법§{n}" for n in act_nos]
            new_refs = list(dict.fromkeys(new_refs))
            try:
                sb_patch(r["id"], {"delegation_refs": new_refs})
                updated += 1
            except Exception as e:
                print(f"  PATCH 실패 {r['id']}: {e}")

    print(f"\n총 업데이트: {updated}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
