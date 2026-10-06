"""What the strict check refuses in a model answer, kind by kind, and the words that kind's prompt says it with (#286).

The strict check (verify_e2e_result.py) refuses an answer for reasons its prompt must have told the model. Runs kept
stopping on reasons only the check knew: the lens id, the count limits (#246), the quote escapes (#252), the search
clues (#255), the assessment field types (#273), and in the 2026-10-06 split gate fresh run a list-only counterexample
(R5) and the verify answer size (R3). This module is the one table of those reasons. Each row names

  kinds     the strict answer kinds it holds for (a merge answer is checked as a synthesis answer)
  item      the strict verdict item that refuses (R3, R5, R6, Q1, A1, A2, A4)
  refusal   a pattern over the problem text strict writes (for Q1: the check_extraction failure reason)
  says      the words every prompt of those kinds holds for it, with {limit} filled from run_limits.json and the
            check's own constants (``values``): the one source the check, the prompt and this table read
  scoped    True when the row holds only where the stage writes an input scope (R5): the prompts PROMPT_SCOPED names

test_prompt_contract.py checks, without a model: every row's words are in every prompt it covers (S3: strict's
conditions are a subset of the prompt's rules); every problem strict writes for a mutated answer matches a row of its
kind, and every row is reached by some mutation (no refusal outside the table, no row strict does not enforce); an
answer written by the prompts' sentences at each limit passes strict (S4). The assess field types are
assess_contract's table (#273); the record gate's value rules (A3) stay with test_assess and test_assess_contract.
"""

from __future__ import annotations

from collections import namedtuple
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
LIMITS = json.loads((HERE / "run_limits.json").read_text(encoding="utf-8"))

Rule = namedtuple("Rule", "name kinds item refusal says scoped")

# The prompts each answer kind is asked with: name -> (strict kind, whether its stage writes an input scope).
PROMPTS = {
    "extract": ("extract", False),
    "synthesis": ("synthesis", True),            # synthesize-reduced.md: a group, or the one call of a reduced run
    "synthesis-digest": ("synthesis", False),    # synthesize.md: the digest-in-final run
    "merge": ("synthesis", True),                # synthesize-merge.md
    "verify": ("verify", True),                  # cited sections and a title list of the others
    "verify-digest": ("verify", False),          # the whole digest
    "final": ("final", True),                    # finalize-reduced.md
    "final-digest": ("final", False),            # finalize.md
    "assess": ("assess", True),
}

# The analyzer's quote rules ask for at most this many characters; the check allows MAX_QUOTE_LENGTH (300).
QUOTE_PROMPT_MAX = 200


def values(verify_e2e_result=None, validate_corpus=None) -> dict:
    """The numbers the rows' words name: run_limits.json, and the check's own constants when given."""
    found = {key: value for key, value in LIMITS.items() if isinstance(value, int) and not isinstance(value, bool)}
    found["quote_prompt_max"] = QUOTE_PROMPT_MAX
    if verify_e2e_result is not None:
        found["search_clue_max_chars"] = verify_e2e_result.SEARCH_CLUE_MAX_CHARS
    if validate_corpus is not None:
        found["min_quote_length"] = validate_corpus.MIN_QUOTE_LENGTH
    return found


