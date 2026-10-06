# OSS 조사 대상 저장소 등록부

> **이 표는 허용 목록이 아니다. run의 범위는 요청 파일과 entry 인자가 정한다.** 등록부에 있다고 조사해도 된다는 뜻이 아니고, 없다고 못 하는 것도 아니다. 코드는 이 파일을 읽지 않는다. 사람과 다음 세션이 "어느 저장소를 언제 왜 어떻게 봤는지"를 다시 짜 맞추려고 남기는 기록이다.

## 읽는 법

- **범위가 정해지는 곳은 따로 있다.** run 하나의 범위는 요청 파일 `request.json`(`source.repositories`, `target.repositories`, `reference_repositories`)이 정하고, 격리 entry는 `--repo` 인자로 받은 저장소로만 relay를 띄운다(`oss-research-collect`, `-collect-issues`, `-candidate-reads`). 요청 파일을 쓸 때 저장소는 이 표에서 가져오고, 새 저장소를 넣으면 이 표에도 행을 더한다.
- **조회 시점의 기록이다.** 수치는 적힌 날짜의 값이다. 다시 조사하기 전에 `oss-research-scope`로 새로 센다.
- 행을 더하거나 고칠 때는 날짜와 근거(어느 조회·문서)를 같이 적는다. 증거 문서 경로는 저장소 루트 기준이다.
- 이 표를 만든 선별 조회 `bin/oss-research-scope`는 격리 entry가 아니다. 호스트의 `gh` 로그인으로 `gh api --method GET`만 하는 읽기 전용 조회이고 범위를 정하는 데만 쓴다. 격리 경계(#189)와 섞어 읽지 않는다.

```bash
oss-research-scope --org eslint --since 2026-07-05 --out-dir <새 디렉터리>
```

`<디렉터리>/summary.md`의 표가 아래 열 형식이라 행을 그대로 붙일 수 있다(역할·마지막 조사·증거 문서는 사람이 채운다). 요청은 20 이하이고 원 응답과 `scope-manifest.json`이 같이 남는다.

## 열

| 열 | 내용 |
|---|---|
| 저장소 | `owner/name` |
| 조직 | GitHub 조직(또는 소유자) |
| 역할 | 원천(패턴) / 대상(후보) / 참조 / 예비 / 제외 |
| 선별 근거 | 어느 조회에서 어떤 수치로 골랐나 |
| 마지막 조사 | 날짜, run 종류(PR 수집·이슈 수집·후보 읽기·모델 분석), 결과어 |
| 증거 문서 | `docs/research/…` 상대 경로 |
| 제외·보류 사유 | 보관 저장소, 구간 안 이슈 0건 등 |
| 메모 | 그 저장소에서 걸린 수집 한계 등 |

## 등록부

사용자가 2026-10-03에 조사 대상 8개를 확정했다(모두 역할 `원천+대상`, 조사 방식 `문서 위주`). 선별 근거는 사용자가 지정한 트래커 이슈(`eslint-ossca/eslint-ossca-2026` #22)의 댓글 21개에서 멘티 PR·이슈 URL을 집계한 값(병합 PR 수)과 저장소 활성도다. 선별 조회 수치(`열린 documentation 이슈`, `열린 제목 docs 이슈`, `2026-07-05 이후 병합 docs PR`)는 2026-10-03에 `oss-research-scope --org eslint --since 2026-07-05`로 센 값이다.

| 저장소 | 조직 | 역할 | 선별 근거 | 마지막 조사 | 증거 문서 | 제외·보류 사유 | 메모 |
|---|---|---|---|---|---|---|---|
| `eslint/eslint` | eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 39. 열린 `documentation` 이슈 10, 열린 제목 `docs` 이슈 6, 병합 `docs` PR 26 | 2026-10-03: PR 수집 1(3건), 이슈 수집 1(partial, 15건), 열린 이슈 44건 전부 읽음. 조건부 문서 후보 3(#21242·#16474·#20892, 확정 0) | `docs/research/2026-10-03-eslint-docs-research/README.md` (**미병합 브랜치** `lee-kyu-hwan/192-github-read-token`에만 있다) | - | 조사 방식: 문서 위주. PR collector 구간 상한 31일·cap 3, 이슈 collector 라벨 필터 없음·cap 15에 예산 60 부족, `closed-unmerged`가 relay 검색 문법에 막힘(2026-10-03 발견, 별도 브랜치에서 수정 중) |
| `eslint/markdown` | eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 9. 병합 `docs` PR 7(열린 `documentation`·제목 `docs` 이슈 0) | 2026-10-03: PR 수집 1(3건). 2026-09-28: PR 수집(두 저장소 합 6건, 저장소당 최신 3건)·격리 모델 분석 11회 → `insufficient` | `docs/research/2026-09-28-189-linux-model-evidence.md` 13·15절 | - | 조사 방식: 문서 위주. PR collector 구간 상한 31일·cap 3 |
| `eslint/css` | eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 9. 병합 `docs` PR 3 | 2026-10-06: 후보 읽기 1(discover, REST 정적 대조 규칙 문서 15 + README, hit 0, 요청 60/60에서 검색 예산 소진 partial, 레코드 0) → workflow `insufficient`. `docs/research/2026-10-06-split-gate-run/README.md` 「이어받기 실행 결과」 | - | - | 조사 방식: 문서 위주 |
| `eslint-markdown/eslint-markdown` | eslint-markdown | 원천+대상 | 트래커 댓글 집계 병합 PR 11 | 2026-10-02: 후보 읽기 1(synthetic 입력, discover partial·locus-search complete). 2026-09-28: 열린 이슈 수집 2회(조건부 후보 4, 확정 0), PR 수집·격리 모델 분석 11회 → `insufficient` | `docs/research/2026-10-02-candidate-reads-live.md`, `docs/research/2026-09-28-issue-live-round2/README.md`, `docs/research/2026-09-28-live-issue-research.md`, `docs/research/2026-09-28-189-linux-model-evidence.md` 13·15절 | - | 이전 이름 `lumirlumir/npm-eslint-markdown`(이름 이전 확인됨, 옛 이름의 과거 문서가 남아 있다). 조사 방식: 문서 위주. 익명 코드 검색은 401이라 후보 읽기가 partial |
| `typescript-eslint/typescript-eslint` | typescript-eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 7 | 2026-10-04: PR 수집 6(21건). 2026-10-06: 격리 모델 분석(분리 판정 실행·재실행·새 실행·이어받기, 원장 통산 26회) → 패턴 2(PAT-001 medium, PAT-002 low). `docs/research/2026-10-06-split-gate-run/README.md` | - | - | 조사 방식: 문서 위주 |
| `eslint/json` | eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 5. 병합 `docs` PR 3 | 없음 | - | - | 조사 방식: 문서 위주 |
| `eslint/code-explorer` | eslint | 원천+대상 | 트래커 댓글 집계 병합 PR 2 | 없음 | - | - | 조사 방식: 문서 위주 |
| `eslint-community/eslint-plugin-eslint-plugin` | eslint-community | 원천+대상 | 트래커 댓글 집계 병합 PR 2 | 없음 | - | - | 조사 방식: 문서 위주 |
| `eslint/eslint.org` | eslint | 제외(보류) | 병합 `docs` PR 11, 열린 `documentation` 이슈 0. 2026-10-03 1단계 선별에서 원천 후보였다 | 2026-10-03: PR 수집 1(3건, docs 포함은 블로그 글 1건) | `docs/research/2026-10-03-eslint-docs-research/README.md` (**미병합 브랜치** `lee-kyu-hwan/192-github-read-token`에만 있다) | 문서 사이트지만 이슈에 문서 라벨이 없고 PR이 블로그 글·의존성 갱신 위주다 | PR collector 31일 상한·cap 3 |
| `eslint-community/eslint-doc-generator` | eslint-community | 제외(보류) | 2026-10-03 1단계 선별에서 예비 확장 후보였다 | 2026-10-03: 이슈 수집 1(구간 90일, 0건, `no-results`) | `docs/research/2026-10-03-eslint-docs-research/README.md` (**미병합 브랜치** `lee-kyu-hwan/192-github-read-token`에만 있다) | 90일 구간에 갱신된 열린 이슈 0 | - |
| `eslint/archive-website` | eslint | 제외 | - | 없음 | - | 보관(archived) 저장소 | 선별 조회는 보관·fork 저장소를 표에서 뺀다 |
| `eslint/js` | eslint | 제외 | 병합 `docs` PR 3 | 없음 | - | 멘티 PR 소량 | - |
| `eslint/csstree` | eslint | 제외 | - | 없음 | - | 멘티 PR 소량 | - |
| `eslint/rewrite` | eslint | 제외 | 병합 `docs` PR 2 | 없음 | - | 멘티 PR 소량 | - |
| `TanStack/query` | TanStack | 제외 | - | 없음 | - | ESLint 생태계 밖 | - |
| `lycheeverse/lychee` | lycheeverse | 제외 | - | 없음 | - | ESLint 생태계 밖 | - |
| `bruits/satteri` | bruits | 제외 | - | 없음 | - | ESLint 생태계 밖 | - |
| `microsoft/vscode-eslint` | microsoft | 제외 | - | 없음 | - | 제외(2026-10-03 확정). 사유는 기록되지 않았다 | - |
| `yeonjuan/html-eslint` | yeonjuan | 제외 | - | 없음 | - | 제외(2026-10-03 확정). 사유는 기록되지 않았다 | - |

## 수집 한계 (2026-10-03 발견)

위 표의 메모에 저장소별로 적은 것을 한곳에 모은다. 모두 별도 브랜치에서 수정 중이므로 고쳐지면 이 절과 해당 행의 메모를 지운다.

- PR collector: 구간 상한 31일, 저장소당 cap 3. 90일 구간은 시작 전에 거부된다.
- 이슈 collector: 라벨 필터 옵션이 없다(`documentation`으로 좁힐 수 없다). cap 15에 요청 예산 60이 모자라 partial이 된다.
- `closed-unmerged` 조회가 relay의 검색 문법에 막힌다.

## 갱신 절차

1. 새 조직이나 저장소를 보려면 `oss-research-scope`로 조회하고, 새 디렉터리의 `summary.md` 표에서 행을 가져온다.
2. 역할·선별 근거(어느 조회에서 어떤 수치)를 적고 추가한다. 조회 날짜와 `scope-manifest.json`의 요청 수를 근거에 남긴다.
3. 조사를 한 뒤에는 마지막 조사(날짜·run 종류·결과어)와 증거 문서를 고친다.
4. 제외하는 저장소도 지우지 말고 사유와 함께 남긴다.
