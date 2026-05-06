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
        "공중 등 협박목적을 위한 자금조달행위의 금지에 관한 법률",
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


_PRIORITY_HIGH = [
    "개정", "시행", "제정", "법률", "법안", "입법", "고시", "훈령",
    "제재", "과태료", "처벌", "행정처분", "금지", "의무화", "규제",
    "위반", "제도화", "시행령", "시행규칙",
]
_PRIORITY_LOW = [
    "소통", "간담회", "행사", "청취", "격려", "참석", "방문",
    "인사", "취임", "기념", "홍보", "인터뷰", "보도참고",
]


def get_priority(article: dict) -> str:
    """보도자료 중요도 자동 산정: 상/중/하"""
    text = article.get("제목", "") + " " + article.get("내용", "")
    high = sum(1 for kw in _PRIORITY_HIGH if kw in text)
    low  = sum(1 for kw in _PRIORITY_LOW  if kw in text)
    if high >= 1:
        return "상"
    if low >= 2 or (low >= 1 and high == 0):
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
        # lsNmKo 검색은 법령명 포함 여부로 필터 (검색어가 제목에 포함되어야 함)
        kw = law_name.replace(' ', '')[:6]
        if kw not in bill_title.replace(' ', '') and kw not in cells[4].get_text(strip=True).replace(' ', ''):
            continue
        propose_date = ''
        for ci in range(6, min(len(cells), 10)):
            txt = cells[ci].get_text(strip=True)
            if txt and (txt[0].isdigit() and len(txt) >= 8):
                propose_date = txt
                break
        items.append({
            "id": hashlib.md5(f"gov-{law_name}-{bill_title}".encode()).hexdigest()[:12],
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
    """국회 입법현황 - GET 후 법령명 키워드로 title 필터링"""
    _ensure_lawmaking_session()
    url = f"{_LAWMAKING_BASE}/gcom/nsmLmSts/out"
    try:
        resp = _APP_SESSION.get(url,
            params={"issLawitmYn": "Y", "pageIndex": "1"},
            timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] nsmLmSts fetch error ({law_name}): {e}")
        return []
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'lxml')
    kw = law_name.replace(' ', '')[:6]
    items = []
    for row in soup.select('table tbody tr'):
        cells = row.find_all('td')
        if len(cells) < 4:
            continue
        title = cells[0].get_text(strip=True)
        if not title or kw not in title.replace(' ', ''):
            continue
        link_tag = cells[0].find('a')
        href = (link_tag.get('href') or '') if link_tag else ''
        bill_no = cells[5].get_text(strip=True) if len(cells) > 5 else ''
        propose_date = cells[4].get_text(strip=True) if len(cells) > 4 else ''
        items.append({
            "id": hashlib.md5(f"asm-{law_name}-{bill_no}-{title[:20]}".encode()).hexdigest()[:12],
            "source": "assembly",
            "category": category,
            "target_law": law_name,
            "bill_title": title,
            "bill_type": "",
            "amendment_type": "",
            "ministry": cells[2].get_text(strip=True) if len(cells) > 2 else '',
            "status": cells[3].get_text(strip=True) if len(cells) > 3 else '',
            "proposer": cells[1].get_text(' ', strip=True) if len(cells) > 1 else '',
            "bill_no": bill_no,
            "propose_date": propose_date,
            "link": (_LAWMAKING_BASE + href) if href.startswith('/') else href,
            "scraped_at": date.today().isoformat(),
        })
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
    """6개 대상 법령의 정부 입법현황 수집 (opinion.lawmaking.go.kr govLm)"""
    results: list[dict] = []
    for cat, laws in LEGISLATION_TARGETS.items():
        for law in laws:
            gov_items = _scrape_govlm(law, cat)
            print(f"  govLm [{law}]: {len(gov_items)}건")
            results.extend(gov_items)
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
    rows = _supabase_request("GET", "legislation_status?order=scraped_at.desc&limit=500")
    if rows:
        return JSONResponse(rows)
    if LEGISLATION_FILE.exists():
        return JSONResponse(json.loads(LEGISLATION_FILE.read_text(encoding='utf-8')))
    return JSONResponse([])


@app.post("/api/legislation/collect")
async def trigger_legislation_collect():
    """대상 법령 입법현황 수집 트리거"""
    loop = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, collect_legislation_status)
    LEGISLATION_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    for item in items:
        _supabase_request("POST", "legislation_status", item)
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
        _supabase_request("POST", "assembly_press", item)
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
            LEGISLATION_FILE.write_text(
                json.dumps(leg_items, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            for item in leg_items:
                _supabase_request("POST", "legislation_status", item)
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
                _supabase_request("POST", "assembly_press", item)
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


def _supabase_request(method: str, path: str, payload: dict | None = None) -> list | dict | None:
    """Supabase REST API 공통 요청"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
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
