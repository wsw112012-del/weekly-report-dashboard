-- 유권해석/판례 + 법령 3단비교용 신규 테이블 스키마
-- 사용자가 Supabase 대시보드 → SQL Editor 에서 실행해주세요.
-- (기존 legislation_status 테이블과 동일한 패턴: id PK + 일반 컬럼)

-- =============================================================================
-- 1. precedent_db — 유권해석례·판례 통합
-- =============================================================================
CREATE TABLE IF NOT EXISTS precedent_db (
    id          TEXT        PRIMARY KEY,
    source      TEXT        NOT NULL,                 -- 'expc' (해석례) / 'prec' (판례)
    target_law  TEXT,                                 -- 대상 법령명 (예: '외국환거래법')
    title       TEXT        NOT NULL,                 -- 사건명 또는 안건명
    agency      TEXT,                                 -- 회신기관 (법제처/금감원/금융위/대법원 등)
    case_no     TEXT,                                 -- 사건번호 / 회신번호
    decided_at  TEXT,                                 -- 선고일자 / 회신일자 'YYYY.MM.DD.'
    summary     TEXT,                                 -- 판시사항 / 회신요지
    body        TEXT,                                 -- 판결요지 / 회신내용 전문
    ref_laws    TEXT,                                 -- 관련 조문 (쉼표 구분 텍스트)
    link        TEXT,                                 -- law.go.kr 상세 URL
    scraped_at  DATE        DEFAULT CURRENT_DATE      -- 마지막 수집일
);

CREATE INDEX IF NOT EXISTS idx_precedent_target_law ON precedent_db(target_law);
CREATE INDEX IF NOT EXISTS idx_precedent_source     ON precedent_db(source);
CREATE INDEX IF NOT EXISTS idx_precedent_decided_at ON precedent_db(decided_at DESC);

-- =============================================================================
-- 2. law_articles — 법령 조문별 본문 (3단비교용)
-- =============================================================================
CREATE TABLE IF NOT EXISTS law_articles (
    id              TEXT        PRIMARY KEY,           -- {law_id}-{jo_no}-{hang or 0}
    law_id          TEXT        NOT NULL,              -- 법령 마스터 ID (예: 'fefta')
    law_name        TEXT        NOT NULL,              -- 표시명 ('외국환거래법' / '외국환거래법 시행령' …)
    law_type        TEXT        NOT NULL,              -- 'act' (법률) / 'enforce' (시행령) / 'regulation' (시행규칙·규정)
    parent_law_id   TEXT,                              -- 상위법 law_id (act는 NULL)
    jo_no           INTEGER     NOT NULL,              -- 조문 번호 (3, 4, ...)
    jo_label        TEXT,                              -- 표시 라벨 ('제3조의2' 등)
    jo_title        TEXT,                              -- 조문 제목 ('(목적)')
    body            TEXT,                              -- 조문 본문 전체
    delegation_refs JSONB,                             -- 자동 추출 위임 참조 (예: ['법§5', '규정§3①'])
    order_idx       INTEGER,                           -- 표시 순서 (law_type 별 jo_no 오름차순)
    scraped_at      DATE        DEFAULT CURRENT_DATE
);

CREATE INDEX IF NOT EXISTS idx_law_articles_law_id      ON law_articles(law_id);
CREATE INDEX IF NOT EXISTS idx_law_articles_law_type    ON law_articles(law_id, law_type);
CREATE INDEX IF NOT EXISTS idx_law_articles_jo_no       ON law_articles(law_id, law_type, jo_no);

-- =============================================================================
-- 3. (선택) Row Level Security — 기존 테이블과 동일 정책 (READ ALL)
-- =============================================================================
-- ALTER TABLE precedent_db ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Public read" ON precedent_db FOR SELECT USING (true);
-- ALTER TABLE law_articles ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Public read" ON law_articles FOR SELECT USING (true);

-- =============================================================================
-- 4. Mock 데이터 1건씩 (UI 스켈레톤 검증용 — OC 승인 전 임시)
-- =============================================================================
INSERT INTO precedent_db (id, source, target_law, title, agency, case_no, decided_at, summary, body, ref_laws, link)
VALUES
    ('mock-expc-001', 'expc', '외국환거래법', '외국환거래법상 거주자의 해외증권 취득 신고 의무 범위',
     '법제처', '안건번호 24-0123', '2024.03.15.',
     '거주자가 해외 비상장 주식을 취득하는 경우 외국환거래법 제18조에 따른 신고 의무가 적용되는지에 대한 해석',
     '외국환거래법 제18조 제1항은 거주자가 자본거래를 하려는 경우 기획재정부장관에게 신고하여야 함을 규정하고 있는바, 해외 비상장 주식 취득 역시 자본거래에 해당하므로 신고 대상이 됨…',
     '외국환거래법 제18조, 동법 시행령 제32조',
     'https://www.law.go.kr/LSO/decisionInfoP.do?decisionSeq=MOCK001'),
    ('mock-prec-001', 'prec', '특정 금융거래정보의 보고 및 이용 등에 관한 법률',
     '특금법 위반에 따른 가상자산사업자 신고 의무 위반 사건',
     '서울행정법원', '2024구합12345', '2024.11.20.',
     '특금법 제7조에 따른 가상자산사업자 신고를 하지 않고 영업한 경우 처벌 대상에 해당',
     '특정 금융거래정보의 보고 및 이용 등에 관한 법률(이하 ''특금법'') 제7조는 가상자산사업자가 영업을 시작하기 전에 금융정보분석원장에게 신고하도록 규정하고 있다…',
     '특금법 제7조, 동법 시행령 제10조의11',
     'https://www.law.go.kr/LSW/precInfoP.do?precSeq=MOCK001')
ON CONFLICT (id) DO NOTHING;

INSERT INTO law_articles (id, law_id, law_name, law_type, parent_law_id, jo_no, jo_label, jo_title, body, delegation_refs, order_idx)
VALUES
    ('fefta-act-1', 'fefta', '외국환거래법', 'act', NULL, 1, '제1조', '(목적)',
     '이 법은 외국환거래와 그 밖의 대외거래의 자유를 보장하고 시장기능을 활성화하여 대외거래의 원활화 및 국제수지의 균형과 통화가치의 안정을 도모함으로써 국민경제의 건전한 발전에 이바지함을 목적으로 한다.',
     '[]'::jsonb, 1),
    ('fefta-enforce-1', 'fefta', '외국환거래법 시행령', 'enforce', 'fefta', 1, '제1조', '(목적)',
     '이 영은 「외국환거래법」에서 위임된 사항과 그 시행에 필요한 사항을 규정함을 목적으로 한다.',
     '["법§1"]'::jsonb, 1),
    ('fefta-regulation-1', 'fefta', '외국환거래규정', 'regulation', 'fefta', 1, '제1-1조', '(목적)',
     '이 규정은 「외국환거래법」 및 동법 시행령에서 위임된 사항과 그 시행에 필요한 사항을 정함을 목적으로 한다.',
     '["법§1", "령§1"]'::jsonb, 1)
ON CONFLICT (id) DO NOTHING;
