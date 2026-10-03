# OSS 조사 읽기 전용 relay

`oss-research-relay`는 #192의 부모 소유 relay다. 부모가 실행하고, 샌드박스
안의 조사 워커(child)는 이 relay의 `127.0.0.1` 포트만 목적지로 허용받는다.
child는 GitHub에 직접 연결하지 않는다. relay는 범용 proxy가 아니다. 정해진
저장소 읽기 endpoint와 고정 GraphQL operation 하나만 받는다. 받은 요청은
기존 #192 클라이언트(`GhApiClient`, `GraphqlQueryClient`)로 인증 없이 보낸다.

srt는 목적지 host:port만 제한한다. 허용된 서버가 GitHub 자체라면 REST
POST, GraphQL mutation, git push 탐색 요청도 그 서버에 도달한다. 그래서
허용 목적지를 이 relay로 바꾸고, 쓰기 요청은 relay가 upstream 호출 전에
거부한다.

이 문서와 테스트는 로컬 fixture만 사용한다. 실제 GitHub 호출 성공, 익명
GraphQL 성공, 모델 인증의 증거가 아니다. GitHub GraphQL은 인증이 필요하므로
익명 relay에서는 실패할 수 있다. 실제 제품 사용 전 launcher gate는 닫힌
상태로 둔다. socket 서비스의 runtime 보호는 #189가 통합하여 검증한다.

## 실행

```bash
oss-research-relay --repo OWNER/NAME [--repo OWNER/.github ...] \
  --request-budget N --max-response-bytes N \
  --response-byte-budget N --timeout-seconds S \
  [--skills-dir /absolute/path/to/skills]
```

`--skills-dir`를 뺀 인자는 모두 필수이고 기본값이 없다. 부모가 경계를
명시해야 한다.

| 인자 | 의미 | 범위 |
| --- | --- | --- |
| `--repo` | 허용 저장소. 반복 가능. 대소문자 무시, 중복 거부 | `OWNER/NAME` |
| `--request-budget` | relay 수명 전체의 upstream 시도 수. 재시도 포함 | 1–10000 |
| `--max-response-bytes` | 응답 본문 하나의 최대 바이트 | 1–64MiB |
| `--response-byte-budget` | relay 수명 전체의 응답 본문 바이트 합계 | 1–1GiB |
| `--timeout-seconds` | relay 요청 하나의 부분 기한. 범위는 아래 "기한" 참고 | 0초 초과 300초 이하 |
| `--skills-dir` | 클라이언트 source를 읽을 skills 디렉터리. 절대 경로만 | 기본 `$HOME/.codex/skills` |

upstream URL과 opener를 바꾸는 인자는 없다. 클라이언트 source는 신뢰된
부모가 `--skills-dir`로 고른 디렉터리에서만 읽는다. 생략하면
`$HOME/.codex/skills`를 쓴다. 그 디렉터리의
`collecting-open-source-issues/scripts/collect_open_source_issues.py`를
불러오고, 그 파일이 같은 디렉터리의
`collecting-recent-closed-prs/scripts/collect_recent_closed_prs.py`를
불러온다.

설치본(`~/.codex/skills`)은 새 #192 전송 코드보다 오래된 `gh` 기반 구현일 수
있다. 그러면 relay 동작이 이 문서와 달라진다. #189 통합에서는 검토한
worktree의 `dot_codex/skills` 절대 경로를 `--skills-dir`로 넘겨야 한다. relay는
설치본을 편집하거나 배포하지 않는다.

HTTP 요청, upstream 결과, fixture, 환경 변수로는 source 경로를 고를 수
없다. 두 파일은 로드 전후에 SHA-256을 계산한다. 값이 바뀌었거나 sibling이
다른 디렉터리에서 로드되면 실패한다. source가 없거나 로드에 실패하면 stdout
없이 종료 코드 2로 끝난다.

준비되면 stdout에 JSON 한 줄을 쓴다. `sources`는 실제로 로드한 두 파일의
resolve된 경로와 SHA-256이다. 부모는 이 값을 provenance로 기록하고, 검토한
커밋의 파일 hash와 대조한다. 이후 SIGTERM이나 SIGINT를 받을 때까지
서비스하고 종료 코드 0으로 끝난다.

