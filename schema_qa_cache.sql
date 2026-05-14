-- AI 자연어 검색 응답 캐시 (유권해석/판례 메뉴)
-- Supabase SQL Editor 에서 실행 (1회).

CREATE TABLE IF NOT EXISTS precedent_qa_cache (
    cache_key    TEXT        PRIMARY KEY,         -- sha1(norm_q + ":" + sorted(law_ids))
    question     TEXT        NOT NULL,            -- 원본 질문
    law_ids      TEXT,                            -- 선택 법령 쉼표 join
    answer       TEXT,                            -- LLM 응답 본문
    citations    JSONB,                           -- [{idx, precedent_id, title, agency, decided_at, link, summary, body}]
    hit_count    INTEGER     DEFAULT 1,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE precedent_qa_cache DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_qa_cache_created ON precedent_qa_cache(created_at DESC);