QUOTE_KINDS = ("extract", "verify")
RULES = [
    # -- extract (verify_e2e_result.schema_problems) --
    Rule("extract keys", ("extract",), "R3", r"keys must be exactly lens, patterns, observations, rejected",
         ('"patterns": [', '"observations": [', '"rejected": [', "JSON 객체 하나만 출력한다"), False),
    Rule("extract size", ("extract",), "R3",
         r"the answer is \d+ bytes as a prompt holds it \(> extract_answer_max_bytes \d+\)",
         ("들여쓰기 1칸으로 다시 쓴 크기(UTF-8, 한글 한 글자는 3바이트)가 {extract_answer_max_bytes} 바이트 이하",
          "{extract_answer_target_bytes} 바이트 이하로 써라"), False),
    Rule("extract lens", ("extract",), "R3", r"lens .*", ('출력의 "lens" 값은 정확히 "',), False),
    Rule("extract counts", ("extract",), "R3", r"(patterns|observations|rejected) must be a list of at most \d+",
         ("patterns 는 최대 {max_patterns}개, observations 는 최대 {max_observations}개, rejected 는 최대 "
          "{max_rejected}개다", "넘으면 답 전체가 거부된다"), False),
    Rule("extract pattern keys", ("extract",), "R3", r"patterns\[\d+\] (is not an object|lacks [a-z_, ]+)",
         ('"title": ', '"description": ', '"kind": ', '"evidence": [', '"applicability": [', '"counterconditions": [',
          '"search_clues": [', '"expected_tests": [', '"maintainer_judgment_required": [', '"confidence": ',
          '"confidence_reason": '), False),
    Rule("extract evidence", ("extract",), "R3", r"patterns\[\d+\]\.evidence must be a non-empty list",
         ("모든 근거(evidence)에는 인용을 붙인다", "서로 다른 PR 2개 이상"), False),
    Rule("extract evidence item", ("extract",), "R3",
         r"patterns\[\d+\]\.evidence\[\d+\] needs pr, quote, claim \(strings\) and role in \['counterexample', 'supports'\]",
         ('{"pr": "<키>", "role": "supports | counterexample", "quote": "<원문 인용>", "claim": ',), False),
    Rule("extract observation", ("extract",), "R3", r"observations\[\d+\] needs pr, note, quote \(strings\)",
         ('{"pr": "<키>", "note": ', '"quote": "<원문 인용>"}'), False),
    Rule("extract rejected", ("extract",), "R3", r"rejected\[\d+\] needs idea, reason \(strings\)",
         ('{"idea": ', '"reason": '), False),
    # -- quotes of extract and verify answers (check_extraction through Q1) --
    Rule("quote key", QUOTE_KINDS, "Q1", r"unknown-key",
         ("가 PR 의 유일한 식별자다", "인용은 그 PR 의 섹션 안에서만 가져온다"), False),
    Rule("quote length", QUOTE_KINDS, "Q1", r"empty-quote|length",
         ("인용(quote)은 다이제스트에서 글자 그대로 복사한 {min_quote_length}~{quote_prompt_max}자다",), False),
    Rule("quote verbatim", QUOTE_KINDS, "Q1", r"not-verbatim",
         ("번역·요약·수정하지 마라", "`...` 로 이어 붙이지 마라", "다이제스트가 붙인 것이라 인용이 될 수 없다"), False),
    # -- synthesis, group and merge (verify_e2e_result.synthesis_problems, R5) --
    Rule("synthesis keys", ("synthesis",), "R3", r"keys must be exactly candidates, dropped",
         ('"candidates": [', '"dropped": [', "JSON 객체 하나만 출력한다"), False),
    Rule("synthesis count", ("synthesis",), "R3",
         r"candidates must be a list of at most \d+ \(synthesis_max_candidates\)",
         ("후보는 **최대 {synthesis_max_candidates}개**", "넘으면 답 전체가 거부된다"), False),
    Rule("synthesis dropped count", ("synthesis",), "R3", r"dropped must be a list of at most \d+",
         ("`dropped` 는 최대 {max_rejected}개다",), False),
    Rule("synthesis candidate keys", ("synthesis",), "R3",
         r"candidates\[\d+\] needs id, statement, cited_prs \(and optional found_by\) only",
         ('{"id": "C1", "statement": ', '"cited_prs": [', '"found_by": "'), False),
    Rule("synthesis id", ("synthesis",), "R3", r"candidates\[\d+\]\.id must be a unique C1 … C99",
         ("id 는 `C1`, `C2` … 순서대로 쓴다",), False),
    Rule("synthesis candidate size", ("synthesis",), "R3",
         r"candidates\[\d+\] is \d+ bytes as a prompt holds it \(> candidate_max_bytes \d+\)",
         ("들여쓰기 1칸 JSON 으로 쓴 크기(UTF-8, 한글 한 글자는 3바이트)가 {candidate_max_bytes} 바이트 이하다",), False),
    Rule("synthesis statement", ("synthesis",), "R3",
         r"candidates\[\d+\]\.statement must be a nonempty string of at most \d+ characters",
         ("`statement` 는 비어 있지 않은 {candidate_statement_max_chars}자 이하의 문자열",), False),
    Rule("synthesis found_by", ("synthesis",), "R3", r"candidates\[\d+\]\.found_by must be a string",
         ("`found_by` 는 문자열이다",), False),
    Rule("synthesis cited", ("synthesis",), "R3",
         r"candidates\[\d+\]\.cited_prs must name two or more distinct corpus digest keys",
         ("서로 다른 PR 2개 이상", "`cited_prs` 는 다이제스트 키(아래 PR "), False),
    Rule("synthesis dropped item", ("synthesis",), "R3", r"dropped\[\d+\] needs idea, reason \(strings\) only",
         ('{"idea": "후보로 삼지 않은 것", "reason": "이유"}',), False),
    Rule("synthesis scope", ("synthesis",), "R5", r"cites digest keys its input did not hold: .*",
         ("**인용이 확인된**(`verified_evidence` 에 있는) 행동만", "cited_prs 에는 근거가 확인된 PR 만 넣는다"), True),
    # -- verify (verify_e2e_result.verify_problems, R5) --
    Rule("verify keys", ("verify",), "R3",
         r"keys must be exactly actionable, actionable_reason, confidence, counterexamples, distinct_prs, "
         r"distinct_repos, evidence_checks, id, revised_statement, verdict",
         ('"id": "<후보 id>"', '"verdict": ', '"evidence_checks": [', '"counterexamples": [', '"distinct_prs": ',
          '"distinct_repos": ', '"actionable": ', '"actionable_reason": ', '"confidence": ', '"revised_statement": ',
          "JSON 객체 하나만 출력한다"), False),
    Rule("verify id", ("verify",), "R3", r"id .* is not the expected candidate .*", ('"id": "<후보 id>"',), False),
    Rule("verify verdict", ("verify",), "R3", r"verdict must be supported, weak, or unsupported",
         ('"verdict": "supported | weak | unsupported"',), False),
    Rule("verify confidence", ("verify",), "R3", r"confidence must be high, medium, or low",
         ('"confidence": "high | medium | low"',), False),
    Rule("verify actionable", ("verify",), "R3", r"actionable must be a boolean", ('"actionable": true',), False),
    Rule("verify counts", ("verify",), "R3", r"distinct_(prs|repos) must be a non-negative integer",
         ('"distinct_prs": 0', '"distinct_repos": 0'), False),
    Rule("verify texts", ("verify",), "R3", r"(actionable_reason|revised_statement) must be a string",
         ('"actionable_reason": "…"', '"revised_statement": "'), False),
    Rule("verify checks", ("verify",), "R3", r"evidence_checks must be a non-empty list",
         ("`evidence_checks` 는 인용된 PR 마다 하나씩 쓰고 비어 있으면 안 된다",), False),
    Rule("verify check item", ("verify",), "R3", r"evidence_checks\[\d+\] needs pr, holds \(bool\), quote, reason only",
         ('{"pr": "<키>", "holds": true, "quote": "<원문 인용>", "reason": "…"}',), False),
    Rule("verify counterexample", ("verify",), "R3",
         r"counterexamples must be a list|counterexamples\[\d+\] needs pr, quote, reason \(strings\) only.*",
         ('"counterexamples": [{"pr": "<키>", "quote": "<원문 인용>", "reason": "…"}]',), False),
    Rule("verify size", ("verify",), "R3", r"verify answer is \d+ bytes \(> verify_answer_max_bytes \d+\)",
         ("출력한 답 그대로의 바이트 수(UTF-8, 한글 한 글자는 3바이트, 공백·줄바꿈 포함)가 {verify_answer_max_bytes} "
          "바이트 이하", "{verify_answer_target_bytes} 바이트 이하로 써라"), False),
    Rule("verify section scope", ("verify",), "R5", r"names PRs whose section was not in its prompt: .*",
         ("인용은 그 PR 의 섹션 안에서만 가져온다", "목록에만 있는 PR 은 본문이 없어 인용할 수 없다"), True),
    Rule("verify listed counterexample", ("verify",), "R5",
         r"names as unquoted counterexamples PRs its prompt did not list: .*",
         ('`quote` 키 없이 `{"pr": "<목록의 키>", "reason": "…"}`', "목록에도 섹션에도 없는 키는 쓰지 마라"), True),
    # -- final (verify_e2e_result.final_problems, search_clue_problems, R5, Q1) --
    Rule("final keys", ("final",), "R3", r"keys must be exactly extraction_ledger, limitations, patterns, records",
         ('"records": {', '"patterns": [', '"extraction_ledger": ', '"limitations": [', "JSON 객체 하나만 출력한다"),
         False),
    Rule("final records", ("final",), "R3", r"records must name every corpus PR-\* id exactly once: .*",
         ("모든 `pr_id` 를 정확히 한 번씩",), False),
    Rule("final patterns", ("final",), "R3", r"patterns must be a list of at most \d+",
         ("`patterns` 는 최대 {max_patterns}개다", "넘으면 답 전체가 거부된다"), False),
    Rule("final evidence", ("final",), "R3", r"patterns\[\d+\]\.confidence\.evidence must be a list of strings",
         ('"confidence": {"level": "low", "evidence": [', "배열의 항목은 비어 있지 않은 문자열이다"), False),
    Rule("final quote pr", ("final",), "R3", r"patterns\[\d+\] quotes unknown PR-\d+",
         ('`PR-001 "<확인된 인용 그대로>"` 형태', "근거 PR 이 아닌 PR 은 인용하지 마라"), False),
    Rule("final ledger", ("final",), "R3", r"extraction_ledger must be a nonempty string",
         ("`extraction_ledger` 는 비어 있지 않은 한 줄 문자열이다",), False),
    Rule("final limitations", ("final",), "R3", r"limitations must be a list of nonempty strings",
         ("`limitations` 는 이번 분석의 한계를 적은 문자열 배열이다(항목은 비어 있지 않은 문자열",), False),
    Rule("final quote entries", ("final",), "R3", r'patterns carry no PR-\* "quote" entry',
         ("항목을 하나 이상 넣는다",), False),
    Rule("final search clues", ("final",), "R6",
         r"patterns\[\d+\]\.search_clues(: not a list|\[\d+\]: (not a string|empty|longer than \d+ characters \(\d+\)|"
         r"edge spaces|double quote|backslash|control character|Hangul .*))",
         ("설명 문장·한국어를 쓰지 않는다", "한 항목은 1~{search_clue_max_chars}자이고 큰따옴표·백슬래시·줄바꿈·탭 같은 "
          "제어 문자·앞뒤 공백이 없다", "어기면 답 전체가 거부된다"), False),
    Rule("final quote scope", ("final",), "R5",
         r"\d+ pattern quote\(s\) are not among the verified quotes its input listed .*|\d+ of them carry a JSON escape .*",
         ("인용은 `verified_evidence` 에서 글자 그대로 복사한다", "JSON 표기일 뿐 원문 글자가 아니므로 인용에 옮기지 마라"),
         True),
    Rule("final quote", ("final",), "Q1", r"unknown-key|empty-quote|length|not-verbatim",
         ("인용은 `verified_evidence` 에서 글자 그대로 복사한다",), False),
    # -- assess (verify_e2e_result.assess_problems and the items after it; field types: assess_contract) --
    Rule("assess keys", ("assess",), "R3", r"keys must be exactly schema_version, discovery_sha256, assessments",
         ('최상위 키는 정확히 `"schema_version"`, `"discovery_sha256"`, `"assessments"` 다',), True),
    Rule("assess version", ("assess",), "R3", r"schema_version must be 1\.0\.0", ('"schema_version": "1.0.0"',), True),
    Rule("assess discovery", ("assess",), "R3|A1",
         r"discovery_sha256 must be a string|answer discovery_sha256 .* is not the input's .*",
         ("최상위 `discovery_sha256` 은 「판정 단위」의 값을 글자 그대로 복사한다",), True),
    Rule("assess count", ("assess",), "R3", r"assessments must be a list of 1 to \d+",
         ("assessment 를 하나 이상 쓴다", "합계는 `max_assessments` 이하다"), True),
    Rule("assess fields", ("assess",), "R3",
         r"assessments\[\d+\](\.[A-Za-z0-9_.]+)?: (missing|unsupported( key .*)?|expected .*)",
         ("항목의 키는 아래 19개뿐이다", "필드 타입(verifier `record` 게이트가 검사하는 타입이다",
          "`sensitivity` 는 `normal` 또는 `security-sensitive` 다", "{assessment_field_types}"), True),
    Rule("assess combinations", ("assess",), "R3",
         r"assessments\[\d+\] assesses .* not a combination of its input|no assessment for .*",
         ("조합마다 assessment 를 하나 이상 쓴다", "목록 밖 조합과 `not_assessable` 조합은 쓰지 마라"), True),
    Rule("assess links", ("assess",), "R5",
         r"assessments\[\d+\] (has \d+ link value\(s\) that are not strings|cites \d+ link\(s\) its input did not hold: .*)",
         ("「허용 근거 링크」의 URL 만", "문장 안의 URL 도 링크로 검사한다"), True),
    Rule("assess policy", ("assess",), "A2",
         r"assessments\[\d+\]\.policy_checks\.\S+ (names a source .*|names a digest; .*|names policy bytes .*)",
         ("`found: true` 이면 `source` 와 `sha256` 을", "`program_rules` 는 이번 입력에 없다"), True),
    Rule("assess rules", ("assess",), "A4",
         r"assessments\[\d+\]\.(execution_evidence must be null.*|reproduction\.method executed.*|"
         r"private_evidence_reference must be null outside private-report-ready)",
         ("`execution_evidence` 는 `null` 이다", "`reproduction.method` 는 `static` 또는 `none` 이다",
          "`private_evidence_reference` 는 `private-report-ready` 가 아니면 `null` 이다"), True),
]


def rules_for(prompt: str) -> list:
    """The rows a prompt (a PROMPTS name) must state."""
    kind, scoped = PROMPTS[prompt]
    return [rule for rule in RULES if kind in rule.kinds and (scoped or not rule.scoped)]


NAME = re.compile(r"\{([a-z_]+)\}")


def words(rule: Rule, filled: dict) -> list:
    """A row's words with each ``{name}`` filled in (the JSON braces of the words stay); ``filled`` may also hold whole
    sections (assessment_field_types). A name ``filled`` does not hold is an error, never left as it is."""
    def fill(match):
        if match.group(1) not in filled:
            raise KeyError("%s names %r, which is not a known value" % (rule.name, match.group(1)))
        return str(filled[match.group(1)])

    return [NAME.sub(fill, phrase) for phrase in rule.says]


def kinds() -> list:
    return sorted({PROMPTS[name][0] for name in PROMPTS})
