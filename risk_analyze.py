"""
risk_analyze.py — AML 기사·보도자료를 쿠콘 관점에서 리스크 평가.

규제 리스크(신규 법령·가이드라인 → 솔루션 영향)와 평판 리스크(사고·제재 이슈)
두 축으로 Gemini LLM 평가. Gemini 실패 시 키워드 기반 룰 폴백.

사용:
    from risk_analyze import analyze_article
    result = analyze_article(article, policy_db)
"""
import json
import os
import re
import ssl as _ssl
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = ("gemini-2.0-flash", "gemini-flash-latest")


class _LaxSSLAdapter(HTTPAdapter):
    """회사 프록시 환경 SSL 우회"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


_SESSION = requests.Session()
_SESSION.verify = False
_SESSION.mount("https://", _LaxSSLAdapter())
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
})


# ── 원문 본문 fetch (lead가 짧을 때 보강) ────────────────────────────────────

# fetch 대상에서 제외할 도메인 (JS 렌더링·로그인 벽 등)
_FETCH_BLOCKLIST = {"naver.com", "n.news.naver.com"}

# 본문 추출 셀렉터 (한국 언론사 공통 패턴)
_BODY_SELECTORS = [
    "#article-body", "#articleBody", "#newsct_article",
    "div.article_body", "div.view_content", "div.news_body",
    "div.cont_view", "div.article-body", "div.viewArticle",
    "div#contents", "article",
]


# 본문에서 제거할 페이지 UI 텍스트 패턴 (정규식)
_UI_NOISE_PATTERNS = [
    re.compile(r"입력\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}[\s\d:]*"),
    re.compile(r"수정\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}[\s\d:]*"),
    re.compile(r"등록\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}[\s\d:]*"),
    re.compile(r"발행\s*\d{4}[-./]\d{1,2}[-./]\d{1,2}[\s\d:]*"),
    re.compile(r"기사[\s가-힣]*?(승인|입력|등록|수정)\s*\d{4}[\s\d.\-:]*"),
    re.compile(r"복사\s*완료!?"),
    re.compile(r"공유하기|공유\s*\||SNS\s*공유|URL\s*복사|URL이?\s*복사"),
    re.compile(r"무단\s*전재[^.]*?금지[.]?"),
    re.compile(r"저작권[^.]*?무단[^.]*?금지[.]?"),
    re.compile(r"ⓒ\s*[\w가-힣]+[^.]*?(All rights reserved|무단)[^.]*"),
    re.compile(r"©\s*[\w가-힣]+[^.]*?(All rights reserved|무단)[^.]*"),
    re.compile(r"[\w가-힣]+\s*기자\s*=\s*"),
    re.compile(r"\[[\w가-힣]+\s*=\s*[\w가-힣]+\s*기자\]"),
    re.compile(r"\([\w가-힣]+\s*=\s*[\w가-힣\s]+\)"),    # (서울=뉴스1) 같은 패턴
    re.compile(r"[\w._%+-]+@[\w.-]+\.\w{2,}"),         # 이메일
    re.compile(r"\b(?:Tel|FAX|전화|팩스)[.:\s]\s*\d{2,4}[-)\s]?\d{3,4}[-\s]?\d{4}", re.IGNORECASE),
    re.compile(r"카카오톡\s*공유|페이스북\s*공유|트위터\s*공유|네이버\s*공유"),
    re.compile(r"댓글\s*\d+|좋아요\s*\d+|조회수\s*\d+"),
    re.compile(r"폰트\s*[크작]게|글자\s*[크작]게"),
    re.compile(r"프린트하기|인쇄하기|스크랩하기"),
]


def _clean_body_noise(text: str) -> str:
    """본문에서 페이지 UI·메타데이터 텍스트 제거."""
    if not text:
        return ""
    cleaned = text
    for pat in _UI_NOISE_PATTERNS:
        cleaned = pat.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _fetch_original_body(url: str, min_len: int = 400) -> str:
    """기사 URL에서 본문 텍스트 추출 + UI 잡문 정리. 실패 시 빈 문자열."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
        if any(blk in host for blk in _FETCH_BLOCKLIST):
            return ""
        resp = _SESSION.get(url, timeout=10)
        resp.raise_for_status()
        html = resp.text
        # BeautifulSoup 지연 import (의존성 격리)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        # 스크립트·스타일·SNS 박스 등 제거
        for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                                   "aside", "iframe", "form", "button"]):
            tag.decompose()
        # 클래스명에 share/sns/copy/footer 포함된 요소 제거
        for el in soup.select('[class*="share"], [class*="sns"], [class*="copy"], '
                              '[class*="footer"], [class*="related"], [class*="recommend"]'):
            el.decompose()
        # 셀렉터 우선
        for sel in _BODY_SELECTORS:
            el = soup.select_one(sel)
            if el:
                text = re.sub(r"\s{2,}", " ", el.get_text(separator=" ", strip=True))
                text = _clean_body_noise(text)
                if len(text) >= min_len:
                    return text[:4000]
        # 폴백: 가장 긴 <p> 묶음
        ps = soup.find_all("p")
        if ps:
            joined = " ".join(p.get_text(strip=True) for p in ps)
            joined = re.sub(r"\s{2,}", " ", joined)
            joined = _clean_body_noise(joined)
            if len(joined) >= min_len:
                return joined[:4000]
        return ""
    except Exception as e:
        print(f"[risk_analyze] 원문 fetch 실패 ({url[:60]}): {e}")
        return ""


