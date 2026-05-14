# AML 일일 리스크 다이제스트 — 설정 가이드

매일 09:00 KST에 신규 AML 보도자료·언론기사를 Gemini로 리스크 평가한 뒤
카드뉴스 형태로 마드라스체크 Flow 채널에 자동 발송한다.

## 구성 요소

| 파일 | 역할 |
|------|------|
| `risk_analyze.py` | 기사 1건 → Gemini로 규제·평판 리스크 평가 (JSON) |
| `flow_card.py` | 평가 결과 → 1200×800 PNG 카드 이미지 |
| `notify_flow.py` | 오케스트레이터 (신규 추출 → 평가 → 카드 생성 → Flow 발송) |
| `.github/workflows/auto_collect.yml` | 매일 09:00 KST 자동 실행 |

## 1. Supabase 테이블 생성 (1회)

Supabase SQL 에디터에서 실행:

```sql
CREATE TABLE IF NOT EXISTS aml_digest_sent (
    url        text PRIMARY KEY,
    sent_at    timestamptz DEFAULT now(),
    risk_grade text,
    title      text
);
```

발송된 기사 url을 기록해 다음날 중복 발송을 방지한다.

## 2. 환경변수 설정

### 로컬 (.env)

`.env` 파일에 추가:

```
FLOW_API_TOKEN=<Flow 워크스페이스 API 토큰>
FLOW_CHANNEL_ID=<발송할 채널 ID>
FLOW_BASE_URL=https://flow.team
# AML_DIGEST_MAX_CARDS=10   # 한 번에 발송할 카드 최대 개수 (기본 10)
# FLOW_API_BASE_PATH=/api/v3  # Flow API 경로 (기본 /api/v3)
```

`SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`, `NAVER_CLIENT_*` 는
기존에 이미 설정돼 있음.

### GitHub Actions Secrets

GitHub 저장소 Settings → Secrets and variables → Actions 에서 추가:

- `FLOW_API_TOKEN`
- `FLOW_CHANNEL_ID`
- `FLOW_BASE_URL` (자체호스팅 시에만, 기본값으로 두려면 생략 가능)

기존 secrets (`SUPABASE_*`, `GEMINI_API_KEY`, `NAVER_*`) 는 그대로 활용.

## 3. 동작 흐름

```
매일 09:00 KST (GitHub Actions cron: 0 0 * * *)
  ↓
1. collect_보도자료.py {데이터,페이먼트,AML} 실행 (기존 step)
  ↓
2. collect_입법현황.py 실행 (기존 step)
  ↓
3. validate_data.py 실행 (기존 step)
  ↓
4. notify_flow.py 실행 (신규 step) ← 본 기능
   a. Supabase articles(type=AML) 로드
   b. aml_digest_sent와 차집합 → 신규 url만 추출
   c. 최근 3일 이내 기사만 (오래된 기사 노이즈 차단)
   d. 각 기사 → risk_analyze.analyze_article() (Gemini)
   e. risk_grade ∈ {"상","중"} 만 통과, 상 우선 정렬
   f. 상위 AML_DIGEST_MAX_CARDS 개만 카드화
   g. PNG 카드 → output/aml_digest_YYYYMMDD/
   h. Flow API POST: 카드 N장 multipart + 본문(목차) 1개 포스트
   i. 성공 시 aml_digest_sent에 url 기록
```

**새 뉴스가 0건일 때**: 발송 생략 (조용)

## 4. 수동 실행·검증

### 카드만 미리보기 (Flow 전송 안 함)

```bash
python notify_flow.py --dry-run
```

→ `output/aml_digest_YYYYMMDD/card_NN.png` 생성, 콘솔에 발송 예정 내용 출력.

### 실제 발송 (테스트 채널에서)

```bash
python notify_flow.py
```

### 단위 테스트

```bash
python risk_analyze.py   # 샘플 기사 1건 평가
python flow_card.py      # 샘플 카드 1장 생성 → output/test_card.png
```

## 5. 운영 팁

### 카드 수 조정

기본 10장. 노이즈가 많거나 적으면 환경변수로 조정:

```
AML_DIGEST_MAX_CARDS=5   # 더 엄선
AML_DIGEST_MAX_CARDS=20  # 더 폭넓게
```

### 발송 이력 초기화

특정 기사 재발송이 필요하면 Supabase에서 row 삭제:

```sql
DELETE FROM aml_digest_sent WHERE url = '<재발송할 url>';
```

전체 초기화:

```sql
TRUNCATE aml_digest_sent;
```

### Flow API 호환성

Flow Open API 엔드포인트·필드명은 워크스페이스 버전에 따라 다를 수 있다.
`notify_flow.py:post_to_flow()` 가 두 단계로 시도:

1. `POST {FLOW_BASE_URL}/api/v3/posts` (multipart, 카드 첨부 + 본문)
2. 실패 시 `POST {FLOW_BASE_URL}/api/v3/channels/{ID}/messages` (텍스트만)

워크스페이스가 다른 경로를 쓴다면 `FLOW_API_BASE_PATH` 로 override.
실제 응답이 200/201이 아니면 콘솔에 응답 본문을 출력하니, 처음 운영 시 한 번 확인 권장.

### Gemini 호출량

매일 평가하는 기사 = AML 카테고리 신규분 (보통 5~30건).
gemini-2.0-flash 기준 무료 한도 내에서 충분.
429 응답 시 자동으로 `gemini-flash-latest` 모델로 폴백.

### 롤백

workflow 마지막 step만 주석 처리하면 즉시 중단되며 수집은 계속된다:

```yaml
# - name: AML 일일 리스크 다이제스트 발송 (Flow)
#   ...
```

`aml_digest_sent` 테이블은 유지 → 재개 시 중복 발송 없음.

## 6. 카드 디자인

```
┌─────────────────────────────────────────┐
│ AML 일일 리스크 다이제스트    2026.05.11 · 1/N │  ← 헤더
├─────────────────────────────────────────┤
│ [상] 등급  #규제                          │  ← 등급 배지
│                                          │
│ 기사 제목 (최대 3줄)                       │
│ 출처기관 · 2026-05-10                    │
├─────────────────────────────────────────┤
│ • 핵심 포인트 1                          │  ← LLM 추출
│ • 핵심 포인트 2                          │
│ • 핵심 포인트 3                          │
│ 영향: [AML/KYC] [가상자산]               │  ← 사업영역 배지
│ ┌─────────────────────────────────┐ │
│ │ ▶ 쿠콘 액션                       │ │  ← 권장 조치
│ │ 솔루션 X 수정 필요…                │ │
│ └─────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

폰트: `C:\Windows\Fonts\malgun.ttf` (Windows) / `NanumGothic.ttf` (Ubuntu).
GitHub Actions self-hosted runner가 Windows라면 그대로 동작.
Ubuntu runner 사용 시 `apt-get install fonts-nanum` 또는 NotoSans CJK 설치 필요.
