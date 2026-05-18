"""daily_monitor_post.py — 매일 수집 직후 Flow에 AML 데일리 뉴스 모니터링 게시글 1건.

파이프라인:
  1. Supabase에서 오늘 AML 보도/언론기사 로드
  2. priority.get_priority() == "상" 1차 컷
  3. 노이즈 키워드 (오늘의 일정·사설·간담회 등) 제외
  4. risk_analyze.analyze_article() (Gemini) 로 평가
  5. risk_grade ∈ {"상", "중"} 통과
  6. 국내/해외 분리 + 입법동향(오늘 일자 단계만) 추가
  7. v1 Bot API 게시글 1건으로 발송

환경변수:
  FLOW_API_KEY      Flow v1 Bot API 키 (x-flow-api-key)
  FLOW_BOT_ID       게시 봇 식별자 (예: biz@coocon.net)
  FLOW_PROJECT_ID   게시 대상 프로젝트 ID
  SUPABASE_URL/KEY  Supabase REST 접근
  GEMINI_API_KEY    risk_analyze.analyze_article() 용

옵션:
  --dry-run    API 호출 없이 본문만 stdout 출력
  --allow-empty  항목 0건이어도 게시
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _today_kst() -> date:
    """KST 기준 오늘 날짜. GitHub Actions(UTC)·로컬 어디서 돌아도 동일하게 KST 일자를 반환."""
    return datetime.now(_KST).date()

from flow_bot import FlowBot
from priority import get_priority

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# ── 분류·필터 상수 ────────────────────────────────────────────────────────────

# 노이즈 컷 — 일정/사설/형식적 보도자료
_NOISE_RE = re.compile(
    r"\[오늘의|오늘의\s*(일정|국회|주요)|국회일정|주요일정|"
    r"\[사설\]|사설\s*[:\]]|방문\s*간담회|취임\s*인사|기념\s*행사|보도참고\]"
)

# AML 무관 오프토픽 — 정치 스캔들·연예·부동산·시세·홍보·칼럼 등.
# 5/17·5/18 운영 중 검출된 실제 케이스 기반으로 보수적으로 작성
# (지나친 일반화 시 가상자산·AML 관련 정상 기사가 잘릴 위험).
_OFFTOPIC_RE = re.compile(
    # 정치 일일 브리핑 묶음 (다주제 혼합)
    r"\[아주이슈\]|\[오늘\s*정치|정치\s*브리핑|"
    # 정치 스캔들 시그니처 표현
    r"사과만\s*할게요|묵묵부답|따까리|"
    # 사생활/이혼 법정
    r"재산분할|이혼\s*소송|위자료\s*청구|"
    # 연예
    r"넷플릭스\s*(라인업|오리지널)|영화\s*제작|드라마\s*(공개|방영)|"
    # 부동산 특집(희망 부동산·집값 등 — 결제수단 보조 기사)
    r"희망\s*20\d{2}\s*부동산|월세.*카드결제|"
    # 의견·시론·칼럼 (AML 정책 직접 보도 아님)
    r"\[(칼럼|시론|사설|논단|로터리|기고)\]|"
    # 회사 홍보·소개 시리즈
    r"\[(보안\s*리딩기업|혁신\s*기업|기업\s*탐방|CEO\s*인터뷰|테크\s*리더)\]|"
    # 시세 헤드라인 (가격 돌파·회복·자금 유입)
    r"\d+\s*달러\s*(돌파|회복)|\d+억\s*달러\s*몰린|시총\s*\d|"
    # 코인 가격 평론
    r"내\s*코인\s*가격|왜\s*내\s*코인|개미\s*투자자.*손실|"
    # 인플루언서 본인 거래 일화
    r"나도\s*[\d.]+\s*(ETH|BTC|XRP|SOL)"
)

# 제목 토픽 화이트리스트 — 제목에 아래 키워드 중 하나도 없으면 AML 무관으로 컷.
# (본문에 "자금세탁/AML" 한두 줄 부수 등장 케이스 — UAE 시장진출·금융권 AI 일반 등 제외용)
_TOPIC_RE = re.compile(
    # AML 직접 키워드
    r"자금\s*세탁|세탁|AML|CDD|KYC|"
    # 가상자산·코인·디지털자산
    r"가상자산|암호화폐|비트코인|이더리움|스테이블|디지털\s*자산|토큰|NFT|"
    r"블록체인|웹\s*3|Web\s*3|코인|"
    # 거래소·지갑
    r"거래소|지갑|월렛|"
    r"두나무|업비트|빗썸|코인원|코빗|고팍스|바이낸스|"
    # 법령·규제기관
    r"특금법|외환법|외국환|전자금융|신용정보|개인정보|정보통신망|"
    r"FIU|금융위|금감원|금융정보분석원|"
    # 규제·집행
    r"규제|제재|과태료|위반|입법|시행령|개정|"
    r"검거|적발|압수|동결|수사|기소|판결|처벌|"
    # 보고·이상거래
    r"트래블룰|STR|CTR|의심거래|이상거래|"
    # 사기 유형
    r"피싱|리딩방|투자리딩|다단계|금융사기|"
    # 해외 정책
    r"클래리티법|MiCA|FATF|"
    # 기타
    r"보이스피싱|불법.*자금|발의안"
)

# 매체 블랙리스트 — AML 관련 기사를 거의 다루지 않는 매체.
# 통과돼도 본문에 자금세탁 단어가 짧게 들어가서 nois cut 우회.
_BLACKLIST_AGENCIES = {
    # KOTRA 시장진출 정보 — 시장진출/투자환경 일반
    "DREAM",
    # 연예·스포츠 매체
    "STARNEWSKOREA", "OSEN", "TV리포트", "스타뉴스", "일간스포츠",
    "스포츠경향", "텐아시아", "마이데일리", "엑스포츠뉴스",
}

# 해외 동향 판별 — 해외 정부/규제기관 또는 명백한 해외 정책 흐름만.
# 단순 외국 회사명(코인베이스/바이낸스 등)은 국내 보도에도 자주 등장하므로 제외.
_OVERSEAS_RE = re.compile(
    r"美\s|미국\s|중국\s|中\s|일본\s|日\s|영국|英\s|EU\b|유럽연합|독일|프랑스|"
    r"싱가포르|홍콩|대만|베트남|인도|호주|캐나다|러시아|두바이|UAE|"
    r"FATF|G7|G20|IMF|BIS|OECD|백악관|연준|미\s*연준|Fed\b|"
    r"美\s*SEC|미\s*SEC|美\s*CFTC|미\s*CFTC|FinCEN|FCA|MAS|"
    r"트럼프|바이든|시진핑|기시다|클래리티\s*법안|GENIUS\s*Act|MiCA"
)
# 한국 컨텍스트 우선 — 위 키워드가 있어도 한국이 주체이면 국내로 분류.
_KR_CONTEXT_RE = re.compile(
    r"한국판|국내(?!\s*외)|한국(?:의|이|에서|에서는|에선)|"
    r"국회\s*(?:통과|의결|상정|가결)|금융위(원회)?|FIU|금감원|"
    r"한은|한국은행|기재부|기획재정부|금융정보분석원"
)

LEG_STATUS_KEYWORDS = ("공포", "국무회의", "시행", "가결")
_DATE_RE = re.compile(r"(\d{4})[\.\s\-]+(\d{1,2})[\.\s\-]+(\d{1,2})")

# 한글 2글자 이상 토큰 (단어 단위 의사 명사 추출)
_HANGUL_RE = re.compile(r"[가-힣]{2,}")
_STOPWORDS: set[str] = {
    "관련", "위한", "위해", "통한", "통해", "대한", "대해", "있다", "있는", "없다", "없는",
    "한다", "이다", "되다", "된다", "이라", "이라고", "이라는", "그리고", "또한", "그러나",
    "하지만", "그것", "이것", "저것", "어떤", "그런", "이런", "저런", "모든", "여러",
    "오늘", "어제", "내일", "올해", "작년", "최근", "지금", "당시", "당분간", "이상", "이하",
    "기자", "보도", "발표", "기사", "확인", "예정", "가능", "필요", "정도", "수준",
    "않다", "되어", "위해", "라고", "라며", "이라며",
}
# 주요 매체 신뢰도 (대표 매체 우선)
_TRUSTED_AGENCIES: list[str] = [
    "연합뉴스", "연합인포맥스", "매일경제", "조선일보", "중앙일보", "한국경제", "동아일보",
    "조선비즈", "이데일리", "뉴스1", "News1", "한겨레", "경향신문", "서울경제",
    "헤럴드경제", "파이낸셜뉴스", "머니투데이", "디지털타임스", "DIGITALTODAY",
    "비즈워치", "비즈니스워치", "전자신문", "법률신문", "메트로신문",
    "SBS", "KBS", "MBC", "YTN", "JTBC",
]


def _tokens(text: str) -> set[str]:
    """제목·본문에서 한글 명사 후보 토큰 set 추출."""
    if not text:
        return set()
    raw = _HANGUL_RE.findall(text)
    return {w for w in raw if w not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _agency_rank(agency: str) -> int:
    """기관 신뢰도 순위. 낮을수록 우선."""
    a = (agency or "").strip()
    for i, t in enumerate(_TRUSTED_AGENCIES):
        if t == a or t in a or a in t:
            return i
    return len(_TRUSTED_AGENCIES) + 1


def _is_similar_pair(a: dict, b: dict, thresh_title: float = 0.6,
                     thresh_combined: float = 0.55) -> bool:
    """두 기사 a, b 가 중복으로 판정되는지.
    1) 링크 완전 일치 → True
    2) 제목 명사 자카드 ≥ thresh_title 또는
       (제목+본문) 명사 자카드 ≥ thresh_combined → True
    """
    la, lb = (a.get("링크") or "").strip(), (b.get("링크") or "").strip()
    if la and la == lb:
        return True
    ta, tb = a.get("제목") or "", b.get("제목") or ""
    title_sim = _jaccard(_tokens(ta), _tokens(tb))
    if title_sim >= thresh_title:
        return True
    full_a = ta + " " + (a.get("내용") or "")[:1200]
    full_b = tb + " " + (b.get("내용") or "")[:1200]
    combined_sim = _jaccard(_tokens(full_a), _tokens(full_b))
    return combined_sim >= thresh_combined


def _pick_representative(group: list[dict]) -> dict:
    """동일 그룹 안에서 대표 1건 선정 — 본문 길이 desc, 기관 신뢰도 asc."""
    def score(x: dict) -> tuple:
        body_len = len(x.get("내용") or "")
        agency = _agency_rank(x.get("기관") or "")
        return (-body_len, agency)
    return sorted(group, key=score)[0]


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(s) -> str:
    """HTML 엔티티 디코드 + 양끝 공백 제거."""
    return html.unescape(str(s or "")).strip()


def _is_noise(article: dict) -> bool:
    text = (article.get("제목") or "") + " " + (article.get("내용") or "")[:500]
    if _NOISE_RE.search(text) or _OFFTOPIC_RE.search(text):
        return True
    # 매체 블랙리스트 — AML과 무관한 매체
    if (article.get("기관") or "").strip() in _BLACKLIST_AGENCIES:
        return True
    # 제목 토픽 화이트리스트 — 제목에 AML 토픽 키워드가 하나도 없으면 컷
    if not _TOPIC_RE.search(article.get("제목") or ""):
        return True
    return False


def _is_overseas(article: dict) -> bool:
    """해외 키워드가 있어도 한국 컨텍스트(한국판/국내/금융위 등)가 더 강하면 국내."""
    text = (article.get("제목") or "") + " " + (article.get("내용") or "")[:400]
    if not _OVERSEAS_RE.search(text):
        return False
    return not _KR_CONTEXT_RE.search(text)


def _key_points(item: dict) -> list[str]:
    kp = item.get("key_points") or []
    if isinstance(kp, list):
        return [_clean(x) for x in kp if x][:3]
    if isinstance(kp, str):
        return [_clean(kp)]
    return []


def _event_date(r: dict) -> str | None:
    """입법현황 row → '실제 단계 발생일' (status 괄호 1순위, propose_date 2순위)."""
    m = re.search(r"\((\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", r.get("status") or "")
    if not m:
        m = _DATE_RE.search(r.get("propose_date") or "")
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _sb_get(path: str):
    url = f"{os.environ['SUPABASE_URL']}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": os.environ["SUPABASE_KEY"],
        "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 데이터 수집 ────────────────────────────────────────────────────────────────

def _fetch_aml_articles(today_str: str, days: int = 2) -> list[dict]:
    """AML 기사 → 오늘 priority='상' + 노이즈 제거 + 다단계 중복 제거.

    중복 제거 단계:
      A. 링크/정규화 제목 1차 dedup (기존)
      B. 어제 priority='상' 후보와 비교 — URL 일치 또는 제목+본문 명사 유사도
         60%↑ 이면 오늘 후보에서 제외 (사용자 요청 1)
      C. 오늘 후보 안에서 명사 유사 그룹화 → 그룹당 본문 길이 desc, 매체 신뢰도
         asc 기준 대표 1건만 선정 (사용자 요청 2)
    """
    rows = _sb_get("articles?type=eq.AML&select=data&order=updated_at.desc&limit=1")
    if not rows:
        return []
    raw = rows[0].get("data") or []

    today_dt = date.fromisoformat(today_str)
    cutoff = today_dt - timedelta(days=days - 1)
    cutoff_str = cutoff.isoformat()
    prev_day_str = (today_dt - timedelta(days=1)).isoformat()

    def _in_range(a: dict) -> bool:
        d = (a.get("날짜") or "")[:10]
        return cutoff_str <= d <= today_str

    # ── 후보 추출 (priority='상' + 노이즈 컷 + 날짜 범위)
    candidates = [a for a in raw
                  if _in_range(a) and get_priority(a) == "상" and not _is_noise(a)]

    # ── A. 링크/정규화 제목 1차 dedup
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for a in candidates:
        link = (a.get("링크") or "").strip()
        title_key = re.sub(r"\s+", "", _clean(a.get("제목") or ""))
        if link and link in seen_links:
            continue
        if title_key and title_key in seen_titles:
            continue
        if link:        seen_links.add(link)
        if title_key:   seen_titles.add(title_key)
        deduped.append(a)

    # ── B. 어제(또는 그 전날) 후보와 명사 유사도 비교 → 중복 제외
    prev_candidates = [a for a in raw
                       if (a.get("날짜") or "")[:10] < today_str
                       and (a.get("날짜") or "")[:10] >= prev_day_str
                       and get_priority(a) == "상" and not _is_noise(a)]
    if prev_candidates:
        filtered: list[dict] = []
        dropped_prev = 0
        for cur in deduped:
            is_dup = any(_is_similar_pair(cur, p) for p in prev_candidates)
            if is_dup:
                dropped_prev += 1
                continue
            filtered.append(cur)
        if dropped_prev:
            print(f"[INFO] 어제 발송 유사 기사 제외: {dropped_prev}건", file=sys.stderr)
        deduped = filtered

    # ── C. 오늘 후보들끼리 명사 유사도 그룹화 → 대표 1건
    groups: list[list[dict]] = []
    for cur in deduped:
        placed = False
        for g in groups:
            if _is_similar_pair(cur, g[0]):
                g.append(cur)
                placed = True
                break
        if not placed:
            groups.append([cur])
    repr_only: list[dict] = []
    dropped_cycle = 0
    for g in groups:
        repr_only.append(_pick_representative(g))
        dropped_cycle += len(g) - 1
    if dropped_cycle:
        print(f"[INFO] 사이클 내 유사 기사 묶음 제외: {dropped_cycle}건", file=sys.stderr)

    # ── _overseas 플래그 + 정렬
    out = [{**a, "_overseas": _is_overseas(a)} for a in repr_only]
    out.sort(key=lambda x: (
        1 if x["_overseas"] else 0,
        -date.fromisoformat((x.get("날짜") or today_str)[:10]).toordinal(),
    ))
    return out


# ── 임베딩 기반 유사도 (Gemini gemini-embedding-001) ──────────────────────────

_EMBED_MODEL = "models/gemini-embedding-001"
_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/{model}:embedContent"
_EMBED_SIM_THRESHOLD = 0.88  # cosine — 같은 사안 0.88+, 다른 사안 ≤0.81 분리선 (회색지대 보수화)
_EMBED_WORKERS = 8


def _embed_text(article: dict) -> str:
    """임베딩 입력 텍스트 — 제목 + 본문 앞 800자."""
    title = _clean(article.get("제목") or "")
    body = _clean(article.get("내용") or "")[:800]
    return (title + "\n" + body).strip()


def _embed_one(text: str, api_key: str) -> list[float] | None:
    payload = {
        "content": {"parts": [{"text": text or " "}]},
        "taskType": "SEMANTIC_SIMILARITY",
    }
    url = _EMBED_URL.format(model=_EMBED_MODEL) + f"?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("embedding", {}).get("values")
    except Exception as e:
        print(f"[WARN] embedContent 실패: {e}", file=sys.stderr)
        return None


def _gemini_embed_batch(texts: list[str]) -> list[list[float]] | None:
    """ThreadPool 로 embedContent 병렬 호출. 단 하나라도 실패하면 None."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not texts:
        return None
    from concurrent.futures import ThreadPoolExecutor
    results: list[list[float] | None] = [None] * len(texts)
    try:
        with ThreadPoolExecutor(max_workers=_EMBED_WORKERS) as ex:
            futures = {ex.submit(_embed_one, t, api_key): i for i, t in enumerate(texts)}
            for f in futures:
                i = futures[f]
                results[i] = f.result()
    except Exception as e:
        print(f"[WARN] Gemini 임베딩 실패 → jaccard 폴백: {e}", file=sys.stderr)
        return None
    if any(r is None for r in results):
        print("[WARN] 일부 임베딩 누락 → jaccard 폴백", file=sys.stderr)
        return None
    return results  # type: ignore[return-value]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _group_by_embedding(articles: list[dict], embeddings: list[list[float]],
                       exclude_embs: list[list[float]],
                       threshold: float = _EMBED_SIM_THRESHOLD,
                       ) -> list[list[dict]]:
    """임베딩 cosine 기반 그룹화. exclude_embs 와 가까운 항목은 사전 제거."""
    keep_idx = []
    for i, emb in enumerate(embeddings):
        if any(_cosine(emb, e) >= threshold for e in exclude_embs):
            continue
        keep_idx.append(i)

    groups: list[list[int]] = []
    for i in keep_idx:
        placed = False
        for g in groups:
            if _cosine(embeddings[i], embeddings[g[0]]) >= threshold:
                g.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])
    return [[articles[i] for i in g] for g in groups]