```json
{"event":"ready","host":"127.0.0.1","port":54321,"base_url":"http://127.0.0.1:54321",
 "sources":[{"role":"issues","path":"/…/collect_open_source_issues.py","sha256":"…"},
            {"role":"sibling","path":"/…/collect_recent_closed_prs.py","sha256":"…"}]}
```

bind 주소는 `127.0.0.1:0`으로 고정이다. 포트는 커널이 고른다.

## HTTP 계약

모든 요청의 `Host` 헤더는 정확히 `127.0.0.1:<port>`여야 한다. 응답은 항상
`application/json`, `Connection: close`, `Cache-Control: no-store`다.

### `GET /v1/health`

upstream을 호출하지 않는다. `{"ok":true,"budget":{...}}`를 돌려준다.

### `GET /v1/rest/<GitHub path>[?query]`

허용 endpoint는 다음과 같다. `O/N`은 `--repo` 허용목록에 있어야 한다.
`owner/.github`도 목록에 명시해야 읽을 수 있다.

| endpoint | query | 응답 범위 검사 |
| --- | --- | --- |
| `/repos/O/N` | 없음 | `full_name`이 요청 저장소 |
| `/repos/O/N/license` | 없음 | `url`이 요청 저장소 아래 |
| `/repos/O/N/issues/K`, `/repos/O/N/pulls/K` | 없음 | `url`이 요청 저장소 아래 |
| `/repos/O/N/issues/K/{comments,timeline}` | `page` | 배열 |
| `/repos/O/N/pulls/K/{files,commits,reviews,comments}` | `page` | 배열 |
| `/repos/O/N/commits/<SHA>` | 없음 | `sha` 일치, `url`이 요청 저장소 아래 |
| `/repos/O/N/contents/<path>` | `ref=<SHA>` 또는 이 relay가 관측한 default branch(아래 「후보 verifier 전용 읽기」) 필수 | 각 항목 `url`이 요청 저장소 아래, 파일이면 `path` 일치 |
| `/repos/O/N/commits/<branch>` | 없음 | `sha`가 40·64자리 hex, `url`이 요청 저장소 아래. branch는 관측한 default branch와 정확히 같을 때만 |
| `/repos/O/N/community/profile` | 없음 | `files` 객체 |
| `/repos/O/N/private-vulnerability-reporting` | 없음 | bool `enabled` |
| `/search/issues` | `q`, `page` | 모든 `items[].repository_url`이 요청 저장소 |
| `/search/code` | `q` | 모든 `items[].repository.full_name`이 요청 저장소. 질의는 아래 verifier 형태만 |
| `/versions` | 없음 | 비어 있지 않은 문자열 배열 |

- `K`는 1–10자리 양의 정수다. `SHA`는 소문자 40자리 또는 64자리 hex다.
  branch 이름은 이 relay가 같은 저장소의 `/repos/O/N` 응답에서 직접 본
  `default_branch`와 정확히 같을 때만 받는다(`commits/<branch>`, `contents` `ref`).
  관측 전이거나 다른 branch면 `403 ref_not_observed`(commits) 또는 `400 bad_query`(contents)다.
- contents 경로는 `/`로 나뉜 1–20개 세그먼트다. 세그먼트는
  `[A-Za-z0-9_.-]`이고 `.`, `..`는 안 된다. `.github` 같은 점 시작 이름은
  허용한다.
- `page`는 1–1000 정수다. `per_page`는 기존 클라이언트가 100으로 고정하므로
  받지 않는다. 같은 query 이름을 반복하면 거부한다.
- `search/issues`의 `q`는 256자 이하 출력 가능한 문자다. `repo:O/N` 토큰이
  정확히 하나 있어야 한다. `repo|org|user|owner` 뒤에 `:`가 붙은 형태가 그
  하나 말고는 없어야 한다. `OR`/`AND`/`NOT`, 괄호, 역슬래시, `-`·`~`·`!` 시작
  토큰은 범위를 넓힐 수 있으므로 거부한다. 큰따옴표와 작은따옴표는 모두
  거부한다. 인용 phrase 안의 `repo:`는 GitHub에서 범위가 아니라 텍스트이기
  때문이다(예: `"foo repo:a/b bar"`). 그래서 공백이 든 label 같은 인용 검색은
  지원하지 않는다. 예외는 아래 후보 verifier의 정확한 형태 하나뿐이다.
