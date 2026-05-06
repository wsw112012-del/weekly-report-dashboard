"""
collect_입법현황.py

국회의원 보도자료(nanet.go.kr) + 정부/국회 입법현황(opinion.lawmaking.go.kr) 수집
결과를 Supabase에 upsert한다.

사용법:
    python collect_입법현황.py              # 입법현황 + 국회의원 보도자료 모두 수집
    python collect_입법현황.py 입법현황      # 입법현황만
    python collect_입법현황.py 보도자료      # 국회의원 보도자료만

필요 패키지:
    pip install requests beautifulsoup4 lxml python-dotenv
"""

import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib3
from datetime import date
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

# ── 환경변수 ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# ── 대상 법령 ──────────────────────────────────────────────────────────────────
LEGISLATION_TARGETS: dict[str, list[str]] = {
    "데이터": [
        "개인정보보호법",
        "신용정보의 이용 및 보호에 관한 법률",
        "정보통신망이용촉진및정보보호등에관한법률",
    ],
    "페이먼트": ["전자금융거래법"],
    "AML": [
        "특정 금융거래정보의 보고 및 이용 등에 관한 법률",
        "공중 등 협박목적을 위한 자금조달행위의 금지에 관한 법률",
    ],
}

ASSEMBLY_KEYWORDS: dict[str, list[str]] = {
    "데이터": ["개인정보", "신용정보", "정보통신망", "데이터", "마이데이터"],
    "페이먼트": ["전자금융", "결제", "핀테크", "간편결제", "지급결제", "PG", "빅테크"],
    "AML": ["자금세탁", "특정금융", "공중협박", "테러자금", "자금조달", "가상자산"],
}

_LAWMAKING_BASE = "https://opinion.lawmaking.go.kr"


# ── HTTP 세션 ──────────────────────────────────────────────────────────────────
def _make_session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    return s


SESSION = _make_session()


def _ensure_lawmaking_session() -> None:
    if not SESSION.cookies.get("JSESSIONID"):
        try:
            SESSION.get(_LAWMAKING_BASE, timeout=10)
        except Exception:
            pass


# ── Supabase upsert ────────────────────────────────────────────────────────────
def _supabase_upsert(table: str, rows: list[dict]) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY or not rows:
        return
    for row in rows:
        payload = json.dumps(row, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/{table}",
            data=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"  [WARN] Supabase upsert {table}: {e}")


