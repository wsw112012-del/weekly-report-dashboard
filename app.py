"""
app.py — 주간보고 파이프라인 대시보드
실행: python app.py 데이터   (또는 페이먼트)
     → 브라우저 자동 오픈 + 해당 유형 자동 수집 시작
"""

import asyncio
import glob
import hashlib
import io
import json
import os
import re
import ssl as _ssl
import subprocess
import sys
import tempfile
import urllib.parse
import warnings
import webbrowser
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests as _requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings()


class _LaxSSLAdapter(HTTPAdapter):
    """SSL 검증 우회 어댑터 (회사 프록시 대응)"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        super().init_poolmanager(*args, **kwargs)


_APP_SESSION = _requests.Session()
_APP_SESSION.verify = False
_APP_SESSION.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
_app_adapter = _LaxSSLAdapter(max_retries=Retry(total=1, backoff_factor=0, status_forcelist=[429, 500, 502, 503]))
_APP_SESSION.mount('https://', _app_adapter)
_APP_SESSION.mount('http://', _app_adapter)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from bs4 import BeautifulSoup

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

BASE_DIR     = Path(__file__).parent
TEMPLATES    = BASE_DIR / "templates"
MAKE_REPORT  = BASE_DIR / "make_report.py"
COLLECT_SCR  = BASE_DIR / "collect_보도자료.py"
HISTORY_FILE       = BASE_DIR / "history.json"
LEGISLATION_FILE   = BASE_DIR / "legislation_status.json"
ASSEMBLY_PRESS_FILE = BASE_DIR / "assembly_press.json"

LEGISLATION_TARGETS: dict[str, list[str]] = {
    "데이터": [
        "개인정보보호법",
        "신용정보의 이용 및 보호에 관한 법률",
        "정보통신망이용촉진및정보보호등에관한법률",
    ],
    "페이먼트": ["전자금융거래법"],
    "AML": [
        "특정 금융거래정보의 보고 및 이용 등에 관한 법률",
        "공중 등 협박목적 및 대량살상무기확산을 위한 자금조달행위의 금지에 관한 법률",
    ],
}

ASSEMBLY_KEYWORDS: dict[str, list[str]] = {
    "데이터": ["개인정보", "신용정보", "정보통신망", "데이터", "마이데이터"],
    "페이먼트": ["전자금융", "결제", "핀테크", "간편결제", "지급결제", "PG", "빅테크"],
    "AML": ["자금세탁", "특정금융", "공중협박", "테러자금", "자금조달", "가상자산"],
}

_DEFAULT_PPT_PATH = Path(r"C:\Users\쿠콘_우승우\Desktop\업무\00. '26 쿠콘전략실\08. 주간보고\데이터전략센터\주간보고")
BASE_PPT_PATH = Path(os.environ.get("PPT_OUTPUT_DIR", str(_DEFAULT_PPT_PATH)))

AUTO_COLLECT_TYPE: str | None = os.environ.get("AUTO_COLLECT_TYPE")

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str = os.environ.get("SUPABASE_KEY", "")

WEEKDAY_KO = ['월', '화', '수', '목', '금', '토', '일']

app = FastAPI()
STATIC_DIR = BASE_DIR / "static"


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def collected_path(report_type: str) -> Path:
    return BASE_DIR / f"collected_{report_type}.txt"


def latest_ppt(report_type: str) -> str | None:
    today = date.today().strftime("%Y%m%d")
    # Render 환경: BASE_DIR/output 에 저장
    for folder in [BASE_PPT_PATH / today, BASE_DIR / "output"]:
        files = glob.glob(str(folder / "*.pptx"))
        if files:
            return max(files, key=os.path.getmtime)
    return None


# 대상 법령명 (LEGISLATION_TARGETS 자동 추출) — 가중치 ×3
_LAW_NAMES: list[str] = [
    law for laws in LEGISLATION_TARGETS.values() for law in laws
]
# 법령 단축 별칭 — 가중치 ×3
_LAW_ALIASES: list[str] = [
    "특금법", "특정금융정보법", "특정금융거래법",
    "개인정보보호법", "신용정보법", "정보통신망법",
    "전자금융거래법", "공협법", "테러자금금지법", "테러자금방지법",
]
# 법률 동작 키워드 — 가중치 ×1 (누적될수록 점수 상승)
_LAW_ACTION_KW: list[str] = [
    "개정", "제정", "시행", "입법예고", "공포", "시행령", "시행규칙",
    "고시", "훈령", "법안", "입법", "의무화", "금지", "처벌",
    "제재", "과태료", "행정처분", "위반", "규제", "제도화",
]
# 저우선 키워드 (홍보·행사성)
_PRIORITY_LOW: list[str] = [
    "소통", "간담회", "행사", "청취", "격려", "참석", "방문",
    "인사", "취임", "기념", "홍보", "인터뷰", "보도참고",
]


def get_priority(article: dict) -> str:
    """보도자료·언론기사 우선순위 자동 산정 (상/중/하).

    법령명·별칭 포함 시 가중치 3, 법률 동작 키워드 각 +1.
    총점 3 이상 → 상, 1 이상(저우선 ≤1) → 중, 법률 무관+저우선 → 하.
    """
    text = article.get("제목", "") + " " + article.get("내용", "")

    law_score    = sum(3 for law in (_LAW_NAMES + _LAW_ALIASES) if law in text)
    action_score = sum(1 for kw in _LAW_ACTION_KW if kw in text)
    low_hits     = sum(1 for kw in _PRIORITY_LOW  if kw in text)

    total = law_score + action_score

    if total >= 3:
        return "상"
    if total >= 1 and low_hits <= 1:
        return "중"
    if low_hits >= 1 and total == 0:
        return "하"
    return "중"


def _parse_from_supabase(report_type: str) -> list[dict] | None:
    """Supabase에서 기사 목록 로드. 실패 시 None 반환"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        import urllib.request, json
        # updated_at 내림차순 정렬 + limit=1 → 항상 최신 수집분만 반환 [C-1 fix]
        url = (f"{SUPABASE_URL}/rest/v1/articles"
               f"?type=eq.{urllib.parse.quote(report_type)}"
               f"&select=data&order=updated_at.desc&limit=1")
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
        if not rows:
            return None
        articles = rows[0].get("data", [])
        for a in articles:
            a["우선순위"] = get_priority(a)
        return articles
    except Exception as e:
        print(f"[WARN] Supabase 조회 실패 ({report_type}): {e}")  # [S-2 fix]
        return None


def parse_collected(report_type: str) -> list[dict]:
    # Supabase 우선, 실패 시 로컬 txt fallback
    from_db = _parse_from_supabase(report_type)
    if from_db is not None:
        return from_db

    path = collected_path(report_type)
    if not path.exists():
        return []
    text     = path.read_text(encoding="utf-8")
    articles = []
    blocks   = re.split(r"={5} 보도자료 \d+ ={5}", text)
    for block in blocks[1:]:
        item = {}
        for field in ("기관", "날짜", "제목", "내용", "링크", "구분"):
            m = re.search(rf"^{field}: (.+)", block, re.MULTILINE)
            item[field] = m.group(1).strip() if m else ""
        item["우선순위"] = get_priority(item)
        articles.append(item)
    return articles


CATEGORIES = ["데이터", "페이먼트", "AML"]


def pipeline_status() -> dict:
    result = {}
    for t in CATEGORIES:
        cp       = collected_path(t)
        ppt      = latest_ppt(t)
        articles = parse_collected(t)
        collected_at = None
        if cp.exists():
            m = re.search(r"수집일: (.+)", cp.read_text(encoding="utf-8"))
            collected_at = m.group(1).strip() if m else None
        result[t] = {
            "collected":     cp.exists(),
            "collected_at":  collected_at,
            "article_count": len(articles),
            "ppt_path":      ppt,
        }
    return result


def normalize_leg_date(d: str) -> str:
    """입법현황 날짜를 YYYY.MM.DD. 형식으로 정규화.
    '2026.2.4.' / '2026-02-04' / '2026. 2. 4.' 모두 → '2026.02.04.'"""
    if not d:
        return ''
    m = re.search(r'(\d{4})[\.\s\-]+(\d{1,2})[\.\s\-]+(\d{1,2})', d)
    if m:
        return f"{m.group(1)}.{m.group(2).zfill(2)}.{m.group(3).zfill(2)}."
    return d


def date_to_display(date_str: str) -> str:
    """'2026-04-23' → \"'26.4.23(목)\""""
    try:
        d  = datetime.strptime(date_str, "%Y-%m-%d")
        wd = WEEKDAY_KO[d.weekday()]
        return f"'{str(d.year)[2:]}.{d.month}.{d.day}({wd})"
    except Exception:
        return date_str


def _strip_prefix(s: str) -> str:
    """앞에 붙은 -, ·, • 등 기호와 공백 제거"""
    return re.sub(r"^[\s\-·•]+", "", s).strip()


