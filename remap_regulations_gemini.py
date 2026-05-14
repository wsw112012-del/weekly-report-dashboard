"""remap_regulations_gemini.py — 매핑 안 된 감독규정·시행규칙을 Gemini로 의미 매핑.

전제:
  - law_articles 테이블에 act/enforce/regulation 모두 적재됨
  - regulation 본문에 '법 제N조' 명시 인용이 없으면 정규식 매핑 실패
  - 이 스크립트는 매핑 실패 행을 Gemini Flash 2.0 으로 분석해
    delegation_refs 컬럼에 ["법§N", "법§M"] 형태로 보강 저장

사용:
  python remap_regulations_gemini.py             # 5개 법령 모두
  python remap_regulations_gemini.py --law efta  # 특정 법령만
  python remap_regulations_gemini.py --dry-run   # API 호출 후 결과만 출력
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 이미 매핑된 행을 식별하는 정규식 — app.py /api/law-comparison 와 동일
PAT_NAMED = re.compile(r"「[^」]+?법(?:률)?」\s*제\s*(\d+)\s*조")
PAT_SHORT = re.compile(
    r"(?:같은\s*법|이\s*법|동법|본법|법률|(?<![가-힣])법)\s*제\s*(\d+)\s*조",
    re.UNICODE,
)

# 비교 대상 5개 법령 (act_law_id 기준)
TARGET_LAWS = ["fefta", "fiu", "cipa", "efta", "itna"]

# 배치당 처리할 감독규정 조문 수 (Gemini 호출당)
BATCH_SIZE = 5


def sb_get(path: str) -> list:
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL 미설정")
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


def call_gemini(prompt: str, retries: int = 3) -> str:
    """Gemini 호출. gemini-flash-latest → 2.5-flash fallback."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0},
    }).encode("utf-8")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_err = ""
    for model in ("gemini-flash-latest", "gemini-2.5-flash"):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_API_KEY}"
        )
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body,
                                              headers={"Content-Type": "application/json"},
                                              method="POST")
                with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                    data = json.loads(resp.read())
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                last_err = str(e)
                if "429" in last_err or "RESOURCE_EXHAUSTED" in last_err:
                    wait = 18 if attempt == 0 else 30
                    print(f"    rate-limit, {wait}s 대기...")
                    time.sleep(wait)
                else:
                    time.sleep(3)
                    break  # 429 가 아니면 모델 교체
    print(f"[WARN] Gemini 실패: {last_err[:120]}")
    return ""


def build_prompt(act_name: str, act_articles: list[dict], reg_batch: list[dict]) -> str:
    """배치(5개 감독규정 조문) 매핑 프롬프트."""
    act_lines = [
        f"- 제{a['jo_no']}조 ({a.get('jo_title') or '제목없음'})"
        for a in act_articles
    ]
    reg_lines = []
    for r in reg_batch:
        body = (r.get("body") or "").strip()[:400]
        title = r.get("jo_title") or ""
        label = r.get("jo_label") or ""
        reg_lines.append(
            f'== ID: {r["id"]}\n'
            f'   조문: {label} ({title})\n'
            f'   본문: {body}'
        )

    return f"""당신은 한국 법률 매핑 전문가입니다.
아래 [감독규정/시행규칙] 의 각 조문을, [{act_name}] 의 어떤 조문과 가장 관련 깊은지 매핑하세요.

[{act_name} 조문 목록]
{chr(10).join(act_lines)}

[매핑 대상 — {len(reg_batch)}개 조문]
{chr(10).join(reg_lines)}

[지침]
1) 각 감독규정 조문이 위임받거나 구체화하는 법률 조 번호를 1~3개 추론.
2) 직접 인용("법 제N조") 이 없어도 의미/주제로 매핑.
3) 정의 조문, 일반 사항, 부칙 등 관련 없으면 빈 배열.
4) 반드시 JSON 으로만 출력 (다른 텍스트 금지).

[출력 형식]
{{
  "ID1": [3, 4],
  "ID2": [],
  "ID3": [7]
}}
"""


