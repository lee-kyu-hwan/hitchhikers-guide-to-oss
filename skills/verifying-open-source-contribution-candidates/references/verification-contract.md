# Verification Contract

This document defines the persisted candidate and verification state. All JSON envelopes use `schema_version: "1.0.0"`, a `generated_by` object, and their named arrays. Candidate output is `{schema_version, generated_by, records}`; discovery and manifest inputs remain separate evidence.

Every pre-cap repository-pattern combination appears in exactly one of records, skipped_by_cap, or failed_scopes.
A request-failed policy observation cannot support issue-ready or pr-ready.
A policy result recorded as unreviewed cannot support a ready status.
A ready absence claim must match the observed absence, and an AI or CLA/DCO absence needs hand-reviewed evidence for every found file.
A ready status requires a locus and a completed search repeated at that locus.
A pull request returned for every locus-derived clue makes the candidate duplicate, and an undecided one keeps it out of a ready status.
A reproduced status requires a static or executed reproduction, a reproduction evidence link, and a confirmed reproduction_evidence check.
A failed recheck makes an actionable candidate unverified with status_reason insufficient-evidence.
record and render preserve partial, failed, and unknown run states instead of defaulting them to complete.

## Candidate fields

The source column is exact: `a` copies observation evidence, meaning discovery facts and, for `duplicate_search`, the locus search that follows the assessment; `b` comes from the assessment; and `c` is calculated by the script. Candidate records have exactly these 29 fields and no additional properties.

| Field | Source |
| --- | --- |
| `candidate_id` | `c` |
| `candidate_key` | `c` |
| `generated_by` | `c` |
| `pattern_ids` | `c` |
| `pattern_revisions` | `c` |
| `repository` | `a` |
| `repository_node_id` | `a` |
| `locus` | `b` |
| `status` | `b` |
| `status_reason` | `b` |
| `sensitivity` | `b` |
| `summary` | `b` |
| `impact` | `b` |
| `readiness_checks` | `b` |
| `ai_policy_status` | `b` |
| `disclosure_required` | `b` |
| `private_evidence_reference` | `b` |
| `evidence_links` | `b` |
| `execution_evidence` | `b` |
| `reproduction` | `b` |
| `policy_checks` | `b` |
| `blocking_gaps` | `b` |
| `superseded_by` | `b` |
| `duplicate_search` | `a` |
| `repository_checks` | `a` |
| `verification_history` | `c` |
| `verified_at` | `a` |
| `verified_base_sha` | `a` |
| `next_recheck_required` | `c` |

`candidate_key` is the lowercase hex SHA-256 of `pattern_id + "\n" + repository_node_id + "\n" + locus`. `pattern_ids` contains one pattern and `pattern_revisions` maps it to the last analysis history revision. IDs are stable, append-only, and never renumbered. `superseded_by` is null or a different candidate ID present in the same output.

RECHECK_MUTABLE_FIELDS:
- `status`
- `status_reason`
- `blocking_gaps`
- `verified_at`
- `verified_base_sha`
- `verification_history`
- `next_recheck_required`
- `repository_checks`
- `duplicate_search`

Recheck may change exactly those nine fields. The other twenty fields remain deeply equal; newly observed policy hashes live only in snapshot evidence.

FAILED_SCOPE_REASONS:
- `budget-exhausted`
- `repository-not-found-or-inaccessible`
- `repository-unauthorized`
- `repository-forbidden`
- `repository-failed`

## Status and reason combinations

| Status | Allowed reasons |
| --- | --- |
| `unverified` | `insufficient-evidence` |
| `reproduced` | `none` |
| `duplicate` | `duplicate-open, duplicate-closed-rejected, duplicate-closed-other, already-fixed` |
| `not-applicable` | `reproduction-failed, already-fixed, archived-or-disabled, fork-redirect-to-upstream` |
| `stale` | `base-moved` |
| `policy-review` | `policy-prohibited, policy-unknown, none` |
| `issue-ready` | `ready` |
| `pr-ready` | `ready` |
| `private-report-ready` | `security-sensitive` |

Any combination outside this table is an input-contract violation. Actionable statuses are `reproduced`, `issue-ready`, `pr-ready`, and `private-report-ready`; they are a set, not a ranking.

`reproduced` is actionable, so it must carry the reproduction it claims. All three hold, or the input is a contract violation:

- `reproduction.method` is `static` or `executed`, never `none`.
- `reproduction.evidence_links` has at least one non-blank link.
- `readiness_checks.reproduction_evidence.result` is `confirmed`.

