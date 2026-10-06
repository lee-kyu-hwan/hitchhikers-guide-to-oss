# 옛 경로 → 새 경로 대응표 (dotfiles#290)

dotfiles(`lee-kyu-hwan/dotfiles`) main `88f17d0`에서 이 저장소로 옮긴 파일의 대응표다. 이력은 `git filter-repo`로 옮겼고(그 커밋까지 74개), 경로 정리는 그 뒤 새 커밋이다.

## 규칙

| dotfiles 경로 | 이 저장소 | 설치 위치(dotfiles chezmoi, 그대로) |
|---|---|---|
| `dot_agents/skills/researching-open-source-contributions/` | `skills/researching-open-source-contributions/` | `~/.agents/skills/researching-open-source-contributions/` (`analysis/`는 설치하지 않음) |
| `dot_codex/skills/<스킬>/` (analyzing·verifying·collecting 5개) | `skills/<스킬>/` | `~/.codex/skills/<스킬>/` |
| `dot_local/bin/executable_oss-research-<이름>` | `bin/oss-research-<이름>` (실행 비트) | `~/.local/bin/oss-research-<이름>` |
| `dot_local/share/oss-research/` | `share/` | 설치하지 않음 |
| `tests/test_oss_research_*.py`, `tests/fixtures/oss_research_*/` | 같은 경로 | — |
| `docs/research/oss-research-{relay,worker,targets}.md` | `docs/oss-research-*.md` | — |
| `docs/development/2026-09-10-80-…/spec.md`, `2026-09-15-80-…/{spec,plan}.md` | 같은 경로 | — (curated 스킬 시험이 읽음, dotfiles에도 남음) |

남긴 것: `tests/fixtures/oss_research_linux_vm/evidence/`(VM 실측 로그 29개)와 날짜별 조사 기록 `docs/research/20*`, `oss-e2e-2026-09-28/` 등은 dotfiles에 있다.

사본(이력 없이 새 커밋으로 넣은 것):

- `share/staged/`: worker `deep-research` staged profile의 고정 원본. dotfiles의 deep-research `SKILL.md`(sha256 `a330b11e…`)와 workflow(sha256 `4d7392eb…`)를 그 해시 그대로 고정한 사본이며, dotfiles 쪽이 바뀌어도 이 사본은 바뀌지 않는다(닫힌 게이트 뒤 profile 전용).
- `tests/fixtures/dotfiles_records/`: 분석 번들 시험이 읽는 dotfiles 실행 기록 3개(`OSS_RESEARCH_RECORDS_ROOT` 기본값).

## 파일별 (209개)

