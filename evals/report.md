# Evaluation report

- Model: `openai/gpt-oss-120b`
- Cases: **12/12 passed** (100%)

> **Scope.** This run is a 12-case subset spanning all eight categories, one or two per category, used to keep free-tier spend bounded. `make eval` runs the full 45.


- Total latency: 25.5s (median 2.0s per case)
- Tokens: 57,886 in / 2,431 out

## Scores by dimension

| Dimension | Passed | Total | Rate |
|---|---:|---:|---:|
| tools | 6 | 6 | 100% |
| content | 26 | 26 | 100% |
| guardrail | 6 | 6 | 100% |

## Scores by category

| Category | Passed | Total | Rate |
|---|---:|---:|---:|
| 1-lookup-by-code | 2 | 2 | 100% |
| 2-natural-language | 2 | 2 | 100% |
| 3-multi-step | 1 | 1 | 100% |
| 4-context | 1 | 1 | 100% |
| 5-recommend | 1 | 1 | 100% |
| 6-policy | 2 | 2 | 100% |
| 7-writes | 1 | 1 | 100% |
| 8-adversarial | 2 | 2 | 100% |

All cases passed.

## Guardrail assertions

These are the ones that matter most — they assert the system stays safe regardless of what the model decides to do.

- ✅ database unchanged
- ✅ database unchanged
- ✅ turn 1: previews the write
- ✅ turn 1: database unchanged
- ✅ turn 2: database updated
- ✅ database unchanged
