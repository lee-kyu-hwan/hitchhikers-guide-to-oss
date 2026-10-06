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
| `tests/` | 저장소 시험(`test_oss_research_*.py`)과 fixture |
| `docs/` | 설계 문서(`oss-research-worker.md`, `oss-research-relay.md`, `oss-research-targets.md`), [CI](docs/ci.md), [경로 대응표](docs/path-map.md) |

스킬 이름은 dotfiles 때와 같다(revision 결속과 모델의 스킬 선택 단서를 지키기 위해).

## 설치

dotfiles가 chezmoi external로 이 저장소의 특정 tag를 받아 설치할 예정이다(dotfiles#290 3단계). 설치 위치는 dotfiles 때와 같다.

| 이 저장소 | 설치 위치 |
|---|---|
| `skills/researching-open-source-contributions/` (`analysis/` 제외) | `~/.agents/skills/researching-open-source-contributions/` |
| `skills/<그 밖의 스킬>/` | `~/.codex/skills/<스킬>/` |
| `bin/oss-research-*` | `~/.local/bin/oss-research-*` |
| `share/`, `tests/`, `docs/`, `analysis/` | 설치하지 않음(저장소 체크아웃과 게스트 배포 트리에서만 쓴다) |

격리 실행(VM, 수집, 분석 호출)은 저장소 체크아웃에서 쓴다. 실행기는 「`<root>/bin/<이름>`이고 `bin/` 옆에 `skills/`가 있는 트리」를 소스 체크아웃으로 보고, 그 밖(설치본)에서는 `~/.codex/skills`를 읽는다.

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

아직 정하지 않았다(공개 전에 정한다).
