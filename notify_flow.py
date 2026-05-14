"""
notify_flow.py — AML 일일 리스크 다이제스트 → Flow 채널 자동 발송.

흐름:
  1. Supabase articles(type=AML) 또는 collected_AML.txt에서 최신 기사 로드
  2. aml_digest_sent 테이블과 차집합 → 신규 url만 추출
  3. risk_analyze.analyze_article() 로 각 기사 평가
  4. risk_grade ∈ {"상", "중"} 만 통과
  5. 0건이면 발송 생략하고 종료
  6. flow_card.render_card() 로 카드 PNG 일괄 생성
  7. Flow API 1회 POST(텍스트 본문 + 카드 N장 multipart 첨부)
  8. 성공 시 aml_digest_sent에 발송 기록

사용:
    python notify_flow.py                 # 정상 발송
    python notify_flow.py --dry-run       # Flow 전송 직전까지만, PNG 저장
"""
import argparse
import json
import os
import re
import ssl as _ssl
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Windows cp949 콘솔 보호
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from risk_analyze import analyze_article
from flow_card import render_card


# ── 환경변수 ──────────────────────────────────────────────────────────────────

SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
FLOW_API_TOKEN  = os.environ.get("FLOW_API_TOKEN", "")
FLOW_CHANNEL_ID = os.environ.get("FLOW_CHANNEL_ID", "")
FLOW_BASE_URL   = os.environ.get("FLOW_BASE_URL", "https://flow.team")

# 한 번에 발송할 카드 최대 개수 (Flow 첨부 제한·가독성 고려)
MAX_CARDS = int(os.environ.get("AML_DIGEST_MAX_CARDS", "10"))

BASE_DIR        = Path(__file__).parent
COLLECTED_AML   = BASE_DIR / "collected_AML.txt"


# ── HTTP 세션 ──────────────────────────────────────────────────────────────────

class _LaxSSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


SESSION = requests.Session()
SESSION.verify = False
SESSION.mount("https://", _LaxSSLAdapter())


# ── Supabase helpers ─────────────────────────────────────────────────────────

def _sb_get(path: str) -> list | None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] Supabase GET {path}: {e}")
        return None


