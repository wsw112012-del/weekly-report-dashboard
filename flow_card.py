"""
flow_card.py — Pillow 기반 카드뉴스 이미지 생성기.

AML 일일 다이제스트 카드 1장 = 기사 1건. 1200x800 PNG.
Flow 채널에 한 포스트 안의 다중 이미지로 첨부되어 슬라이드처럼 넘겨 볼 수 있음.

사용:
    from flow_card import render_card
    render_card(card_data, Path('out.png'))
"""
import html
import os
import re
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _clean(text: str) -> str:
    """HTML 엔티티 디코딩 + 공백 정규화."""
    if not text:
        return ""
    decoded = html.unescape(str(text))
    # 잔여 < > 태그 제거
    decoded = re.sub(r"<[^>]+>", "", decoded)
    return re.sub(r"\s+", " ", decoded).strip()


def _fit_url(draw, url: str, font, max_width: int) -> str:
    """URL이 max_width를 초과하면 중간 생략(앞부분…뒷부분) 형태로 단축."""
    if not url:
        return ""
    if draw.textlength(url, font=font) <= max_width:
        return url
    # 호스트 추출
    m = re.match(r"^(https?://[^/]+)(/.*)?$", url)
    if not m:
        return url
    host, path = m.group(1), m.group(2) or ""
    # 호스트만으로도 넘치면 그냥 호스트 표시
    if draw.textlength(host, font=font) >= max_width:
        return host
    # 호스트 + 경로 양끝 ... 중간 생략
    suffix = path[-15:] if len(path) > 15 else path
    candidate = host + path
    while len(candidate) > 30 and draw.textlength(candidate + " … " + suffix, font=font) > max_width:
        candidate = candidate[:-1]
    return candidate + " … " + suffix if candidate != host + path else candidate


# ── 폰트 ──────────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",           # Windows
    "C:/Windows/Fonts/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",  # Ubuntu (GH Actions)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/Library/Fonts/AppleSDGothicNeo.ttc",   # macOS
]
_FONT_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = _FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # 어떤 폰트도 못 찾으면 기본(영문) 폰트라도
    return ImageFont.load_default()


# ── 디자인 상수 ────────────────────────────────────────────────────────────────

CARD_W, CARD_H = 1200, 800

COLOR_BG       = (250, 250, 252)
COLOR_HEADER   = (30, 41, 59)       # 다크 네이비
COLOR_HEADER_T = (255, 255, 255)
COLOR_DIVIDER  = (226, 232, 240)
COLOR_TEXT     = (15, 23, 42)
COLOR_MUTED    = (100, 116, 139)
COLOR_ACTION_BG = (254, 252, 232)   # 옅은 노랑
COLOR_ACTION_BORDER = (250, 204, 21)

# 리스크 등급별 배지 색상
GRADE_COLORS = {
    "상":  (220, 38, 38),     # 빨강
    "중":  (234, 88, 12),     # 주황
    "하":  (100, 116, 139),   # 회색
    "무관": (148, 163, 184),  # 연회색
}

# 리스크 타입별 배지 색상
TYPE_COLORS = {
    "규제": (37, 99, 235),    # 파랑
    "평판": (124, 58, 237),   # 보라
    "기회": (22, 163, 74),    # 초록
    "무관": (100, 116, 139),
}


# ── 텍스트 헬퍼 ────────────────────────────────────────────────────────────────

def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """한글·영문 혼용 텍스트를 픽셀 폭 기준으로 줄바꿈."""
    if not text:
        return []
    lines = []
    for paragraph in text.split("\n"):
        cur = ""
        for ch in paragraph:
            test = cur + ch
            w = draw.textlength(test, font=font)
            if w <= max_width:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        if not paragraph:
            lines.append("")
    return lines


def _draw_pill(draw, x, y, text, font, bg_color, text_color=(255, 255, 255),
               pad_x=14, pad_y=6, radius=14) -> int:
    """둥근 모서리 알약형 배지 그리기. 오른쪽 끝 x 좌표 반환."""
    tw = draw.textlength(text, font=font)
    # textbbox로 정확한 높이 추정
    bbox = draw.textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    w = int(tw + pad_x * 2)
    h = int(th + pad_y * 2)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=bg_color)
    # 텍스트 baseline 조정: bbox top offset 제거
    draw.text((x + pad_x, y + pad_y - bbox[1]), text, font=font, fill=text_color)
    return x + w


# ── 메인 카드 렌더링 ───────────────────────────────────────────────────────────

