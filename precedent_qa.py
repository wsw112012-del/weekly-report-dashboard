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
아래는 사용자의 질문과, 관련도가 높은 한국 판례·유권해석·비조치의견서 발췌 목록입니다.

[작성 지침]
1) 사용자 질문에 직접적으로 답하세요. 추측·면책 문구는 최소화.
2) 사례를 인용할 때는 반드시 본문 안에 [번호] 형식으로 표기 (예: [1], [3]).
3) 답변은 3~5문단으로 구성하고, 각 문단은 의미 단위로 끊으세요.
4) 사례가 다양한 출처(대법원/하급심 판례, 금융위·금감원 유권해석, 비조치의견서)에 걸쳐 있다면, **가능한 한 서로 다른 출처를 골고루 인용**하세요. 판례가 있다면 반드시 1건 이상 포함하세요.
5) 답변 구조는 **요점 → 근거 → 적용** 순으로 가독성 있게 작성하세요. 항목을 나눌 때는 `가.`, `나.`, `다.` 또는 `1)`, `2)` 와 같은 표기를 사용해 가독성을 높이세요.
6) 인용된 사례 번호 외 다른 사례는 답변에 포함하지 마세요.
7) 한국어로 답변, 마크다운 표(table)·코드블록 사용 금지. 단, 강조가 필요한 구절은 **굵게** 표기 가능.

[사용자 질문]
{question}

[관련 사례 목록]
{cases}

