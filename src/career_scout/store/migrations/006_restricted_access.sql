-- The audit trail for the one place a restricted document is decrypted — T014.
--
-- Every opening is written here before the plaintext is returned. A row with a
-- purpose other than the user opening their own file would be a defect.

CREATE TABLE restricted_access (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES document(id),
    opened_at   TEXT NOT NULL,
    purpose     TEXT NOT NULL
);