def render_card(card: dict, out_path: Path,
                page_no: int | None = None,
                total: int | None = None,
                digest_date: str | None = None) -> Path:
    """카드 1장을 out_path 에 PNG로 저장.

    card 스키마:
        title           기사 제목 (str)
        agency          출처 기관/언론사 (str)
        date            기사 날짜 'YYYY-MM-DD' (str)
        risk_grade      "상|중|하|무관"
        risk_type       "규제|평판|기회|무관"
        impacted_areas  ["AML/KYC", ...]
        key_points      ["...", "..."]
        coocon_action   권장 액션 (str, 옵션)
        link            원문 URL (옵션)
    """
    img = Image.new("RGB", (CARD_W, CARD_H), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ── 헤더 ──────────────────────────────────────────────────────────────
    HEADER_H = 80
    draw.rectangle([0, 0, CARD_W, HEADER_H], fill=COLOR_HEADER)

    f_hdr = _load_font(26, bold=True)
    today = digest_date or date.today().strftime("%Y.%m.%d")
    draw.text((40, 26), "AML 일일 리스크 다이제스트", font=f_hdr, fill=COLOR_HEADER_T)

    f_hdr_sub = _load_font(20)
    sub_text = today
    if page_no and total:
        sub_text = f"{today}  ·  {page_no}/{total}"
    sw = draw.textlength(sub_text, font=f_hdr_sub)
    draw.text((CARD_W - 40 - sw, 30), sub_text, font=f_hdr_sub, fill=(200, 211, 224))

    # ── 본문 영역 ──────────────────────────────────────────────────────────
    y = HEADER_H + 36
    x_left = 40
    body_w = CARD_W - x_left * 2

    # ── 1) 등급/타입 배지 ────────────────────────────────────────────────
    f_pill = _load_font(20, bold=True)
    grade = card.get("risk_grade", "무관")
    rtype = card.get("risk_type", "무관")

    grade_color = GRADE_COLORS.get(grade, GRADE_COLORS["무관"])
    type_color  = TYPE_COLORS.get(rtype, TYPE_COLORS["무관"])

    end_x = _draw_pill(draw, x_left, y, f"[{grade}] 등급", f_pill, grade_color)
    _draw_pill(draw, end_x + 10, y, f"#{rtype}", f_pill, type_color)
    y += 56

    # ── 2) ◆ 정책명 (CLAUDE.md: 「」로 감싼 핵심 명칭) ──────────────────────
    f_policy = _load_font(30, bold=True)
    policy_name = _clean(card.get("policy_name", "")) or _clean(card.get("title", ""))
    policy_display = f"◆ 「{policy_name}」"
    p_lines = _wrap_text(draw, policy_display, f_policy, body_w)
    for line in p_lines[:2]:
        draw.text((x_left, y), line, font=f_policy, fill=COLOR_TEXT)
        y += 42

    # ── 3) 출처·날짜 ──────────────────────────────────────────────────────
    y += 2
    f_meta = _load_font(19)
    agency = _clean(card.get("agency", ""))
    date_s = card.get("date", "")
    meta_text = f"{agency}  ·  {date_s}" if agency and date_s else (agency or date_s)
    draw.text((x_left, y), meta_text, font=f_meta, fill=COLOR_MUTED)
    y += 30

    # 디바이더
    draw.line([(x_left, y), (CARD_W - x_left, y)], fill=COLOR_DIVIDER, width=2)
    y += 18

    # ── 4) ▷ 한 줄 요약 ──────────────────────────────────────────────────
    summary = _clean(card.get("summary", ""))
    if summary:
        f_summary = _load_font(22, bold=True)
        s_lines = _wrap_text(draw, "▷ " + summary, f_summary, body_w)
        for line in s_lines[:2]:
            draw.text((x_left, y), line, font=f_summary, fill=COLOR_HEADER)
            y += 32
        y += 8

    # ── 5) ◆ 주요 내용 (구체적 변경사항) ────────────────────────────────────
    changes = [_clean(p) for p in (card.get("key_changes") or []) if _clean(p)]
    if changes:
        f_section = _load_font(20, bold=True)
        draw.text((x_left, y), "◆ 주요 내용", font=f_section, fill=COLOR_HEADER)
        y += 30
        f_bullet = _load_font(20)
        for ch in changes[:3]:
            draw.ellipse([x_left + 10, y + 11, x_left + 16, y + 17], fill=COLOR_HEADER)
            wrapped = _wrap_text(draw, ch, f_bullet, body_w - 30)
            for j, line in enumerate(wrapped[:2]):
                draw.text((x_left + 26, y + j * 28), line, font=f_bullet, fill=COLOR_TEXT)
            y += max(28, min(len(wrapped), 2) * 28) + 4
        y += 6

    # ── 6) ◆ 향후 방향 (있는 경우만) ────────────────────────────────────
    future = _clean(card.get("future_plan", ""))
    if future:
        f_section = _load_font(20, bold=True)
        draw.text((x_left, y), "◆ 향후 방향", font=f_section, fill=COLOR_HEADER)
        y += 28
        f_bullet = _load_font(20)
        draw.ellipse([x_left + 10, y + 11, x_left + 16, y + 17], fill=COLOR_HEADER)
        wrapped = _wrap_text(draw, future, f_bullet, body_w - 30)
        for j, line in enumerate(wrapped[:2]):
            draw.text((x_left + 26, y + j * 28), line, font=f_bullet, fill=COLOR_TEXT)
        y += max(28, min(len(wrapped), 2) * 28) + 8

    # ── 7) 영향 영역 ──────────────────────────────────────────────────────
    areas = card.get("impacted_areas") or []
    if areas:
        f_area_label = _load_font(19, bold=True)
        draw.text((x_left, y), "영향:", font=f_area_label, fill=COLOR_MUTED)
        ax = x_left + draw.textlength("영향:", font=f_area_label) + 10
        f_area = _load_font(17, bold=True)
        for a in areas[:4]:
            ax = _draw_pill(draw, ax, y - 2, a, f_area,
                            (226, 232, 240), text_color=COLOR_HEADER) + 6
        y += 34

    # ── 8) 쿠콘 액션 박스 ─────────────────────────────────────────────────
    action = _clean(card.get("coocon_action", ""))
    if action:
        # URL footer(36px) 위로 충분히 떨어진 위치에 배치
        max_box_bottom = CARD_H - 60
        box_h = 90
        if y + box_h > max_box_bottom:
            box_h = max(60, max_box_bottom - y)
        box_y = y
        draw.rounded_rectangle(
            [x_left, box_y, CARD_W - x_left, box_y + box_h],
            radius=12, fill=COLOR_ACTION_BG, outline=COLOR_ACTION_BORDER, width=2,
        )
        f_act_label = _load_font(19, bold=True)
        draw.text((x_left + 18, box_y + 10), "▶ 쿠콘 액션",
                  font=f_act_label, fill=COLOR_HEADER)
        f_act = _load_font(19)
        wrapped = _wrap_text(draw, action, f_act, body_w - 40)
        for i, ln in enumerate(wrapped[:2]):
            draw.text((x_left + 18, box_y + 40 + i * 26),
                      ln, font=f_act, fill=COLOR_TEXT)

    # ── 7) 원문 URL footer (카드 최하단 고정) ──────────────────────────────
    link = (card.get("link") or "").strip()
    if link:
        f_url_label = _load_font(15, bold=True)
        f_url = _load_font(15)
        footer_y = CARD_H - 36   # 하단에서 36px 위
        # 상단 디바이더
        draw.line([(x_left, footer_y - 12), (CARD_W - x_left, footer_y - 12)],
                  fill=COLOR_DIVIDER, width=1)
        label = "원문 ▷ "
        draw.text((x_left, footer_y), label, font=f_url_label, fill=COLOR_HEADER)
        label_w = draw.textlength(label, font=f_url_label)
        url_text = _fit_url(draw, link, f_url, body_w - int(label_w) - 10)
        draw.text((x_left + label_w, footer_y), url_text,
                  font=f_url, fill=COLOR_MUTED)

    # 저장
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), format="PNG", optimize=True)
    return out_path


# ── CLI 테스트 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample = {
        "title":         "[단독] 1주일에 수십억 자금 세탁…'피싱 돈줄' 된 서울 도심",
        "agency":        "한국경제",
        "date":          "2026-05-10",
        "policy_name":   "특정금융정보법 시행령 일부개정안",
        "summary":       "1000만원 이상 가상자산 거래 의심거래 자동 분류 의무화",
        "key_changes": [
            "8월 20일 개정 특금법 시행",
            "트래블룰 100만원 미만 거래까지 확대 적용",
            "VASP 보고 의무 강화 및 STR 자동 분류 신설",
        ],
        "future_plan":   "하위 규정 정비 완료 후 8월 본격 시행 예정",
        "risk_grade":    "상",
        "risk_type":     "규제",
        "impacted_areas": ["AML/KYC", "가상자산"],
        "coocon_action": "AML/KYC 솔루션 STR 자동 탐지 로직 고도화 및 VASP 고객사 안내 필요",
        "link":          "https://www.hankyung.com/article/2026051066731",
    }
    out = render_card(sample, Path("output/test_card.png"), page_no=1, total=3)
    print(f"카드 저장: {out}")
