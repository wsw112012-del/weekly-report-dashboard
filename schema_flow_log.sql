-- Flow 게시 로그 — 같은 날 중복 발송 방지
-- Supabase SQL Editor 에서 실행 (1회).

CREATE TABLE IF NOT EXISTS flow_post_log (
    post_date    DATE        PRIMARY KEY,    -- 게시일 (KST 기준 yyyy-mm-dd)
    bot_id       TEXT,                       -- 게시 봇 식별자
    project_id   TEXT,                       -- 게시 프로젝트 ID
    post_id      TEXT,                       -- Flow 응답 postId
    tiny_url     TEXT,                       -- Flow 응답 tinyUrl
    total_items  INTEGER,                    -- 게시된 항목 수
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE flow_post_log DISABLE ROW LEVEL SECURITY;
