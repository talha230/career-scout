-- Mail tracking, and one send gate for every kind of message — T056, T061 to T065, T084.
--
-- An application, an auto-reply and an outreach message are all sends, and no
-- send skips the gate. Rather than a second path for the other two, packages and
-- the reservation row carry a `kind`, and career_scout/send re-derives each kind's
-- current destination from its own source row: the posting's apply address, the
-- reply's sender, or the contact's published address (checked against the
-- suppression list). `application` is therefore the table of every reserved
-- send, and the /applications screen filters it to kind = 'application'.

ALTER TABLE application_package ADD COLUMN kind TEXT NOT NULL DEFAULT 'application'
    CHECK (kind IN ('application', 'reply', 'outreach'));
ALTER TABLE application_package ADD COLUMN reply_id TEXT REFERENCES reply(id);
ALTER TABLE application_package ADD COLUMN contact_id TEXT REFERENCES contact(id);

ALTER TABLE application ADD COLUMN kind TEXT NOT NULL DEFAULT 'application'
    CHECK (kind IN ('application', 'reply', 'outreach'));
-- Errors from the steps after a successful send (label, Drive record). The send
-- happened; these are recorded, never retried as a send.
ALTER TABLE application ADD COLUMN followup_errors TEXT;

-- What the poller keeps about a message. Only messages that belong to an
-- application's thread, or come from an application's employer domain, are ever
-- stored — the rest of the user's mail is never read into Jarvis.
ALTER TABLE reply ADD COLUMN body_text TEXT;
ALTER TABLE reply ADD COLUMN link_basis TEXT;          -- 'thread' | 'sender_domain' | NULL
ALTER TABLE reply ADD COLUMN classification_detail TEXT; -- JSON: matched rules and scores

CREATE TABLE mailbox_state (
    credential_id  TEXT PRIMARY KEY REFERENCES credential(id),
    history_id     TEXT,
    last_polled_at TEXT,
    last_status    TEXT CHECK (last_status IN ('ok', 'disconnected', 'error')),
    last_error     TEXT
);
