"""
collect_보도자료.py

korea.kr 보도자료 + 금융정보분석원(kofiu.go.kr) 자동 수집 스크립트
수집 결과를 collected_{유형}.txt에 저장한다.

사용법:
    python collect_보도자료.py 데이터
    python collect_보도자료.py 페이먼트
    python collect_보도자료.py AML

필요 패키지:
    pip install requests beautifulsoup4
"""

import os
import sys
import re
import time
import subprocess
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from bs4 import BeautifulSoup

# ════════════════════════════════════════════════════════════
#  수집 설정 — 이 블록만 수정하세요
# ════════════════════════════════════════════════════════════

COLLECT_DAYS = 7   # 최근 N일

CONFIG = {
    '데이터': {
        'search_words': ['AI', '개인정보', '데이터'],
        'keywords': [
            '데이터', 'AI', '인공지능', '개인정보', '마이데이터',
            '클라우드', '디지털', '데이터법', '개보위', '국가데이터',
            '데이터산업', '데이터경제', '생성형',
        ],
        'agencies': [
            '과학기술정보통신부',
            '개인정보보호위원회',
            '방송통신위원회',
            '법무부',
            '법제처',
        ],
    },
    '페이먼트': {
        'search_words': ['전자금융', '가상계좌', '지급결제', '핀테크', '선불충전'],
        'keywords': [
            '전자금융', '핀테크', '간편결제', '전자지급',
            '선불전자', '선불충전', '지급결제', '전자화폐',
            '빅테크', '오픈뱅킹', '금융혁신', '전자금융업',
            '특정금융', '가상자산', '가상계좌', '입금이체',
            '결제대행', '전자지급결제대행', 'PG', '이체',
            '금융위원회', '금감원', '한국은행',
        ],
        'agencies': [
            '금융위원회',
            '금융감독원',
            '한국은행',
            '기획재정부',
            '법무부',
            '법제처',
        ],
    },
    'AML': {
        'search_words': ['자금세탁', 'FIU', '특정금융'],
        'keywords': [
            '자금세탁', '자금세탁방지', 'AML', '특정금융', 'FATF', 'FIU',
            '의심거래', 'STR', '제재', '과태료', '가상자산범죄', '불법자금',
            '금융정보분석', '테러자금', '자금추적',
        ],
        'agencies': [
            '금융위원회',
            '금융감독원',
            '법무부',
            '법제처',
            '검찰청',
        ],
    },
}

# ════════════════════════════════════════════════════════════

KOREA_KR_URL = "https://www.korea.kr/briefing/pressReleaseList.do"
KOFIU_PRESS_URL    = "https://www.kofiu.go.kr/kor/notification/pressRelease.do"
KOFIU_SANCTION_URL = "https://www.kofiu.go.kr/kor/notification/sanctions.do"

# ── Naver 뉴스 API ─────────────────────────────────────────────────────────────
NAVER_CLIENT_ID     = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NAVER_NEWS_URL      = "https://openapi.naver.com/v1/search/news.json"

