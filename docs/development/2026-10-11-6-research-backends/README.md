# #6 격리 조사 모델 실행을 Claude·Codex·agy 백엔드로 넓힌다 — 0·1단계 기록

- 이슈: https://github.com/lee-kyu-hwan/hitchhikers-guide-to-oss/issues/6
- 기준: `origin/main` `74e49d6`(v0.1.4). 브랜치 `lee-kyu-hwan/6-research-backends`.
- 이 문서는 0단계(agy Linux 지원 확인, S6)와 1단계(백엔드 어댑터 리팩터, S1·S2)의
  기록이다. 2·3단계(Codex·agy 실호출, VM 설치)는 별도 PR이다.
- 이번 작업의 모델 호출 비용: **0 USD**(실제 모델 호출 없음, VM 사용 없음).

## 0단계 — agy(Antigravity CLI) Linux 지원 확인 (S6)

조사일 2026-10-11. 읽기만 했다(공식 문서·설치 스크립트·호스트 바이너리 `--help`).
agy를 실행해 모델을 부른 적은 없다(`--help`·`--version`·`help <sub>`만).

### 결론

| 항목 | 결과 | 근거 |
|---|---|---|
| Linux arm64 빌드 | **있음** | 설치 스크립트가 `arm64\|aarch64 → arch="arm64"`, `linux_${arch}` manifest를 받는다. manifest `linux_arm64.json`: version `1.3.3`, url `https://storage.googleapis.com/antigravity-public/antigravity-cli/1.3.3-5524738307653632/linux-arm/cli_linux_arm64.tar.gz`, sha512 `1bfd3503…fe430c`. 호스트 바이너리의 플랫폼 enum 문자열에 `LINUX_ARM64`·`LINUX_AMD64`·`DARWIN_ARM64`·`DARWIN_AMD64`·`WINDOWS_AMD64`가 있다. Arm Learning Path(2026-08-04 갱신)도 "Arm Linux binary to `~/.local/bin`"으로 적는다 |
| 비대화형 모드 | `-p`/`--print`/`--prompt`(단일 프롬프트), `--print-timeout`(기본 0 = 무제한), `--input-format stream-json`, `--disable-slash-commands`, `--model`, `--effort low\|medium\|high\|xhigh\|max` | `agy --help`(1.3.3, 아래 발췌), headless 문서 |
| 출력 형식 | `--output-format text\|json\|stream-json`. json 봉투·stream `result` 이벤트 필드: `conversation_id`, `status`(SUCCESS/ERROR/CANCELED/INTERRUPTED/INVALID/WAITING/RUNNING), `response`, `error`, `duration_seconds`, `num_turns`, `usage{input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}`, `denied_actions`(1.1.27). **result에 모델 이름이 없다**; stream `init` 이벤트에만 `model`이 있고 그것도 override 때만 | headless 문서, CHANGELOG |
| 도구 권한 제어 | `~/.gemini/antigravity-cli/settings.json`의 `permissions.allow/deny/ask`(규칙 `command(...)`, `read_file(...)`, `write_file(...)`, `read_url(...)`, `unsandboxed(...)`; Deny > Ask > Allow), `toolPermission`(`request-review` 기본, `proceed-in-sandbox`, `always-proceed`, `strict`), `enableTerminalSandbox`, `allowNonWorkspaceAccess`, CLI `--sandbox`(세션에 sandbox 강제), `--mode accept-edits\|plan`, `--dangerously-skip-permissions`. headless에서 승인 못 받는 도구는 **soft-deny**: 실행은 계속되고 exit 0, stderr에 안내. Claude `dontAsk`에 정확히 대응하는 모드는 없다 | permissions·sandbox·reference·headless 문서 |
| 인증 | 기본은 Google 계정 브라우저 로그인(원격이면 URL 출력·코드 붙이기), 토큰은 secure keyring(`/logout`이 keyring에서 지운다). headless는 "cached credentials"를 쓰고 비대화형에서 미인증이면 `authentication required` 오류로 종료. 대안: `GEMINI_API_KEY`(1.1.13, Gemini API 직접 = 유료 API, 사용자 결정 사항), Application Default Credentials(`AGY_ADC_AUTH`, 1.1.10) | headless 문서, CHANGELOG, 설치 가이드 |
| 설정 격리 | 설정은 `~/.gemini/antigravity-cli/settings.json`, 전역 규칙은 `~/.gemini/config/`. **설정 홈을 바꾸는 환경변수는 문서에 없다** → run 전용 `HOME`으로만 격리 가능(3단계에서 실측) | features·reference 문서, CHANGELOG 1.2.15·1.2.16 |
| 종료 코드 | 모델/에이전트 오류로 끝난 headless run은 exit `3`에 `AGY_ERROR` 줄(1.2.10); 응답이 없으면 non-zero; 모르는 `--model`은 non-zero(`ERROR`) | CHANGELOG, headless 문서 |

**3단계 보류 사유 없음.** Linux arm64 빌드가 있으므로 macOS 경로를 열 필요가 없고,
agy를 격리 밖 보조로만 쓸지 묻는 분기도 해당하지 않는다. 3단계에서 실측해야 하는
것은 아래 「3단계에 남긴 질문」이다.

### 호스트 `agy --help` 발췌 (1.3.3, macOS arm64, 2026-10-11)

```
$ which agy && agy --version
/opt/homebrew/bin/agy
1.3.3
$ file "$(readlink -f /opt/homebrew/bin/agy)"
Mach-O 64-bit executable arm64
$ agy --help
  --dangerously-skip-permissions  Auto-approve all tool permission requests without prompting
  --disable-slash-commands        Disable slash command and skill expansion in print mode
  --effort                        Reasoning effort for the current CLI session (low|medium|high|xhigh|max)
  --input-format                  Input format for print mode (text, stream-json). ... requires --output-format stream-json (default text)
  --json-schema                   Optional JSON schema string or path to a schema file to enforce structured output ...
  --mode                          Set the agent execution mode for this session (accept-edits, plan)
  --model                         Model for the current CLI session
  --output-format                 Output format for print mode (text, json, stream-json) (default text)
  -p                              Short alias for --print
  --print                         Run a single prompt non-interactively and print the response
  --print-timeout                 Optional time limit for print mode; 0 waits until the turn completes (default 0s)
  --prompt                        Alias for --print
  --sandbox                       Run in a sandbox with terminal restrictions enabled
Available subcommands: agent(s) changelog help install mcp mic-serve models plugin(s) remote-control update
```

