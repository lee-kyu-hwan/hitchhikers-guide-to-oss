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
