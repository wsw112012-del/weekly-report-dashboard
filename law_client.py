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