바이너리 문자열에서 본 접속 대상 후보(3단계 허용 목적지 실측의 출발점):
`antigravity.google`(oauth-callback, auth-success, oauth/client-metadata.json),
`antigravity.google.com`, `antigravity-unleash.goog`(feature flag), 자동 갱신기
`antigravity-cli-auto-updater-974169037036.us-central1.run.app`, `storage.googleapis.com`
(배포). 모델 API endpoint는 문자열로 확정하지 못했다(실측 필요).

### 근거 URL

- 설치 스크립트(OS·arch 분기, manifest URL, sha512 검증): https://antigravity.google/cli/install.sh
- Linux arm64 manifest: https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_arm64.json
- 시작 가이드(설치 명령, `~/.local/bin/agy`): https://antigravity.google/docs/getting-started?tab=cli
- Headless 모드(플래그·JSON 필드·soft-deny·종료 코드·인증): https://antigravity.google/docs/cli/headless
- 권한(allow/deny/ask, 프리셋, settings.json 위치): https://antigravity.google/docs/permissions
- Sandbox(`--sandbox`, Linux namespaces/macOS Seatbelt, 네트워크 기본 차단): https://antigravity.google/docs/sandbox
- CLI reference(`toolPermission`, `enableTerminalSandbox`, `/logout` keyring): https://antigravity.google/docs/cli/reference
- CHANGELOG(1.3.3; `GEMINI_API_KEY` 1.1.13, `AGY_ADC_AUTH`, exit 3 1.2.10, `denied_actions` 1.1.27, `--mode` headless 수정 1.1.12, Android/Termux 바이너리 1.2.15): https://raw.githubusercontent.com/google-antigravity/antigravity-cli/refs/heads/main/CHANGELOG.md
- Arm Learning Path(Arm Linux 설치): https://learn.arm.com/install-guides/antigravity/
- 이전 실측(dotfiles#169·#193): 호출당 고정 입력 약 12k 토큰, 프롬프트 캐시 없음, `--json-schema` 비권장, `status: SUCCESS`에 빈 `response` + `denied_actions`(권한 거부가 성공처럼 보임), `search_web`은 되고 `read_url`·`command`는 거부.

### 3단계에 남긴 질문 (실측 항목)

1. 설정 격리: 설정 홈 환경변수가 없으니 run 전용 `HOME` 아래 `~/.gemini/antigravity-cli/settings.json`을 부모가 써 주는 방식이 먹는지(deny `command(*)`, `write_file(*)`, `read_url(*)` + `toolPermission: strict`), keyring 없는 Linux 게스트에서 cached credential을 어디서 읽는지.
2. 권한: `dontAsk` 대응 모드가 없으므로 공통 규칙대로 **도구 없는 텍스트 처리 모드**로 제한한다. soft-deny가 exit 0·빈 `response`로 끝나므로 판정기는 `denied_actions != []` 또는 빈 `response`를 실패로 본다(dotfiles#193).
3. 관찰 모델: result에 모델이 없다. `--output-format stream-json`의 `init.model`(override 때만) 또는 다른 근거가 필요하다. 없으면 M5(관찰 모델) 검사는 agy에서 PASS할 수 없다.
4. 허용 목적지: 로그인·feature flag·자동 갱신기·모델 API를 실측해 최소 집합을 정하고, 자동 갱신(`update`, auto-updater)을 막는다(바이너리 sha 고정).
5. 인증: 사용자 결정(Google 로그인 캐시 파일 vs `GEMINI_API_KEY`). 유료 API로 자동 전환하지 않는다.

## 1단계 — 백엔드 어댑터 리팩터 (S1·S2)

### 바뀐 것

| 파일 | 변경 |
|---|---|
| `bin/oss-research-worker` | `Backend` NamedTuple과 `CLAUDE`·`CODEX`·`AGY`·`BACKENDS`·`DEFAULT_BACKEND`; `--backend` 인자; `MeasuredLaunch.backend`(기본 `claude`)와 `cli_sha256`(이전 `claude_sha256`); `LaunchRequest.backend`; `measured_launch_problems`·`measured_runtime_problems`·`require_runtime`·`build_environment`·`build_policy`·`build_command`·`build_staged_command`·`result_problems`·`observed_models`·`collect`·`run_supervised`·`run_research`가 `backend`(기본 `CLAUDE`)를 받음; manifest에 `backend` 키 |
| `bin/oss-research-egress-pin` | `apply --backend claude\|codex\|agy\|none`(기본 `claude`), `MODEL_NAMES`·`pinned_names`, manifest `backend` 키(schema `1.1.0`), `verify`가 백엔드별 이름 집합과 대조 |
| `bin/oss-research-linux-run` | `worker_backend`(worker 인자의 `--backend`, 비AI entry는 `none`), `open_egress`가 `--backend`를 넘기고 apply 응답의 `backend`를 대조, record에 `backend` |
| `tests/test_oss_research_worker_backends.py` | 신설 22건(아래) |
| `tests/test_oss_research_worker_linux.py` | fixture 필드 이름 `claude_sha256 → cli_sha256` 2곳, fake egress-pin이 apply 응답에 `backend`를 되돌림, apply argv 기대값에 `--backend claude` |
| `tests/test_oss_research_worker_auth.py` | CLI 옵션 열거에 `--backend` |
| `docs/oss-research-worker.md` | 「백엔드」 절, `--backend` 설명, 미구현 표 |

고치지 않은 것: `skills/` 전체(분석 번들 `verify_e2e_result.py` 포함 — M5 관찰 모델 검사는 이미 요청 모델 기준이라 백엔드 중립이고, M4의 자격 변수 표는 Codex 자격 변수가 정해지는 2단계 항목), `share/linux/`(Claude 측정 하네스 그대로), `install.sh`.

### S1 — Claude 경로 바이트 불변

main `74e49d6`의 worker로 고정 입력에서 계산한 `sha256(json.dumps(value, sort_keys=True))`와
브랜치의 값이 같다. 시험 `ClaudePathUnchangedTests`가 이 상수를 들고 매번 비교한다.

| 대상 | sha256 (main = 브랜치) |
|---|---|
| `build_command` read-only(선택 없음) | `eaea845d13e1b4823f9afb6d8282b903953b6b0fddf86a443246bf25550aa268` |
| `build_command` read-only + `claude-opus-5-5`/medium | `c9895e337a92e350515cd1bfc09ac6a5effe231c385b22c312236d484a28558b` |
| `build_command` auto + 선택 | `9fa50ee37057cafe6d1b9dc6634f22416b050fe8ca7580c6e242cf2b9021cc31` |
| `build_staged_command` corpus-extract | `9241254294db549b22407052d4176d3ff546e0da63cdab5a79dd82af9a1e893b` |
| `build_staged_command` deep-research(+example.org) | `b33b52b69921986ee928d6f091c5158641513a819779f63e8ef4d3a1b1a785f2` |
| `build_policy` darwin / +relay 4321 / model=False | `ce16b43a…761266` / `ab9dfc39…b1a4d0` / `5782da23…6bda70` |
| `build_policy` linux / +relay / model=False / +staged (호스트 의존 부분인 symlink 해석·`socatPath`를 고정한 값) | `f54d0417…6a034f` / `ac130806…568319` / `6205e1e2…671296` / `6319ce4b…c83389` |
| `build_environment` darwin·linux / +credential | `f6de7a5e…468c35` / `e76bf1e9…f8f7` |
| `MEASURED_LAUNCHES["linux"]` 앞 11필드 | `f944439cf6914249855e1d863f024e722b4a2c8811d2fee8430afd615fa6bc3d` (12번째 `backend="claude"`가 추가됨) |

(전체 64자리는 시험 파일 `FROZEN`에 있다.) 기존 Claude 시험도 그대로 통과한다
(`test_read_only_argv_is_unchanged`·`test_read_only_policy_is_unchanged`·측정 launch
게이트 시험 `MeasuredGateTests` 등; 수치는 아래).

### S2 — 측정되지 않은 백엔드는 exit 3

`UnmeasuredBackendGateTests`:
- `measured_launch_problems`가 `backend codex is not the measured claude`를 첫 항목으로 낸다.
- `require_runtime(... backend=CODEX|AGY)`는 Linux·macOS 모두 `Blocked`("isolated launch is closed"), `load_credential` 0회, Linux에서는 `checked_credential`(메타데이터)도 0회, run 디렉터리 없음, 메시지에 토큰 값 없음.
- 요청과 어댑터의 백엔드가 다르면 Blocked(`names backend codex, not claude`).
- `main`(in-process, 게이트 mock): `--backend codex|agy`가 `LaunchRequest.backend`와 `backend=` 양쪽으로 게이트에 전달되고 relay·run_research 호출 0, run 디렉터리 없음.
- CLI(subprocess): `--backend codex|agy|claude|없음` → exit 3, `--backend gemini|""` → exit 2(`--backend` 안내), 모두 run 디렉터리·자식 실행 흔적 없음, 토큰 값 0회.
- 어댑터 fail closed: `build_command`·`build_staged_command`·`build_environment`·`build_policy(model=True)`·`run_research`가 `Blocked`, `result_problems`는 "no measured result parser", `observed_models`는 `None`. `build_policy(model=False)`(비AI probe)는 백엔드 endpoint가 필요 없어 그대로 만들어진다.

egress pin(`EgressPinBackendTests`): 기본 apply는 이전과 같은 두 이름을 핀하고 manifest에
`backend: claude`; `none`은 GitHub만; `codex`·`agy`·모르는 이름은 nft·hosts·manifest를
건드리기 전에 거부; manifest의 `backend`를 고치면 verify가 거부. unit wrapper
(`WrapperBackendTests`): `--backend` 파싱(기본·`=`형·중복·값 없음·모르는 값), 비AI entry는
`none`, `open_egress`가 `--backend`를 넘기고 되돌아온 백엔드가 다르면 증거 없음.

### 시험 수치

macOS(작성자 호스트, 2026-10-11, Python 3.14): 저장소 시험 20모듈 **693개 OK, skip 39**
(`worker_backends` 22 신설; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·
`offline_integration` 3·`worker_skills` 2 skip). 로컬 Linux CI(Docker, `--network none`)는
아래 「로컬 Linux CI」.

게스트 gate 사례(`tests/fixtures/oss_research_worker/linux/gate_cases.sh`, G1~G9)와 측정
launch의 게스트 실행은 VM이 필요하다. 이 Dispatch에서는 VM을 쓰지 않았다(사용 전 조정자
`ask` 규칙; 1단계는 오프라인 시험만). 게스트 사례는 CI에서도 돌지 않는다(VM 전용 스크립트,
`docs/ci.md`의 skip 규칙과 같은 범주). 측정 launch 판정 자체는 단위 시험
`MeasuredGateTests.test_the_measured_launch_opens_and_reads_the_token_last`가 고정한다.

### #4에서 온 실행기 변경 요청의 반영 (조정자 전달, 2026-10-11)

#4 워커의 기록 `docs/development/2026-10-11-4-isolation-acceptance-remaining/README.md` 8절의 세 제안 중 둘을
이 PR에 넣었다(Claude 경로 바이트 불변 조건 유지: `build_command`·정책·환경은 그대로다).

| 제안 | 반영 | 내용 |
|---|---|---|
| 8.1 auto를 CLI 입력에서 막는다 | **반영** | `main`의 `--permission-mode` choices를 `(dontAsk,)`로. `PERMISSION_MODES`(`build_command`)와 하네스 `model-auto`, 게이트 수준 auto 반례 시험은 그대로(심층 방어). 시험: `test_cli_accepts_only_dont_ask_and_auto_is_refused_as_input`(auto → exit 2, 자식 흔적 없음), linux `test_main_hands_the_whole_launch_to_the_gate`의 auto 사례는 `SystemExit(2)`·게이트 미호출로, skills `test_every_profile_is_still_blocked`의 auto 조합은 `dontAsk`로. 게스트 `gate_cases.sh` G4의 기대값은 exit 2로 적었다(VM 재실행은 하지 않았다). 문서: 「실행」 항목, 「미구현과 미증명」 auto 행, 「검증 상태」 permission 행, 「남은 실제 시험 게이트」 6 |
| 8.2 staged 측정 harness(`unit_inner.py model-staged`, `model_once.sh` MODE, 판정기 S1/S2, `verify_pickup` 계약 검토) | **별도 Dispatch로 처리 제안** | 제품 코드가 아닌 측정 하네스 3~4파일의 새 모드와 판정 계약을 더하는 일이고, VM 없이는 한 번도 돌려 보지 못한 채 넣게 된다. 1단계 PR의 「Claude 경로 불변」 검증 범위와 섞이지 않게 분리한다 |
| 8.3 문서 결정 문구 | **반영** | 「제한: setsid 탈출」 끝과 「남은 실제 시험 게이트」 3에 macOS 결정 문구(측정 launch 후보 아님, 전용 계정 + setsid·Keychain 재측정 선행), 4·5에 "모델 child가 relay를 조회하는 구성은 없다" 문구 |

### 로컬 Linux CI

`docs/ci.md` 방식(Docker `python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root `runner`),
2026-10-11, 커밋 직전 트리(아래 PR의 HEAD와 같은 추적 파일).

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 20모듈 | **693개 OK (skip 40)**; 모듈별 모두 OK(`worker_backends` 22 신설, `worker_linux` 174, `worker` 43, `worker_auth` 47, `worker_skills` 39, `egress_pin` 26) | 모듈별 모두 OK(693, skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2), selftest failures=0 / 129 cases 0 / 19 cases 0 | 돌리지 않음(분석 번들 미변경) |
| 스킬 시험 6묶음 | **642개 OK** (researching 145, analyzing 77, verifying 138, recent-closed 162, curated 53, issues 67) | 돌리지 않음(스킬 미변경) |
| `__pycache__` | 0 | 0 |

이전 기준(`docs/ci.md` 2026-10-09: 저장소 671 OK skip 40, 스킬 642 OK)과 비교하면 저장소 시험만 +22(`worker_backends`)이고
나머지 수치는 같다. 첫 Docker 실행에서는 Linux 정책 해시 4건이 실패했는데, `build_policy`가 Linux에서 호스트의
symlink 해석(`/usr/bin/socat → socat1`)과 `socatPath`를 넣기 때문이었다. 시험이 그 두 호스트 의존 부분을 고정하도록 고치고
(`host_free`), main `74e49d6`의 worker로 같은 방식으로 다시 계산한 값(컨테이너가 낸 값과 같다)으로 상수를 바꿨다.
게스트(VM) 사례는 CI 범주 밖이고 이번 Dispatch에서 돌리지 않았다(위 「시험 수치」).

## 후속 (a) — 8.2 staged 측정 하네스 + 게스트 gate 사례 재실행 (2026-10-12)

PR #21 병합 뒤 `origin/main` `c789087`에서 새 브랜치 `lee-kyu-hwan/6-staged-harness`. 제품 코드 중 바뀐 것은
unit wrapper 1파일(아래 「PR #21 회귀」)뿐이고 `bin/oss-research-worker`·`bin/oss-research-egress-pin`은
그대로다(1단계의 Claude 경로 해시 시험 17건이 그대로 통과한다). 실제 모델 호출 0회, 비용 0 USD.

### 바뀐 것 (8.2, #4 워커의 diff 제안 기준)

| 파일 | 변경 |
|---|---|
| `tests/fixtures/oss_research_worker/linux/unit_inner.py` | 모드 `model-staged OUT PROFILE CREDENTIAL MODEL EFFORT SECONDS PROMPT`(인자 9개). `model_once`와 같은 계약(`claude-opus-5-5`·medium·≤300 s)과 같은 전제 순서(verify_runtime·platform_problems·checked_input·checked_run_dir·Selection·checked_credential·load_credential) 뒤 `run_research(..., staged=StagedRun(PROFILE, reviewed_source_root, ()), ...)`. 프로필이 `corpus-extract`·`deep-research`가 아니거나 PROMPT가 일반 파일이 아니거나 65536 B를 넘으면 어떤 읽기도 전에 exit 2. 요약에 `requested_profile`·`execution_profile`(manifest)·`staged`(staging 기록)·`staged_changed`(collect의 `staged-changed:` 문제만)·`output_files`(이름·크기만, 50개 상한)·`result_meta`를 더한다. 모델 텍스트는 찍지 않는다. `model_once`의 result_meta 추출은 공용 `result_meta()`로 옮겼다(동작 같음) |
| `share/linux/model_once.sh` | MODE `staged-corpus`·`staged-deep`(PROFILE `corpus-extract`·`deep-research`). PROMPT(10번째 인자) 필수, analysis와 같은 P0 검사(root 소유·쓰기 불가·크기·UTF-8·자격 표지 없음) 뒤 root가 `$OUT/prompt.txt`(ossresearch 0600)로 복사하고 sha 대조(W3), inner가 그 경로를 받아 자기 `input/task.txt`로 다시 복사한다. 제품 main은 거치지 않는다(`PRODUCT=no`). 판정은 `judge`(S1·S2), 요약 출력(`INFO R inner`)에 `requested_profile`·`execution_profile`·`staged_changed`·`output_files`를 더했다. 모델 모드 셋(`model-once`·`model-auto`·staged)은 wrapper에 `--backend claude`를 준다 |
| `share/linux/model_once_verdict.py` | `judge`가 `model-staged` 요약도 받는다. staged에는 V2(고정 답 OK) 대신 **S1-staged-unchanged**(staging 기록의 `files`가 비어 있지 않고 모두 64-hex sha256을 가지며 `settings.json`을 포함, `staged_changed == []`)와 **S2-profile-requested**(`requested_profile`이 staged 프로필이고 manifest의 `execution_profile`·기록의 `profile`과 같음). R1~R5·V1·V3~V5는 같다. `model-once` 판정은 바뀌지 않았다 |
| `share/linux/verify_pickup.py` | `--staged corpus-extract|deep-research` 계약(아래 결정) |
| `bin/oss-research-linux-run` | `--backend claude|codex|agy|none`(기본 `none`, `--entry verify` 전용) → `open_egress`. `--entry worker`는 지금처럼 worker 인자에서 읽고, 다른 entry에 이 옵션을 주면 exit 2. 조정자 결정 (A), 2026-10-12 |
| `tests/test_oss_research_worker_staged_harness.py`(신설) | 오프라인 시험 13개(아래) |
| `tests/test_oss_research_worker_backends.py` | `WrapperBackendTests` +2(verify entry의 `--backend`, main → launch 전달·거부) |
| `docs/oss-research-worker.md` | 「백엔드」 egress pin 항목에 verify entry 옵션과 회귀, 판정·회수 행, 「미구현과 미증명」 스킬·Agent 행, 「검증 상태」 호환성 행, 「남은 실제 시험 게이트」 5에 하네스 준비 사실 |

### `verify_pickup.py` 결정 — 기본 계약은 staged 묶음에도 맞고, `--staged PROFILE`을 더했다

코드로 확인한 사실: 결과 묶음은 `run/result/`를 tar로 뜬 것이고 worker는 staged run에서도 같은 파일 집합
(`manifest.json`·`report.md`·`stdout.json`·`stderr.log`[·`relay.log`])을 쓴다. `output/`은 `run/output/`
(`WRITABLE_AREAS`)에 남고 `result/`에 들어가지 않으므로 묶음의 member 규칙은 바뀌지 않는다. manifest에는
`execution_profile`과 `staged`(staging 기록)가 더 있지만 기본 계약은 그 키를 보지 않는다. 따라서 **기본 계약
(member 집합·`succeeded`·`report.md` sha)은 staged 묶음을 그대로 받는다.** 다만 프로필을 묶지 않아 read-only
run의 묶음과 구분하지 못하므로, 측정이 쓸 명시 계약 `--staged PROFILE`을 더했다: 기본 검사 전부 + manifest
`execution_profile == PROFILE`, `staged.profile == PROFILE`, `staged.files`가 비어 있지 않고 모두 64-hex sha256을
갖고 `settings.json`을 포함, `permission_mode dontAsk`, `exit_code 0`, `failures`·`problems` 비어 있음, 자격 검사
`clean`. 출력 JSON에 `contract: staged`·`execution_profile`. 잘못된 사용(`--staged` 값 없음·`read-only`·다른 옵션
결합)은 풀기 전에 끝난다. `output/` 산출물은 묶음에 없으므로 inner 요약의 `output_files`(이름·크기)로만 관찰한다.
묶음에 넣는 것은 측정 뒤 별도 결정이다.

### PR #21 회귀 — verify entry의 pin backend가 항상 `none`

1단계에서 `worker_backend()`는 `--entry worker`가 아니면 `none`을 돌려줬다. 측정 하네스(`unit_inner.py`의
`model-once`·`model-auto`·새 `model-staged`)는 `--entry verify`로 돌므로 `--egress pin`이면 GitHub 이름만 열리고
`api.anthropic.com`은 `/etc/hosts` 차단·nft 미허용이 되어 모델 child가 접속할 수 없다(이번 gate 사례는
`--egress none`이라 영향이 없고, 2026-10-11 S1 측정은 1단계 이전 `74e49d6`에서 돌았다). 조정자 결정 (A,
2026-10-12): wrapper에 자기 옵션 `--backend`(verify entry 전용, 기본 `none`)를 더하고 `model_once.sh`의 모델
모드가 `--backend claude`를 준다. 제품 worker·egress-pin은 그대로다. 시험: `WrapperBackendTests`
`test_verify_entry_takes_the_wrappers_own_backend`(verify + claude → claude, 기본 none, 비AI entry none 유지,
worker entry는 자기 인자에서 읽고 wrapper 옵션은 거부, 모르는 값 ValueError)와
`test_main_hands_the_verify_backend_to_the_launch_or_refuses_it`(main → launch에 `claude` 전달, 모르는 값·빈
값·값 없음·worker entry에 준 경우 exit 2, launch 미호출). apply 응답의 backend 대조는 기존
`test_open_egress_pins_the_given_backend`가 본다.

### 오프라인 시험 (신설 `tests/test_oss_research_worker_staged_harness.py`, 13개)

| 묶음 | 시험 | 고정하는 것 |
|---|---|---|
| `ModelOnceScriptTests` 2 | `bash -n`; 모르는 MODE exit 2; `staged-corpus`·`staged-deep`·`analysis`는 PROMPT 없으면 `FAIL P0-prompt-argument (<MODE> needs PROMPT)` exit 2(sudo 전); MODE 표 1곳, 제품 경로 표에 staged 없음 | 스크립트의 CMD 블록을 떼어 변수만 주고 평가: staged 두 모드의 argv(`--entry verify --egress pin --backend claude --timeout 330 -- model-staged OUT PROFILE CRED MODEL EFFORT INNER OUT/prompt.txt`), `model-once`·`model-auto`의 argv(기존 + `--backend claude`), `--egress none`에서도 backend 전달 |
| `UnitInnerStagedTests` 5 | 인자 표(`model-staged: 9`, 모자라거나 많으면 usage로 SystemExit, 맞아도 wrapper 밖이면 "not inside the unit wrapper"); 계약 밖 프로필·prompt(symlink·빈 파일·65537 B·없음·디렉터리)·model/effort/seconds는 어떤 읽기도 전에 exit 2(`measured_launch`가 호출되지 않음을 mock으로); `run_research`를 mock해 전달 인자(runtime·cli·prompt·input/run 디렉터리·`claude-profile2`·300 s·relay None·`dontAsk`·자격 값·Selection·`StagedRun(profile, 저장소 루트, ())`)와 요약(`requested_profile`·`execution_profile`·`staged`·`staged_changed []`·`output_files`·`result_meta`·`report_text`, 모델 텍스트·자격 값 없음)을 보고 그 요약이 `judge`에서 모두 PASS; `staged-changed` 문제가 있는 실패 run은 exit 1과 `staged_changed` 목록; 설치본(`source_checkout None`)은 Blocked이고 input/run 디렉터리를 만들지 않음 |
| `VerdictStagedTests` 3 | 완전한 staged token 기록은 11개 검사 모두 PASS(S1·S2 포함, V2 없음), deep-research도 PASS, sentinel은 R5까지; S1(기록 없음·files 빔·settings.json 없음·sha 형식·`staged_changed` 비어 있지 않음/None)·S2(read-only·프로필 불일치·None·기록 프로필 다름·요청 없음)·R4·V1·V4 반례 FAIL, 답 텍스트는 판정하지 않음; `model-once` 판정 불변(V2 유지, S1 없음), `model-auto` 요약은 R3 FAIL, CLI `judge … token` exit 0/1 |
| `PickupStagedTests` 3 | `--staged corpus-extract` 통과(`contract staged`), 같은 묶음이 기본 계약도 통과(결정), deep-research 묶음을 corpus-extract로 요구하면 두 문제; read-only run·기록 없음·pin 없음·sha 형식·settings.json 없음·auto·exit 1·problems·자격 FAIL; 실패 manifest는 기본 검사가 먼저; 잘못된 사용 4가지는 풀지 않고 종료 |

실제 하네스(`model_once.sh`)의 게스트 실행은 측정 Dispatch의 몫이다(모델 호출이 따라오므로 이번에는 돌리지 않았다).

### 게스트 gate 사례 재실행 (G1~G9, VM, 모델 호출 없음)

조정자 `ask` 승인 뒤(사용자 사전 승인 2026-10-12) 커밋 `96c1a0d`(하네스+wrapper, README 제외)를 게스트
`oss-research-debian`에 root 소유 `/opt/oss-research/src-96c1a0d`로 배포하고(`git archive HEAD` 스트림,
`files_not_root 0 group_other_write 0`; 파일 sha256 목록은 호스트 추적 파일과 같다. 유일한 차이는 당시 작성 중이던 이
README) `tests/fixtures/oss_research_worker/linux/gate_cases.sh 96c1a0d`를 vmadmin으로 돌렸다. sentinel 가짜 자격만,
claude는 PATH에 없고, 모두 `--egress none`. 모델 호출 0, 토큰 0, 비용 0 USD. 전후 units 0·procs 0·creds 0·pin 없음,
`/tmp/osr-g-*` 0. 기존 `src-*` 트리와 기존 증거 디렉터리는 건드리지 않았다.

| 사례 | 구성 | 기대 | 실제 | 판정 |
|---|---|---|---|---|
| G1 | wrapper `--entry verify` `unit-check` | unit 안에서 `unit_problems []`, wrapper exit 0 | `oss-research-5345d0f45e348b03 0 {"unit_problems": []}` | PASS |
| G2 | wrapper 밖 직접 실행(marker 없음) | exit 3 | rc=3 "not started by the unit wrapper (no unit marker)" | PASS |
| G3 | 위조 marker, stdin이 곧 닫힘 | exit 75(`PARENT_GONE_EXIT`, EOF guard가 먼저 끝냄) | rc=75 | PASS |
| G3b | 위조 marker, 열린 pipe, unit 밖 | exit 3 | rc=3 "this process is not in the wrapper unit's cgroup" | PASS |
| G4 | `--permission-mode auto` | **exit 2**(입력 거부, 8.1) | exit 2, argparse usage | PASS |
| G5 | `--relay-repo eslint/eslint` | exit 3 | "relay a relay is not the measured none" | PASS |
| G6 | `--execution-profile deep-research` | exit 3 | "execution profile deep-research is not the measured read-only" | PASS |
| G7 | `--execution-profile corpus-extract` | exit 3 | "execution profile corpus-extract is not the measured read-only" | PASS |
| G8 | `--model claude-sonnet-5-5` | exit 3 | "model claude-sonnet-5-5 is not the measured claude-opus-5-5" | PASS |
| G9 | 범위 일치, claude PATH 제외 | exit 3(runtime pin) | "this is not the measured runtime: claude is not on PATH" | PASS |

어느 사례도 run 디렉터리를 만들지 않았다(`run_dir_exists=no` ×9). G3의 기대값 75는 이전 판(a189x, 2026-10-02)과 같고,
`gate_cases.sh` 머리말의 기대값 서술을 이에 맞게 고쳤다(1단계에서는 "나머지 전부 exit 3"으로 적었다). 부수 관찰: 배포 때
게스트 시계가 호스트보다 약 11062 s 느려 tar가 "time stamp in the future" 경고 322줄을 냈고(`deploy.err`), 첫 wrapper
실행의 시계 검사가 RTC로 재동기했다(상태 전 `2026-10-11T21:25:18Z` → 후 `2026-10-12T00:30:55Z`; 2026-10-11에도 본 호스트
잠자기 효과).

증거: `~/.oss-research-vm/logs/a6-gate-20261012T003032Z/`(0700, `SHA256SUMS` 18 entries, `shasum -a 256 -c` 전부 OK).
핵심 파일 sha256: `guest-gate-cases.log` `d45707b25e3be83c9e917be9f2a42094a18ba426b109850d2e45ac27c35b574f`, `deploy.log` `d2e73911d2c0e702888e4eb62bc4893789af2d3baa58479fe884cf2b3880ad08`, `SHA256SUMS` 자체 `adc58cce29ad9087e6362478a44fad40dd5e8da98633f5a291e9a385a41f79e8`.

### 로컬 Linux CI

`docs/ci.md` 방식(Docker `python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root `runner`),
2026-10-12, 한 번에 하나만 실행. 추적 파일 기준 트리는 아래 PR HEAD와 같다(이 CI 수치 문단만 CI 뒤에 채웠다).

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 21모듈 | **708개 OK (skip 40)**; 모듈별 모두 OK(`worker_staged_harness` 13 신설, `worker_backends` 24(+2), `worker_linux` 174, `relay` 57 — 알려진 flake `ConnectionCapTests`는 단독 실행이라 재현되지 않음) | 모듈별 모두 OK(708, skip 39) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2), selftest failures=0 / 129 cases 0 / 19 cases 0 | 돌리지 않음(미변경) |
| 스킬 시험 6묶음 | **659개 OK** (researching 162, analyzing 77, verifying 138, recent-closed 162, curated 53, issues 67) | 돌리지 않음(미변경) |
| `__pycache__` | 0 | 0 |

1단계 PR(저장소 693, 스킬 642)과 비교하면 저장소 시험 +15(이 PR의 13+2), 스킬 +17은 main에 그사이 병합된 #19(researching
145→162)이고 이 PR과 무관하다. 게스트(VM) 사례 G1~G9는 위 절대로 별도 실행했고 CI 범주 밖이다.

