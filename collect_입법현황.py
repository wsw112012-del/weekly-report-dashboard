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
import re
import sys
import time
import urllib.parse
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
    "페이먼트": ["전자금융거래법", "외국환거래법"],
    "AML": [
        "특정 금융거래정보의 보고 및 이용 등에 관한 법률",
        # 2014년 개정으로 제명에 '대량살상무기확산' 포함됨. 현행 풀네임만 사용
        # (구 명칭 '공중 등 협박목적을 위한…' 행은 별도 마이그레이션으로 통합)
        "공중 등 협박목적 및 대량살상무기확산을 위한 자금조달행위의 금지에 관한 법률",
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


# ── 날짜/입법현황 파싱 helpers ───────────────────────────────────────────────
def normalize_leg_date(d: str) -> str:
    """입법현황 날짜를 YYYY.MM.DD. 형식으로 정규화"""
    if not d:
        return ""
    m = re.search(r"(\d{4})[\.\s\-]+(\d{1,2})[\.\s\-]+(\d{1,2})", d)
    if m:
        return f"{m.group(1)}.{m.group(2).zfill(2)}.{m.group(3).zfill(2)}."
    return d


def parse_lawflow(soup) -> tuple[str, str]:
    """입법현황 ul 마지막 li → (상태명, 날짜)"""
    ul = soup.find("ul", class_=re.compile(r"lawflow|flowStep", re.I))
    if not ul:
        for h3 in soup.find_all("h3"):
            if "입법현황" in h3.get_text(strip=True):
                ul = h3.find_next("ul")
                if ul:
                    break
    if not ul:
        return "", ""
    items = ul.find_all("li")
    if not items:
        return "", ""
    last_text = items[-1].get_text(strip=True)
    status = re.split(r"\s*\(", last_text)[0].strip()
    dm = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.", last_text)
    flow_date = normalize_leg_date(f"{dm.group(1)}.{dm.group(2)}.{dm.group(3)}.") if dm else ""
    return status, flow_date


# ── Supabase helpers ───────────────────────────────────────────────────────────
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


def _supabase_get(table: str, query: str = "") -> list[dict]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [WARN] Supabase GET {table}: {e}")
        return []


def _supabase_patch(table: str, query: str, data: dict) -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{query}",
        data=payload,
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
            pass
    except Exception as e:
        print(f"  [WARN] Supabase PATCH {table}: {e}")


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
        # id: href(=link 경로) 우선. 같은 bill_title 다른 link도 별개 행으로 보존.
        # href 없을 때만 law_name+bill_title 폴백.
        id_key = href if href else f"{law_name}-{bill_title}"
        items.append({
            "id": hashlib.md5(f"gov-{id_key}".encode()).hexdigest()[:12],
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
        proposer_raw = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        # 제안일 추출: "홍길동의원 등 3인(2026. 4. 15.)" → "2026.04.15."
        dm = re.search(r"\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\)", proposer_raw)
        propose_date = normalize_leg_date(f"{dm.group(1)}.{dm.group(2)}.{dm.group(3)}.") if dm else ""
        # id: href 우선, bill_no 차선, 최종 폴백은 law_name+title
        id_key = href or bill_no or f"{law_name}-{title[:30]}"
        items.append({
            "id": hashlib.md5(f"asm-{id_key}".encode()).hexdigest()[:12],
            "source": "assembly",
            "category": category,
            "target_law": law_name,
            "bill_title": title,
            "bill_type": "",
            "amendment_type": "",
            "ministry": cells[2].get_text(strip=True) if len(cells) > 2 else "",
            "status": cells[3].get_text(strip=True) if len(cells) > 3 else "",
            "proposer": proposer_raw,
            "bill_no": bill_no,
            "propose_date": propose_date,
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


# ── 기존 enrich 값 보존 머지 ───────────────────────────────────────────────────
_PRESERVE_KEYS = ("propose_date", "summary", "reason", "propose_info", "committee_review")


_DROP_IF_EMPTY = ("propose_date", "summary", "reason", "propose_info", "committee_review", "status")


def merge_with_existing(items: list[dict]) -> list[dict]:
    """재수집한 items에 Supabase의 기존 enrich 값을 보존해서 머지.
    PostgREST upsert(resolution=merge-duplicates)는 dict에 키가 있으면 값으로 덮어쓰고
    (빈 문자열도 명시적 덮어쓰기) 키가 없으면 기존 컬럼 값을 보존한다. 따라서
    1) 기존 값으로 빈 필드를 채우고, 2) 그래도 빈 채로 남은 키는 dict에서 제거한다."""
    if not SUPABASE_URL or not SUPABASE_KEY or not items:
        # Supabase 없어도 빈 키 제거는 동일 정책 적용 (로컬 저장에는 영향 없도록 사본)
        return items
    existing = _supabase_get(
        "legislation_status",
        "select=link,propose_date,status,summary,reason,propose_info,committee_review&limit=2000",
    )
    by_link = {r["link"]: r for r in existing if r.get("link")}
    for it in items:
        prev = by_link.get(it.get("link"))
        if prev:
            for k in _PRESERVE_KEYS:
                if not it.get(k) and prev.get(k):
                    it[k] = prev[k]
            if not it.get("status") and prev.get("status"):
                it["status"] = prev["status"]
        # 빈 보존 키는 dict에서 제거 — 그래야 PostgREST upsert가 기존 컬럼 값 유지
        for k in _DROP_IF_EMPTY:
            if k in it and not it[k]:
                del it[k]
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
    return results


# ── 국회입법현황 상세 수집 ──────────────────────────────────────────────────────
def _fetch_assembly_detail(link: str) -> dict:
    """국회 법안 상세 페이지에서 발의정보·제안이유·국회진행상황 파싱"""
    try:
        _ensure_lawmaking_session()
        resp = SESSION.get(link, timeout=20)
        html = resp.content.decode("utf-8", errors="replace")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        tables = soup.find_all("table")
        propose_info = ""
        reason_part = ""
        content_part = ""
        for table in tables:
            for row in table.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if not th or not td:
                    continue
                th_txt = th.get_text(strip=True)
                td_txt = td.get_text(separator="\n", strip=True)
                if "발의정보" in th_txt and not propose_info:
                    propose_info = td_txt
                elif "제안이유및주요내용" in th_txt.replace(" ", ""):
                    reason_part = td_txt
                    content_part = ""
                elif "제안이유" in th_txt and not reason_part:
                    reason_part = td_txt
                elif "주요내용" in th_txt and not content_part:
                    content_part = td_txt
        main_content = "\n".join(p for p in [reason_part, content_part] if p)

        committee_review: list[dict] = []
        for block in soup.find_all("div", class_="nsmCnt"):
            head_el = block.find("p", class_="head")
            head = head_el.get_text(strip=True) if head_el else ""
            items: list[str] = []
            result_label = ""
            result_items: list[str] = []
            in_result = False
            for child in block.children:
                if not hasattr(child, "name"):
                    continue
                if child.name == "p" and "tit" in (child.get("class") or []):
                    in_result = True
                    result_label = child.get_text(strip=True)
                elif child.name == "ul":
                    texts = [li.get_text(strip=True) for li in child.find_all("li") if li.get_text(strip=True)]
                    if in_result:
                        result_items.extend(texts)
                    else:
                        items.extend(texts)
            entry: dict = {"head": head, "items": items}
            if result_label:
                entry["result_label"] = result_label
                entry["result_items"] = result_items
            if head:
                committee_review.append(entry)

        return {"propose_info": propose_info, "summary": main_content, "committee_review": committee_review}
    except Exception as e:
        print(f"  [WARN] assembly detail ({link[:60]}): {e}")
        return {}


def _fetch_govlm_detail(link: str) -> dict:
    """정부입법현황 govLm 상세 페이지 파싱 (table th/td → h3 → dl/dt/dd 순 시도)"""
    try:
        _ensure_lawmaking_session()
        resp = SESSION.get(link, timeout=20)
        html = resp.content.decode("utf-8", errors="replace")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        import re as _re
        def _norm(s: str) -> str:
            return _re.sub(r"[\s··‧・·]+", "", s)

        _REASON_KW  = ["제·개정이유", "제개정이유", "개정이유", "제정이유", "제안이유"]
        _SUMMARY_KW = ["주요내용", "주요 내용"]

        reason = ""
        summary = ""

        # 전략 1: table th/td
        for tbl in soup.find_all("table"):
            for row in tbl.find_all("tr"):
                th = row.find("th")
                td = row.find("td")
                if not th or not td:
                    continue
                th_n = _norm(th.get_text(strip=True))
                td_txt = td.get_text(separator="\n", strip=True)
                if any(_norm(k) in th_n for k in _REASON_KW) and not reason:
                    reason = td_txt
                elif any(_norm(k) in th_n for k in _SUMMARY_KW) and not summary:
                    summary = td_txt
                elif _norm("제안이유및주요내용") in th_n or _norm("제·개정이유및주요내용") in th_n:
                    combined = td_txt
                    for sep in ["주요내용", "주요 내용"]:
                        if sep in combined:
                            parts = combined.split(sep, 1)
                            reason = reason or parts[0].strip()
                            summary = summary or parts[1].strip()
                            break
                    if not reason and not summary:
                        summary = combined

        # 전략 2: h3 sibling div
        if not summary:
            for h3 in soup.find_all("h3"):
                if any(_norm(k) in _norm(h3.get_text(strip=True)) for k in _SUMMARY_KW):
                    parent = h3.find_parent("div")
                    nxt = parent.find_next_sibling("div") if parent else None
                    if nxt:
                        summary = nxt.get_text(separator=" ", strip=True)
                        break

        # 전략 3: dl/dt/dd
        if not summary:
            for dl in soup.find_all("dl"):
                for dt in dl.find_all("dt"):
                    dd = dt.find_next_sibling("dd")
                    if not dd:
                        continue
                    dt_n = _norm(dt.get_text(strip=True))
                    dd_txt = dd.get_text(separator="\n", strip=True)
                    if any(_norm(k) in dt_n for k in _SUMMARY_KW) and not summary:
                        summary = dd_txt
                    elif any(_norm(k) in dt_n for k in _REASON_KW) and not reason:
                        reason = dd_txt

        flow_status, flow_date = parse_lawflow(soup)
        return {"summary": summary, "reason": reason,
                "flow_status": flow_status, "flow_date": flow_date}
    except Exception as e:
        print(f"  [WARN] govlm detail ({link[:60]}): {e}")
        return {}


def enrich_gov_details() -> None:
    """Supabase의 정부입법현황 항목 중 summary 없는 것들을 govLm 상세 페이지에서 채워넣기"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[INFO] SUPABASE_URL 없음 — gov 상세 수집 생략")
        return

    print("=== 정부입법현황 상세 수집 시작 ===")
    # summary 누락 + propose_date 누락 두 부류 모두 enrich 대상
    null_sum = _supabase_get("legislation_status", "source=eq.gov&summary=is.null&select=id,link&limit=500")
    empty_sum = _supabase_get("legislation_status", "source=eq.gov&summary=eq.&select=id,link&limit=500")
    null_pd = _supabase_get("legislation_status", "source=eq.gov&propose_date=is.null&select=id,link&limit=500")
    empty_pd = _supabase_get("legislation_status", "source=eq.gov&propose_date=eq.&select=id,link&limit=500")
    all_items = null_sum + empty_sum + null_pd + empty_pd

    seen: set[str] = set()
    targets: list[dict] = []
    for it in all_items:
        lk = it.get("link", "")
        if lk and lk not in seen and "/lmSts/govLm/" in lk:
            seen.add(lk)
            targets.append(it)

    print(f"  대상: {len(targets)}건")
    enriched = 0
    for it in targets:
        lk = it["link"]
        detail = _fetch_govlm_detail(lk)
        if not any(detail.get(k) for k in ("summary", "reason", "flow_date", "flow_status")):
            time.sleep(0.3)
            continue
        patch: dict = {}
        if detail.get("summary"):     patch["summary"] = detail["summary"]
        if detail.get("reason"):      patch["reason"]  = detail["reason"]
        if detail.get("flow_date"):   patch["propose_date"] = detail["flow_date"]
        if detail.get("flow_status"): patch["status"]       = detail["flow_status"]
        if not patch:
            time.sleep(0.3)
            continue
        encoded = urllib.parse.quote(lk, safe="")
        _supabase_patch("legislation_status", f"link=eq.{encoded}", patch)
        enriched += 1
        time.sleep(0.5)

    print(f"  상세 수집 완료: {enriched}건")


def enrich_assembly_details() -> None:
    """Supabase의 국회입법현황 항목 중 summary 없는 것들을 상세 페이지에서 채워넣기"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[INFO] SUPABASE_URL 없음 — 상세 수집 생략")
        return

    print("=== 국회입법현황 상세 수집 시작 ===")
    # summary가 없는 국회 항목 조회 (null + 빈 문자열 두 번 조회)
    null_items = _supabase_get(
        "legislation_status",
        "source=eq.assembly&summary=is.null&select=id,link&limit=500",
    )
    empty_items = _supabase_get(
        "legislation_status",
        "source=eq.assembly&summary=eq.&select=id,link&limit=500",
    )
    all_items = null_items + empty_items

    # 중복 링크 제거
    seen: set[str] = set()
    targets: list[dict] = []
    for it in all_items:
        lk = it.get("link", "")
        if lk and lk not in seen and "/gcom/nsmLmSts/out/" in lk:
            seen.add(lk)
            targets.append(it)

    print(f"  대상: {len(targets)}건")
    enriched = 0
    for it in targets:
        lk = it["link"]
        detail = _fetch_assembly_detail(lk)
        if not detail.get("summary") and not detail.get("propose_info"):
            time.sleep(0.3)
            continue
        patch: dict = {
            "summary":      detail.get("summary", ""),
            "propose_info": detail.get("propose_info", ""),
        }
        if detail.get("committee_review"):
            patch["committee_review"] = detail["committee_review"]
        encoded = urllib.parse.quote(lk, safe="")
        _supabase_patch("legislation_status", f"link=eq.{encoded}", patch)
        enriched += 1
        time.sleep(0.5)

    print(f"  상세 수집 완료: {enriched}건")


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
            print("기존 enrich 값 머지 중...")
            leg_items = merge_with_existing(leg_items)
            print("Supabase 업로드 중...")
            _supabase_upsert("legislation_status", leg_items)
            print("업로드 완료")
        else:
            print("[INFO] SUPABASE_URL 없음 — 로컬 저장 생략")
        enrich_gov_details()
        enrich_assembly_details()

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
