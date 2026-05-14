"""law_client.py — 법제처 국가법령정보 OpenAPI 얇은 클라이언트.

활용: https://www.law.go.kr/DRF/lawSearch.do (목록), lawService.do (상세)
인증: OC 파라미터(가입 ID, 예: 'woowoo')

지원 target:
  - law   : 법령 검색·본문 (현행법령)
  - prec  : 판례 검색·본문
  - expc  : 법령해석례 검색·본문
  - admrul: 행정규칙(고시·훈령·감독규정)

사내망 self-signed cert 대응 — verify=False 기본.
"""
from __future__ import annotations

import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BASE_SEARCH  = "https://www.law.go.kr/DRF/lawSearch.do"
_BASE_SERVICE = "https://www.law.go.kr/DRF/lawService.do"
_DETAIL_BASE  = "https://www.law.go.kr"  # 응답 안의 상대 link 절대화


class LawClient:
    def __init__(self, oc: str, verify_ssl: bool = False, timeout: int = 20):
        self.oc = oc
        self._verify = verify_ssl
        self._timeout = timeout

    # ── 검색 ──────────────────────────────────────────────────────────────────
    def _search(self, target: str, query: str = "", display: int = 50,
                page: int = 1, **extra) -> dict:
        params = {"OC": self.oc, "target": target, "type": "JSON", "display": display, "page": page}
        if query:
            params["query"] = query
        params.update(extra)
        r = requests.get(_BASE_SEARCH, params=params, verify=self._verify, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def search_law(self, query: str, display: int = 50) -> list[dict]:
        """현행법령 검색. 응답: [{법령ID, 법령일련번호(MST), 법령명한글, 법령구분명, ...}]"""
        data = self._search("law", query=query, display=display)
        root = data.get("LawSearch", {})
        items = root.get("law", []) or []
        return items if isinstance(items, list) else [items]

    def search_precedent(self, query: str, display: int = 50, page: int = 1) -> list[dict]:
        """판례 검색."""
        data = self._search("prec", query=query, display=display, page=page)
        root = data.get("PrecSearch", {})
        items = root.get("prec", []) or []
        return items if isinstance(items, list) else [items]

    def search_expc(self, query: str, display: int = 50, page: int = 1) -> list[dict]:
        """법령해석례 검색."""
        data = self._search("expc", query=query, display=display, page=page)
        root = data.get("Expc", {})
        items = root.get("expc", []) or []
        return items if isinstance(items, list) else [items]

    def search_admrul(self, query: str, display: int = 50, page: int = 1) -> list[dict]:
        """행정규칙 검색 (감독규정·시행규칙·고시·훈령 등).
        응답 키: 행정규칙ID, 행정규칙일련번호, 행정규칙명, 행정규칙종류, 발령기관, ...
        """
        data = self._search("admrul", query=query, display=display, page=page)
        root = data.get("AdmRulSearch", {})
        items = root.get("admrul", []) or []
        return items if isinstance(items, list) else [items]

    # ── 상세 조회 ─────────────────────────────────────────────────────────────
    def _service(self, target: str, **params) -> dict:
        params = {"OC": self.oc, "target": target, "type": "JSON", **params}
        r = requests.get(_BASE_SERVICE, params=params, verify=self._verify, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def fetch_law_articles(self, mst: str) -> list[dict]:
        """법령 본문 조회(조문 단위). MST = 법령일련번호.

        응답 정규화: [{조문번호, 조문가지번호, 조문제목, 조문내용, 항: [...]}]
        """
        data = self._service("law", MST=str(mst))
        root = data.get("법령", {})
        jomun = root.get("조문", {})
        if isinstance(jomun, dict):
            arr = jomun.get("조문단위") or []
        else:
            arr = jomun or []
        if isinstance(arr, dict):
            arr = [arr]
        return arr

    def fetch_precedent_detail(self, prec_id: str) -> dict:
        """판례 본문(판시사항·판결요지·참조판례·이유 등)."""
        data = self._service("prec", ID=str(prec_id))
        return data.get("PrecService", {}) or {}

    def fetch_expc_detail(self, expc_id: str) -> dict:
        """법령해석례 본문(질의요지·회신·이유)."""
        data = self._service("expc", ID=str(expc_id))
        return data.get("ExpcService", {}) or {}

    def fetch_admrul_articles(self, rule_id: str) -> list[dict]:
        """행정규칙 본문 조회 + 조문 단위 파싱. rule_id = 행정규칙일련번호 (ID 파라미터).

        응답 AdmRulService.조문내용 은 보통 list of strings — 각 element 가 한 조문
        ('제N조(제목) 본문...') 인데, 외국환거래규정 등 일부 행정규칙은 element 안에
        여러 조문이 합쳐진 경우도 있음. 두 케이스 모두 처리하기 위해 각 element 안에서
        조문 헤더(제N조 / 제M-N조 / 제N조의M) 로 split.
        반환 스키마는 fetch_law_articles 와 호환: [{조문번호, 조문제목, 조문내용, 항}]
        """
        import re
        data = self._service("admrul", ID=str(rule_id))
        root = data.get("AdmRulService") or {}
        if not root:
            return []
        items = root.get("조문내용") or []
        if isinstance(items, str):
            items = [items]
        elif isinstance(items, dict):
            items = [items.get("조문내용") or ""]
        if not items:
            return []

        # 헤더 패턴 두 종:
        # (A) 줄 시작 anchor — 다중 라인 element 의 본문 안 참조 (예: 법 제3조) 보호
        # (B) anchor 무시 + 괄호 제목 필수 — 단일 라인 blob (외국환거래규정 등) 처리
        head_pat_lined = re.compile(
            r"(?:^|\n)\s*제\s*(\d+)\s*(?:[-\s]\s*(\d+))?\s*(?:조의\s*(\d+))?\s*조"
            r"\s*(?:\(([^)]*)\))?",
            re.UNICODE,
        )
        head_pat_blob = re.compile(
            r"제\s*(\d+)\s*(?:[-\s]\s*(\d+))?\s*(?:조의\s*(\d+))?\s*조"
            r"\s*\(([^)]+)\)",  # blob 케이스는 (제목) 필수 — 참조 오인 차단
            re.UNICODE,
        )
        articles: list[dict] = []
        for raw in items:
            if not isinstance(raw, str):
                continue
            txt = raw.strip()
            if not txt:
                continue
            # \n 없으면 blob 케이스 (외국환거래규정), 있으면 lined 케이스
            head_pat = head_pat_blob if "\n" not in txt else head_pat_lined
            matches = list(head_pat.finditer(txt))
            if not matches:
                # 헤더 없음 → 전체를 단일 항목 (장 제목 등은 무시되어도 됨)
                continue
            for i, m in enumerate(matches):
                # 매칭 위치 ~ 다음 매칭 직전 까지가 한 조문 본문
                seg_start = m.start()
                seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(txt)
                segment = txt[seg_start:seg_end].strip()
                if not segment:
                    continue
                # 조문번호 표기 — "1", "1-1", "3-2(의2)" 케이스 처리
                num = m.group(1) or ""
                dash = m.group(2)
                ui   = m.group(3)
                jo_no = num
                if dash:
                    jo_no = f"{num}-{dash}"
                elif ui:
                    jo_no = f"{num}-{ui}"
                title = (m.group(4) or "").strip()
                body  = segment[m.end() - seg_start:].strip()
                if not body:
                    continue
                articles.append({
                    "조문번호": jo_no or str(len(articles) + 1),
                    "조문제목": title,
                    "조문내용": body,
                    "항": [],
                })
        return articles

    # ── helper ────────────────────────────────────────────────────────────────
    @staticmethod
    def absolutize(link: str) -> str:
        """응답 안의 상대 link(/DRF/...)를 절대 URL로."""
        if not link:
            return ""
        if link.startswith("http://") or link.startswith("https://"):
            return link
        if link.startswith("/"):
            return _DETAIL_BASE + link
        return link