### 남은 항목

- staged 실제 측정 2회(`corpus-extract` ≤ 0.3 USD, `deep-research` ≤ 2 USD; #4 8.2의 계획표): 별도 Dispatch, 사용자 예산 승인
  뒤 `model_once.sh token <src> … pin normal 330 300 staged-corpus|staged-deep <PROMPT>`. 측정 뒤 `MEASURED_LAUNCHES`에
  staged launch를 더할지는 별도 결정.
- `output/` 산출물을 결과 묶음에 넣을지(지금은 inner 요약의 `output_files`로만 관찰).
- 2단계 Codex·3단계 agy(위 0·1단계 절), 분석 번들 M4 자격 변수 표.

## 후속 (b) — staged 실제 측정 (2026-10-13)

PR #23 병합 뒤 `origin/main` `5952d82`에서 새 브랜치 `lee-kyu-hwan/6-staged-measure`. 「후속 (a)」의 「남은 항목」 첫째,
8.2 계획표의 staged 실측 2회(`corpus-extract`·`deep-research`)를 했다. 모델 호출은 조정자 `ask` 승인 뒤 회차마다 1회,
사용자 상한 2.5 USD. 1회차가 제품 결함으로 FAIL이어서(아래) 조정자 결정으로 결함을 고친 커밋 `3d345f1`에서 1회차를 다시
돌렸다(모델 호출 3회, **누적 0.1641218 USD**). `MEASURED_LAUNCHES`에 staged launch를 더하는 것은 이번에 하지 않았다(권고만, 아래).

### 실행 전 기록

| 항목 | 값 |
|---|---|
| 게스트 | `oss-research-debian`(Debian 13 aarch64, Lima), srt 0.0.77, node v24.20.0, Claude 2.1.283. 시계 호스트와 동일(offset 0.09 s, 재동기 없음) |
| 배포 | `git archive 5952d82` → root 소유 `/opt/oss-research/src-5952d82`(새로 배포, `files_not_root 0 group_other_write 0`), 파일 sha256 목록이 커밋의 추적 파일과 같음(`tree-diff.txt` 빔). 재시도용 `src-3d345f1`도 같은 방식·같은 결과 |
| 하네스 명령 | `bash <src>/share/linux/model_once.sh token <src> /opt/oss-research/srt-0.0.77 /opt/oss-research/node-v24.20.0-linux-arm64/bin pin normal 330 300 staged-corpus\|staged-deep /root/a6b/prompt-<corpus\|deep>.txt < <호스트 자격 파일>`(자격은 셸 리다이렉트만; 호스트는 `stat`만 봤다) |
| 계약 | `claude-profile2`, `claude-opus-5-5`·medium, inner 300 s, wrapper 330 s, `--egress pin`, `--backend claude`(PR #23) |
| PROMPT | `prompt-corpus.txt` 2048 B sha256 `0dc4358d5a1650789eaba9f1eeb3d889acfad5a304141db6fe97e83841386803`: 가짜 PR 레코드 3건이 든 자기완결 과제. 메인이 `Agent(corpus-extractor)` 1회 → 서브에이전트가 `task.txt`의 EXTRACTION PROMPT에 JSON으로 답 → 메인이 Write로 `output/extract-01.json` 1개 → 한 줄 답. `prompt-deep.txt` 656 B sha256 `bd58157aa8c32891cf6a3f9bc77cdd5792953775cf3b53dce829642736513baf`: Skill 도구로 `deep-research`를 불러 Node.js Active LTS 버전·EOL 월을 묻는 짧은 질문(`--web-domain` 없음), 도구가 거부·실패하면 한 문장으로 알리고 멈추라고 지시. 둘 다 호스트에서 `checked_input`·`checked_staged_input`을 미리 통과시켰고 게스트 복사본 sha가 같다 |
| 예산 | 1회차 ≤ 0.3, 2회차 ≤ 2, 재시도 ≤ 0.3 USD(각 회차 `ask`로 승인) |

### 회차별 결과

| 회차 | 트리 | 하네스 | judge(11) | pickup `--staged` | 자식 | 비용(USD) | 관찰 모델 |
|---|---|---|---|---|---|---|---|
| 1 `staged-corpus` | `src-5952d82` | `SUMMARY fails=2` | 8 PASS / **3 FAIL**(V1·V3·V4; R1~R5·S1·S2·V5 PASS) | FAIL(`missing: report.md`) | exit 1, 0 s, API 호출 없음 | **0** | 없음 |
| 2 `staged-deep` | `src-5952d82` | `fails=0` | **11 PASS** | PASS(기본 계약도 PASS) | exit 0, 4.1 s, 2턴, `permission_denials []` | **0.0759696** | `claude-opus-5-5`만, webSearchRequests 0 |
| 1 재시도 `staged-corpus` | `src-3d345f1` | `fails=0` | **11 PASS** | PASS(기본 계약도 PASS) | exit 0, 9.5 s, 3턴, `permission_denials []` | **0.0881522** | `claude-opus-5-5`만 |

세 회차 모두 자격 scan `clean`, `exact_value_check clean`, pin apply/verify/clear 0, 정리 사실(Z) 전부 PASS, 전후 units 0·procs 0·creds 0,
`/tmp/osr-m-*` 0. 비용은 CLI 추정(`costBasis list`)이고 청구서가 아니다.

**1회차(FAIL, 제품 결함).** 자식 Claude가 모델 호출 전에 끝났다. `stderr.log`: `Error: --agents file not found:
/tmp/osr-m-out-9kgY/run/policy/claude-agents.json (a value that is not a JSON object is read as a file path)`. 원인(호스트 오프라인 재현
`repro-agents-path.py`, 커밋된 worker만으로): `build_policy`는 Linux에서 `run/policy`·`run/result`를 자식에게 읽기 불가로 둔다(`denyRead "/"`,
`allowRead`에 run 디렉터리 없음, 코드 주석에 의도 명시). 그런데 `build_staged_command`는 corpus-extract에 `--agents <run>/policy/claude-agents.json`
경로를 넘겼다 → 샌드박스 안 CLI에는 없는 파일. deep-research는 `--agents`가 없고 staged 파일·`settings.json`은 읽기 가능한
`run/home/.claude` 아래라 영향이 없다. 오프라인 시험은 파일 존재·0400·내용만 봤고 자식의 가독성은 보지 않았다(srt probe
`StagedBoundaryTests`도 그 경로를 읽지 않는다). staged 기록(S1)·프로필 결속(S2)은 이 실패 run에서도 PASS여서 하네스는 설계대로 동작했다.

**수정(커밋 `3d345f1`, 조정자 결정 ③·소유권 확장).** `build_staged_command`가 `--agents`에 같은 객체를 `json.dumps(spec.agents)` 인라인으로
넘긴다(CLI 오류문이 말하는 방식). `run/policy`를 자식에게 읽기 불가로 두는 설계, 파일 `run/policy/claude-agents.json`(0400)과
`agents_sha256` 기록(`stage_research_config`), collect의 `staged-changed: agents file` 검사는 그대로다. deep-research argv는 바뀌지 않았다.
시험: `ClaudePathUnchangedTests`의 FROZEN `argv_corpus-extract` **`9241254294db549b22407052d4176d3ff546e0da63cdab5a79dd82af9a1e893b` →
`33e3a977a59a8904bc040992e04a5055d6d4c3a2d5b788da425c542f8296d84f`**(결함 수정이므로 기대값을 바꿨다; 주석에 이전 값과 이유),
`test_corpus_extract_argv`는 `--agents`를 JSON으로 대조, 새 `test_staged_argv_paths_are_readable_to_the_child`는 두 프로필의 staged argv에서
Claude에 넘기는 절대 경로 인자가 darwin·linux 두 `build_policy`의 `allowRead`+`allowWrite` 안에 있고 `run/policy`·`run/result` 아래가 아님을
고정한다(같은 검사를 `5952d82` 코드에 돌리면 `<run>/policy/claude-agents.json` UNREADABLE로 실패, 수정 코드는 통과 — 증거 `retry/`·보고서).
`docs/oss-research-worker.md`의 `--agents` 설명 두 줄(프로필 표, staging 3항)을 인라인으로 고쳤다(그 밖의 줄은 Codex 2단계 워커 소유).

**2회차(PASS, 그러나 스킬은 호출되지 않았다).** 하네스·판정·회수는 전부 PASS: staged 파일 3개(`skills/deep-research/SKILL.md`
`a330b11e…`, `workflows/deep-research.js` `4d7392eb…`, `settings.json`)가 pin과 같고 변경 0, `execution_profile deep-research`. 그러나 모델의 답
(`report.md` 238 B, sha `bd7c1346…`): *The Skill tool failed to load "deep-research" with the error: "Skill deep-research cannot be used with
Skill tool due to disable-model-invocation. Ask the user to run /deep-research themselves — it cannot be invoked via the Skill tool."* 즉
Claude 2.1.283은 staged 스킬을 **모델 호출 불가(disable-model-invocation)** 로 취급했고 `Workflow(deep-research)`·`WebSearch`에 이르지 못했다
(`subagent_stats spawned 0`, webSearchRequests 0, 2턴). WebFetch 거부·300 s 초과 같은 예상 위험은 발생하지 않았다(그 단계 전에 멈췄다).
X8(계약 §3.2 "staged 로드·자식 경계")의 판정: **로드는 됨(S1), 모델이 Skill 도구로 쓸 수는 없음** → deep-research 프로필은 지금 설계(모델이
`Skill(deep-research)`를 부름)로는 동작하지 않는다. 원인 후보는 아직 확인하지 않았다(아래 「남은 질문」).

**1회차 재시도(PASS).** `Agent(corpus-extractor)` 1회(`subagent_stats`: spawned 1, completed 1, `by_type {"corpus-extractor": 1}`, 깊이 1),
`output/extract-01.json` 1개(204 B; `output/`은 묶음에 없어 inner 요약의 `output_files`로만 봤다), 답 `{"written": "extract-01.json"}`(`report.md`
sha `13b90fbf…`), `permission_denials []`, 모델 전환 0(`modelUsage` opus만 — 서브에이전트도 부모 모델을 썼다), staged 변경 0, `agents_sha256`
`23e216fe…`. 8.2 계획표의 기대 관찰(init tools·Agent 1회·output 1개·denials 0·staged 변경 0·모델 전환 0)을 init 도구 목록(json 출력에는 없음)만
빼고 모두 확인했다.

### 증거

`~/.oss-research-vm/logs/a6-staged-20261012T225608Z/`(0700, `SHA256SUMS` 105 entries, `shasum -a 256 -c` 전부 OK). 하위 `corpus/`(1회차),
`deep/`(2회차), `retry/`(재배포 `src-3d345f1` + `retry/corpus/`). 각 run에 `harness.log`, `record.json`, `result.tar`, `host-judge.txt`,
`pickup.json`, `pickup-default.json`, `result-summary.txt`, `unpacked/`(PASS run), 전후 게스트 상태. 핵심 sha256: `SHA256SUMS`
`c17310b1c04a508f7533fe3caa4c7e77e7de552d89ea8a1baefb7745e5d9a58a`, `README.txt` `8c67dd91033d4b94512747b094d03b795958ff5ae83edf5d174b18d014aff964`,
`corpus/harness.log` `b3f7a817fc8e27d513f46441581fce23eb80ff5493ea86b33dce320f55a32b15`, `deep/harness.log`
`326eb446d45c455ec9e462fc3a65b3c82cc5a7eea5506a146bc69fa725a0957e`, `retry/corpus/harness.log` `f846f469650b7386d3ad2ce56263a1a10419a6a128d0840df53ac9f0c3ce3ed1`,
`deep/result.tar` `4a6bd28f85bec31533d4874c1644dcc650d4cf57f8881171a947e5fa715b08f3`, `retry/corpus/result.tar`
`61287ffe03cecf81c0d650413010a7446e2b447b6f407347f1bc0f66f67e847e`. 게스트 원본(root 전용) `/var/lib/oss-research-evidence/oss-research-5a11bf1e28da130a`·
`…-e84e8d6d8b2e734b`·`…-54ebc1cad1b68ff0`. 게스트 helper `/root/a6b`는 끝에 지웠고 배포 트리는 선례대로 남겼다. 기존 증거 디렉터리는 건드리지 않았다.

### 로컬 Linux CI

`docs/ci.md` 방식(Docker `python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root `runner`), 2026-10-12T23:13Z, 한 번에
하나만 실행(시작 전 `docker ps` 빔). CI 트리 = 커밋 `3d345f1` + `docs/oss-research-worker.md`의 두 줄 수정; 이 README 절은 CI 뒤에 썼다(시험에
영향 없는 문서). main `5952d82`(708 OK skip 40 / 번들 OK / 스킬 659 / pycache 0)과 비교해 저장소 시험 **+1**(새 `test_staged_argv_paths_are_readable_to_the_child`).

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 21모듈 | **709개 OK (skip 40)**; 모듈별 모두 OK(`worker_skills` 40(+1), `worker_backends` 24, `worker` 43, `worker_staged_harness` 13, `worker_linux` 174, `relay` 57 — 알려진 flake `ConnectionCapTests`는 단독 실행이라 재현되지 않음) | 바뀐 3모듈(`worker_skills`·`worker_backends`·`worker`) 107 OK(skip 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2), selftest failures=0 / 129 cases 0 / 19 cases 0 | 돌리지 않음(미변경) |
| 스킬 시험 6묶음 | **659개 OK** (researching 162, analyzing 77, verifying 138, recent-closed 162, curated 53, issues 67) | 돌리지 않음(미변경) |
| `__pycache__` | 0 | 0 |

### 결론과 권고

- **corpus-extract**: 결함 수정 뒤 1회 PASS(판정 11·회수·기대 관찰). `MEASURED_LAUNCHES`에 더할 후보지만 **이번에는 더하지 않는다**. 권고: 더하기 전에
  (1) 같은 커밋에서 분석 번들이 만든 실제 추출 prompt(≈60 KB, `Agent` 여러 번)로 1회 더 재어 `--max-turns 20`·300 s 안에 끝나는지 보고,
  (2) `output/` 산출물을 결과 묶음에 넣을지(지금은 inner 요약으로만 관찰) 정한 뒤, (3) staged launch 필드(profile·web_domains·agents sha)를
  `MEASURED_LAUNCHES` 표에 어떻게 적을지 정한다.
- **deep-research**: 하네스는 PASS지만 스킬이 모델 호출 불가로 거부돼 **측정 목적(Workflow 로드·자식 경계)은 미달**. `MEASURED_LAUNCHES`에 더하지
  않는다. 설계(모델이 `Skill(deep-research)`를 부름)가 Claude 2.1.283의 `--print` 자식에서 성립하는지부터 확인해야 한다.
- 결함의 교훈: argv의 경로 인자는 정책의 읽기 집합과 함께 시험해야 한다(`test_staged_argv_paths_are_readable_to_the_child`). 1단계의 Claude 경로
  해시 시험은 "불변"을 고정하는 것이라 이런 결함을 잡지 못한다.

### 남은 질문

- `disable-model-invocation`의 출처: SKILL.md 머리말에는 그 필드가 없다. `--restricted`·`--print`·`--setting-sources ""`·`--permission-mode dontAsk`
  중 무엇이 staged 스킬을 사용자 전용으로 만드는지, 혹은 CLI 2.1.283이 `~/.claude/skills`의 스킬을 기본 사용자 전용으로 보는지 — CLI 문서·
  옵션 확인이 필요하다(다음 측정 전, 모델 호출 없이 `claude --help`·설정 조사로 가능할 수도 있다).
- deep-research의 `Workflow` 도구가 `--restricted` 자식에 존재하는지는 아직 관찰되지 않았다(Skill 단계에서 멈춤).
- 2단계 Codex·3단계 agy(0·1단계 절), 분석 번들 M4 자격 변수 표, `output/` 묶음 포함 여부.
