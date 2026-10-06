#!/usr/bin/env python3
"""Write one self-contained prompt file per independent extraction or verification call.

  extract  one prompt per digest shard and lens: OUT/prompts/extract-<lens>[-NN].md
           (--max-patterns/--max-observations/--max-rejected, all three or none, state
           the answer's count limits in the rules, and --max-answer-bytes with them its
           size limit, and --answer-target-bytes below it the size to aim for; without
           them the bytes are as before)
  verify   one prompt per merged candidate: OUT/prompts/verify-<id>.md (--max-answer-bytes states the
           answer's size limit, measured on the bytes as written, with the rule that evidence_checks is not empty,
           and --answer-target-bytes below it the size to aim for; without them the bytes are as before)

A prompt file is the whole input of one call, so any executor can run it: agy
through dispatching-agy-workers, a tool-less sub-agent, or the coordinator
itself. Each call answers with one JSON object saved as
OUT/results/<prompt stem>.result.json. Every run empties OUT/prompts and
OUT/results first, so a glob never mixes in a stale prompt or answer. Use a
different OUT for extract and verify. Exit 0 on success, 2 on invalid input.
"""

import argparse
import json
from pathlib import Path
import re
import sys

LENSES = {
    "blind": "전체(관점 제한 없음). 모든 PR 에서 반복되는 행동을 찾아라.",
    "correctness": (
        "동작 정확성 — 오탐·미탐, 경계 조건, 자동 수정·코드 변환의 의미 보존, "
        "이름·형태만 보고 판정하는 로직의 한계, 바인딩·지원 버전·실행 환경 확인. "
        "이 관점에 해당하는 패턴만 찾아라."
    ),
    "structure": (
        "변경 구조 — 어떤 파일이 함께 바뀌는지(구현·등록·문서·테스트·fixture·README), "
        "문서와 실제 동작의 불일치, 외부 도구와 동작을 맞추는 회귀 테스트. "
        "이 관점에 해당하는 패턴만 찾아라."
    ),
    "maintenance": (
        "유지보수 — 의존성 업데이트(봇 PR 포함), 지원 런타임·engines·CI matrix 와의 호환, "
        "릴리스 PR, 자동 종료(autoclosed). 이 관점에 해당하는 패턴만 찾아라."
    ),
    "review": (
        "리뷰 동학 — 봇 리뷰어와 관리자 리뷰가 최종 diff 를 어떻게 바꿨는지, "
        "변경 요청→수정→승인의 흐름, 작성자가 리뷰 제안을 받아들이거나 거절한 근거. "
        "이 관점에 해당하는 패턴만 찾아라."
    ),
}
CANDIDATE_ID = re.compile(r"[A-Za-z0-9_-]+")

INTERPRETATION = """해석 규칙:
- closed-unmerged 는 머지되지 않았다는 사실뿐이다. 거절·잘못된 제안·종료 사유로 해석하지 마라. 종료 사유를 보여주는 직접 증거(관리자 발언 등)가 있을 때만 사유를 말한다.
- 승인(APPROVED) 리뷰와 머지는 다른 정보다.
- Copilot·coderabbit 같은 봇 리뷰 문장은 봇의 주장이다. 관리자 판단으로 취급하지 마라.
- 작성자의 테스트 실행 보고는 보고일 뿐이다. 실행 결과로 취급하지 마라."""

QUOTE_RULES = """인용 규칙:
- 인용(quote)은 다이제스트에서 글자 그대로 복사한 20~200자다. 번역·요약·수정하지 마라. 원문 언어 그대로다.
- 한 인용은 한 곳에서 이어진 글자다. 여러 줄이나 여러 곳을 `...` 로 이어 붙이지 마라. 백틱·따옴표도 원문 그대로 둔다.
- 인용은 그 PR 의 섹션 안에서만 가져온다. 제목·본문·diff·커밋 메시지·코멘트 원문만 인용이 된다. `###` 줄의 키, `url:`·`repository:`·`state:` 줄, `- 경로 (modified +3/-1)` 같은 목록 머리, `[작성자]` 표시, `…[+N chars]` 표시는 다이제스트가 붙인 것이라 인용이 될 수 없다."""