def _fetch_all_aml_titles(today_str: str, days: int = 2,
                          exclude: list[dict] | None = None) -> list[dict]:
    """AML 전체 기사(우선순위 무관) → 노이즈 컷 + 1차 dedup + 유사도 그룹 대표만.

    - 상등급(이미 요약 카드로 노출된 항목)은 exclude 로 받아 유사도 비교로 추가 제외.
    - 결과는 제목·URL만 노출하는 용도이므로 본문은 그대로 두되 정렬·중복 제거에만 활용.
    """
    rows = _sb_get("articles?type=eq.AML&select=data&order=updated_at.desc&limit=1")
    if not rows:
        return []
    raw = rows[0].get("data") or []

    today_dt = date.fromisoformat(today_str)
    cutoff_str = (today_dt - timedelta(days=days - 1)).isoformat()

    def _in_range(a: dict) -> bool:
        d = (a.get("날짜") or "")[:10]
        return cutoff_str <= d <= today_str

    candidates = [a for a in raw if _in_range(a) and not _is_noise(a)]

    # ── A. 링크/정규화 제목 1차 dedup
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for a in candidates:
        link = (a.get("링크") or "").strip()
        title_key = re.sub(r"\s+", "", _clean(a.get("제목") or ""))
        if link and link in seen_links:
            continue
        if title_key and title_key in seen_titles:
            continue
        if link:        seen_links.add(link)
        if title_key:   seen_titles.add(title_key)
        deduped.append(a)

    # ── B+C. 임베딩 cosine 으로 상등급 유사 제외 + 후보 내부 그룹화
    #         실패 시 명사 jaccard 폴백
    exclude_list = list(exclude or [])
    all_articles = exclude_list + deduped
    embeddings = _gemini_embed_batch([_embed_text(a) for a in all_articles])

    if embeddings and len(embeddings) == len(all_articles):
        exclude_embs = embeddings[:len(exclude_list)]
        cand_embs = embeddings[len(exclude_list):]
        groups = _group_by_embedding(deduped, cand_embs, exclude_embs)
        print(f"[INFO] 전체기사 임베딩 dedup: 후보 {len(deduped)} → 그룹 {len(groups)}",
              file=sys.stderr)
    else:
        # 폴백: 명사 jaccard
        if exclude_list:
            deduped = [a for a in deduped
                       if not any(_is_similar_pair(a, e) for e in exclude_list)]
        groups = []
        for cur in deduped:
            placed = False
            for g in groups:
                if _is_similar_pair(cur, g[0]):
                    g.append(cur)
                    placed = True
                    break
            if not placed:
                groups.append([cur])
    repr_only = [_pick_representative(g) for g in groups]

    # 최신순 정렬
    repr_only.sort(key=lambda x: (x.get("날짜") or "")[:10], reverse=True)
    return repr_only


