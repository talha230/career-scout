-- Recording WHY two postings were judged the same vacancy — T030b.
--
-- `opportunity` already carries is_canonical and canonical_id, which say THAT a
-- merge happened. They do not say which rule fired or how close the match was,
-- and a merge is a judgement that can be wrong: it hides a real vacancy behind
-- another one. So every merge writes a row here naming the rule, its similarity
-- and the text that justified it, and a merge can then be inspected and argued
-- with rather than taken on trust.
--
-- Nothing is deleted. Reversing a merge is setting is_canonical back to 1 and
-- clearing canonical_id; the evidence row stays, so the reversal is auditable too.

CREATE TABLE opportunity_duplicate (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id     TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    duplicate_id     TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,

    -- Which of the three rules in jarvis/discovery/dedupe.py fired.
    rule             TEXT NOT NULL CHECK (rule IN (
                         'identical_dedupe_key',
                         'identical_url',
                         'employer_title_place_similarity'
                     )),
    -- 1.0 for the two exact rules; the measured title ratio for the fuzzy one.
    similarity       REAL NOT NULL CHECK (similarity BETWEEN 0.0 AND 1.0),
    evidence         TEXT NOT NULL,

    -- The config version the similarity threshold came from, so an old merge
    -- still explains itself after the threshold is retuned.
    config_version   TEXT,
    run_id           TEXT,
    merged_at        TEXT NOT NULL,

    UNIQUE (canonical_id, duplicate_id)
);

CREATE INDEX opportunity_duplicate_canonical ON opportunity_duplicate (canonical_id);
CREATE INDEX opportunity_duplicate_duplicate ON opportunity_duplicate (duplicate_id);

-- Two columns dedupe needs that 001 did not carry.
--
-- url_canonical is the apply URL with campaign tracking stripped and every
-- identifying parameter kept. It is dedupe rule 2, and it is indexed because the
-- alternative — comparing the candidate against every stored URL — is quadratic
-- and does not finish at real feed sizes. It is NOT compared fuzzily: two
-- unrelated postings on one board differ only in a numeric id and score 0.92-0.97
-- against each other, above any usable cut-off.
ALTER TABLE opportunity ADD COLUMN url_canonical TEXT;

-- city distinguishes two offices of one employer in one country, which is the
-- difference between a duplicate and a second genuine vacancy. It is also what
-- the savings engine will need for cost of living: a country is not a rent.
-- Unresolved stays NULL — never the country's largest city, never a guess.
ALTER TABLE opportunity ADD COLUMN city TEXT;

CREATE INDEX opportunity_url ON opportunity (url_canonical)
    WHERE url_canonical IS NOT NULL;