EXTRACT_TAIL = """패턴의 기준:
- 패턴은 서로 다른 PR 2개 이상에서 반복해서 관찰되는 행동이다. PR 1개에서만 보이는 것은 patterns 가 아니라 observations 에 넣는다.
- 모든 근거(evidence)에는 인용을 붙인다.
- 다른 오픈소스 저장소에서 기여 후보를 찾는 데 쓸 수 있어야 한다. search_clues 는 후보 저장소에서 GitHub 검색(`repo:<저장소> "<단서>"` 꼴의 이슈·PR·코드 검색)에 그대로 넣을 짧은 문자열이다: 파일 경로 조각, 헤딩·코드 토큰, 도메인, 정확한 영어 문구(예: `## Options`, `further_reading`, `npm install eslint`, `kangax.github.io`). 설명 문장·한국어를 쓰지 않는다. 한 항목은 1~200자이고 큰따옴표·백슬래시·줄바꿈·앞뒤 공백이 없다. 앞의 5개만 검색에 쓰인다.
- 억지로 개수를 늘리지 마라. 근거가 약하면 rejected 에 이유와 함께 넣는다. 패턴이 0개여도 된다.

출력: 설명·코드펜스 없이 아래 형태의 JSON 객체 하나만 출력한다. 문자열 값은 한국어로 쓰되 quote 는 원문 그대로, search_clues 는 위 규칙의 검색 문자열로 둔다.
{
  "lens": "<LENS_ID>",
  "patterns": [
    {
      "title": "짧은 이름",
      "description": "재사용 가능한 행동을 1~2문장으로",
      "kind": "defect-class | convention | maintenance-policy | review-dynamic",
      "evidence": [
        {"pr": "<키>", "role": "supports | counterexample", "quote": "<원문 인용>", "claim": "이 PR 이 패턴을 어떻게 보여주는지"}
      ],
      "applicability": ["적용 조건"],
      "counterconditions": ["적용하면 안 되는 조건"],
      "search_clues": ["<검색 문자열>"],
      "expected_tests": ["기여할 때 필요한 테스트"],
      "maintainer_judgment_required": ["관리자가 판단해야 할 남은 문제"],
      "confidence": "high | medium | low",
      "confidence_reason": "신뢰도의 이유와 한계"
    }
  ],
  "observations": [
    {"pr": "<키>", "note": "PR 1개에서만 보인 행동", "quote": "<원문 인용>"}
  ],
  "rejected": [
    {"idea": "검토했지만 패턴으로 삼지 않은 것", "reason": "이유"}
  ]
}"""

# The rule the extract tail gains when the caller passes the count limits its checker enforces.
COUNT_RULE_ANCHOR = "패턴이 0개여도 된다.\n"
COUNT_RULE = (
    "- 개수 상한: patterns 는 최대 {patterns}개, observations 는 최대 {observations}개, rejected 는 최대 {rejected}개다. "
    "넘으면 답 전체가 거부된다. 넘치는 항목은 근거가 약한 것부터 버려라(rejected 나 observations 로 옮기지 마라). "
    "개수를 맞추려고 근거를 지어내지 마라.\n"
)
# The rule after the count rule when the caller also passes the answer size limit its checker enforces.
SIZE_RULE = (
    "- 답 크기 상한: 답 JSON 을 들여쓰기 1칸으로 다시 쓴 크기(UTF-8, 한글 한 글자는 3바이트)가 {answer_bytes} 바이트 "
    "이하여야 한다. 넘으면 답 전체가 거부된다. 넘칠 것 같으면 note·claim·description 을 짧게 쓰고 근거가 약한 "
    "항목부터 버려라. 인용(quote)은 원문 그대로 두고 자르지 마라.\n"
)
# The rule after the size rule when the caller also passes a target under that limit: the limit is measured on the
# answer rewritten as indent-1 JSON, which a model cannot measure on the bytes it writes, so the prompt asks for less.
TARGET_RULE = (
    "- 답 크기 목표: 상한에 여유를 두고 들여쓰기 1칸으로 다시 쓴 크기 {target_bytes} 바이트 이하로 써라. "
    "공백 없이 출력해도 들여쓰기 1칸으로 다시 써서 재므로, 출력한 바이트보다 15% 안팎 커진다.\n"
)