# ── 본문 → 자동 핵심 포인트 추출 (LLM 누락 시 fallback) ───────────────────

_SENTENCE_SPLIT = re.compile(r"(?<=[다요됨음.\!\?])\s+")

# 인터뷰 인용 패턴 — fallback 추출 시 제외
_QUOTE_INTERVIEW = re.compile(
    r'(라며|라고\s*(했|덧붙였|밝혔|전했|말했|언급|강조)|'
    r'\(=\s*\w+\)|\(.*?\s*기자\)|"?라고 말했다|"?라고 했다)'
)
# 따옴표 큰 비중 — 인용 발언 문장
_HAS_QUOTE = re.compile(r'["“”][^"“”]{8,}["“”]')


def _auto_extract_points(text: str, n: int = 3,
                         min_len: int = 20, max_len: int = 90) -> list[str]:
    """본문에서 의미 있는 첫 N개 문장 추출 — 인터뷰·잡문 컷."""
    if not text:
        return []
    # HTML 엔티티·괄호 첨부 제거
    clean = re.sub(r"&[a-z]+;|\[.*?\]", " ", text)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    sentences = _SENTENCE_SPLIT.split(clean)
    out = []
    seen_prefixes = set()
    for s in sentences:
        s = s.strip()
        if not (min_len <= len(s) <= max_len):
            continue
        # 인터뷰 인용 컷
        if _QUOTE_INTERVIEW.search(s):
            continue
        # 큰 비중의 따옴표 인용 컷 (예: '"어차피 우리는…" "테더가 부족하다"는 등')
        if _HAS_QUOTE.search(s):
            continue
        # 접속사 헤더 제거
        s = re.sub(r"^(아울러|또한|이어|또|한편|그러나|하지만|특히)\s+", "", s)
        # 중복 (앞 15자 동일) 컷
        prefix = s[:15]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        out.append(s)
        if len(out) >= n:
            break
    return out


def _derive_summary_from_title(title: str) -> str:
    """제목에서 한 줄 요약을 생성 — 머리표지·따옴표·말줄임표 제거 + 50자 이내."""
    if not title:
        return ""
    s = title.strip()
    s = re.sub(r"^\s*\[[^\]]{1,15}\]\s*", "", s)        # [단독] 류 제거
    s = re.sub(r'["“”\'‘’]', "", s)                      # 따옴표 제거
    s = re.sub(r"\.{2,}|…+", "", s)                      # 말줄임표
    s = re.split(r"\s*\[[^\]]*$", s)[0]                   # 미완결 [...
    return re.sub(r"\s{2,}", " ", s).strip()[:80]


# ── 룰 기반 폴백 자산 ────────────────────────────────────────────────────────────

# 규제 액션 키워드 (법령 변경 신호)
_REGULATORY_KW = [
    "개정안", "시행령", "시행규칙", "고시", "가이드라인",
    "입법예고", "국무회의", "본회의 통과", "공포", "의결",
    "신설", "강화", "의무화", "도입",
]

