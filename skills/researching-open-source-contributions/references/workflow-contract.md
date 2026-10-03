# Workflow Contract

## Request

A request is one JSON object with exactly these keys. Unknown keys are rejected.

```json
{
  "schema_version": "1.0.0",
  "mode": "offline-fixture | offline-reuse",
  "source": {
    "kind": "recent-closed-prs",
    "repositories": ["owner/name"],
    "interval": {"start_at": "2026-09-01T00:00:00Z", "end_at": "2026-09-08T00:00:00Z"},
    "outcome": "all | merged | closed-unmerged",
    "max_per_repository": 5,
    "request_budget": 200,
    "reference_repositories": []
  },
  "target": {
    "repositories": ["owner/name"],
    "max_candidates_per_repo": 5,
    "max_candidates_total": 20,
    "discover_request_budget": 100,
    "locus_request_budget": 50,
    "allow_clone": false
  },
  "budget": {"total_api_requests": 400, "max_elapsed_seconds": null},
  "static_only": true,
  "provenance": {"model": "unknown", "effort": "unknown"}
}
```

- `source` is where historical PRs come from. `target` is where candidates are
  checked. The two lists are independent.
- `target` may be `null` when the caller names no target. That run collects and
  analyzes only: there are no candidate caps, discover/locus budgets, or clone
  flag to state, `discovery`, `locus-search`, and `record` report `skipped`
  with the reason, and `adopt-discovery`, `adopt-locus-search`, and `record`
  are refused as `target-unspecified`. A source repository is never used as a
  stand-in target. An object `target` keeps every rule below.
- There is no live mode. `offline-reuse` adopts real outputs that were produced
  earlier. `offline-fixture` is for tests and is the only mode that accepts
  `synthetic-fixture` origins.
- `static_only` must be `true`, and `allow_clone` must be `false`. Clone-based
  static search is not connected yet.
- `provenance` is what the session says about its own model and effort. Use
  `"unknown"` when the session does not say. Nothing is fixed or guessed.
- `source.kind` of `issues` is accepted as a request, but the run stays
  `pending-unsupported`: the Issue corpus is not a pull request corpus. An
  isolated Issue collection entry exists; its output is not connected to this
  workflow. A run takes exactly one source kind; a list or a mix is rejected.
- `source.kind` of `curated-tracker` replaces the recent-closed keys with
  exactly these (no `interval` or `outcome`):

  ```json
  "source": {
    "kind": "curated-tracker",
    "tracker": {"repository": "owner/tracker", "issue_number": 7},
    "repositories": ["owner/name"],
    "max_prs": 5,
    "request_budget": 200,
    "reference_repositories": []
  }
  ```

  `tracker` and `max_prs`/`request_budget` are the curated collector's
  `--tracker-repo`, `--issue`, `--max-prs`, and `--request-budget`.
  `repositories` is the caller's allow list for the pull requests the tracker
  names. Collection output never widens it.

## Run directory

`init` creates the run directory, or re-enters one created for the same
request bytes. Any other request is a `scope-conflict` and needs a new
directory. A non-empty directory without the run marker is refused.

```text
RUN/workflow-run.json      marker {run_id, request_sha256}
RUN/request.json           request bytes (immutable)
RUN/state.json             checkpoints, budget ledger, events
RUN/stages/<stage>/gen-NNN adopted inputs and derived outputs
RUN/report.md              report command output
```

The stages are `collect`, `digest`, `analysis`, `discovery`, `locus-search`,
and `record`. Every generation records its status, reasons, input SHA-256 and
source path, output SHA-256 values, the request hash, the sibling and workflow
revisions, and, for AI stages, the claimed provenance.

## Checkpoint rules

1. Inputs are copied into the reserved next `gen-NNN` directory and hashed
   from the copied bytes. They are validated with the sibling validators before
   `state.json` makes that generation active; a failed command removes the
   directory, and a leftover uncommitted directory is refused, not reused. A rejection leaves checkpoints, outputs, and
   the ledger unchanged. Only a `rejected` event is appended.
2. The same input bytes with the same tool revisions are revalidated and
   reported as `reused`. No new generation is created.
3. Different input bytes, or the same bytes under a changed sibling or
   workflow revision, need `--replace` for an adopted stage. The previous
   generation is kept, and downstream active checkpoints become `invalidated`.
4. Every command re-checks active checkpoints. A changed output byte, a
   generation path that is a symlink or resolves outside the run, or a
   checkpoint recorded under other sibling/workflow revisions blocks the stage
   and everything that depends on it until the stage is re-adopted with
   `--replace` (`digest` and `record` are simply run again). The run marker
   records the run directory's real path; a moved or redirected run is refused.
   `report.md` is written atomically and never through a symlink.
