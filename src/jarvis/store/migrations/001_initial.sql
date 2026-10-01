-- Jarvis initial schema.
--
-- One SQLite file per user, on that user's own machine. There is no tenant
-- column anywhere because there is no second tenant: isolation is the
-- filesystem, which is a stronger boundary than any row-level policy.
--
-- Conventions:
--   * every sourced value carries source_url + fetched_at, both NOT NULL
--   * estimates live in their own column, never mixed with disclosed values
--   * nothing sourced is UPDATEd; rows are superseded
--   * timestamps are ISO-8601 UTC strings ('YYYY-MM-DDTHH:MM:SSZ')

-- ---------------------------------------------------------------- reference

CREATE TABLE country (
    iso2                  TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    default_savings_floor REAL,              -- NULL = not yet sourced; never silently 2000
    floor_basis           TEXT,              -- required whenever default_savings_floor is set
    floor_source_url      TEXT,
    floor_as_of           TEXT,
    currency              TEXT,
    CHECK (default_savings_floor IS NULL OR
           (floor_basis IS NOT NULL AND floor_source_url IS NOT NULL AND floor_as_of IS NOT NULL))
);

CREATE TABLE source (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    provider     TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    country_iso2 TEXT REFERENCES country(iso2),
    -- 'api' and 'feed' may be fetched; 'manual_review_only' may never be.
    access_mode  TEXT NOT NULL CHECK (access_mode IN ('api', 'feed', 'manual_review_only')),
    legal_basis  TEXT NOT NULL,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 20,
    high_water_mark TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

CREATE TABLE opportunity (
    id                     TEXT PRIMARY KEY,
    kind                   TEXT NOT NULL CHECK (kind IN ('job', 'scholarship')),
    country_iso2           TEXT REFERENCES country(iso2),
    role_family            TEXT NOT NULL DEFAULT 'unclassified',
    title                  TEXT NOT NULL,
    employer               TEXT NOT NULL,
    work_arrangement       TEXT CHECK (work_arrangement IN ('remote', 'onsite', 'project')),

    -- Parsed once, at ingest. No later step reads description.
    requirements           TEXT,             -- JSON
    -- 0.0-1.0. Below the configured floor the sufficiency verdict is
    -- 'not_evaluated', which does NOT authorise generation. An unparseable
    -- posting must fail closed: measured evidence shows 481/2305 legacy
    -- assessments found no vocabulary match at all.
    requirements_confidence REAL NOT NULL DEFAULT 0.0
        CHECK (requirements_confidence BETWEEN 0.0 AND 1.0),

    description            TEXT,
    description_format     TEXT,
    raw_payload            TEXT,             -- JSON as returned by the source
    snapshot_path          TEXT,

    apply_route            TEXT NOT NULL DEFAULT 'manual'
        CHECK (apply_route IN ('email', 'published_api', 'portal', 'manual')),
    apply_target           TEXT,
    deadline               TEXT,

    pay_disclosed          TEXT,             -- JSON; present only if the employer published it
    pay_estimate           TEXT,             -- JSON with basis, source_url, as_of
    funding_disclosed      TEXT,             -- JSON; scholarships

    source_id              TEXT REFERENCES source(id),
    source_url             TEXT NOT NULL,
    fetched_at             TEXT NOT NULL,
    posted_at              TEXT,

    dedupe_key             TEXT NOT NULL,
    employer_norm          TEXT NOT NULL,
    title_norm             TEXT NOT NULL,
    is_canonical           INTEGER NOT NULL DEFAULT 1 CHECK (is_canonical IN (0, 1)),
    canonical_id           TEXT REFERENCES opportunity(id),

    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    expired_at             TEXT
);

CREATE UNIQUE INDEX opportunity_dedupe ON opportunity (dedupe_key);
CREATE INDEX opportunity_scope   ON opportunity (country_iso2, kind, expired_at);
CREATE INDEX opportunity_dupcheck ON opportunity (employer_norm, title_norm);
CREATE INDEX opportunity_family  ON opportunity (role_family);

-- Every source a deduplicated posting appeared in.
CREATE TABLE opportunity_source (
    opportunity_id TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    source_id      TEXT NOT NULL REFERENCES source(id),
    source_url     TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, source_id)
);

