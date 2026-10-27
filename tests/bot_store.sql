-- Bot store schema v4, copied here to keep router tests independent of the bot package.

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  type TEXT,
  name TEXT,
  service_url TEXT NOT NULL,
  tenant_id TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  conversation_id TEXT NOT NULL,
  id TEXT NOT NULL,
  ts TEXT,
  sender_aad_id TEXT,
  sender_name TEXT,
  text TEXT NOT NULL,
  mentioned INTEGER NOT NULL,
  reply_to_id TEXT,
  received_at TEXT NOT NULL,
  raw_html TEXT,
  attachments_json TEXT NOT NULL DEFAULT '[]',
  quoted_json TEXT NOT NULL DEFAULT '[]',
  edited_at TEXT,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT,
  restored_at TEXT,
  modified_at TEXT,
  sender_app_id TEXT,
  live_seen_at TEXT,
  graph_seen_at TEXT,
  first_source TEXT NOT NULL DEFAULT 'live',
  change_seq INTEGER NOT NULL DEFAULT 0,
  mentions_json TEXT,
  PRIMARY KEY (conversation_id, id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('store_generation',lower(hex(randomblob(16))));
INSERT INTO meta VALUES ('change_sequence','0');
-- Simulate a writer publishing changes; actual bot migration and merge behavior
-- is tested against toucan_bot.store in the bot suite.
CREATE TRIGGER message_insert AFTER INSERT ON messages BEGIN
  UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='change_sequence';
  UPDATE messages SET change_seq=CAST((SELECT value FROM meta WHERE key='change_sequence') AS INTEGER)
    WHERE conversation_id=NEW.conversation_id AND (id=NEW.id OR reply_to_id=NEW.id);
END;
CREATE TRIGGER message_update AFTER UPDATE OF ts,sender_aad_id,sender_app_id,sender_name,text,
    raw_html,mentioned,reply_to_id,attachments_json,quoted_json,edited_at,deleted,deleted_at,
    restored_at,modified_at,mentions_json ON messages BEGIN
  UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='change_sequence';
  UPDATE messages SET change_seq=CAST((SELECT value FROM meta WHERE key='change_sequence') AS INTEGER)
    WHERE conversation_id=NEW.conversation_id AND (id=NEW.id OR reply_to_id=NEW.id);
END;
CREATE TABLE IF NOT EXISTS members (
  conversation_id TEXT NOT NULL,
  id TEXT NOT NULL,
  aad_id TEXT,
  name TEXT,
  PRIMARY KEY (conversation_id, id)
);
CREATE TABLE IF NOT EXISTS sent (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  activity_id TEXT,
  text TEXT NOT NULL,
  sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catchup (
  conversation_id TEXT PRIMARY KEY, cursor TEXT, last_success TEXT, last_error TEXT, http_status INTEGER,
  next_url TEXT, upper_bound TEXT, retry_at REAL, last_request_at REAL,
  auth_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_media (
  conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, source TEXT NOT NULL,
  url TEXT NOT NULL, attachment_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
  claim_id TEXT, claim_until REAL,
  PRIMARY KEY (conversation_id,message_id,source,url)
);