# 평판·사고 키워드 (제재·사고 신호)
_REPUTATION_KW = [
    "제재", "과태료", "처분", "위반", "적발", "수사", "기소",
    "검거", "압수", "범죄수익", "자금세탁", "보이스피싱", "환전소",
    "사고", "유출", "해킹", "탈취",
]

# 사업영역 매핑 (기사 → 쿠콘 사업영역)
_AREA_KW = {
    "AML/KYC":  ["자금세탁", "특정금융", "AML", "FIU", "FATF", "STR",
                 "의심거래", "고객확인", "KYC", "테러자금", "공중협박",
                 "트래블룰", "가상자산 사업자", "VASP"],
    "PG/결제":  ["전자금융", "PG", "지급결제", "결제대행", "간편결제",
                 "선불충전", "정산자금", "오픈뱅킹"],
    "마이데이터": ["마이데이터", "전송요구권", "개인정보", "신용정보",
                  "본인신용정보"],
    "가상자산":  ["가상자산", "코인", "스테이블코인", "테더", "USDT",
                 "디지털자산", "암호화폐"],
}


def _classify_by_rule(article: dict) -> dict:
    """LLM 실패 또는 미설정 시 룰 기반 등급/타입/영역 산정."""
    text = (article.get("제목", "") + " " + article.get("내용", ""))

    reg_score = sum(1 for kw in _REGULATORY_KW if kw in text)
    rep_score = sum(1 for kw in _REPUTATION_KW if kw in text)

    if reg_score >= 2:
        risk_type, type_score = "규제", reg_score
    elif rep_score >= 2:
        risk_type, type_score = "평판", rep_score
    elif reg_score >= 1:
        risk_type, type_score = "규제", reg_score
    elif rep_score >= 1:
        risk_type, type_score = "평판", rep_score
    else:
        risk_type, type_score = "무관", 0

    # 영역 매칭
    impacted = [area for area, kws in _AREA_KW.items()
                if any(kw in text for kw in kws)]

    # 등급
    if type_score >= 3 and impacted:
        grade = "상"
    elif type_score >= 1 and impacted:
        grade = "중"
    elif impacted:
        grade = "하"
    else:
        grade = "무관"

    return {
        "policy_name":    "",
        "summary":        "",
        "key_changes":    [],
        "future_plan":    "",
        "risk_grade":     grade,
        "risk_type":      risk_type,
        "impacted_areas": impacted,
        "coocon_action":  "",
        "key_points":     [],
        "_source":        "rule",
    }


# ── LLM 평가 ──────────────────────────────────────────────────────────────────


_POLICY_AREA_CONTEXT = """
쿠콘 사업영역 참고:
- AML/KYC: 자금세탁방지·고객확인·트래블룰 솔루션
- PG/결제: 전자금융·간편결제·오픈뱅킹·PG 정산
- 마이데이터: 본인신용정보 전송요구·통합 데이터 플랫폼
- 가상자산: VASP 신고·KYC·트래블룰 연동
""".strip()


