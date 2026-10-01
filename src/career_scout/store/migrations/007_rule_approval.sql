-- Rule-based approval (I-30).
--
-- An approval was always 'human'. The user may now switch on a rule set that
-- approves an application package for them. A rule approval is never anonymous:
-- it must carry the rules and the inputs that satisfied them, so it can be
-- checked by hand afterwards. Everything the send gate re-checks is unchanged.
--
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. Nothing
-- references approval by foreign key, and no trigger or view names it.

CREATE TABLE approval_new (
    id                    TEXT PRIMARY KEY,
    package_id            TEXT NOT NULL REFERENCES application_package(id) ON DELETE CASCADE,
    approved_by           TEXT NOT NULL DEFAULT 'human' CHECK (approved_by IN ('human', 'rule')),
    approved_at           TEXT NOT NULL,
    content_hash          TEXT NOT NULL,
    rendered_hash         TEXT NOT NULL,
    destination_hash      TEXT NOT NULL,
    destination_snapshot  TEXT NOT NULL,
    mailbox_credential_id TEXT NOT NULL,
    consumed_at           TEXT,
    rule_basis            TEXT,              -- JSON: the rules and inputs, for 'rule' only
    CHECK ((approved_by = 'human') = (rule_basis IS NULL))
);

INSERT INTO approval_new (id, package_id, approved_by, approved_at, content_hash, rendered_hash,
                          destination_hash, destination_snapshot, mailbox_credential_id,
                          consumed_at)
SELECT id, package_id, approved_by, approved_at, content_hash, rendered_hash,
       destination_hash, destination_snapshot, mailbox_credential_id, consumed_at
FROM approval;

DROP TABLE approval;
ALTER TABLE approval_new RENAME TO approval;
CREATE UNIQUE INDEX approval_package ON approval (package_id);
