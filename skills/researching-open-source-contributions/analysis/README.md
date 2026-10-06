# 격리 분석 번들 (analysis)

분석 단계를 격리 모델 호출로 나눠 돌리는 호스트 쪽 도구다. 수집 corpus로 만든 workflow run에서 단계별 입력(추출·종합·합침·검증·최종, 후보 판정)을 만들고, 모델 답을 strict 검사해 채택하고, 결정적으로 조립한다. 호출 하네스는 원장·call_plan 결속으로 호출 수를 지킨다. 계약과 결정의 기록은 `docs/research/oss-e2e-2026-09-28/README.md`다(2026-09-28 실험 번들에서 시작했고 #242~#273을 거쳐 실제 분석 경로가 됐다).

## 무엇이 어디에 있나

| 파일 | 하는 일 |
|---|---|
| `stage_inputs.py` | 단계 입력 생성(`extract-inputs`, `dry-plan`, `synthesis-input`, `synthesis-merge-input`, `continue-plan`, `verify-inputs`, `final-input`, `assess-inputs`)과 결정적 조립(`assemble`) |
| `verify_e2e_result.py` | 모델 답 strict 검사와 채택(`--answer-kind extract|synthesis|verify|final|assess`) |
| `assess_contract.py` | 판정 필드 타입 표(verifier record 검사와 동기화) |
| `prompts/` | 종합·합침·최종·판정 지시문(상한은 `{{키}}`로 `run_limits.json`에서 채운다) |
| `run_limits.json` | strict 상한과 worker 선택값 |
| `request.template.json` | workflow 요청 틀 |
| `prepare_input.sh` | 수집 산출물 → blind 추출 입력 1개 |
| `harness/analysis_call.sh` | 호스트에서 격리 모델 호출 1회와 원장(호출 상한·call_plan 결속·이어받기 체인) |
| `test_*.py`, `selftest_*.py` | 시험(모델·VM·네트워크 없음, 일부는 이 머신의 증거가 있을 때만 돈다) |

## 실행

- 저장소 체크아웃에서 쓴다. 형제 스킬(workflow `scripts/oss_workflow.py`, analyzer·verifier)과 격리 스택(`tests/fixtures/oss_research_worker/linux/`), 실행 기록(`docs/research/oss-e2e-2026-09-28/`)을 저장소 상대 경로로 읽기 때문이다. 그래서 `.chezmoiignore`가 이 `analysis/` 하위 트리를 $HOME 배포에서 뺀다(스킬 본체 `SKILL.md`·`scripts/`·`references/`·`tests/`는 그대로 배포된다).
- 시험은 모듈마다 따로 돌린다(같은 프로세스에서 여러 모듈을 discover하면 offline guard가 하네스 시험의 하위 프로세스를 막는다). CI의 스킬 시험은 `<스킬>/tests`만 찾으므로 이 시험들은 CI 밖이다(옮기기 전과 같다).

```sh
for f in dot_agents/skills/researching-open-source-contributions/analysis/test_*.py; do /opt/homebrew/bin/python3 -B -m unittest "$f"; done
for f in dot_agents/skills/researching-open-source-contributions/analysis/selftest_*.py; do /opt/homebrew/bin/python3 -B "$f"; done
```

## 옛 경로 대응(#261)

| 옛 경로 | 새 경로 |
|---|---|
| `docs/research/oss-e2e-2026-09-28/stage_inputs.py` | `dot_agents/skills/researching-open-source-contributions/analysis/stage_inputs.py` |
| `docs/research/oss-e2e-2026-09-28/verify_e2e_result.py` | `dot_agents/skills/researching-open-source-contributions/analysis/verify_e2e_result.py` |
| `docs/research/oss-e2e-2026-09-28/assess_contract.py` | `dot_agents/skills/researching-open-source-contributions/analysis/assess_contract.py` |
| `docs/research/oss-e2e-2026-09-28/prepare_input.sh` | `dot_agents/skills/researching-open-source-contributions/analysis/prepare_input.sh` |
| `docs/research/oss-e2e-2026-09-28/prompts/*.md`(6개) | `dot_agents/skills/researching-open-source-contributions/analysis/prompts/*.md` |
| `docs/research/oss-e2e-2026-09-28/run_limits.json` | `dot_agents/skills/researching-open-source-contributions/analysis/run_limits.json` |
| `docs/research/oss-e2e-2026-09-28/request.template.json` | `dot_agents/skills/researching-open-source-contributions/analysis/request.template.json` |
| `docs/research/oss-e2e-2026-09-28/test_*.py`(9개)·`selftest_*.py`(3개) | `dot_agents/skills/researching-open-source-contributions/analysis/test_*.py`·`selftest_*.py` |
| `tests/fixtures/oss_research_worker/linux/analysis_call.sh` | `dot_agents/skills/researching-open-source-contributions/analysis/harness/analysis_call.sh` |
| `docs/research/oss-e2e-2026-09-28/README.md`, `fixtures/`, `request.json`, `run_limits.legacy-fixture.json` | 그대로(실행 기록·증거) |

## 격리 스택 (#274에서 옮김)

#261 때 `tests/fixtures/oss_research_worker/linux/`에 남겼던 격리 스택의 실행 파일은 #274에서 `dot_local/share/oss-research/linux/`로 옮겼다. 하네스(`harness/analysis_call.sh`)는 그 경로(호스트 `$REPO/dot_local/share/oss-research/linux/…`, 게스트 `$S/dot_local/share/oss-research/linux/model_once.sh`)를 부른다. 게스트 수락 시험(`unit_inner.py`·탐침·사례 스크립트)과 시험 데이터는 시험이라 `tests/fixtures/`에 남는다. 구분표와 결정은 `dot_local/share/oss-research/README.md`.

| 옛 경로 | 새 경로 |
|---|---|
| `tests/fixtures/oss_research_worker/linux/verify_pickup.py` | `dot_local/share/oss-research/linux/verify_pickup.py` |
| `tests/fixtures/oss_research_worker/linux/model_once.sh` | `dot_local/share/oss-research/linux/model_once.sh` |
| `tests/fixtures/oss_research_worker/linux/model_once_verdict.py` | `dot_local/share/oss-research/linux/model_once_verdict.py` |
| `tests/fixtures/oss_research_worker/linux/collect_live.sh` | `dot_local/share/oss-research/linux/collect_live.sh` |
| `tests/fixtures/oss_research_worker/linux/candidate_reads_live.sh` | `dot_local/share/oss-research/linux/candidate_reads_live.sh` |