def _build_prompt(article: dict, policy_db: list[dict]) -> str:
    title   = article.get("제목", "")[:200]
    body    = (article.get("내용", "") or "")[:1500]
    agency  = article.get("기관", "")
    date_s  = article.get("날짜", "")

    # 정책DB의 핵심 항목만 컨텍스트로 (이름·사업영역·영향도)
    policy_lines = []
    for p in (policy_db or [])[:20]:
        nm = p.get("정책명") or ""
        ar = p.get("사업영역") or ""
        im = p.get("영향도") or ""
        if nm:
            policy_lines.append(f"- {nm} [{ar}, 영향도:{im}]")
    policy_ctx = "\n".join(policy_lines) if policy_lines else "(생략)"

    return f"""당신은 쿠콘(금융데이터·전자금융·AML 솔루션 SaaS 기업)의 리스크 분석가입니다.
주간보고 PPT 작성 규칙(CLAUDE.md)에 따라 보도자료/언론기사를 요약하고, 동시에 쿠콘 리스크를 평가합니다.

{_POLICY_AREA_CONTEXT}

현재 쿠콘이 트래킹 중인 주요 정책(참고):
{policy_ctx}

[입력 기사]
  기관: {agency}
  날짜: {date_s}
  제목: {title}
  본문:
{body[:3000]}

────────────────────────────────────────
**1단계: PPT 작성 형식에 따른 요약** (CLAUDE.md 규칙 준수)

[policy_name] 정책/사안 핵심 명칭 (10~30자)
- 보도자료 원문 제목을 그대로 복사하지 말고 핵심 명칭으로 축약
- 「」 꺾쇠 괄호, 따옴표(", ", ', '), 대괄호([크립토3] 등), 말줄임표(...) **모두 사용 금지**
- 머리표지([단독], [기자수첩] 등)와 부제목·말줄임표 부분은 제거하고 정책·사안의 본질만
- 예: "특정금융정보법 시행령 일부개정안", "AI 기본법 개정", "FATF 평가 대응 AML 규제 강화"

[summary] 한 줄 요약 (30~70자)
- 제목·부제목을 그대로 복사하지 않는다
- 정책 변경의 핵심을 개조식 1줄로 직접 요약
- 예: "처리방침 작성지침 개정 — 생성형 AI 서비스 기준 신설"
- 예: "1000만원 이상 가상자산 거래 의심거래 자동 분류 의무화"

[key_changes] 주요 내용 (3개 이상, 각 25~70자)
- 신설 항목·시행일·적용 대상·의무 사항 등 **구체적 변경사항** 나열
- **summary와 동일한 내용을 첫 항목에 반복하지 말 것** (다른 각도의 사실로 채울 것)
- 배경 설명·기대효과·인사말·정치 발언 등 부연 내용 생략
- **인터뷰 인용문 금지** ("...라며", "...라고 했다", "...덧붙였다" 같은 발화 인용 형식 사용 금지)
- 사실·수치 중심으로 작성
- 예: "8월 20일 시행", "트래블룰 100만원 미만으로 확대", "VASP 보고 의무 강화"

[future_plan] 향후 방향 (선택, 본문에 명시된 경우만)
- 시행 일정, 하위 규정 정비 계획 등이 본문에 있을 때만 1~2줄
- 없으면 반드시 빈 문자열 "" 로 둘 것 (추측 금지)

────────────────────────────────────────
**2단계: 쿠콘 리스크 평가**

[risk_grade] 판정 기준
- "상": 쿠콘 솔루션 즉시 수정·고객 대응 필요 (시행 임박, 의무사항 신설, 직접 제재 등)
- "중": 모니터링·검토 필요 (입법예고, 국회 통과, 동종업계 사고 등)
- "하": 일반 동향 (특별 액션 불필요)
- "무관": 4대 영역과 무관 → 이 경우 impacted_areas=[], coocon_action=""

[risk_type] 규제 | 평판 | 기회 | 무관

[impacted_areas] AML/KYC·PG/결제·마이데이터·가상자산 중에서 선택

[coocon_action] (impacted_areas 있을 때 필수)
- "X 솔루션의 Y 기능 점검 필요", "Z 고객사 대상 안내 발송" 같이 동사로 끝나는 구체 액션
- impacted_areas 비어있으면 ""

────────────────────────────────────────
**문체 규칙 (반드시 준수)**: 모든 텍스트(summary, key_changes, future_plan, coocon_action)는
**~~다체가 아닌 명사형 종결(음슴체)** 로 작성하세요.

예시:
  - "8월 20일 시행" (O) / "8월 20일 시행될 예정이다" (X)
  - "VASP 의무 강화" (O) / "VASP 의무가 강화된다" (X)
  - "솔루션 점검 필요" (O) / "솔루션 점검이 필요합니다" (X)
  - "범죄 수익 적발" (O) / "범죄 수익이 적발됐다" (X)
  - "쿠콘 솔루션 영향도 검토" (O) / "쿠콘 솔루션 영향도를 검토해야 한다" (X)

────────────────────────────────────────
출력은 아래 JSON 스키마만 정확히 따르세요. 다른 설명·머리말·꼬리말 없이 JSON만:

{{
  "policy_name":    "정책 핵심 명칭 (「」 없이)",
  "summary":        "한 줄 요약 (30~70자)",
  "key_changes":    ["구체적 변경 1", "구체적 변경 2", "구체적 변경 3"],
  "future_plan":    "향후 방향 (없으면 \\"\\")",
  "risk_grade":     "상|중|하|무관",
  "risk_type":      "규제|평판|기회|무관",
  "impacted_areas": ["AML/KYC", ...],
  "coocon_action":  "쿠콘 권장 액션 1~2줄"
}}

JSON:"""


