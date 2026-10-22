## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was verified

- [ ] `make lint typecheck test` passes locally
- [ ] Frontend touched: `make frontend e2e` passes
- [ ] Behaviour change has a test that fails without it
- [ ] Numbers quoted in docs were re-measured, not copied

## Safety

- [ ] No credential, key, token or account data anywhere in the diff
- [ ] No live order path added, and no risk control or hard limit weakened
- [ ] Market data stays real (live or recorded); no synthetic prices
