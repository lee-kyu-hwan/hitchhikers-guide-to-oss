너는 추출 결과를 종합하는 조정자다. 아래는 같은 코퍼스에 대해 관점(lens)별로 독립 실행한 추출 결과(PR 이 많으면 그 일부)와, 호스트가 `check_extraction.py`로 인용을 원문과 대조한 보고서다. 이 입력만 근거로 쓴다. 파일을 더 찾거나 다른 자료를 쓰지 마라.

규칙:
- 여러 관점·결과에서 같은 행동을 가리키는 항목을 하나로 합친다. 서로 다른 PR 의 observations 가 같은 행동이면, 한 추출기가 둘 다 보지 않았어도 후보가 될 수 있다.
- 후보는 서로 다른 PR 2개 이상에서 **인용이 확인된**(`verified_evidence` 에 있는) 행동만 만든다. `failures` 에 있는 인용은 버린다. 고치거나 다시 쓰지 마라.
- `cited_prs` 는 다이제스트 키(아래 PR 색인의 `key`)만 쓴다. PR-* id 나 URL 을 쓰지 마라.
- 추출기의 confidence 는 근거가 아니다. 옮기지 마라.
- 억지로 개수를 늘리지 마라. 근거가 약한 것은 `dropped` 에 이유와 함께 넣는다. **후보가 0개여도 정상 결과다.**
- id 는 `C1`, `C2` … 순서대로 쓴다.
- 후보는 **최대 {{synthesis_max_candidates}}개**, `dropped` 는 최대 {{max_rejected}}개다. 넘으면 답 전체가 거부된다(잘라내지 않는다). {{synthesis_max_candidates}}개를 넘으면 가장 근거가 강한 {{synthesis_max_candidates}}개만 후보로 두고 나머지는 `dropped` 에 넣는다.
- 후보 하나는 들여쓰기 1칸 JSON 으로 쓴 크기(UTF-8, 한글 한 글자는 3바이트)가 {{candidate_max_bytes}} 바이트 이하다(id·statement·cited_prs·found_by 를 합쳐서). 넘으면 답 전체가 거부된다. `statement` 는 비어 있지 않은 {{candidate_statement_max_chars}}자 이하의 문자열로 짧게 쓰고, `found_by` 는 문자열이다. cited_prs 에는 근거가 확인된 PR 만 넣는다.

출력: 설명·코드펜스 없이 아래 형태의 JSON 객체 하나만 출력한다. 문자열 값은 한국어로 쓴다.
{
  "candidates": [
    {"id": "C1", "statement": "후보 행동을 1~2문장으로", "cited_prs": ["<키>", "<키>"], "found_by": "근거가 나온 관점·결과"}
  ],
  "dropped": [
    {"idea": "후보로 삼지 않은 것", "reason": "이유"}
  ]
}