`record` rejects an assessment that misses any of them, like any other gate failure. `validate` rejects a candidates file whose current `reproduced` record misses one, and so do `render`, `recheck`, and `record --candidates`, which read the file through the same check. Such a file cannot be extended; fix the assessment and record it to a new output. The other eleven readiness checks stay optional for `reproduced`: only the public ready states need all twelve. No other status gains a reproduction requirement.

## Readiness checks

Each value is `{result, evidence_links, note}`. Result is `confirmed`, `not-confirmed`, or `not-applicable`. A confirmed value has at least one evidence link. Public ready states require all twelve confirmed, except `rejection_still_valid` may be not-applicable when no prior rejection exists.

| Key | Question |
| --- | --- |
| `present_on_head` | Is the opportunity present on the observed head? |
| `impact_described` | Is user or maintainer impact explicit? |
| `open_and_closed_searched` | Were open and closed issues and pull requests searched? |
| `rejection_still_valid` | Was any prior rejection reviewed against current facts? |
| `repository_active_and_issues_enabled` | Is the repository active with the needed channel enabled? |
| `policy_files_reviewed` | Were repository and organization policy files reviewed? |
| `ai_policy_allowed` | Does the policy permit the proposed assistance? |
| `not_security_sensitive` | Is the public path safe? |
| `reproduction_evidence` | Is reproduction evidence adequate? |
| `design_difference_reviewed` | Were differences from prior work assessed? |
| `provenance_allows_independent_work` | Does provenance permit independent implementation? |
| `verified_at_and_sha_recorded` | Are observation time and base SHA recorded? |

## Assessment and duplicate gate

An assessment contains `schema_version`, `discovery_sha256`, and `assessments`. Each assessment names the pattern, repository, locus, status, reason, sensitivity, summary, impact, all readiness checks, `duplicate_verdict`, policy result, disclosure requirement, private reference, evidence links, execution evidence, reproduction, policy checks, blocking gaps, and supersession.

`sensitivity` is exactly `normal` or `security-sensitive`. `record` refuses any other value before writing (exit 2) and `validate` refuses it on a candidate and on every snapshot, because an unknown value would otherwise slip past the `security-sensitive` gates that keep a candidate out of `issue-ready` and `pr-ready` and route it to `private-report-ready`.

`duplicate_verdict` is `{matched_items, judgment}` and is preserved in every snapshot. When `readiness_checks.open_and_closed_searched.result` is `confirmed`, `duplicate_verdict.judgment` must be non-empty. Discovery combinations that were skipped, failed, or absent cannot be assessed.

## Policy gate

`policy_checks` has exactly the eight keys `contributing`, `issue_template`, `pr_template`, `security_policy`, `code_of_conduct`, `cla_or_dco`, `ai_policy`, and `program_rules`. Each value is `{found, source, sha256, assessment, evidence_links}`.

`found` is three-valued and the three values are not interchangeable. `true` means the policy was observed, `false` means it was looked for and is not there, and `null` means it was never reviewed. A ready status — `issue-ready`, `pr-ready`, or `private-report-ready` — requires every key except `program_rules` to be `true` or `false`; a `null` is a gate failure, so a candidate can no longer reach a ready status without any policy observation at all. A `request-failed` observation of a mapped path is likewise a gate failure for those statuses, whether the failure was in the target repository or in `{owner}/.github`.

A `found: true` value needs a non-empty `source` and a `sha256` bound to bytes the run actually holds. Two bindings are accepted. The first is a policy file that discovery fetched, matched by the paths mapped to that key. The second is a hand-reviewed policy supplied to `record` through `--policy-evidence`, a local JSON document `{schema_version, observations}` whose observations have exactly `repository`, `policy_key`, `path`, `source_repository`, `source_url`, and `content_path`. `content_path` must be a local file, never a URL; the script hashes its bytes and only that digest is accepted, only for the named repository and policy key. The observation, its digest, and an untrusted excerpt are recorded in the manifest run inputs. This is the path for a policy that lives outside the ten conventional paths, such as an assistance policy kept in a repository's own documentation tree.

`found` must be exactly a boolean or `null`; numbers such as `0` or `1` are rejected even though they compare equal to booleans.

For a ready status, a `found: false` value is an absence claim bound to observations, and its `sha256` must be `null`. Every path mapped to the key must have a `found` or `absent` observation in the target repository and in `{owner}/.github`; the organization pass is skipped only when the target is `{owner}/.github` itself. A 404 for `{owner}/.github` counts as absent. A missing observation, any other status, or a `request-failed` observation leaves the absence unobserved and fails the gate.

