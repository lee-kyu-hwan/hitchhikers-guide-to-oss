# Orca에서 OSS 조사 워커 실행

`oss-research-worker`는 #189의 조사 전용 실행기다. 조사 프로세스를
Anthropic sandbox runtime(`srt`)으로 감싸고, 결과와 종료 상태를 검증한 뒤
기록만 한다. 2026-09-27 현재 **실제 조사 실행은 막혀 있다**(fail closed).

- 전용 로컬 prefix에 버전을 고정해 설치한 `srt` 0.0.77에서, AI가 아닌 fixture로
  파일·자식 프로세스·환경·Unix socket·네트워크·Keychain fixture·정책 손상
  경계를 실제로 관찰했다(아래 「실측 결과」).
- 실행기는 설치된 srt의 lock·버전·설치 트리를 대조한다. lock은
  `tests/fixtures/oss_research_worker/runtime/`에 커밋되어 `npm ci`로 같은 트리를
  재현한다. 닫힌 게이트는 인증 파일의 메타데이터(경로·소유자·mode·링크 수·크기)만
  검사하고 토큰 내용은 읽지 않는다.
- 게이트 뒤에는 조사 프로세스의 `--permission-mode`(`dontAsk`|`auto`, 기본
  `dontAsk`) 선택과, 인증 파일을 한 번 읽어 **조사 프로세스 환경에만** 넣고
  수거 때 노출을 검사하는 최소 준비 코드가 있다(「인증」). 가짜 sentinel과
  로컬 fake 자식으로만 시험했다.
- 실제 Claude 바이너리는 이 정책 안에서 `--version`·`--help` **기동만** 확인했다
  (네트워크 허용 없음, 인증 없음, 새 HOME). 모델 요청·인증·스킬·auto는 실행하지
  않았으므로 게이트는 닫혀 있다.
- 게이트 뒤에 조사 스킬 실행 구성기(`--execution-profile corpus-extract|deep-research`)가
  있다(「조사 스킬 실행 구성」). 검토된 원본 스킬·워크플로를 hash로 고정해 run 전용
  설정 디렉터리에 복사하고 srt로 쓰기를 막는다. 인자·정책·manifest는 오프라인으로,
  staged 설정 변경 거부와 `output/` 쓰기는 실제 srt 비AI probe로 확인했다. 스킬을
  실제로 실행한 적은 없다.
- 이 문서의 어떤 결과도 정상 조사, 인증 성공, 인증 방식의 최종 선택, 계정
  바인딩, 스킬 호환성, auto 차단의 증거가 아니다.

## 실행

```bash
python3 dot_local/bin/executable_oss-research-worker \
  --profile claude-profile1 \
  --input-dir /absolute/research-input \
  --prompt-file /absolute/research-input/task.txt \
  --run-dir /absolute/runs/2026-09-27-a \
  --runtime-prefix /absolute/srt-0.0.77 \
  --credential-file /absolute/credentials/claude-profile1/oauth-token \
  --permission-mode dontAsk
```

- `--input-dir`: 조사 입력 전용 디렉터리. symlink가 하나라도 있으면 거부한다.
  부모 `HOME`을 포함하는 경로(`/`, `/Users` 등)도 거부한다.
- `--prompt-file`: 입력 디렉터리 안의 일반 파일, 64 KiB 이하 UTF-8.
- `--run-dir`: 아직 없는 경로. 실행마다 새로 만들고 입력과 겹치면 거부한다.
- `--runtime-prefix`: 아래 설치 절차로 만든 srt 전용 prefix. PATH의 `srt`는
  쓰지 않는다.
- `--credential-file`: 지금 구현된 유일한 인증 바인딩 입력이다(선택지 하나일
  뿐이다). 게이트는 프로필 전용 토큰 파일의 메타데이터만 검사한다(「인증」
  참고). 없으면 "no credential binding"으로 막는다. 인증 방식 자체는 아직 정하지
  않았다.
- `--permission-mode dontAsk|auto`: 조사 프로세스 Claude의 권한 모드. 기본
  `dontAsk`. 다른 값(`default`·`bypassPermissions`·`plan` 등)은 종료 코드 2로
  거부한다. 도구 표면(`Read,Glob,Grep`, 거부 목록, `--safe-mode` 등)은 두 모드가
  같다. `auto`는 CLI 인자 준비일 뿐이고, auto로 AI를 실행한 적이 없으므로 auto의
  차단 동작 증거가 아니다. 게이트가 닫혀 있어 어느 값이든 종료 코드 3이다.
- `--timeout`: 기본 1800초. 양의 유한한 값만 받는다. relay와 조사 프로세스
  전체에 거는 벽시계 상한 하나다(「supervisor」). 넘기면 두 프로세스 그룹을
  모두 종료하고 실패 처리한다.
- `--relay-repo OWNER/NAME`(반복): 주면 #192 읽기 전용 relay를 같이 띄운다.
  `--relay-request-budget`(기본 100), `--relay-max-response-bytes`(4 MiB),
  `--relay-response-byte-budget`(64 MiB), `--relay-timeout-seconds`(30)는
  relay 자체의 상한 안에서만 받는다. 저장소는 1~20개, 대소문자 무시 중복 금지다.
  이 값들은 게이트보다 먼저 검사한다. 잘못되면 종료 코드 2다. relay 경로와
  인터프리터는 인자로 받지 않는다.

- `--execution-profile read-only|corpus-extract|deep-research`: 기본 `read-only`는
  위 도구 표면 그대로다. 나머지 둘은 별도 staged run이다(「조사 스킬 실행 구성」).
  잘못된 값은 종료 코드 2다. 게이트가 닫혀 있어 어느 값이든 종료 코드 3이다.
- `--web-domain HOST`(반복): deep-research에서 WebFetch가 닿을 수 있는 호스트.
  기본은 없음이다. 소문자 호스트 이름만 받고(포트·scheme·wildcard·IP·`localhost`
  거부), GitHub 계열(`github.com`·`githubusercontent.com`·`github.io`·
  `githubassets.com`·`github.dev`·`githubapp.com`·`ghcr.io`와 그 하위)은 거부한다.
  GitHub 조회는 relay로만 한다. 다른 프로필에서 주면 종료 코드 2다.

종료 코드는 다음과 같다. 0은 결과 검증 통과, 2는 입력 거부, 3은 격리 실행
불가(blocker), 4는 실행했으나 결과 검증 실패다. 3이면 run 디렉터리를 만들지
않고, `srt`·`claude`도 시작하지 않는다. 비격리 재시작은 없다.

## 조사 스킬 실행 구성