- `/versions`는 저장소가 아닌 유일한 읽기로, collector의 API 버전 사전 확인용이다.
  정확한 경로의 GET만 받는다. query가 있으면 400 `query_not_allowed`다(끝의
  빈 `?`는 빈 query로 본다). 요청 예산·응답 크기·timeout은 다른 endpoint와 같다.
- path에 `%`, `\`, `;`, `#`, 제어문자, `//`, 빈 세그먼트, dot 세그먼트가 있으면
  거부한다. 퍼센트 인코딩된 path는 디코딩하지 않고 그대로 거부한다.

### 후보 verifier 전용 읽기 (`oss-research-relay-verify`용)

`oss-research-relay-verify`는 `verifying-open-source-contribution-candidates`의
`verify_candidates.main`에 이 relay를 transport로 주입한다(discover·locus-search만).
그 verifier가 보내는 GET만 relay가 받도록 위 표의 `commits/<branch>`,
`community/profile`, `private-vulnerability-reporting`, `/search/code`와 branch ref를 더했다.
쓰기·POST·redirect·예산·크기·timeout·allowlist 규칙은 그대로다.

- 검색 질의는 verifier의 정확한 형태만 받는다. 인용된 clue 하나(`[^"\]` 1–200자)와 인용 밖
  `repo:O/N` 하나다. 이슈 검색은 `repo:O/N "clue" is:issue|pr is:open|closed`, 코드 검색은
  `repo:O/N "clue"`다. 이 형태가 아닌 코드 검색은 `403 search_grammar_not_allowed`다.
- 응답 범위 검사: `commits/<branch>`는 `sha`와 `url`, 코드 검색은 모든 항목의 저장소, community는
  `files`, 취약점 보고는 `enabled`를 본다. 어긋나면 `502 upstream_out_of_scope`다.
- 익명 코드 검색은 GitHub가 401로 답하는 것으로 가정하고 그 응답을 그대로 전달한다. 실제 익명 응답은
  이 문서의 어떤 실행으로도 측정하지 않았다(fixture mock만).
- 이 경로는 mock으로만 검증했다. 오프라인 시험은 `tests/test_oss_research_relay_verify.py`, 격리 안(srt·unit) 실행은 비AI entry `executable_oss-research-candidate-reads`(`linux-run --entry candidate-reads`)가 한다. entry는 relay를 `--repo <대상>`과 `--repo <소유자>/.github` 두 저장소로만 띄우고 요청 예산을 60 이하로 고정하며, 호스트 회수는 `verify_pickup.py --candidate-reads`가 한다(`oss-research-worker.md` 「후보 verifier 읽기 entry」). relay의 허용 route·메서드·예산 규칙은 entry를 만들면서 넓히지 않았다. 실제 GitHub 조회와 익명 코드 검색 응답은 측정하지 않았다.

### `POST /v1/graphql`

`Content-Type: application/json`이어야 하고, 본문은 8KiB 이하여야 한다.
query string은 붙일 수 없다.

```json
{"operation":"IssueEvidence","variables":{"owner":"O","name":"N","number":1}}
```

`operation`은 `IssueEvidence` 하나만 받는다. relay는
`collect_open_source_issues.ISSUE_EVIDENCE_QUERY` 문서를 그대로
`GraphqlQueryClient.query`에 넘긴다. 변수는 정확히 `owner`, `name`,
`number`(bool이 아닌 1–2^31-1 정수)여야 한다. `operation`, `owner`, `name`은
문자열이어야 한다. 배열·객체·null·bool이면 upstream 호출 없이 400이다.
저장소는 허용목록에 있어야 한다. JSON 어느 깊이에서든 같은 키가 두 번
나오면 해석이 갈릴 수 있으므로 400 `bad_json`으로 거부한다. `NaN`,
`Infinity` 같은 비표준 상수도 같다. `query` 키나 다른 키가 있으면 거부하므로 child가 임의의 GraphQL
문서를 보낼 수 없다. relay에 새 GraphQL parser는 없다. 응답은 `data` 또는
`errors`를 가진 객체여야 한다.

