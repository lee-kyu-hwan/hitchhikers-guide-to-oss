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

Linux(bubblewrap)에서는 이 자손이 srt의 PID namespace와 함께 죽을 것으로
예상한다(「Linux (bubblewrap) 대응」). **미측정**이다. macOS의 FAIL은 그대로다.

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

이 머신(macOS 26.4.1)에서 시험한 두 중첩 방향 모두 두 번째 Seatbelt 프로파일
적용이 EPERM으로 실패했다. 따라서 이 경로로는 srt와 별도 deny 프로파일을 합성하지
못했다. srt 0.0.77 README에 문서화된 설정 키에도 거부 키가 없다. 이것은 그
경로 하나의 실측이며, 다른 모든 Keychain 차단 방식이 불가능하다는 증명은 아니다.
이 문서가 검토한 대안은 셋이다. 모두 이번에 구현하지 않았다.

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

## Linux (bubblewrap) 대응 — Debian 13 게스트 비AI 실측

설계는 `docs/research/2026-09-28-189-linux-runtime-plan.md`에 있다. 실측 증거·명령·로그는
`docs/research/2026-09-28-189-linux-isolation-evidence.md`에 있다. 2026-09-28 Lima VM
`oss-research-debian`(Debian 13 arm64, bubblewrap 0.12.0)에서 조사 사용자 `ossresearch`로
돌렸다. 게스트 전체 시험 240건 OK(skip 6)이고, cgroup·결과 회수 fixture도 통과했다.
**모델 요청 경로는 측정하지 않았다**(게이트 닫힘).

- **macOS 불변**: `SYSTEM_READ`(macOS 실측값), 정책 키, argv, 닫힌 게이트 메시지와
  순서는 그대로다. `build_policy`·`build_environment`·`require_runtime`에 `platform`
  인자(기본 `sys.platform`)가 붙었을 뿐이다.
- **플랫폼별 읽기**: `system_read(platform)`. Linux `SYSTEM_READ_LINUX`는 실측값
  `/usr/lib`, `/usr/bin/bash`, `/usr/bin/socat`, `/etc/hosts`다. 그 밖에 추가되는 것은 둘이다.
  하나는 링크 항목의 대상(Debian `/usr/bin/socat1`)이다. 다른 하나는 `runtime`으로 찾은 고정
  prefix의 `apply-seccomp` 한 파일이다(`build_policy(runtime=…)`). srt는 bwrap 안에서 명령을
  bash로 돌리고 apply-seccomp를 실행한다. multiarch 디렉터리와 loader만으로는 bash가 뜨지
  않았다. Linux에서는 **run 디렉터리를 읽기 목록에서 뺀다**. 넣으면 그 아래 쓰기 영역이
  읽기 전용으로 덮인다(실측). 쓰기 영역은 allowWrite로 읽힌다. policy·result는 자식에게
  보이지 않는다. 모델 요청 경로(TLS 루트 등)는 측정하지 않았으므로
  `SYSTEM_READ_MEASURED["linux"] = False`다. `/etc` 전체, `/home`, `/tmp`, `/proc`,
  `/usr/bin` 전체는 넣지 않는다(오프라인 시험이 고정한다). 알 수 없는 플랫폼은 `Blocked`다.
- **Linux 네트워크 정책(오프라인 검증만, 게스트 미검증)**: 모델 endpoint를
  `api.anthropic.com:443`으로 좁힌다(README `:port` 접미사). `network.deniedResolvedAddresses`에
  `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`(Lima host gateway·DNS 포함), `100.64.0.0/10`,
  `fc00::/7`을 둔다(README 키). srt는 loopback·link-local·metadata·자기 주소를 원래 거부한다.
  macOS 정책은 실측된 형태 그대로다. srt·조사 child·relay·unit 환경에 `*_PROXY`가 없음을
  시험으로 고정했다. srt 자체 환경에 proxy 변수가 있으면 parent proxy로 나가 resolved-address
  검사가 빠지기 때문이다(README 「Resolved-address check」).
- **`socatPath`(Linux만)**: srt는 링크 읽기 항목을 대상에만 bind한다. 그래서
  `/usr/bin/socat`이라는 이름이 안에 없고 in-sandbox 프록시가 뜨지 않았다. 정책 최상위에
  `socatPath`(해석된 socat)를 넣는다. srt 0.0.77 설정 schema 키이고 README에는 없다.
  넣은 뒤 프록시 응답을 실측했다. macOS 정책 키는 README 키 그대로다.
- **Linux 자식 PATH**: `build_environment`가 Linux에서 `PATH=/usr/bin:/bin`으로 고정한다.
  srt는 bwrap·socat·rg를 PATH로 찾으므로, 검사한 바로 그 파일을 쓰게 한다.
- **파일 검사 `linux_static_problems`** (프로세스를 띄우지 않는다. `require_runtime`이
  `verify_runtime` 뒤에 부른다): 비root, WSL1 아님, `/usr/bin`의 `bwrap`·`socat`·`rg`가
  root 소유 일반 파일이고 set-id·group/other 쓰기가 없음(심볼릭 링크는 대상을 검사),
  srt prefix가 `/opt` 아래로 해석되고 그 안 전체가 root 소유이며 group/other 쓰기·특수
  파일·prefix 밖으로 가는 링크가 없음, 이 아키텍처의 `apply-seccomp`가 링크가 아닌 root
  소유 실행 파일. 도구·`apply-seccomp`·prefix 위의 **조상 디렉터리 전체**(적힌 경로와
  해석된 경로 양쪽, `/`까지)도 root 소유·group/other 쓰기 없음이어야 한다. 쓰기 가능한
  조상이 있으면 검사 뒤에 rename으로 바꿔치기할 수 있기 때문이다.
  `user.max_user_namespaces`가 양의 정수, Debian식 `unprivileged_userns_clone`이
  있으면 1, cgroup v2, `mountinfo`에 호스트 공유(virtiofs·9p·fuse.sshfs) 없음.
  읽지 못한 파일은 문제로 센다. Ubuntu의 AppArmor userns 제한 값만으로는 거부하지
  않는다(전용 profile이면 1이어도 된다). 판정은 실행 preflight가 한다.
- **srt prefix 소유와 위치**: 게스트 root가 `/opt` 아래(예:
  `/opt/oss-research/srt-0.0.77`)에 설치한 prefix만 받는다. 조사 uid는 실행만 하고
  검사와 기동 사이에 바꾸거나 바꿔치기할 수 없어야 하기 때문이다. 조사 uid가
  `npm ci`한 prefix, `/opt` 밖 prefix, `/opt` 안의 링크가 밖을 가리키는 prefix는
  거부된다.
