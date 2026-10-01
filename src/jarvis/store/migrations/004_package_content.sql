-- What a package needs beyond 001 — T044 to T046, T054, T055.
--
-- content_sha256 is the hash a human approves: the documents' lines and the claim
-- trace, canonicalised. It lives on the package so the approval can be checked
-- against what the package actually holds, not against whatever a caller says.
--
-- route decides what approval means. 'email' is sent through the gate in
-- jarvis/send; 'portal' is never sent by Jarvis at all — the user submits the
-- prefilled worksheet on the employer's site and records that they did.

ALTER TABLE application_package ADD COLUMN content_sha256 TEXT;
ALTER TABLE application_package ADD COLUMN route TEXT CHECK (route IN ('email', 'portal'));
-- JSON list of every QC finding, FAIL and WARN, each with the offending line.
ALTER TABLE application_package ADD COLUMN qc_findings TEXT;
-- JSON list of warnings a human must see before approving, e.g. a likely
-- duplicate application to the same employer and title (T055).
ALTER TABLE application_package ADD COLUMN warnings TEXT;
ALTER TABLE application_package ADD COLUMN superseded_by TEXT REFERENCES application_package(id);

CREATE INDEX application_package_opportunity ON application_package (opportunity_id, generated_at DESC);