기준: `docs/research/2026-09-27-78-research-skill-runtime-contract.md`(#82 worktree)
§3·§8. 스킬을 새로 쓰지 않고, 검토된 source checkout의 원본을 그대로 staging한다.
`read-only` 실행의 인자·정책·환경은 바뀌지 않는다(회귀 시험으로 고정).

| 프로필 | `--tools` | `--allowedTools` | `--disallowedTools` | 빼는 플래그 | staging |
|---|---|---|---|---|---|
| `read-only`(기본) | `Read,Glob,Grep` | 같음 | 기존 목록 | 없음 | 없음 |
| `corpus-extract` | `Read,Glob,Grep,Agent,Write` | `Read Glob Grep Agent(corpus-extractor) Write` | `Bash,PowerShell,WebFetch,WebSearch,Edit,NotebookEdit,Skill,Workflow` | `--safe-mode`(custom agent를 끄므로) | 부모가 쓴 `--agents` 파일(`corpus-extractor`, 도구 `Read`만, model 지정 없음), `--add-dir <run>/output` |
| `deep-research` | `Read,Skill,Workflow,WebSearch,WebFetch` | `Read Skill(deep-research) Workflow(deep-research) WebSearch` + 도메인마다 `WebFetch(domain:<host>)` | `Bash,PowerShell,Edit,Write,NotebookEdit` | `--safe-mode`, `--disable-slash-commands` | `dot_agents/skills/deep-research/SKILL.md`(`a330b11e…`), `dot_claude/workflows/deep-research.js`(`4d7392eb…`) |

- 두 staged 프로필 모두 `--restricted`, `--strict-mcp-config`, `--no-chrome`,
  `--setting-sources ""`, `--permission-prompts none`, `--max-turns 20`을 유지하고
  `--settings <run>/home/.claude/settings.json`을 더한다. 권한 모드는 기존과 같이
  `dontAsk`|`auto`만 받는다.
- `auto`에서는 허용 규칙이 경계가 아니다. 거부 목록에 없는 도구 사용을 분류기가
  허가할 수 있다. 그래서 도구 존재는 `--tools`로, 파일·네트워크는 srt로 막는
  것을 경계로 본다. WebFetch를 어떤 도메인에 쓰든 srt `allowedDomains`(모델 도메인 +
  `--web-domain` 목록 + ready를 확인한 relay port)를 벗어나지 못한다.
- Agent·Workflow 자식을 만들 수 있다는 것은 격리 증거가 아니다. 자식이 같은 경계에
  남는지는 계약 문서 §3.2 수락 조건으로 live에서 따로 판정한다.

staging(`stage_research_config`)은 `prepare_run` 뒤, 자식 시작 전에 부모가 한다.

1. 원본 위치는 이 스크립트의 chezmoi source checkout(`reviewed_source_root`)이다.
   설치본(`~/.local/bin/oss-research-worker`)이나 `~/.claude`·프로필 디렉터리·동기화
   스킬은 쓰지 않는다. 설치본에서 staged 프로필을 고르면 Blocked다.
2. 원본마다 source root·중간 디렉터리·파일이 symlink가 아닌지 보고 `O_NOFOLLOW`로
   읽는다. 내용 SHA-256이 코드에 고정한 pin과 다르거나, 파일이 없거나, 일반 파일이
   아니면 Blocked다. 모두 확인한 뒤에만 복사한다.
3. `run/home/.claude/{skills,workflows,agents,commands,rules,output-styles,plugins}`를
   만들고(staging 대상이 아니면 빈 채로), 원본을 0444로 복사한다. 복사본은 다시 읽어
   hash를 대조하고 다르면 Blocked다. 설정 파일
   `settings.json`(argv와 같은 allow/deny 규칙, `disableAllHooks: true`)도 부모가
   0444로 쓴다. corpus-extract의 agent 정의는 `run/policy/claude-agents.json`(0400)이다.
4. 기록(원본 경로·staged 경로·hash, 프로필, 도메인, agents 파일 hash)은
   `run/policy/staged-manifest.json`(0400)과 결과 manifest의 `staged`에 남는다.
   manifest에는 `execution_profile`도 들어간다.
5. srt 정책의 `denyWrite`에 위 디렉터리 7개와 `settings.json`·`settings.local.json`·
   `CLAUDE.md`를 더한다. `run/policy`·`run/result`는 원래 거부다.
   `run/home/.claude` 자체는 막지 않는다(Claude가 `projects/` 등을 쓴다).
6. 수거 때(`staged_problems`) staged 파일 hash, 보호 디렉터리에 추가된 항목, 보호
   파일의 새 생성, staged manifest·agents 파일 변경을 다시 본다. 하나라도 있으면
   실패다(`staged-changed:`·`staged-added:`·`staged-manifest-changed`).
   staging이 Blocked면 `staging-blocked:`로 기록하고 자식을 시작하지 않는다.

입력 검사: `--safe-mode`를 빼면 Claude가 작업 디렉터리의 `CLAUDE.md`·프로젝트 설정을
읽으므로, staged 프로필은 입력 어디에든 `CLAUDE.md`·`CLAUDE.local.md`·`.claude`·
`.mcp.json`이 있으면 종료 코드 2로 거부한다. 목록을 읽을 수 없는 하위 디렉터리가
있어도 거부한다(건너뛰지 않는다). 환경은 `read-only`와 같은 허용 목록이다
(부모 `HOME`·`CLAUDE_CONFIG_DIR`·SSH·GH·Orca·MCP 비상속).

실측(srt 0.0.77, Python `/opt/homebrew/bin/python3`, 비AI probe,
`StagedBoundaryTests` 2건): 두 프로필에서 staged 파일 덮어쓰기·chmod·삭제·rename·
hard link, 스킬 디렉터리 rename·`rmtree`, `skills`·`workflows`·`.claude`·`home` 상위
디렉터리 rename, `settings.json` 교체·삭제, `settings.local.json`·`CLAUDE.md` 생성과
symlink, 새 skill·workflow·agent·command·rule·output style·plugin 생성, 빈 보호
디렉터리 `rmdir`, staged manifest·agents 파일 교체가 모두 `EPERM`이었다. 전후 hash가
같았다. `output/`·`tmp/`·`home/` 쓰기와 `home/.claude/projects` 생성은 성공했다.
대조군으로 staged `denyWrite`를 뺀 정책에서는 같은 시험이 실패했다(0444 mode만으로는
막히지 않는다).

미검증(live 필요, 계약 문서 R1~R5): Claude가 `--restricted`·`--setting-sources ""`
아래에서 staged 스킬·워크플로를 실제로 로드하는지와 같은 이름의 번들
`/deep-research` 중 어느 쪽을 쓰는지, `--agents` 파일이 `--safe-mode` 없이 적용되는지,
`Agent(corpus-extractor)`·`Workflow(deep-research)` 규칙이 실제로 그 대상만 허용하는지,
워크플로 자식이 받는 도구 목록, 실행된 스크립트 사본 hash, 계정 동기화 스킬 유입,
WebSearch 실행 위치. 인자 생성 성공은 스킬 실행 성공이 아니다.

## 실행 순서와 경계

1. 입력·run 경로 검사.
2. 실행 게이트(`require_runtime`). 지금은 항상 종료 코드 3으로 끝난다.
   - `verify_runtime`: `--runtime-prefix`가 없거나 `dist/cli.js`가 없으면
     설치되지 않았다고 알린다. 있으면 `package-lock.json`의 전체 패키지 집합이
     `SRT_LOCKED`(srt와 전이 의존성 4개의 버전·integrity)와 같아야 한다.
     설치된 `package.json`의 이름·버전과 `node_modules` 트리 SHA-256
     (`SRT_TREE_SHA256`)도 맞아야 한다. 통과하면 `[node, dist/cli.js]`를 쓴다.
   - `--credential-file`이 없으면 "no credential binding for <profile>"로 막는다.
   - 있으면 `checked_credential`로 메타데이터만 검사한다. 토큰 내용은 읽지
     않는다(`os.read`·`load_credential` 호출 없음, `ClosedGateTests`). 통과해도
     "no real launch has been measured"로 막는다. 상수·플래그·환경변수로 여는
     방식이나 자동 fallback은 두지 않았다.
   - 아래 3~5단계는 게이트 뒤에 있다. 지금은 테스트가 함수를 직접 불러서만
     실행된다. 런타임 시험은 실제 srt를 쓴다. relay 준비(`relay_launch`)도
     게이트 뒤다. 게이트가 막으면 relay·srt·claude 어떤 프로세스도 시작하지
     않고 run 디렉터리도 만들지 않는다(`ClosedGateTests`).
3. relay를 요청했으면 먼저 relay를 띄우고 ready 줄을 검사한다(「supervisor」).
   run 디렉터리 구성. `policy/`와 `result/`는 부모 전용이고, 조사
   프로세스의 쓰기 영역은 `home/`·`tmp/`·`output/`뿐이다. 정책 파일은
   `policy/srt-settings.json`(0400)이다.
4. `node <prefix>/…/dist/cli.js --settings <정책> -- <claude 실제 경로> --print
   --output-format json …`. Claude 인자는 기존 도구 표면을 그대로 쓴다.
   `Read,Glob,Grep`만 제공하고
   `--safe-mode --restricted --strict-mcp-config`,
   `--permission-mode <dontAsk|auto>`·`--permission-prompts none`을 쓴다.
   Agent·검색·스킬은 꺼져 있다(이번에 바꾸지 않았다).
5. 결과 수거(`collect`). 부모가 stdout을 상한 있는 버퍼로 받아 `result/`에 둔다.

## supervisor (relay + 조사 프로세스)

`run_supervised`가 relay와 조사 프로세스를 `Supervisor` 하나로 감싼다. 게이트
뒤라서 지금은 함수 시험에서만 실행된다.

- **deadline 하나**: `--timeout`으로 monotonic deadline 하나를 만든다. 모든
  프로세스는 `start_new_session`으로 자기 그룹을 가진다. deadline이 지나면 새
  프로세스를 시작하지 않는다(`timeout` 기록).
- **신뢰 relay 경로**: 실행기 옆의 고정 이름만 쓴다. source tree에서는
  `executable_oss-research-relay`와 같은 checkout의 `dot_codex/skills`, 설치본에서는
  `oss-research-relay`와 `~/.codex/skills`다. 인터프리터는 실행기 자신의
  `sys.executable`이다. relay가 없으면 `Blocked`다. relay 환경은 허용 목록
  (`PATH`·`LANG`·`LC_*`·`TERM`)과 `PYTHONDONTWRITEBYTECODE`뿐이다. GitHub 토큰은
  넘기지 않는다(relay는 무인증).
- **ready 검증**: 부모가 relay 시작 전에 두 형제 스킬 파일(`issues`, `sibling`)을
  직접 해시한다. ready 줄은 줄바꿈 포함 16 KiB 이하 한 줄이어야 한다. 읽는 동안
  relay stderr도 상한 안에서 비운다. JSON은 중복 키를 거부한다. 필드는 정확히
  `event`·`host`·`port`·`base_url`·`sources`이고, `ready`,
  `127.0.0.1`, 1–65535 정수 port(bool 제외), `http://127.0.0.1:<port>`,
  부모 해시와 같은 `sources`여야 한다. 하나라도 어긋나면 조사 프로세스를
  시작하지 않는다.
- **정책 결합**: 검증된 port만 `allowedDomains`에 `127.0.0.1:<port>`로 더한다.
  `allowLocalBinding: false`는 그대로다. 기본 정책은 넓히지 않았다. 모델 실행
  정책은 `api.anthropic.com` + relay port이고, 비AI probe 전용 정책
  (`build_policy(..., model=False)`)은 relay port 하나뿐이다.
- **출력 상한**: stdout 8 MiB, stderr 1 MiB, relay stderr 1 MiB, ready 뒤 relay
  stdout 16 KiB. 넘으면 즉시 두 그룹을 죽이고 `output-too-large: <stream>`으로
  실패한다. 잘린 앞부분이 올바른 JSON이어도 성공으로 수거하지 않는다.
- **종료 조건**: 조사 프로세스가 끝나고 파이프가 닫히면 끝난다. 끝난 뒤
  2초(`EXIT_GRACE_SECONDS`) 안에 파이프가 닫히지 않으면 `child-pipes-held-open`
  이다. relay는 정리 시점까지 살아 있어야 한다. 조사 프로세스보다 먼저, 또는
  같은 순간에 끝나도 `relay-exited`다.
- **정리**: 모든 그룹에 `killpg(SIGKILL)`, 5초(`CLEANUP_SECONDS`) 안에서 reap하고
  파이프를 닫는다. 그 시간 뒤에도 그룹이 남아 있으면 `process-group-survived`로
  기록한다. deadline 뒤 정리도 이 5초로 끝난다.
- **실패 원인**: manifest의 `failures`에 정확한 원인을 남긴다: `timeout`,
  `relay-not-started: <예외>`, `relay-exited`, `relay-ready-too-large`,
  `relay-not-ready: <이유>`, `output-too-large: <stream>`,
  `child-not-started: <예외>`, `child-pipes-held-open`,
  `process-not-reaped`, `process-group-survived`. 하나라도 있으면 `exit_code`는
  null, `status`는 failed이고 `report.md`를 만들지 않는다. relay를 썼으면
  manifest `relay`에 port·sources·relay SHA-256이 들어가고 relay stderr는
  `result/relay.log`에 남는다. run 디렉터리 자체(`prepare_run`)가 실패하면
  믿을 수 있는 manifest가 없으므로 예외로 끝난다(종료 코드 4).
- **`launch`**: 기존 `(exit_code, stdout, stderr)` 형태를 유지한다. relay 없이
  같은 `Supervisor`를 쓴다. 무제한 `communicate`는 없어졌다. supervisor가 멈춘
  경우 exit_code는 None이고, 원인은 `run_supervised` 쪽에만 남는다.

### 제한: setsid 탈출

그룹 밖으로 나간 자손은 정리하지 못한다. 실측(실제 srt 0.0.77): sandbox 안의
probe가 fork한 자손이 `os.setsid()`에 **성공**했다. 그 자손은 stdout을 쥔 채
남았다. supervisor는 2초 grace 뒤 `child-pipes-held-open`으로 실패를 기록하고
파이프를 닫고 끝났다(약 3초, 기한 안). 그러나 자손은 cleanup 뒤에도 **살아
있었다**(시험이 직접 종료). 그 자손은 srt 안에서 시작했으므로 같은 Seatbelt
프로파일 아래에 있을 것으로 보지만, 이것은 확인하지 않았다. 완전한 정리를
보장하지 못하므로 게이트는 닫힌 채 둔다. sudo나 다른 권한 우회는 쓰지 않았다.

### 환경 변수 (허용 목록)

부모 환경에서 `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`만 넘긴다.
`HOME=run/home`, `TMPDIR=run/tmp`, `CLAUDE_CONFIG_DIR=run/home/.claude`는
새로 지정한다. GitHub 토큰, `GH_CONFIG_DIR`, SSH agent, `GIT_SSH_COMMAND`,
AWS 등 클라우드 변수, Orca 핸들·소켓 변수, 부모 `CLAUDE_CONFIG_DIR`은
넘기지 않는다. 부모의 `ANTHROPIC_API_KEY`와 `CLAUDE_CODE_OAUTH_TOKEN`도 넘기지
않는다. 게이트 뒤 경로에서 인증 파일을 준 경우에만 `build_environment`가 그 파일
값을 `CLAUDE_CODE_OAUTH_TOKEN` 하나로 넣는다. 부모의 같은 이름 변수는 그때도 쓰지
않는다. relay 환경(`relay_environment`)과 srt 정책은 인증과 무관하게 그대로다
(relay 무인증, `allowedDomains`에 OAuth 도메인 없음).

**srt는 환경을 거르지 않는다**(실측). 부모가 넘긴 변수는 그대로 sandbox 안에
보인다. 그래서 이 허용 목록이 유일한 필터다. srt는 자기 프록시용 변수를 추가한다:
`HTTP(S)_PROXY`·`ALL_PROXY` 등, `NO_PROXY`, `GIT_SSH_COMMAND`(`nc -X 5` 프록시),
`GIT_CONFIG_PARAMETERS`, `JAVA_TOOL_OPTIONS`, `CLOUDSDK_PROXY_*`,
`SANDBOX_RUNTIME`이다. `CLOUDSDK_PROXY_PASSWORD`와 프록시 URL에는 srt가 만든 로컬
프록시 인증값이 들어 있다. 부모 비밀은 아니다. 값은 출력하지 않았다.

### srt 정책

sandbox-runtime README에 있는 키만 쓴다.

| 키 | 값 |
|---|---|
| `filesystem.denyRead` | `["/"]` (기본 거부) |
| `filesystem.allowRead` | `SYSTEM_READ`, 입력 디렉터리, run 디렉터리, `claude` 실제 파일 |
| `filesystem.allowWrite` | `run/home`, `run/tmp`, `run/output` |
| `filesystem.denyWrite` | `run/policy`, `run/result` |
| `network.allowedDomains` | `api.anthropic.com` |
| `network.deniedDomains` | `[]` (0.0.77은 이 키가 없으면 정책을 거부한다) |
| `network.allowUnixSockets` | `[]` |
| `network.allowAllUnixSockets` | `false` |
| `network.allowLocalBinding` | `false` |

`SYSTEM_READ`는 `/usr/share/icu`, `/etc`, `/private/etc/hosts` 셋뿐이다.
`/usr`·`/etc`·`/private/etc` 전체, `/bin`, `/sbin`, `/System`, `/dev`,
`/private/var/select`는 뺐다. 실측(srt 0.0.77, Claude Code 2.1.283, Python probe):

| 읽기 허용(+ claude 파일, cwd) | `claude --version` | `claude --help` | Python `getaddrinfo("localhost")` |
|---|---|---|---|
| 없음 | 성공 | Bun `panic` SIGTRAP, exit 1 | 실패 |
| `/usr/share/icu` | 성공 | 성공 | 실패 |
| `/usr/share/icu`, `/etc`, `/private/etc/hosts` | 성공 | 성공 | 성공(`::1`) |
| `/private/etc/hosts`만 또는 `/etc/hosts`만 | — | — | 실패 |

- dyld shared cache와 시스템 라이브러리는 srt 기본 프로파일로 이미 읽힌다.
  `/usr/lib`·`/System/Library`를 넣지 않아도 Claude·Python·git이 떴다.
- `/etc`를 넣으면 srt는 `(subpath "/etc")`를 만든다(라이브러리로 생성한 프로파일
  확인). `/etc`는 `/private/etc`로 가는 링크라서 이 규칙은 링크 노드만 덮는다.
  `/etc/passwd`, `/etc/shells`, `/private/etc/ssh/ssh_config`는 계속 거부됐다
  (런타임 시험). `/etc` 전체로 풀리는 현상은 관찰되지 않았다.
- 거부 확인(런타임 시험): `/etc/passwd`, `/private/etc/ssh/ssh_config`,
  `/usr/local` 목록, `/usr/bin` 목록, `/Library/Preferences` 목록,
  `/usr/share/zoneinfo.default/UTC`. `/usr/share/icu`는 ICU 데이터만 있다.
- Claude는 cwd를 읽지 못하면 `An unknown error occurred (Unexpected)`로 끝난다.
  실행기의 cwd는 읽기 허용된 입력 디렉터리다.
- srt는 `TMPDIR` 아래에 자체 Unix socket(`srt-mux-<pid>-0.sock`)을 만든다. 경로가
  104바이트를 넘으면 `listen EINVAL`로 시작하지 못한다(fail closed). run 디렉터리는
  짧은 경로에 둔다.
- 시험 fixture만 더 연다: Python 설치 경로와 `/opt/homebrew`(probe·git),
  `/usr/bin/security`와 `/System/Library/(Private)Frameworks`(Keychain 시험),
  `/private/etc/ssl/openssl.cnf`(git HTTP helper가 없으면 중단). 실행기 정책에는 없다.

모델 요청, 인증, 도구 사용 중 추가 읽기가 필요한지는 실측하지 않았다. OAuth를
쓰기로 결정하면 공식
문서상 `claude.ai`와 `platform.claude.com`도 필요하다. 인증 결정 전에는 넣지
않는다. GitHub 도메인도 없다. 조사 입력은 조정자가 검토한 수집 결과를 로컬
파일로 주는 기존 offline 경로를 재사용한다.

### 결과 수거

`collect`는 먼저 부모 전용 경로를 확인한다. run 디렉터리, `result/`,
`policy/`, 정책 파일이 만든 그대로여야 한다. symlink로 바뀌었거나, 다른 곳으로
풀리거나, 종류가 다르거나, `result/`가 비어 있지 않으면 아무 파일도 쓰지 않고
실패한다.

그다음 다음을 모두 확인한다. 하나라도 어기면 `status: failed`이고 `report.md`를
만들지 않는다.

- 종료 코드 0. 타임아웃이면 실패.
- stdout이 `type: result` JSON이고 `subtype: success`, `is_error: false`.
- `permission_denials`가 빈 목록. #209 실측에서 권한 거부가 있어도 `claude`는
  0으로 끝났기 때문이다.
- `result` 텍스트가 비어 있지 않음.
- `home/`·`tmp/`·`output/` 루트가 만든 그대로의 디렉터리임. 없어졌거나
  symlink로 바뀌었으면 실패.
- 그 안에 symlink나 영역 밖으로 풀리는 경로가 없음. `output/`에는 일반 파일과
  디렉터리만 있어야 함.

인증 값을 넘긴 실행이면 먼저 노출 검사를 한다(「인증」의 「노출 검사」).
노출이 있거나 검사가 상한에 걸리면 실패이고 `stdout.json`·`stderr.log`·
`relay.log`·`report.md`를 쓰지 않는다.

`result/`에는 `stdout.json`, `stderr.log`, `manifest.json`을 쓴다. 성공하면
`report.md`(0444)도 쓴다. manifest에는 상태, 프로필, 종료 코드, 문제 목록,
정책·보고서 SHA-256이 들어간다. `run_research` 경로면 `permission_mode`, 인증을
넘겼으면 `credential: {variable, exact_value_check}`도 들어간다(값·경로는 없음). 해시는 무결성 보조 수단이다. 내용의 안전성은
보증하지 않는다. 부모는 보고서를 실행하지 않는다.

### Orca 보고

조사 프로세스는 Orca Dispatch 워커가 아니다. reporter도 없다. Orca 소켓,
Dispatch capability, 터미널 핸들을 받지 않는다. 바깥 개발 워커가 manifest를
확인한다. 그다음 자기 live preamble의 공식 `worker_done`으로 **자기** Task를
보고한다. 결과 파일만으로 완료를 대리 정산하지 않는다.

## srt 설치 (전용 prefix, 버전 고정)

전역 설치나 `npx`는 쓰지 않는다. 설치는 커밋된 lock fixture로 재현한다.

```bash
PREFIX=/absolute/srt-0.0.77            # 짧은 경로, 사용자 쓰기 전용
mkdir -p "$PREFIX"
cp tests/fixtures/oss_research_worker/runtime/package.json \
   tests/fixtures/oss_research_worker/runtime/package-lock.json "$PREFIX/"
(cd "$PREFIX" && npm ci --ignore-scripts --no-audit --no-fund)
```

최초 실측 설치(2026-09-27)는 `npm view`로 패키지 정보를 본 뒤
`npm install --prefix … --save-exact --ignore-scripts @anthropic-ai/sandbox-runtime@0.0.77`로
했다. lock fixture는 같은 캐시에서 `name: oss-research-srt-runtime`인
`package.json`으로 `npm install --offline`해 만들었다. `node_modules`와 소스
패키지는 커밋하지 않았다.

| 항목 | 값 |
|---|---|
| 패키지 | `@anthropic-ai/sandbox-runtime` 0.0.77, repository `github.com/anthropics/sandbox-runtime`, maintainers는 `@anthropic.com` 계정 |
| tarball integrity | `sha512-uOe6kkAbo91r5shXXBxZ1DKbOpWmnXkNDDujXrFJRaHSG7D7s8b7Yfsu0pGDkYFgKZ2ECtVCNisl6pCYcaMF7A==` |
| 잠긴 의존성 | `@pondwader/socks5-server` 1.0.10, `commander` 12.1.0, `node-forge` 1.4.0, `zod` 3.25.76 (integrity는 `SRT_LOCKED`) |
| `node_modules` 트리 SHA-256 | `e39390f88844ecf70ca4923a99314be5a85a7a35460d31f8508fe6b55cdae643` (npm 숨은 lockfile `.package-lock.json` 제외) |
| lock fixture | `tests/fixtures/oss_research_worker/runtime/package-lock.json`. 게이트는 파일 해시가 아니라 패키지 항목(`SRT_LOCKED`)을 대조한다. 오프라인 시험이 fixture 항목 = `SRT_LOCKED`를 확인한다 |
| node / npm | v24.20.0 / 11.19.0 (nvm). srt CLI는 sandbox 밖의 node로 돈다 |
| `srt --version` | `1.0.0`. `process.env.npm_package_version || '1.0.0'`이라 버전 확인에 쓸 수 없다 |

재현성 확인:

- 캐시를 분리한 두 번째 prefix에 `npm install`로 다시 설치했다. 트리 차이는 숨은
  lockfile의 `name` 한 줄뿐이었다. 이 파일을 빼면 트리 해시가 같았다.
- 커밋한 lock fixture를 별도 임시 디렉터리에 복사하고
  `npm ci --offline --ignore-scripts`를 실행했다. 트리 해시가 `SRT_TREE_SHA256`과
  같았고 `verify_runtime`을 통과했다(`InstallTests`, 기존 캐시 사용, 네트워크 없음).

provenance와 무결성의 한계:

- integrity는 npm 레지스트리가 준 tarball과 lock이 일치한다는 뜻이다. 레지스트리
  계정·배포 파이프라인이 정상이라는 증거는 아니다. npm provenance 서명
  (`npm audit signatures`)은 확인하지 않았다. GitHub 소스와 tarball의 대조도
  하지 않았다.
- srt의 의존성은 `^` 범위다. 같은 명령으로 나중에 설치하면 새 의존성 버전이
  풀릴 수 있다. 그러면 `SRT_LOCKED`·트리 해시가 달라져 게이트가 막는다(fail
  closed). 커밋한 lock으로 `npm ci`를 쓰면 같은 버전이 설치된다. 버전을 올리려면
  새로 실측하고 lock·상수를 함께 갱신해야 한다.
- 트리 해시는 검사 시점의 값이다. 검사와 실행 사이에 prefix가 바뀌는 경우는
  막지 않는다. prefix는 조사 프로세스의 쓰기 영역 밖이다.
- node 바이너리, npm, macOS 자체의 무결성은 검증하지 않았다. PATH의 `node`를 쓴다.
  Claude 바이너리의 무결성도 검증하지 않았다.
- srt는 beta research preview다.

설치본 README와 소스(`dist/sandbox/macos-sandbox-utils.js`, `http-proxy.js`,
`sandbox-config.js`)에서 확인한 사항:

- 설정 파일이 없거나 비었거나 검증에 실패하면 기본값으로 떨어지지 않고
  종료한다. `--settings`로 준 파일은 반드시 있어야 한다.
- 읽기는 기본 허용이다. deny 후 allow로 다시 연다. 쓰기는 allow-only다.
- macOS에서 Seatbelt 프로파일은 프록시 포트로 가는 localhost 접속만 허용한다.
  `allowLocalBinding: true`면 `(remote ip "localhost:*")`가 추가된다.
- 허용 목록 필터는 DNS 조회·접속 전에 적용된다(`http-proxy.js`).
- 프로파일은 `com.apple.SecurityServer`·`com.apple.securityd.xpc` mach-lookup을
  고정으로 허용한다. 설정에는 추가 허용용 `allowMachLookup`만 있고 거부 키는
  없다(`sandbox-config.js`). Keychain 전용 설정 키도 없다.
- `network.filterRequest`(요청별 메서드·본문 검사)는 라이브러리 콜백 전용이다.
  CLI 설정 JSON으로는 쓸 수 없다. HTTPS에 적용하려면 실험적 `tlsTerminate`가
  필요하다.
- 스키마에는 `credentials`(환경변수·파일 mask, 프록시 치환)가 있다. README에는
  없어 쓰지 않았다.

## 실측 결과 (srt 0.0.77, 2026-09-27)

환경: macOS 26.4.1 (25E253), Python 3.14.7(Homebrew), git 2.55.0(Homebrew),
node v24.20.0, Claude Code 2.1.283(`--version`·`--help`만 실행).

재현 명령과 결과:

인터프리터는 `/opt/homebrew/bin/python3`(3.14.7)을 명시한다. 이 머신의 기본
`python3`는 Xcode 3.9.6이며, #82 조정자 재실행에서 runtime 10건이 실패했다.

```bash
PY=/opt/homebrew/bin/python3
OSS_RESEARCH_SRT_PREFIX=/absolute/srt-0.0.77 \
  $PY -B -m unittest tests.test_oss_research_worker_runtime -v
# Ran 19 tests in 8.8s — OK, exit 0
$PY -B -m unittest tests.test_oss_research_worker_runtime
# 변수 없음: 전부 skip, exit 0 — 증거 아님
$PY -B -m unittest tests.test_oss_research_worker
# Ran 35 tests — OK, exit 0 (오프라인 계약, fake srt)
$PY -B -m unittest tests.test_oss_research_worker_auth
# Ran 29 tests — OK, exit 0 (게이트 뒤 인증 준비, 가짜 sentinel·fake 자식)
OSS_RESEARCH_SRT_PREFIX=/absolute/srt-0.0.77 \
  $PY -B -m unittest tests.test_oss_research_worker_supervisor
# Ran 29 tests in 25.8s — OK, exit 0 (변수 없으면 실제 srt 3건 skip)
```

sandbox 안에서 도는 프로그램은 `tests/fixtures/oss_research_worker/probe.py`다.
AI가 아니다. 정책은 실행기의 `build_policy` 결과에 fixture용 읽기 경로
(`FIXTURE_READ`: Python 설치 경로와 `/opt/homebrew`, git용)만 더했다. 부모 환경에는
가짜 sentinel(`GH_TOKEN`, `SSH_AUTH_SOCK`, `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`, `ORCA_*` 등)을 넣고 실행기의 `build_environment`를 거쳤다.
모든 임시 파일은 `/private/tmp/osrw-*`에 만들고 지웠다.

| 시험 | 기대 | 실제 |
|---|---|---|
| 입력 읽기, `output/`·`tmp/`·`home/` 쓰기 | 성공 | 성공 |
| 입력 파일 변조·입력에 새 파일 | 거부 | EPERM, 입력 해시·목록 불변 |
| 가짜 부모 HOME 비밀 읽기·설정 쓰기 | 거부 | EPERM, 파일 불변 |
| 실제 HOME 목록·새 파일 | 거부 | EPERM, 파일 생성 없음 |
| 다른 worktree(fixture)·이 worktree에 새 파일 | 거부 | EPERM, 생성 없음 |
| `policy/` 교체·새 파일, `result/` 심기, `output/` 루트 이동 | 거부 | EPERM. `parent_path_problems`·`area_problems` 빈 목록 |
| `/private/tmp`, `/tmp/claude` 쓰기 | 거부 | EPERM |
| `/etc/hosts` 읽기 | 성공 | 성공 |
| `/etc/passwd`, `/private/etc/ssh/ssh_config`, `/usr/share/zoneinfo.default/UTC` 읽기, `/usr/local`·`/usr/bin`·`/Library/Preferences` 목록 | 거부 | EPERM |
| 자식 프로세스에서 위 전부 | 같음 | 같음 |
| `output/.mcp.json`, `home/.claude/settings.json` 쓰기 | README 필수 차단 목록은 cwd 기준 | 쓰기 성공. run 전용이고 부모가 읽지 않으므로 기록만 함 |
| 환경 sentinel(프로세스·자식) | 없음 | 없음. 남은 변수는 허용 목록 + srt 프록시 변수 + 셸 기본값 |
| 같은 sentinel을 거르지 않고 srt에 전달 | — | 보임. srt는 환경을 거르지 않음 |
| Orca 유사 Unix socket(부모가 listen, 부모 연결은 성공) | 거부 | 프로세스·자식 모두 EPERM, 서버 accept 0건 |
| 허용 `127.0.0.1:A`로 프록시 경유 GET | 성공 | 200 |
| 허용 포트 A 직접 접속(HTTP·TCP) | 거부 | EPERM. **임의 포트 허용이 직접 우회를 열지 않음** |
| 비허용 포트 B 프록시 경유 / 직접 | 거부 | 403 `X-Proxy-Error: blocked-by-allowlist` / EPERM, B 서버 0건 |
| 비허용 이름 `api.github.invalid` 프록시 경유 | 거부 | 403 blocked-by-allowlist |
| 실제 정책(로컬 허용 없음)으로 A | 거부 | 프록시 403, 직접 EPERM, A 서버 0건 |
| 허용 목적지로 REST POST / GraphQL mutation | 차단 필요 | **차단 안 됨**: 201 / 200, 서버가 기록 |
| 허용 목적지로 git push(HTTP) | 차단 필요 | **차단 안 됨**: `info/refs?service=git-receive-pack`가 서버에 도달. 서버가 403으로 거부 |
| 입력의 bare 저장소로 git push(file) | 거부 | 쓰기 경계로 거부("unable to create temporary object directory"), ref 불변 |
| `allowLocalBinding: true`에서 비허용 포트 직접 접속 | — | 성공. 직접 우회 경로가 열림. 실행기는 `false` 유지 |
| 정책 없음·빈 파일·손상 JSON·`deniedDomains` 누락·읽기 불가(0000) | 명령 실행 안 함 | 모두 비0 종료, `RAN` 출력 없음 |
| 알 수 없는 키(`network.allowedMethods`) | 거부 기대 | **조용히 무시하고 실행**. 오타·가상 키는 아무것도 제한하지 않음 |
| 실행기 CLI + 실제 prefix | exit 3 | exit 3 "srt 0.0.77 verified … no credential binding", run 없음 |
| 커밋한 lock으로 별도 임시 디렉터리 `npm ci --offline` | 같은 트리 | 트리 해시 일치, `verify_runtime` 통과 |
| 실제 `claude --version`(실행기 정책, `allowedDomains: []`, 인증 env 없음, 새 HOME) | 기동 | exit 0, sandbox 밖과 같은 `2.1.283 (Claude Code)` |
| 실제 `claude --help`(같은 조건) | 기동 | exit 0, `Usage: claude`. sentinel 출력 없음, 부모 전용 경로 불변 |
| 같은 조건에서 `/usr/share/icu`를 뺀 `claude --help` | — | Bun panic, 비0 종료 |
| 실행기 CLI + `cli.js` 변조 사본 / `node_modules` 삭제 | exit 3 | "tree differs" / "is not installed" |

참고: 첫 실행에서는 비허용 이름 시험에 `api.github.com`을 썼다. srt 프록시는
허용 목록 필터를 DNS 조회·접속보다 먼저 적용하므로 403으로 끝났다. 외부로
패킷이 전혀 나가지 않았는지는 관찰하지 않았다. 이후 `.invalid` 이름으로 바꿨다.

HTTP 시험 참고: srt는 `NO_PROXY`에 `localhost,127.0.0.1,::1`과 사설 대역을
넣는다. 이를 따르는 클라이언트(urllib, git)는 로컬 주소로 직접 접속을 시도해
EPERM을 받는다. probe는 프록시 모드에서 `NO_PROXY`를 지운다.

### Keychain

일회용 파일 Keychain(`/private/tmp/osrw-*/keychains/fixture.keychain-db`)에 가짜
sentinel 항목 하나를 넣었다. `security create-keychain`·`add-generic-password`는
명시 경로에만 썼다. 사용자 검색 목록(`list-keychains -d user`)과 기본 Keychain은
시험 전후 같았다. 시험이 끝나면 `delete-keychain`으로 명시 경로만 지웠다. 로그인·
기본 Keychain 항목은 읽지 않았다.

| 조건 | 실제 |
|---|---|
| sandbox 밖 조회 | sentinel 읽힘 (fixture 동작 확인) |
| 실행기 정책(Keychain 파일이 `denyRead` 안) | 조회 실패(`security` exit 44, "could not be found"), sentinel 출력 없음 |
| 같은 정책 + Keychain 디렉터리만 `allowRead` | **sandbox 안에서 sentinel 읽힘** |

추가 경계 검토(같은 fixture, 조정자 요청):

| 조건 | 실제 |
|---|---|
| `sandbox-exec -p '(allow default)(deny mach-lookup SecurityServer, securityd.xpc)'`만 | 조회 실패(exit 44). mach-lookup 거부는 파일형 Keychain에도 효과가 있다 |
| srt만(Keychain 디렉터리 읽기 허용) | sentinel 읽힘 |
| 위 deny 프로파일 안에서 srt 실행 | `sandbox_apply: Operation not permitted`, 시작 불가 |
| srt 안에서 위 deny 프로파일로 `sandbox-exec` | `sandbox_apply: Operation not permitted`, 시작 불가 |

macOS는 이미 sandbox된 프로세스에 두 번째 Seatbelt 프로파일을 적용하지 못한다.
그래서 srt와 별도 deny 프로파일을 합성할 수 없다. srt 설정에도 거부 키가 없다.
남는 방법은 셋이다. 모두 이번에 구현하지 않았다.

1. srt 라이브러리가 만든 프로파일 문자열을 고쳐 두 허용 규칙을 빼는 방식. srt
   패치에 해당하고 문서화된 기능이 아니다. 권하지 않는다.
2. 로그인 Keychain이 없는 전용 macOS 사용자 계정으로 조사 프로세스를 실행하는
   방식. 관리자 설정이 필요하다. 사용자 결정이다.
3. Keychain이 없는 Linux VM·컨테이너에서 실행하는 방식. 현재 docker daemon은
   미실행이다. 사용자 결정이다.

결론: srt에는 Keychain 차단이 없다. 파일형 Keychain은 파일 읽기 거부로만
막혔다. 프로파일이 `com.apple.securityd.xpc`·`com.apple.SecurityServer`를
허용하므로 로그인 Keychain·데이터 보호 Keychain(XPC 경로)의 차단은 **증명하지
못했다**. HOME 분리나 파일 deny만으로 Keychain이 막혔다고 주장하지 않는다.
현재 지원 설정(README 키)으로 이 mach 서비스를 거부하는 방법은 없다.

## 인증

인증 방식은 아직 정하지 않았다. 아래 `setup-token` 파일은 **지금 구현된 입력
검사 하나**일 뿐, 필수 요건으로 확정한 것이 아니다. 선택지는 격리 환경과 함께
정한다(`docs/research/2026-09-27-189-isolation-environment-options.md`):
부모가 대상 프로필로 만든 `setup-token` 토큰 파일 전달, 조사 전용 계정이나
Linux 게스트 안의 별도 로그인(`.credentials.json`), API key·`apiKeyHelper`
(유료 전환, 사용자 결정) 등이다.

공식 문서(authentication, 조정자 재확인)에 따르면 `claude setup-token`은
구독으로 모델 요청만 하는 장기 토큰을 만든다. 브라우저 승인 후 터미널에
출력하고 자동 저장하지 않는다. `--bare`는 이 OAuth 환경변수를 읽지 않는다.
격리 `CLAUDE_CONFIG_DIR`에서는 부모 프로필의 Keychain 로그인을 쓸 수 없다.

이번까지 구현한 것은 파일 검사와, 닫힌 게이트 뒤의 최소 전달 준비다. 토큰
발급·추출은 하지 않았다. 실제 부모 설정·토큰·Keychain·SSH는 조회하지 않았고,
로그인·`setup-token`·모델 호출·외부 접속도 하지 않았다. 시험은 가짜 sentinel
파일과 로컬 fake 자식만 썼다.

### 파일 검사 (`open_credential`, 게이트와 게이트 뒤 공통)

- 경로는 `<dir>/<profile>/oauth-token`이어야 한다. 다른 프로필 디렉터리면 거부한다.
  상위 경로에 다른 프로필 이름(`claude-profile1`/`claude-profile2`)이 있어도
  거부한다(프로필 오매핑 방지).
- 파일과 디렉터리는 symlink가 아니어야 한다. 디렉터리를 `O_NOFOLLOW|O_DIRECTORY`로
  열고, 그 descriptor 기준으로 파일을 `O_NOFOLLOW|O_NONBLOCK`으로 연다. 소유자·
  mode·종류·링크 수·크기는 연 descriptor의 `fstat`으로 본다. 그래서 검사한
  대상과 나중에 읽는 대상이 같다. FIFO는 막히지 않고 "not a regular file"로 거부한다.
- 현재 사용자 소유, 파일 0600, 디렉터리 0700이어야 한다.
- 링크 수가 1이어야 한다. 다른 프로필 디렉터리에서 같은 파일을 hard link로 거는
  경우를 거부한다.
- 크기는 20~4097바이트여야 한다.
- 입력·run 디렉터리 밖이어야 한다.
- 오류 메시지에 내용을 넣지 않는다. 시험이 확인한다.

닫힌 게이트(`require_runtime`)는 이 검사(`checked_credential`)만 하고 항상
`Blocked`로 끝난다. 토큰 내용은 읽지 않는다.

### 게이트 뒤 준비 (지금은 시험에서만 실행)

- `load_credential`: 위 검사 뒤 같은 descriptor에서 **한 번만** 읽는다. 읽은 뒤
  `fstat`의 크기·mtime이 달라졌거나 읽은 길이가 크기와 다르면 거부한다. 내용은
  한 줄 ASCII `[A-Za-z0-9._-]{20,4096}`(끝 개행 하나 허용)이어야 한다. 실제 토큰
  형식은 확인하지 않았다. 값은 `Credential` 객체에만 있다. `repr`/`str`은 프로필
  이름만 보이고, pickle·JSON 직렬화는 실패한다.
- 게이트가 열리면 `require_runtime`이 `checked_credential` 대신 `load_credential`
  결과를 돌려주는 구조다(`main`은 이미 `(runtime, claude, credential)`을 받는다).
  읽기는 여전히 한 번이다. 여는 변경은 실제 시험 게이트 뒤 별도 리뷰 대상이다.
- 프로필 대조: `run_research`·`run_supervised`는 `Credential.profile`이 실행
  프로필과 다르면 run 디렉터리·정책·프로세스를 만들기 전, 값을 꺼내기(`reveal`)
  전에 `Blocked`로 끝난다(`require_profile`).
- 전달: `build_environment(parent, run_dir, credential)`가 조사 프로세스 환경에만
  `CLAUDE_CODE_OAUTH_TOKEN`을 넣는다. argv·relay·정책·파일 복사로는 넘기지 않는다.
- 시작 전 거부: 값이 relay argv·환경에 있으면 relay를 띄우지 않고
  `credential-in-relay`, 조사 프로세스 argv(프롬프트 포함)에 있으면 조사 프로세스를
  띄우지 않고 `credential-in-argv`로 실패한다.

### 노출 검사 (수거, fail closed)

인증 값을 넘긴 실행에서 `collect`가 쓰기 전에 확인한다. 걸리면 실패이고
`stdout.json`·`stderr.log`·`relay.log`·`report.md`를 쓰지 않는다. manifest에는
위치만 남긴다(`credential-exposed: <stdout|stderr|stdout json|report|relay stderr|home|tmp|output>`).

- 스트림: stdout 원문, stderr, relay stderr, JSON으로 decode한 stdout
  (`\uXXXX` escape 대응), result에서 꺼낸 보고서 텍스트.
- 쓰기 영역 `home/`·`tmp/`·`output/`: 파일·디렉터리 **이름**과 일반 파일 **내용**.
  링크는 따라가지 않고 특수 파일은 읽지 않는다.
- 검사 상한: 파일 10,000개(`EXPOSURE_MAX_FILES`), 총 256 MiB(`EXPOSURE_MAX_BYTES`),
  30초(`EXPOSURE_SECONDS`). 넘거나, 읽을 수 없는 파일이 있거나, 목록을 읽을 수
  없는 디렉터리가 있으면(`os.walk`의 `onerror`로 모든 탐색 오류를 받는다. 조용히
  건너뛰어 clean으로 판정하지 않는다)
  `credential-scan-incomplete: <영역> <files|bytes|time|unreadable>`로 노출과 같이
  실패한다.
- 문제·실패 문구에 값이 들어 있으면(예: 값을 이름으로 쓴 symlink 경로)
  `credential-exposed: problem text`/`failure text`로 바꾼다.
- 마지막으로 직렬화한 manifest에 값이 있으면 manifest도 쓰지 않고 실패한다.

제한(정확한 범위):

- **정확한 값의 부분 문자열만** 찾는다. base64·URL 인코딩·쪼개기·뒤집기 등 다른
  변환은 찾지 못한다. JSON escape는 stdout 전체가 JSON일 때만 decode해서 본다.
  stdout이 출력 상한에서 잘려 값의 앞부분만 남은 경우도 찾지 못한다.
- 조사 프로세스와 그 자식은 환경에서 값을 읽을 수 있다. 모델 요청 본문으로
  `api.anthropic.com`에 보내는 경로는 막지 못한다.
- 노출을 찾아도 **자식이 쓴 원본 파일은 지우지 않는다**. run 디렉터리(0700)에 남으므로
  실패한 run 디렉터리는 사용자가 지워야 한다. 부모는 값을 결과 파일에 저장하지
  않는 것까지만 보장한다.
- run 디렉터리 밖(쓰기 영역 밖)은 srt 쓰기 경계에 맡긴다. 프로세스 메모리, srt
  자체 로그, OS 수준 기록은 검사하지 않는다.
- setsid로 그룹을 벗어난 자손(「제한: setsid 탈출」)은 검사 뒤에도 파일을 쓸 수
  있다.

이 검사는 "파일이 그 프로필 자리에 있고 다른 프로필과 공유되지 않는다"만
확인한다. 토큰이 실제로 그 프로필 계정의 것인지, 인증이 되는지는 증명하지 않는다.

`setup-token` 파일 방식을 고른 경우의 준비 절차(실제 시험 직전, 사용자가 직접):

1. 대상 프로필(profile1 또는 profile2)의 구독으로 `claude setup-token`을 실행하고
   브라우저에서 승인한다.
2. 출력된 토큰을 `<dir>/<profile>/oauth-token`에 저장한다. 디렉터리는 0700,
   파일은 0600으로 둔다. 셸 history·로그·argv에 남기지 않는다.
3. 실행기에 `--credential-file`로 경로만 넘긴다. 그래도 게이트는 닫혀 있고,
   게이트는 토큰 내용을 읽지 않는다.
   아래 실제 시험 게이트를 통과한 뒤 여는 코드를 별도로 리뷰한다.

## 미구현과 미증명의 구분

| 기능 | 코드 상태 | live 증거 | 독립 구현 계획(제안) |
|---|---|---|---|
| 스킬·Agent(deep-research) | **닫힌 게이트 뒤 구성기**. `read-only`는 그대로다. `corpus-extract`·`deep-research` 프로필이 도구 표면, 검토된 원본 staging(hash pin, manifest), staged 경로 `denyWrite`, 수거 때 변경 검사를 만든다(「조사 스킬 실행 구성」) | 없음. staged 변경 거부는 실제 srt 비AI probe만 | ① 도구 목록 확정(계약 문서). ② 구성기 완료(오프라인 + srt probe). ③ live에서 staged 로드·자식 경계(계약 §3.2, R1~R5)를 판정한다. ④ WebFetch 도메인과 relay 후보는 사용자·조정자 결정이다 |
| 인증 sentinel 바인딩 | **닫힌 게이트 뒤 최소 준비**. 게이트는 메타데이터만 검사. 게이트 뒤 `load_credential`(1회 읽기)·자식 환경 전달·시작 전 argv/relay 거부·수거 노출 검사가 있고 가짜 sentinel 오프라인 시험만 있다(`test_oss_research_worker_auth.py`) | 없음(sentinel 파일만) | ① 완료(오프라인). ② 가짜 토큰으로 실제 Claude가 명시적 인증 실패를 내는지 실측한다. 모델 도메인 접속이 필요하므로 조정자 승인 후에 한다. ③ 사용자가 발급한 실제 토큰으로 1회 호출하는 것은 사용자 단계다 |
| auto CLI 선택 | **CLI 준비만**. `--permission-mode dontAsk|auto`(기본 `dontAsk`)를 `build_command`에 넘긴다. 도구 표면은 같다 | 없음. auto로 AI를 실행하지 않아 auto 차단은 미검증 | ① 완료(오프라인). ② auto에서 위 차단 시험을 Claude 도구 경로로 반복한다. OS 경계는 srt가 맡으므로 auto에서도 같아야 하지만 실측 전에는 주장하지 않는다 |
| Claude 바이너리 기동 | 정책에 필요한 읽기 경로 반영 | `--version`·`--help`만 | 모델 요청 경로의 추가 읽기·네트워크 요구는 인증 단계에서 측정한다 |

## 검증 상태

| 항목 | 판정 | 근거 |
|---|---|---|
| srt 버전·잠금·트리 대조 | PASS (실측) | 실제 prefix 통과, 변조·삭제 사본 exit 3 |
| 정책 유효성(0.0.77) | PASS (실측) | 기존 후보 정책은 `deniedDomains` 누락으로 srt가 거부했다. 추가 후 실행됨 |
| 파일 경계(입력·부모 HOME·실제 HOME·타 worktree·정책·결과) | PASS (실측, fixture) | `FileBoundaryTests` |
| 자식 프로세스 동일 경계 | PASS (실측, fixture) | 파일·환경·socket·네트워크 시험의 child 단계 |
| 환경 sentinel 분리 | PASS (실측) | srt는 거르지 않음. 실행기 허용 목록이 거름 |
| Orca 유사 Unix socket 거부 | PASS (실측) | 서버 accept 0건 |
| 네트워크: 허용 조회 / 비허용·직접 우회 거부 | PASS (실측, 로컬) | `allowLocalBinding: false` 전제 |
| 네트워크: 허용 목적지로의 쓰기(REST·GraphQL mutation·git push) 차단 | **FAIL(미차단)** | srt는 목적지만 거른다. 현재 실행기는 GitHub를 허용하지 않아 쓰기 경로가 없다. 조회를 열려면 #192 중계가 필요 |
| 정책 부재·손상 fail closed | PASS (실측) | 알 수 없는 키는 무시되는 한계 있음 |
| 결과 수거·타임아웃·보고서 비실행 | PASS (mock) | `CollectTests` 12건 |
| supervisor: 한 deadline·출력 상한·ready 검증·실패 원인·두 그룹 정리 | PASS (오프라인 fake + 실제 srt 비AI probe) | `test_oss_research_worker_supervisor.py` 29건. 실제 srt 3건: relay 경유 조회 성공과 나머지 거부, 무응답 upstream에서 deadline 종료, setsid 관찰. 실제 srt 성공 manifest는 probe가 낸 result 모양 JSON이다. 모델 결과가 아니다 |
| supervisor: setsid 탈출 자손 정리 | **FAIL(제한)** | srt 안에서 setsid 성공, 자손 생존. 실패는 기한 안에 기록됨 |
| 인증 파일 형태 검사 | PASS (오프라인, sentinel) | `CredentialTests` 7건. 실제 토큰·바인딩 미검증 |
| 닫힌 게이트의 토큰 비읽기 | PASS (오프라인) | `ClosedGateTests`(auth): `os.read`·`load_credential` 없이 Blocked, 두 모드 모두 `main` exit 3·프로세스 없음 |
| 게이트 뒤 인증 전달·노출 검사 | PASS (오프라인, sentinel·fake 자식) | `test_oss_research_worker_auth.py` 29건. 다른 프로필 credential은 시작 전 Blocked, 목록 불가 디렉터리는 검사 미완료 실패, 자식 환경에만 전달, argv·relay 거부, stdout·JSON escape·보고서·stderr·relay log·영역 파일·이름 노출 시 실패와 결과 미저장, 검사 상한 fail closed. 인코딩된 값은 못 찾는 한계 있음. 실제 Claude 인증 미검증 |
| permission mode 선택 | PASS (오프라인) | `dontAsk`/`auto`만 허용, 기본 `dontAsk`, 도구 표면 불변. auto 동작은 미검증 |
| 시스템 읽기 최소화 | PASS (실측) | `SYSTEM_READ` 3개. 양성: Claude 기동·`localhost` 해석·hosts. 음성: `/etc/passwd` 등 6건 |
| lock 재현 | PASS (실측, 오프라인 캐시) | 커밋한 lock으로 `npm ci` → 같은 트리. node·OS·Claude 바이너리 무결성은 미검증 |
| Keychain | **미검증(부분 관찰)** | 파일형 fixture는 파일 deny로만 막힘. srt 설정으로 mach-lookup 거부 불가, 중첩 불가. 로그인·XPC 경로 미증명 |
| Claude 실행 파일 기동 | PASS (실측, 기동만) | `--version`·`--help` exit 0, 네트워크·인증 없음. 모델 요청 미검증 |
| 정상 조사(모델 호출·결과 반환) | 미검증 | 모델 호출 안 함 |
| staged 프로필 인자·정책·manifest | PASS (오프라인) | `test_oss_research_worker_skills.py`: read-only argv·정책 불변, 프로필별 도구·규칙, 도메인 검사(GitHub 계열 거부), pin 불일치·누락·symlink(파일·디렉터리·root) Blocked, 설치본 Blocked, 입력의 Claude 설정 거부, 부모 비밀 비상속, 수거 때 변경 실패, `main` 모든 프로필 exit 3 |
| staged 설정 보호 | PASS (실측, 비AI probe) | `StagedBoundaryTests` 2건. 상위 디렉터리 rename·삭제 포함 변경 거부, `output/` 쓰기 허용. denyWrite를 뺀 대조군은 실패 |
| 스킬·서브에이전트 호환성 | 미검증 | 구성기만 있다. Claude가 staged 스킬·워크플로를 로드해 실행하는지, 자식 경계는 live 필요 |
| auto 모드 | 미검증 | AI를 실행하지 않았다. 실행기 기본은 `dontAsk`이고 `auto`는 CLI 인자 준비만 했다 |

## 남은 실제 시험 게이트

게이트를 열기 전에 모두 필요하다. 하나라도 실패하면 닫힌 채 둔다.

1. **Claude 모델 요청 경로**: 기동(`--version`·`--help`)은 확인했다. `--print`로
   요청을 보낼 때 추가 읽기 예외나 `localhost` 프록시 해석이 Bun에서 되는지는
   미측정이다. 가짜 인증으로 명시적 실패가 나는지 먼저 본다.
2. **인증 바인딩**: 인증 방식을 먼저 고른다(「인증」). 그 방식으로 정상 모델 호출 1회. 만료·잘못된
   토큰에서 명시적 실패. 토큰이 argv·로그·결과에 남지 않는지 확인한다. 전달
   방식과 노출 범위는 사용자 결정이다.
3. **Keychain**: srt 설정에는 mach-lookup 거부 키가 없고, 별도 Seatbelt deny
   프로파일은 srt와 중첩되지 않는다(`sandbox_apply` EPERM). 따라서 srt 설정만으로는
   불가능하다. 최소 대안은 (a) 로그인 Keychain이 없는 전용 macOS 사용자 계정으로
   조사 프로세스 실행(관리자 설정 필요), (b) Keychain이 없는 Linux VM·컨테이너(현재
   docker daemon 미실행)다. 선택은 사용자 결정이다. 그 전까지 게이트는 닫혀 있다.
4. **조회 중계(#192, 별도 writer)**: 조정자 결정. 부모 소유 loopback 포트, srt
   `allowedDomains`에 정확한 `127.0.0.1:<port>`만, `allowLocalBinding: false`.
   조사 프로세스의 GitHub 직접 접속 금지. 범용 프록시·임의 URL 전달 금지. 공개
   읽기 최소 REST와 기존 스킬 GraphQL query만 허용한다. git-upload-pack은
   보류한다. 실행기는 이제 relay를 띄우고 ready를 검증한 port 한 줄만 허용
   목록에 더한다(「supervisor」). REST POST 405는 제품 supervisor 경로에서도
   확인했다. mutation·push 음성 시험은 #82 통합 시험 결과를 따른다.
   남은 것: setsid 탈출 자손의 정리(「제한: setsid 탈출」).
5. **스킬·Agent 호환성**: deep-research가 요구하는 도구 표면을 격리 안에서 실제로
   수행한다. 도구를 꺼서 성공 판정하지 않는다.
6. **auto**: `--permission-mode auto` 인자는 준비됐다. 위 차단 시험을 auto
   모드에서 반복한다. 미지원·폴백 동작도 기록한다.
7. **알 수 없는 정책 키**: srt가 무시하므로 실행기가 만든 정책의 키 집합을
   문서화된 키로 고정한다(오프라인 계약 시험). 상위 버전으로 올릴 때 재실측한다.

## 이전 검토에서 이어지는 사실

- 2026-09-23 Claude Code 2.1.280 실제 `claude-profile2` 세션에서 `gh issue
  create --help`를 요청했다. Claude는 제공 도구가 `Read`, `Glob`, `Grep`뿐이라
  실행할 수 없다고 응답했고, GitHub 요청은 발생하지 않았다. 이 결과는 도구
  표면의 증거다. OS 격리의 증거가 아니다.
- 서브에이전트나 Claude 도구 allowlist는 OS sandbox가 아니다.
- 이전 실행기는 `os.environ.copy()`와 차단 목록을 썼고, 같은 `HOME`과 프로필
  디렉터리를 상속했다. 지금은 허용 목록과 run 전용 `HOME`을 쓰고,
  작업트리 루트 실행 조건을 `--input-dir` 검사로 대체했다.
- 공식 문서 요약: sandbox runtime은 파일 도구·MCP·hook을 포함한 Claude Code
  프로세스 전체를 격리한다. 내장 Bash sandbox는 Bash·PowerShell·Monitor만
  격리한다. `failIfUnavailable`, `allowUnsandboxedCommands`는 내장 Bash
  sandbox의 `sandbox.*` 키라 쓰지 않았다.
- profile2 개발 워커는 세션에서 승인된
  `/Users/lee-kyu-hwan/.local/share/ai-account-profiles/claude/profile2`를 썼다.
  구버전 실행기와 셸 함수에 남은 `~/.claude` fallback은 정리 대상이다. 이
  실행기는 부모 프로필 디렉터리를 쓰지 않는다.

## 후속으로 보류

- 외부 저장소의 설치 스크립트·테스트·빌드 실행은 일회용 VM 단계로 분리한다.
- 조회 중계는 #192 별도 writer가 맡는다(위 게이트 4).
- API key 방식(유료 전환)이나 provider·model·effort 변경은 사용자 결정이다.

## 범위

이 실행기는 지정한 경로로 시작한 조사 프로세스만 제한한다. 일반 Claude·Codex
세션이나 사용자가 직접 실행한 Orca 에이전트의 권한은 바꾸지 않는다.