VERIFY_TAIL = """너는 반박 검증자다. 아래 후보 패턴 하나가 다이제스트로 뒷받침되는지 따진다. 후보를 만든 사람의 편을 들지 마라. 근거가 약하면 약하다고 말하는 것이 네 일이다.

확인할 것:
1. 인용된 PR 마다 다이제스트가 그 주장을 실제로 보여주는지. 인용 규칙을 지킨 인용을 붙인다.
2. 인용되지 않은 다른 PR 중 반례가 있는지.
3. 서로 다른 PR 2개 이상에서 반복되는지. 한 저장소 안의 반복인지 여러 저장소에 걸친 반복인지.
4. 다른 오픈소스 저장소에서 기여 후보를 찾는 데 쓸 수 있는지(actionable). 결함 종류를 가리키면 쓸 수 있고, 일상 절차나 관례 설명뿐이면 쓸 수 없다.
5. 신뢰도. 한 저장소의 사례만으로는 high 가 될 수 없다. closed-unmerged PR 은 머지된 변경의 근거가 아니다.

출력: 설명·코드펜스 없이 JSON 객체 하나만 출력한다.
{
  "id": "<후보 id>",
  "verdict": "supported | weak | unsupported",
  "evidence_checks": [{"pr": "<키>", "holds": true, "quote": "<원문 인용>", "reason": "…"}],
  "counterexamples": [{"pr": "<키>", "quote": "<원문 인용>", "reason": "…"}],
  "distinct_prs": 0,
  "distinct_repos": 0,
  "actionable": true,
  "actionable_reason": "…",
  "confidence": "high | medium | low",
  "revised_statement": "근거에 맞게 고친 패턴 문장 (고칠 필요 없으면 원문)"
}

후보:"""

# The rules the verify tail gains before its output form when the caller passes the answer size limit its checker
# enforces (#286). The checker measures the answer's own bytes, whitespace included, so the rule asks for one line.
VERIFY_RULES_ANCHOR = "\n출력: 설명·코드펜스 없이 JSON 객체 하나만 출력한다.\n{"
VERIFY_RULES = (
    "\n규칙:\n"
    "- `evidence_checks` 는 인용된 PR 마다 하나씩 쓰고 비어 있으면 안 된다. 비어 있으면 답 전체가 거부된다.\n"
    "- 답 크기 상한: 출력한 답 그대로의 바이트 수(UTF-8, 한글 한 글자는 3바이트, 공백·줄바꿈 포함)가 {answer_bytes} 바이트 "
    "이하여야 한다. 넘으면 답 전체가 거부된다. 들여쓰기·줄바꿈 없이 한 줄로 출력하고, 넘칠 것 같으면 reason·"
    "actionable_reason·revised_statement 를 짧게 써라. 인용(quote)은 원문 그대로 두고 자르지 마라.\n"
)
VERIFY_TARGET_RULE = "- 답 크기 목표: 상한에 여유를 두고 같은 측정으로 {target_bytes} 바이트 이하로 써라.\n"
# A prompt that carries only the cited sections lists the other PRs by title; such a PR has no text to quote, so a
# counterexample naming it is the one item without a quote (the checker keeps it to the listed keys, #286).
LISTED_SCOPE = (
    "PR 이 많아 후보가 인용한 PR 섹션만 싣고 나머지는 제목 목록만 싣는다. "
    "목록에만 있는 PR 은 본문이 없어 인용할 수 없다. 그런 PR 이 반례로 의심되면 counterexamples 에 PR 하나마다 "
    '`quote` 키 없이 `{"pr": "<목록의 키>", "reason": "…"}` 로 적는다(`pr` 은 목록의 키 그대로 쓰고 비우지 않는다). '
    "섹션이 실린 PR 의 반례에는 인용을 붙인다. 목록에도 섹션에도 없는 키는 쓰지 마라. 어기면 답 전체가 거부된다."
)