def process_law(law_id: str, dry_run: bool = False) -> dict:
    print(f"\n=== {law_id} ===")
    # 1) 법률 act 조문 목록 (Gemini 컨텍스트용)
    acts = sb_get(
        f"law_articles?law_id=eq.{law_id}&law_type=eq.act"
        f"&select=jo_no,jo_label,jo_title,law_name&order=jo_no.asc&limit=500"
    )
    # 중복 jo_no 제거 (대표 1개씩)
    seen, act_unique = set(), []
    for a in acts:
        no = a.get("jo_no") or 0
        if no in seen: continue
        seen.add(no)
        act_unique.append(a)
    if not act_unique:
        print(f"  act 조문 없음 → 스킵")
        return {"updated": 0, "skipped": 0}
    print(f"  법률 act 조문 {len(act_unique)}건")

    # 2) regulation 행 (law_id 본인 + parent_law_id 둘 다)
    rows = sb_get(
        f"law_articles?or=(law_id.eq.{law_id},parent_law_id.eq.{law_id})"
        f"&law_type=eq.regulation"
        f"&select=id,law_name,jo_no,jo_label,jo_title,body,delegation_refs&limit=500"
    )
    # 매핑 안 된 행만 필터
    unmapped = []
    for r in rows:
        t = (r.get("body") or "") + " " + (r.get("jo_title") or "")
        if PAT_NAMED.search(t) or PAT_SHORT.search(t):
            continue
        unmapped.append(r)
    print(f"  매핑 안 된 regulation {len(unmapped)}건")
    if not unmapped:
        return {"updated": 0, "skipped": 0}

    # 3) law_name 별로 묶어서 처리 (parent regulation 들이 섞여 있을 수 있어 그룹별 act_name)
    act_name_map = {r.get("law_name", ""): r.get("law_name", "") for r in acts}
    # act 그룹의 첫 law_name 을 대표로 사용
    act_name = acts[0].get("law_name") if acts else law_id
    print(f"  대표 법령명: {act_name}")

    # 4) 배치 처리
    updated = 0
    for i in range(0, len(unmapped), BATCH_SIZE):
        batch = unmapped[i:i + BATCH_SIZE]
        prompt = build_prompt(act_name, act_unique, batch)
        print(f"  배치 {i//BATCH_SIZE + 1}/{(len(unmapped) + BATCH_SIZE - 1)//BATCH_SIZE} ({len(batch)}건) 호출 중...")
        resp = call_gemini(prompt)
        if not resp:
            print(f"    → 응답 비어있음 스킵")
            time.sleep(5)
            continue
        # JSON 파싱
        try:
            parsed = json.loads(resp)
        except json.JSONDecodeError:
            # ```json ... ``` 코드블록 케이스
            m = re.search(r"\{.*\}", resp, re.DOTALL)
            if not m:
                print(f"    → JSON 파싱 실패: {resp[:80]!r}")
                continue
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                print(f"    → JSON 파싱 실패")
                continue
        # 5) 각 row 의 delegation_refs 에 "법§N(gemini)" 추가
        for r in batch:
            rid = r["id"]
            mapped = parsed.get(rid, [])
            if not isinstance(mapped, list) or not mapped:
                continue
            existing = r.get("delegation_refs") or []
            if isinstance(existing, str):
                try: existing = json.loads(existing)
                except: existing = []
            new_refs = list(existing) + [f"법§{n}" for n in mapped if isinstance(n, int)]
            # 중복 제거
            new_refs = list(dict.fromkeys(new_refs))
            print(f"    {rid}: 법§{mapped}")
            if not dry_run:
                try:
                    sb_patch(rid, {"delegation_refs": new_refs})
                    updated += 1
                except Exception as e:
                    print(f"    PATCH 실패 ({rid}): {e}")
        # rate limit 여유 (15 RPM = 4초 간격)
        time.sleep(4.2)

    print(f"  결과: {updated}건 업데이트")
    return {"updated": updated, "skipped": len(unmapped) - updated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--law", default=None, help="특정 law_id 만")
    parser.add_argument("--dry-run", action="store_true", help="DB 업데이트 안 함")
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY 미설정", file=sys.stderr)
        return 1

    targets = [args.law] if args.law else TARGET_LAWS
    total = 0
    for lid in targets:
        if lid not in TARGET_LAWS and args.law:
            print(f"알 수 없는 law_id: {lid}", file=sys.stderr)
            continue
        result = process_law(lid, dry_run=args.dry_run)
        total += result["updated"]
    print(f"\n총 업데이트: {total}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