[답변]
"""


_SRC_LABEL = {
    "prec":       "판례(법원)",
    "fsc_reply":  "유권해석(금융위·금감원)",
    "fsc_nonact": "비조치의견서",
    "law_article":"법령 조문",
}


def build_prompt(question: str, candidates: list[dict], max_cases: int = 20) -> str:
    cases = []
    for i, c in enumerate(candidates[:max_cases], 1):
        title = (c.get("title") or "").strip()
        agency = (c.get("agency") or "").strip()
        date_ = (c.get("decided_at") or "").strip()
        src   = _SRC_LABEL.get((c.get("source") or "").lower(), "사례")
        summary = (c.get("summary") or "").strip()[:600]
        body = (c.get("body") or "").strip()[:800]
        block = f"[{i}] ({src}) {title}\n    기관: {agency} | 일자: {date_}\n"
        if summary:
            block += f"    질의/요지: {summary}\n"
        if body:
            block += f"    회신/판결: {body}\n"
        cases.append(block)
    return _PROMPT_HEADER.format(question=question.strip(), cases="\n".join(cases))


# ── 통합 검색 (Westlaw IA — Phase A) ─────────────────────────────────────
def build_unified_candidates(query: str, types: list[str], laws: list[str],
                              top_k: int = 50) -> list[dict]:
    """precedent_db + law_articles 통합 검색.

    Args:
        query: 자연어 또는 키워드
        types: ['prec','fsc_reply','fsc_nonact','law_article'] 부분집합. 빈 리스트 = 전부.
        laws:  대상 법령 정식명 리스트. 빈 리스트 = 전부.
        top_k: 정확도 상위 N건

    Returns:
        [{source, item_id, title, agency, decided_at, summary, body, link,
          law_id, law_type, jo_label, jo_title, _score}]  통합 스키마
    """
    types = types or ['prec', 'fsc_reply', 'fsc_nonact', 'law_article']
    out: list[dict] = []

    # precedent_db
    if any(t in types for t in ('prec', 'fsc_reply', 'fsc_nonact')):
        prec_rows = []
        if laws:
            or_parts = [f"target_law.eq.{urllib.parse.quote(l, safe='')}" for l in laws]
            flt = f"&or=({','.join(or_parts)})"
        else:
            flt = ""
        # 유형 필터
        prec_types = [t for t in types if t in ('prec', 'fsc_reply', 'fsc_nonact')]
        if prec_types and len(prec_types) < 3:
            type_or = ",".join(f"source.eq.{t}" for t in prec_types)
            flt += f"&or=({type_or})" if not flt else f"&and=(source.in.({','.join(prec_types)}))"
        try:
            prec_rows = _sb_get(
                f"precedent_db?select=id,source,target_law,title,agency,case_no,"
                f"decided_at,summary,body,ref_laws,link"
                f"&limit=2000{flt}"
            )
        except Exception:
            prec_rows = []
        for r in prec_rows:
            if prec_types and r.get('source') not in prec_types:
                continue
            out.append({
                "source":     r.get("source", "prec"),
                "item_id":    r.get("id"),
                "target_law": r.get("target_law", ""),
                "title":      r.get("title", ""),
                "agency":     r.get("agency", ""),
                "case_no":    r.get("case_no", ""),
                "decided_at": r.get("decided_at", ""),
                "summary":    r.get("summary", "") or "",
                "body":       r.get("body", "") or "",
                "ref_laws":   r.get("ref_laws", "") or "",
                "link":       r.get("link", ""),
            })

    # law_articles
    if 'law_article' in types:
        try:
            law_rows = _sb_get(
                "law_articles?select=id,law_id,law_name,law_type,jo_no,jo_label,"
                "jo_title,body&limit=2000"
            )
        except Exception:
            law_rows = []
        for r in law_rows:
            # 법령 필터 (law_name 부분 매칭)
            if laws:
                hit = any(L.replace(' ', '') in (r.get('law_name','').replace(' ', ''))
                          for L in laws)
                if not hit:
                    continue
            out.append({
                "source":     "law_article",
                "item_id":    r.get("id"),
                "target_law": r.get("law_name", ""),
                "title":      f"{r.get('law_name','')} {r.get('jo_label','')} {r.get('jo_title','')}".strip(),
                "agency":     "",
                "case_no":    "",
                "decided_at": "",
                "summary":    "",
                "body":       r.get("body", "") or "",
                "ref_laws":   "",
                "link":       "",
                "law_id":     r.get("law_id", ""),
                "law_type":   r.get("law_type", ""),
                "jo_label":   r.get("jo_label", ""),
                "jo_title":   r.get("jo_title", ""),
            })

    # 자카드 정확도 스코어링
    q_tokens = _tokens(query)
    if q_tokens:
        for item in out:
            text = " ".join(filter(None, [
                item.get("title"), item.get("summary"),
                (item.get("body") or "")[:1500]
            ]))
            item["_score"] = _jaccard(q_tokens, _tokens(text))
        out = [x for x in out if x.get("_score", 0) > 0]
        out.sort(key=lambda x: -x["_score"])
    else:
        for item in out:
            item["_score"] = 0.0

    return out[:top_k]


# ── 법령 조문 후보 (Phase 3 — 파일 분석용) ─────────────────────────────────
def build_law_candidates(text: str, top_k: int = 15, db_limit: int = 2000) -> list[dict]:
    """law_articles 에서 텍스트 토큰 자카드 상위 top_k 반환."""
    try:
        rows = _sb_get(
            f"law_articles?select=id,law_id,law_name,law_type,jo_no,jo_label,jo_title,body"
            f"&limit={db_limit}"
        )
    except Exception:
        rows = []
    q_tokens = _tokens(text)
    if not q_tokens:
        return rows[:top_k]
    scored = []
    for r in rows:
        body = (r.get("body") or "")[:1500]
        cell = " ".join(filter(None, [r.get("law_name"), r.get("jo_title"), body]))
        sim = _jaccard(q_tokens, _tokens(cell))
        if sim > 0:
            scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:top_k]]


# ── 파일 분석용 프롬프트 ─────────────────────────────────────────────────────
_DOC_PROMPT_HEADER = """\
당신은 한국 금융·개인정보 법령 분야 전문가입니다.
사용자가 법무팀·법무법인의 법률검토 문서를 첨부했고, 우리 DB의 회신사례·판례·법령 조문과
비교 검증을 요청했습니다.

