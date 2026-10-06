# 격리 조사 스택의 실행 파일 (oss-research)

격리 조사(#78·#189)의 실제 실행 경로가 쓰는 파일이다. 호스트 실행기(`bin/oss-research-*`)와 게스트 unit wrapper(`oss-research-linux-run`) 옆에서, 호스트 회수·게스트 모델 1회·판정·실제 수집 운영을 맡는다. #274에서 `tests/fixtures/oss_research_worker/linux/`에서 옮겼다(`git mv`, 이력은 이어진다). #290에서 dotfiles의 `dot_local/share/oss-research/`가 이 저장소의 `share/`가 됐다(아래 「저장소 분리 뒤(#290)」). 이 문서의 이슈 번호는 dotfiles 이슈다.

## 무엇이 어디에 있나 (구분표, 2026-10-06)

참조 수는 `git grep -l`로 센 실사용 파일 수다(날짜로 시작하는 조사 기록, 분석 번들의 실행 기록 `docs/research/oss-e2e-2026-09-28/`, 자기 자신 제외). 줄임: `t:` = `tests/test_oss_research_*`, `fx:` = `tests/fixtures/oss_research_worker/linux/`, `bin:` = `bin/oss-research-*`, `analysis:` = 분석 번들.

| 파일 | 분류 | 어디서 도나 | 실사용 참조 | 위치 |
|---|---|---|---|---|
| `verify_pickup.py` | 실행: 결과 tar strict 회수(기본·auto·collect·issues·candidate-reads 계약) | 호스트 | 15: 수집·이슈 수집·후보 읽기 실행기, 분석 하네스, 운영 스크립트 2, `fx:collect_docs_cases.sh`, 시험 4, 문서 2 | **여기** |
| `model_once.sh` | 실행: 게스트 모델 1회(분석 호출의 token·sentinel) | 게스트(vmadmin, 안에서 sudo) | 4: 분석 하네스, `model_once_verdict.py`, `t:worker_linux` | **여기** |
| `model_once_verdict.py` | 실행: 모델 1회 판정(scan·judge·judge-worker) | 게스트(`model_once.sh` 안) + 호스트(분석 하네스) | 5: 분석 하네스, `model_once.sh`, `fx:unit_inner.py`, `t:worker_linux` | **여기** |
| `collect_live.sh` | 실행: 실제 수집 1회 운영(`linux-run --entry collect --egress pin`) | 게스트 | 0(운영자가 직접 부른다) | **여기** |
| `candidate_reads_live.sh` | 실행: 실제 후보 읽기 1단계 운영 | 게스트 | 0(운영자가 직접 부른다) | **여기** |
| `unit_inner.py` | 게스트 수락 시험: linux-run의 「시험용 비AI verify entry」 | 게스트 unit | 8 (linux-run의 신뢰 entry 목록에 이 경로가 고정) | `tests/fixtures/…/linux/` 그대로 |
| `*_probe.py`(candidate_write·clock_guard·collect·github_token), `probe.py` | 게스트 수락 시험의 탐침 | 게스트(srt·unit 안) | 1~2 | 그대로 |
| `*_cases.sh`(candidate_reads·clock_guard·collect·collect_docs·collect_issues·github_token·gate), `egress_integration.sh`, `cgroup_escape.py` | 게스트 수락 사례(mock, egress none 또는 게스트 안 로컬 fixture) | 게스트 | 0~2 | 그대로 |
| `egress_verdict.py` | egress 통합 사례 기록 판정 | 호스트 | 2 | 그대로 |
| `supervised_run.py` | 감독 실행 시험(`tests` 패키지를 import) | 게스트 | 2 | 그대로 |
| `evidence/`, `pickup_partial/`, `pickup_zero/`, `candidate_reads_live/SYNTHETIC-…`, `runtime/package*.json` | 시험 데이터 | — | 시험 | 그대로 |

## 위치 결정

- **실행 경로에 쓰는 다섯 파일만 옮겼다.** 게스트 수락 시험(`unit_inner.py`·탐침·사례)은 이름 그대로 시험이다: `linux-run`은 `unit_inner.py`를 「the non-AI verification entry of the tests」로 신뢰 목록에 고정해 두었고, 사례 스크립트는 격리 성질을 확인하는 시험이다. 이들을 옮기면 보안 경계(신뢰 entry 목록)를 바꾸게 되므로 이번에는 하지 않았다.
- **`share/linux/`**(dotfiles 당시 `dot_local/share/oss-research/linux/`): 이름을 바꾸지 않고(`.py`·`.sh`) 디렉터리만 바꿨다. #274 당시에는 저장소 루트까지 깊이가 옛 위치와 같아(`Path(__file__).parents[4]`) 형제 경로 계산이 그대로였다. #290 분리 뒤에는 `parents[2]`다(`verify_pickup.HOST_SKILLS`, `model_once_verdict`의 worker 경로). `bin/oss-research-*` 실행기로 만드는 안은 이름·import 방식(확장자 없는 파일)과 배포 방식이 바뀌어 동작 변경이 되므로 고르지 않았다.
- **배포(chezmoi, dotfiles 당시)**: 이 디렉터리의 배포 대상은 `~/.local/share/oss-research/…`였다(`dot_` → `.`). 그러나 여기 파일은 모두 저장소 체크아웃(호스트: 형제 스킬·`$REPO` 경로) 또는 게스트의 배포 커밋 트리(`/opt/oss-research/src-<커밋>/share/linux/…`)에서만 쓴다. 옮기기 전(`tests/**`)에도 $HOME에 배포되지 않았다. 그래서 `.chezmoiignore`의 `.local/share/oss-research` 두 줄로 배포에서 뺀다(배포 결과는 이 PR 전과 같다). 게스트는 chezmoi가 아니라 `git archive <커밋>`으로 트리를 받으므로 영향이 없다.
- 게스트 경로: 분석 하네스는 `$S/share/linux/model_once.sh`, `model_once.sh`는 `$SRC/share/linux/model_once_verdict.py`를 부른다(`S`·`SRC` = `/opt/oss-research/src-<커밋>`).

## 동작 불변 확인 (#274, 2026-10-06, 모델·토큰·GitHub 0)

- **S1/S2 호스트**: 옮기기 전(main `67fa3b4`)과 뒤에 같은 입력으로 `verify_pickup.py`·`model_once_verdict.py judge-worker`를 돌린 20항목(분석 호출 5건의 회수·판정, 기간별 수집 3건 `--collect`, 후보 읽기 2건 `--candidate-reads`, 시험 fixture pickup 4건; 수집기 sha는 현재 트리 값, 토큰 자리에는 가짜 sentinel 파일, 증거는 읽기만)의 rc·stdout·stderr sha가 모두 같다. `tests/test_oss_research_*.py`와 하네스 시험(`test_assess_ledger`·`test_continue_plan`) 670개의 시험 id·skip 집합이 같고 실패는 앞뒤 모두 0(648 OK·skip 39, 6, 16).
- **S3 게스트**(VM `oss-research-debian`, 두 커밋을 `git archive`로 배포, 모두 `--egress none`): 분석 하네스 sentinel dry 1회(claude 없음)가 옮기기 전·뒤 모두 harness 0·judge 0·`SUMMARY fails=0`·결과 tar 없음이고 `judge.txt`가 같다(2026-10-04 기록 dry-1과도 같다). `collect_docs_cases.sh`(loopback mock 8건 + 인자 거부 4건)의 wrapper rc와 요청 수(33·33·40·33·3·33·11·11)가 두 커밋과 2026-10-03 기록(`a192-collect-docs-20261003T104109Z`)에서 같고, 그 산출물 8건의 호스트 strict 회수가 모두 rc 0·problems 0이며 `pickup.json`이 바이트 단위로 같다(tar는 실행 시각이 달라 sha가 다르다). 옮긴 트리의 게스트 사본에는 옛 경로가 없다.
- **S4**: 다섯 파일의 옛 경로는 날짜로 시작하는 조사 기록 8개 파일과 대응표(이 README, 분석 번들 README)에만 남는다.

## 저장소 분리 뒤(#290)

- 이 디렉터리는 저장소 체크아웃(호스트)과 게스트의 배포 커밋 트리(`/opt/oss-research/src-<커밋>/share/linux/…`)에서만 쓴다. $HOME에 설치하지 않는다(dotfiles 당시 `.chezmoiignore`로 뺀 것과 같다).
- `staged/`: worker의 `deep-research` staged profile이 읽는 고정 원본 사본(dotfiles `dot_agents/skills/deep-research/SKILL.md` sha256 `a330b11e…`, `dot_claude/workflows/deep-research.js` sha256 `4d7392eb…`, dotfiles 88f17d0의 바이트 그대로). dotfiles 쪽이 바뀌어도 이 사본은 바뀌지 않는다(닫힌 게이트 뒤 profile 전용).

## 옛 경로 대응(#274)

| 옛 경로 | 새 경로 |
|---|---|
| `tests/fixtures/oss_research_worker/linux/verify_pickup.py` | `share/linux/verify_pickup.py` |
| `tests/fixtures/oss_research_worker/linux/model_once.sh` | `share/linux/model_once.sh` |
| `tests/fixtures/oss_research_worker/linux/model_once_verdict.py` | `share/linux/model_once_verdict.py` |
| `tests/fixtures/oss_research_worker/linux/collect_live.sh` | `share/linux/collect_live.sh` |
| `tests/fixtures/oss_research_worker/linux/candidate_reads_live.sh` | `share/linux/candidate_reads_live.sh` |