- **실행 preflight `linux_execution_preflight(prefix, environ)`**: bwrap의
  user·PID·net namespace + 새 `/proc` 조합, `apply-seccomp /bin/true`, AF_UNIX
  대조군(밖에서 생성 성공 exit 0)과 apply-seccomp 아래 AF_UNIX(EPERM·EACCES만 exit 3,
  EMFILE 같은 다른 오류는 exit 4로 실패)를 각각 5초 상한으로 띄운다. 조사 사용자
  환경에 `SSH_AUTH_SOCK`이 있으면 실패다. **프로세스를 띄우므로 `main`은 부르지 않는다.**
  게스트 task가 `RealLinuxPreflightTests`로 따로 부른다.
- **게이트**: Linux에서는 파일 검사에 문제가 있으면 그 목록으로, 없으면 "system read
  set is not measured yet"으로 `Blocked`(exit 3)다. 두 경우 모두 토큰을 읽지 않고
  relay·조사 프로세스·run 디렉터리를 만들지 않는다
  (`test_oss_research_worker_linux.LinuxClosedGateTests`).
- **시험 플랫폼화**: runtime·supervisor·skills 시험은 `darwin`·`linux`에서 실행된다.
  짧은 임시 경로(`/private/tmp`·`/tmp`), fixture 읽기 경로, Linux 경로 대체, 플랫폼별
  거부 errno를 나눴다. Linux에서 거부로 세는 errno는 EPERM·EROFS·EBUSY다. ENOENT는
  호스트에 그 경로(쓰기면 부모)가 있을 때만 센다. EACCES는 세지 않는다. 직접 TCP 거부는
  같은 포트에 호스트에서 접속되는 대조군이 있어야 센다. rename의 EXDEV(바인드 마운트
  경계)와 없는 denyWrite 경로 생성의 EEXIST(srt 읽기 전용 stub, 실행 뒤 잔여 없음 확인)도
  센다. `/tmp` 쓰기는 srt 전용 tmpfs에 남고 호스트에 닿지 않음을 확인한다(격리된 쓰기).
  Keychain, ICU 크래시, local binding 시험은 macOS 전용이다. 게스트에 git이 없어 git
  push 시험은 skip이다.
- **게스트 서비스**: resolved varlink와 D-Bus system bus socket은 srt 밖 조사 사용자는
  connect할 수 있다(대조군). srt 안에서는 거부됐다(`GuestServiceTests`).
- **cgroup 층**: `tests/fixtures/oss_research_worker/linux/cgroup_escape.py`가 먼저 유닛
  없이 setsid 자손이 살아남는지 확인한다(대조군). 그다음 `systemd-run`
  (`KillMode=control-group`) 유닛에서는 같은 자손이 남지 않음을 확인한다(3회 통과).
- **unit wrapper(실행 경로 연결)**: `dot_local/bin/executable_oss-research-linux-run`. 게스트
  관리자(root)가 실행하고, 고정 inner(제품 실행기 또는 비AI 검증 entry
  `linux/unit_inner.py`)를 `systemd-run --wait --pipe --collect --uid=ossresearch
  -p KillMode=control-group` 고정 argv로 한 unit에 넣는다. 실행기의 `parent_eof_guard`는
  wrapper 종료(SIGKILL 포함) 때 stdin EOF로 unit 주 프로세스를 끝낸다. wrapper는 timeout·
  SIGTERM/SIGINT 때도, 정상 종료 때도 항상 unit을 끝내고 unit 소멸·cgroup 소멸을 확인한다.
  게스트 실측(정상·timeout·SIGTERM·wrapper SIGKILL·worker entry 닫힌 게이트) 5건 ×4회 통과는
  리뷰 수정 **전** `3b26c50` 기준이다. 수정본(import 전 stdlib 신뢰 검사·PATH 제한·MemoryMax·정리
  오류 보존·run lock·none 모드 idle 확인)과 `--egress pin` 통합(B egress-pin 호출, fixture TLS)의
  게스트 재검증은 `docs/research/2026-09-28-189-cgroup-integration.md` 9절(E1·E2·E3)에 있다.
  근거는 `docs/research/2026-09-28-189-cgroup-integration.md`에 있다. 실제 조사 실행은 아직
  unit으로 돌리지 않았다(게이트 닫힘).
- **결과 회수**: `linux/supervised_run.py`(실 srt + relay + 비AI probe)의 run/result를
  호스트가 `limactl shell … tar` 스트림(상한 64 MiB)으로 받는다. `linux/verify_pickup.py`가
  `data` filter로 풀고 파일 집합·manifest `succeeded`·`report.md` 해시를 확인한다.
- **Linux setsid 시험**: 자손이 보고한 pid는 namespace pid라 `os.kill`로 판정하지
  않는다. 자손 argv에 있는 고유 marker 경로를 호스트 `/proc/*/cmdline`에서 찾는다
  (`processes_holding`). 먼저 살아 있는 대조 프로세스를 찾아 scan이 보이는지
  확인한다. `/proc`를 읽지 못하거나 자기 자신이 안 보이면 "남은 프로세스 없음"이
  아니라 오류다.

## 모델·effort 전달 (계약만, live 미검증)

- 근거: 고정된 Claude 2.1.283을 게스트 srt 안에서 `--help`로만 실행했다(허용 도메인 없음, 새 HOME, 인증·config·keychain 조회 없음, 모델 호출 없음). `--model <model>`(alias 또는 전체 이름)과 `--effort <level>`(`low, medium, high, xhigh, max`)이 있었다. 로그: `~/.oss-research-vm/logs/a189cg-claude-help-flags.log`(help 본문 sha256 `6bc118c7…d6f4`).
- 입력: `--model`·`--effort`는 둘 다 주거나 둘 다 생략한다. model은 한 인자로 그대로 넘기는 opaque 문자열이다. 인쇄 가능 ASCII 1–128자이고 공백·제어문자·선행 `-`는 거부한다. `claude-opus-5-5[1m]` 같은 CLI 표현은 그대로 둔다(argv라 셸을 거치지 않는다). effort는 help enum이다. 실행기는 기본값을 고르지 않고 값을 바꾸지 않는다.
- argv: `build_command`·`build_staged_command`는 값이 있을 때만 permission 플래그 앞에 `--model X --effort Y`를 넣는다. 없으면 argv가 이전과 byte 단위로 같다.
- live 조건: main은 게이트를 지난 직후, relay나 child를 만들기 전에 두 값이 없으면 `Blocked`(exit 3)다. 게이트를 test double로 열어도 기본 모델로 호출되지 않는다(`SelectionGateTests`). `run_research`도 credential이 있는 run에서 selection이 없으면 시작 전에 `Blocked`다. 지금 main은 닫힌 게이트에서 exit 3이다. 값을 줘도 마찬가지다.
- 노출 검사: credential 정확값 검사는 새 manifest 필드(`requested_*`)에도 적용된다. 값이 들어 있으면 manifest를 쓰지 않는다.
- 기록: manifest의 `requested_model`·`requested_effort`는 요청값일 뿐이다. 실제 모델 성공을 뜻하지 않는다. 결과 JSON에 `modelUsage`가 있을 때만 `observed_models`를 따로 남긴다.
- 값은 사용자가 나중에 준다. 전용 oauth-token 미확인 blocker는 그대로다.