class PromptError(ValueError):
    pass


def _load_keymap(digest_dir):
    path = Path(digest_dir) / "keymap.json"
    try:
        keymap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromptError("keymap.json could not be read; run build_digest.py first: " + str(error))
    if (
        not isinstance(keymap, dict)
        or not isinstance(keymap.get("sections"), dict)
        or not isinstance(keymap.get("shards"), list)
        or not all(isinstance(section, dict) for section in keymap["sections"].values())
    ):
        raise PromptError("keymap.json is not a build_digest.py keymap")
    return keymap


def _read_shard(digest_dir, name):
    try:
        return (Path(digest_dir) / name).read_text(encoding="utf-8")
    except OSError as error:
        raise PromptError("digest shard could not be read: " + str(error))


def _sections(text, keys):
    """Split a digest file into {key: section text} at the headers of known keys."""
    matches = [match for match in re.finditer(r"^### (\S+) — ", text, re.M) if match.group(1) in keys]
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.start():end].rstrip() + "\n"
    return sections


def _head(keymap, keys, scope):
    repositories = sorted({keymap["sections"][key]["repository"] for key in keymap["sections"]})
    examples = ", ".join(keys[:3])
    return (
        "너는 오픈소스 PR 코퍼스에서 재사용 가능한 변경 패턴을 다루는 분석 워커다. "
        "도구를 쓰지 마라. 파일을 읽거나 검색하지 마라. 아래 다이제스트만 근거로 쓴다.\n\n"
        "코퍼스는 저장소 " + str(len(repositories)) + "개(" + ", ".join(repositories) + ")에서 "
        "종료된 PR " + str(len(keymap["sections"])) + "개다. " + scope + " "
        "각 PR 은 `### <키> — <제목>` 으로 시작하고, 키(예: " + examples + ")가 PR 의 유일한 식별자다.\n\n"
        + INTERPRETATION + "\n\n" + QUOTE_RULES + "\n\n=== 다이제스트 시작 ===\n"
    )


def _prepare(out_dir, kind):
    """Empty OUT/prompts of this kind and OUT/results so no stale call or answer survives."""
    prompts, results = Path(out_dir) / "prompts", Path(out_dir) / "results"
    other = "verify" if kind == "extract" else "extract"
    if list(prompts.glob(other + "-*.md")):
        raise PromptError("--out already holds " + other + " prompts; use a separate run directory")
    prompts.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    for stale in [*prompts.glob(kind + "-*.md"), *results.glob("*.result.json")]:
        stale.unlink()
    return prompts


def _write(prompts_dir, name, text):
    path = Path(prompts_dir) / name
    path.write_text(text, encoding="utf-8")
    return {"file": str(path), "bytes": len(text.encode("utf-8"))}


def extract_tail(limits=None):
    """EXTRACT_TAIL, with the count rule after the last criterion when limits {patterns, observations, rejected},
    and the size rule after it when limits also holds answer_bytes, and the target rule after that when it holds
    target_bytes (#280)."""
    if limits is None:
        return EXTRACT_TAIL
    rules = COUNT_RULE.format(**limits)
    if limits.get("answer_bytes") is not None:
        rules += SIZE_RULE.format(**limits)
    if limits.get("target_bytes") is not None:
        rules += TARGET_RULE.format(**limits)
    return EXTRACT_TAIL.replace(COUNT_RULE_ANCHOR, COUNT_RULE_ANCHOR + rules, 1)


def verify_tail(limits=None):
    """VERIFY_TAIL, with the rules before its output form when limits hold answer_bytes, and the target rule after
    them when they also hold target_bytes (#286)."""
    if limits is None or limits.get("answer_bytes") is None:
        return VERIFY_TAIL
    rules = VERIFY_RULES.format(**limits)
    if limits.get("target_bytes") is not None:
        rules += VERIFY_TARGET_RULE.format(**limits)
    return VERIFY_TAIL.replace(VERIFY_RULES_ANCHOR, rules + VERIFY_RULES_ANCHOR, 1)