def _extract_json(text: str) -> dict | None:
    """Gemini 응답에서 첫 번째 JSON 블록 추출."""
    if not text:
        return None
    # ```json ... ``` 블록 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
    else:
        # 첫 { 부터 마지막 } 까지
        start = text.find("{")
        end   = text.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _strip_brackets(name: str) -> str:
    """정책명 전반 정제: 꺾쇠·따옴표·머리표지·말줄임표 + 미완결 [...] 제거."""
    if not name:
        return ""
    s = name.strip()
    # 1단계: 따옴표 안 인용구 통째로 제거 ("…", "…", ' … ' 등)
    s = re.sub(r'"[^"]*"', " ", s)
    s = re.sub(r'[“][^”]*[”]', " ", s)
    s = re.sub(r"'[^']*'", " ", s)
    s = re.sub(r"[‘][^’]*[’]", " ", s)
    # 2단계: 완결형 대괄호 [단독], [크립토3] 등 제거
    s = re.sub(r"\[[^\]]{1,20}\]", " ", s)
    # 3단계: 미완결 대괄호·괄호 제거 (앞쪽이든 뒤쪽이든)
    s = re.sub(r"\s*\[[^\]]*$", " ", s)
    s = re.sub(r"^\s*[^\[]*\]\s*", " ", s) if s.count("]") > s.count("[") else s
    s = re.sub(r"\s*\([^\)]*$", " ", s)
    # 4단계: 잔존 꺾쇠 제거
    s = s.replace("「", "").replace("」", "")
    s = s.replace("『", "").replace("』", "")
    # 5단계: 말줄임표·잔존 따옴표 — 공백으로 치환해 단어 붙음 방지
    s = re.sub(r"\.{2,}", " ", s)
    s = re.sub(r"…+", " ", s)
    s = re.sub(r"[\"“”'‘’]", "", s)
    # 6단계: — / – 이후 부제목 제거
    s = re.split(r"\s+[—–]\s+", s)[0]
    # 7단계: 공백·구두점 정리
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.\?!])", r"\1", s)  # 공백 후 구두점 → 구두점 붙임
    s = re.sub(r"^[,.\s]+|[,.\s]+$", "", s)
    return s.strip()


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """두 문자열의 어절 자카드 유사도가 threshold 이상이면 동일 토픽으로 간주."""
    if not a or not b:
        return False
    wa = set(re.findall(r"[가-힣A-Za-z0-9]+", a))
    wb = set(re.findall(r"[가-힣A-Za-z0-9]+", b))
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


_QUOTE_PATTERNS = re.compile(r'(라며|라고\s*(했|덧붙였|밝혔|전했|말했))')


def _drop_quote_lines(items: list[str]) -> list[str]:
    """인터뷰 인용문 패턴이 포함된 항목 제거."""
    return [s for s in items if not _QUOTE_PATTERNS.search(s)]


# ── 음슴체(명사형 종결) 변환 ─────────────────────────────────────────────
# 예: "...적발됐다." → "...적발"  /  "필요하다." → "필요"  /  "추세다." → "추세"

_NOUN_RULES = [
    # 어색한 종결을 자연스러운 명사형으로 매핑 (우선 적용)
    (re.compile(r"(?:나온다|나왔다|나오고\s*있다)\.?\s*$"), "제기"),
    (re.compile(r"(?:봤다|본다|보인다)\.?\s*$"),          "전망"),
    (re.compile(r"커지고\s*있\S*\.?\s*$"),                 "확대"),
    (re.compile(r"증가하고\s*있\S*\.?\s*$"),               "증가"),
    (re.compile(r"줄어들고\s*있\S*\.?\s*$"),               "감소"),
    (re.compile(r"늘어나고\s*있\S*\.?\s*$"),               "증가"),
    (re.compile(r"있다\.?\s*$"),                            "있음"),
    (re.compile(r"없다\.?\s*$"),                            "없음"),
    (re.compile(r"(전망|예상|예정|기대|분석|해석)이다\.?\s*$"), r"\1"),
    (re.compile(r"(\S+?)됐다\.?\s*$"),                      r"\1"),
    (re.compile(r"(\S+?)되었다\.?\s*$"),                    r"\1"),
    (re.compile(r"(\S+?)되었습니다\.?\s*$"),                r"\1"),
    (re.compile(r"(\S+?)된다\.?\s*$"),                      r"\1"),
    (re.compile(r"(\S+?)됩니다\.?\s*$"),                    r"\1"),
    (re.compile(r"(\S+?)했다\.?\s*$"),                      r"\1"),
    (re.compile(r"(\S+?)하였다\.?\s*$"),                    r"\1"),
    (re.compile(r"(\S+?)했습니다\.?\s*$"),                  r"\1"),
    (re.compile(r"(\S+?)한다\.?\s*$"),                      r"\1"),
    (re.compile(r"(\S+?)합니다\.?\s*$"),                    r"\1"),
    (re.compile(r"(\S+?)하다\.?\s*$"),                      r"\1"),
    (re.compile(r"이다\.?\s*$"),                            ""),
    (re.compile(r"입니다\.?\s*$"),                          ""),
    # 마지막 폴백: ~다. 종결만 떼기 (이미 위에서 처리 안 된 케이스)
    (re.compile(r"다\.?\s*$"),                              ""),
]