def _fetch_legislation(today_str: str) -> list[dict]:
    """AML 카테고리 입법현황 중 status/propose_date 가 오늘인 항목."""
    rows = _sb_get(f"legislation_status?category=eq.AML"
                   f"&select=bill_title,ministry,status,link,target_law,propose_date"
                   f"&limit=1000")
    seen: set[str] = set()
    result: list[dict] = []
    for r in rows:
        if not any(kw in (r.get("status") or "") for kw in LEG_STATUS_KEYWORDS):
            continue
        if _event_date(r) != today_str:
            continue
        key = (r.get("target_law") or "") + "|" + (r.get("bill_title") or "")
        if key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


# ── 본문 조립 (시안 A: 컴팩트 카드) ────────────────────────────────────────────

_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"


def _format_article_card(idx: int, x: dict) -> str:
    date_str = (x.get("날짜") or "")[:10]
    lines = [
        _SEP,
        f"🔴 [상]  #{idx}  ({date_str})",
        _clean(x.get("제목")),
        f"· 기관: {_clean(x.get('기관') or '-')}",
    ]
    # 본문 lead 2~3줄 (있으면) — 너무 길지 않게 자름
    body = _clean(x.get("내용") or "")
    if body:
        # 첫 문단(개행 기준) 또는 200자
        first_para = body.split("\n")[0][:240]
        lines.append(f"· 요약: {first_para}")
    link = x.get("링크") or ""
    if link:
        lines.append(f"· 원문: {link}")
    return "\n".join(lines)