### 응답

성공하면 HTTP 200이다.

```json
{"ok":true,"status":200,"headers":{"etag":"...","link":"..."},"payload":<JSON>,
 "budget":{"limit":N,"consumed":n,"response_bytes_limit":B,"response_bytes_consumed":b}}
```

전달하는 응답 헤더는 `etag`, `link`, `x-ratelimit-limit`,
`x-ratelimit-remaining`, `x-ratelimit-reset`뿐이다.

실패 응답은 `{"ok":false,"error":<code>,"message":...,"budget":{...}}`다.
upstream 실패에는 `endpoint`와 `upstream_status`가 붙을 수 있다.

**재시도 대기가 기한을 넘는 수신 응답** (2026-09-28 추가)

직전 시도가 실제 4xx/5xx 응답을 받았고, 그 재시도 대기가 요청 기한을 넘을 수 있다. 예: 403 rate limit, 429 `Retry-After`. relay는 이 경우 기다리지도 다시 요청하지도 않는다. 대신 그 응답을 `502 upstream_failed`로 돌려준다.

- `upstream_status`: 실제로 받은 status
- `message`: upstream의 원래 `message`. 출력 가능 문자만, 500자 이하
- `upstream_rate_limit`: 받은 헤더 중 `retry-after`, `x-ratelimit-limit|remaining|reset|used`(1~12자리 정수)와 `x-ratelimit-resource`(`[a-z_]` 32자 이하)만. 그 밖은 버린다.
- `upstream_retry_wait_seconds`
- `relay_reason`: `"the retry wait passes the relay request deadline"`

stderr에는 JSON 한 줄(`upstream_retry_wait_passes_deadline`)을 남긴다.

adapter(`oss-research-relay-collect`와 이를 재사용하는 `oss-research-relay-issues`)는 이 헤더를 같은 규칙으로 검사하고, 어긋나면 `malformed_envelope`로 거부한다. 통과하면 그 응답의 헤더로 collector에 넘긴다.

collector(`collect_recent_closed_prs._retry_delay`)는 서버가 명시한 대기를 그대로 지킨다.

- 유한한 `Retry-After`, 또는 reset까지의 시간을 상한으로 줄이지 않는다.
- 헤더가 없을 때의 지수 fallback만 300초 상한이다.
- `inf`, `nan`, 음수, 형식 오류 값은 fallback으로 처리한다.
- 긴 대기는 supervisor의 기존 timeout이 끝낸다.

응답을 받지 못한 전송 실패, 본문 읽기 중 기한, 대기열 기한은 여전히 `504 upstream_timeout`·`relay_busy_timeout`이다.

**할당량이 전혀 없는 GraphQL 403** (2026-09-28 추가, 실측: 익명 GraphQL)

조건은 endpoint `/graphql`, HTTP 403, `x-ratelimit-resource: graphql`, `x-ratelimit-limit: 0`, `x-ratelimit-remaining: 0`이 모두 맞을 때다.

- 이때는 공유 client(`_no_graphql_quota`)가 이 실행에서 재시도하지 않고 원래 실패를 돌려준다. 손에 든 응답이 할당량 0을 말하기 때문이다(정책).
  - 관측: 2026-09-28 익명 GraphQL 호출 1회에서, reset까지 기다리는 동안 entry timeout에 걸렸다.
  - reset 이후 할당량이 생기는지는 측정하지 않았다. 모든 익명 호출에 영구히 해당한다는 뜻도 아니다.
- relay는 이 실패를 `502 upstream_failed` + `upstream_status 403` + 그 응답의 안전 `upstream_rate_limit`으로 돌려준다. 수신한 응답에서 온 모든 `upstream_failed`에 이 헤더를 같은 규칙으로 붙인다.
- 그러면 adapter 쪽 collector도 같은 판정을 한다. 이슈당 upstream 요청은 1회, 대기는 0이다. GraphQL 범주만 partial이 되고, REST 사실은 보존된다.
- 그 밖의 경우는 기존대로 기다린다: 양의 할당량이나 남은 할당량, 다른 resource, 헤더 누락·형식 오류, 429, REST endpoint.