-- Every sourced number: tax, rent, food, transport, wage stats, visa rules.
-- Superseded, never updated, so an old explanation still resolves.
CREATE TABLE reference_figure (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'tax_rate', 'rent', 'utilities', 'food', 'transport',
                      'health_insurance', 'flight_home', 'wage_stat', 'visa_rule')),
    country_iso2  TEXT REFERENCES country(iso2),
    city          TEXT,
    occupation    TEXT,
    value         REAL NOT NULL,
    unit          TEXT NOT NULL,
    currency      TEXT,
    source_url    TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    quote         TEXT NOT NULL,             -- must appear verbatim in snapshot_path
    snapshot_path TEXT NOT NULL,
    superseded_by TEXT REFERENCES reference_figure(id)
);

CREATE INDEX reference_current ON reference_figure (kind, country_iso2, city)
    WHERE superseded_by IS NULL;

-- A lookup with no row raises. There is deliberately no nearest-date fallback.
CREATE TABLE fx_rate (
    id         TEXT PRIMARY KEY,
    base       TEXT NOT NULL,
    quote      TEXT NOT NULL,
    rate       REAL NOT NULL,
    as_of      TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE UNIQUE INDEX fx_unique ON fx_rate (base, quote, as_of);

-- ------------------------------------------------------------------ profile

CREATE TABLE document (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    restricted      INTEGER NOT NULL DEFAULT 0 CHECK (restricted IN (0, 1)),
    -- 0 with a note where there is no text layer. A scanned degree certificate
    -- must be reported as un-extracted, never as an empty extraction.
    extracted_ok    INTEGER NOT NULL DEFAULT 0 CHECK (extracted_ok IN (0, 1)),
    extraction_note TEXT,
    page_count      INTEGER,
    uploaded_at     TEXT NOT NULL
);

CREATE TABLE profile_record (
    id            TEXT PRIMARY KEY,
    field_path    TEXT NOT NULL,
    value         TEXT,
    value_type    TEXT NOT NULL DEFAULT 'string',
    -- NULL document_id means the user typed it. Typed and document-read values
    -- stay distinguishable everywhere either is shown; a typed value is never
    -- recorded as extracted.
    document_id   TEXT REFERENCES document(id),
    locator       TEXT,                      -- JSON {page, start, end, snippet}
    confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    confirmed_at  TEXT,
    superseded_by TEXT REFERENCES profile_record(id),
    created_at    TEXT NOT NULL,
    CHECK (document_id IS NOT NULL OR locator IS NULL)
);

CREATE INDEX profile_current ON profile_record (field_path) WHERE superseded_by IS NULL;

CREATE TABLE worked_example (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    situation   TEXT NOT NULL,
    task        TEXT NOT NULL,
    action      TEXT NOT NULL,
    result      TEXT NOT NULL,
    skills      TEXT,                        -- JSON array
    record_ids  TEXT,                        -- JSON array of profile_record ids
    created_at  TEXT NOT NULL
);

-- Thresholds, channels, caps. Never a constant in code.
CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                -- JSON
    updated_at TEXT NOT NULL
);

-- -------------------------------------------------------------- assessment

CREATE TABLE assessment (
    opportunity_id      TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    config_version      TEXT NOT NULL,
    match_score         REAL,
    confidence          TEXT CHECK (confidence IN ('low', 'medium', 'high')),
    formula             TEXT NOT NULL,
    weights             TEXT NOT NULL,       -- JSON
    inputs              TEXT NOT NULL,       -- JSON
    unscored_components TEXT NOT NULL DEFAULT '[]',  -- JSON: component + reason + renormalisation
    projection          TEXT,                -- JSON lines, each with reference_figure_id + fx_rate_id
    passes_floor        INTEGER CHECK (passes_floor IN (0, 1)),
    floor_applied       REAL,
    floor_source        TEXT,                -- 'user_override' | 'country_default' | 'fallback'
    eligibility_verdict TEXT CHECK (eligibility_verdict IN ('eligible', 'ineligible', 'unscored')),
    eligibility_detail  TEXT,
    pay_basis           TEXT CHECK (pay_basis IN ('disclosed', 'estimate')),
    sufficiency         TEXT NOT NULL DEFAULT 'not_evaluated'
        CHECK (sufficiency IN ('sufficient', 'insufficient', 'not_evaluated')),
    run_id              TEXT,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, config_version)
);

CREATE INDEX assessment_shortlist ON assessment (passes_floor, match_score DESC);

CREATE TABLE rejection (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    rule           TEXT NOT NULL,
    reason         TEXT NOT NULL,
    evidence       TEXT,
    run_id         TEXT,
    rejected_at    TEXT NOT NULL
);