def _to_noun_ending(text: str) -> str:
    """문장 끝 ~~다체를 명사형(음슴체)으로 변환. 빈 문자열은 그대로."""
    if not text:
        return text
    s = text.strip()
    # 마침표 여러 개 정리
    s = re.sub(r"\.{2,}\s*$", "", s)
    for pattern, replacement in _NOUN_RULES:
        new = pattern.sub(replacement, s)
        if new != s:
            s = new
            break
    # 끝 공백·구두점 정리
    s = re.sub(r"[,.\s]+$", "", s).strip()
    return s


def _to_noun_endings(items: list[str]) -> list[str]:
    return [_to_noun_ending(s) for s in items if s]


def _normalize_result(data: dict) -> dict:
    """LLM 출력 정규화 — 누락·잘못된 값을 안전 기본값으로."""
    valid_grades = {"상", "중", "하", "무관"}
    valid_types  = {"규제", "평판", "기회", "무관"}

    grade = str(data.get("risk_grade", "")).strip()
    if grade not in valid_grades:
        grade = "무관"

    rtype = str(data.get("risk_type", "")).strip()
    if rtype not in valid_types:
        rtype = "무관"

    areas = data.get("impacted_areas") or []
    if not isinstance(areas, list):
        areas = []
    areas = [str(a).strip() for a in areas if str(a).strip()]

    # 신규 필드 (PPT 형식)
    policy_name = _strip_brackets(str(data.get("policy_name", "") or "").strip())[:80]
    summary     = str(data.get("summary", "") or "").strip()[:140]
    future_plan = str(data.get("future_plan", "") or "").strip()[:200]

    key_changes = data.get("key_changes") or data.get("key_points") or []
    if not isinstance(key_changes, list):
        key_changes = []
    key_changes = [str(p).strip() for p in key_changes if str(p).strip()]
    # 인터뷰 인용문 제거
    key_changes = _drop_quote_lines(key_changes)
    # summary와 너무 유사한 첫 항목 제거 (중복 방지)
    if summary and key_changes and _similar(summary, key_changes[0]):
        key_changes = key_changes[1:]
    key_changes = key_changes[:4]

    action = str(data.get("coocon_action", "") or "").strip()

    # 정합성 강제: 영향 영역이 없으면 등급도 무관으로 자동 조정
    if not areas and grade in ("상", "중", "하"):
        grade = "무관"
        rtype = "무관"
        action = ""

    # 음슴체 변환 — 텍스트 종결을 명사형으로
    summary     = _to_noun_ending(summary)
    future_plan = _to_noun_ending(future_plan)
    action      = _to_noun_ending(action)
    key_changes = _to_noun_endings(key_changes)

    return {
        "policy_name":    policy_name,
        "summary":        summary,
        "key_changes":    key_changes,
        "future_plan":    future_plan,
        "risk_grade":     grade,
        "risk_type":      rtype,
        "impacted_areas": areas,
        "coocon_action":  action,
        # 하위 호환: 기존 코드가 key_points 참조 시
        "key_points":     key_changes,
        "_source":        "llm",
    }


# 4대 사업영역과 관련된 모든 핵심 키워드 (사전 컷 게이트용)
_RELEVANT_KEYWORDS = set()
for _kws in _AREA_KW.values():
    _RELEVANT_KEYWORDS.update(_kws)