def extract(digest_dir, out_dir, lenses, limits=None):
    keymap = _load_keymap(digest_dir)
    unknown = [lens for lens in lenses if lens not in LENSES]
    if unknown:
        raise PromptError("unknown lens: " + ", ".join(unknown) + " (choose from " + ", ".join(LENSES) + ")")
    tail = extract_tail(limits)
    prompts = _prepare(out_dir, "extract")
    shards = keymap["shards"]
    written = []
    for index, shard in enumerate(shards, start=1):
        text = _read_shard(digest_dir, shard)
        keys = [key for key, section in keymap["sections"].items() if section.get("shard") == shard]
        if len(shards) == 1:
            scope = "다이제스트는 그 전체다."
        else:
            scope = (
                "PR 수가 많아 다이제스트를 " + str(len(shards)) + "조각으로 나눴고 이것은 그중 "
                + str(index) + "번째 조각(PR " + str(len(keys)) + "개)이다. 다른 조각의 PR 은 "
                "조정자가 나중에 합친다. 이 조각에서 PR 1개로만 보이는 행동도 observations 에 빠짐없이 적어라."
            )
        suffix = "" if len(shards) == 1 else "-" + str(index).zfill(2)
        for lens in lenses:
            prompt = (
                _head(keymap, keys, scope)
                + text.rstrip()
                + "\n=== 다이제스트 끝 ===\n\n이번 관점: "
                + lens
                + " — "
                + LENSES[lens]
                # The answer's "lens" is the machine id alone, never the description above.
                + '\n출력의 "lens" 값은 정확히 "' + lens + '" 이다(설명 없이 이 식별자 그대로).'
                + "\n\n"
                + tail.replace('"<LENS_ID>"', '"' + lens + '"')
                + "\n"
            )
            written.append(_write(prompts, "extract-" + lens + suffix + ".md", prompt))
    return written


def _load_candidates(path, keymap):
    try:
        candidates = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromptError("candidates could not be read as JSON: " + str(error))
    if not isinstance(candidates, list) or not candidates:
        raise PromptError("candidates must be a nonempty JSON array")
    seen = set()
    for index, candidate in enumerate(candidates):
        label = "candidates[" + str(index) + "]"
        if not isinstance(candidate, dict):
            raise PromptError(label + " must be an object")
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(candidate_id):
            raise PromptError(label + ".id must match [A-Za-z0-9_-]+")
        if candidate_id in seen:
            raise PromptError("duplicate candidate id: " + candidate_id)
        seen.add(candidate_id)
        if not isinstance(candidate.get("statement"), str) or not candidate["statement"].strip():
            raise PromptError(label + ".statement must be a nonempty string")
        cited = candidate.get("cited_prs")
        if not isinstance(cited, list) or not cited or not all(isinstance(key, str) for key in cited):
            raise PromptError(label + ".cited_prs must be a nonempty array of digest keys")
        missing = [key for key in cited if key not in keymap["sections"]]
        if missing:
            raise PromptError(label + " cites unknown digest keys: " + ", ".join(map(str, missing)))
    return candidates