def _format_title_url(idx: int, x: dict) -> str:
    """전체 기사 — 제목·URL만 한 항목당 1~2줄."""
    title = _clean(x.get("제목"))
    link = x.get("링크") or ""
    line1 = f"{idx:>2}. {title}"
    return f"{line1}\n    {link}" if link else line1


def _format_leg_card(idx: int, r: dict) -> str:
    title = _clean(r.get("bill_title"))
    ministry = _clean(r.get("ministry") or "-")
    status = _clean(r.get("status") or "-")
    link = r.get("link") or ""
    lines = [
        _SEP,
        f"⚖️ [입법]  #{idx}",
        title,
        f"· 부처: {ministry}",
        f"· 단계: {status}",
    ]
    if link:
        lines.append(f"· 원문: {link}")
    return "\n".join(lines)


def build_contents(today_str: str, days: int = 2) -> tuple[str, int]:
    articles = _fetch_aml_articles(today_str, days=days)
    legs = _fetch_legislation(today_str)
    all_titles = _fetch_all_aml_titles(today_str, days=days, exclude=articles)

    domestic = [x for x in articles if not x["_overseas"]]
    overseas = [x for x in articles if x["_overseas"]]

    total = len(articles) + len(legs) + len(all_titles)
    range_str = (f"{(date.fromisoformat(today_str) - timedelta(days=days-1)).strftime('%m.%d')}"
                 f"~{date.fromisoformat(today_str).strftime('%m.%d')}")
    header = [
        f"📊 AML 모니터링 — {today_str.replace('-', '.')} ({range_str} 수집분)",
        f"국내 {len(domestic)}건 · 해외 {len(overseas)}건 · 입법 {len(legs)}건"
        f" · 전체 {len(all_titles)}건",
    ]
    sections: list[str] = []
    idx = 0
    if domestic:
        sections.append(f"\n🇰🇷 국내 동향 ({len(domestic)}건)")
        for x in domestic:
            idx += 1
            sections.append(_format_article_card(idx, x))
    if overseas:
        sections.append(f"\n🌍 해외 동향 ({len(overseas)}건)")
        for x in overseas:
            idx += 1
            sections.append(_format_article_card(idx, x))
    if legs:
        sections.append(f"\n⚖️ 입법동향 ({len(legs)}건)")
        for r in legs:
            idx += 1
            sections.append(_format_leg_card(idx, r))

    if all_titles:
        sections.append(_SEP)
        sections.append(f"\n📰 전체 기사 — 제목·URL ({len(all_titles)}건, 중복 제외)")
        for i, x in enumerate(all_titles, start=1):
            sections.append(_format_title_url(i, x))

    if total == 0:
        sections.append("\n오늘 신규 의미있는 AML 동향이 없습니다.")
    else:
        sections.append(_SEP)

    return "\n".join(header + sections), total


