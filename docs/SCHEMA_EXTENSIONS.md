# `x_` extensions to JSON Resume

`data/master_cv.json` is JSON Resume v1.0.0 plus five documented extensions. Standard
consumers ignore `x_`-prefixed keys, so the file stays valid JSON Resume.

## `x_achievements`

Quantified accomplishments, kept separate from `work[].highlights` because an achievement
needs provenance that a duty does not. Mandated by the build spec.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Stable identifier, referenced by generated documents |
| `statement` | string | The claim, in the words it would appear in a CV |
| `linked_to` | object | `{type: work\|project, id}` |
| `impact_metric` | object \| null | The measurement. Null when there is none |
| `metric_required` | bool | Whether this claim needs a metric to be usable |
| `verified` | bool | True only with third-party corroboration |
| `verification_gap` | string | Required when `verified` is false: what is missing |
| `source` | object | `{document_id, also_appears_in[], corroborated_by[]}` |
| `date_added` | date | ISO date |
| `usable_in_applications` | bool | False blocks it from every generated document |
| `usage_caveat` | string | How it may and may not be phrased |

Inside `impact_metric`, any `<field>_metric_required: true` marks a slot that is
deliberately empty. The validator surfaces each one as a DECLARED_GAP, and the scorer
treats the achievement as not fully quantified.

**Why `verified` is strict.** A claim repeated across your CV, cover letter and CDR is not
corroborated — all three trace to the same author. Only an employer letter, degree,
transcript, test report or third-party reference sets `verified: true`.

## `x_documents`

The registry every `source` cites. `third_party: true` marks documents you did not write;
only those can support `verified: true`. The Phase 1 validator fails any record that claims
verification while citing only self-authored sources.

## `x_mobility`

Citizenship, passport, English-test status, professional recognition, and experience totals.

`experience_totals` carries **two** figures, deliberately:

- `conservative_years_documented` (3.77) — evidenced by employer letters. Used by the
  scorer and the hard filters.
- `optimistic_years_if_still_employed` (6.62) — assumes continuous employment. Reported
  only, never scored against.

The gap between them is the cost of open question Q1.

## `x_preferences`

Target roles, salary expectation, relocation and remote appetite. `declared_verbatim`
records your instruction word for word, with the interpretation applied to it stated
separately, so a misreading is visible rather than baked in.

## `x_open_questions`

Every gap that needs your answer, with severity, the field it affects, and why it matters.
`severity: BLOCKER` prevents Phase 5 from generating submittable documents under
`--require-ready`. These are questions, not TODOs: each names what changes once answered.
