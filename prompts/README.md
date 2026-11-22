# Prompt register

Build prompts are read from `prompts/build/`. Their version headers and content hashes are recorded with provider decisions. Customer and source text are untrusted data; neither can override the prompt.

| Prompt | Version | Purpose |
|---|---|---|
| PR-03 | 1.4 | Assess sensitivity and whether a request can safely receive an informational answer |
| PR-04 | 1.4 | Draft concise atomic claims with supporting passage citations |
| PR-05 | 1.5 | Block unsupported, irrelevant, unsafe or out-of-scope drafts using claim-local evidence |

The `evaluation/` prompts assist review; they do not replace the two independent human reviewers required by the evaluation framework. `baseline-v1.1/` preserves the earlier prompt revision for traceability. Removed Decider experiments remain in the local pre-cleanup archive and historical evidence; they are not part of the runtime.