CREATE INDEX rejection_rule ON rejection (rule);

-- ------------------------------------------------------------- sufficiency

CREATE TABLE sufficiency_rule (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version              INTEGER NOT NULL,
    document_type        TEXT NOT NULL CHECK (document_type IN (
                             'cv', 'cover_letter', 'motivation_letter',
                             'statement_of_purpose', 'research_statement')),
    role_family          TEXT,               -- NULL applies to every family
    kind                 TEXT CHECK (kind IN ('job', 'scholarship')),
    required_field_paths TEXT NOT NULL,      -- JSON array
    conditional_on       TEXT,               -- JSON predicate over opportunity.requirements
    label                TEXT NOT NULL,
    rationale            TEXT NOT NULL
);

CREATE INDEX sufficiency_lookup ON sufficiency_rule (version, document_type, role_family);

CREATE TABLE sufficiency_verdict (
    opportunity_id      TEXT PRIMARY KEY REFERENCES opportunity(id) ON DELETE CASCADE,
    verdict             TEXT NOT NULL
        CHECK (verdict IN ('sufficient', 'insufficient', 'not_evaluated')),
    reason              TEXT,                -- why not_evaluated, e.g. low requirements_confidence
    rules_applied       TEXT NOT NULL,       -- JSON array of sufficiency_rule ids
    fields_examined     TEXT NOT NULL,       -- JSON array; makes the verdict hand-reproducible
    missing_field_paths TEXT NOT NULL DEFAULT '[]',
    rule_version        INTEGER NOT NULL,
    computed_at         TEXT NOT NULL
);

-- ------------------------------------------------------------- application

CREATE TABLE application_package (
    id              TEXT PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunity(id) ON DELETE CASCADE,
    documents       TEXT NOT NULL,           -- JSON [{type, path, sha256}]
    claim_trace     TEXT NOT NULL,           -- JSON {sentence -> profile_record.id}
    -- Hash of the rendered bytes actually attached. Rendering happens once,
    -- here; nothing is re-rendered at send time.
    rendered_sha256 TEXT NOT NULL,
    qc_verdict      TEXT NOT NULL CHECK (qc_verdict IN ('pass', 'blocked')),
    blocked_reason  TEXT,
    omissions       TEXT,                    -- JSON: unconfirmed values deliberately excluded
    state           TEXT NOT NULL DEFAULT 'generated' CHECK (state IN (
                        'generated', 'blocked', 'awaiting_approval', 'approved',
                        'submitted', 'awaiting_user_submission')),
    generated_at    TEXT NOT NULL,
    CHECK (qc_verdict = 'pass' OR blocked_reason IS NOT NULL)
);

-- An approval binds five things, not two.
CREATE TABLE approval (
    id                    TEXT PRIMARY KEY,
    package_id            TEXT NOT NULL REFERENCES application_package(id) ON DELETE CASCADE,
    approved_by           TEXT NOT NULL DEFAULT 'human' CHECK (approved_by = 'human'),
    approved_at           TEXT NOT NULL,
    content_hash          TEXT NOT NULL,     -- the package contents
    rendered_hash         TEXT NOT NULL,     -- the bytes that will be attached
    destination_hash      TEXT NOT NULL,     -- recipient + route + opportunity id
    destination_snapshot  TEXT NOT NULL,     -- JSON, as it stood at approval
    -- Which mailbox it leaves from. Reconnecting a different Google account
    -- between approval and send changes the sender identity the recipient
    -- sees, and is refused.
    mailbox_credential_id TEXT NOT NULL,
    consumed_at           TEXT               -- claimed by conditional UPDATE; single-send guard
);

CREATE UNIQUE INDEX approval_package ON approval (package_id);