## 비AI 수집 entry (`executable_oss-research-collect`)

조사 worker(`executable_oss-research-worker`의 `main`)와 수집 entry는 역할이 다른 별개 프로그램이다. unit wrapper에서는 `--entry worker|collect|verify`로 고른다.

| | worker `main` | 수집 entry `collect` |
|---|---|---|
| 하는 일 | 격리된 Claude 조사 child(모델 호출) | 최근 닫힌 PR 수집(비AI): relay와 검토된 collector를 srt 안에서 실행 |
| 게이트 | `require_runtime`: 측정된 launch 하나(Linux: Debian 13 aarch64, 측정 node·claude sha256, read-only, dontAsk, relay 없음, claude-profile2, claude-opus-5-5 medium, unit wrapper 안)만 열고 나머지는 닫는다. `SYSTEM_READ_MEASURED["linux"]`는 False다(`2026-09-28-189-linux-model-evidence.md` 11절) | 모델 gate와 무관하다. 모델·credential·profile·prompt·permission 인자와 코드 경로가 없다(시험이 확인). 그래서 모델 gate를 우회하는 수단이 될 수 없다 |
| srt 정책 | `build_policy(model=True)`: 모델 endpoint, 모델 readset | `build_policy(model=False)`: relay loopback port만. 읽기는 Linux 시스템 집합과 수집 전용 집합(pinned python, collector의 `SKILL_REVISION_PATHS` 6개 파일, tz `UTC`와 대상) |
| 재사용 | — | worker의 `relay_launch`·`build_policy`·`run_supervised`(모델 run이 아닐 때만 쓰는 `record` 인자)·`parent_eof_guard`·`verify_runtime`·`platform_problems`. 코드를 복제하지 않았다 |
| 상한 | 사용자 선택 | 고정: PR 5개/저장소(PR당 약 8요청 + 고정 3요청이 예산 60 안에 들어가는 값. 2026-10-03 전에는 3), 요청 예산 60(relay 예산도 같다), 명시 UTC 구간 31일 이하, relay 응답 4 MiB·총 64 MiB, 30 s |
| 결과 | `result/manifest.json`, `report.md` 등 | `run/output`(corpus·collection-manifest·inventory·transport)과 `run/result`(manifest·relay.log·stderr.log·collection-envelope). 출력 파일은 regular·16 MiB 이하만 O_NOFOLLOW·비차단으로 읽어 hash한다 |
| 회수 | `verify_pickup.py`(기본·auto 계약) | `verify_pickup.py --collect --collector-sha256 <호스트 검토 sha> --wrapper-record <root wrapper JSON> --upstream github-rest-via-relay|loopback-mock` |

- upstream·base URL·proxy를 넘기는 CLI 옵션이나 env는 없다. relay는 api.github.com에만 간다.
- 시험(`unit_inner.py collect`)은 같은 `run_collection`에 local mock upstream과 relay용 시험 `sitecustomize`만 옆에 둔다. 이때 envelope의 `upstream`은 `loopback-mock`이다. live 채택은 `--upstream github-rest-via-relay`를 요구하므로 mock 결과는 live로 받아지지 않는다.
- 실측과 한계: `2026-09-28-189-linux-model-evidence.md` 10절. (실제 GitHub 조회와 relay→pin→api.github.com은 2026-10-02·03에 실행했다: `2026-10-02-candidate-reads-live.md`, `2026-10-03-eslint-docs-research/README.md`.)

### 문서 위주 수집 보완 (#192 후속, 2026-10-03)

2026-10-03 조사(`docs/research/2026-10-03-eslint-docs-research/README.md` 결함 절)에서 발견한 결함 4건과, 문서 위주 조사에 필요한 수집 기능을 코드로 보완했다. 코드·gate·srt 정책·모델 경로는 바꾸지 않았고, relay 변경은 응답 헤더 전달 두 개뿐이다(허용 route·메서드·예산·쓰기 거부 규칙은 그대로).

| 변경 | 내용 |
|---|---|
| `--docs-only title\|label` (PR 수집, 기본 꺼짐) | 검색 단계는 고정 양성 형태 하나만 붙인다: `title` = `docs in:title`(제목에 단어 docs), `label` = `label:documentation`. 따옴표·부정·OR 없는 평평한 토큰이라 relay 검색 문법을 넓히지 않아도 통과한다. 검색이 못 거르는 부분은 hydrate 뒤 **고정 규칙**(`docs-title-prefix-or-docs-path/1`)이 거른다: 제목이 `docs`, `docs(`, `docs:`로 시작(대소문자 무시, `docs ...` 포함)하거나, 변경 파일 경로가 `docs/` 디렉터리 아래이거나 `.md`·`.mdx`·`README*`이면 통과(`CHANGELOG*`는 문서 변경으로 치지 않는다: release PR 때문). 둘 중 하나면 통과(OR). 규칙은 collector의 상수이며 호출자 입력이 아니다 |
| 필터의 manifest 기록 | 실행 record에 `docs_only`, 저장소마다 `docs_filter` = `{mode, rule, search_matched, hydrated, passed, dropped}`(검색 매치 n, hydrate m, 통과, 탈락). 탈락한 PR도 요청 예산을 쓴 것이므로 기록한다. 탈락은 경고도 partial도 아니다. cap은 **통과한** PR 수로 센다. 필터가 꺼져 있으면 두 필드가 없고 요청 fingerprint도 그대로다. envelope `request`에도 `docs_only`는 켰을 때만 있다 |
| `--outcome closed-unmerged` | 검색에 부정 토큰 `-is:merged` 대신 GitHub의 양성 qualifier **`is:unmerged`**를 쓴다. 2026-10-03 run에서 relay가 부정 토큰을 `403 search_grammar_not_allowed`로 거부해 이 값이 relay 경로에서 쓸 수 없었다. relay는 부정 토큰을 계속 거부한다 |
| PR cap | entry 상한 PR 3 → **5**(저장소당). 예산은 60 그대로: 3회차 실측 PR당 약 8요청, 고정 3요청 |
| 이슈 cap·예산(`collect-issues`) | 이슈 하나가 약 4요청(이슈·댓글·timeline REST 3 + GraphQL 1), 저장소당 고정 3요청이다(2026-10-03 실측 14건 = 59·60요청). 그래서 **기본값**을 cap 14·예산 60(예산이 읽을 수 있는 값)으로 하고, **상한**은 cap 20 그대로·예산 60 → **90**(3 + 4×20 = 83)으로 올렸다. cap 15는 예산 70 이상을 주면 완결된다(15에 기본 예산 60이면 14건 + partial인 것은 그대로다: 호출자가 예산을 정한다). worker의 relay 예산 상한(10,000)은 그대로다. 이전에 cap 20·예산 60 상한이 서로 맞지 않아 15건 지시가 항상 14건 + partial이 되던 불일치를 이 방향(기본값을 내리고 상한을 올림)으로 해소했다 |
| 닫힌 이슈 | `collect-issues --state closed\|all`, `--date-field closed`는 collector와 entry가 이미 지원했다(`open`+`closed` 날짜만 거부). 코드 변경은 없고 relay 경유 종단 시험으로 고정했다: state가 manifest `filters`에 남고, 검색은 `is:closed`·`closed:`로 나가며 부정 토큰이 없다 |
| 0건 수집의 회수 | `verify_pickup.py --collect`가 「검색 매치 0·수집 0」(`collection_status: complete`, 저장소 `no-results`, records 0)을 정상 결과(rc 0)로 받는다. 빈 corpus는 manifest가 설명할 때만 정상: 요청한 저장소마다 항목이 있고 `selected_count 0`이며, (`no-results`이고 `matched_count 0`) 이거나 (docs 필터를 요청했고 `collected`이며 `docs_filter.passed 0`)이다. 설명이 없거나 partial·failed·다른 저장소·불린 카운트면 계속 rc 1이다. adapter 오류·partial은 0건과 구분된다. `--issues`는 이미 0건을 받았다(변경 없음) |
| 인증 표기의 의미 | transport.json에 `credential_scope: "child"`와 `credential_scope_note`를 더했다. 기존 `credential`(`none: token_provider returned None …`)과 collector manifest의 `preflight.login: "anonymous"`는 값을 바꾸지 않는다(child 관점에서 맞다. collector manifest는 skill 소유 스키마이고 gh→urllib 바이트 동등성 비교기 `tests/transport_output_equivalence.py`가 비교한다). relay가 읽기 전용 토큰을 가졌는지는 envelope·run manifest의 `github_token.authenticated`가 말한다 |
| rate-limit 헤더 기록 | relay가 성공 응답에 `x-ratelimit-used`·`x-ratelimit-resource`를 더 전달한다(짧은 숫자·식별자일 때만). 어댑터가 요청마다 `rate_limit: {limit, remaining, used, resource}`(정수·식별자)를 transport 기록에 남긴다(REST·GraphQL 공통, 헤더가 없으면 필드 없음). 값은 쿼터 사실이고 비밀이 아니다. 토큰·Authorization·`reset`은 기록하지 않는다. 2026-10-03 조사에서 성공 응답의 한도를 헤더로 증명하지 못한 공백을 메운다 |