- Path-specific keys (`contributing`, `issue_template`, `pr_template`, `security_policy`, `code_of_conduct`) are absent only when every mapped path is absent. A found file at a mapped path, or a `--policy-evidence` observation naming the key, contradicts the claim and fails the gate.
- `cla_or_dco` and `ai_policy` are mapped to all ten paths because such a policy can live in any of those documents. File presence therefore neither proves nor refutes them, and the script never infers semantic absence. A ready absence claim requires a `--policy-evidence` observation for the same repository and key that has the same `source_repository`, the same `path`, and a local digest equal to the discovery `sha256` for every found file in the mapped scope. When every mapped path is absent there is nothing that binds the reviewed scope, so the claim fails the gate even with unrelated evidence.

This binding proves which bytes were read and where they came from. It does not prove that the policy is absent: that remains the assessment's own review judgment, recorded in `assessment` and `evidence_links`, and it covers only the conventional paths plus the hand-reviewed files. When the scope is ambiguous, keep the candidate at `policy-review`; non-ready statuses keep accepting unbound `false` values.

Recheck re-observes only the conventional paths. A hand-reviewed digest therefore cannot be reproduced by recheck, and the resulting policy-signature change moves the candidate to `policy-review` rather than silently keeping it ready.

## Locus search

`discover` searches for duplicates before a locus exists, so its queries carry pattern-level clues only. The `locus-search` command runs after the assessment fixes each locus and repeats the search with clues derived from that locus. Its output is `{schema_version, generated_by, discovery_sha256, searches, status}`, and each search has exactly `pattern_id`, `repository`, `locus`, `clues`, `queries`, `complete`, `unused_clues`, and `method_limitations`.

`record` takes that document through `--locus-search`. Its `discovery_sha256` must match the discovery input, every stored query must still be scoped to its own repository, and a ready status requires a matching `(pattern_id, repository, locus)` entry that is complete and has at least one clue. A ready status also requires a non-empty `locus`.

### Locus duplicate rule

The assessment is written before the locus search, so `record` applies the locus search's pull requests itself without another assessment. Only the matching `(pattern_id, repository, locus)` search counts, only items with `pull_request: true`, and only the clues derived from the locus (operator clues are left out).

- **Same locus**: at least two locus-derived clues were searched and the pull request is returned for every one of them, open or closed. Search matches title, body, and comments, so this means its text names every token of the locus. Changed files are not read: that would need a request outside R3.5.
- **Undecided**: the pull request is not a same-locus match, but every clue that did not return it had a listing cut short (`total_count` above the listed items, or no answer), or fewer than two locus-derived clues were searched.

A same-locus pull request appends its URL to `duplicate_verdict.matched_items`, adds a `record:` sentence to `duplicate_verdict.judgment`, and adds a `locus-duplicate:` blocking gap. A candidate whose status is still open for a proposal (`issue-ready`, `pr-ready`, `private-report-ready`, `reproduced`, `policy-review`, `unverified`) becomes `duplicate` with `duplicate-open` when any match is open and `duplicate-closed-other` otherwise: a search item does not say whether a closed pull request was merged or why it closed. `duplicate`, `not-applicable`, and `stale` keep their status and gain only the gap. An undecided pull request matters only for a ready status: it becomes `policy-review` with reason `none` and a `locus-duplicate-undetermined:` gap for a person to check. These are values `record` writes into the `b` fields `status`, `status_reason`, and `blocking_gaps` and into the snapshot; the assessment file is unchanged. The rule can over-match: a pull request that names every locus token without changing the locus file (for example one that edits every rule) is reported too, and the gap lists every URL so a person can tell them apart.

The candidate's `duplicate_search` is the union of both rounds: locus queries are appended, `locus_clues` lists the locus-derived clues and is empty when no locus round ran, and `complete` is true only when both rounds returned every query. `complete` means every query returned a response. It never means no duplicate exists, and `method_limitations` says so on every record so the report cannot be read the other way.

Remote free text is stored only as `{text, sha256, source_url, truncated, untrusted: true}`. It is inert data, never a command, permission, path, option, or decision input.

The `sha256` field has context-specific persisted meanings that reflect the current producer behavior:

- For an ordinary untrusted-text object, `sha256` is the digest of the stored text UTF-8 bytes.
- For a policy file excerpt, `sha256` is the digest of the full decoded content bytes before truncation, and equals the containing policy result's `sha256`. A truncated excerpt therefore does not generally hash to its stored `text`.
- For a policy directory excerpt, `sha256` is the digest of sorted-name canonical JSON bytes: valid string names are sorted and serialized with sorted keys and compact separators. It also equals the containing policy result's `sha256`.