# 일반 AML/규제 관련어도 추가
_RELEVANT_KEYWORDS.update([
    "자금세탁", "특정금융", "특금법", "특금",
    "AML", "KYC", "FIU", "FATF", "STR",
    "보이스피싱", "환전소", "범죄수익", "테러자금", "공협법",
    "전자금융", "전금법", "PG", "지급결제", "결제대행", "오픈뱅킹",
    "마이데이터", "신용정보", "신정법", "본인신용정보",
    "개인정보", "개인정보보호법", "개보법",
    "가상자산", "코인", "스테이블코인", "암호화폐", "디지털자산", "VASP",
    "금융위", "금감원", "금융정보분석원", "개인정보보호위원회", "개보위",
    "트래블룰", "의심거래",
])


# 일일정리·브리핑 류 (여러 주제 묶음 기사는 카드뉴스 부적합)
_BRIEFING_TITLE_KW = [
    "뉴스브리핑", "브리핑", "오늘의 이슈", "이슈 정리",
    "주요 뉴스", "헤드라인", "핫뉴스", "데일리뉴스",
    "오늘의 코인", "마감시황", "장 마감",
]


def _is_relevant_topic(article: dict) -> bool:
    """4대 영역과 직접 연관된 기사만 LLM 평가 대상.

    기준 (모두 만족):
      1. 제목에 브리핑/시황 등 묶음 키워드 없음
      2. **제목 자체에** 4대 영역 핵심 키워드가 1개 이상 포함

    본문에만 키워드가 있는 경우는 통과시키지 않음 (제목과 본문 토픽 불일치 방지).
    "묻지마 살인" 같이 본문에 우연히 AML 키워드가 들어간 기사를 거른다.
    """
    title = article.get("제목", "") or ""

    # 1) 브리핑 류 컷
    if any(kw in title for kw in _BRIEFING_TITLE_KW):
        return False

    # 2) 제목 자체에 키워드 매칭 (의무)
    return any(kw in title for kw in _RELEVANT_KEYWORDS)


def _enrich_article_body(article: dict, min_len: int = 400) -> dict:
    """기사 본문이 짧으면 원문 URL에서 fetch해 보강. dict 복사본 반환."""
    enriched = dict(article)
    body = (enriched.get("내용") or "").strip()
    if len(body) >= min_len:
        return enriched
    fetched = _fetch_original_body(enriched.get("링크", ""), min_len=min_len)
    if fetched and len(fetched) > len(body):
        enriched["내용"] = fetched
    return enriched