| HTTP | code | 원인 | upstream 호출 |
| --- | --- | --- | --- |
| 400 | `bad_host`, `bad_target`, `header_not_allowed`, `bad_path`, `bad_query`, `query_not_allowed`, `bad_json`, `bad_graphql`, `bad_variables`, `unexpected_body` | 형식·헤더·경로·query 거부 | 없음 |
| 403 | `endpoint_not_allowed`, `repository_not_allowed`, `operation_not_allowed`, `search_scope_not_allowed`, `search_grammar_not_allowed` | 허용목록 밖 | 없음 |
| 404 | `not_found` | 알 수 없는 relay 경로 | 없음 |
| 405 | `method_not_allowed` | REST GET 외 method, CONNECT, HEAD, OPTIONS, TRACE | 없음 |
| 411/413/415 | `length_required`, `request_too_large`, `bad_content_type` | GraphQL 본문 형식 | 없음 |
| 429 | `budget_exhausted`, `byte_budget_exhausted` | 요청·바이트 예산 소진 | 소진 뒤 없음 |
| 502 | `upstream_failed` | 비 2xx(redirect 포함), 잘못된 JSON, 전송 실패 | 있음 |
| 502 | `upstream_too_large` | 응답 크기 상한 초과 | 있음 |
| 502 | `upstream_out_of_scope` | 성공 응답이 요청 범위 밖 | 있음 |
| 504 | `upstream_timeout`, `relay_busy_timeout` | 기한 초과(응답을 받지 못한 경우. 받은 응답의 긴 재시도 대기는 위 502) | 있을 수 있음 |

## 보안 제약

- 인증 없음: 두 클라이언트에 `token_provider`로 항상 `None`을 주입한다.
  `GH_TOKEN`, `GITHUB_TOKEN`, `gh auth token`을 읽지 않는다. child가 보낸
  `Authorization`, `Proxy-Authorization`, `Cookie`는 전달하지 않고 요청 자체를
  400으로 거부한다. `If-None-Match`, `If-Modified-Since`, `Expect`,
  `Transfer-Encoding`도 거부한다.
- 목적지 고정: upstream은 기존 클라이언트가 만드는
  `https://api.github.com`뿐이다. 기존 `_check_read_only_request`가 GET과
  `/graphql` query POST만 통과시킨다. 제품 opener는 `ProxyHandler({})`로
  `HTTPS_PROXY` 같은 환경 proxy를 무시하고 redirect를 따라가지 않는다.
  3xx는 `upstream_failed`다. absolute-form·authority-form 요청 대상, CONNECT,
  다른 `Host`는 거부한다.
- 쓰기 없음: REST는 GET만 받는다. GraphQL은 고정 query 하나만 받는다.
  clone, `git-upload-pack`, `git-receive-pack`, Git Data API, 모든 쓰기
  endpoint는 허용목록에 없다.
- 예산: 요청 예산은 재시도를 포함한 upstream 시도마다 차감되고 relay 수명
  동안 공유된다. 응답 바이트 예산도 모든 요청의 합계다. upstream 호출은 lock
  하나로 직렬화하므로 여러 연결이 동시에 와도 합계가 정확하다.
- 기한: `--timeout-seconds`는 relay 요청 하나의 총 wall-clock 상한이
  아니다. 기한은 child 요청의 헤더와 본문을 다 받고 검증을 통과한 뒤
  upstream 호출에 들어갈 때 시작한다. 이 기한이 실제로 보장하는 것은 다음
  세 가지다.
  - lock 대기: 기한 안에 lock을 못 잡으면 504 `relay_busy_timeout`이다.
  - 재시도 대기: 남은 시간을 넘기는 대기는 하지 않고 504를 낸다.
  - 응답 본문 읽기: 본문은 `read1`으로 socket read 하나씩 읽는다. 매번 남은
    시간으로 socket timeout을 다시 설정하므로 조금씩 흘려 보내는 본문도
    기한 근처에서 끊긴다.

  다음 구간에는 총 시간 상한이 없다.
  - child 요청 수신: 연결별 socket timeout 10초는 recv 한 번마다 적용된다.
    천천히 흘려 보내는 요청 헤더는 이보다 오래 스레드를 붙잡을 수 있다.
    동시 연결 8개 상한만 적용된다.
  - `urllib` open: DNS 조회에는 timeout이 없다. TCP 연결, TLS handshake, 응답
    헤더 수신은 socket 연산 하나마다 `min(30초, 남은 시간)`만 적용된다.
    헤더를 조금씩 흘리는 upstream은 기한을 넘길 수 있다.

  따라서 relay 프로세스 전체의 wall-clock timeout은 #189 부모의 필수
  계약이다. 부모는 조사 run의 총 기한이 지나면 relay 프로세스를 종료해야
  한다. child의 relay 요청에도 자체 timeout을 둬야 한다.
