-- Making a rejection reversible without deleting it.
--
-- Two stages write rejections: authenticity at ingest, hard filters at screening.
-- Each re-assessment reconciles its OWN stage's rows against its latest verdict —
-- a posting that now passes has those rows lifted, not deleted, so "it was
-- rejected under rule X until run Y" stays answerable.
--
-- A rejection is active while lifted_at IS NULL. Before this, an ingest-time
-- rejection could never be revisited: a known dedupe key is a sighting, and a
-- sighting never re-ran the check, so disabling a rule brought nothing back.

ALTER TABLE rejection ADD COLUMN stage TEXT
    CHECK (stage IN ('authenticity', 'hard_filter'));

-- Rule ids are namespaced by stage: F* are hard filters, J*/S* authenticity.
UPDATE rejection
   SET stage = CASE WHEN rule LIKE 'F%' THEN 'hard_filter' ELSE 'authenticity' END;

ALTER TABLE rejection ADD COLUMN lifted_at     TEXT;
ALTER TABLE rejection ADD COLUMN lifted_reason TEXT;
ALTER TABLE rejection ADD COLUMN lifted_run_id TEXT;

-- One active row per rule per posting. Re-screening a still-rejected posting
-- used to append a duplicate row on every run; those repeats are retired, not
-- deleted, pointing at the first row they repeat.
UPDATE rejection
   SET lifted_at = rejected_at,
       lifted_reason = 'repeat of rejection #' || (
           SELECT MIN(r.id) FROM rejection r
            WHERE r.opportunity_id = rejection.opportunity_id
              AND r.stage = rejection.stage AND r.rule = rejection.rule)
 WHERE id NOT IN (SELECT MIN(id) FROM rejection GROUP BY opportunity_id, stage, rule);

CREATE UNIQUE INDEX rejection_active
    ON rejection (opportunity_id, stage, rule) WHERE lifted_at IS NULL;