시험과 실측은 아래 「검증 상태」 표의 해당 행과 `docs/research/2026-10-03-eslint-docs-research/README.md` 결함 절의 해소 표시에 있다. 게스트 사례는 `tests/fixtures/oss_research_worker/linux/collect_docs_cases.sh`(`--egress none`, loopback mock)와 `github_token_cases.sh` T4~T7(sentinel)이다.

## 후보 verifier 읽기 entry (`executable_oss-research-candidate-reads`)

후보 verifier(`verifying-open-source-contribution-candidates`)의 GitHub 읽기 두 단계, `discover`와 `locus-search`를 비AI로 격리 안에서 실행하는 entry다. 수집 entry(`collect`, `collect-issues`)와 같은 구조이고 모델 gate와 무관하다. unit wrapper에서는 `--entry candidate-reads`로 고른다(시험 fixture entry `verify`와 다르다). mock(loopback)으로만 검증했다. 실제 GitHub 조회와 익명 `/search/code` 응답은 측정하지 않았다.

```text
executable_oss-research-candidate-reads --out-dir OUT --input-dir IN --stage discover \
    --repo OWNER/NAME --pattern PAT-001 [--pattern ...] [--max-candidates-per-repo 5] \
    [--max-candidates-total 5] [--max-clues-per-pattern 5] [--request-budget 60] [--timeout 300]
executable_oss-research-candidate-reads --out-dir OUT --input-dir IN --stage locus-search \
    --repo OWNER/NAME [--clue TEXT ...] [--max-clues-per-locus 5] [--request-budget 60] [--timeout 300]
```

| | 계약 |
|---|---|
| 하는 일 | relay(`relay_launch`, 인증 없는 GET 허용목록)를 부모가 띄우고, srt 안에서 `executable_oss-research-relay-verify --require-proxy`가 검토된 verifier를 skills 디렉터리에서 경로로 불러 `discover` 또는 `locus-search`를 실행한다. srt의 loopback proxy로만 relay에 닿는다(직접 접속은 srt netns가 거부) |
| 없는 것 | 모델·credential·profile·prompt·permission 경로, upstream·base URL·proxy 옵션, clone(`--allow-clone`·`--clone-root`·`--keep-clone`·`--fixture-dir`·`--api-version`을 넘기지 않는다), markdown 출력. 대상 저장소 코드는 실행하지 않는다(static 읽기만). 시험이 이것들을 고정한다 |
| 정책 | `build_policy(model=False)`: relay loopback port 하나만 `allowedDomains`. 부모의 proxy·토큰 변수는 넘기지 않는다 |
| 입력 | `--input-dir`는 단계가 정한 파일만 정확히 담는다: `discover`는 `analysis.json`, `locus-search`는 `discovery.json`과 `assessment.json`. 각 파일은 O_NOFOLLOW·비차단으로 여는 regular 파일, 4 MiB 이하, group·other 쓰기 불가, 중복 키·NaN 없는 JSON 객체다. 디렉터리도 group·other 쓰기 불가다. discover는 요청한 패턴이 분석에 있고 superseded가 아니어야 하고, locus-search는 discovery가 정확히 이 저장소 하나의 것이고 assessment가 그 discovery의 sha256에 묶여 있어야 한다 |
| 고정 상한 | 대상 저장소 1개(relay는 소유자의 `.github` 저장소도 허용: verifier의 정책 읽기가 필요로 한다), 패턴 1~5개(`PAT-<n>`), 후보 `per repo`·`total` 각 1~5, 패턴당 단서 1~5, locus당 단서 1~5, `--clue` 5개 이하·각 200자 이하(따옴표·역슬래시·제어문자·가장자리 공백 없음), 요청 예산 1~60(relay도 같은 예산), timeout 1800 s 이하. 범위 밖 인자·입력은 **relay를 띄우기 전에** exit 2다 |
| srt 읽기 집합 | Linux 시스템 집합 + 이 entry의 집합: pinned python, 어댑터가 옆에서 불러오는 `executable_oss-research-relay-collect`, verifier의 `REVISION_PATHS` 7개 파일(`SKILL.md`, `agents/openai.yaml`, `references/verification-contract.md`, `references/github-verification-contract.md`, `scripts/verify_candidates.py`, `tests/test_verify_candidates.py`, `tests/test_skill_contract.py`). **tz 파일은 필요 없다**(게스트에서 tz 없이 통과, 수집 entry와 다르다). 실행 때 verifier의 `REVISION_PATHS`를 **소스 텍스트(`ast`)에서** 읽어(verifier 코드는 부모에서 실행하지 않는다) 상수와 다르면 relay 전에 Blocked다. 상수가 verifier와 같은지는 시험이 고정한다. 목록에서 한 파일을 빼면 verifier가 출력 revision을 쓸 때 실패한다(게스트 사례 `drop-verifier-file`: exit 1, 출력 없음) |
| 출력 | `OUT/run/output/`의 `discovery.json`(discover) 또는 `locus-search.json`(locus-search), `verification-manifest.json`, `transport.json`과 `OUT/run/result/`의 `manifest.json`(이 run), `relay.log`, `stderr.log`, `read-envelope.json`. envelope는 모든 파일과 **입력 파일**의 크기·sha256을 요청, child의 exit, relay의 ready sources에 묶는다. 출력은 regular·16 MiB 이하만 O_NOFOLLOW·비차단·fstat로 읽는다(링크·FIFO·디렉터리·거대 sparse 파일은 fail closed) |
| 상태 | verifier의 exit 그대로다. 0은 `succeeded`(entry exit 0), 3은 `partial`(exit 5: 예산 소진, 익명 코드 검색 불가, 읽지 못한 원본), 그 밖은 `failed`(exit 4). 부모의 supervisor 실패가 있으면 항상 `failed`다. 모든 결과를 남기고 올리지 않는다 |
| 회수 | `verify_pickup.py --candidate-reads --stage S --verifier-sha256 H --relay-issues-sha256 H --relay-sibling-sha256 H --wrapper-record R.json --upstream U`(S는 `discover` 또는 `locus-search`, U는 `github-rest-via-relay` 또는 `loopback-mock`). 호스트가 커밋 트리에서 계산한 sha256과 대조하고(verifier, relay가 읽는 issues·sibling), 요청 범위·예산·대상 저장소, 모든 요청의 delivery(`relayed` 또는 4xx·5xx를 받은 `upstream-error`), verifier가 소비한 요청 수, envelope 크기·sha, 출력 revision(호스트 verifier의 `compute_revision()`), 입력 sha, 후보·단서 상한, upstream 구분을 본다. mock 결과를 live로 요구하면 FAIL이다 |