CREATE TABLE application (
    id                  TEXT PRIMARY KEY,
    package_id          TEXT NOT NULL REFERENCES application_package(id),
    opportunity_id      TEXT NOT NULL REFERENCES opportunity(id),
    channel             TEXT NOT NULL CHECK (channel IN ('email', 'published_api', 'manual')),
    status              TEXT NOT NULL CHECK (status IN (
                            'sending', 'submitted', 'acknowledged', 'rejected',
                            'interview', 'offer', 'info_requested', 'withdrawn')),
    -- Written in state 'sending' BEFORE the provider is called. Gmail offers no
    -- idempotency key, so a send that succeeds while its follow-up write fails
    -- must be recoverable by reconciliation, never by retrying the send.
    sent_at             TEXT,
    provider_message_id TEXT,
    gmail_thread_id     TEXT,
    gmail_label         TEXT,
    drive_record_url    TEXT,
    destination         TEXT NOT NULL,
    submission_evidence TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX application_sent ON application (sent_at DESC) WHERE sent_at IS NOT NULL;
CREATE UNIQUE INDEX application_provider_msg ON application (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE TABLE application_status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    actor          TEXT NOT NULL,            -- 'human' | 'run:<id>' | 'gmail_poll'
    note           TEXT,
    changed_at     TEXT NOT NULL
);

CREATE TABLE reply (
    id                        TEXT PRIMARY KEY,
    -- Nullable on purpose: a reply that cannot be matched to an application is
    -- retained, not discarded.
    application_id            TEXT REFERENCES application(id) ON DELETE SET NULL,
    gmail_message_id          TEXT NOT NULL UNIQUE,
    gmail_thread_id           TEXT NOT NULL,
    from_address              TEXT NOT NULL,
    subject                   TEXT,
    received_at               TEXT NOT NULL,
    classification            TEXT CHECK (classification IN (
                                  'acknowledgement', 'rejection', 'interview_invite',
                                  'offer', 'information_request', 'unclassified')),
    classification_confidence REAL,
    classification_corrected_to TEXT,
    corrected_at              TEXT
);

-- ---------------------------------------------------------------- outreach

CREATE TABLE contact (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    role                 TEXT,
    institution          TEXT,
    email                TEXT NOT NULL,
    -- NOT NULL makes "only published contact details" structural rather than a
    -- policy someone has to remember. An address with no published page cannot
    -- be stored at all.
    published_source_url TEXT NOT NULL,
    published_read_at    TEXT NOT NULL,
    country_iso2         TEXT REFERENCES country(iso2),
    do_not_contact       INTEGER NOT NULL DEFAULT 0 CHECK (do_not_contact IN (0, 1)),
    notice_sent_at       TEXT,
    erased_at            TEXT,
    created_at           TEXT NOT NULL
);

-- Outlives any contact row, and any reinstall. Holds a hash, not an address,
-- so honouring a suppression costs less data about that person than a contact
-- row would.
CREATE TABLE contact_suppression (
    email_sha256 TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('do_not_contact', 'erasure')),
    requested_at TEXT NOT NULL
);

CREATE TABLE outreach_message (
    id             TEXT PRIMARY KEY,
    contact_id     TEXT NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
    opportunity_id TEXT REFERENCES opportunity(id),
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'generated' CHECK (state IN (
                       'generated', 'awaiting_approval', 'approved', 'sent')),
    notice_included INTEGER NOT NULL DEFAULT 0 CHECK (notice_included IN (0, 1)),
    sent_at        TEXT,
    provider_message_id TEXT,
    created_at     TEXT NOT NULL
);

-- ------------------------------------------------------------- credentials

CREATE TABLE credential (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,             -- 'google' | 'anthropic' | 'openai'
    account_label TEXT,                      -- the Gmail address, for display
    scopes        TEXT NOT NULL,             -- JSON array
    token_path    TEXT,                      -- credentials/*.json, chmod 600
    connected_at  TEXT NOT NULL,
    revoked_at    TEXT,
    needs_renewal INTEGER NOT NULL DEFAULT 0 CHECK (needs_renewal IN (0, 1))
);

-- ------------------------------------------------------------- operations

CREATE TABLE run (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,               -- 'full' | 'discover' | 'gmail' | 'backup'
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    counts      TEXT,                        -- JSON
    obstacles   TEXT,                        -- JSON; every agent returns one, empty or not
    error       TEXT
);

CREATE INDEX run_recent ON run (kind, started_at DESC);

-- One row per message that left the machine. Backs the rolling-window cap
-- count, and survives an application being deleted.
CREATE TABLE send_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at             TEXT NOT NULL,
    destination_country TEXT,
    kind                TEXT NOT NULL CHECK (kind IN ('application', 'outreach', 'reply')),
    reference_id        TEXT NOT NULL
);

CREATE INDEX send_log_window ON send_log (sent_at DESC);

CREATE TABLE backup_record (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('daily', 'weekly', 'manual')),
    taken_at      TEXT NOT NULL,
    path          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    error         TEXT,
    -- A backup that has never been restored is not a backup.
    restored_at   TEXT,
    restore_result TEXT
);

CREATE TABLE schema_migration (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
