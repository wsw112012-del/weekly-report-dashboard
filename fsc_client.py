"""fsc_client.py — 금융위원회 회신사례 포털(better.fsc.go.kr) 클라이언트.

엔드포인트:
  - selectReplyCaseLawreqList.do  : 법령해석 목록 (DataTables JSON)
  - LawreqDetail.do?lawreqIdx=X   : 법령해석 상세 (HTML, td 본문)
  - selectReplyCaseOpinionList.do : 비조치의견서 목록
  - OpinionDetail.do?opinionIdx=X : 비조치의견서 상세

목록 응답: {recordsTotal, data: [{lawreqIdx|opinionIdx, category, title,
            (lawreq|opinion)Number, status, dpNm}], recordsFiltered}

상세 페이지 td 순서:
  td0: title(헤더)
  td1: 처리구분  td2: 소관부서
  td3: title  td4: 처리구분  td5: 공개여부(Y/N)
  td6: 등록자  td7: 회신일자  td8: 첨부파일
  td9: 질의요지  td10: 회답  td11: 이유
"""
from __future__ import annotations

import re
import sys
import urllib3
import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BASE = "https://better.fsc.go.kr"
_LIST_URL_BASE = "/fsc_new/replyCase/TotalReplyList.do?stNo=11&muNo=117&muGpNo=75"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class FscClient:
    KINDS = {
        "law":     {
            "list_url":    "/fsc_new/replyCase/selectReplyCaseLawreqList.do",
            "detail_url":  "/fsc_new/replyCase/LawreqDetail.do",
            "idx_key":     "lawreqIdx",
            "number_key":  "lawreqNumber",
            "menu":        {"muNo": "109", "muGpNo": "75", "stNo": "11"},
            "source":      "fsc_reply",
        },
        "opinion": {
            "list_url":    "/fsc_new/replyCase/selectReplyCaseOpinionList.do",
            "detail_url":  "/fsc_new/replyCase/OpinionDetail.do",
            "idx_key":     "opinionIdx",
            "number_key":  "opinionNumber",
            "menu":        {"muNo": "111", "muGpNo": "75", "stNo": "11"},
            "source":      "fsc_nonact",
        },
    }

    def __init__(self, verify_ssl: bool = False, timeout: int = 20):
        self._s = requests.Session()
        self._s.verify = verify_ssl
        self._s.headers.update({
            "User-Agent": _UA,
            "Referer":    f"{_BASE}/fsc_new/replyCase/TotalReplyList.do",
        })
        self._timeout = timeout
        # 세션 쿠키 초기화
        self._s.get(f"{_BASE}{_LIST_URL_BASE}", timeout=timeout)

    def _reset_session(self) -> None:
        """세션 만료/SSL EOF 등 후 새 세션 + 쿠키 재발급."""
        import time
        time.sleep(2)
        self._s = requests.Session()
        self._s.verify = False
        self._s.headers.update({
            "User-Agent": _UA,
            "Referer":    f"{_BASE}/fsc_new/replyCase/TotalReplyList.do",
        })
        try:
            self._s.get(f"{_BASE}{_LIST_URL_BASE}", timeout=self._timeout)
        except Exception:
            pass

    def list_items(self, kind: str, start: int = 0, length: int = 100,
                   search: str = "", retries: int = 3) -> dict:
        """kind in {'law','opinion'} 의 목록 한 페이지 조회. 일시 실패 시 재시도."""
        if kind not in self.KINDS:
            raise ValueError(f"kind 는 {list(self.KINDS)} 중 하나")
        cfg = self.KINDS[kind]
        data = {
            "draw": "1",
            "start": str(start),
            "length": str(length),
            "search[value]": search,
            "search[regex]": "false",
            "order[0][column]": "0",
            "order[0][dir]": "desc",
        }
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = self._s.post(f"{_BASE}{cfg['list_url']}", data=data,
                                 headers={"X-Requested-With": "XMLHttpRequest"},
                                 timeout=self._timeout)
                r.raise_for_status()
                ct = r.headers.get("Content-Type", "")
                if "json" not in ct.lower() and not r.text.lstrip().startswith("{"):
                    # 사이트가 에러 HTML 반환 — 세션 갱신 후 재시도
                    raise ValueError(f"non-json response (ct={ct[:40]})")
                return r.json()
            except Exception as e:
                last_err = e
                self._reset_session()
        # 모두 실패 시 빈 결과 반환 (호출자가 다음 페이지로 진행 가능)
        print(f"  [WARN] list_items 최종 실패 (kind={kind} start={start}): {last_err}",
              file=sys.stderr)
        return {"recordsTotal": 0, "data": []}

    def list_all(self, kind: str, max_items: int | None = None, page_size: int = 100):
        """페이지 순회 generator."""
        start = 0
        total = None
        while True:
            resp = self.list_items(kind, start=start, length=page_size)
            if total is None:
                total = int(resp.get("recordsTotal", 0))
            for row in resp.get("data", []) or []:
                yield row
            start += page_size
            if start >= total:
                return
            if max_items is not None and start >= max_items:
                return

    def fetch_detail(self, kind: str, idx: int | str, retries: int = 2) -> dict:
        """상세 페이지 td 본문 파싱. SSL EOF 등 일시 실패 시 재시도.
        반환: {title, status, department, public_yn, register, replied_at,
               attachment, question, answer, reason, link}
        """
        cfg = self.KINDS[kind]
        params = {**cfg["menu"], cfg["idx_key"]: str(idx)}
        last_err = None
        r = None
        for attempt in range(retries + 1):
            try:
                r = self._s.get(f"{_BASE}{cfg['detail_url']}",
                                params=params, timeout=self._timeout)
                r.raise_for_status()
                break
            except Exception as e:
                last_err = e
                self._reset_session()
        if r is None:
            raise RuntimeError(f"detail fetch 실패: {last_err}")
        soup = BeautifulSoup(r.text, "lxml")
        tds = soup.find_all("td")

        def _get(i: int) -> str:
            if i < len(tds):
                return tds[i].get_text("\n", strip=True)
            return ""

        # td 인덱스는 위 docstring 참조. 안전을 위해 길이 체크
        title       = _get(0) or _get(3)
        status      = _get(1)
        department  = _get(2)
        public_yn   = _get(5)
        register    = _get(6)
        replied_at  = _get(7)
        attachment  = _get(8)
        question    = _get(9)
        answer      = _get(10)
        reason      = _get(11)

        return {
            "title":       title.strip(),
            "status":      status.strip(),
            "department":  department.strip(),
            "public_yn":   public_yn.strip(),
            "register":    register.strip(),
            "replied_at":  replied_at.strip(),
            "attachment":  attachment.strip(),
            "question":    question.strip(),
            "answer":      answer.strip(),
            "reason":      reason.strip(),
            "link":        f"{_BASE}{cfg['detail_url']}?"
                           f"{'&'.join(f'{k}={v}' for k,v in params.items())}",
        }
