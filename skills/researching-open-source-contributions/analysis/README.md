# 격리 분석 번들 (analysis)

분석 단계를 격리 모델 호출로 나눠 돌리는 호스트 쪽 도구다. 수집 corpus로 만든 workflow run에서 단계별 입력(추출·종합·합침·검증·최종, 후보 판정)을 만들고, 모델 답을 strict 검사해 채택하고, 결정적으로 조립한다. 호출 하네스는 원장·call_plan 결속으로 호출 수를 지킨다. 계약과 결정의 기록은 `docs/research/oss-e2e-2026-09-28/README.md`다(2026-09-28 실험 번들에서 시작했고 #242~#273을 거쳐 실제 분석 경로가 됐다).

## 무엇이 어디에 있나

| 파일 | 하는 일 |
|---|---|
| `stage_inputs.py` | 단계 입력 생성(`extract-inputs`, `dry-plan`, `synthesis-input`, `synthesis-merge-input`, `continue-plan`, `verify-inputs`, `final-input`, `assess-inputs`)과 결정적 조립(`assemble`) |
| `verify_e2e_result.py` | 모델 답 strict 검사와 채택(`--answer-kind extract|synthesis|verify|final|assess`) |
| `assess_contract.py` | 판정 필드 타입 표(verifier record 검사와 동기화) |
| `prompt_contract.py` | 지시문↔strict 동기화 표(#286): kind마다 strict가 거부하는 조건과 그 kind 지시문이 말하는 문장 |
| `prompts/` | 종합·합침·최종·판정 지시문(상한은 `{{키}}`로 `run_limits.json`에서 채운다) |
| `run_limits.json` | strict 상한, worker 선택값, 추출·검증 답 크기 목표치, 한정 재시도 정책(`model_retry`) |
| `retry_policy.py` | 한정 재시도 정책 하나(#280): call_plan의 재시도 몫, 재시도할 strict 판정 분류, 하네스의 재시도 허용(`admit`) |
| `request.template.json` | workflow 요청 틀 |
| `machine_settings.py` | 이 머신의 설정값(#290): VM 작업 루트(`OSS_RESEARCH_VM_ROOT`)와 날짜별 실행 기록 위치(`OSS_RESEARCH_RECORDS_ROOT`). 지정하지 않으면 분리 전과 같은 값 |
| `prepare_input.sh` | 수집 산출물 → blind 추출 입력 1개 |
| `harness/analysis_call.sh` | 호스트에서 격리 모델 호출 1회와 원장(호출 상한·call_plan 결속·이어받기 체인·재시도 몫, `--retry-of`) |
| `harness/call_with_retry.py` | 계획된 호출 1개: 하네스 호출 → 원장 줄 확인 → strict 검사·채택, 답 내용 위반이면 같은 prompt로 한정 재시도(#280) |
| `test_*.py`, `selftest_*.py` | 시험(모델·VM·네트워크 없음, 일부는 이 머신의 증거가 있을 때만 돈다) |

## 실행

- 저장소 체크아웃에서 쓴다. 형제 스킬(workflow `scripts/oss_workflow.py`, analyzer·verifier)과 격리 스택(`tests/fixtures/oss_research_worker/linux/`, `share/linux/`), 시험이 읽는 실행 기록 사본(`tests/fixtures/dotfiles_records/`)을 저장소 상대 경로로 읽기 때문이다. 그래서 이 `analysis/` 하위 트리는 $HOME에 설치하지 않는다(dotfiles 당시 `.chezmoiignore`로 뺐다. 스킬 본체 `SKILL.md`·`scripts/`·`references/`·`tests/`는 설치된다). 실행 기록 원본은 dotfiles `docs/research/`에 있다.
- 머신 설정값(#290): `OSS_RESEARCH_VM_ROOT`(VM 작업 루트, 기본 `~/.oss-research-vm`), `OSS_RESEARCH_VM_NAME`(Lima 인스턴스, 기본 `oss-research-debian`, 하네스만 읽음), `OSS_RESEARCH_RECORDS_ROOT`(날짜별 실행 기록, 기본 이 저장소의 사본 `tests/fixtures/dotfiles_records`; dotfiles 체크아웃의 `docs/research`를 주면 원본을 읽는다). 비었거나 지정하지 않으면 기본값이다(VM 두 값은 분리 전과 같다). 기본값 고정 시험은 `test_machine_settings.py`다.
- 시험은 모듈마다 따로 돌린다(같은 프로세스에서 여러 모듈을 discover하면 offline guard가 하네스 시험의 하위 프로세스를 막는다). CI의 스킬 시험은 `<스킬>/tests`만 찾으므로 이 시험들은 CI 밖이다(옮기기 전과 같다).

```sh
for f in skills/researching-open-source-contributions/analysis/test_*.py; do /opt/homebrew/bin/python3 -B -m unittest "$f"; done
for f in skills/researching-open-source-contributions/analysis/selftest_*.py; do /opt/homebrew/bin/python3 -B "$f"; done
```

## 한정 재시도와 답 크기 목표 (#280)

2026-10-06 분리 판정 실행(`docs/research/2026-10-06-split-gate-run/`)은 코드 결함 없이 모델 호출 12회 중 2회가 모델 규칙 위반(인용 길이 Q1, 답 크기 R3)으로 strict에서 거부돼 사람 승인 없이는 끝까지 갈 수 없었다. 검증은 그대로 두고, 그런 위반만 정해진 횟수 안에서 흡수한다.

### 재시도 몫 (`run_limits.json` `model_retry`, `retry_policy.retry_share`)

- 정책: 호출당 1회(`per_call`), 단계(extract·synthesis·verify·final)별 최대 3회(`per_stage`, 그 단계 호출 수 × `per_call`를 넘지 않음), 계획 전체 최대 5회(`total`). 원천은 `run_limits.json` 하나다.
- 모든 분석 call_plan(`extract-inputs`·`dry-plan`의 최악 계획, digest-in-final 계획, `continue-plan`)이 `retry` 블록을 갖고, `max_model_calls`(= 원장 한도)는 계획 호출 + 재시도 몫이다. 최악 계획은 이 합으로 절대 상한 40을 검사해, 계획 호출만으로는 들어가도 재시도 몫까지 넣어 넘으면 모델 호출 전에 거부한다(40은 올리지 않고 몫도 깎지 않는다). 판정(assess) 계획은 재시도 몫이 없다.
- `total` 5를 고른 근거: 관측 위반율 2/12(17%)를 21건 계획 27회에 적용하면 약 4.5회다. 그리고 예전에 돌 수 있던 가장 큰 묶음(eslint 28건, 계획 35회)이 몫을 더해도 40 안에 남는 가장 큰 값이다. `per_stage` 3은 추출 한 단계가 몫을 다 써서 뒤 단계가 재시도를 못 하는 일을 막는다.
- 하네스는 몫을 재시도에만 쓰게 한다: `--retry-of` 없는 호출은 원장에 계획 호출 수(한도 − 몫)만큼 쌓이면 거부한다(exit 3).

### 무엇을 재시도하나 (`retry_policy.classify`, `admit`)

- 재시도한다: extract·synthesis·verify·final 답의 strict 판정이 답 내용 항목에서만 FAIL일 때(`R2` JSON 아님, `R3` 모양·크기·개수, `R5` 입력 범위, `R6` 검색 단서, `Q1` 인용). 그 실패에 막힌 BLOCKED 항목은 괜찮다.
- 재시도하지 않는다: 하네스 exit(staging·transport·로그 미확인·원장 한도·call_plan 결속), 깨끗하지 않은 원장 줄(측정 launch, pickup·judge, 관측 모델·권한 모드), 다른 항목 FAIL(C·P·S·M·R0·R1·A), assess 답, legacy 판정, ok인데 채택 실패, gate(비용) 거부.
- 재시도는 같은 prompt 바이트로만 한다(앞 호출의 원장 `input_sha256`·`prompt_sha256_recorded`, 판정 P3가 이 prompt sha와 같아야 함). 재시도 답도 같은 strict 검사를 거친다. prompt·답·상한을 고치지 않는다.
- 이름: 첫 호출 `NAME`, 재시도 `NAME-r2`(호출당 1회이므로 r2까지). `analysis_call.sh --retry-of NAME ... NAME-r2 ...`가 `retry_policy.py admit`으로 허용을 묻고, 원장 줄에 `attempt`, `retry_of`(첫 호출), `previous`, `retry_stage`, `failure_sha256`(앞 호출 `verdict.json`의 sha256), `failed_items`를 더 적는다. 일반 호출 줄은 예전 그대로다.
- `harness/call_with_retry.py`가 이 순서를 한 호출 단위로 돈다: `--gate`(예: 비용 확인) → 하네스 → 원장 줄 확인(run_limits worker의 모델·권한 모드) → `verify_e2e_result.py`(strict, `--out <호출>/verdict.json --adopt-to`) → 재시도할 수 있으면 같은 prompt로 다시. exit 0 채택, 10 깨끗하지 않은 호출, 11 재시도 안 하는 판정이거나 몫 소진, 12 gate 거부.

### 이어받기와의 맞물림 (#82·#258·#262)

- 재시도로 채택된 답은 그 시도(`<호출>-r2`)의 기록으로 결속된다(`adopted_extract_answer`·`adopted_reduce_answer`가 첫 호출부터 시도 순서로 찾는다). 분리 판정 실행에서 조정자 승인으로 손으로 다시 부른 `extract-structure-01-r2`도 같은 규칙으로 결속된다.
- 추출 도중 멈춘 실행(부분 이어받기): `continue-plan`에 채택된 추출 답 일부만 주면, 남은 추출 prompt(추출 stage가 기록한 바이트·sha, `pending`)와 뒤 단계 전부(최악 계획의 단계 수)와 그 재시도 몫으로 계획을 만든다. 하네스는 `call_plan.extract + completed.extract = shards × lenses`를 확인한다. 이 continue stage를 다시 이어받을 때는 남겼던 추출 답을 부모 원장(그 호출을 한 원장)의 기록으로 결속한다.
- 하네스의 체인 상한(조상 + 부모 호출 + 이 한도 ≤ 40)은 그대로다. 부모의 재시도 호출도 부모 호출 수에 들어간다.

### 답 크기 목표 (`extract_answer_target_bytes` 6,500)

- strict 검사 값은 바꾸지 않는다: `extract_answer_max_bytes` 7,680, 측정은 답을 들여쓰기 1칸으로 다시 쓴 크기 그대로다.
- 고른 방법: 지시문에 여유를 둔 목표치를 더한다(분석기 `build_prompts.py extract --answer-target-bytes`, 크기 상한 문장 바로 뒤 한 줄). 측정 기준을 "모델이 낸 바이트"로 바꾸는 안은 검사 방법을 바꾸는 것이라 고르지 않았다. 모델은 자기가 낸 바이트도 정확히 세지 못하므로 같은 기준으로 여유를 주는 쪽이 검사와 지시문이 같은 수를 말하는 규칙(#248)을 지킨다.
- 6,500의 근거: 분리 판정 실행의 위반 답은 공백 없이 7,300 B였고 들여쓰기 1칸으로 다시 쓰면 8,342 B(+14%)였다. 7,680 / 1.15 ≈ 6,680이므로 그보다 조금 낮게 잡았다. 지시문은 "공백 없이 내도 다시 써서 재므로 15% 안팎 커진다"고 함께 알린다.
- 목표치는 `run_limits.json` 한 곳에서 오고, 크기 상한이 없거나 목표가 상한 이상이면(옛 상한·시험용 작은 상한) 문장을 넣지 않는다. 추출 stage.json이 `extract_answer_target_bytes`를 기록하고, `continue-plan`은 기록된 값(없으면 목표 문장 없이)으로 prompt를 다시 써서 바이트를 비교한다. 그래서 #280 이전 추출 stage(분리 판정 실행)도 바이트 그대로 이어받는다. 인용 길이처럼 지시문에 이미 있는데도 어긴 규칙은 바꾸지 않고 재시도로 흡수한다.

### 기록값 (오프라인, 모델·VM·GitHub 0)

| 항목 | 값 |
|---|---|
| 21건 corpus(분리 판정 실행과 같은 수집 6개) dry plan | 추출 15 + 종합·합침 8(5 + 2 + 1) + 검증 3 + 최종 1 = 계획 27, 재시도 몫 5(extract 3·synthesis 3·verify 3·final 1, 전체 5) → **최악 32 ≤ 40**. 추출 최악 prompt 58,943 B(목표 문장 247 B 증가, 실행 당시 58,696 B) |
| 분리 판정 실행 이어받기(`continue-plan`, 채택 답 10개 바이트 그대로, `--records` = 그 원장) | 남은 추출 5(`extract-blind-03`, `extract-{correctness,maintenance,review,structure}-03`, 기록된 prompt 바이트) + 종합·합침 8 + 검증 3 + 최종 1 = 17, 재시도 몫 5 → **한도 22**. 부모 원장 12회 + 22 = 34 ≤ 40. `extract-structure-01`은 `extract-structure-01-r2`의 기록으로 결속. 하네스 사본(가짜 VM)이 이 계획과 부모 원장 사본을 받아 한도 22·부모 12회를 기록함 |
| 남은 위험 | 남은 추출 5개는 기록된 prompt 바이트(목표 문장 없음)로 불러야 바이트 그대로 이어받으므로, `extract-blind-03`의 답 크기 위반은 목표 문장이 아니라 재시도 1회로만 흡수된다. 새 실행은 목표 문장이 든 prompt를 쓴다 |

시험: `test_model_retry.py`(S1~S3: 재시도 후 채택·원장 2줄, 호출당·단계·계획 몫 소진 뒤 멈춤, transport·로그·결속·관측 모델·gate는 재시도 없음, 허용 거부 사례, 분리 판정 실행의 실제 판정 분류), `test_dry_plan.py`(S4: 21건 32회, 몫 때문에 넘는 계획 거부), `test_continue_plan.py`(부분 이어받기, 재시도 시도 결속, 분리 판정 실행 증거), `tests/test_oss_research_worker_linux.py`(원장: 몫 포함 한도, 몫 형식 거부, 부분 이어받기 계획 수용), 분석기 `tests/test_extraction.py`(목표 문장).

## 지시문↔strict 동기화 (#286)

strict가 답을 거부하는 이유는 모두 그 답의 지시문이 말한 규칙이어야 한다. 분리 판정 새 실행은 검증 첫 호출에서 이 계약 결함 2개로 멈췄다(지시문을 따른 목록 전용 반례의 빈 `pr`이 R5에 걸림, 지시문에 없는 검증 답 크기 5,120 B). 결정과 기록값은 `docs/research/oss-e2e-2026-09-28/README.md` 3.1h에 있다.

- 목록에만 있는 PR의 반례는 `quote` 키 없는 `{"pr": "<목록의 키>", "reason": "…"}`다. 검증 scope의 `listed`가 그 키를 적고, R5가 인용 없는 반례를 그 키로만 받는다. 범위 밖 키, 빈 키는 여전히 FAIL이다.
- 검증 지시문은 크기 상한 5,120 B(출력한 바이트 그대로)와 목표 4,300 B(`verify_answer_target_bytes`), 빈 `evidence_checks` 금지를 말한다. 검사 값은 그대로다.
- `prompt_contract.py`(표)와 `test_prompt_contract.py`(시험): 표의 모든 줄이 그 kind의 모든 지시문에 있는지(S3), 변형한 답에서 strict가 내는 거부 문구가 모두 표에 있는지, 지시문대로 쓴 경계 답이 strict를 통과하는지(S4) 본다. strict에 거부를 더하면 표와 지시문도 함께 고쳐야 시험이 통과한다.
- 분리 판정 새 실행 이어받기: `continue-plan`에 추출 stage와 run의 종합 답(`--synthesis-result .../synthesis.result.json`)을 주면, 그 stage에 묶인 원장에서 추출 답 15개와 종합 답을 바이트 그대로 결속하고 검증부터 계획한다. 검증 3 + 최종 1 + 재시도 몫 4 = 한도 8, 부모 22 + 8 = 30 ≤ 40. 검증 prompt는 `verify-inputs`가 새 지시문으로 다시 쓴다.

## 판정 입력의 렌더링 (#11)

`assess-inputs`는 discovery 저장값을 바꾸지 않고 판정 prompt의 블록만 판정용으로 줄여 보인다. 2026-10-08 eslint/css discovery(패턴 2 × 저장소 1, 질의당 100건 페이지를 돌려준 짧은 단서 `http://`·`:::note`, 검색 항목 350건)는 원본 그대로 넣으면 292,478 B로 65,536 B 상한을 넘었고, 항목 밖 고정부만 56,077 B였다.

| 블록 | 렌더링 | 근거 |
|---|---|---|
| 「저장소 사실」 | `static_search`를 저장소 사실로 올려 조합마다 반복하지 않는다(`REPOSITORY_FACT_KEYS`). 조합의 값이 다르면 그 「discovery 관찰」에 남는다 | 정적 읽기는 저장소의 head_sha에서 한 번 읽은 것이고, 최상위 `static_reads`는 모든 레코드의 사본이어야 한다(`static_reads_not_assessable`) |
| `static_search.external_links` | 항목마다 `경로:줄 URL` 한 줄(`assess_static_search`). 공백이 있어 `https_values`가 외부 URL을 허용 링크로 모으지 않는다 | 외부 사이트의 URL은 이 저장소에 대한 근거가 아니다 |
| 「정책 관찰」 | `{"observations": [찾았거나 실패한 관찰 전체], "absent": ["source:path", …]}`(`assess_policy_files`) | 부재 관찰은 다른 필드가 모두 null·false·빈 값이고, 「정책 키별 허용 값」이 같은 부재를 키마다 적는다 |
| `duplicate_search` | 질의마다 `q`·`total_count`·`incomplete_results` 보존 + `received`·`shown`·`truncated`. 항목은 앞 `ASSESS_SEARCH_ITEMS_PER_QUERY`(3)개만 `URL 종류 상태[:사유] [closed_at=…] \| 제목` 한 줄씩(`assess_duplicate_search`, `assess_search_row`). 0건 질의는 `empty_queries`. 전체 수는 `queries_total`·`items_received`·`items_shown` | N=3의 근거는 `stage_inputs.py`의 상수 주석(실측 3개 63,288 B, 4개 64,596 B, 5개 65,630 B). 보인 항목의 `html_url`만 허용 링크에 들어가므로 보이지 않은 항목은 인용할 수 없다(지시문·`prompt_contract` assess links 행) |

줄여도 상한을 넘으면 전과 같이 쓰기 전에 거부한다(메시지의 관찰 블록 크기는 줄인 뒤의 크기다). 지시문은 잘린 목록으로 「중복 없음」을 단정하지 않도록 `truncated`를 적게 하고 미결(`unverified` / `insufficient-evidence`)을 정상 결과로 둔다. 시험은 `test_assess.py`의 `S1SearchSummary`(합성 질의당 100건 레코드 2개)·`S2RefusedAfterSummary`·`SearchSummaryViews`, 동기화는 `test_prompt_contract.py`다.

## 옛 경로 대응(#261)

| 옛 경로 | 새 경로 |
|---|---|
| `docs/research/oss-e2e-2026-09-28/stage_inputs.py` | `skills/researching-open-source-contributions/analysis/stage_inputs.py` |
| `docs/research/oss-e2e-2026-09-28/verify_e2e_result.py` | `skills/researching-open-source-contributions/analysis/verify_e2e_result.py` |
| `docs/research/oss-e2e-2026-09-28/assess_contract.py` | `skills/researching-open-source-contributions/analysis/assess_contract.py` |
| `docs/research/oss-e2e-2026-09-28/prepare_input.sh` | `skills/researching-open-source-contributions/analysis/prepare_input.sh` |
| `docs/research/oss-e2e-2026-09-28/prompts/*.md`(6개) | `skills/researching-open-source-contributions/analysis/prompts/*.md` |
| `docs/research/oss-e2e-2026-09-28/run_limits.json` | `skills/researching-open-source-contributions/analysis/run_limits.json` |
| `docs/research/oss-e2e-2026-09-28/request.template.json` | `skills/researching-open-source-contributions/analysis/request.template.json` |
| `docs/research/oss-e2e-2026-09-28/test_*.py`(9개)·`selftest_*.py`(3개) | `skills/researching-open-source-contributions/analysis/test_*.py`·`selftest_*.py` |
| `tests/fixtures/oss_research_worker/linux/analysis_call.sh` | `skills/researching-open-source-contributions/analysis/harness/analysis_call.sh` |
| `docs/research/oss-e2e-2026-09-28/README.md`, `fixtures/`, `request.json`, `run_limits.legacy-fixture.json` | 그대로(실행 기록·증거) |

## 격리 스택 (#274에서 옮김)

#261 때 `tests/fixtures/oss_research_worker/linux/`에 남겼던 격리 스택의 실행 파일은 #274에서 `share/linux/`로 옮겼다. 하네스(`harness/analysis_call.sh`)는 그 경로(호스트 `$REPO/share/linux/…`, 게스트 `$S/share/linux/model_once.sh`)를 부른다. 게스트 수락 시험(`unit_inner.py`·탐침·사례 스크립트)과 시험 데이터는 시험이라 `tests/fixtures/`에 남는다. 구분표와 결정은 `share/README.md`.

| 옛 경로 | 새 경로 |
|---|---|
| `tests/fixtures/oss_research_worker/linux/verify_pickup.py` | `share/linux/verify_pickup.py` |
| `tests/fixtures/oss_research_worker/linux/model_once.sh` | `share/linux/model_once.sh` |
| `tests/fixtures/oss_research_worker/linux/model_once_verdict.py` | `share/linux/model_once_verdict.py` |
| `tests/fixtures/oss_research_worker/linux/collect_live.sh` | `share/linux/collect_live.sh` |
| `tests/fixtures/oss_research_worker/linux/candidate_reads_live.sh` | `share/linux/candidate_reads_live.sh` |