def verify(digest_dir, candidates_path, out_dir, limits=None):
    keymap = _load_keymap(digest_dir)
    tail = verify_tail(limits)
    candidates = _load_candidates(candidates_path, keymap)
    prompts = _prepare(out_dir, "verify")
    shards = {name: _read_shard(digest_dir, name) for name in keymap["shards"]}
    all_keys = list(keymap["sections"])
    written = []
    for candidate in candidates:
        if len(shards) == 1:
            material = next(iter(shards.values())).rstrip()
            scope = "다이제스트는 그 전체다."
        else:
            sections = {}
            for text in shards.values():
                sections.update(_sections(text, keymap["sections"]))
            cited = list(dict.fromkeys(candidate["cited_prs"]))
            index = "\n".join(
                "- " + key + " — " + str(keymap["sections"][key]["title"])
                + " (" + str(keymap["sections"][key]["state"]) + ")"
                for key in all_keys
                if key not in cited
            )
            material = "\n\n".join(sections[key].rstrip() for key in cited)
            if index:
                material += "\n\n### 나머지 PR 목록 (본문 없음, 반례 후보를 가리킬 때만 쓴다)\n" + index
            scope = LISTED_SCOPE
        prompt = (
            _head(keymap, all_keys, scope)
            + material
            + "\n=== 다이제스트 끝 ===\n\n"
            + tail
            + "\n"
            + json.dumps(candidate, ensure_ascii=False, indent=1)
            + "\n"
        )
        written.append(_write(prompts, "verify-" + candidate["id"] + ".md", prompt))
    return written


def _positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("must be a positive integer, got " + repr(value))
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    extract_parser = commands.add_parser("extract", help="one prompt per digest shard and lens")
    extract_parser.add_argument("--digest-dir", required=True, help="build_digest.py --out directory")
    extract_parser.add_argument("--out", required=True, help="run directory; prompts/ and results/ go under it")
    extract_parser.add_argument(
        "--lens", action="append", choices=sorted(LENSES), help="lens to run; repeat. Default: all five"
    )
    for kind in ("patterns", "observations", "rejected"):
        extract_parser.add_argument(
            "--max-" + kind, type=_positive, help="count limit the answer's " + kind + " must keep; give all three or none"
        )
    extract_parser.add_argument(
        "--max-answer-bytes", type=_positive, help="size limit of the answer (indent-1 JSON, UTF-8); needs the count limits"
    )
    extract_parser.add_argument(
        "--answer-target-bytes", type=_positive, help="size to aim for, under --max-answer-bytes (the same measure)"
    )
    verify_parser = commands.add_parser("verify", help="one adversarial prompt per candidate")
    verify_parser.add_argument("--digest-dir", required=True, help="build_digest.py --out directory")
    verify_parser.add_argument("--candidates", required=True, help='JSON array of {"id", "statement", "cited_prs"}')
    verify_parser.add_argument("--out", required=True, help="run directory, not the extract one; prompts/ and results/ go under it")
    verify_parser.add_argument(
        "--max-answer-bytes", type=_positive, help="size limit of the answer, its bytes as written (UTF-8, whitespace included)"
    )
    verify_parser.add_argument(
        "--answer-target-bytes", type=_positive, help="size to aim for, under --max-answer-bytes (the same measure)"
    )
    args = parser.parse_args(argv)
    if args.command == "extract":
        limits = {kind: getattr(args, "max_" + kind) for kind in ("patterns", "observations", "rejected")}
        given = [value is not None for value in limits.values()]
        if any(given) and not all(given):
            extract_parser.error("--max-patterns, --max-observations and --max-rejected go together")
        if args.max_answer_bytes is not None and not all(given):
            extract_parser.error("--max-answer-bytes needs --max-patterns, --max-observations and --max-rejected")
        limits["answer_bytes"] = args.max_answer_bytes
        if args.answer_target_bytes is not None and (args.max_answer_bytes is None
                                                     or args.answer_target_bytes >= args.max_answer_bytes):
            extract_parser.error("--answer-target-bytes needs --max-answer-bytes and must be below it")
        limits["target_bytes"] = args.answer_target_bytes
    else:
        if args.answer_target_bytes is not None and (args.max_answer_bytes is None
                                                     or args.answer_target_bytes >= args.max_answer_bytes):
            verify_parser.error("--answer-target-bytes needs --max-answer-bytes and must be below it")
        limits = {"answer_bytes": args.max_answer_bytes, "target_bytes": args.answer_target_bytes}
    try:
        if args.command == "extract":
            written = extract(args.digest_dir, args.out, args.lens or list(LENSES), limits if all(given) else None)
        else:
            written = verify(args.digest_dir, args.candidates, args.out, limits)
    except PromptError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(written, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
