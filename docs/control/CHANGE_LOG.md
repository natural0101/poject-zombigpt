# Change log for this branch

What changed, under which step, with the commit. `CHANGELOG.md` at the root is
the product's; this one is the plan's.

_(entries appended per commit)_
## 50c851c — steps 1–10

The control system: plan, status, gate, counter, baseline evidence.

## 367124b — steps 11–19

`canonical_json_bytes`, atomic binary writes, digests read back from the file,
`size_bytes` from the file. Carries the redactor and MCP-block fixes too, which
are credited later under steps 23–24 and 41–45 rather than here.

Linux: 3712 passed, 2 skipped. Windows: still red, by design — step 30 is where
that is verified, and nothing before it may claim otherwise.