# ── 정부 입법현황 스크래퍼 ──────────────────────────────────────────────────────
def scrape_govlm(law_name: str, category: str) -> list[dict]:
    _ensure_lawmaking_session()
    url = f"{_LAWMAKING_BASE}/lmSts/govLm"
    try:
        resp = SESSION.get(
            url,
            params={"lsNmKo": law_name, "govLmStsScYn": "Y", "pageIndex": "1"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [WARN] govLm ({law_name}): {e}")
        return []

    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    kw = law_name.replace(" ", "")[:6]
    items = []
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        link_tag = cells[1].find("a")
        href = (link_tag.get("href") or "") if link_tag else ""
        bill_title = cells[1].get_text(strip=True)
        if not bill_title:
            continue
        if kw not in bill_title.replace(" ", "") and kw not in cells[4].get_text(strip=True).replace(" ", ""):
            continue
        items.append({
            "id": hashlib.md5(f"gov-{law_name}-{bill_title}".encode()).hexdigest()[:12],
            "source": "gov",
            "category": category,
            "target_law": law_name,
            "bill_title": bill_title,
            "bill_type": cells[2].get_text(strip=True),
            "amendment_type": cells[3].get_text(strip=True),
            "ministry": cells[4].get_text(strip=True),
            "status": cells[5].get_text(strip=True) if len(cells) > 5 else "",
            "proposer": "",
            "bill_no": "",
            "link": (_LAWMAKING_BASE + href) if href.startswith("/") else href,
            "scraped_at": date.today().isoformat(),
        })
    return items


# ── 국회 입법현황 스크래퍼 ──────────────────────────────────────────────────────
def scrape_nsmlmsts(law_name: str, category: str) -> list[dict]:
    _ensure_lawmaking_session()
    url = f"{_LAWMAKING_BASE}/gcom/nsmLmSts/out"
    try:
        resp = SESSION.get(
            url,
            params={"issLawitmYn": "Y", "pageIndex": "1"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [WARN] nsmLmSts ({law_name}): {e}")
        return []

    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    kw = law_name.replace(" ", "")[:6]
    items = []
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        title = cells[0].get_text(strip=True)
        if not title or kw not in title.replace(" ", ""):
            continue
        link_tag = cells[0].find("a")
        href = (link_tag.get("href") or "") if link_tag else ""
        bill_no = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        items.append({
            "id": hashlib.md5(f"asm-{law_name}-{bill_no}-{title[:20]}".encode()).hexdigest()[:12],
            "source": "assembly",
            "category": category,
            "target_law": law_name,
            "bill_title": title,
            "bill_type": "",
            "amendment_type": "",
            "ministry": cells[2].get_text(strip=True) if len(cells) > 2 else "",
            "status": cells[3].get_text(strip=True) if len(cells) > 3 else "",
            "proposer": cells[1].get_text(" ", strip=True) if len(cells) > 1 else "",
            "bill_no": bill_no,
            "link": (_LAWMAKING_BASE + href) if href.startswith("/") else href,
            "scraped_at": date.today().isoformat(),
        })
    return items


# ── 국회의원 보도자료 스크래퍼 ─────────────────────────────────────────────────
def _classify_assembly_press(title: str) -> str | None:
    for cat, kws in ASSEMBLY_KEYWORDS.items():
        if any(kw in title for kw in kws):
            return cat
    return None


def scrape_assembly_press(pages: int = 10) -> list[dict]:
    url = "https://www.nanet.go.kr/lowcontent/assamblybodo/selectAssamblyBodoList.do"
    items: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = SESSION.get(url, params={"pageIndex": str(page)}, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"  [WARN] nanet page {page}: {e}")
            break
        html = resp.content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("table tbody tr")
        if not rows:
            break
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue
            link_tag = cells[3].find("a", class_="detailLink")
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            category = _classify_assembly_press(title)
            if not category:
                continue
            seq = link_tag.get("data-search-seq", "")
            items.append({
                "id": hashlib.md5(f"nanet-{seq}".encode()).hexdigest()[:12],
                "source": "nanet",
                "category": category,
                "title": title,
                "part": cells[1].get_text(strip=True),
                "org": cells[2].get_text(strip=True),
                "pub_date": cells[5].get_text(strip=True),
                "link": "https://www.nanet.go.kr/lowcontent/assamblybodo/selectAssamblyBodoList.do",
                "scraped_at": date.today().isoformat(),
            })
        time.sleep(0.3)
    return items


# ── 입법현황 전체 수집 ─────────────────────────────────────────────────────────
def collect_legislation() -> list[dict]:
    results: list[dict] = []
    for cat, laws in LEGISLATION_TARGETS.items():
        for law in laws:
            gov = scrape_govlm(law, cat)
            print(f"  govLm [{law}]: {len(gov)}건")
            results.extend(gov)
            time.sleep(0.5)
            asm = scrape_nsmlmsts(law, cat)
            print(f"  nsmLmSts [{law}]: {len(asm)}건")
            results.extend(asm)
            time.sleep(0.5)
    return results


# ── 메인 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "전체"
    run_leg = mode in ("전체", "입법현황")
    run_press = mode in ("전체", "보도자료")

    if run_leg:
        print("=== 입법현황 수집 시작 ===")
        leg_items = collect_legislation()
        print(f"총 {len(leg_items)}건 수집 완료")
        if SUPABASE_URL:
            print("Supabase 업로드 중...")
            _supabase_upsert("legislation_status", leg_items)
            print("업로드 완료")
        else:
            print("[INFO] SUPABASE_URL 없음 — 로컬 저장 생략")

    if run_press:
        print("=== 국회의원 보도자료 수집 시작 ===")
        press_items = scrape_assembly_press(pages=10)
        print(f"총 {len(press_items)}건 수집 완료")
        if SUPABASE_URL:
            print("Supabase 업로드 중...")
            _supabase_upsert("assembly_press", press_items)
            print("업로드 완료")
        else:
            print("[INFO] SUPABASE_URL 없음 — 로컬 저장 생략")