# ── main ──────────────────────────────────────────────────────────────────────

# ── 발송 로그 (같은 날 중복 발송 방지) ─────────────────────────────────────
def _check_already_posted(today_str: str) -> dict | None:
    """flow_post_log 에 오늘 row 있으면 dict 반환, 없으면 None."""
    su = os.environ.get("SUPABASE_URL"); sk = os.environ.get("SUPABASE_KEY")
    if not su or not sk:
        return None
    url = f"{su}/rest/v1/flow_post_log?post_date=eq.{today_str}&select=*"
    req = urllib.request.Request(url, headers={
        "apikey": sk, "Authorization": f"Bearer {sk}"})
    try:
        rows = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return rows[0] if rows else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[WARN] flow_post_log 테이블 미존재 — 중복 발송 방지 무력화. "
                  "schema_flow_log.sql 실행 필요", file=sys.stderr)
        else:
            print(f"[WARN] flow_post_log 조회 실패: HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[WARN] flow_post_log 조회 실패: {e}", file=sys.stderr)
        return None


def _log_post(today_str: str, response: dict, total: int) -> None:
    """게시 성공 후 flow_post_log upsert."""
    su = os.environ.get("SUPABASE_URL"); sk = os.environ.get("SUPABASE_KEY")
    if not su or not sk:
        return
    data_node = (response or {}).get("response", {}).get("data", {}) or {}
    row = {
        "post_date":   today_str,
        "bot_id":      os.environ.get("FLOW_BOT_ID", ""),
        "project_id":  str(data_node.get("projectId") or os.environ.get("FLOW_PROJECT_ID", "")),
        "post_id":     str(data_node.get("postId") or ""),
        "tiny_url":    data_node.get("tinyUrl") or "",
        "total_items": total,
    }
    req = urllib.request.Request(
        f"{su}/rest/v1/flow_post_log",
        data=json.dumps(row, ensure_ascii=False).encode("utf-8"),
        headers={"apikey": sk, "Authorization": f"Bearer {sk}",
                 "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[WARN] flow_post_log 테이블 미존재 — 발송 로그 기록 실패. "
                  "schema_flow_log.sql 실행 필요", file=sys.stderr)
        else:
            print(f"[WARN] flow_post_log 기록 실패: HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] flow_post_log 기록 실패: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="API 호출 없이 본문만 stdout 출력")
    parser.add_argument("--allow-empty", action="store_true",
                        help="항목 0건이어도 게시")
    parser.add_argument("--force", action="store_true",
                        help="오늘 이미 게시했어도 강제 재발송")
    parser.add_argument("--date", default=None,
                        help="특정 날짜로 backfill (YYYY-MM-DD). 미지정 시 KST 오늘")
    args = parser.parse_args()

    missing = [k for k in ("FLOW_API_KEY", "FLOW_BOT_ID", "FLOW_PROJECT_ID",
                            "SUPABASE_URL", "SUPABASE_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"[ERROR] 환경변수 누락: {', '.join(missing)}", file=sys.stderr)
        return 1
    if not os.environ.get("GEMINI_API_KEY"):
        print("[WARN] GEMINI_API_KEY 없음 — risk_analyze 가 룰 기반 폴백으로만 동작",
              file=sys.stderr)

    if args.date:
        try:
            today_str = date.fromisoformat(args.date).isoformat()
        except ValueError:
            print(f"[ERROR] --date 형식 오류 (YYYY-MM-DD 필요): {args.date}", file=sys.stderr)
            return 1
    else:
        today_str = _today_kst().isoformat()

    # 중복 발송 방지 — 같은 날 이미 게시된 경우 skip (--force 로 무시 가능)
    if not args.dry_run and not args.force:
        existing = _check_already_posted(today_str)
        if existing:
            print(f"[INFO] {today_str} 이미 게시됨 — postId={existing.get('post_id')} "
                  f"tinyUrl={existing.get('tiny_url')} (강제 재발송: --force)")
            return 0

    contents, total = build_contents(today_str)
    title = f"📊 AML 일일 모니터링 — {today_str.replace('-', '.')}"

    print(f"[INFO] today={today_str} total={total}건")
    print(f"[INFO] title={title}")
    print("---- contents ----")
    print(contents)
    print("---- /contents ----")

    if args.dry_run:
        print("[INFO] --dry-run, API 호출 생략")
        return 0
    if total == 0 and not args.allow_empty:
        print("[INFO] 항목 0건 — 게시 생략 (--allow-empty 로 강제 게시 가능)")
        return 0

    bot = FlowBot(os.environ["FLOW_API_KEY"])
    res = bot.create_post(
        bot_id=os.environ["FLOW_BOT_ID"],
        project_id=os.environ["FLOW_PROJECT_ID"],
        title=title,
        contents=contents,
    )
    print(f"[OK] response: {json.dumps(res, ensure_ascii=False)}")
    _log_post(today_str, res, total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
