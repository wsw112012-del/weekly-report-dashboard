"""precedent_qa.py — AI 자연어 검색 도우미.

흐름:
  build_candidates(question, law_ids) → Supabase precedent_db 후보 30건
  build_prompt(question, candidates)   → Gemini 입력 prompt 문자열
  parse_citations(answer, candidates)  → answer 안 [n] 마커 기반 인용 카드 추출

토큰화·자카드는 daily_monitor_post 와 동일 패턴 (의존성 분리 위해 자체 정의).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

# ── 토큰 / 유사도 ─────────────────────────────────────────────────────────────
_HANGUL_RE = re.compile(r"[가-힣]{2,}")
_STOPWORDS: set[str] = {
    "관련", "위한", "위해", "통한", "통해", "대한", "대해", "있다", "있는",
    "없다", "없는", "한다", "이다", "되다", "된다", "그리고", "또한", "그러나",
    "오늘", "어제", "내일", "올해", "최근", "지금", "예정", "가능",
    "기준", "경우", "여부", "어떻게", "어떤", "무엇", "어디", "언제",
}


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {w for w in _HANGUL_RE.findall(text) if w not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalize_question(q: str) -> str:
    """질문 정규화 — 양끝 공백 제거 + 연속 공백 단일화 + 소문자."""
    if not q:
        return ""
    q = re.sub(r"\s+", " ", q.strip())
    return q.lower()


def build_cache_key(question: str, law_ids: list[str]) -> str:
    norm_q = normalize_question(question)
    laws_key = ",".join(sorted(law_ids or []))
    return hashlib.sha1(f"{norm_q}:{laws_key}".encode("utf-8")).hexdigest()


# ── 후보 검색 ────────────────────────────────────────────────────────────────
def _sb_get(path: str) -> list:
    su = os.environ.get("SUPABASE_URL")
    sk = os.environ.get("SUPABASE_KEY")
    if not su or not sk:
        return []
    req = urllib.request.Request(
        f"{su}/rest/v1/{path}",
        headers={"apikey": sk, "Authorization": f"Bearer {sk}"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def build_candidates(question: str, law_ids: list[str],
                     top_k: int = 30, db_limit: int = 1500) -> list[dict]:
    """precedent_db 에서 법령 필터 통과 행 → 질문 토큰 자카드 상위 top_k 반환."""
    # 1) law_ids in.() 필터 — 선택된 법령만
    if law_ids:
        in_list = ",".join(f'"{urllib.parse.quote(l, safe="")}"' for l in law_ids)
        # PostgREST in 필터는 in.(value1,value2,...) — 한글은 URL 인코딩 필요
        # 간단하게 or filter 로 대체
        or_parts = [f"target_law.eq.{urllib.parse.quote(l, safe='')}" for l in law_ids]
        flt = f"or=({','.join(or_parts)})"
    else:
        flt = ""
    qs = "precedent_db?select=id,source,target_law,title,agency,decided_at,summary,body,link" \
         f"&limit={db_limit}"
    if flt:
        qs += f"&{flt}"
    try:
        rows = _sb_get(qs)
    except Exception:
        rows = []

    # 2) 질문 토큰 vs 행 텍스트 토큰 자카드
    q_tokens = _tokens(question)
    if not q_tokens:
        return rows[:top_k]

    scored = []
    for r in rows:
        text = " ".join(filter(None, [
            r.get("title"), r.get("summary"), (r.get("body") or "")[:1500]
        ]))
        sim = _jaccard(q_tokens, _tokens(text))
        if sim > 0:
            scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:top_k]]


# ── Gemini 프롬프트 ──────────────────────────────────────────────────────────
_PROMPT_HEADER = """\
당신은 한국 금융·개인정보 법령 분야의 법률·규제 전문가입니다.
아래는 사용자의 질문과, 관련도가 높은 한국 회신사례·판례 발췌 목록입니다.

[작성 지침]
1) 사용자 질문에 직접적으로 답하세요. 추측·면책 문구는 최소화.
2) 사례를 인용할 때는 반드시 본문 안에 [번호] 형식으로 표기 (예: [1], [3]).
3) 답변은 3~5문단으로 구성하고, 각 문단은 의미 단위로 끊으세요.
4) 인용된 사례 번호 외 다른 사례는 답변에 포함하지 마세요.
5) 한국어로 답변, 마크다운 사용 금지.

[사용자 질문]
{question}

[관련 사례 목록]
{cases}

[답변]
"""


def build_prompt(question: str, candidates: list[dict], max_cases: int = 20) -> str:
    cases = []
    for i, c in enumerate(candidates[:max_cases], 1):
        title = (c.get("title") or "").strip()
        agency = (c.get("agency") or "").strip()
        date_ = (c.get("decided_at") or "").strip()
        summary = (c.get("summary") or "").strip()[:600]
        body = (c.get("body") or "").strip()[:800]
        block = f"[{i}] {title}\n    기관: {agency} | 일자: {date_}\n"
        if summary:
            block += f"    질의/요지: {summary}\n"
        if body:
            block += f"    회신/판결: {body}\n"
        cases.append(block)
    return _PROMPT_HEADER.format(question=question.strip(), cases="\n".join(cases))


# ── 인용 파싱 ────────────────────────────────────────────────────────────────
_CITE_RE = re.compile(r"\[(\d+)\]")


def parse_citations(answer: str, candidates: list[dict],
                    max_cases: int = 20) -> list[dict]:
    """answer 안 [n] 마커 추출 → 후보 배열의 (n-1) 인덱스 원본을 citations 카드로."""
    used_idx: set[int] = set()
    for m in _CITE_RE.finditer(answer or ""):
        try:
            n = int(m.group(1))
            if 1 <= n <= max_cases:
                used_idx.add(n)
        except ValueError:
            continue
    out: list[dict] = []
    for n in sorted(used_idx):
        if n - 1 >= len(candidates):
            continue
        c = candidates[n - 1]
        out.append({
            "idx":          n,
            "precedent_id": c.get("id"),
            "source":       c.get("source"),
            "title":        c.get("title"),
            "agency":       c.get("agency"),
            "decided_at":   c.get("decided_at"),
            "summary":      c.get("summary"),
            "body":         c.get("body"),
            "link":         c.get("link"),
        })
    return out