def _split_bullets(text: str) -> list[str]:
    """텍스트에서 핵심 포인트 분리 — 최소 2개 보장"""
    clean = re.sub(r"\.{2,}", " ", text).strip()

    # 1차: '  - ' 또는 ' - ' 구분자
    parts = re.split(r"\s{1,2}-\s+", clean)
    parts = [_strip_prefix(p) for p in parts if len(p.strip()) > 6]
    if len(parts) >= 2:
        return parts

    # 2차: '·' '•' 구분자
    parts2 = [_strip_prefix(p) for p in re.split(r"[·•]\s*", clean) if len(p.strip()) > 6]
    if len(parts2) > len(parts):
        parts = parts2
    if len(parts) >= 2:
        return parts

    # 3차: 마침표 뒤 분리
    parts3 = [_strip_prefix(p) for p in re.split(r"\.\s+(?=[가-힣A-Z])", clean) if len(p.strip()) > 6]
    if len(parts3) > len(parts):
        parts = parts3
    if len(parts) >= 2:
        return parts

    # 4차: 쉼표 분리
    parts4 = [_strip_prefix(p) for p in re.split(r",\s+", clean) if len(p.strip()) > 6]
    if len(parts4) > len(parts):
        parts = parts4
    if len(parts) >= 2:
        return parts

    # 5차: 텍스트가 충분히 길면 반으로 분리
    if len(clean) >= 40:
        mid = clean.rfind(' ', 20, len(clean) // 2 + 20)
        if mid > 0:
            return [_strip_prefix(clean[:mid]), _strip_prefix(clean[mid:])]

    return parts if parts else [_strip_prefix(clean)]



_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

_ATTACH_ONLY = re.compile(r'첨부\s*자료[^가-힣]{0,10}참고|보도자료를 전재하여 제공')

# 기관별 직접 크롤링 설정
AGENCY_SITE_CONFIG: dict = {
    "금융위원회": {
        "base": "https://www.fsc.go.kr",
        "list_url": "https://www.fsc.go.kr/no010101",
        "link_re": re.compile(r'^/no010101/\d+'),
        "content_selectors": ["div.body", "div.cont", "div.content-body"],
    },
    "개인정보보호위원회": {
        "base": "https://www.pipc.go.kr",
        "list_url": "https://www.pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS074&mCode=C010010000",
        "link_re": re.compile(r'nttId=\d+'),
        "content_selectors": [],  # 상세 페이지가 JS 렌더링 → 본문 불가
    },
}


def _title_sim(a: str, b: str) -> float:
    """두 제목의 어절 기반 유사도 (0-1)"""
    a_w = set(re.findall(r'[가-힣a-zA-Z0-9]+', a))
    b_w = set(re.findall(r'[가-힣a-zA-Z0-9]+', b))
    if not a_w or not b_w:
        return 0.0
    return len(a_w & b_w) / max(len(a_w), len(b_w))


def _fetch_agency_list(agency: str) -> list[tuple[str, str]]:
    """기관 목록 페이지 → [(제목, 절대URL)] 반환"""
    cfg = AGENCY_SITE_CONFIG.get(agency)
    if not cfg:
        return []
    try:
        resp = _APP_SESSION.get(cfg["list_url"], timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')
        base = cfg["base"]
        link_re = cfg["link_re"]
        result = []
        seen = set()
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if not link_re.search(href):
                continue
            text = re.sub(r'\s+', ' ', a_tag.get_text(separator=' ', strip=True))
            text = re.sub(r'N$', '', text).strip()  # pipc "신규" N 마커 제거
            if not text or len(text) < 5:
                continue
            # fsc: ID가 경로에 있어 쿼리 제거, pipc: nttId가 쿼리에 있어 전체 보존
            if href.startswith('/'):
                clean_href = href.split('?')[0] if re.match(r'^/no\d+/\d+', href) else href
                full_url = base + clean_href
            else:
                full_url = href
            if full_url not in seen:
                seen.add(full_url)
                result.append((text, full_url))
        return result
    except Exception:
        return []


def _fetch_body_from_agency(agency: str, title: str) -> str:
    """기관 사이트에서 제목으로 기사를 찾아 본문 반환 (fsc만 본문 지원)"""
    cfg = AGENCY_SITE_CONFIG.get(agency)
    if not cfg or not cfg.get("content_selectors"):
        return ""
    links = _fetch_agency_list(agency)
    if not links:
        return ""
    best_url, best_score = "", 0.0
    for link_title, link_url in links:
        score = _title_sim(title, link_title)
        if score > best_score:
            best_score, best_url = score, link_url
    if best_score < 0.25 or not best_url:
        return ""
    try:
        resp = _APP_SESSION.get(best_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')
        for sel in cfg["content_selectors"]:
            div = soup.select_one(sel)
            if div:
                for tag in div.find_all(['script', 'style', 'img', 'figure']):
                    tag.decompose()
                text = re.sub(r'\s{2,}', ' ', div.get_text(separator=' ', strip=True))
                if len(text) > 80 and not _ATTACH_ONLY.search(text[:60]):
                    return text[:4000]
        return ""
    except Exception:
        return ""


def _find_agency_article_url(agency: str, title: str) -> str:
    """기관 목록에서 제목으로 가장 유사한 기사 URL 반환 (본문 불가 기관용)"""
    links = _fetch_agency_list(agency)
    if not links:
        return ""
    best_url, best_score = "", 0.0
    for link_title, link_url in links:
        score = _title_sim(title, link_title)
        if score > best_score:
            best_score, best_url = score, link_url
    return best_url if best_score >= 0.25 else ""


def _parse_odt_bytes(data: bytes) -> str:
    """ODT(ZIP) 바이트에서 content.xml 텍스트 추출"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if 'content.xml' not in z.namelist():
                return ""
            xml = z.read('content.xml').decode('utf-8', errors='replace')
            text = re.sub(r'<[^>]+>', ' ', xml)
            text = re.sub(r'\s{2,}', ' ', text).strip()
            return text[:5000] if len(text) > 80 else ""
    except Exception:
        return ""


def _fetch_odt_from_page(soup: BeautifulSoup, base_url: str) -> str:
    """페이지에서 ODT 첨부파일을 찾아 다운로드 후 본문 반환"""
    from urllib.parse import urlparse
    parsed = urlparse(base_url)

    # 중복 URL 방지용 seen set
    seen: set = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        link_text = a.get_text(strip=True).lower()

        # hwpx 링크는 건너뜀 (ODT만 처리)
        if 'hwpx' in link_text or 'hwpx' in href.lower():
            continue

        if 'download.do' not in href and 'filedown' not in href:
            continue

        if href.startswith('/'):
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        if href in seen:
            continue
        seen.add(href)

        try:
            r = _APP_SESSION.get(href, timeout=20, allow_redirects=True)
            r.raise_for_status()
            if r.content[:2] == b'PK':
                text = _parse_odt_bytes(r.content)
                if text:
                    return text
        except Exception:
            continue
    return ""


def _fetch_body(url: str) -> str:
    """보도자료 상세 페이지에서 본문 텍스트 추출 (requests 기반, ODT fallback 포함)"""
    if not url:
        return ""
    try:
        resp = _APP_SESSION.get(url, timeout=15)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, 'lxml')
        for sel in ['.article_body', '.view_content', '.articleView',
                    '#articleBody', '.news_body', '.cont_view', '.press_content']:
            div = soup.select_one(sel)
            if div:
                for tag in div.find_all(['script', 'style', 'img', 'figure']):
                    tag.decompose()
                text = re.sub(r'\s{2,}', ' ', div.get_text(separator=' ', strip=True))
                if len(text) < 80 or _ATTACH_ONLY.search(text):
                    # 본문이 없거나 "첨부 자료 참고" / "전재하여 제공"뿐 → ODT 시도
                    return _fetch_odt_from_page(soup, url)
                return text[:4000]
        # 셀렉터 미매칭 → ODT 시도
        return _fetch_odt_from_page(soup, url)
    except Exception:
        return ""


def _extract_policy_name(title: str, body: str) -> str:
    """제목·본문에서 「」로 감쌀 정책·법령 핵심 명칭 추출"""
    # 본문 앞부분의 「」 또는 ｢｣ 우선
    for src in [body[:800], title]:
        m = re.search(r'[「｢](.+?)[」｣]', src)
        if m:
            return m.group(1).strip()
    # 제목에서 법령·지침 접미어 포함 명칭 추출
    m2 = re.search(
        r'([가-힣\w\(\)·\s]{4,35}'
        r'(?:법|규정|지침|기준|제도|고시|안내서|가이드라인|업무처리기준|처리방침|작성지침))',
        title,
    )
    if m2:
        return m2.group(1).strip()
    # 접미어 없으면 제목에서 기관명/부사절 제거 후 핵심 명사구 반환
    clean = re.sub(r'\[.*?\]|\.{2,}', '', title).strip()
    # "과기정통부, ..." / "금융위, ..." 패턴에서 쉼표 이후 부분 사용
    m3 = re.match(r'^[가-힣]+(?:부|처|원|위|청|원회)[,，]\s*(.+)', clean)
    if m3:
        clean = m3.group(1).strip()
    # " - " 이후 부제목 제거
    clean = re.split(r'\s+-\s+', clean)[0].strip()
    return clean[:40]


def _extract_summary_line(title: str, body: str) -> str:
    """정책 변경 핵심을 개조식 1줄로 요약 (CLAUDE.md: '처리방침 작성지침 개정 — 생성형 AI 서비스 기준 신설' 형태)"""
    src = body[:600] if body else title

    # "OOO을/를 VERB" 패턴
    m = re.search(
        r'([가-힣\w\s「」｢｣\(\)]{5,35}(?:을|를)\s*[가-힣]{2,8}'
        r'(?:했|하였|합니다|했다|한다|할 예정|예정))',
        src,
    )
    if m and 10 < len(m.group(1)) < 60:
        return m.group(1).strip()

    # 제목에서 쉼표/접속사 뒤 동사구 분리 → "앞부분 — 뒷부분" 형태
    m2 = re.search(r'[,，]\s*(.{5,40}(?:추진|도입|시행|개정|강화|마련|신설|제정|발표|적용))', title)
    if m2:
        front = title[:title.find(m2.group(0))].strip()
        front = re.sub(r'\[.*?\]|\.{2,}', '', front).strip()
        return f"{front[:30]} — {m2.group(1).strip()}"

    # 본문 첫 유의미한 문장
    for sent in re.split(r'(?<=[다했음니])\.\s+', src):
        sent = sent.strip()
        if 15 < len(sent) < 80 and any(
            kw in sent for kw in ['개정', '시행', '도입', '신설', '제정', '강화', '발표', '마련']
        ):
            return re.sub(r'\[.*?\]|\.{2,}', '', sent).strip()

    clean = re.sub(r'\[.*?\]|\.{2,}', '', title).strip()
    return clean[:50]


def _extract_bullets(body: str, lead: str, title: str) -> list[str]:
    """본문에서 구체적 변경사항 bullet 목록 추출 (CLAUDE.md: 신설 항목·시행일·적용 대상 등)"""
    src = body if body else lead

    # ①②③④⑤ / ➊➋➌➍ 열거 항목
    enum_items = re.findall(r'[①②③④⑤⑥⑦⑧➊➋➌➍➎➏➐➑]\s*([^①②③④⑤⑥⑦⑧➊➋➌➍➎➏➐➑\n]{10,150})', src)
    if len(enum_items) >= 2:
        return [_strip_prefix(b.split('.')[0]) for b in enum_items[:4]]

    # "첫째/둘째/셋째" 항목
    ordinal = re.findall(r'(?:첫째|둘째|셋째|넷째|다섯째)[,，\s]+([^첫둘셋넷다\n]{10,120})', src)
    if len(ordinal) >= 2:
        return [_strip_prefix(b) for b in ordinal[:4]]

    # ○ 기반 항목
    circle_items = re.findall(r'[○◦]\s*([^\n○◦]{10,120})', src)
    if len(circle_items) >= 2:
        return [_strip_prefix(b) for b in circle_items[:4]]

    # 정책 키워드 포함 문장 추출
    useful = []
    for sent in re.split(r'(?<=[다했음니다])\.\s+', src[:2000]):
        sent = sent.strip()
        if 15 < len(sent) < 150 and any(
            kw in sent for kw in [
                '시행', '적용', '의무', '허용', '금지', '신설', '개정',
                '강화', '완화', '도입', '폐지', '제한', '확대', '기준',
                '선정', '지원', '확보', '추진', '발표', '마련', '구축',
            ]
        ):
            useful.append(_strip_prefix(sent))
    if useful:
        return useful[:4]

    # 최후: lead나 제목 분리
    return [_strip_prefix(p) for p in _split_bullets(lead or title) if len(p) > 10][:3]


GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")


def _llm_format_article(agency: str, title: str, date_disp: str, body: str, lead: str) -> str:
    """Gemini REST API로 CLAUDE.md 작성 규칙에 맞는 ◆/•/- 초안 생성 (gRPC 대신 REST 사용)"""
    import requests as _req, ssl as _ssl
    from requests.adapters import HTTPAdapter
    from urllib3.util.ssl_ import create_urllib3_context

    class _LaxSSL(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            ctx = create_urllib3_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            kw["ssl_context"] = ctx
            super().init_poolmanager(*a, **kw)

    session = _req.Session()
    session.verify = False
    session.mount("https://", _LaxSSL())

    src = body or lead
    prompt = f"""아래 보도자료를 정해진 형식으로 요약해줘. 형식 외 설명·머리말·꼬리말은 절대 출력하지 마.

[입력]
기관: {agency}
날짜: {date_disp}
제목: {title}
본문:
{src[:3000]}

[출력 형식 — 들여쓰기 포함 정확히 따를 것]
◆ {agency} | 「정책/사안명」  {date_disp}
  - 핵심 1줄 요약

    • 주요 내용
      - 구체적 변경사항 1
      - 구체적 변경사항 2
      - 구체적 변경사항 3

    • 향후 방향
      - 세부 내용

[작성 규칙 — 반드시 준수]
◆ 제목 줄:
- 정책/사안명은 반드시 「」 꺾쇠 괄호로 감싼다 (예: 「개인정보 처리방침 작성지침」 개정)
- 보도자료 원문 제목을 그대로 쓰지 말고 핵심 명칭으로 축약한다
- 말줄임표(...) 사용 금지 — 완결된 명칭으로 기재
- 날짜는 입력값({date_disp})을 그대로 사용

- 한 줄 요약:
- 제목·부제목을 그대로 복사하지 않는다
- 정책 변경의 핵심을 개조식 1줄로 직접 요약 (예: - 처리방침 작성지침 개정 — 생성형 AI 서비스 기준 신설)

• 주요 내용 세부 항목:
- 소제목은 반드시 "주요 내용"으로 표기 ("변경 내용" 등 다른 명칭 금지)
- 한 줄 요약과 동일한 내용을 반복하지 않는다
- 신설 항목·시행일·적용 대상·의무 사항 등 구체적 변경사항을 3개 이상 나열
- 배경 설명·기대효과·인사말 등 부연 내용 생략
- 세부 항목(-) 앞에 bullet(•)이나 - 기호를 중복으로 붙이지 않는다

• 향후 방향:
- 명확한 향후 방향이 본문에 있을 때만 포함, 없으면 섹션 자체를 생략

전체 분량은 PPT 1장에 맞게 간결하게 유지한다."""

    # gemini-2.0-flash 우선, 429 시 gemini-flash-latest 로 폴백
    for model_id in ("gemini-2.0-flash", "gemini-flash-latest"):
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_id}:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        r = session.post(url, json=payload, timeout=30)
        if r.status_code == 429:
            continue  # rate limit → 다음 모델 시도
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raise RuntimeError("Gemini API 요청 한도 초과 — 잠시 후 다시 시도해주세요")


def auto_format_article(article: dict) -> str:
    """article dict → ◆/•/- 형식 문자열 (CLAUDE.md 규칙 적용)"""
    agency    = article.get("기관", "")
    title     = re.sub(r"\s+", " ", article.get("제목", "")).strip()
    lead      = re.sub(r"\s+", " ", article.get("내용", "")).strip()
    url       = article.get("링크", "")
    date_disp = date_to_display(article.get("날짜", ""))

    # 금융정보분석원은 kofiu JSON API로 본문 우선 fetch
    if 'kofiu.go.kr' in url:
        kofiu_detail = _fetch_kofiu_detail(url)
        body = '\n'.join(filter(None, [kofiu_detail.get('reason', ''), kofiu_detail.get('summary', '')]))
    else:
        body = _fetch_body(url)

    # korea.kr 본문이 비어 있으면 기관 사이트에서 직접 시도
    if not body:
        body = _fetch_body_from_agency(agency, title)

    # 본문은 여전히 없지만 기관 사이트 URL은 찾을 수 있는 경우 (pipc 등)
    agency_url = ""
    if not body and agency in AGENCY_SITE_CONFIG and AGENCY_SITE_CONFIG[agency].get("content_selectors") == []:
        agency_url = _find_agency_article_url(agency, title)

    no_body = not body and not lead

    # Gemini API가 설정된 경우 LLM 우선 시도
    if GEMINI_API_KEY and (body or lead):
        try:
            result = _llm_format_article(agency, title, date_disp, body, lead)
            if result and "◆" in result:
                if no_body:
                    return result
                return result
        except Exception:
            pass  # fallback to regex

    # regex fallback
    policy_name = _extract_policy_name(title, body)
    summary     = _extract_summary_line(title, body)
    bullets     = _extract_bullets(body, lead, title)

    # 최소 2개 bullet 보장
    if len(bullets) < 2:
        for fb in _split_bullets(lead or title):
            if fb not in bullets and len(fb) > 10:
                bullets.append(fb)
    bullets = [b for b in bullets if b and len(b) > 8][:4]

    lines = [
        f"◆ {agency} | 「{policy_name}」  {date_disp}",
        f"  - {summary}",
        "",
        "    • 주요 내용",
    ]
    for b in bullets:
        lines.append(f"      - {b}")

    # 본문을 가져오지 못한 경우 안내 추가
    if no_body:
        lines.append("")
        if agency_url:
            lines.append(f"    ※ 본문을 가져올 수 없습니다. 원문: {agency_url}")
        else:
            lines.append("    ※ 본문을 가져올 수 없습니다. 원문 버튼으로 내용 확인 후 직접 수정해 주세요.")

    return "\n".join(lines)


# ── 라우트 ────────────────────────────────────────────────────────────────────

@app.get("/static/{filename}")
async def serve_static(filename: str):
    file_path = STATIC_DIR / filename
    if not file_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(file_path))


@app.get("/", response_class=HTMLResponse)
async def root():
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/config")
async def get_config():
    return JSONResponse({"auto_collect_type": AUTO_COLLECT_TYPE})


@app.get("/api/status")
async def get_status():
    return JSONResponse(pipeline_status())


@app.get("/api/articles/all")
async def get_all_articles():
    return JSONResponse({t: parse_collected(t) for t in CATEGORIES})


@app.get("/api/articles/{report_type}")
async def get_articles(report_type: str):
    return JSONResponse(parse_collected(report_type))


# ── 입법현황 스크래퍼 ──────────────────────────────────────────────────────────

_LAWMAKING_BASE = "https://opinion.lawmaking.go.kr"


def _ensure_lawmaking_session() -> None:
    """opinion.lawmaking.go.kr 세션 쿠키 확보 (최초 1회)"""
    if not _APP_SESSION.cookies.get('JSESSIONID'):
        try:
            _APP_SESSION.get(_LAWMAKING_BASE, timeout=10)
        except Exception:
            pass


_bill_summary_cache: dict[str, str] = {}
_bill_detail_cache: dict[str, dict] = {}


def _normalize_summary(text: str, max_chars: int = 3000) -> str:
    """입법현황 본문(summary/reason) 표현 정규화 — 모든 상세 fetch 결과에 동일 적용.

    A. 보이지 않는 글자 제거 + NBSP → 공백
    B. 가운데점/말줄임표/연속 마침표 정리
    C. 인용부호 통일 (작은/큰 따옴표, 반각→전각 꺾쇠)
    D. 공백/줄 정리 (다중 공백 단일화, trim, 빈 줄 묶음, 꺾쇠 안쪽 공백 제거)
    E. 마무리 컷 — max_chars 초과 시 마지막 문장 종료 부호 기준으로 자름
    """
    if not text:
        return text or ''

    # A. invisible 글자 제거, NBSP → 공백
    text = re.sub(r'[​-‍⁠﻿]', '', text)
    text = text.replace(' ', ' ').replace(' ', ' ')

    # B. 가운데점/말줄임표/마침표 정리
    text = re.sub(r'[‧・·]', '·', text)
    text = re.sub(r'\.{3,}', '…', text)

    # C. 인용부호 통일
    text = re.sub(r"[‘’‚′‵]", "'", text)
    text = re.sub(r'[“”„″‶]', '"', text)
    text = text.replace('｢', '「').replace('｣', '」')

    # D-1. 꺾쇠 안쪽 공백 제거: 「 법령명 」 → 「법령명」
    text = re.sub(r'「\s+', '「', text)
    text = re.sub(r'\s+」', '」', text)

    # D-2. 줄 단위 공백 정리
    out_lines: list[str] = []
    blank_pending = False
    for ln in text.split('\n'):
        ln = re.sub(r'[ \t]+', ' ', ln).strip()
        if not ln:
            blank_pending = bool(out_lines)
            continue
        if blank_pending:
            out_lines.append('')
            blank_pending = False
        out_lines.append(ln)
    result = '\n'.join(out_lines)

    # E. max_chars 컷 — 마지막 문장 종료부호까지만
    if len(result) > max_chars:
        cut = result[:max_chars]
        m = re.search(r'^.*[.!?。](?=[\s\n]|$)', cut, re.DOTALL)
        result = (m.group(0) if m else cut).rstrip()

    return result


def _parse_lawflow(soup) -> tuple[str, str]:
    """입법현황 ul 마지막 li에서 (상태명, 날짜) 추출.
    실제 govLm 페이지는 ul에 class가 없으므로 h3='입법현황' 이후 첫 ul로 탐색."""
    import re as _re
    ul = None
    # 1) class 기반 (구버전 호환)
    ul = soup.find('ul', class_=_re.compile(r'lawflow|flowStep', _re.I))
    # 2) h3='입법현황' 이후 가장 가까운 ul
    if not ul:
        for h3 in soup.find_all('h3'):
            if '입법현황' in h3.get_text(strip=True):
                ul = h3.find_next('ul')
                if ul:
                    break
    if not ul:
        return '', ''
    items = ul.find_all('li')
    if not items:
        return '', ''
    last_text = items[-1].get_text(strip=True)
    status = _re.split(r'\s*\(', last_text)[0].strip()
    date_m = _re.search(r'(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.', last_text)
    date = normalize_leg_date(f"{date_m.group(1)}.{date_m.group(2)}.{date_m.group(3)}.") if date_m else ''
    return status, date


def _gemini_generate_reason(bill_title: str, summary: str) -> str:
    """Gemini로 제·개정이유 간략 생성 (주요내용 기반)"""
    if not GEMINI_API_KEY or not bill_title:
        return ''
    import requests as _req, ssl as _ssl
    from requests.adapters import HTTPAdapter
    from urllib3.util.ssl_ import create_urllib3_context

    class _LaxSSL(HTTPAdapter):
        def init_poolmanager(self, *a, **kw):
            ctx = create_urllib3_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            kw['ssl_context'] = ctx
            super().init_poolmanager(*a, **kw)

    sess = _req.Session()
    sess.verify = False
    sess.mount('https://', _LaxSSL())

    prompt = f"""아래 법령안의 '제·개정이유'를 2~3문장으로 간략히 작성해줘.
설명·머리말·꼬리말 없이 이유 문장만 출력할 것.

법령안명: {bill_title}
주요내용: {summary[:500] if summary else '없음'}

제·개정이유 (2~3문장):"""

    for model_id in ('gemini-2.0-flash', 'gemini-flash-latest'):
        try:
            url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
                   f'{model_id}:generateContent?key={GEMINI_API_KEY}')
            r = sess.post(url, json={'contents': [{'parts': [{'text': prompt}]}]}, timeout=20)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        except Exception:
            continue
    return ''


def _fetch_kofiu_detail(link: str) -> dict:
    """kofiu.go.kr 보도자료 상세에서 개정배경/주요내용/향후계획 추출"""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(link)
    item_no = parse_qs(parsed.query).get('ntcnYardOrdrNo', [''])[0]
    if not item_no:
        return {}
    try:
        resp = _APP_SESSION.get(
            'https://www.kofiu.go.kr/cmn/board/selectBoardDetail.do',
            params={'ntcnYardOrdrNo': item_no, 'seCd': ''},
            headers={'X-Requested-With': 'XMLHttpRequest'},
            timeout=15,
        )
        resp.raise_for_status()
        item = resp.json().get('result', {})
        html_cn = item.get('ntcnYardCn', '')
        if not html_cn:
            return {}

        soup = BeautifulSoup(html_cn, 'lxml')

        _REASON_KW   = ['개정 배경', '개정배경', '추진 배경', '추진배경', '제·개정 이유', '제개정이유']
        _SUMMARY_KW  = ['주요 개정 내용', '주요개정내용', '주요 내용', '주요내용', '개정 내용', '개정내용']
        _FUTURE_KW   = ['향후 계획', '향후계획', '향후 방향', '향후방향']
        _ALL_SECTION = _REASON_KW + _SUMMARY_KW + _FUTURE_KW

        def _norm(s: str) -> str:
            return re.sub(r'[\s··‧・·]+', '', s)

        def _is_section_hdr(line: str) -> bool:
            t = _norm(line.strip())
            return bool(t) and any(_norm(k) == t for k in _ALL_SECTION)

        def _clean_full(raw: str) -> str:
            # MS Word 조건부주석 제거
            text = re.sub(r'\[if [^\]]+\].*?\[endif\]', '', raw, flags=re.DOTALL)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()

        # 블록 요소에만 개행 삽입, 인라인(span 등)은 텍스트 노드 그대로 이어붙임
        _BLOCK = {'p', 'div', 'tr', 'li', 'br', 'h1', 'h2', 'h3', 'h4', 'td', 'th'}
        from bs4 import NavigableString, Comment
        def _extract_text(node) -> str:
            if isinstance(node, Comment):
                return ''
            if isinstance(node, NavigableString):
                return str(node)
            if node.name in _BLOCK:
                inner = ''.join(_extract_text(c) for c in node.children)
                return '\n' + inner + '\n'
            return ''.join(_extract_text(c) for c in node.children)

        raw_text = _extract_text(soup)
        raw_text = _clean_full(raw_text)
        lines = [ln.strip() for ln in raw_text.split('\n')]

        def _section_text(keywords: list[str]) -> str:
            """full_text에서 섹션 헤더 사이 텍스트 추출"""
            hdr_idx = None
            for i, ln in enumerate(lines):
                if any(_norm(k) == _norm(ln.strip()) for k in keywords):
                    hdr_idx = i
                    break
            if hdr_idx is None:
                return ''
            parts = []
            for ln in lines[hdr_idx + 1:]:
                if _is_section_hdr(ln):
                    break
                parts.append(ln)
            return _clean_full('\n'.join(parts))

        reason  = _section_text(_REASON_KW)
        summary = _section_text(_SUMMARY_KW)
        future  = _section_text(_FUTURE_KW)

        if future:
            summary = (summary + '\n\n향후 계획\n' + future).strip() if summary else future

        # 주요 내용 없으면 불렛/본문 기반 fallback
        # kofiu 보도참고는 "주요 개정 내용" 헤더 없이 (Wingdings 네모) 또는
        # ■/□/▶/◆/- 로 핵심 bullet을 표시 → 헤더 미발견 시 본문 전체 묶음 사용
        if not summary:
            # 헤더 없는 보도참고 — 본문 줄을 원문 순서 그대로 묶고 bullet만 정규화.
            # 다양한 글머리(- * • ■ □ ▶ ◆ ● ○ ※ ★ ◇ ▣ ▪ ▫ √ ⇨ Wingdings /)는 "· "로 통일.
            _BULLET_RE = re.compile('^[\\-\\*•■□▶◆●○※★◇▣▪▫√⇨]\\s*')
            kept: list[str] = []
            for ln in lines:
                if not ln or _is_section_hdr(ln):
                    continue
                m = _BULLET_RE.match(ln)
                if m:
                    kept.append('· ' + ln[m.end():].lstrip())
                elif len(ln) >= 8:
                    kept.append(ln)
            summary = '\n'.join(kept)[:3000]

        # flow_status: 제목에서 추출
        title = item.get('ntcnYardSjNm', '')
        flow_status = next(
            (kw for kw in ['국무회의 의결', '시행', '공포', '입법예고'] if kw in title), ''
        )
        flow_date = normalize_leg_date((item.get('ntcnYardRgiDt') or '')[:10])

        return {'summary': _normalize_summary(summary), 'reason': _normalize_summary(reason),
                'flow_status': flow_status, 'flow_date': flow_date}
    except Exception as e:
        print(f'[WARN] kofiu detail error ({link[:60]}): {e}')
        return {}


def _fetch_govlm_detail(link: str) -> dict:
    """정부입법현황 govLm 상세 페이지 파싱 (opinion.lawmaking.go.kr/lmSts/govLm/.../detailRP)"""
    try:
        _ensure_lawmaking_session()
        resp = _APP_SESSION.get(link, timeout=20)
        html = resp.content.decode('utf-8', errors='replace')
        soup = BeautifulSoup(html, 'lxml')

        def _norm(s: str) -> str:
            return re.sub(r'[\s··‧・·]+', '', s)

        reason = ''
        summary = ''

        # ── 전략 1: table th/td 구조 (govLm 상세 페이지 주요 포맷) ──
        _REASON_KW  = ['제·개정이유', '제개정이유', '개정이유', '제정이유', '제안이유']
        _SUMMARY_KW = ['주요내용', '주요 내용']
        for tbl in soup.find_all('table'):
            for row in tbl.find_all('tr'):
                th = row.find('th')
                td = row.find('td')
                if not th or not td:
                    continue
                th_n = _norm(th.get_text(strip=True))
                td_txt = td.get_text(separator='\n', strip=True)
                if any(_norm(k) in th_n for k in _REASON_KW) and not reason:
                    reason = td_txt
                elif any(_norm(k) in th_n for k in _SUMMARY_KW) and not summary:
                    summary = td_txt
                elif _norm('제안이유및주요내용') in th_n or _norm('제·개정이유및주요내용') in th_n:
                    # 하나의 th에 둘 다 있는 경우
                    combined = td_txt
                    for sep in ['주요내용', '주요 내용']:
                        if sep in combined:
                            parts = combined.split(sep, 1)
                            reason = reason or parts[0].strip()
                            summary = summary or parts[1].strip()
                            break
                    if not reason and not summary:
                        summary = combined

        # ── 전략 2: h3 헤더 이후 콘텐츠 탐색 (govLm 실제 페이지 구조) ──
        def _content_after_h3(h3_tag):
            """h3 이후 실제 본문 콘텐츠 탐색.
            UI 버튼 텍스트('전체 펼침' 등 짧은 side class)는 건너뜀."""
            _SKIP_CLASSES = {'side', 'btn', 'button', 'more', 'toggle'}
            _MIN_LEN = 20  # 20자 미만은 UI 버튼으로 간주하고 건너뜀

            # 1) 같은 컨테이너 내 h3 이후 형제 탐색
            for sib in h3_tag.find_next_siblings():
                if sib.name in ('h3', 'h2', 'h4'):
                    break
                cls = set(sib.get('class') or [])
                if cls & _SKIP_CLASSES:
                    continue
                txt = sib.get_text(separator='\n', strip=True)
                if txt and len(txt) >= _MIN_LEN:
                    return txt

            # 2) 부모 div의 next-sibling div (govLm 실제 구조)
            parent = h3_tag.find_parent('div')
            if parent:
                nxt = parent.find_next_sibling('div')
                if nxt:
                    txt = nxt.get_text(separator='\n', strip=True)
                    if txt and len(txt) >= _MIN_LEN:
                        return txt
            return ''

        if not summary:
            for h3 in soup.find_all('h3'):
                h3_n = _norm(h3.get_text(strip=True))
                if any(_norm(k) in h3_n for k in _SUMMARY_KW):
                    summary = _content_after_h3(h3)
                    if summary:
                        break
        if not reason:
            for h3 in soup.find_all('h3'):
                h3_n = _norm(h3.get_text(strip=True))
                if any(_norm(k) in h3_n for k in _REASON_KW):
                    reason = _content_after_h3(h3)
                    if reason:
                        break

        # ── 전략 3: dl/dt/dd 구조 ──
        if not summary:
            for dl in soup.find_all('dl'):
                for dt in dl.find_all('dt'):
                    dt_n = _norm(dt.get_text(strip=True))
                    dd = dt.find_next_sibling('dd')
                    if not dd:
                        continue
                    dd_txt = dd.get_text(separator='\n', strip=True)
                    if any(_norm(k) in dt_n for k in _SUMMARY_KW) and not summary:
                        summary = dd_txt
                    elif any(_norm(k) in dt_n for k in _REASON_KW) and not reason:
                        reason = dd_txt

        flow_status, flow_date = _parse_lawflow(soup)

        # ── 제·개정이유 없으면 Gemini 생성 ──
        if not reason and (summary or link):
            bill_title_from_page = ''
            title_tag = soup.find('title')
            if title_tag:
                bill_title_from_page = title_tag.get_text(strip=True).split('|')[0].strip()
            if not bill_title_from_page:
                for sel in ['h2.pageTitle', 'h2', '.bill-title']:
                    el = soup.select_one(sel)
                    if el:
                        bill_title_from_page = el.get_text(strip=True)
                        break
            if bill_title_from_page or summary:
                reason = _gemini_generate_reason(bill_title_from_page, summary)

        return {
            'summary': _normalize_summary(summary),
            'reason': _normalize_summary(reason),
            'flow_status': flow_status,
            'flow_date': flow_date,
            'is_assembly': False,
        }
    except Exception as e:
        print(f'[WARN] govlm detail error ({link[:60]}): {e}')
        return {}


def _fetch_assembly_bill_detail(link: str) -> dict:
    """국회입법현황 상세 페이지 파싱 (opinion.lawmaking.go.kr/gcom/nsmLmSts/out/.../detailRP)"""
    try:
        _ensure_lawmaking_session()
        resp = _APP_SESSION.get(link, timeout=20)
        html = resp.content.decode('utf-8', errors='replace')
        soup = BeautifulSoup(html, 'lxml')

        tables = soup.find_all('table')

        # ── 기본정보 (모든 테이블 순회 — 페이지마다 테이블 위치가 다를 수 있음) ──
        propose_info = ''
        reason_part = ''
        content_part = ''
        for table in tables:
            for row in table.find_all('tr'):
                th = row.find('th')
                td = row.find('td')
                if not th or not td:
                    continue
                th_txt = th.get_text(strip=True)
                td_txt = td.get_text(separator='\n', strip=True)
                if '발의정보' in th_txt and not propose_info:
                    propose_info = td_txt
                elif '제안이유및주요내용' in th_txt.replace(' ', ''):
                    # 통합 셀인 경우 전체를 main_content로
                    reason_part = td_txt
                    content_part = ''
                elif '제안이유' in th_txt and not reason_part:
                    reason_part = td_txt
                elif '주요내용' in th_txt and not content_part:
                    content_part = td_txt
        # 제안이유 + 주요내용 합산
        main_content = '\n'.join(p for p in [reason_part, content_part] if p)

        # ── 국회진행상황 (div.nsmCnt 블록들) ──
        committee_review: list[dict] = []
        for block in soup.find_all('div', class_='nsmCnt'):
                head_el = block.find('p', class_='head')
                head = head_el.get_text(strip=True) if head_el else ''
                items: list[str] = []
                result_label = ''
                result_items: list[str] = []
                in_result = False
                for child in block.children:
                    if not hasattr(child, 'name'):
                        continue
                    if child.name == 'p' and 'tit' in (child.get('class') or []):
                        in_result = True
                        result_label = child.get_text(strip=True)
                    elif child.name == 'ul':
                        texts = [li.get_text(strip=True) for li in child.find_all('li') if li.get_text(strip=True)]
                        if in_result:
                            result_items.extend(texts)
                        else:
                            items.extend(texts)
                entry: dict = {'head': head, 'items': items}
                if result_label:
                    entry['result_label'] = result_label
                    entry['result_items'] = result_items
                if head:
                    committee_review.append(entry)

        return {
            'propose_info': _normalize_summary(propose_info, max_chars=1000),
            'summary': _normalize_summary(main_content),
            'reason': '',
            'committee_review': committee_review,
            'flow_status': '',
            'flow_date': '',
            'is_assembly': True,
        }
    except Exception as e:
        print(f'[WARN] assembly bill detail error ({link[:60]}): {e}')
        return {}


def _fetch_bill_detail(link: str) -> dict:
    """법안 상세 페이지에서 제·개정이유 + 주요내용 + 입법현황(상태, 날짜) 추출"""
    if not link:
        return {}
    if link in _bill_detail_cache:
        return _bill_detail_cache[link]
    # kofiu.go.kr 링크는 별도 처리
    if 'kofiu.go.kr' in link:
        result = _fetch_kofiu_detail(link)
        _bill_detail_cache[link] = result
        return result
    # 국회입법현황 링크는 별도 처리
    if '/gcom/nsmLmSts/out/' in link:
        result = _fetch_assembly_bill_detail(link)
        if result:
            _bill_detail_cache[link] = result
        return result
    # 정부입법현황 govLm 링크는 전용 파서 사용
    if '/lmSts/govLm/' in link:
        result = _fetch_govlm_detail(link)
        _bill_detail_cache[link] = result
        return result
    try:
        _ensure_lawmaking_session()
        resp = _APP_SESSION.get(link, timeout=20)
        html = resp.content.decode('utf-8', errors='replace')
        soup = BeautifulSoup(html, 'lxml')

        def _norm(s: str) -> str:
            return re.sub(r'[\s··‧・]+', '', s)

        def _extract_section(keywords: list[str]) -> str:
            for h3 in soup.find_all('h3'):
                h3_norm = _norm(h3.get_text(strip=True))
                for kw in keywords:
                    if _norm(kw) in h3_norm:
                        parent = h3.find_parent('div')
                        nxt = parent.find_next_sibling('div') if parent else None
                        if nxt:
                            return nxt.get_text(separator='\n', strip=True)
            return ''

        reason  = _extract_section(['제·개정이유', '제개정이유', '개정이유', '제정이유'])
        summary = _extract_section(['주요내용'])
        flow_status, flow_date = _parse_lawflow(soup)

        # 페이지 제목에서 법령안명 추출
        bill_title_from_page = ''
        title_tag = soup.find('title')
        if title_tag:
            bill_title_from_page = title_tag.get_text(strip=True).split('|')[0].strip()
        if not bill_title_from_page:
            for sel in ['h2.pageTitle', 'h2', '.bill-title', '.lm-title']:
                el = soup.select_one(sel)
                if el:
                    bill_title_from_page = el.get_text(strip=True)
                    break

        # 제·개정이유가 AJAX로 비어있으면 Gemini로 생성
        if not reason and (summary or bill_title_from_page):
            reason = _gemini_generate_reason(bill_title_from_page, summary)

        result = {'summary': _normalize_summary(summary), 'reason': _normalize_summary(reason),
                  'flow_status': flow_status, 'flow_date': flow_date}
        _bill_detail_cache[link] = result
        return result
    except Exception as e:
        print(f'[WARN] bill detail fetch error ({link[:60]}): {e}')
        return {}


def _fetch_bill_summary(link: str) -> str:
    """법안 상세 페이지에서 주요내용 추출 (bill_detail 위임)"""
    return _fetch_bill_detail(link).get('summary', '')


def _scrape_govlm(law_name: str, category: str) -> list[dict]:
    """정부 입법현황 - GET + lsNmKo 파라미터"""
    _ensure_lawmaking_session()
    url = f"{_LAWMAKING_BASE}/lmSts/govLm"
    try:
        resp = _APP_SESSION.get(url,
            params={"lsNmKo": law_name, "govLmStsScYn": "Y", "pageIndex": "1"},
            timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] govLm fetch error ({law_name}): {e}")
        return []
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'lxml')
    items = []
    for row in soup.select('table tbody tr'):
        cells = row.find_all('td')
        if len(cells) < 5:
            continue
        link_tag = cells[1].find('a')
        href = (link_tag.get('href') or '') if link_tag else ''
        bill_title = cells[1].get_text(strip=True)
        if not bill_title:
            continue
        # 서버측 lsNmKo 필터 후 클라이언트에서 법령명 핵심어 재검증
        kw = law_name.replace(' ', '')[:6]
        if kw not in bill_title.replace(' ', ''):
            continue
        # 목록 테이블에 발의일자 컬럼 없음 — 상세 enrich(_parse_lawflow) 시 채워짐
        propose_date = ''
        items.append({
            "id": hashlib.md5(f"gov-{href or (law_name + bill_title + cells[2].get_text(strip=True))}".encode()).hexdigest()[:12],
            "source": "gov",
            "category": category,
            "target_law": law_name,
            "bill_title": bill_title,
            "bill_type": cells[2].get_text(strip=True),
            "amendment_type": cells[3].get_text(strip=True),
            "ministry": cells[4].get_text(strip=True),
            "status": cells[5].get_text(strip=True) if len(cells) > 5 else '',
            "proposer": "",
            "bill_no": "",
            "propose_date": propose_date,
            "link": (_LAWMAKING_BASE + href) if href.startswith('/') else href,
            "scraped_at": date.today().isoformat(),
        })
    return items


def _scrape_nsmlmsts(law_name: str, category: str) -> list[dict]:
    """국회 입법현황 — scBlNmSct 서버 사이드 키워드 필터 + pageSize=100"""
    _ensure_lawmaking_session()
    url = f"{_LAWMAKING_BASE}/gcom/nsmLmSts/out"
    items = []
    for page in range(1, 20):
        try:
            resp = _APP_SESSION.get(url, params={
                "scBlNm": "scBlNm_blNm",
                "scBlNmSct": law_name,
                "pageSize": "100",
                "pageIndex": str(page),
            }, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"[WARN] nsmLmSts fetch error ({law_name} p{page}): {e}")
            break
        raw = resp.content.decode('utf-8', errors='replace')
        soup = BeautifulSoup(raw, 'lxml')
        rows = soup.select('table tbody tr')
        if not rows:
            break
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 4:
                continue
            title = cells[0].get_text(strip=True)
            if not title:
                continue
            link_tag = cells[0].find('a')
            href = (link_tag.get('href') or '') if link_tag else ''
            bill_no = cells[5].get_text(strip=True) if len(cells) > 5 else ''
            proposer_raw = cells[1].get_text(' ', strip=True) if len(cells) > 1 else ''
            # 제안일 추출: "홍길동의원 등 3인(2026. 4. 15.)" → "2026.4.15."
            dm = re.search(r'\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?\)', proposer_raw)
            propose_date = normalize_leg_date(f"{dm.group(1)}.{dm.group(2)}.{dm.group(3)}.") if dm else ''
            items.append({
                "id": hashlib.md5(f"nsm-{bill_no or href or (law_name + title[:30])}".encode()).hexdigest()[:12],
                "source": "assembly",
                "category": category,
                "target_law": law_name,
                "bill_title": title,
                "bill_type": "",
                "amendment_type": "",
                "ministry": cells[2].get_text(strip=True) if len(cells) > 2 else '',
                "status": cells[3].get_text(strip=True) if len(cells) > 3 else '',
                "proposer": proposer_raw,
                "bill_no": bill_no,
                "propose_date": propose_date,
                "link": (_LAWMAKING_BASE + href) if href.startswith('/') else href,
                "scraped_at": date.today().isoformat(),
            })
        # 단일 페이지 결과면 종료
        paging = soup.select('.paging a, .pagination a')
        page_nums = [a.get_text(strip=True) for a in paging if a.get_text(strip=True).isdigit()]
        if not page_nums or max(int(p) for p in page_nums) <= page:
            break
    return items


def _classify_assembly_press(title: str) -> str | None:
    """보도자료 제목 키워드로 데이터/페이먼트/AML 분류. 해당 없으면 None"""
    for cat, kws in ASSEMBLY_KEYWORDS.items():
        if any(kw in title for kw in kws):
            return cat
    return None


def _scrape_assembly_press() -> list[dict]:
    """nanet.go.kr 국회의원 보도자료 수집 (데이터/페이먼트/AML 키워드 필터)"""
    url = "https://www.nanet.go.kr/lowcontent/assamblybodo/selectAssamblyBodoList.do"
    items: list[dict] = []
    for page in range(1, 11):  # 최근 10페이지(200건)
        try:
            resp = _APP_SESSION.get(url, params={"pageIndex": str(page)}, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"[WARN] nanet page {page}: {e}")
            break
        html = resp.content.decode('utf-8', errors='replace')
        soup = BeautifulSoup(html, 'lxml')
        rows = soup.select('table tbody tr')
        if not rows:
            break
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 6:
                continue
            link_tag = cells[3].find('a', class_='detailLink')
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            category = _classify_assembly_press(title)
            if not category:
                continue
            seq = link_tag.get('data-search-seq', '')
            part = cells[1].get_text(strip=True)
            org = cells[2].get_text(strip=True)
            pub_date = cells[5].get_text(strip=True)
            items.append({
                "id": hashlib.md5(f"nanet-{seq}".encode()).hexdigest()[:12],
                "source": "nanet",
                "category": category,
                "title": title,
                "part": part,
                "org": org,
                "pub_date": pub_date,
                "link": f"https://www.nanet.go.kr/lowcontent/assamblybodo/selectAssamblyBodoList.do",
                "scraped_at": date.today().isoformat(),
            })
    return items


def collect_legislation_status() -> list[dict]:
    """대상 법령의 정부+국회 입법현황 수집 (opinion.lawmaking.go.kr)"""
    results: list[dict] = []
    for cat, laws in LEGISLATION_TARGETS.items():
        for law in laws:
            gov_items = _scrape_govlm(law, cat)
            print(f"  govLm [{law}]: {len(gov_items)}건")
            results.extend(gov_items)
            asm_items = _scrape_nsmlmsts(law, cat)
            print(f"  nsmLmSts [{law}]: {len(asm_items)}건")
            results.extend(asm_items)
    return results


_POLICY_COLUMNS = [
    "id","정책명","구분","주무부처","사업영역","영향도",
    "공포일","시행일","상태","핵심내용","쿠콘액션",
    "담당부서","내부대응기한","모니터링주기","출처URL","트래킹여부","비고"
]

_POLICY_DEFAULT = [
    {"id":"P001","정책명":"전자금융거래법 개정 (PG 정산자금 외부관리)","구분":"법률","주무부처":"금융위원회","사업영역":"PG/결제","영향도":"높음","공포일":"2025-12-16","시행일":"2026-12-17","상태":"시행예정","핵심내용":"PG업자 정산자금 전액 외부관리 의무화(예치/신탁/보증보험). 자본금 요건 상향.","쿠콘액션":"① 정산자금 외부관리 이행 현황 점검\n② 자본금 요건 충족 여부 확인","담당부서":"전략팀/재무팀","내부대응기한":"2026-11-17","모니터링주기":"월간","출처URL":"https://www.fsc.go.kr","트래킹여부":"Y","비고":""},
    {"id":"P002","정책명":"PG업자 정산자금 외부관리 가이드라인","구분":"가이드라인","주무부처":"금융감독원","사업영역":"PG/결제","영향도":"높음","공포일":"2025-09-17","시행일":"2026-01-01","상태":"시행중","핵심내용":"정산자금 매일 산정. 60% 이상 신탁/지급보증보험 외부관리.","쿠콘액션":"이행 현황 점검 및 내부 보고체계 구축","담당부서":"전략팀","내부대응기한":"2026-03-31","모니터링주기":"분기","출처URL":"https://www.fss.or.kr","트래킹여부":"Y","비고":""},
    {"id":"P003","정책명":"개인정보보호법 개정 (전송요구권)","구분":"법률","주무부처":"개인정보보호위원회","사업영역":"마이데이터","영향도":"높음","공포일":"2025-04-01","시행일":"2025-10-02","상태":"시행중","핵심내용":"개인정보 전송요구권 도입(제35조의2). 보건의료/통신/에너지 분야부터 단계 적용.","쿠콘액션":"전송요구 처리 절차 수립, API 연동 준비","담당부서":"전략팀/개발팀","내부대응기한":"2026-06-30","모니터링주기":"월간","출처URL":"https://www.pipc.go.kr","트래킹여부":"Y","비고":""},
    {"id":"P004","정책명":"특정금융거래정보법 (특금법)","구분":"법률","주무부처":"금융정보분석원","사업영역":"AML/KYC","영향도":"높음","공포일":"","시행일":"","상태":"시행중","핵심내용":"VASP 신고 의무, AML/CFT, 트래블룰(건당 100만원 이상 가상자산 출고 시 정보전달) 시행 중","쿠콘액션":"트래블룰 시스템 운영 점검, KYC 프로세스 강도 확인 (분기)","담당부서":"AML팀","내부대응기한":"","모니터링주기":"분기","출처URL":"","트래킹여부":"Y","비고":""},
    {"id":"P005","정책명":"가상자산 이용자 보호법","구분":"법률","주무부처":"금융위원회","사업영역":"가상자산","영향도":"높음","공포일":"2024-01-01","시행일":"2024-07-19","상태":"시행중","핵심내용":"이용자 예치금 분리 보관, 불공정거래 금지, 사업자 배상책임","쿠콘액션":"스테이블코인 결제 인프라 연동 시 이용자보호 요건 충족 여부 확인","담당부서":"전략팀","내부대응기한":"","모니터링주기":"분기","출처URL":"","트래킹여부":"Y","비고":""},
    {"id":"P006","정책명":"디지털자산기본법 (2단계 입법)","구분":"법률","주무부처":"금융위원회","사업영역":"가상자산","영향도":"높음","공포일":"","시행일":"","상태":"계류중","핵심내용":"스테이블코인 발행주체 요건, 거래소 지배구조 등. 정무위 소위 계류 중 (2026.4 기준)","쿠콘액션":"입법 동향 주간 모니터링, 발행주체·준비금 요건 확정 즉시 사업모델 검토","담당부서":"전략팀","내부대응기한":"","모니터링주기":"주간","출처URL":"","트래킹여부":"Y","비고":""},
]


def _policy_db_load() -> list[dict]:
    """Supabase policy_db 테이블 로드. 없으면 기본 데이터 반환."""
    rows = _supabase_request("GET", "policy_db?order=id.asc&limit=500")
    if rows:
        return rows
    return _POLICY_DEFAULT


def _policy_db_seed():
    """Supabase가 비어 있으면 기본 데이터 삽입"""
    rows = _supabase_request("GET", "policy_db?limit=1")
    if rows is not None and len(rows) == 0:
        for row in _POLICY_DEFAULT:
            _supabase_request("POST", "policy_db", row)


@app.get("/api/policydb")
async def get_policydb():
    return JSONResponse(_policy_db_load())


@app.post("/api/policydb")
async def create_policy(req: Request):
    body = await req.json()
    # id 자동 생성
    existing = _policy_db_load()
    nums = [int(r["id"][1:]) for r in existing if r.get("id","").startswith("P") and r["id"][1:].isdigit()]
    new_num = max(nums, default=0) + 1
    body["id"] = f"P{new_num:03d}"
    result = _supabase_request("POST", "policy_db", body)
    return JSONResponse(body if result is None else (result[0] if isinstance(result, list) else result))


@app.put("/api/policydb/{policy_id}")
async def update_policy(policy_id: str, req: Request):
    body = await req.json()
    body["id"] = policy_id
    result = _supabase_request("PATCH", f"policy_db?id=eq.{policy_id}", body)
    return JSONResponse({"ok": True})


@app.delete("/api/policydb/{policy_id}")
async def delete_policy(policy_id: str):
    _supabase_request("DELETE", f"policy_db?id=eq.{policy_id}")
    return JSONResponse({"ok": True})


@app.get("/api/legislation")
async def get_legislation():
    """대상 법령 입법현황 반환 (Supabase 우선, 로컬 파일 fallback)"""
    rows = _supabase_request("GET", "legislation_status?order=scraped_at.desc&limit=500") or []
    local: list = []
    if LEGISLATION_FILE.exists():
        try:
            local = json.loads(LEGISLATION_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    # 로컬 파일이 더 많으면 로컬 우선 (Supabase upsert 미완료 상황 대응)
    if len(local) > len(rows):
        return JSONResponse(local)
    if rows:
        return JSONResponse(rows)
    return JSONResponse([])


@app.get("/api/bill-summary")
async def get_bill_summary(link: str = ""):
    """법안 상세 페이지 요약 반환 (제·개정이유 + 주요내용 + 입법현황)"""
    if not link:
        return JSONResponse({"summary": "", "reason": "", "flow_status": "", "flow_date": ""})

    # Supabase 캐시 우선 조회 (Render geo-block 우회)
    if SUPABASE_URL and SUPABASE_KEY:
        encoded = urllib.parse.quote(link, safe='')
        cached = _supabase_request(
            "GET",
            f"legislation_status?link=eq.{encoded}&select=summary,reason,status,propose_date,propose_info,committee_review&limit=1",
        )
        row = cached[0] if cached else None
        # summary가 비어도 reason/status/propose_date 중 하나라도 있으면 부분 캐시 히트
        cache_hit = row and any(row.get(k) for k in ("summary", "reason", "status", "propose_date"))
        if cache_hit and row.get("summary"):
            is_asm = '/gcom/nsmLmSts/out/' in link
            return JSONResponse({
                "summary":          row.get("summary", ""),
                "reason":           row.get("reason", ""),
                "flow_status":      row.get("status", ""),
                "flow_date":        row.get("propose_date", ""),
                "propose_info":     row.get("propose_info", ""),
                "committee_review": row.get("committee_review") or [],
                "is_assembly":      is_asm,
            })
        # summary 누락된 부분 캐시 — 실시간 스크래핑 후 Supabase 보강
        if cache_hit and not row.get("summary"):
            loop2 = asyncio.get_running_loop()
            fresh = await loop2.run_in_executor(None, _fetch_bill_detail, link)
            if fresh.get("summary"):
                encoded2 = urllib.parse.quote(link, safe='')
                patch: dict = {"summary": fresh["summary"], "reason": fresh.get("reason", "") or row.get("reason", "")}
                if fresh.get("flow_date"):   patch["propose_date"] = fresh["flow_date"]
                if fresh.get("flow_status"): patch["status"]       = fresh["flow_status"]
                if fresh.get("propose_info"): patch["propose_info"] = fresh["propose_info"]
                if fresh.get("committee_review"): patch["committee_review"] = fresh["committee_review"]
                _supabase_request("PATCH", f"legislation_status?link=eq.{encoded2}", patch)
                row = {**row, **patch}
            is_asm = '/gcom/nsmLmSts/out/' in link
            return JSONResponse({
                "summary":          row.get("summary", ""),
                "reason":           row.get("reason", ""),
                "flow_status":      row.get("status", ""),
                "flow_date":        row.get("propose_date", ""),
                "propose_info":     row.get("propose_info", ""),
                "committee_review": row.get("committee_review") or [],
                "is_assembly":      is_asm,
            })

    # 실시간 스크래핑 (캐시 미적중 또는 Supabase 없을 때)
    loop = asyncio.get_running_loop()
    detail = await loop.run_in_executor(None, _fetch_bill_detail, link)

    # 성공 시 Supabase write-back (summary + 입법현황 날짜/상태 포함)
    if detail.get("summary") and SUPABASE_URL and SUPABASE_KEY:
        encoded = urllib.parse.quote(link, safe='')
        patch_body: dict = {"summary": detail["summary"], "reason": detail.get("reason", "")}
        if detail.get("propose_info"):
            patch_body["propose_info"] = detail["propose_info"]
        if detail.get("committee_review"):
            patch_body["committee_review"] = detail["committee_review"]
        if detail.get("flow_date"):
            patch_body["propose_date"] = detail["flow_date"]   # 입법현황 날짜로 갱신
        if detail.get("flow_status"):
            patch_body["status"] = detail["flow_status"]       # 입법현황 상태로 갱신
        _supabase_request("PATCH", f"legislation_status?link=eq.{encoded}", patch_body)

    return JSONResponse({
        "summary":          detail.get("summary", ""),
        "reason":           detail.get("reason", ""),
        "flow_status":      detail.get("flow_status", ""),
        "flow_date":        detail.get("flow_date", ""),
        "propose_info":     detail.get("propose_info", ""),
        "committee_review": detail.get("committee_review", []),
        "is_assembly":      detail.get("is_assembly", False),
    })


@app.get("/api/article-html")
async def get_article_html(url: str = ""):
    """외부 기사 HTML 프록시 — 여백 CSS 주입 후 반환 (srcdoc용)"""
    if not url:
        return HTMLResponse("")
    try:
        resp = _APP_SESSION.get(url, timeout=12, allow_redirects=True)
        final_url = resp.url
        content = resp.content.decode(resp.apparent_encoding or 'utf-8', errors='replace')
        soup = BeautifulSoup(content, 'lxml')
        # base 태그로 상대 URL 해결
        existing_base = soup.find('base')
        if existing_base:
            existing_base['href'] = final_url
            existing_base['target'] = '_blank'
        else:
            base_tag = soup.new_tag('base', href=final_url, target='_blank')
            if soup.head:
                soup.head.insert(0, base_tag)
        # 여백 + 가독성 CSS 주입
        style_tag = soup.new_tag('style')
        style_tag.string = (
            "body,#wrap,#container,.container,.wrapper,.contents,"
            "#content,.content,.article-wrap,.news-wrap{"
            "padding-left:20px!important;padding-right:20px!important;}"
            "img{max-width:100%!important;height:auto!important;}"
        )
        if soup.head:
            soup.head.append(style_tag)
        return HTMLResponse(str(soup), media_type="text/html; charset=utf-8")
    except Exception as e:
        return HTMLResponse(
            f"<html><body style='padding:20px;font-family:sans-serif;color:#666'>"
            f"<p>원문을 불러올 수 없습니다.</p><p style='font-size:12px'>{e}</p></body></html>"
        )


@app.get("/api/article-content")
async def get_article_content(url: str = ""):
    """외부 보도자료·언론기사 원문 텍스트 추출 프록시"""
    if not url:
        return JSONResponse({"text": "", "error": "no url"})
    try:
        resp = _APP_SESSION.get(url, timeout=10, allow_redirects=True)
        soup = BeautifulSoup(
            resp.content.decode(resp.apparent_encoding or 'utf-8', errors='replace'),
            'lxml'
        )
        for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer',
                                   'aside', 'iframe', 'noscript']):
            tag.decompose()
        SELECTORS = [
            'article', '.article-body', '.article-content', '.news-content',
            '.press-view', '.press-content', '.view-content', '.cont-area',
            '#article-body', '#articleBody', '.articleBody',
            '.article_body', '.article_view', 'main',
        ]
        for sel in SELECTORS:
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 100:
                text = el.get_text(separator='\n', strip=True)
                text = re.sub(r'\n{3,}', '\n\n', text)
                return JSONResponse({"text": text[:4000]})
        paras = [p.get_text(strip=True) for p in soup.find_all('p')
                 if len(p.get_text(strip=True)) > 30]
        text = '\n\n'.join(paras[:30])
        return JSONResponse({"text": text[:4000] if text else ""})
    except Exception as e:
        return JSONResponse({"text": "", "error": str(e)})


@app.get("/api/news")
async def get_news(q: str = "", display: int = 10, propose_date: str = ""):
    """네이버 뉴스 검색 API — 입법현황 관련 언론기사 반환 (키워드 + 날짜 ±7일 필터)"""
    client_id     = os.environ.get("NAVER_CLIENT_ID", "")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not q or not client_id or not client_secret:
        return JSONResponse({"items": [], "error": "missing query or API keys"})

    fetch_count = min(display * 5, 100)
    url = (
        "https://openapi.naver.com/v1/search/news.json"
        f"?query={urllib.parse.quote(q)}&display={fetch_count}&sort=date"
    )
    try:
        resp = _APP_SESSION.get(
            url,
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            timeout=8,
        )
        raw_items = resp.json().get("items", [])
    except Exception as e:
        return JSONResponse({"items": [], "error": str(e)})

    # 키워드 필터: 2자 이상 핵심 단어가 제목 또는 본문에 포함
    _SKIP = {'법률', '관한', '이용', '보고', '위한', '관련', '대한', '따른', '등에', '이상', '이하', '으로', '에서', '하여'}
    keywords = [w for w in re.split(r'[\s·,]+', q) if len(w) >= 2 and w not in _SKIP]

    def _strip_html(s: str) -> str:
        return re.sub(r'<[^>]+>', '', s)

    def _relevant(item: dict) -> bool:
        if not keywords:
            return True
        text = _strip_html(item.get('title', '') + ' ' + item.get('description', ''))
        return any(kw in text for kw in keywords)

    # 날짜 범위 필터: propose_date ±7일 (없으면 스킵)
    center_dt = None
    if propose_date:
        m = re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})', propose_date)
        if m:
            from datetime import date as _date, timedelta
            center_dt = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    from email.utils import parsedate_to_datetime
    def _in_range(item: dict) -> bool:
        if not center_dt:
            return True
        try:
            pub = parsedate_to_datetime(item.get('pubDate', '')).date()
            return abs((pub - center_dt).days) <= 7
        except Exception:
            return True

    filtered = [it for it in raw_items if _relevant(it) and _in_range(it)][:display]
    return JSONResponse({"items": filtered})


@app.post("/api/legislation/enrich")
async def enrich_legislation():
    """전체 입법현황 상세 페이지에서 입법현황 상태.날짜 일괄 업데이트"""
    from concurrent.futures import ThreadPoolExecutor
    import threading

    async def generator():
        if not LEGISLATION_FILE.exists():
            yield "data: 로컬 파일 없음\n\n"
            return
        items = json.loads(LEGISLATION_FILE.read_text(encoding='utf-8'))
        total = len(items)
        yield f"data: 총 {total}건 상세 페이지 수집 시작\n\n"
        lock = threading.Lock()
        done = [0]

        def fetch_one(item):
            link = item.get('link', '')
            if not link:
                return
            detail = _fetch_bill_detail(link)
            if detail.get('flow_status'):
                item['status'] = detail['flow_status']
            if detail.get('flow_date'):
                item['propose_date'] = detail['flow_date']
            if detail.get('summary'):
                item['summary'] = detail['summary']
            if detail.get('reason'):
                item['reason'] = detail['reason']
            if detail.get('propose_info'):
                item['propose_info'] = detail['propose_info']
            if detail.get('committee_review'):
                item['committee_review'] = detail['committee_review']
            with lock:
                done[0] += 1

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(fetch_one, item) for item in items]
            while any(not f.done() for f in futures):
                await asyncio.sleep(1)
                yield f"data: 진행 {done[0]}/{total}\n\n"
        LEGISLATION_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
        for item in items:
            _supabase_request("POST", "legislation_status", item, upsert=True)
        yield f"data: ✓ 완료 ({done[0]}건 업데이트)\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _merge_with_existing_enriched(items: list[dict]) -> list[dict]:
    """재수집한 items에 기존 enrich 데이터(propose_date 등)를 보존해서 머지.
    목록 페이지엔 발의일이 없어 propose_date가 빈 값으로 들어오므로,
    그대로 upsert하면 기존 입법현황 마지막 단계 날짜가 사라진다."""
    existing_map: dict[str, dict] = {}
    if SUPABASE_URL and SUPABASE_KEY:
        existing_rows = _supabase_request(
            "GET",
            "legislation_status?select=link,propose_date,status,summary,reason,propose_info,committee_review&limit=2000",
        ) or []
        existing_map = {r['link']: r for r in existing_rows if r.get('link')}
    # 로컬 파일도 머지 소스에 포함
    if LEGISLATION_FILE.exists():
        try:
            for r in json.loads(LEGISLATION_FILE.read_text(encoding='utf-8')):
                lk = r.get('link')
                if lk and lk not in existing_map:
                    existing_map[lk] = r
        except Exception:
            pass

    _PRESERVE_KEYS = ('propose_date', 'summary', 'reason', 'propose_info', 'committee_review', 'status')
    for item in items:
        existing = existing_map.get(item.get('link'))
        if existing:
            for k in _PRESERVE_KEYS:
                if not item.get(k) and existing.get(k):
                    item[k] = existing[k]
        # 빈 보존 키는 dict에서 제거 — PostgREST upsert(merge-duplicates)는 키가 있으면
        # 빈 값으로 명시적 덮어쓰기 하므로, 키 자체를 빼야 기존 컬럼 값이 보존된다.
        for k in _PRESERVE_KEYS:
            if k in item and not item[k]:
                del item[k]
    return items


@app.post("/api/legislation/collect")
async def trigger_legislation_collect():
    """대상 법령 입법현황 수집 트리거"""
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, collect_legislation_status)
    items = _merge_with_existing_enriched(items)
    LEGISLATION_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in items:
        _supabase_request("POST", "legislation_status", item, upsert=True)
    return JSONResponse({"ok": True, "count": len(items)})


@app.get("/api/assembly-press")
async def get_assembly_press():
    """국회의원 보도자료 반환 (Supabase 우선, 로컬 파일 fallback)"""
    rows = _supabase_request("GET", "assembly_press?order=pub_date.desc&limit=200")
    if rows:
        return JSONResponse(rows)
    if ASSEMBLY_PRESS_FILE.exists():
        return JSONResponse(json.loads(ASSEMBLY_PRESS_FILE.read_text(encoding='utf-8')))
    return JSONResponse([])


@app.post("/api/assembly-press/collect")
async def trigger_assembly_press_collect():
    """nanet.go.kr 국회의원 보도자료 수집 트리거"""
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, _scrape_assembly_press)
    ASSEMBLY_PRESS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in items:
        _supabase_request("POST", "assembly_press", item, upsert=True)
    return JSONResponse({"ok": True, "count": len(items)})


@app.get("/api/stream/{report_type}")
async def stream_collect(report_type: str):
    """SSE: collect_보도자료.py 실시간 로그 스트림 (전체=3개 순차 실행)"""
    targets = CATEGORIES if report_type == "전체" else [report_type]

    async def event_generator():
        loop = asyncio.get_running_loop()
        collect_env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        for cat in targets:
            yield f"data: ━━ [{cat}] 수집 시작 ━━\n\n"
            proc = await loop.run_in_executor(
                None,
                lambda c=cat: subprocess.Popen(
                    [sys.executable, "-u", str(COLLECT_SCR), c],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(BASE_DIR),
                    env=collect_env,
                ),
            )
            while True:
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if not line:
                    break
                yield f"data: {line.decode('utf-8', errors='replace').rstrip()}\n\n"
            await loop.run_in_executor(None, proc.wait)
            yield f"data: ✓ [{cat}] 수집 완료\n\n"

        # 입법현황 수집
        yield "data: ━━ [입법현황] 수집 시작 ━━\n\n"
        try:
            leg_items = await loop.run_in_executor(None, collect_legislation_status)
            leg_items = _merge_with_existing_enriched(leg_items)
            LEGISLATION_FILE.write_text(
                json.dumps(leg_items, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            for item in leg_items:
                _supabase_request("POST", "legislation_status", item, upsert=True)
            yield f"data: ✓ 입법현황 {len(leg_items)}건 완료\n\n"
        except Exception as e:
            yield f"data: [WARN] 입법현황 수집 오류: {e}\n\n"

        # 국회의원 보도자료 수집
        yield "data: ━━ [국회의원 보도자료] 수집 시작 ━━\n\n"
        try:
            press_items = await loop.run_in_executor(None, _scrape_assembly_press)
            ASSEMBLY_PRESS_FILE.write_text(
                json.dumps(press_items, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            for item in press_items:
                _supabase_request("POST", "assembly_press", item, upsert=True)
            yield f"data: ✓ 국회의원 보도자료 {len(press_items)}건 완료\n\n"
        except Exception as e:
            yield f"data: [WARN] 국회의원 보도자료 수집 오류: {e}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class FormatRequest(BaseModel):
    indices: list[int]


@app.post("/api/format/{report_type}")
async def format_articles(report_type: str, req: FormatRequest):
    """선택된 기사 인덱스를 받아 ◆/•/- 형식 텍스트로 변환 (URL에서 본문 fetch 포함)"""
    articles = parse_collected(report_type)
    selected = [articles[i] for i in req.indices if 0 <= i < len(articles)]
    loop = asyncio.get_running_loop()  # [C-2 fix] deprecated get_event_loop() 대체
    content = await loop.run_in_executor(
        None,
        lambda: "\n\n".join(auto_format_article(a) for a in selected),
    )
    return JSONResponse({"content": content})


class GenerateRequest(BaseModel):
    content: str


def _supabase_request(method: str, path: str, payload: dict | None = None, upsert: bool = False) -> list | dict | None:
    """Supabase REST API 공통 요청"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    prefer = "resolution=merge-duplicates,return=representation" if upsert else "return=representation"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[WARN] Supabase {method} {path} 실패: {e}")
        return None


def _save_history(content: str, ppt_path: str, category: str) -> None:
    """PPT 생성 완료 이력을 Supabase에 저장 (로컬 history.json 병행)"""
    title_m = re.search(r'◆[^\|]+\|\s*「?(.+?)」?\s{2,}', content)
    date_m  = re.search(r"'(\d{2}\.\d+\.\d+\([가-힣]\))", content)
    title   = title_m.group(1).strip() if title_m else content.split('\n')[0][:60]
    article_date = date_m.group(1) if date_m else ''
    entry = {
        "id": int(datetime.now().timestamp()),
        "ppt_created_at": date.today().isoformat(),
        "article_date": article_date,
        "title": title,
        "summary": content[:400],
        "ppt_path": ppt_path,
        "category": category,
    }
    # Supabase 저장
    _supabase_request("POST", "history", entry)
    # 로컬 fallback 저장
    history: list = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception:
            history = []
    history.insert(0, entry)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')


def _load_history() -> list:
    """Supabase에서 이력 로드. 실패 시 로컬 history.json fallback"""
    rows = _supabase_request("GET", "history?order=id.desc&limit=200")
    if rows is not None:
        return rows
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return []


@app.get("/api/history")
async def get_history():
    """PPT 생성 이력 반환"""
    return JSONResponse(_load_history())


@app.get("/api/ppt/{item_id}")
async def download_ppt(item_id: int):
    """history id로 PPT 파일 다운로드"""
    history = _load_history()
    entry = next((h for h in history if h.get("id") == item_id), None)
    if not entry:
        return JSONResponse({"error": "항목 없음"}, status_code=404)
    ppt_path = Path(entry.get("ppt_path", ""))
    if not ppt_path.exists():
        return JSONResponse({"error": f"파일 없음: {ppt_path.name}"}, status_code=404)
    return FileResponse(
        path=str(ppt_path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=ppt_path.name,
    )


@app.post("/api/generate/{report_type}")
async def generate_ppt(report_type: str, req: GenerateRequest):
    """CONTENT 교체 후 make_report.py 실행 — tempfile로 race condition 방지 [C-3 fix]"""
    src     = MAKE_REPORT.read_text(encoding="utf-8")
    new_src = re.sub(
        r"(# ── 여기에 보고 내용을 붙여넣으세요 ─+\n)CONTENT = \"\"\".*?\"\"\"",
        lambda m: m.group(1) + f'CONTENT = """\n{req.content}\n"""',
        src,
        flags=re.DOTALL,
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(new_src)
            tmp_path = tmp.name
        result = subprocess.run(
            [sys.executable, tmp_path, report_type],
            capture_output=True, text=True, cwd=str(BASE_DIR),
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    ppt = latest_ppt(report_type)
    if result.returncode == 0 and ppt:
        _save_history(req.content, ppt, report_type)
    return JSONResponse({
        "ok":       result.returncode == 0,
        "log":      result.stdout + result.stderr,
        "ppt_path": ppt,
    })


# ── 직접 실행 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    import threading
    import uvicorn

    PORT = int(os.environ.get("PORT", 8765))
    IS_RENDER = os.environ.get("RENDER") == "true"

    report_type = sys.argv[1] if len(sys.argv) > 1 else None
    if report_type and report_type in ("데이터", "페이먼트", "AML"):
        os.environ["AUTO_COLLECT_TYPE"] = report_type
        AUTO_COLLECT_TYPE = report_type

    if IS_RENDER:
        # Render: 브라우저 열기 없이 0.0.0.0 바인딩
        print(f"Render 환경 감지 — 서버 시작: 0.0.0.0:{PORT}")
        uvicorn.run(app, host="0.0.0.0", port=PORT, reload=False)
    else:
        def _port_in_use(port: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(("localhost", port)) == 0

        already_running = _port_in_use(PORT)

        if already_running:
            print(f"서버가 이미 실행 중입니다: http://localhost:{PORT}")
            if report_type:
                print(f"→ '{report_type}' 보도자료 자동 수집을 시작합니다.")
                subprocess.run([sys.executable, "collect_보도자료.py", report_type])
        else:
            threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
            print(f"서버 시작: http://localhost:{PORT}")
            if report_type:
                print(f"→ '{report_type}' 보도자료 자동 수집을 시작합니다.")
            uvicorn.run(app, host="0.0.0.0", port=PORT, reload=False)