[작성 지침]
1) 첨부 문서의 핵심 쟁점을 3~7개 글머리(•) 형식으로 추출하고, 이를 답변 맨 앞에 'KEY_ISSUES:' 라벨로 묶어 적으세요.
2) 본문 답변에서 우리 회신사례·판례를 인용할 때 [1][2] 형식, 법령 조문 인용은 [L1][L2] 형식을 사용하세요.
3) 사용자의 추가 질문이 있으면 함께 답하고, 없으면 쟁점 분석에만 집중.
4) 답변 마지막 단락은 "일치 / 보완 / 불명확" 포인트로 명시적으로 정리.
5) 한국어, 마크다운 사용 금지. 평어체.

[첨부 법률검토 본문]
{doc_text}

[사용자 추가 질문]
{question}

[우리 회신사례·판례 후보]
{cases}

[우리 법령 조문 후보]
{laws}

[답변]
"""


def build_doc_prompt(doc_text: str, question: str | None,
                     precedent_cands: list[dict],
                     law_cands: list[dict],
                     max_cases: int = 15,
                     max_laws: int = 10) -> str:
    cases = []
    for i, c in enumerate(precedent_cands[:max_cases], 1):
        cases.append(
            f"[{i}] {(c.get('title') or '').strip()}\n"
            f"    기관: {(c.get('agency') or '').strip()} | 일자: {(c.get('decided_at') or '').strip()}\n"
            f"    요지: {((c.get('summary') or '').strip())[:400]}"
        )
    laws = []
    for i, l in enumerate(law_cands[:max_laws], 1):
        laws.append(
            f"[L{i}] {l.get('law_name','')} {l.get('jo_label','')} {l.get('jo_title','')}\n"
            f"     {((l.get('body') or '').strip())[:500]}"
        )
    return _DOC_PROMPT_HEADER.format(
        doc_text=(doc_text or "")[:25000],
        question=(question or "").strip() or "(없음)",
        cases="\n\n".join(cases) or "(매칭된 사례 없음)",
        laws="\n\n".join(laws) or "(매칭된 조문 없음)",
    )


_KEY_ISSUES_RE = re.compile(r"KEY_ISSUES\s*:\s*(.*?)(?:\n\s*\n|\Z)", re.DOTALL | re.IGNORECASE)
_BULLET_LINE_RE = re.compile(r"^[\s•\-·▪▶◆●○]+\s*(.+)$", re.MULTILINE)
_LAW_CITE_RE = re.compile(r"\[L(\d+)\]")


def parse_doc_response(answer: str, precedent_cands: list[dict],
                       law_cands: list[dict],
                       max_cases: int = 15, max_laws: int = 10) -> dict:
    """answer 에서 KEY_ISSUES / [n] / [Ln] 추출."""
    # 1) KEY_ISSUES 블록 추출
    key_issues: list[str] = []
    m = _KEY_ISSUES_RE.search(answer or "")
    answer_body = answer or ""
    if m:
        block = m.group(1)
        for line_m in _BULLET_LINE_RE.finditer(block):
            line = line_m.group(1).strip()
            if line:
                key_issues.append(line)
        # KEY_ISSUES 블록은 본문에서 제거
        answer_body = (answer[:m.start()] + answer[m.end():]).strip()

    # 2) [n] 인용 → citations
    citations = parse_citations(answer_body, precedent_cands, max_cases=max_cases)

    # 3) [Ln] 법령 인용 → law_citations
    law_used: set[int] = set()
    for cm in _LAW_CITE_RE.finditer(answer_body):
        try:
            n = int(cm.group(1))
            if 1 <= n <= max_laws:
                law_used.add(n)
        except ValueError:
            continue
    law_citations: list[dict] = []
    for n in sorted(law_used):
        if n - 1 >= len(law_cands):
            continue
        l = law_cands[n - 1]
        law_citations.append({
            "idx":      n,
            "law_id":   l.get("law_id"),
            "law_name": l.get("law_name"),
            "law_type": l.get("law_type"),
            "jo_label": l.get("jo_label"),
            "jo_title": l.get("jo_title"),
            "body":     l.get("body"),
        })

    return {
        "answer":        answer_body,
        "key_issues":    key_issues,
        "citations":     citations,
        "law_citations": law_citations,
    }


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