def analyze_article(article: dict, policy_db: list[dict] | None = None) -> dict:
    """기사 1건 → 리스크 평가 dict 반환.

    1. 본문 짧으면 원문 URL fetch로 보강
    2. Gemini LLM 평가
    3. LLM 실패 → 룰 기반 폴백
    4. LLM 성공이지만 key_points 비어있으면 본문에서 자동 추출 보완
    """
    enriched = _enrich_article_body(article)

    # 사전 키워드 게이트: 4대 영역과 무관한 기사는 LLM 호출 없이 즉시 무관
    if not _is_relevant_topic(enriched):
        return {
            "policy_name":    "",
            "summary":        "",
            "key_changes":    [],
            "future_plan":    "",
            "risk_grade":     "무관",
            "risk_type":      "무관",
            "impacted_areas": [],
            "coocon_action":  "",
            "key_points":     [],
            "_source":        "pre-gate",
        }

    if not GEMINI_API_KEY:
        result = _classify_by_rule(enriched)
    else:
        prompt = _build_prompt(enriched, policy_db or [])
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        result = None
        for model_id in GEMINI_MODELS:
            try:
                url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"{model_id}:generateContent?key={GEMINI_API_KEY}")
                resp = _SESSION.post(url, json=payload, timeout=25)
                if resp.status_code == 429:
                    continue
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = _extract_json(text)
                if not data:
                    continue
                result = _normalize_result(data)
                break
            except Exception as e:
                print(f"[risk_analyze] {model_id} 실패: {e}")
                continue
        if result is None:
            result = _classify_by_rule(enriched)

    # ── Fallback: policy_name 비어있으면 기사 제목 정제 ──────────────────
    if not result.get("policy_name"):
        t = (enriched.get("제목", "") or "").strip()
        # 1차: 따옴표 안 인용구 제거 시도
        t_quoteless = re.sub(r'["“”\'‘’][^"“”\'‘’]+["“”\'‘’]', " ", t)
        # 따옴표 제거 결과가 충분히 길면 그것을 사용
        sb1 = _strip_brackets(t_quoteless)
        if len(sb1) >= 6:
            result["policy_name"] = sb1[:60]
        else:
            # 2차: 원본에 _strip_brackets만 적용 (따옴표 안 보존)
            sb2 = _strip_brackets(t)
            if len(sb2) >= 6:
                result["policy_name"] = sb2[:60]
            else:
                # 3차: 머리표지만 제거한 최소 정제
                minimal = re.sub(r"^\s*\[[^\]]{1,15}\]\s*", "", t).strip()
                result["policy_name"] = minimal[:60] or t[:60]
        result["_pn_source"] = "title-derived"

    # ── Fallback: summary 비어있으면 ───────────────────────────────────
    # 우선순위: 본문 의미 있는 첫 문장 → 정제된 제목 → 정책명
    if not result.get("summary"):
        body_sentences = _auto_extract_points(enriched.get("내용", ""),
                                              n=1, min_len=20, max_len=80)
        if body_sentences:
            result["summary"] = body_sentences[0]
        else:
            title_summary = _derive_summary_from_title(enriched.get("제목", ""))
            result["summary"] = title_summary or result.get("policy_name", "")
        result["_sm_source"] = "auto-extract"

    # ── Fallback: key_changes 비어있으면 본문에서 자동 추출 ────────────
    if not result.get("key_changes"):
        # summary와 다른 문장들로 채우기 위해 더 많이 뽑고 유사도 필터
        auto = _auto_extract_points(enriched.get("내용", ""), n=8,
                                    min_len=20, max_len=110)
        summary_val = result.get("summary", "")
        filtered = []
        seen_prefixes = set()
        for s in auto:
            if summary_val and _similar(summary_val, s, threshold=0.5):
                continue
            # 항목 간 중복 컷
            prefix = s[:15]
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            filtered.append(s)
            if len(filtered) >= 3:
                break
        if filtered:
            result["key_changes"] = filtered
            result["key_points"]  = filtered
            result["_kc_source"]  = "auto-extract"

    # ── Fallback: 영향 영역 있는데 coocon_action 비어있으면 룰 기반 생성 ───
    if result.get("impacted_areas") and not result.get("coocon_action"):
        areas = ", ".join(result["impacted_areas"][:2])
        rtype = result.get("risk_type", "")
        if rtype == "규제":
            result["coocon_action"] = f"{areas} 솔루션 영향도 검토 및 고객사 안내"
        elif rtype == "평판":
            result["coocon_action"] = f"{areas} 영역 사고 경위 파악 및 자사 솔루션 점검"
        else:
            result["coocon_action"] = f"{areas} 영역 동향 모니터링"
        result["_action_source"] = "rule-default"

    # ── 최종 음슴체 변환 (fallback으로 채운 값도 포함) ───────────────────
    result["summary"]       = _to_noun_ending(result.get("summary", ""))
    result["future_plan"]   = _to_noun_ending(result.get("future_plan", ""))
    result["coocon_action"] = _to_noun_ending(result.get("coocon_action", ""))
    result["key_changes"]   = _to_noun_endings(result.get("key_changes", []) or [])
    result["key_points"]    = result["key_changes"]

    return result


# ── CLI 테스트 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sample = {
        "기관": "한국경제",
        "날짜": "2026-05-10",
        "제목": "[단독] 1주일에 수십억 자금 세탁…'피싱 돈줄' 된 서울 도심",
        "내용": "현행 특정금융정보법은 가상자산 사업자의 자금세탁방지 의무를 중심으로 설계돼 있어 미등록 가상자산 사업자를 규제하기 어렵다. "
                 "범죄 조직이 달러 연동 스테이블코인 테더를 이용해 대규모 자금세탁을 진행 중. 명동의 한 환전소는 6개월간 약 3,100억원 규모의 범죄수익을 테더로 환전.",
        "링크": "https://www.hankyung.com/article/2026051066731",
    }
    result = analyze_article(sample, [])
    print(json.dumps(result, ensure_ascii=False, indent=2))