These meanings document the existing fields and calculations; they do not add a field or change the schema.

## Static-search evidence

Static search retains its existing `available`, `hits`, `execution_surfaces`, `skipped_files`, `clone_sha`, and `error` fields. It additively reports `static_search.complete`, `static_search.exclusions`, and `static_search.read_failures`.

- `available` means a successful clone allowed the search routine to run. `complete` means traversal and every non-excluded file eligibility check and read completed.
- `exclusions` has exactly the nonnegative counters `symlink`, `oversize`, and `binary`. Intentional exclusions do not make the search incomplete.
- `read_failures` is an ordered array of clone-relative `{path, operation}` entries, where operation is `walk`, `stat`, or `read`. It never stores exception text. File-level stat/read failures contribute to `skipped_files`; walk failures do not invent an unknown file count.
- A non-empty `read_failures` emits `static-search-incomplete:<count>` and makes the usable result partial without discarding positive hits.

For compatibility, missing complete is unknown/incomplete, never implicitly true. A zero-hit absence requires both available and complete. The follow-up consumers #80 through #82 must check both values before treating zero hits as absence; positive hits remain usable when a search is incomplete.

## Snapshots

`verification_history` is append-only. A snapshot contains `verification_id`, `revision`, `verified_at`, `verified_base_sha`, `status`, `status_reason`, `sensitivity`, `evidence`, and `inputs.kind`. Top-level status, reason, time, and base SHA equal the last snapshot.

- A record snapshot has `inputs.kind: record` plus non-null `discovery_sha256` and `assessment_sha256`.
- A recheck snapshot has `inputs.kind: recheck` plus null discovery and assessment hashes.
- Recheck inherits `readiness_checks`, `reproduction`, `execution_evidence`, and `duplicate_verdict` by deep copy from the immediately preceding snapshot.
- Recheck records current `repository_checks`, `duplicate_search`, policy hashes, and `observed_head_sha`; `verified_base_sha` continues to identify the assessed base.
- A moved base becomes `stale`; changed duplicate or policy evidence becomes `policy-review`; recheck never promotes a candidate to a ready state.

## Manifest and exit codes

The manifest envelope is `{schema_version, generated_by, runs}`. Each append-only run contains `run_id`, `command`, start and completion observations, `inputs`, `budget`, `repositories`, `clones`, `failed_scopes`, `retry_events`, `method_limitations`, `warnings`, `status`, and `outcome`. Every requested repository has one ordered `{repository, outcome, reason, stages_completed, head_sha}` entry. The recorded commands are `discover`, `locus-search`, `record`, and `recheck`. `plan` is offline: it counts the requests a discover run can take against its budget and proposes a split, makes no request and writes no run.

| Code | Meaning |
| --- | --- |
| `0` | Complete normal result, including an honest non-actionable result. |
| `1` | Candidate validation failed. |
| `2` | Invalid input, contract violation, or assessment gate failure. |
| `3` | Usable partial result. |
| `4` | Failed with no usable candidate records. |

Discover applies one priority rule. Code `4` is allowed only when all repositories had actually attempted access failures. Otherwise any access failure, incomplete duplicate search, unavailable code search, or exhausted budget is code `3`; budget exhaustion always exits `3`. A failure output and manifest are still written for audit.

The `clone-cleanup-failed:` and `static-search-incomplete:` warning prefixes also produce code `3` and partial status. They are additive OR conditions after the all-access-failures code `4` decision and do not change the budget-exhaustion rule. A clone manifest entry remains exactly `{repository, path, sha, removed, size_bytes}`; cleanup warnings do not add clone fields.

## Ownership and recovery

The sibling PR collector owns normalized historical PR collection. The sibling pattern analyzer owns evidence-backed PAT-* derivation. This verifier reads their validated analysis without modifying it and is the only sibling that issues CAN-* records. It never performs GitHub writes.

Recovery paths are explicit:

- partial → new discover run
- budget exhausted in the searches → `plan`, then the proposed discover runs adopted together as one discovery
- stale → new discover + record
- gate failure → fix assessment, then record
- missing or incomplete locus search → new locus-search run, then record

Existing candidate IDs and history remain intact during every recovery.

## Follow-up: clone failure diagnostics

The current `clone_repository` discards git stderr, and `_static_record` reduces clone diagnostics to clone-failed. This limits operational diagnosis of clone and revision lookup failures. A future contract should separately classify the failing stage and retain a redacted, length-bounded stderr summary. That work requires separate approval because its redaction and persistence rules need their own review. It is not implemented here. No issue is created by this change.
