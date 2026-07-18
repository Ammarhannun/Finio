-- Finio — multiple coach chats.
-- Run once in the Supabase SQL editor. Existing messages land in the
-- 'default' chat; new chats get their own id.

alter table chat_history
    add column if not exists chat_id text not null default 'default';

create index if not exists chat_history_user_chat_idx
    on chat_history (user_id, chat_id, timestamp);