- 호스트 offline 시험: `tests/test_oss_research_candidate_reads.py`(실 relay와 실 adapter를 loopback mock에 붙여 만든 산출물을 한 member씩 고쳐 반례를 만든다. srt 없이 돌아 transport가 `direct`이므로 pickup 시험은 그 한 줄을 `srt-proxy`로 바꾼 member로 한다).
- 게스트 사례: `tests/fixtures/oss_research_worker/linux/candidate_reads_cases.sh`(`--egress none`, loopback mock). 실제 api.github.com을 대상으로 한 실행은 없다. 이름 해석만으로도 DNS 질의가 나갈 수 있어서다.
- 이 entry는 verifier가 읽는 근거만 모은다. 후보 판정(`record`)·`recheck`·`render`는 하지 않는다. 입력의 분석 산출물은 지금 synthetic fixture이고 실제 패턴은 아직 없다.

## 읽기 전용 GitHub 토큰 (비AI entry 셋의 `--github-token-file`)

`collect`·`collect-issues`·`candidate-reads`는 선택 인자 `--github-token-file /absolute/path`를 받아 relay에만 넘긴다. relay 쪽 계약(파일 검사, 붙는 곳, 노출 금지, 권장 토큰)은 `oss-research-relay.md` 「읽기 전용 GitHub 토큰」이 정한다. 아래는 entry와 회수 쪽이다. 설계·측정·한계는 `2026-10-03-github-read-token.md`다. 모델 gate와 모델 토큰 경로는 바뀌지 않았다.

| | 계약 |
|---|---|
| 기본 | **토큰 없음 = 익명**이다. 인자가 없으면 오류가 아니고 지금까지와 같다. 환경 변수(`GH_TOKEN`, `GITHUB_TOKEN`)와 `gh auth token`은 읽지 않는다 |
| 인자 | `--github-token-file`은 한 번만, 4096자 이하의 평범한 절대 경로(정규화됨, NUL 없음)여야 한다. 출력 디렉터리, `candidate-reads`의 입력 디렉터리, skills 트리 안의 경로는 거부한다 |
| 거부 시점 | entry 부모가 relay와 같은 검사(`load_github_token`)로 **relay를 띄우기 전에** 한 번 읽어 확인한다. 실패하면 고정 문구로 exit 2이고 relay·srt·unit 안의 프로세스는 시작하지 않는다. 부모는 값을 보관하지 않고 sha256만 남긴다 |
| 전달 | relay argv에 `--github-token-file <path>`만 덧붙인다(`launch_with_token`). srt 안의 child는 값도 파일도 받지 못한다. 정책의 읽기 집합이 그 경로를 덮으면 `Blocked`다(`token_path_is_readable_by_child`). 경로를 child에 알려 주지도 않는다 |
| 기록 | manifest와 envelope에 `github_token: {authenticated: bool, token_file_sha256: hex64 | null}`만 남는다. 값·접두어·길이는 없다. 익명이면 `authenticated: false`, sha256 `null`이다 |
| 회수 | `verify_pickup.py`는 이 표지를 **필수**로 본다(envelope와 run manifest가 같고 정확히 이 두 키). 표지가 없거나 모양이 틀리면 FAIL이다. relay.log·transport.json·run manifest·envelope에 자격 모양(`ghp_`·`github_pat_`·`Bearer`)이 있으면 FAIL이다. 마지막 옵션 `--forbid-value-file FILE`을 주면 **모든** member에 그 값이 정확히 있는지도 본다(파일은 일반 파일, 4096바이트 이하, 8자 이상 한 줄이어야 하고 못 쓰면 건너뛰지 않고 FAIL) |
| 게스트 설치 | 모델 토큰과 같은 방식이다. run 전용 tmpfs `/run/oss-research-credentials/<RID>/github-readonly/token`(위쪽 root 0711, 디렉터리 ossresearch 0700, 파일 0600, `O_EXCL`·`O_NOFOLLOW`)에 설치하고, 설치 전에 그 경로만 지우는 reaper(systemd timer)를 걸어 활성을 확인한다. 끝나면 사본과 reaper를 지운다. 이 설치는 시험 하네스(`github_token_cases.sh`)가 한다. wrapper는 `--` 뒤 인자를 그대로 넘기므로 바뀐 것이 없다. srt의 읽기 집합에는 이 경로가 없다 |

- 호스트 offline 시험: `tests/test_oss_research_github_token.py`(가짜 sentinel만).
- 게스트 사례: `tests/fixtures/oss_research_worker/linux/github_token_cases.sh`(`--egress none`, loopback mock, sentinel만). 세 entry의 normal(mock upstream이 모든 요청에서 `Bearer <sentinel>`을 보는지 mock 쪽에서만 대조), 인자 없는 익명, srt child의 읽기 시도(`token-probe`), wrapper SIGKILL, 설치 직후 harness SIGKILL(reaper가 사본을 지움), 인자 거부를 본다.
- 실제 토큰과 실제 `api.github.com`은 사용하지 않았다. 실제 토큰 측정은 별도 작업이다.
- 비밀값 파일(토큰 등)은 `secret-file-store <경로>`로 만들고, 에이전트는 값을 대화에 받지 않고 `receiving-secrets-from-user` 스킬 절차를 따른다.
- 조사 대상 저장소와 조사 이력은 `docs/research/oss-research-targets.md` 등록부에 적고, 조직 단위 선별은 호스트의 읽기 전용 `oss-research-scope`로 한다(격리 entry가 아니다).

