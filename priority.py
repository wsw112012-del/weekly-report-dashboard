"""priority.py — 보도자료·언론기사 우선순위(상/중/하) 산정.

app.py 의 동일 이름 함수와 같은 로직 (중복 정의 회피용 경량 모듈).
collect_입법현황.py 의 LEGISLATION_TARGETS 를 단일 진실 원천으로 사용.
"""
from collect_입법현황 import LEGISLATION_TARGETS


# 대상 법령명 — 가중치 ×3
_LAW_NAMES: list[str] = [
    law for laws in LEGISLATION_TARGETS.values() for law in laws
]
# 법령 단축 별칭 — 가중치 ×3
_LAW_ALIASES: list[str] = [
    "특금법", "특정금융정보법", "특정금융거래법",
    "개인정보보호법", "신용정보법", "정보통신망법",
    "전자금융거래법", "공협법", "테러자금금지법", "테러자금방지법",
]
# 법률 동작 키워드 — 가중치 ×1 (누적)
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
    """보도자료·언론기사 우선순위 자동 산정 (상/중/하)."""
    text = (article.get("제목") or "") + " " + (article.get("내용") or "")
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
