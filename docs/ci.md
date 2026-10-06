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