def _sb_post(path: str, payload: dict | list) -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=data,
        headers={
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=ignore-duplicates,return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as _:
            return True
    except Exception as e:
        print(f"[WARN] Supabase POST {path}: {e}")
        return False


# ── 데이터 로드 ────────────────────────────────────────────────────────────────

def load_articles_from_supabase() -> list[dict]:
    rows = _sb_get("articles?type=eq.AML&select=data&order=updated_at.desc&limit=1")
    if not rows:
        return []
    return rows[0].get("data") or []


def load_articles_from_local() -> list[dict]:
    if not COLLECTED_AML.exists():
        return []
    text = COLLECTED_AML.read_text(encoding="utf-8")
    articles = []
    for block in re.split(r"={5} 보도자료 \d+ ={5}", text)[1:]:
        item = {}
        for field in ("기관", "날짜", "제목", "내용", "링크", "구분"):
            m = re.search(rf"^{field}: (.+)", block, re.MULTILINE)
            item[field] = m.group(1).strip() if m else ""
        if item.get("제목"):
            articles.append(item)
    return articles


def load_policy_db() -> list[dict]:
    rows = _sb_get("policy_db?order=id.asc&limit=200")
    return rows or []


def load_already_sent_urls() -> set[str]:
    rows = _sb_get("aml_digest_sent?select=url&limit=5000")
    if not rows:
        return set()
    return {r.get("url") for r in rows if r.get("url")}


def record_sent(items: list[dict]) -> None:
    """발송된 기사 url을 aml_digest_sent에 기록."""
    payload = [
        {
            "url":        it.get("링크", "") or "",
            "risk_grade": it.get("risk_grade", ""),
            "title":      it.get("제목", "")[:300],
        }
        for it in items if it.get("링크")
    ]
    if not payload:
        return
    _sb_post("aml_digest_sent", payload)


# ── 신규 추출 + 리스크 평가 ────────────────────────────────────────────────────

def _is_recent(date_str: str, days: int = 3) -> bool:
    """기사 날짜가 최근 N일 이내인지. 파싱 실패 시 True (보수적)."""
    if not date_str:
        return True
    m = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", date_str)
    if not m:
        return True
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return True
    return (date.today() - d) <= timedelta(days=days)


def filter_and_analyze(articles: list[dict], sent_urls: set[str],
                      policy_db: list[dict]) -> list[dict]:
    """신규 기사만 골라 리스크 평가 후 등급 상·중만 반환."""
    fresh = []
    seen_url = set()
    for a in articles:
        link = a.get("링크", "") or ""
        if not link or link in sent_urls or link in seen_url:
            continue
        if not _is_recent(a.get("날짜", "")):
            continue
        seen_url.add(link)
        fresh.append(a)

    print(f"[notify] 신규 후보 {len(fresh)}건 평가 시작")

    analyzed = []
    for i, a in enumerate(fresh, 1):
        result = analyze_article(a, policy_db)
        a.update(result)
        grade = result.get("risk_grade", "무관")
        print(f"  [{i:>2}/{len(fresh)}] {grade:>2} | {a.get('제목','')[:60]}")
        analyzed.append(a)

    # 상·중만 통과, 상 우선 정렬 후 상위 MAX_CARDS 컷
    passed = [a for a in analyzed if a.get("risk_grade") in ("상", "중")]
    passed.sort(key=lambda x: 0 if x.get("risk_grade") == "상" else 1)
    total_passed = len(passed)
    if total_passed > MAX_CARDS:
        print(f"[notify] 통과(상·중) {total_passed}건 → 상위 {MAX_CARDS}건만 카드화 (나머지는 텍스트 본문에 목록)")
        passed = passed[:MAX_CARDS]
    else:
        print(f"[notify] 통과(상·중): {total_passed}건 / 전체 {len(analyzed)}건")
    return passed


# ── 카드 이미지 일괄 생성 ──────────────────────────────────────────────────────

def render_all_cards(items: list[dict], out_dir: Path,
                     digest_date: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, a in enumerate(items, 1):
        card_data = {
            "title":          a.get("제목", ""),
            "agency":         a.get("기관", ""),
            "date":           a.get("날짜", ""),
            "policy_name":    a.get("policy_name", ""),
            "summary":        a.get("summary", ""),
            "key_changes":    a.get("key_changes") or a.get("key_points", []),
            "future_plan":    a.get("future_plan", ""),
            "risk_grade":     a.get("risk_grade", "무관"),
            "risk_type":      a.get("risk_type", "무관"),
            "impacted_areas": a.get("impacted_areas", []),
            "coocon_action":  a.get("coocon_action", ""),
            "link":           a.get("링크", ""),
        }
        out = out_dir / f"card_{i:02d}.png"
        render_card(card_data, out, page_no=i, total=len(items),
                    digest_date=digest_date)
        paths.append(out)
    return paths


# ── Flow API 발송 ─────────────────────────────────────────────────────────────

def build_post_body(items: list[dict]) -> str:
    today = date.today().strftime("%Y.%m.%d")
    lines = [
        f"📋 AML 일일 리스크 다이제스트 ({today})",
        f"신규 리스크 {len(items)}건이 감지되었습니다. 아래 카드를 확인해 주세요.",
        "",
        "─" * 30,
    ]
    for i, a in enumerate(items, 1):
        grade = a.get("risk_grade", "")
        rtype = a.get("risk_type", "")
        title = a.get("제목", "")[:80]
        link  = a.get("링크", "")
        lines.append(f"{i}. [{grade}/{rtype}] {title}")
        if link:
            lines.append(f"   {link}")
    return "\n".join(lines)


def post_to_flow(items: list[dict], card_paths: list[Path]) -> bool:
    """Flow Open API로 한 포스트에 카드 이미지 N장 + 본문 텍스트 게시.

    주의: Flow Open API 엔드포인트·필드명은 워크스페이스/버전에 따라 다를 수 있음.
    환경변수 FLOW_API_BASE_PATH 로 override 가능 (기본: /api/v3).
    실패 시 fallback: 텍스트만 메시지로 전송.
    """
    if not FLOW_API_TOKEN or not FLOW_CHANNEL_ID:
        print("[ERROR] FLOW_API_TOKEN / FLOW_CHANNEL_ID 미설정 — 발송 불가")
        return False

    body = build_post_body(items)
    api_base = os.environ.get("FLOW_API_BASE_PATH", "/api/v3")

    # 1차 시도: posts 엔드포인트(multipart)
    url = f"{FLOW_BASE_URL.rstrip('/')}{api_base}/posts"
    files = []
    for i, p in enumerate(card_paths, 1):
        files.append(
            ("attachments", (p.name, open(p, "rb"), "image/png"))
        )
    data = {
        "channel_id": FLOW_CHANNEL_ID,
        "title":      f"AML 일일 리스크 다이제스트 ({date.today().strftime('%Y-%m-%d')})",
        "body":       body,
    }
    headers = {"Authorization": f"Bearer {FLOW_API_TOKEN}"}

    try:
        resp = SESSION.post(url, headers=headers, data=data, files=files, timeout=60)
        print(f"[notify] Flow POST {url} → {resp.status_code}")
        if resp.status_code in (200, 201):
            return True
        print(f"  응답: {resp.text[:500]}")
    except Exception as e:
        print(f"[notify] Flow 1차 시도 실패: {e}")
    finally:
        for _, ftuple in files:
            try:
                ftuple[1].close()
            except Exception:
                pass

    # 2차 시도: messages 엔드포인트 (텍스트만)
    fallback_url = f"{FLOW_BASE_URL.rstrip('/')}{api_base}/channels/{FLOW_CHANNEL_ID}/messages"
    try:
        resp = SESSION.post(
            fallback_url,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({"text": body}, ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        print(f"[notify] Flow fallback POST → {resp.status_code}")
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"[notify] Flow fallback 실패: {e}")
        return False


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False) -> int:
    print(f"=== AML 다이제스트 시작 (dry_run={dry_run}) ===")

    # 1. 데이터 로드
    articles = load_articles_from_supabase()
    if not articles:
        print("[notify] Supabase 미설정 또는 비어있음 → 로컬 파일 fallback")
        articles = load_articles_from_local()
    print(f"[notify] 전체 기사: {len(articles)}건")
    if not articles:
        print("[notify] 기사 없음 — 종료")
        return 0

    # 2. 발송 이력 로드
    sent_urls = load_already_sent_urls()
    print(f"[notify] 이미 발송된 url: {len(sent_urls)}건")

    # 3. 정책 DB 로드 (LLM 컨텍스트)
    policy_db = load_policy_db()
    print(f"[notify] 정책DB: {len(policy_db)}건")

    # 4. 신규 + 리스크 평가
    items = filter_and_analyze(articles, sent_urls, policy_db)
    if not items:
        print("[notify] 통과한 신규 리스크 없음 — 발송 생략하고 종료")
        return 0

    # 5. 카드 이미지 생성
    today_str = date.today().strftime("%Y%m%d")
    out_dir = BASE_DIR / "output" / f"aml_digest_{today_str}"
    card_paths = render_all_cards(
        items, out_dir, digest_date=date.today().strftime("%Y.%m.%d"))
    print(f"[notify] 카드 {len(card_paths)}장 생성 → {out_dir}")

    # 6. 발송
    if dry_run:
        print("\n--- DRY-RUN: 아래 내용이 Flow에 발송될 예정 ---")
        print(build_post_body(items))
        print(f"\n첨부 카드:")
        for p in card_paths:
            print(f"  - {p}")
        print("\n실제 발송은 생략됨 (--dry-run)")
        return 0

    ok = post_to_flow(items, card_paths)
    if ok:
        record_sent(items)
        print(f"[notify] ✓ 발송 완료 ({len(items)}건)")
        return 0
    else:
        print("[notify] ✗ 발송 실패")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Flow 발송 직전까지만 실행, PNG는 저장")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
