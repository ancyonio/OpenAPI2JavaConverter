# Raw measurement data

The prompts the judges were actually given, and their raw answers. Kept so the numbers in
`../RESULTS.md` can be re-derived rather than taken on trust:

```bash
python -I tests/trigger-evals/score_routing.py \
  tests/trigger-evals/runs/iteration-1/judge-prompt.md \
  tests/trigger-evals/runs/iteration-1/judge-1.json \
  tests/trigger-evals/runs/iteration-1/judge-2.json \
  tests/trigger-evals/runs/iteration-1/judge-3.json
```

`iteration-1` is the shipped descriptions. `iteration-2` is the scope clause that was added to
`openapi-codegen-determinism` and then reverted — kept because the evidence for reverting is
more useful than the change was.

Each `judge-prompt.md` embeds the exact descriptions in force for that run, so a prompt plus its
answers is a self-contained record. Scoring joins on query text, so the two iterations'
different query orderings are harmless.