5. Bindings: the collection manifest must match the source scope (repositories,
   interval, outcome, cap, per-run budget) in every record, and every corpus
   record must come from a source repository. For `curated-tracker`, every
   manifest record must also pass the curated collector's own manifest check
   and carry `collection_method: "tracker-issue-comments"`, the same tracker
   repository and Issue number, `max_prs`, and `request_budget` as exact
   integers (a JSON boolean never counts as a number), well-formed
   `reference_counts`, `hydration_outcomes`, and `failed_scopes`, and no
   hydrated pull request outside `repositories`. Every corpus record must carry
   only tracker sources whose Issue number and URLs name the requested tracker,
   from the curated generator, with observations from runs in that manifest.
   A repository check alone never proves where a record came from. Discovery must name the active
   analysis SHA-256, target budget, clone flag, and caps, and its partition must
   cover every repository-pattern combination exactly once. A missing target
   repository or pattern is `partial`. A locus search must name the active
   discovery SHA-256 and the locus budget. `record --policy-evidence` copies the
   evidence JSON and every referenced `content_path` file into the generation,
   hashes each, and hands the verifier the copies, so changed policy bytes are
   never reused silently. The stored `policy-evidence.json` points at those
   copies and re-verifies without the producer directory. Because
   `content_path` is chosen by result data, it must resolve to a regular file
   inside the evidence JSON's own directory; `..`, symlinks, other absolute
   paths, and URLs are rejected before any byte is read.

## Budget ledger

Producer manifests report request counts. The ledger stores one entry per
stable run identity: the SHA-256 of producer, command, run id, start and
completion time, and inputs or request fingerprint. A missing, negative,
boolean, or non-integer request count is rejected rather than read as zero, and
the same identity reported with a different count is a conflict. The curated
collector's same-run resume replaces its run record with a running total, so
its identity leaves out the completion time and a larger total for the same
run supersedes the earlier one instead of being added; a smaller one is a
conflict. A manifest that repeats
earlier runs, such as the verifier manifest shared by discover and
locus-search, is therefore counted once. The ledger survives resume and
replace. When the total exceeds `budget.total_api_requests`, the summed
elapsed time exceeds `max_elapsed_seconds`, or a time budget is set and some
run lacks timestamps (`time-budget-unverifiable`), the adopted stage is
`blocked`. Unknown time is never counted as zero.
In `offline-fixture` mode, the counts are fake-opener responses. This workflow
itself always sends zero external requests.

## Status words

| Stage status | Meaning |
| --- | --- |
| `complete` | Validated and fully covered |
| `partial` | Usable with recorded gaps |
| `failed` | Producer reported failure; downstream stops |
| `blocked` | Budget exceeded or unverifiable; downstream stops |
| `no-patterns` / `no-candidates` | Normal insufficient result |
| `skipped` | Candidate stage not run because `target` is `null` (status only; never a generation) |

The run outcome is one of `candidates`, `no-candidates` (decisive
non-actionable verdicts only), `insufficient`, `analysis-only` (patterns were
adopted but `target` is `null`, so nothing was verified), `failed`, `blocked`,
`awaiting-input`, or `pending-unsupported`. A null-target run with no pattern
stays `insufficient`. Its completeness is `complete` or
`partial`.

## Division of work

| Step | Kind | Owner |
| --- | --- | --- |
| collect (recent-closed) | network research | isolated research launcher only, through its non-AI collection entry; printed here as plan data |
| collect (curated tracker), discover, locus-search | network research | isolated research launcher only, and it has no entry verified against real data for these yet; printed here as plan data |
| pattern extraction, synthesis | AI research | isolated research launcher only, in its one measured read-only launch, following the sibling contracts |
| assessment, and anything that needs Agent, agy, sub-agents, or deep-research | AI research | isolated research launcher only, and no such path is open or verified yet |
| corpus validation, digest, prompts, output gate, record, validate, report | mechanical, offline | this workflow calling the sibling scripts in the development session |

## Isolation gate

This workflow never evaluates or bypasses the research launcher's gate. It
adopts outputs that already exist and runs synthetic fixture tests; it opens
no network connection and starts no process. The `argv` it prints is plan
data with `executed: false`: it shows the scope of a step, not the isolated
command. Direct research from the host session, GitHub writes, and lookups
outside the recorded budget stay forbidden.

New research of either kind runs only through the launcher's unit-wrapper
entries inside the Linux VM, with the user's approval and the coordinator's
instruction.

| Launcher path | State |
| --- | --- |
| non-AI collection of pull requests and of Issues | has run through its entry up to real reads; the Issue reads came back partial, and the Issue output is not connected to this workflow |
| model run, one measured launch: Debian 13 arm64 guest, read-only, `dontAsk`, no relay, the one measured combination of profile, model, and effort | open; every other model launch exits 3 |
| `auto` | closed; no OS-level evidence that the boundaries hold under it |
| staged runs (deep-research, Agent, search) | closed; never run with a model |
| model run with a relay | closed |
| macOS, every launch | closed; a setsid-escaped descendant survives cleanup and Keychain isolation is unverified |
| candidate reads (discover, locus-search) | no isolated entry verified against real data |
| curated tracker collection | no isolated entry |
| assessment from an isolated model | no path |

A row that is closed or unverified is never reported as available, and a
fake success manifest from a probe is not a model result.

`origin` is a caller claim. It does not prove that a model ran or that
independent verification took place. The analyzer ledger string is not proof
either.