## 시계 검사 (unit wrapper의 실행 전 조건)

Linux 게스트(Lima/VZ)는 호스트 Mac이 잠자는 동안 **시스템 시계가 멈추고** RTC(`rtc-efi`, 호스트의 시각)는 계속 간다. 게스트에서 NTP에는 닿지 못하므로(조사·resolver uid의 송신이 막혀 있다) `systemd-timesyncd`는 동기화하지 못한다. 2026-10-02에 시스템 시계가 RTC보다 7시간 1분, 19분 46초 늦은 일이 두 번 있었다(`2026-10-02-189-isolation-acceptance-matrix.md` 9절 3, 11절). 시계가 틀리면 실제 모델 호출·TLS 인증서 검증·pin lease 판단이 어긋나므로 `executable_oss-research-linux-run`이 **모든 entry(`worker`·`collect`·`collect-issues`·`candidate-reads`·`verify`)와 모든 egress 모드**에서 실행 전에 시계를 대조한다. 새 네트워크 허용(NTP), nft·timesyncd 설정 변경은 쓰지 않는다.

| | 계약 |
|---|---|
| 시점 | bootstrap(신뢰 검사)과 run lock 획득 뒤, **unit·pin·relay·자격보다 먼저**. lock을 쥔 채 하므로 다른 run이 중간 상태를 보지 않는다 |
| 비교 | RTC(`/sys/class/rtc/rtc0/since_epoch`, 정수 초, 최종 경로를 따라가지 않고 열어 64 B까지 읽고 양의 정수만 받는다)와 `time.time()`의 차이(시스템 − RTC) |
| 허용 오차 | **5초**(`CLOCK_TOLERANCE_SECONDS`). 근거: RTC가 정수 초이고 두 읽기가 원자적이지 않아 정상일 때도 최대 1.5초쯤 차이가 나고, 시계가 영향을 주는 가장 짧은 시간(pin lease 60초, relay 요청 기한 30초)보다 한 자리 작다. 안이면 아무것도 하지 않는다(`action: none`) |
| 재설정 | 5초를 넘으면 `/usr/bin/date -u -s @<RTC>`(고정 argv, 고정 환경 `LC_ALL=C`·`PATH=/usr/bin:/bin`, 10초 timeout, shell 없음)로 시스템 시계를 RTC에서 다시 맞춘 뒤 RTC와 다시 읽어 허용 오차 안인지 확인한다(`action: resynced`). 네트워크 요청 없음 |
| Blocked 조건 | 모두 exit 3(`EXIT_BLOCKED`)이고 **아무것도 시작하지 않는다**: `rtc-unreadable`(RTC를 읽지 못함, 값이 정수가 아님, 재설정 뒤 다시 읽기 실패), `offset-too-large`(차이가 7일 초과: RTC 자체를 의심, 재설정하지 않는다), `resync-failed`(프로그램 실패·timeout), `still-off-after-resync`(맞춘 뒤에도 허용 오차 밖). 조용히 진행하는 fallback은 없다 |
| 신뢰 | 재설정 프로그램 `CLOCK_SET_PROGRAM`은 `nft`·`systemd-run`과 같은 `trusted_file` 검사 대상이다(절대 경로, 링크 아님, root 소유, group·other 쓰기 불가, set-id 아님, 두 철자의 상위 디렉터리 전부 닫힘). 하나라도 어긋나면 worker를 import하기 **전에** bootstrap에서 Blocked다. PATH 탐색·사용자 인자·환경 변수로 바꿀 수 없다(시계 관련 옵션·변수 없음, 시험이 고정) |
| 증거 | wrapper record의 `clock`: `checked_at`(RTC 시각), `tolerance_seconds`, `offset_before`(시스템 − RTC, 초), `action`(`none`/`resynced`/`blocked`), `offset_after`, `reason`(Blocked일 때). Blocked일 때는 `unit: null`, `failures: ["clock-guard: <reason>"]`인 record가 stdout에 한 줄 나가고 stderr에 고정 문구가 나간다. 이 record는 이미 있는 Blocked 규칙(exit 3, 시작 전)을 따른다 |
| 끝난 뒤 | 같은 record의 `clock.after_run`에 끝난 뒤의 차이(`offset`, `within_tolerance`, `error`)를 남긴다. **읽기만 하고 시계를 맞추지 않으며 실행 결과·`failures`·exit code를 바꾸지 않는다**: 호스트 pickup이 `failures == []`를 성공의 조건으로 쓰므로 환경 변화로 이미 끝난 실행을 실패로 뒤집으면 기존 판정 규칙이 깨진다. 허용 오차를 넘었으면 `within_tolerance: false`로 표시하고, 그 run의 시각 의존 증거는 의심해야 한다 |

**한계**: ① 이 검사는 **실행이 시작될 때**의 시계만 맞춘다. 실행 도중 호스트가 잠들어 시계가 다시 멈추면 막지 못하고, `after_run`에 표시될 뿐이다(호스트가 잠들지 않게 하는 것은 호스트 쪽 일이다). ② 한 번에 최대 7일까지만 맞춘다. ③ RTC가 UTC라는 가정(`RTC in local TZ: no`)과 RTC가 호스트 시각이라는 가정에 기댄다. ④ 허용 오차 5초 안의 어긋남은 그대로 둔다. ⑤ NTP 동기화는 하지 않으므로 게스트의 `System clock synchronized`는 계속 `no`다. ⑥ 시스템 시계를 앞이든 뒤든 RTC로 바꾸므로 그 사이에 이미 있는 파일의 mtime과 순서가 어긋날 수 있다(게스트에서 실행 중인 다른 run이 없다는 run lock 아래에서만 한다).

시험: `tests/test_oss_research_clock_guard.py`(주입한 시계·RTC·재설정으로 오차 안, 재설정 성공, 맞춘 뒤에도 어긋남, RTC 읽기 실패, 재설정 실패, 오차 과대, untrusted 프로그램, 모든 entry×egress에서 Blocked일 때 `launch`·unit·pin 확인이 호출되지 않음, 끝난 뒤 표시), 게스트 사례 `tests/fixtures/oss_research_worker/linux/clock_guard_cases.sh`(실제로 시계를 과거로 돌린 뒤 wrapper 실행).

## 미구현과 미증명의 구분

