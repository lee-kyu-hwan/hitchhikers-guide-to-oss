# #6 2단계 — Codex 백엔드 (오프라인 구현 → VM 비AI 사례 → 실제 호출)

- 이슈: https://github.com/lee-kyu-hwan/hitchhikers-guide-to-oss/issues/6 「2단계 — Codex 백엔드」, S3~S5.
- 기준: `origin/main` `8a21bcc`(PR #26 staged 측정 병합) 위로 rebase. 브랜치 `lee-kyu-hwan/6-codex-backend`.
- 1단계 기록(어댑터 구조, fail closed 규칙): `docs/development/2026-10-11-6-research-backends/README.md`.
- 실제 Codex 호출: 아래 「실제 호출」 절(구독 인증, 최대 3회). 유료 API 비용 0 USD.

## 1. 조사 근거 (읽기만)

호스트 `codex-cli 0.162.1`(npm `@openai/codex`, `~/.config/nvm/.../bin/codex`)을 **빈 run 전용 `CODEX_HOME`·`HOME`**으로
`--version`·`--help`·`exec --help`·`login --help`·`features list`·`doctor --help`·`debug prompt-input`만 실행했다.
`~/.codex`는 읽지 않았다. 소스는 태그 `rust-v0.162.1`(`openai/codex`의 `codex-rs/`)을 읽기만 했다.

| 항목 | 결론 | 근거 |
|---|---|---|
| 비대화형 실행 | `codex exec --json --ephemeral --skip-git-repo-check --ignore-rules --ignore-user-config --sandbox read-only --model M -c model_reasoning_effort="E" -- PROMPT`. exec에는 `-a`가 없고 approval은 exec가 `never`로 고정한다 | `exec --help`, `exec/src/lib.rs` |
| 도구 없는 모드 | 환경 `CODEX_EXEC_SERVER_URL=none`이면 실행 환경이 없어 셸·apply_patch·view_image·request_permissions 도구 자체가 생기지 않는다. 나머지(웹 검색, 이미지 생성, sub-agent, cloud skills, goals, sleep, request_user_input, apps MCP, plugin sync, analytics·Statsig)는 `--disable`·`-c`로 끈다(`CODEX_DISABLED_FEATURES`, `CODEX_CONFIG`) | `core/src/tools/spec_plan.rs`, `exec-server/src/environment.rs` |
| 설정 홈 | `CODEX_HOME`=run/`home/.codex`(새 디렉터리). `--ignore-user-config`·`--ignore-rules`·`project_doc_max_bytes=0`·`skills.*=false`로 config.toml·.rules·AGENTS.md·스킬을 읽지 않는다 | `config/src/loader`, `core/src/agents_md.rs` |
| 인증 | ChatGPT 로그인 파일 `$CODEX_HOME/auth.json`(`auth_mode`, `OPENAI_API_KEY`, `tokens{id_token, access_token, refresh_token, account_id}`, `last_refresh`). 환경 `CODEX_API_KEY`는 유료 API라 자식에 넘기지 않는다(허용 목록 밖). 토큰 갱신은 `auth.openai.com/oauth/token`이고 갱신 시 파일을 다시 쓴다(access 만료 5분 전 또는 last_refresh 8일 경과) | `login/src/auth/storage.rs`, `manager.rs` |
| 모델 endpoint | `chatgpt.com/backend-api/codex`(`/responses` WebSocket 우선, 실패 시 HTTPS, `/models` 목록, `accounts/check` workspace routing). TLS는 rustls native roots → `SSL_CERT_FILE` | `model-provider-info`, `http-client/README.md` |
| 관찰 모델 | `--json` JSONL에는 모델 필드가 없다. 서버 응답 헤더 `openai-model`을 세션이 로그로 남긴다: `RUST_LOG=…,codex_core::session=info`면 stderr에 `server reported model X (matches requested model)`, 다르면 warn과 JSONL `error` item `model rerouted: A -> B` | `core/src/session/mod.rs`, `exec/src/event_processor_with_jsonl_output.rs` |
| Linux 샌드박스 | 0.162의 Linux 샌드박스는 Landlock이 아니라 bubblewrap(시스템 또는 내장). 도구가 없으므로 시작되지 않는다 → srt(bwrap) 안 중첩 문제는 해당 없음. Codex 샌드박스는 끄지 않았다(`--dangerously-bypass…` 없음) | `linux-sandbox/README.md` |
| Linux arm64 배포물 | GitHub 릴리스 `rust-v0.162.1`(2026-10-09) 자산 `codex-aarch64-unknown-linux-musl.tar.gz` 103,595,962 B, 릴리스 digest = 받은 파일 sha256 `bf3ca2719f1143b1440fbd85c149379e696c782c50ad7991f8bfcec60b2600c1`(호스트 대조 일치). 안의 정적 musl 바이너리 sha256 `071cbe8d821cf1b02b6d8ba6c8b7991b228fcb8519dcb68a37d31222bb8cabe5` | `gh api … releases/tags/rust-v0.162.1`, `shasum` |

조정자 결정(2026-10-13): 위 1~6 설계 승인, 모델 `gpt-6.1-sol`/`medium`, 인증은 사용자가 별도 터미널에서
`CODEX_HOME=~/.local/share/oss-research-credentials/codex-profile1 codex login --device-auth`(주 로그인 `~/.codex`와
별도 토큰 계열)로 만들고, 호스트에서는 stat·`grep -c`만 하며 게스트에는 stdin으로 넘긴다.

**의도치 않은 접속 1건(보고)**: argv 파싱 확인을 위해 호스트에서 빈 `CODEX_HOME`(로그인 없음)으로 제품 argv를 한 번
실행했더니 Codex가 인증 없이 `api.openai.com/v1/responses`(WebSocket→HTTPS)에 접속해 401로 거부됐다(모델 응답·비용
없음, 프롬프트는 `Reply with exactly OK`). 그래서 실행기는 **로그인 파일 없는 Codex run을 시작 전에 막는다**
(`run_research`: "a codex run needs its ChatGPT login file"), 허용 목적지도 `chatgpt.com`뿐이다.

## 2. 바뀐 것

| 파일 | 변경 |
|---|---|
| `bin/oss-research-worker` | `Backend`에 `profiles`·`credential_file`·`environment`·`system_read_linux`·`config_unwritable`(기본값 있음, Claude는 그대로). `CODEX`: `codex-profile1`, `auth.json`(파일 전달), `CODEX_HOME`=`home/.codex`, Linux `chatgpt.com:443`, 고정 변수 `CODEX_EXEC_SERVER_URL=none`·`SSL_CERT_FILE`·`RUST_LOG`, 읽기 추가 `/etc/ssl/certs/ca-certificates.crt`, `tmp` 쓰기 금지. `build_codex_command`(도구 없는 exec, dontAsk만, 모델·effort 필수), `load_codex_credential`(형태 검사, API key·PAT·agent identity·Bedrock 거부, 세 토큰을 정확값 비밀로), `codex_credential_problems`(run 안에 갱신이 필요하면 거부), `Credential.secrets()`/`found_in`(노출 검사가 토큰 셋을 각각 찾음), `write_login`/`remove_login`(부모가 run에 쓰고 run 뒤 sha 불변 확인 후 삭제, 바뀌면 `credential-file-changed`), `codex_events`·`observed_models`(JSONL 파서, stderr 서버 모델 줄), `MEASURED_BACKEND_LAUNCHES`(빈 표; Codex는 S4 통과 뒤 등록), `--profile`에 `codex-profile1`(백엔드와 짝이 안 맞으면 exit 2) |
| `bin/oss-research-egress-pin` | `MODEL_NAMES["codex"] = ("chatgpt.com",)` |
| `share/linux/model_once.sh` | MODE `codex-once`·`codex-analysis`(로그인 파일 200..65536 B를 tmpfs `codex-profile1/auth.json`에 설치, PATH에 sha 고정 codex, wrapper `--backend codex`; codex sentinel은 `--egress none`만) |
| `share/linux/model_once_verdict.py` | `scan`이 auth.json이면 세 토큰을 각각 찾음, `judge`의 Codex 판정(R4~R6, V1~V5: 관찰 모델 = `gpt-6.1-sol`과 서버 모델 로그 줄, 텍스트 항목만, 로그인 사본 삭제) |
| `tests/fixtures/oss_research_worker/linux/unit_inner.py` | `codex-probe CASE`(S3 비AI), `codex-once`(S4), `codex-analysis PROMPT`(S5) |
| `tests/fixtures/oss_research_worker/linux/codex_cases.sh` | 신설: 게스트 설치(tarball·바이너리 sha 대조 뒤 root 0755)와 비AI 사례 실행 |
| `skills/…/analysis/verify_e2e_result.py`, `selftest_verify.py` | 조정자 승인 범위: M4 기대 형태를 manifest `backend`별로(codex만 `{file: home/.codex/auth.json, clean}`; backend 없음·claude는 바이트 그대로). selftest 사례 +6 |
| `tests/test_oss_research_worker_codex.py` | 신설 28개(아래) |
| `tests/test_oss_research_worker_backends.py` | Codex가 더 이상 "이름만"이 아님(agy만 미측정), 게이트 사례는 백엔드별 프로필·자격 파일, egress pin `codex` 이름 |
| `docs/oss-research-worker.md` | 「백엔드」 절에 Codex |

**Claude 경로 불변**: `ClaudePathUnchangedTests`의 해시 17건이 그대로 통과한다(rebase 뒤 main의 `argv_corpus-extract` 새 값 포함).
Claude는 `profiles`만 `PROFILES`로 채웠고 나머지 새 필드는 기본값(`None`·빈 튜플)이라 argv·정책·환경이 같다. `open_credential`은
Claude일 때 이름·프로필 목록·크기 한계를 그대로 쓴다. 노출 검사는 Claude에서 비밀이 토큰 하나라 동작이 같다.

**staged와의 관계**: rebase로 들어온 PR #26 변경은 `build_staged_command`(corpus-extract `--agents` 인라인)뿐이다. Codex run은
그 함수를 거치지 않는다: `run_research`가 Codex의 staged 요청을 먼저 막고(`backend codex has no staged profile`),
`build_staged_command`는 그대로 `require_measured_argv`(Claude만)다. 시험 `test_argv_refusals`·`test_refused_before_any_directory`.

## 3. 오프라인 시험 (`tests/test_oss_research_worker_codex.py`, 28개)

| 묶음 | 고정하는 것 |
|---|---|
| `CodexAdapterTests` 5 | 백엔드 필드 표; argv 전체(도구 끄기·설정·`--`, bypass·approval·workspace-write·`--search` 없음); auto·selection 없음·Codex 고유 effort(ultra) 거부, staged argv 거부; 환경(부모의 `CODEX_API_KEY`·`OPENAI_API_KEY`·`CODEX_ACCESS_TOKEN`·`RUST_LOG`·`SSL_CERT_FILE` 미상속, 자격 변수 없음, `CODEX_HOME`이 TMPDIR 밖); 정책(`chatgpt.com:443`, CA 파일, denyWrite에 `home/.codex/tmp`·`auth.json`, darwin Blocked, 비AI probe 정책엔 endpoint 없음) |
| `CodexCredentialTests` 4 | 정상 파일의 비밀 셋·repr 비노출; API key·`auth_mode≠chatgpt`·PAT·agent identity·Bedrock·토큰 누락/형식/중복·exp 없음·last_refresh·JSON 아님·중복 키 거부(메시지에 값 없음); 파일 검사(이름·프로필 짝·mode·링크 수·크기·다른 프로필 이름 상위·입력 안); 신선도(만료 임박·8일·미래) |
| `CodexResultTests` 3 | 완료 턴·마지막 agent_message; reconnect error 이벤트·WebSocket fallback·Code Mode 알림은 비치명; 도구 item·reroute·기타 error item·turn.failed·미완료·뒤따르는 이벤트·빈 답·JSONL 아님 실패; 관찰 모델(서버 로그 줄·reroute, Claude 모양과 섞이지 않음) |
| `CodexRunTests` 5 | 가짜 srt·codex로 `run_research`: 자식이 보는 로그인 사본(0600, 내용 sha)·run 뒤 삭제·manifest credential `{file, clean}`·관찰 모델; 서버 줄 없으면 관찰 모델 없음; 토큰을 home에 복사하거나 stderr에 쓰면 노출 실패; 로그인 사본을 고치면 `credential-file-changed`; 갱신 임박·로그인 없음·staged는 디렉터리 전 Blocked |
| `CodexGateTests` 4 | 측정 launch 없음(첫 문제 `backend codex is not the measured claude`); 등록되면 정확히 그 launch만 열림; 게이트는 로그인 파일을 읽지 않음; CLI 프로필·백엔드 짝 |
| `CodexHarnessScriptTests` 2 | `model_once.sh` codex 모드 입력 검사(sentinel은 none만, analysis는 PROMPT 필수)와 argv, `codex_cases.sh` sha 상수 |
| `CodexVerdictTests` 2 | Codex 판정 11항목 PASS·각 반례 FAIL, sentinel은 R4~R6만; scan이 세 토큰 각각을 찾음 |
| `CodexInnerTests` 3 | `codex_once` 계약·prompt 거부(읽기 전), 제품 경로 인자(백엔드·프로필·300 s·dontAsk·selection)와 요약이 판정기 PASS, prompt-input 사실 추출 |

## 4. VM 비AI 사례 (S3)

조정자 승인(2026-10-13 23:40Z, 모델 0·토큰 0) 뒤 게스트 `oss-research-debian`에서 실행했다. 커밋 `c27010c`(rebase 뒤,
`8a21bcc` 위)를 root 소유 `/opt/oss-research/src-c27010c`로 배포(`files_not_root 0 group_other_write 0`, 추적 파일 sha 목록
호스트와 동일), tarball sha 대조 뒤 `/opt/oss-research/codex-0.162.1/codex`(root:root 0755, sha `071cbe8d…abe5`) 설치.
모든 사례는 wrapper `--entry verify`(조사 사용자, unit 안)로 돌았다.

| 사례 | 구성 | 결과 | 판정 |
|---|---|---|---|
| C1 version | 정책 `model=False`(endpoint 없음), egress none | `codex-cli 0.162.1`, exit 0 | PASS |
| C2 exec --help | 같음 | exit 0, help 본문 | PASS |
| C3 prompt-input | 제품의 `--disable`·`-c`로 `codex debug prompt-input`, 입력에 AGENTS.md·`.codex/config.toml`·`.agents/skills`·`.rules` canary | 모델 입력 3항목(permissions·collaboration_mode·environment_context), 스킬·multi-agent·apps·canary 없음, `sandbox_mode read-only` | PASS |
| C4 login status | 빈 `CODEX_HOME` | `Not logged in`(exit 1): 다른 인증 저장소를 보지 못함 | PASS |
| C5 escape | Codex 정책 안의 bash 내장으로 범위 밖 14곳 읽기·나열 | 첫 실행은 probe 결함(`head` 없음, 디렉터리 open이 성공)으로 판정 불가 → 수정 뒤 재실행(`cf60939`): `/etc/passwd`·`/etc/shadow`·`/home/ossresearch`·`/home/vmadmin/.codex`·`/run/oss-research-credentials`·증거 경로 등 부재, `/proc/1/environ` 거부, `/root`·`/home`은 빈 디렉터리, `/opt/oss-research`는 허용 경로의 상위 마운트 지점 이름 2개(`codex-0.162.1`, `srt-0.0.77`)만, CA 파일만 읽힘 | PASS(재실행) |
| C6 ablation | 읽기 집합 하나씩 빼고 version·prompt-input | `/usr/lib`·`/usr/bin/bash` 빼면 srt의 bash가 못 뜸(exit 1); `/usr/bin/socat`·`/etc/hosts`·CA 파일은 비네트워크 명령에는 불필요(네트워크 경로는 아래 C7·Claude 측정) | 기록 |
| C7 doctor | `--egress pin --backend codex`(pin apply·verify·clear 0), Codex HTTP 스택이 인증 없이 `HEAD chatgpt.com/backend-api/codex/responses` | CA 있음: provider 도달 ok(srt 프록시+pin 경유 TLS 성공); CA 뺌: provider 도달 fail("one or more required provider endpoints are unreachable over HTTP", network.env는 "custom CA env var points at an unreadable path") → CA 파일이 필요. WebSocket 도달은 두 경우 모두 warning(인증 없음) | PASS |
| 하네스 sentinel | `model_once.sh sentinel … none … codex-once`(가짜 로그인 stdin, 외부 접속 없음) | 첫 실행: run 실패 사유에 `symlink in home: home/.codex/tmp/arg0/…`(Codex가 시작마다 자기 바이너리로의 symlink 4개를 만든다) → **제품 수정**(459bd46: `CODEX_HOME/tmp`를 부모가 만들고 srt denyWrite, Codex는 "could not create PATH aliases" 경고 뒤 진행). 재실행: 하네스 fails=0(reaper·설치·scan clean·증거·정리 PASS, `login_copy_left false`), run은 네트워크가 없어 예상대로 실패. 결과 묶음의 첫 item이 `Code Mode is unavailable … will fail closed`(gpt-6.1-sol 메타데이터 `tool_mode: code_mode_only`, host 바이너리 없음) → **파서 수정**(13bfb10: 알려진 비치명 알림) | 하네스 PASS, 발견 2건 수정 |

외부 접속은 C7의 인증 없는 HEAD(CA 있음/없음 2회 실행)뿐이다. 전후 units 0, procs 0, creds 0, pin 없음, `/tmp/osr-c-*` 0.

증거(SHA256SUMS 봉인, `shasum -a 256 -c` 전부 OK):

| 디렉터리 | SHA256SUMS sha256 | 핵심 파일 |
|---|---|---|
| `~/.oss-research-vm/logs/a6-codex-s3-20261012T234041Z/`(24 entries) | `c857b4ccb32c09dde492d50dd7a65a3f03eb685d51b58f51c1a7c15673bdaa82` | `guest-codex-cases.log` `8a288742…a5d`, `guest-codex-sentinel.log` `57da10a7…9b589`, `codex-install.log` `d83b9748…a1e2` |
| `~/.oss-research-vm/logs/a6-codex-s3b-20261012T234520Z/`(26 entries) | `3ff5e7f649385df2426565ca033fe9e94335b314eaa0356813841be6680f2a4d` | `guest-codex-cases.log` `14dee361…827a`, `guest-codex-sentinel.log` `d75238c7…4fc9`, `sentinel-result/stdout.json`(가짜 로그인 run의 이벤트) |

## 5. 실제 호출 (S4, S5)

준비된 것(실행 전 조정자 승인 필요, 구독 인증, 최대 3회):

- 인증 파일: `~/.local/share/oss-research-credentials/codex-profile1/auth.json`(0600, 디렉터리 0700), 사용자가 별도 터미널에서
  `install -d -m 700 ~/.local/share/oss-research-credentials/codex-profile1 && CODEX_HOME=~/.local/share/oss-research-credentials/codex-profile1 codex login --device-auth`.
- S4: `model_once.sh token <src> /opt/oss-research/srt-0.0.77 <node bin> pin normal 330 300 codex-once < auth.json`
  → 판정 `judge`(R1~R6, V1~V5), 회수 `verify_pickup.py`, 로그·산출물의 토큰 셋 `grep -c` 0.
- S5: `… codex-analysis <PROMPT>`, PROMPT = 2026-10-04 r2 실행의 `extract-blind-04/task.txt`(17,695 B, Claude로 strict evidence
  통과), 검증 `verify_e2e_result.py --answer-kind extract --lens blind --corpus <r2 corpus> --limits <codex worker 값의 limits>`.

결과: (S4·S5 실행 뒤 채운다)

## 6. 로컬 Linux CI

`docs/ci.md` 방식(Docker `python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root `runner`),
2026-10-13, 한 번에 하나만 실행, 트리 = 커밋 `9e9c510`(이 CI 문단 앞).

| 묶음 | Linux |
|---|---|
| 저장소 시험 `tests/test_*.py` 22모듈 | **737개 OK (skip 40)**; 모듈별 모두 OK(`worker_codex` 28 신설, `worker_backends` 24, `worker` 43, `worker_auth` 47, `worker_skills` 40, `worker_staged_harness` 13, `worker_linux` 174, `egress_pin` 26, `relay` 57) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2), `selftest_verify` failures=0(사례 +6: M4 백엔드별), kinds 129 cases 0, scope 19 cases 0 |
| 스킬 시험 6묶음 | **659개 OK** (researching 162, analyzing 77, verifying 138, recent-closed 162, curated 53, issues 67) — 변화 없음 |
| `__pycache__` | 0 |

이전 기준(main `8a21bcc`: 저장소 709 = PR #23의 708 + PR #26의 skills 시험 1)과 비교하면 저장소 +28(`worker_codex`),
스킬 수 변화 없음(분석 번들 selftest 사례만 +6, 단위 시험 수 아님).

## 7. 남은 항목

- S4·S5(위). S4 통과 뒤 `MEASURED_BACKEND_LAUNCHES[("codex", "linux")]` 등록.
- 3단계 agy.
