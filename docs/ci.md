# CI

`.github/workflows/test.yml`이 저장소 시험(`tests/`), 분석 번들 시험(`skills/researching-open-source-contributions/analysis/`), 스킬 시험(`skills/*/tests`), `__pycache__` 검사를 돌린다. dotfiles의 CI(dotfiles `docs/ci.md`, dotfiles#271·#274)를 이 저장소 구조에 맞춰 옮긴 것이다.

## 러너와 트리거

- `ubuntu-latest`(Linux). macOS 분은 Linux보다 약 10배 비싸다. 표준 라이브러리만 쓰므로 `pip install`은 없다. dotfiles와 달리 zsh 설치 단계는 없다(zsh가 필요한 시험이 여기에는 없다).
- `pull_request`와 `workflow_dispatch`만. main push는 다시 돌리지 않는다. PR이 main 최신 위에 있으면 병합 결과는 PR에서 시험한 트리와 같다. PR 뒤 main이 앞서 나간 채 병합했다면 `workflow_dispatch`로 main에서 한 번 돌린다.

## 단계

| 단계 | 방식 |
|---|---|
| 저장소 테스트 | `tests/test_*.py` 모듈마다 별도 프로세스(`python3 -m unittest discover -s tests -p <모듈>.py`), `nproc × 2`개씩 동시에, 큰 파일부터. 저장소 루트에서 `discover`해야 `import tests.…`를 쓰는 모듈이 깨지지 않는다 |
| 분석 번들 테스트 | `analysis/test_*.py`(unittest)와 `selftest_*.py`(스크립트)를 모듈마다 별도 프로세스로. 한 프로세스에서 여러 모듈을 discover하면 offline guard가 하네스 시험의 하위 프로세스를 막는다 |
| 스킬 테스트 | `skills/*/tests`마다 `cd <스킬> && python3 -m unittest discover -s tests` |
| `__pycache__` 검사 | `PYTHONDONTWRITEBYTECODE=1`인데도 생기면 실패 |

이 머신(작성자 호스트)의 실행 증거(`~/.oss-research-vm/logs`)나 고정한 srt(`OSS_RESEARCH_SRT_PREFIX`), 실제 Linux `/proc`가 있어야 도는 시험은 CI에서 skip된다.

## 로컬에서 Linux CI 재현

저장소 루트에서 실행한다. 네트워크는 이미지를 만들 때만 쓰고, 시험은 `--network none`으로 돈다. 추적 파일(커밋하지 않은 변경 포함)만 컨테이너 안 사본으로 옮기므로 작업 트리는 바뀌지 않는다.

```sh
docker build -t oss-guide-ci-linux - <<'EOF'
FROM python:3.14
RUN useradd -m runner
USER runner
EOF

# COPYFILE_DISABLE: macOS tar 가 확장 속성을 ._* 파일로 넣으면 패키징 시험이 깨진다
git ls-files -z | COPYFILE_DISABLE=1 tar --no-xattrs --null -T - -cf - |
  docker run --rm -i --network none --cpuset-cpus=0,1 oss-guide-ci-linux bash -c '
    set -u; export PYTHONDONTWRITEBYTECODE=1
    mkdir ~/repo && cd ~/repo && tar xf - || exit 2
    git init -q && git add -A && git -c user.name=ci -c user.email=ci@localhost commit -qm ci || exit 2
    python3 -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"
    b=skills/researching-open-source-contributions/analysis
    for f in $b/test_*.py $b/selftest_*.py; do
      case "$f" in */selftest_*) out=$(python3 "$f" 2>&1 | tail -1) ;; *) out=$(python3 -m unittest "$f" 2>&1 | grep -E "^(OK|FAILED)") ;; esac
      echo "$(basename "$f"): $out"
    done
    for dir in skills/*/tests; do
      skill="$(dirname "$dir")"
      echo "$skill: $(cd "$skill" && python3 -m unittest discover -s tests 2>&1 | grep -E "^(OK|FAILED)")"
    done
    find . -name __pycache__ -print'
```

- 비root 사용자(`runner`)로 돈다. CI 러너도 비root이고, root면 mode-000 파일을 읽을 수 있어 일부 시험이 skip된다.
- Apple Silicon에서는 arm64 이미지가 돈다. CI는 x64다.

## 결과

2026-10-06, 분리 직후(dotfiles main `88f17d0`에서 추출 + 경로 정리). 로컬 Docker(`python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root). 같은 방법으로 dotfiles `88f17d0`의 해당 시험만 돌린 결과와 모듈별로 같다(시험 수·skip·결과).

| 묶음 | 이 저장소 | dotfiles의 같은 시험 |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 18모듈 | 656개 OK (skip 40) | `tests/test_oss_research_*.py` 656개 OK (skip 40) |
| 분석 번들 `test_*.py` 12개 | 모두 OK (skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2) | 같음 |
| 분석 번들 `selftest_*.py` 3개 | failures=0 / 129 cases 0 / 19 cases 0 | 같음 |
| 스킬 시험 6묶음 | 622개 OK (researching 136, analyzing 77, verifying 127, recent-closed 162, curated 53, issues 67) | 같음 |
| `__pycache__` | 0 | 0 |

dotfiles 저장소 시험 전체(904개, skip 55)와의 차이 248개·skip 15는 dotfiles에 남은 시험(orchestrator, secret-file-store, claude 프로필 등)이다.

macOS(작성자 호스트, 같은 날)도 모듈별 시험 수·skip이 dotfiles 사본과 같다. 두 쪽에서 똑같이 실패한 3모듈은 환경 탓이다: `test_oss_research_candidate_reads`·`test_oss_research_collect_issues`의 각 1건은 시험 사본 경로(`/private/tmp/claude-…`)에 `claude`가 들어 있어 「읽기 경로에 claude가 없다」 단언이 걸린다(다른 경로에서는 통과). `analysis/test_continue_plan` 1건은 이 호스트의 증거 `a78-split-gate-fresh-…/ledger/continued-by.json`이 dotfiles#288 실행 뒤 생겨서다(CI에서는 skip).

### 2026-10-07, install.sh와 형제 탐색 변경(hitchhikers-guide-to-oss#1)

같은 Docker 방식(`python:3.14` linux/arm64, `--network none --cpuset-cpus=0,1`, 비root). 새 시험은 `tests/test_install.py` 15개(S1~S4, 임시 HOME·BIN_DIR·프로젝트)와 workflow 스킬의 형제 탐색 순서 시험 2개다.

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 19모듈 | 671개 OK (skip 40) | 모듈별 모두 OK(skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip은 위와 같음), selftest failures=0 | `test_continue_plan` 1건 실패(위에 적은 이 호스트의 증거 `continued-by.json` 탓, 변경과 무관), 나머지 OK |
| 스킬 시험 6묶음 | 624개 OK (researching 138, 나머지 위와 같음) | 같음 |
| `__pycache__` | 0 | 0 |

### 2026-10-08, discover 예산 사전 계산과 분할 실행(hitchhikers-guide-to-oss#3)

같은 Docker 방식(`python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root). 새 시험은 verifier의 `DiscoverPlanTests` 6개(S1: 규칙 문서 15개·단서 8개의 사전 계산과 분할 제안, `plan` 명령, discover의 stderr 계획 줄)와 workflow 스킬의 `tests/test_split_discovery.py` 7개(S2: 정적 run + 검색 run 채택 → 검색까지 complete·레코드 생성, cap 축소 run, head 이동, 반례, 예산 소진 뒤 재계획)다.

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 19모듈 | 671개 OK (skip 40) | 모듈별 모두 OK(skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip은 위와 같음), selftest failures=0 | `test_continue_plan` 1건 실패(위에 적은 이 호스트의 증거 `continued-by.json` 탓, 변경과 무관), 나머지 OK |
| 스킬 시험 6묶음 | 637개 OK (researching 145, verifying 133, 나머지 위와 같음) | 같음 |
| `__pycache__` | 0 | 0 |

### 2026-10-08, 공개 전환 준비 — 개인 경로 기본값·fixture 가명화(hitchhikers-guide-to-oss#9)

같은 Docker 방식(`python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root). 변경은 실행 코드의 VM 루트 기본값을 `HOME` 기준(`~/.oss-research-vm`)으로 바꾼 것, `test_machine_settings.py`를 가짜 HOME으로 고정한 것(+1건: HOME을 따라가는 기본값), `pickup_partial/p4-eslint-markdown/collect.tar`의 커밋 작성자 이메일·실명 가명화(envelope의 bytes·sha256 재계산), curated 스킬 eval 문서의 scratchpad 절대 경로 placeholder화(고정 digest 갱신)다.

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 19모듈 | 671개 OK (skip 40) | 모듈별 모두 OK(skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip: assess 1, assess_contract 1, continue_plan 8, dry_plan 3, model_retry 1, prompt_contract 3, reduce_inputs 1, search_clues 1, stage_inputs 2; machine_settings 9개 OK), selftest failures=0 | `test_continue_plan` 1건 실패(위에 적은 이 호스트의 증거 `continued-by.json` 탓, 변경과 무관), 나머지 OK |
| 스킬 시험 6묶음 | 637개 OK (researching 145, analyzing 77, verifying 133, recent-closed 162, curated 53, issues 67) | 같음 |
| `__pycache__` | 0 | 0 |

컨테이너 안에서 `git grep -c lee-kyu-hwan -- skills bin tests install.sh`는 0건이다. `git grep -l "/Users/"`로는 4파일이 남는데 모두 「`/Users/`가 없어야 한다」는 가드 단언(curated·workflow·machine_settings 시험)과 호스트 공유 마운트를 흉내 내는 가짜 경로 `/Users/me`(worker_linux 시험)다.

### 2026-10-08, assess 입력의 검색 요약과 고정부 축소(hitchhikers-guide-to-oss#11)

같은 Docker 방식(`python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root). 변경은 `stage_inputs.py assess-inputs`의 prompt 렌더링(discovery 저장값은 그대로)과 그 지시문·동기화 표·시험이다. 새 시험은 `analysis/test_assess.py`의 7개(S1 `S1SearchSummary` 2개: 질의당 100건 레코드 2개가 상한 안에 들고 수치를 보존, 정적 읽기가 다른 레코드는 자기 값을 유지; S2 `S2RefusedAfterSummary` 1개: 요약 뒤에도 초과하면 기존 메시지로 거부; 뷰 함수 단위 `SearchSummaryViews` 4개)와 지시문 needle 추가다. S3(지시문↔strict 동기화)는 `test_prompt_contract.py`의 기존 시험이 새 문장(`보이지 않은 항목은 인용할 수 없다`)을 assess links 행으로 검사한다.

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 19모듈 | 671개 OK (skip 40) | 모듈별 모두 OK(skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip은 위와 같음; `test_assess` 32개), selftest failures=0 | `test_continue_plan` 1건 실패(위에 적은 이 호스트의 증거 `continued-by.json` 탓, 변경과 무관), 나머지 OK |
| 스킬 시험 6묶음 | 637개 OK (researching 145, analyzing 77, verifying 133, recent-closed 162, curated 53, issues 67) | 같음 |
| `__pycache__` | 0 | 0 |

S4(작성자 로컬, 읽기만, 모델 호출 0): `~/.oss-research-vm/logs/a3-css-discover-split-20261008T004910Z/wf-run/stages/discovery/gen-001/`의 합친 discovery(354,556 B, 조합 2)로 임시 디렉터리에서 오프라인 `assess-inputs`를 돌리면 prompt는 63,288 B(상한 65,536, 변경 전 292,478 B)다. discovery.json·manifest.json의 sha256은 실행 전후 같다.

### 2026-10-09, locus 단서에서 디렉터리 이름 제외(hitchhikers-guide-to-oss#13)

같은 Docker 방식(`python:3.14` linux/arm64, Python 3.14.8, `--network none --cpuset-cpus=0,1`, 비root). 변경은 verifier `_locus_clues`가 locus의 파일 이름(마지막 경로 조각 + `#fragment`)에서만 단서를 내는 것과 그 계약 서술이다. `locus-search`의 질의 생성과 `record`의 판정이 같은 함수를 쓰므로 locus당 요청(4 × 단서 수)은 디렉터리 토큰만큼 줄고, 다른 영향은 없다. 새 시험은 verifier의 `LocusDirectoryClueTests` 3개(S1: `docs/rules/x-y.md`에서 `rules`·`x-y` 둘 다에 반환된 PR은 같은 locus가 아니라 undecided, `unverified` 후보는 불변, `locus-search`는 `x-y`만 질의)와 단서 함수 시험 1개·fixture 단서 수 시험 1개다. S2는 기존 `LocusDuplicateGateTests`의 판정이 모두 그대로다(fixture locus의 단서가 `example-boundary`·`ExampleBoundary` 둘이 되어 입력 구성만 인덱스 대신 `CLUES[:-1]`을 쓴다).

| 묶음 | Linux | macOS(작성자 호스트) |
|---|---|---|
| 저장소 시험 `tests/test_*.py` 19모듈 | 671개 OK (skip 40) | 모듈별 모두 OK(skip 39; `worker_runtime` 21·`worker_linux` 9·`worker_supervisor` 4·`offline_integration` 3·`worker_skills` 2) |
| 분석 번들 `test_*.py` 12개 + `selftest_*.py` 3개 | 모두 OK(skip은 위와 같음), selftest failures=0 | `test_continue_plan` 1건 실패(위에 적은 이 호스트의 증거 `continued-by.json` 탓, 변경과 무관), 나머지 OK |
| 스킬 시험 6묶음 | 642개 OK (researching 145, analyzing 77, verifying 138, recent-closed 162, curated 53, issues 67) | 같음 |
| `__pycache__` | 0 | 0 (시험 전 수동 `import`가 남긴 `scripts/__pycache__` 1개는 시험이 만든 것이 아니며 지웠다) |

S3(작성자 로컬, 읽기만, 모델·GitHub 호출 0): `~/.oss-research-vm/logs/a3-css-assess-rerun-20261008T130416Z/`의 discovery·assessment·locus-search(sha256 `a17f01ad…`, `9dfebd57…`, `828ceeea…`)를 임시 디렉터리로 복사해 오프라인 `record`를 돌리면, 변경 전 코드(HEAD 사본)는 CAN-001을 `duplicate`/`duplicate-closed-other`로 두고 `locus-duplicate:` gap에 PR 6개(#565·#473·#202·#183·#171·#156)를 적는다. 변경 후에는 CAN-001이 `unverified`/`insufficient-evidence`로 남고 locus gap과 `matched_items`가 없다(옛 문서의 `rules` 단서는 판정에서 빠지고 `no-invalid-at-rule-placement` 하나로는 결정하지 않는다). CAN-002는 불변. 증거 디렉터리의 `SHA256SUMS` 157개는 실행 전후 모두 OK다.