아래 스킬·Agent 행의 구성기 구현은 PR #226으로 **MERGED**(main `f723ee3`)됐다. #82
워크플로·통합 시험은 PR #225로 **MERGED**(main `08910e6`)됐다. 둘 다 이 브랜치의 기준
(`origin/main` `08910e6`)에 들어 있다. 병합은 이슈 수락이 아니다. 실제 모델로는 검증하지 않았다. 승인된 오프라인 개발은 착수마다 ask하지
않지만, live gate(실제 환경 구성·인증·live 실행)는 닫힌 채 유지한다.

| 기능 | 코드 상태 | live 증거 | 독립 구현 계획(제안) |
|---|---|---|---|
| 스킬·Agent(deep-research) | **닫힌 게이트 뒤 구성기**. `read-only`는 그대로다. `corpus-extract`·`deep-research` 프로필이 도구 표면, 검토된 원본 staging(hash pin, manifest), staged 경로 `denyWrite`, 수거 때 변경 검사를 만든다(「조사 스킬 실행 구성」) | 없음. staged 변경 거부는 실제 srt 비AI probe만 | ① 도구 목록 확정(계약 문서). ② 구성기 완료(오프라인 + srt probe). ③ live에서 staged 로드·자식 경계(계약 §3.2, R1~R5)를 판정한다. ④ WebFetch 도메인과 relay 후보는 사용자·조정자 결정이다 |
| 인증 sentinel 바인딩 | **닫힌 게이트 뒤 최소 준비**. 게이트는 메타데이터만 검사. 게이트 뒤 `load_credential`(1회 읽기)·자식 환경 전달·시작 전 argv/relay 거부·수거 노출 검사가 있고 가짜 sentinel 오프라인 시험만 있다(`test_oss_research_worker_auth.py`) | 없음(sentinel 파일만) | ① 완료(오프라인). ② 가짜 토큰으로 실제 Claude가 명시적 인증 실패를 내는지 실측한다. 모델 도메인 접속이 필요하므로 조정자 승인 후에 한다. ③ 사용자가 발급한 실제 토큰으로 1회 호출하는 것은 사용자 단계다 |
| auto CLI 선택 | **CLI 준비만**. `--permission-mode dontAsk|auto`(기본 `dontAsk`)를 `build_command`에 넘긴다. 도구 표면은 같다 | Linux 1회: mode 적용은 관찰했지만 모델 자동 전환으로 FAIL이다. OS 차단 증거는 없다(`2026-09-28-189-linux-model-evidence.md`) | ① 완료(오프라인). ② auto에서 위 차단 시험을 Claude 도구 경로로 반복한다. OS 경계는 srt가 맡으므로 auto에서도 같아야 하지만 실측 전에는 주장하지 않는다 |
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
| 네트워크: 허용 목적지로의 쓰기(REST·GraphQL mutation·git push) 차단 | srt 단독은 **FAIL(미차단)**, relay 경유는 relay 검사로 거부 | srt는 목적지만 거른다. 실행기는 GitHub를 허용하지 않고, supervisor가 띄운 #192 relay port만 허용한다. REST 쓰기 405는 제품 supervisor 경로, GraphQL mutation·CONNECT 터널 등은 #82 통합 시험(`tests/test_oss_research_offline_integration.py`)에서 확인. git push 경로는 relay에 없다 |
| 정책 부재·손상 fail closed | PASS (실측) | 알 수 없는 키는 무시되는 한계 있음 |
| 결과 수거·타임아웃·보고서 비실행 | PASS (mock) | `CollectTests` 12건 |
| supervisor: 한 deadline·출력 상한·ready 검증·실패 원인·두 그룹 정리 | PASS (오프라인 fake + 실제 srt 비AI probe) | `test_oss_research_worker_supervisor.py` 29건. 실제 srt 3건: relay 경유 조회 성공과 나머지 거부, 무응답 upstream에서 deadline 종료, setsid 관찰. 실제 srt 성공 manifest는 probe가 낸 result 모양 JSON이다. 모델 결과가 아니다 |
| supervisor: setsid 탈출 자손 정리 | **FAIL(제한, macOS)** / Linux **PASS(실측)** | macOS: srt 안에서 setsid 성공, 자손 생존. 실패는 기한 안에 기록됨. Linux(Debian 게스트): PID namespace로 정리됨(`/proc` 대조 scan). cgroup 유닛 fixture도 통과 |
| Linux 분기(정책·파일 검사·실행 preflight·닫힌 게이트) | PASS (오프라인 fake + Debian 게스트 실측) | `test_oss_research_worker_linux.py`, `RealLinuxPreflightTests`. 증거 `docs/research/2026-09-28-189-linux-isolation-evidence.md` |
| Linux 비AI 격리(파일·환경·socket·네트워크·relay·staged·fail closed·결과 회수) | PASS (실측, Debian 게스트) | 같은 증거 문서. 모델 경로 읽기 집합, Linux `allowLocalBinding`, git 경로는 미검증 |
| 인증 파일 형태 검사 | PASS (오프라인, sentinel) | `CredentialTests` 7건. 실제 토큰·바인딩 미검증 |
| 닫힌 게이트의 토큰 비읽기 | PASS (오프라인) | `ClosedGateTests`(auth): `os.read`·`load_credential` 없이 Blocked, 두 모드 모두 `main` exit 3·프로세스 없음 |
| 게이트 뒤 인증 전달·노출 검사 | PASS (오프라인, sentinel·fake 자식) | `test_oss_research_worker_auth.py` 29건. 다른 프로필 credential은 시작 전 Blocked, 목록 불가 디렉터리는 검사 미완료 실패, 자식 환경에만 전달, argv·relay 거부, stdout·JSON escape·보고서·stderr·relay log·영역 파일·이름 노출 시 실패와 결과 미저장, 검사 상한 fail closed. 인코딩된 값은 못 찾는 한계 있음. 실제 Claude 인증 미검증 |
| permission mode 선택 | PASS (오프라인) | `dontAsk`/`auto`만 허용, 기본 `dontAsk`, 도구 표면 불변. auto 동작은 미검증 |
| 시스템 읽기 최소화 | PASS (실측) | `SYSTEM_READ` 3개. 양성: Claude 기동·`localhost` 해석·hosts. 음성: `/etc/passwd` 등 6건 |
| lock 재현 | PASS (실측, 오프라인 캐시) | 커밋한 lock으로 `npm ci` → 같은 트리. node·OS·Claude 바이너리 무결성은 미검증 |
| Keychain | **미검증(부분 관찰)** | 파일형 fixture는 파일 deny로만 막힘. srt 0.0.77 README 키에 mach-lookup 거부가 없고, 이 머신에서 시험한 중첩 두 방향은 EPERM. 로그인·XPC 경로 미증명. 계정 분리·VM은 시험하지 않은 별도 경로 |
| Claude 실행 파일 기동 | PASS (실측, 기동만) | `--version`·`--help` exit 0, 네트워크·인증 없음. 모델 요청 미검증 |
| 정상 조사(모델 호출·결과 반환) | Linux dontAsk **1회 PASS(격리 모델 gate)**. 제품 gate는 그 측정 launch 하나에 한해 열림(`2026-09-28-189-linux-model-evidence.md` 11절, 제품 경로 실제 호출은 아직 없음) / 실제 조사 미검증 | Debian 게스트, root wrapper + `--egress pin`, `claude-profile2`·`claude-opus-5-5`·medium, 고정 답 `OK`. 요청 모델과 관찰 모델이 같다. `2026-09-28-189-linux-model-evidence.md` 3절. OSS 조회와 relay는 하지 않았다 |
| staged 프로필 인자·정책·manifest | PASS (오프라인) | `test_oss_research_worker_skills.py`: read-only argv·정책 불변, 프로필별 도구·규칙, 도메인 검사(GitHub 계열 거부), pin 불일치·누락·symlink(파일·디렉터리·root) Blocked, 설치본 Blocked, 입력의 Claude 설정 거부, 부모 비밀 비상속, 수거 때 변경 실패, `main` 모든 프로필 exit 3 |
| staged 설정 보호 | PASS (실측, 비AI probe) | `StagedBoundaryTests` 2건. 상위 디렉터리 rename·삭제 포함 변경 거부, `output/` 쓰기 허용. denyWrite를 뺀 대조군은 실패 |
| unit wrapper의 시계 검사 | PASS (오프라인 + 게스트 실측) | 시스템 시계를 과거로 돌리고 wrapper를 실행하면 재동기되고(record `resynced`), 재설정 프로그램을 쓸 수 없게 한 반례와 오차 과대에서는 Blocked로 아무것도 시작하지 않는다. 실행 도중의 호스트 잠자기는 막지 못한다(위 「시계 검사」). `2026-10-02-189-isolation-acceptance-matrix.md` 13절 |
| 후보 verifier 읽기 entry(`candidate-reads`) | PASS (mock) / 실제 조회 미검증 | 호스트 offline 22건, 게스트 사례(`--egress none`, loopback mock): discover complete·partial, locus-search, 읽기 집합 항목 누락·verifier 누락·어댑터 의존 누락, 대상 route 없음, 무응답, 쓰기 메서드 거부, proxy 없는 직접 접속, 부모 EOF, 인자·입력 거부(relay 전), unit 밖 Blocked. 실제 GitHub 조회·익명 `/search/code`·모델과의 연결은 미검증. `docs/research/2026-10-02-189-isolation-acceptance-matrix.md` |
| 문서 위주 수집 보완(#192 후속: `--docs-only`, `is:unmerged`, cap, 0건 회수, `credential_scope`, quota 헤더) | PASS (오프라인 + 게스트 mock 실측) / 실제 조회는 하지 않음 | 시험 먼저: 새 시험이 구현 전 실패(collector 시험 162건 중 failures 2·errors 40, relay·어댑터·entry·pickup 새 시험도 각각 구현 전 실패)한 뒤 통과. 호스트 offline(17개 모듈) **Ran 593 OK, skipped 39**(기준 556), collector 스킬 시험 PR 162·이슈 67 OK(기준 143·67). 게스트(`src-8972cd0`) `ossresearch` 묶음 **Ran 542 OK, skipped 11**, root `RealUnitWrapperTests` Ran 5 OK. 게스트 사례 `collect_docs_cases.sh`(`--egress none`, loopback mock): docs-only title/label(4건 중 1건 통과), cap 5(6건 중 5건), `is:unmerged`(검색에 부정 토큰 없음), 빈 검색(complete·records 0), quota(33/33 요청에 rate_limit), 닫힌 이슈(`state: closed`), cap 15 + 예산 70, 인자 거부 4건(cap 6, docs-only body, 이슈 예산 91, open+closed 날짜). 8개 산출물 모두 호스트 strict pickup rc 0(빈 검색 포함). sentinel `github_token_cases.sh` T1~T7·A·P·R·K·B `SUMMARY fails=0`, 값 0회. 증거 `/Users/lee-kyu-hwan/.oss-research-vm/logs/a192-collect-docs-20261003T104109Z/` |
| 스킬·서브에이전트 호환성 | 미검증 | 구성기만 있다. Claude가 staged 스킬·워크플로를 로드해 실행하는지, 자식 경계는 live 필요 |
| auto 모드 | **FAIL(1회 관찰) / OS gate INSUFFICIENT** | Linux 1회(관찰용 stream-json). init `permissionMode=auto`는 관찰했다. CLI가 refusal(cyber) 뒤 세션 전체를 `claude-opus-4-8`로 자동 전환했다(A-V4 FAIL). decoy 시도는 모두 Claude `--restricted` 층이 거부해 OS 증거가 없다(SKIP). 같은 문서 4절 |

## 남은 실제 시험 게이트

게이트를 열기 전에 모두 필요하다. 하나라도 실패하면 닫힌 채 둔다.

1. **Claude 모델 요청 경로**: 기동(`--version`·`--help`)은 확인했다. `--print`로
   요청을 보낼 때 추가 읽기 예외나 `localhost` 프록시 해석이 Bun에서 되는지는
   미측정이다. 가짜 인증으로 명시적 실패가 나는지 먼저 본다.
   Linux(Debian 게스트)에서는 dontAsk 1회로 요청 경로가 동작하는 것을 관찰했다.
   읽기 집합이 최소인지는 측정하지 않았고 `SYSTEM_READ_MEASURED["linux"]`는 그대로다.
   가짜 토큰 401 시험은 공개 endpoint 대상이라 하지 않았다.
2. **인증 바인딩**: 인증 방식을 먼저 고른다(「인증」). 그 방식으로 정상 모델 호출 1회. 만료·잘못된
   토큰에서 명시적 실패. 토큰이 argv·로그·결과에 남지 않는지 확인한다. 전달
   방식과 노출 범위는 사용자 결정이다. Linux에서는 사용자가 승인한 전용 파일을
   run 전용 tmpfs로 넘겨 정상 호출 1회를 했다. 로그와 결과에서 토큰은 0회
   나왔다(`2026-09-28-189-linux-model-evidence.md`). 만료·잘못된 토큰의 실패 시험은
   하지 않았다.
3. **Keychain**: srt 0.0.77 README 키에는 mach-lookup 거부 키가 없고, 이 머신에서
   시험한 별도 Seatbelt deny 프로파일의 중첩 두 방향은 `sandbox_apply` EPERM이었다.
   그 범위에서는 srt 설정만으로 막지 못했다(다른 방식의 불가능 증명은 아니다).
   검토한 대안은 (a) 로그인 Keychain이 없는 전용 macOS 사용자 계정으로
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
6. **auto**: `--permission-mode auto` 인자는 준비됐다. Linux 1회 실행에서 폴백을
   관찰했다. CLI가 refusal 뒤 `claude-opus-4-8`로 세션 전체를 전환했다. 이 전환을
   막을 방법이 정해지기 전에는 auto를 쓰지 않는다. decoy 시도는 Claude `--restricted`
   층에서 먼저 거부됐다. OS 경계는 기존 비AI 증거로 판단한다
   (`2026-09-28-189-linux-model-evidence.md`).
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