# ── Supabase ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# 유형별 Naver 검색 키워드 (정부 공식 보도자료 보완용)
NAVER_QUERIES = {
    '데이터': ['AI기본법', '개인정보보호', '마이데이터', '데이터규제', '인공지능법'],
    '페이먼트': ['전자금융 규제', '핀테크 정책', '가상자산 규제', '지급결제 법령'],
    'AML': ['자금세탁방지', '특정금융정보법', 'FIU', '가상자산 범죄', '불법자금'],
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DIR = os.path.dirname(os.path.abspath(__file__))

# 언론사 도메인 → 한국어 이름 매핑
OUTLET_MAP = {
    'chosun.com': '조선일보', 'joongang.co.kr': '중앙일보', 'joins.com': '중앙일보',
    'donga.com': '동아일보', 'hankyung.com': '한국경제', 'mk.co.kr': '매일경제',
    'sedaily.com': '서울경제', 'etnews.com': '전자신문', 'zdnet.co.kr': 'ZDNet',
    'dt.co.kr': '디지털타임스', 'newsis.com': '뉴시스', 'yonhapnews.co.kr': '연합뉴스',
    'yna.co.kr': '연합뉴스', 'newspim.com': '뉴스핌', 'edaily.co.kr': '이데일리',
    'news1.kr': '뉴스1', 'hani.co.kr': '한겨레', 'khan.co.kr': '경향신문',
    'fnnews.com': '파이낸셜뉴스', 'inews24.com': '아이뉴스24', 'bloter.net': '블로터',
    'ddaily.co.kr': '디지털데일리', 'thebell.co.kr': '더벨', 'mt.co.kr': '머니투데이',
    'moneys.mt.co.kr': '머니S', 'bizwatch.co.kr': '비즈워치', 'wowtv.co.kr': '한국경제TV',
    'jtbc.co.kr': 'JTBC', 'sbs.co.kr': 'SBS', 'kbs.co.kr': 'KBS',
    'mbc.co.kr': 'MBC', 'ytn.co.kr': 'YTN', 'imaeil.com': '매일신문',
    'busan.com': '부산일보', 'heraldcorp.com': '헤럴드경제', 'news.naver.com': '네이버뉴스',
    'naver.com': '네이버뉴스',
}


def extract_outlet(url: str) -> str:
    """URL 도메인에서 언론사명 추출"""
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        for key, name in OUTLET_MAP.items():
            if key in domain:
                return name
        parts = domain.split('.')
        return parts[0].upper() if parts else '언론기사'
    except Exception:
        return '언론기사'


# ── 날짜 유틸 ──────────────────────────────────────────────────────────────────

def get_date_range(days: int) -> tuple[str, str]:
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')


def parse_date(s: str):
    """'2026.04.24' / '2026-04-24' 등 → date 객체, 실패 시 None"""
    m = re.search(r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


# ── curl 유틸 ──────────────────────────────────────────────────────────────────

def _curl(url: str, extra_args: list[str] | None = None) -> str | None:
    cmd = ['curl', '-sk', '--tlsv1.2', '-A', UA,
           '--max-time', '30', '--retry', '3', '--retry-delay', '2', url]
    if extra_args:
        cmd[1:1] = extra_args
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        return None
    return result.stdout.decode('utf-8', errors='replace')


# ── korea.kr 스크래핑 ──────────────────────────────────────────────────────────

def get_total_pages(html: str) -> int:
    matches = re.findall(r'pageLink\((\d+)\)', html)
    return int(matches[-1]) if matches else 1


def scrape_korea_page(page_index: int, start_date: str, end_date: str,
                      search_word: str = '') -> tuple[list[dict], str]:
    params = {
        'pageIndex': page_index,
        'startDate': start_date,
        'endDate':   end_date,
        'period':    'direct',
        'srchWord':  search_word,
        'repCodeType': '',
        'repCode':   '',
    }
    url  = KOREA_KR_URL + '?' + urllib.parse.urlencode(params)
    html = _curl(url)
    if html is None:
        raise RuntimeError(f"curl 실패 (검색어: {search_word}, page {page_index})")

    soup = BeautifulSoup(html, 'lxml')
    articles: list[dict] = []
    list_section = soup.find('div', class_='list_type')
    if not list_section:
        return articles, html

    for li in list_section.find_all('li'):
        a_tag     = li.find('a', href=True)
        title_tag = li.find('strong')
        lead_tag  = li.find('span', class_='lead')
        source    = li.find('span', class_='source')
        if not a_tag:
            continue
        title = title_tag.get_text(strip=True) if title_tag else ''
        lead  = lead_tag.get_text(strip=True)  if lead_tag  else ''
        date_str = agency = ''
        if source:
            spans = source.find_all('span')
            if len(spans) >= 2:
                date_str = spans[0].get_text(strip=True)
                agency   = spans[1].get_text(strip=True)
        href    = a_tag.get('href', '')
        m       = re.search(r'newsId=(\d+)', href)
        news_id = m.group(1) if m else ''
        if href.startswith('http'):
            url = href
        elif news_id:
            url = f"https://www.korea.kr/briefing/pressReleaseView.do?newsId={news_id}"
        else:
            url = ''
        if title and agency:
            articles.append({
                'title':       title,
                'lead':        lead,
                'date_str':    date_str,
                'agency':      agency,
                'news_id':     news_id,
                'url':         url,
                'source_type': '보도자료',
            })
    return articles, html


def scrape_korea_kr(report_type: str, max_pages: int = 5) -> list[dict]:
    start_date, end_date = get_date_range(COLLECT_DAYS)
    cfg = CONFIG[report_type]
    search_words = cfg.get('search_words', [cfg.get('search_word', '')])

    all_articles: list[dict] = []
    print(f"검색 기간: {start_date} ~ {end_date} (최근 {COLLECT_DAYS}일)")

    for sw in search_words:
        print(f"  korea.kr 검색어: '{sw}'")
        time.sleep(3)
        try:
            arts, html = scrape_korea_page(1, start_date, end_date, sw)
        except RuntimeError as e:
            print(f"    1페이지 실패: {e}")
            continue
        total = min(get_total_pages(html), max_pages)
        print(f"    {total}페이지 수집 예정")
        all_articles.extend(arts)
        for page in range(2, total + 1):
            time.sleep(3)
            try:
                p_arts, _ = scrape_korea_page(page, start_date, end_date, sw)
                all_articles.extend(p_arts)
            except RuntimeError as e:
                print(f"    페이지 {page} 실패 (건너뜀): {e}")

    print(f"korea.kr 1차 수집: {len(all_articles)}건 (중복 포함)")
    return all_articles


# ── Naver 뉴스 API 수집 ────────────────────────────────────────────────────────

def scrape_naver_news(report_type: str) -> list[dict]:
    """Naver 뉴스 API로 키워드 검색 → 최근 COLLECT_DAYS일 기사 수집"""
    import json
    queries = NAVER_QUERIES.get(report_type, [])
    if not queries:
        return []

    start_d = date.today() - timedelta(days=COLLECT_DAYS)
    all_items: list[dict] = []
    seen_links: set[str] = set()

    for query in queries:
        print(f"  [Naver] 검색어: '{query}'")
        encoded = urllib.parse.quote(query)
        url = f"{NAVER_NEWS_URL}?query={encoded}&display=20&sort=date"
        cmd = [
            'curl', '-sk', '--tlsv1.2', '--max-time', '15',
            '-H', f'X-Naver-Client-Id: {NAVER_CLIENT_ID}',
            '-H', f'X-Naver-Client-Secret: {NAVER_CLIENT_SECRET}',
            url,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode != 0:
                print(f"    API 호출 실패")
                continue
            data = json.loads(r.stdout.decode('utf-8', errors='replace'))
        except Exception as e:
            print(f"    파싱 오류: {e}")
            continue

        items = data.get('items', [])
        for item in items:
            # pubDate 예: "Wed, 30 Apr 2026 10:00:00 +0900"
            pub_raw = item.get('pubDate', '')
            parsed_d = None
            m = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', pub_raw)
            if m:
                month_map = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                             'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
                try:
                    parsed_d = date(int(m.group(3)), month_map.get(m.group(2), 0), int(m.group(1)))
                except Exception:
                    pass

            if parsed_d and parsed_d < start_d:
                continue

            link = item.get('originallink') or item.get('link', '')
            if link in seen_links:
                continue
            seen_links.add(link)

            title = re.sub(r'<[^>]+>', '', item.get('title', '')).strip()
            desc  = re.sub(r'<[^>]+>', '', item.get('description', '')).strip()
            date_str = parsed_d.strftime('%Y-%m-%d') if parsed_d else pub_raw[:10]

            all_items.append({
                'title':       title,
                'lead':        desc,
                'date_str':    date_str,
                'agency':      extract_outlet(link),
                'news_id':     f'naver_{abs(hash(link))}',
                'url':         link,
                'source_type': '언론기사',
            })
        time.sleep(1)

    print(f"  [Naver] 총 {len(all_items)}건 수집")
    return all_items


# ── kofiu.go.kr 스크래핑 (AML 전용) ──────────────────────────────────────────

def scrape_kofiu_press() -> list[dict]:
    """금융정보분석원 보도자료 수집"""
    print("  [kofiu] 보도자료 수집 중...")
    html = _curl(KOFIU_PRESS_URL)
    if not html:
        print("  [kofiu] 보도자료 접속 실패")
        return []

    soup = BeautifulSoup(html, 'lxml')
    articles = []
    start_d = date.today() - timedelta(days=COLLECT_DAYS)
    end_d   = date.today()

    rows = (
        soup.select('table.boardList tbody tr') or
        soup.select('table tbody tr') or
        soup.select('.board-list li') or
        soup.select('ul.list li')
    )

    for row in rows:
        title_el = (
            row.select_one('td.title a') or
            row.select_one('td a[href*="view"]') or
            row.select_one('td a[href*="seq"]') or
            row.select_one('.title a') or
            row.select_one('a')
        )
        tds      = row.find_all('td')
        date_el  = tds[-1] if tds else None

        if not title_el:
            continue
        title_text = title_el.get_text(strip=True)
        date_text  = date_el.get_text(strip=True) if date_el else ''
        parsed_d   = parse_date(date_text)

        if parsed_d and not (start_d <= parsed_d <= end_d):
            continue
        if not title_text or len(title_text) < 4:
            continue

        href = title_el.get('href', '') if hasattr(title_el, 'get') else ''
        if href.startswith('http'):
            url = href
        elif href:
            url = f"https://www.kofiu.go.kr{href}"
        else:
            url = KOFIU_PRESS_URL
        articles.append({
            'title':       title_text,
            'lead':        title_text,
            'date_str':    parsed_d.strftime('%Y-%m-%d') if parsed_d else date_text,
            'agency':      '금융정보분석원',
            'news_id':     f'kofiu_press_{abs(hash(title_text))}',
            'url':         url,
            'source_type': '보도자료',
        })

    print(f"  [kofiu] 보도자료 {len(articles)}건")
    return articles


def scrape_kofiu_sanctions() -> list[dict]:
    """금융정보분석원 제재 공개안 수집 (자금세탁방지 법령 위반)"""
    print("  [kofiu] 제재 공개안 수집 중...")
    html = _curl(KOFIU_SANCTION_URL)
    if not html:
        print("  [kofiu] 제재 페이지 접속 실패")
        return []

    soup = BeautifulSoup(html, 'lxml')
    articles = []
    start_d = date.today() - timedelta(days=COLLECT_DAYS)
    end_d   = date.today()

    rows = (
        soup.select('table.boardList tbody tr') or
        soup.select('table tbody tr') or
        soup.select('.board-list li')
    )

    for row in rows:
        tds = row.find_all('td')
        if len(tds) < 2:
            continue
        title_el = row.select_one('td a') or tds[1]
        date_el  = tds[-1]

        title_text = title_el.get_text(strip=True) if title_el else ''
        date_text  = date_el.get_text(strip=True)  if date_el  else ''
        parsed_d   = parse_date(date_text)

        if parsed_d and not (start_d <= parsed_d <= end_d):
            continue
        if not title_text or len(title_text) < 4:
            continue

        href = (row.select_one('td a') or {}).get('href', '') if row.select_one('td a') else ''
        if href.startswith('http'):
            url = href
        elif href:
            url = f"https://www.kofiu.go.kr{href}"
        else:
            url = KOFIU_SANCTION_URL
        articles.append({
            'title':       f'[제재] {title_text}',
            'lead':        f'금융정보분석원 제재 공개: {title_text} — 자금세탁방지법 위반',
            'date_str':    parsed_d.strftime('%Y-%m-%d') if parsed_d else date_text,
            'agency':      '금융정보분석원',
            'news_id':     f'kofiu_sanc_{abs(hash(title_text))}',
            'url':         url,
            'source_type': '보도자료',
        })

    print(f"  [kofiu] 제재 공개안 {len(articles)}건")
    return articles


# ── 필터링 ─────────────────────────────────────────────────────────────────────

def filter_by_keywords(articles: list[dict], report_type: str) -> list[dict]:
    keywords = CONFIG[report_type]['keywords']
    filtered = [
        a for a in articles
        if any(kw in (a['title'] + ' ' + a['lead']) for kw in keywords)
    ]
    print(f"2차 키워드 필터 후: {len(filtered)}건 (원본 {len(articles)}건)")
    return filtered


def filter_by_agency(articles: list[dict], report_type: str) -> list[dict]:
    agencies = CONFIG[report_type]['agencies']
    if not agencies:
        print("3차 기관 필터: 전체 기관 수집")
        return articles
    # kofiu·언론기사는 기관 필터 적용 안 함 (별도 수집처)
    filtered = [
        a for a in articles
        if a['agency'] in agencies
        or a['agency'] == '금융정보분석원'
        or a.get('source_type') == '언론기사'
    ]
    print(f"3차 기관 필터 후: {len(filtered)}건")
    return filtered


def deduplicate(articles: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for a in articles:
        if a['news_id'] not in seen:
            seen.add(a['news_id'])
            result.append(a)
    return result


def _title_keywords(title: str) -> set[str]:
    """제목에서 3자 이상 의미 단어 추출 (조사·짧은 단어 제외)"""
    clean = re.sub(r'&[a-z]+;', ' ', title)  # HTML 엔티티 제거
    return set(w for w in re.findall(r'[가-힣]{3,}|[A-Za-z]{3,}', clean))


def _first_keyword(title: str) -> str:
    """제목의 첫 번째 3자 이상 키워드 (주어 역할)"""
    clean = re.sub(r'&[a-z]+;|\[.*?\]|\(.*?\)', ' ', title)
    m = re.search(r'[가-힣]{3,}|[A-Za-z]{3,}', clean)
    return m.group(0) if m else ''


def deduplicate_by_title(articles: list[dict], threshold: float = 0.35) -> list[dict]:
    """제목 키워드 유사도 기반 중복 제거 — Naver 뉴스 동일 사건 다언론사 필터용.
    같은 주어(첫 키워드)가 있으면 threshold를 0.2로 낮춰 더 공격적으로 제거."""
    result: list[dict] = []
    for a in articles:
        kw_a   = _title_keywords(a['title'])
        subj_a = _first_keyword(a['title'])
        is_dup = False
        for kept in result:
            kw_k   = _title_keywords(kept['title'])
            union  = kw_a | kw_k
            if not union:
                continue
            sim    = len(kw_a & kw_k) / len(union)
            subj_k = _first_keyword(kept['title'])
            # 같은 주체(기업/기관)면 낮은 threshold 적용
            cutoff = 0.2 if (subj_a and subj_a == subj_k) else threshold
            if sim >= cutoff:
                is_dup = True
                break
        if not is_dup:
            result.append(a)
    return result


# ── 저장 ──────────────────────────────────────────────────────────────────────

def save_to_file(articles: list[dict], report_type: str) -> None:
    start_date, end_date = get_date_range(COLLECT_DAYS)
    output_file = os.path.join(_DIR, f'collected_{report_type}.txt')

    lines = [
        f"[수집 정보]",
        f"유형: {report_type}",
        f"수집일: {date.today().strftime('%Y-%m-%d')}",
        f"기간: {start_date} ~ {end_date}",
        f"건수: {len(articles)}",
        "",
    ]
    for i, a in enumerate(articles, 1):
        lines += [
            f"{'=' * 5} 보도자료 {i} {'=' * 5}",
            f"기관: {a['agency']}",
            f"날짜: {a['date_str']}",
            f"제목: {a['title']}",
            f"내용: {a['lead']}",
            f"링크: {a.get('url', '')}",
            f"구분: {a.get('source_type', '보도자료')}",
            "",
        ]

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"저장 완료: {output_file}")


def upload_to_supabase(articles: list[dict], report_type: str) -> None:
    """수집된 기사를 Supabase에 upsert"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    import json, urllib.request
    # save_to_file의 agency/date_str 키를 parse_collected 형식으로 변환
    rows = []
    for a in articles:
        rows.append({
            "기관": a.get("agency",       a.get("기관", "")),
            "날짜": a.get("date_str",     a.get("날짜", "")),
            "제목": a.get("title",        a.get("제목", "")),
            "내용": a.get("lead",         a.get("내용", "")),
            "링크": a.get("url",          a.get("링크", "")),
            "구분": a.get("source_type",  a.get("구분", "보도자료")),
        })
    payload = json.dumps({
        "type": report_type,
        "data": rows,
        "updated_at": date.today().isoformat(),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/articles",
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Supabase 업로드 완료 ({report_type}): {len(rows)}건 → status {resp.status}")
    except Exception as e:
        print(f"Supabase 업로드 실패: {e}")


# ── 메인 실행 ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in CONFIG:
        print(f"사용법: python collect_보도자료.py {'|'.join(CONFIG.keys())}")
        sys.exit(1)

    report_type = sys.argv[1]

    # korea.kr 수집
    articles = scrape_korea_kr(report_type)

    # AML 전용: kofiu.go.kr 추가 수집
    if report_type == 'AML':
        print("\n[kofiu.go.kr] 추가 수집 중...")
        time.sleep(2)
        articles.extend(scrape_kofiu_press())
        time.sleep(2)
        articles.extend(scrape_kofiu_sanctions())

    # Naver 뉴스 API 추가 수집 (제목 유사도 기반 중복 제거 포함)
    print("\n[Naver 뉴스 API] 수집 중...")
    naver_raw = scrape_naver_news(report_type)
    naver_dedup = deduplicate_by_title(naver_raw)
    print(f"  [Naver] 제목 중복 제거 후: {len(naver_dedup)}건 (원본 {len(naver_raw)}건)")
    articles.extend(naver_dedup)

    if not articles:
        print("수집된 보도자료가 없습니다.")
        sys.exit(0)

    filtered = filter_by_keywords(articles, report_type)
    filtered = filter_by_agency(filtered, report_type)

    if not filtered:
        print("필터 결과 관련 보도자료가 없습니다.")
        sys.exit(0)

    unique = deduplicate(filtered)
    print(f"중복 제거 후: {len(unique)}건")

    save_to_file(unique, report_type)
    upload_to_supabase(unique, report_type)