| dotfiles | 이 저장소 | 비고 |
|---|---|---|
| `dot_local/bin/executable_oss-research-candidate-reads` | `bin/oss-research-candidate-reads` |  |
| `dot_local/bin/executable_oss-research-collect` | `bin/oss-research-collect` |  |
| `dot_local/bin/executable_oss-research-collect-issues` | `bin/oss-research-collect-issues` |  |
| `dot_local/bin/executable_oss-research-egress-pin` | `bin/oss-research-egress-pin` |  |
| `dot_local/bin/executable_oss-research-linux-run` | `bin/oss-research-linux-run` |  |
| `dot_local/bin/executable_oss-research-relay` | `bin/oss-research-relay` |  |
| `dot_local/bin/executable_oss-research-relay-collect` | `bin/oss-research-relay-collect` |  |
| `dot_local/bin/executable_oss-research-relay-issues` | `bin/oss-research-relay-issues` |  |
| `dot_local/bin/executable_oss-research-relay-verify` | `bin/oss-research-relay-verify` |  |
| `dot_local/bin/executable_oss-research-scope` | `bin/oss-research-scope` |  |
| `dot_local/bin/executable_oss-research-windows` | `bin/oss-research-windows` |  |
| `dot_local/bin/executable_oss-research-worker` | `bin/oss-research-worker` |  |
| `docs/development/2026-09-10-80-collecting-curated-contribution-prs/spec.md` | `docs/development/2026-09-10-80-collecting-curated-contribution-prs/spec.md` |  |
| `docs/development/2026-09-15-80-post-review-hardening/plan.md` | `docs/development/2026-09-15-80-post-review-hardening/plan.md` |  |
| `docs/development/2026-09-15-80-post-review-hardening/spec.md` | `docs/development/2026-09-15-80-post-review-hardening/spec.md` |  |
| `docs/research/oss-research-relay.md` | `docs/oss-research-relay.md` |  |
| `docs/research/oss-research-targets.md` | `docs/oss-research-targets.md` |  |
| `docs/research/oss-research-worker.md` | `docs/oss-research-worker.md` |  |
| `dot_local/share/oss-research/README.md` | `share/README.md` |  |
| `dot_local/share/oss-research/linux/candidate_reads_live.sh` | `share/linux/candidate_reads_live.sh` | #274 전에는 `tests/fixtures/oss_research_worker/linux/` |
| `dot_local/share/oss-research/linux/collect_live.sh` | `share/linux/collect_live.sh` | #274 전에는 `tests/fixtures/oss_research_worker/linux/` |
| `dot_local/share/oss-research/linux/model_once.sh` | `share/linux/model_once.sh` | #274 전에는 `tests/fixtures/oss_research_worker/linux/` |
| `dot_local/share/oss-research/linux/model_once_verdict.py` | `share/linux/model_once_verdict.py` | #274 전에는 `tests/fixtures/oss_research_worker/linux/` |
| `dot_local/share/oss-research/linux/verify_pickup.py` | `share/linux/verify_pickup.py` | #274 전에는 `tests/fixtures/oss_research_worker/linux/` |
| `(사본) dot_agents/skills/deep-research/SKILL.md` | `share/staged/skills/deep-research/SKILL.md` |  |
| `(사본) dot_claude/workflows/deep-research.js` | `share/staged/workflows/deep-research.js` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/SKILL.md` | `skills/analyzing-open-source-pr-patterns/SKILL.md` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/agents/openai.yaml` | `skills/analyzing-open-source-pr-patterns/agents/openai.yaml` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/references/analysis-contract.md` | `skills/analyzing-open-source-pr-patterns/references/analysis-contract.md` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/references/data-contract.md` | `skills/analyzing-open-source-pr-patterns/references/data-contract.md` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/references/extraction-contract.md` | `skills/analyzing-open-source-pr-patterns/references/extraction-contract.md` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/scripts/build_digest.py` | `skills/analyzing-open-source-pr-patterns/scripts/build_digest.py` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/scripts/build_prompts.py` | `skills/analyzing-open-source-pr-patterns/scripts/build_prompts.py` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/scripts/check_extraction.py` | `skills/analyzing-open-source-pr-patterns/scripts/check_extraction.py` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/scripts/validate_corpus.py` | `skills/analyzing-open-source-pr-patterns/scripts/validate_corpus.py` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/tests/test_extraction.py` | `skills/analyzing-open-source-pr-patterns/tests/test_extraction.py` |  |
| `dot_codex/skills/analyzing-open-source-pr-patterns/tests/test_validate_corpus.py` | `skills/analyzing-open-source-pr-patterns/tests/test_validate_corpus.py` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/SKILL.md` | `skills/collecting-curated-contribution-prs/SKILL.md` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/agents/openai.yaml` | `skills/collecting-curated-contribution-prs/agents/openai.yaml` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/evals/behavioral-eval.md` | `skills/collecting-curated-contribution-prs/evals/behavioral-eval.md` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/references/collection-contract.md` | `skills/collecting-curated-contribution-prs/references/collection-contract.md` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/references/github-rest-contract.md` | `skills/collecting-curated-contribution-prs/references/github-rest-contract.md` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/scripts/collect_curated_contribution_prs.py` | `skills/collecting-curated-contribution-prs/scripts/collect_curated_contribution_prs.py` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/expected-corpus.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/expected-corpus.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/initial-corpus.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/initial-corpus.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/manifest-cases.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/manifest-cases.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/partial-hydration-thread.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/partial-hydration-thread.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/pr-hydration.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/pr-hydration.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/recollected-corpus.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/recollected-corpus.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/reference-classification.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/reference-classification.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread-edited.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread-edited.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread-failures.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread-failures.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread.json` | `skills/collecting-curated-contribution-prs/tests/fixtures/tracker-thread.json` |  |
| `dot_codex/skills/collecting-curated-contribution-prs/tests/test_collect_curated_contribution_prs.py` | `skills/collecting-curated-contribution-prs/tests/test_collect_curated_contribution_prs.py` |  |
| `dot_codex/skills/collecting-open-source-issues/SKILL.md` | `skills/collecting-open-source-issues/SKILL.md` |  |
| `dot_codex/skills/collecting-open-source-issues/agents/openai.yaml` | `skills/collecting-open-source-issues/agents/openai.yaml` |  |
| `dot_codex/skills/collecting-open-source-issues/references/collection-contract.md` | `skills/collecting-open-source-issues/references/collection-contract.md` |  |
| `dot_codex/skills/collecting-open-source-issues/references/github-api-contract.md` | `skills/collecting-open-source-issues/references/github-api-contract.md` |  |
| `dot_codex/skills/collecting-open-source-issues/scripts/collect_open_source_issues.py` | `skills/collecting-open-source-issues/scripts/collect_open_source_issues.py` |  |
| `dot_codex/skills/collecting-open-source-issues/tests/fixtures/synthetic-issues.json` | `skills/collecting-open-source-issues/tests/fixtures/synthetic-issues.json` |  |
| `dot_codex/skills/collecting-open-source-issues/tests/test_collect_open_source_issues.py` | `skills/collecting-open-source-issues/tests/test_collect_open_source_issues.py` |  |
| `dot_codex/skills/collecting-recent-closed-prs/SKILL.md` | `skills/collecting-recent-closed-prs/SKILL.md` |  |
| `dot_codex/skills/collecting-recent-closed-prs/agents/openai.yaml` | `skills/collecting-recent-closed-prs/agents/openai.yaml` |  |
| `dot_codex/skills/collecting-recent-closed-prs/references/collection-contract.md` | `skills/collecting-recent-closed-prs/references/collection-contract.md` |  |
| `dot_codex/skills/collecting-recent-closed-prs/references/github-rest-contract.md` | `skills/collecting-recent-closed-prs/references/github-rest-contract.md` |  |
| `dot_codex/skills/collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py` | `skills/collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py` |  |
| `dot_codex/skills/collecting-recent-closed-prs/tests/test_collect_recent_closed_prs.py` | `skills/collecting-recent-closed-prs/tests/test_collect_recent_closed_prs.py` |  |
| `dot_agents/skills/researching-open-source-contributions/SKILL.md` | `skills/researching-open-source-contributions/SKILL.md` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/README.md` | `skills/researching-open-source-contributions/analysis/README.md` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/assess_contract.py` | `skills/researching-open-source-contributions/analysis/assess_contract.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/assess_contract.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/harness/analysis_call.sh` | `skills/researching-open-source-contributions/analysis/harness/analysis_call.sh` | #261 전에는 `tests/fixtures/oss_research_worker/linux/analysis_call.sh` |
| `dot_agents/skills/researching-open-source-contributions/analysis/harness/call_with_retry.py` | `skills/researching-open-source-contributions/analysis/harness/call_with_retry.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/machine_settings.py` | `skills/researching-open-source-contributions/analysis/machine_settings.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/prepare_input.sh` | `skills/researching-open-source-contributions/analysis/prepare_input.sh` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prepare_input.sh`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompt_contract.py` | `skills/researching-open-source-contributions/analysis/prompt_contract.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/assess.md` | `skills/researching-open-source-contributions/analysis/prompts/assess.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/assess.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/finalize-reduced.md` | `skills/researching-open-source-contributions/analysis/prompts/finalize-reduced.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/finalize-reduced.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/finalize.md` | `skills/researching-open-source-contributions/analysis/prompts/finalize.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/finalize.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/synthesize-merge.md` | `skills/researching-open-source-contributions/analysis/prompts/synthesize-merge.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/synthesize-merge.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/synthesize-reduced.md` | `skills/researching-open-source-contributions/analysis/prompts/synthesize-reduced.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/synthesize-reduced.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/prompts/synthesize.md` | `skills/researching-open-source-contributions/analysis/prompts/synthesize.md` | #261 전에는 `docs/research/oss-e2e-2026-09-28/prompts/synthesize.md`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/request.template.json` | `skills/researching-open-source-contributions/analysis/request.template.json` | #261 전에는 `docs/research/oss-e2e-2026-09-28/request.template.json`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/retry_policy.py` | `skills/researching-open-source-contributions/analysis/retry_policy.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/run_limits.json` | `skills/researching-open-source-contributions/analysis/run_limits.json` | #261 전에는 `docs/research/oss-e2e-2026-09-28/run_limits.json`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/selftest_verify.py` | `skills/researching-open-source-contributions/analysis/selftest_verify.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/selftest_verify.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/selftest_verify_kinds.py` | `skills/researching-open-source-contributions/analysis/selftest_verify_kinds.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/selftest_verify_kinds.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/selftest_verify_scope.py` | `skills/researching-open-source-contributions/analysis/selftest_verify_scope.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/selftest_verify_scope.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/stage_inputs.py` | `skills/researching-open-source-contributions/analysis/stage_inputs.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/stage_inputs.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_assess.py` | `skills/researching-open-source-contributions/analysis/test_assess.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_assess.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_assess_contract.py` | `skills/researching-open-source-contributions/analysis/test_assess_contract.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_assess_contract.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_assess_ledger.py` | `skills/researching-open-source-contributions/analysis/test_assess_ledger.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_assess_ledger.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_continue_plan.py` | `skills/researching-open-source-contributions/analysis/test_continue_plan.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_continue_plan.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_dry_plan.py` | `skills/researching-open-source-contributions/analysis/test_dry_plan.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_dry_plan.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_machine_settings.py` | `skills/researching-open-source-contributions/analysis/test_machine_settings.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_model_retry.py` | `skills/researching-open-source-contributions/analysis/test_model_retry.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_prompt_contract.py` | `skills/researching-open-source-contributions/analysis/test_prompt_contract.py` |  |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_quote_escape.py` | `skills/researching-open-source-contributions/analysis/test_quote_escape.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_quote_escape.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_reduce_inputs.py` | `skills/researching-open-source-contributions/analysis/test_reduce_inputs.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_reduce_inputs.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_search_clues.py` | `skills/researching-open-source-contributions/analysis/test_search_clues.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_search_clues.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/test_stage_inputs.py` | `skills/researching-open-source-contributions/analysis/test_stage_inputs.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/test_stage_inputs.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/analysis/verify_e2e_result.py` | `skills/researching-open-source-contributions/analysis/verify_e2e_result.py` | #261 전에는 `docs/research/oss-e2e-2026-09-28/verify_e2e_result.py`(이력은 그 경로부터 이어짐) |
| `dot_agents/skills/researching-open-source-contributions/references/workflow-contract.md` | `skills/researching-open-source-contributions/references/workflow-contract.md` |  |
| `dot_agents/skills/researching-open-source-contributions/scripts/oss_workflow.py` | `skills/researching-open-source-contributions/scripts/oss_workflow.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/offline_fixture.py` | `skills/researching-open-source-contributions/tests/offline_fixture.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_analysis_readopt.py` | `skills/researching-open-source-contributions/tests/test_analysis_readopt.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_digest_shards.py` | `skills/researching-open-source-contributions/tests/test_digest_shards.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_merge_collections.py` | `skills/researching-open-source-contributions/tests/test_merge_collections.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_multi_target.py` | `skills/researching-open-source-contributions/tests/test_multi_target.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_oss_workflow.py` | `skills/researching-open-source-contributions/tests/test_oss_workflow.py` |  |
| `dot_agents/skills/researching-open-source-contributions/tests/test_period_merge.py` | `skills/researching-open-source-contributions/tests/test_period_merge.py` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/SKILL.md` | `skills/verifying-open-source-contribution-candidates/SKILL.md` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/agents/openai.yaml` | `skills/verifying-open-source-contribution-candidates/agents/openai.yaml` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/evals/behavioral-eval.md` | `skills/verifying-open-source-contribution-candidates/evals/behavioral-eval.md` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/references/github-verification-contract.md` | `skills/verifying-open-source-contribution-candidates/references/github-verification-contract.md` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/references/verification-contract.md` | `skills/verifying-open-source-contribution-candidates/references/verification-contract.md` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/scripts/verify_candidates.py` | `skills/verifying-open-source-contribution-candidates/scripts/verify_candidates.py` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/analysis.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/analysis.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/combo-stage/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/combo-stage/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/partition-violation/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/partition-violation/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/repo-stage/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/repo-stage/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/unstarted/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/budget/unstarted/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/candidates-existing.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/candidates-existing.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/caps/analysis.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/caps/analysis.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/common/analysis.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/common/analysis.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/failures/all-404/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/failures/all-404/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/failures/cases.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/failures/cases.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/analysis.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/analysis.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/fixture.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/fixture.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/injection/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/insufficient/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/insufficient/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/insufficient/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/insufficient/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/mixed/case.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/mixed/case.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/negative/cases.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/negative/cases.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/negative/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/negative/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/positive/assessment-issue-ready.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/positive/assessment-issue-ready.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/positive/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/positive/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/redaction/analysis.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/redaction/analysis.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/redaction/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/redaction/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/security/assessments.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/security/assessments.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/stale/cases.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/stale/cases.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/fixtures/stale/responses.json` | `skills/verifying-open-source-contribution-candidates/tests/fixtures/stale/responses.json` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/test_skill_contract.py` | `skills/verifying-open-source-contribution-candidates/tests/test_skill_contract.py` |  |
| `dot_codex/skills/verifying-open-source-contribution-candidates/tests/test_verify_candidates.py` | `skills/verifying-open-source-contribution-candidates/tests/test_verify_candidates.py` |  |
| `(사본) docs/research/2026-10-04-period-research/analysis-eslint-request.json` | `tests/fixtures/dotfiles_records/2026-10-04-period-research/analysis-eslint-request.json` |  |
| `(사본) docs/research/oss-e2e-2026-09-28/fixtures/corpus-min.json` | `tests/fixtures/dotfiles_records/oss-e2e-2026-09-28/fixtures/corpus-min.json` |  |
| `(사본) docs/research/oss-e2e-2026-09-28/run_limits.legacy-fixture.json` | `tests/fixtures/dotfiles_records/oss-e2e-2026-09-28/run_limits.legacy-fixture.json` |  |
| `tests/fixtures/oss_research_linux_vm/egress/egress_guest_verify.sh` | `tests/fixtures/oss_research_linux_vm/egress/egress_guest_verify.sh` |  |
| `tests/fixtures/oss_research_linux_vm/egress/mock_dns.py` | `tests/fixtures/oss_research_linux_vm/egress/mock_dns.py` |  |
| `tests/fixtures/oss_research_linux_vm/egress/mock_tls.py` | `tests/fixtures/oss_research_linux_vm/egress/mock_tls.py` |  |
| `tests/fixtures/oss_research_linux_vm/egress_control_guest.sh` | `tests/fixtures/oss_research_linux_vm/egress_control_guest.sh` |  |
| `tests/fixtures/oss_research_linux_vm/lima-oss-research-debian.yaml` | `tests/fixtures/oss_research_linux_vm/lima-oss-research-debian.yaml` |  |
| `tests/fixtures/oss_research_linux_vm/lima-oss-research.yaml` | `tests/fixtures/oss_research_linux_vm/lima-oss-research.yaml` |  |
| `tests/fixtures/oss_research_linux_vm/preflight_guest.sh` | `tests/fixtures/oss_research_linux_vm/preflight_guest.sh` |  |
| `tests/fixtures/oss_research_linux_vm/roundtrip_host.sh` | `tests/fixtures/oss_research_linux_vm/roundtrip_host.sh` |  |
| `tests/fixtures/oss_research_linux_vm/safe_tar.py` | `tests/fixtures/oss_research_linux_vm/safe_tar.py` |  |
| `tests/fixtures/oss_research_linux_vm/selftest_safe_tar.py` | `tests/fixtures/oss_research_linux_vm/selftest_safe_tar.py` |  |
| `tests/fixtures/oss_research_offline_integration/relay_probe.py` | `tests/fixtures/oss_research_offline_integration/relay_probe.py` |  |
| `tests/fixtures/oss_research_offline_integration/sitecustomize.py` | `tests/fixtures/oss_research_offline_integration/sitecustomize.py` |  |
| `tests/fixtures/oss_research_relay/sitecustomize.py` | `tests/fixtures/oss_research_relay/sitecustomize.py` |  |
| `tests/fixtures/oss_research_worker/candidate_reads_live/SYNTHETIC-NOT-A-REAL-ANALYSIS.analysis.json` | `tests/fixtures/oss_research_worker/candidate_reads_live/SYNTHETIC-NOT-A-REAL-ANALYSIS.analysis.json` |  |
| `tests/fixtures/oss_research_worker/linux/candidate_reads_cases.sh` | `tests/fixtures/oss_research_worker/linux/candidate_reads_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/candidate_write_probe.py` | `tests/fixtures/oss_research_worker/linux/candidate_write_probe.py` |  |
| `tests/fixtures/oss_research_worker/linux/cgroup_escape.py` | `tests/fixtures/oss_research_worker/linux/cgroup_escape.py` |  |
| `tests/fixtures/oss_research_worker/linux/clock_guard_cases.sh` | `tests/fixtures/oss_research_worker/linux/clock_guard_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/clock_guard_probe.py` | `tests/fixtures/oss_research_worker/linux/clock_guard_probe.py` |  |
| `tests/fixtures/oss_research_worker/linux/collect_cases.sh` | `tests/fixtures/oss_research_worker/linux/collect_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/collect_docs_cases.sh` | `tests/fixtures/oss_research_worker/linux/collect_docs_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/collect_issues_cases.sh` | `tests/fixtures/oss_research_worker/linux/collect_issues_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/collect_probe.py` | `tests/fixtures/oss_research_worker/linux/collect_probe.py` |  |
| `tests/fixtures/oss_research_worker/linux/egress_integration.sh` | `tests/fixtures/oss_research_worker/linux/egress_integration.sh` |  |
| `tests/fixtures/oss_research_worker/linux/egress_verdict.py` | `tests/fixtures/oss_research_worker/linux/egress_verdict.py` |  |
| `tests/fixtures/oss_research_worker/linux/evidence/e3-13b2ba5-1.record.json` | `tests/fixtures/oss_research_worker/linux/evidence/e3-13b2ba5-1.record.json` |  |
| `tests/fixtures/oss_research_worker/linux/evidence/e3-13b2ba5-2.record.json` | `tests/fixtures/oss_research_worker/linux/evidence/e3-13b2ba5-2.record.json` |  |
| `tests/fixtures/oss_research_worker/linux/gate_cases.sh` | `tests/fixtures/oss_research_worker/linux/gate_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/github_token_cases.sh` | `tests/fixtures/oss_research_worker/linux/github_token_cases.sh` |  |
| `tests/fixtures/oss_research_worker/linux/github_token_probe.py` | `tests/fixtures/oss_research_worker/linux/github_token_probe.py` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_partial/p4-eslint-markdown/collect.tar` | `tests/fixtures/oss_research_worker/linux/pickup_partial/p4-eslint-markdown/collect.tar` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_partial/p4-eslint-markdown/record.json` | `tests/fixtures/oss_research_worker/linux/pickup_partial/p4-eslint-markdown/record.json` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run2-pr20898/collect.tar` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run2-pr20898/collect.tar` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run2-pr20898/record.json` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run2-pr20898/record.json` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run5-pr21366/collect.tar` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run5-pr21366/collect.tar` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run5-pr21366/record.json` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run5-pr21366/record.json` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run6-pr21315/collect.tar` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run6-pr21315/collect.tar` |  |
| `tests/fixtures/oss_research_worker/linux/pickup_zero/run6-pr21315/record.json` | `tests/fixtures/oss_research_worker/linux/pickup_zero/run6-pr21315/record.json` |  |
| `tests/fixtures/oss_research_worker/linux/supervised_run.py` | `tests/fixtures/oss_research_worker/linux/supervised_run.py` |  |
| `tests/fixtures/oss_research_worker/linux/unit_inner.py` | `tests/fixtures/oss_research_worker/linux/unit_inner.py` |  |
| `tests/fixtures/oss_research_worker/probe.py` | `tests/fixtures/oss_research_worker/probe.py` |  |
| `tests/fixtures/oss_research_worker/runtime/package-lock.json` | `tests/fixtures/oss_research_worker/runtime/package-lock.json` |  |
| `tests/fixtures/oss_research_worker/runtime/package.json` | `tests/fixtures/oss_research_worker/runtime/package.json` |  |
| `tests/fixtures/oss_research_worker_supervisor/relay_probe.py` | `tests/fixtures/oss_research_worker_supervisor/relay_probe.py` |  |
| `tests/fixtures/oss_research_worker_supervisor/sitecustomize.py` | `tests/fixtures/oss_research_worker_supervisor/sitecustomize.py` |  |
| `tests/test_oss_research_candidate_reads.py` | `tests/test_oss_research_candidate_reads.py` |  |
| `tests/test_oss_research_clock_guard.py` | `tests/test_oss_research_clock_guard.py` |  |
| `tests/test_oss_research_collect_issues.py` | `tests/test_oss_research_collect_issues.py` |  |
| `tests/test_oss_research_egress_pin.py` | `tests/test_oss_research_egress_pin.py` |  |
| `tests/test_oss_research_github_token.py` | `tests/test_oss_research_github_token.py` |  |
| `tests/test_oss_research_offline_integration.py` | `tests/test_oss_research_offline_integration.py` |  |
| `tests/test_oss_research_relay.py` | `tests/test_oss_research_relay.py` |  |
| `tests/test_oss_research_relay_collect.py` | `tests/test_oss_research_relay_collect.py` |  |
| `tests/test_oss_research_relay_issues.py` | `tests/test_oss_research_relay_issues.py` |  |
| `tests/test_oss_research_relay_verify.py` | `tests/test_oss_research_relay_verify.py` |  |
| `tests/test_oss_research_scope.py` | `tests/test_oss_research_scope.py` |  |
| `tests/test_oss_research_windows.py` | `tests/test_oss_research_windows.py` |  |
| `tests/test_oss_research_worker.py` | `tests/test_oss_research_worker.py` |  |
| `tests/test_oss_research_worker_auth.py` | `tests/test_oss_research_worker_auth.py` |  |
| `tests/test_oss_research_worker_linux.py` | `tests/test_oss_research_worker_linux.py` |  |
| `tests/test_oss_research_worker_runtime.py` | `tests/test_oss_research_worker_runtime.py` |  |
| `tests/test_oss_research_worker_skills.py` | `tests/test_oss_research_worker_skills.py` |  |
| `tests/test_oss_research_worker_supervisor.py` | `tests/test_oss_research_worker_supervisor.py` |  |