- 연결 수: 동시 연결 스레드는 최대 8개다. 초과 연결은 즉시 닫는다. 연결마다
  client socket timeout은 10초다. `127.0.0.1` 밖 client 주소는 받지 않는다.
- 결과 처리: relay는 upstream 본문을 JSON으로 파싱해 그대로 돌려줄 뿐이다.
  실행하거나 해석하지 않는다. child도 결과를 데이터로만 다뤄야 한다.

## 미지원

- 인증 토큰, 모델 auth broker, 사용자 credential 전달
- clone, `git-upload-pack`, archive(`tarball`/`zipball`)
- `/user`, `/versions/...` 같은 하위 경로, 조직·사용자 endpoint
- 관측한 default branch가 아닌 branch 이름 ref. 코드 검색도 verifier의 정확한 질의 형태만 받는다
- 조건부 요청(`If-None-Match`)과 캐시
- 목록 endpoint의 `state`, `sort`, `since` 같은 추가 query
- `IssueEvidence` 외 GraphQL operation
- upstream override. 테스트만 모듈 함수에 로컬 opener를 주입한다

## 테스트

```bash
python3 -m unittest tests.test_oss_research_relay
```

테스트는 `socket.connect`를 감싸 `127.0.0.1` 밖 연결을 모두 거부한다.
CLI subprocess에는 `tests/fixtures/oss_research_relay/sitecustomize.py`를
`PYTHONPATH`로 넣어 같은 보호를 건다. relay의 upstream은 테스트 전용
opener가 로컬 fixture HTTP 서버로 보낸다. 이렇게 child→relay와
relay→fixture 구간 모두 실제 HTTP로 검사한다. 부모 환경에
`GH_TOKEN`/`GITHUB_TOKEN`/`HTTPS_PROXY`를 넣은 상태에서 다음을 확인한다.

- 허용 읽기: REST 목록·단건·commit·contents·search와 GraphQL
  `IssueEvidence`가 익명 GET·고정 POST로만 나간다.
- 거부: 각 쓰기 method, 허용목록 밖 endpoint·저장소, 인코딩 path,
  traversal, SSRF 형태, CONNECT, Host 위조, credential 헤더, 추가
  query, 범위 이탈 search 문법, raw GraphQL·mutation, 잘못된 변수. 모두
  upstream 호출 0회다.
- upstream 실패: redirect 미추종, 크기 초과, 잘못된 JSON, 범위 밖 응답,
  요청 예산(재시도 포함), 바이트 예산, 느린 응답, 조금씩 흘리는 본문,
  대기열 기한, 연결 수 상한.
- search 인용 거부: `"foo repo:a/b bar"` 같은 인용 phrase 안의 scope를
  checker 단위와 HTTP 단위 모두에서 거부한다.
- GraphQL 타입: `operation`이 배열·객체·null·bool·숫자이거나 변수 타입이
  틀리면 400이다. 중복 키와 `NaN`/`Infinity`는 400 `bad_json`이다.
- CLI: 경계 인자 필수, 알 수 없는 인자 거부, 상대 경로이거나 source가 없는
  `--skills-dir` 거부, ready 줄, `sources` 경로·SHA-256 기록, 로드 중 hash
  변경 시 실패, HTTP query로 source 지정 불가, health, SIGTERM 종료,
  기본 스킬 경로가 없으면 종료 코드 2.
