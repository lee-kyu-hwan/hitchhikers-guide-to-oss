# The Hitchhiker's Guide to OSS

> **Don't Panic.**

오픈소스에 처음 기여하는 사람을 위한 조사 도구다. 대상 저장소의 최근 PR·이슈를 모으고, 받아들여지는 기여의 패턴을 분석하고, 지금 손댈 만한 기여 후보를 근거와 함께 찾는다. 모델 호출과 GitHub 읽기는 격리된 Linux VM 안에서만 돈다.

이 저장소는 [dotfiles](https://github.com/lee-kyu-hwan/dotfiles)에서 분리했다(dotfiles#290). 이력은 `git filter-repo`로 옮겼다.

## 구조

| 경로 | 내용 |
|---|---|
| `skills/researching-open-source-contributions/` | 조사 workflow 스킬. `analysis/`는 격리 분석 번들(호스트 하네스, 단계 입력, strict 검사) |
| `skills/analyzing-open-source-pr-patterns/` | 수집 corpus에서 기여 패턴 추출 |
| `skills/verifying-open-source-contribution-candidates/` | 기여 후보 검증 |
| `skills/collecting-recent-closed-prs/`, `skills/collecting-curated-contribution-prs/`, `skills/collecting-open-source-issues/` | PR·트래커·이슈 수집 |
| `bin/oss-research-*` | 격리 실행기 12개(worker, relay, 수집·후보 읽기 entry, Linux unit wrapper, egress pin 등) |
| `share/linux/` | 호스트 회수·게스트 모델 1회·판정·운영 스크립트 |
| `share/staged/` | `deep-research` staged profile의 고정 원본 사본(아래 대응표 참고) |
| `install.sh` | 설치 스크립트(아래 「설치」) |
| `tests/` | 저장소 시험(`test_oss_research_*.py`, `test_install.py`)과 fixture |
| `docs/` | 설계 문서(`oss-research-worker.md`, `oss-research-relay.md`, `oss-research-targets.md`), [CI](docs/ci.md), [경로 대응표](docs/path-map.md) |

스킬 이름은 dotfiles 때와 같다(revision 결속과 모델의 스킬 선택 단서를 지키기 위해).

## 설치

이 저장소가 직접 설치한다(hitchhikers-guide-to-oss#1). 체크아웃을 하나 두고 `install.sh`가 그 체크아웃으로 symlink를 건다. 복사하지 않으므로 설치된 버전은 체크아웃의 tag가 정한다. 스킬은 당분간 프로젝트 범위([oss-learning-lab](https://github.com/lee-kyu-hwan/oss-learning-lab))에만 연결한다. 다른 작업에서 스킬이 잘못 호출되거나 컨텍스트 비용이 드는 일이 없고, 조사 결과와 기여 노트가 lab에 함께 쌓인다.

```sh
git clone https://github.com/lee-kyu-hwan/hitchhikers-guide-to-oss.git ~/.local/share/hitchhikers-guide-to-oss
cd ~/.local/share/hitchhikers-guide-to-oss
git checkout v0.1.4
./install.sh --project ~/code/oss-learning-lab   # 경로는 lab 체크아웃
./install.sh --check --project ~/code/oss-learning-lab
```

| 이 저장소 | `--project DIR` | `--user` |
|---|---|---|
| `skills/researching-open-source-contributions/`의 `SKILL.md`·`scripts/`·`references/`·`tests/` (`analysis/` 제외) | `DIR/.agents/skills/<스킬>/<항목>`, `DIR/.claude/skills/<스킬>/<항목>` | `~/.agents/skills/<스킬>/<항목>` |
| `skills/<그 밖의 스킬 5개>/` | `DIR/.agents/skills/<스킬>`, `DIR/.claude/skills/<스킬>` | `~/.agents/skills/<스킬>` |
| `bin/oss-research-*` 12개 | `${BIN_DIR:-~/.local/bin}/oss-research-*` (범위와 무관하게 항상) | 같음 |
| `share/`, `tests/`, `docs/`, `analysis/` | 설치하지 않음(저장소 체크아웃과 게스트 배포 트리에서만 쓴다) | 같음 |

- Codex는 작업 디렉터리부터 저장소 루트까지의 `.agents/skills`와 `~/.agents/skills`를, Claude Code는 프로젝트 `.claude/skills/`를 읽는다. 실행기는 명령어라 자동 호출되지 않으므로 늘 `~/.local/bin`에 둔다.
- workflow 스킬은 디렉터리 하나로 연결하지 않고 실제 디렉터리 안에 항목 4개를 연결한다. `analysis/`는 격리 분석 번들(호스트 하네스·단계 지시문·strict 검사)이라 스킬 내용으로 노출하지 않기 위해서다(dotfiles 때와 같은 모양).
- 같은 명령을 여러 번 실행해도 결과는 같다. 다른 대상을 가리키는 symlink나 실제 파일이 있으면 아무것도 만들지 않고 멈춘다. `--force`를 주면 symlink와 일반 파일만 바꾸고, 디렉터리는 끝까지 바꾸지 않는다. `--check`는 연결마다 `ok`·`missing`·`conflict`를 보여 주고 모두 `ok`일 때만 0으로 끝난다.
- `HOME`·`BIN_DIR`은 환경 변수다(시험은 임시 HOME·BIN_DIR로 돈다).

업데이트는 체크아웃을 옮기고 다시 연결한다. 제거는 `--uninstall`이다(이 체크아웃을 가리키는 연결만 지우고, 그 밖의 것은 `skip`으로 남긴다).

```sh
cd ~/.local/share/hitchhikers-guide-to-oss
git fetch --tags && git checkout vX.Y.Z && ./install.sh --project ~/code/oss-learning-lab

./install.sh --uninstall --project ~/code/oss-learning-lab
```

격리 실행(VM, 수집, 분석 호출)은 저장소 체크아웃에서 쓴다. 실행기는 symlink를 따라간 뒤 「`<root>/bin/<이름>`이고 `bin/` 옆에 `skills/`가 있는 트리」를 소스 체크아웃으로 보므로, `install.sh`로 연결한 실행기는 체크아웃의 `skills/`를 읽는다. 체크아웃 밖으로 복사한 실행기만 `~/.codex/skills`를 읽는다.

workflow 스킬이 형제 스킬을 찾는 순서는 `--skills-root` → `OSS_WORKFLOW_SKILLS_ROOT` → 자기 실제 디렉터리의 부모(체크아웃의 `skills/`) → 연결된 위치의 부모 → `~/.agents/skills` → `~/.codex/skills`다(`scripts/oss_workflow.py`의 `candidate_roots`).

머신 설정값은 환경 변수로 준다: `OSS_RESEARCH_VM_ROOT`(VM 작업 루트), `OSS_RESEARCH_VM_NAME`(Lima 인스턴스), `OSS_RESEARCH_RECORDS_ROOT`(시험이 읽는 실행 기록), `OSS_RESEARCH_SRT_PREFIX`(고정한 srt 설치). 자세한 것은 `skills/researching-open-source-contributions/analysis/README.md`.

## 시험

표준 라이브러리만 쓴다. 로컬 Linux 재현 방법과 결과는 [docs/ci.md](docs/ci.md).

```sh
python3 -m unittest discover -s tests
for d in skills/*/tests; do (cd "$(dirname "$d")" && python3 -m unittest discover -s tests); done
```

## 기록과 이슈 번호

- 날짜별 조사 기록과 증거(실행 로그, 판정, corpus)는 dotfiles [`docs/research/`](https://github.com/lee-kyu-hwan/dotfiles/tree/main/docs/research)에 있다. 이 저장소의 문서·주석에 나오는 `docs/research/20…` 경로는 dotfiles 경로다.
- 코드·문서의 이슈·PR 번호(`#78`, `#189`, `#290` 등)는 dotfiles 이슈·PR이다. 이 저장소의 이슈는 `hitchhikers-guide-to-oss#N`으로 적는다.
- 학습 기록과 기여 노트: [oss-learning-lab](https://github.com/lee-kyu-hwan/oss-learning-lab). 합치지 않고 서로 링크한다.

## 옛 경로 → 새 경로

[docs/path-map.md](docs/path-map.md)에 규칙과 파일별 대응(dotfiles main `88f17d0` 기준)이 있다.

고정 사본: `share/staged/`는 dotfiles의 deep-research `SKILL.md`·workflow를 sha256 `a330b11e…`·`4d7392eb…`로 고정한 사본이다. dotfiles 쪽이 바뀌어도 이 사본은 바뀌지 않는다(닫힌 게이트 뒤 profile 전용).

## 라이선스

MIT 라이선스다. 전문은 루트의 [LICENSE](LICENSE) 파일에 있다.
