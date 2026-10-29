# Willy's Hermes fork upgrade procedure

This is the standing procedure for maintaining Willy's Hermes fork. It applies
to this fork's source-managed Linux host deployment, not upstream release
engineering or every supported platform. Personal OS owns the live deployment,
its approvals, private evidence and task thread. A project is the repository
workspace; each upgrade outcome is a task with one owning thread.

The October 22, 2026 v0.22.2 rollout established the baseline: final affected
core and companion tests, copied-state rehearsal, recoverable source/runtime
bundles, encrypted backup extraction, controlled cutover and live verification.
That rollout also changed gateway topology and credentials. Repeat those extra
migration checks only when the new change affects them.

## 1. Establish scope and authority

- Pin the official release commit, current live fork commit and candidate base.
  Inspect release notes, relevant regressions and all local customizations plus
  their upstream callers. Keep official `origin` fetch-only; push only to `wcass`.
- Prepare in an isolated checkout. Preserve concurrent fork changes and unrelated
  dirty work. Update the existing fork PR and owning thread rather than creating
  a second implementation owner.
- Candidate preparation, source review, merge and live deployment have distinct
  approval boundaries. Record the actual authorization. A draft PR, passing
  tests, or this procedure does not authorize a merge or deployment.
- Keep profiles, grants, models, skill/tool allowlists, delivery safeguards and
  external service ownership intact unless Willy explicitly approves a change.
  Do not use real business writes, test email or credential expansion as tests.

## 2. Prepare isolated tests and control temporary storage

- Check available bytes and inodes on the filesystems holding the checkout,
  <!-- no-tmp: ok — capacity inspection, not a scratch allocation -->
  runtime, test scratch, `/tmp` and `/var/tmp` before a large run. Record baseline
  usage, a run-specific storage budget and a stop threshold that leaves room for
  live services. Check usage during long runs; do not wait for exhaustion.
- Use a locked, disposable test environment with the required explicit extras,
  scrubbed credentials and temporary homes. Production credentials and writable
  live profile paths must not enter the test environment.
- Always use `scripts/run_tests.sh`. On this Linux target its per-file runner
  creates short disk-backed `/var/tmp/hermes-pytest-<uid>/r-*` directories, sets
  child `TMPDIR` and `PYTEST_DEBUG_TEMPROOT`, and removes each directory on exit.
  Keep that behavior; setting only the parent `TMPDIR` is insufficient to prove
  every child or helper uses it. Verify the actual filesystem is disk-backed.
- Allocate non-runner build/rehearsal scratch explicitly on disk, with private
  permissions and short paths where Unix sockets require them. Record exact
  owned directories and child process identities. Avoid large RAM-backed
  <!-- no-tmp: ok — explicitly prohibits RAM-backed fixtures -->
  `/tmp` fixtures, shared pytest roots and unlimited parallel workers.
- Choose bounded concurrency and realistic per-file timeouts. Investigate
  killed/timed-out files before retrying; split diagnostic selections when
  appropriate. Preserve the original failed receipt and explain corrections.

## 3. Freeze and verify the candidate

- Record the final source commit/tree, companion repository commits and lockfile
  inputs. Export companion sources from exact commits, preserving executable
  modes and required fixtures. Test the code that will actually be deployed.
- Run one consolidated affected core matrix and the maintained companion matrix
  against those frozen inputs, including profile isolation, authentication,
  permissions, ACP tool filtering, plugin APIs, sessions, routing and delivery.
  This is an affected-system matrix, not a requirement to run every upstream test.
- Run all blocking static checks. Audit changed dependencies and build affected
  frontend/runtime artifacts. Reuse prior evidence only when its relevant input
  blobs are unchanged and the artifacts remain identifiable and available.
- Review custom code in both directions of interaction with upstream. Address
  actionable findings between requested review rounds. Record skipped or
  disabled remote CI/review honestly; an automatic "review skipped" is no review.
- Report failures, platform skips and unresolved gaps separately. Overlapping
  diagnostic pass counts do not establish a clean final matrix. Source changes
  after the freeze need proportionate re-verification and a new recorded head.

## 4. Rehearse startup and recovery on copies

- Stage deployable runtime/assets for the eventual canonical source layout.
  A test virtual environment tied to a temporary checkout is not a deployment
  bundle. Preserve the supported Python version, explicit extras, external
  install stamp and administrator-owned unit settings.
- Use consistent copies of current profile databases, routing, queues and
  receipts, with outbound delivery disabled. Verify migrations and integrity,
  history/ancestry, profile and transport identity, pending obligations, plugins
  and unchanged approved settings. Check configuration-schema handling so first
  boot does not reset model or delegation choices.
- Restore the old source bundle into an empty directory and execute its matching
  runtime in isolation. Retain compatible assets/configuration and an explicit
  recovery sequence. A database downgrade-open test alone is not full rollback
  proof. Rehearse rollback using copies that include representative newer state.
- Rollback must preserve and reconcile post-cutover history, receipts and
  business state. Never replace newer state blindly with an old snapshot, replay
  uncertain delivery, or run old and new consumers concurrently.
- Keep raw copied state and credentials private and outside Git. Commit only
  sanitized receipts, aggregates, hashes and source identities.

## 5. Prepare and authorize cutover

- Present the concrete deployment/rollback sequence, remaining risks and measured
  interruption estimate. A service shutdown timeout is not a total outage
  estimate. Willy chooses the acceptable maintenance window.
- Obtain a fresh encrypted backup and verify actual isolated extraction/restore
  of the required data and recovery bundle. An older rehearsal or backup exit
  code alone is insufficient. Reconcile current state again at the window.
- Check active turns and scheduled publications; drain and stop writers under
  the approved window. Capture fresh consistent checkpoints immediately before
  mutation, and retain the matching old source/runtime/assets/configuration.
- Promote only the reviewed tree, preserving newer fork commits. Record merge
  identity and any tree equivalence. Restart only authorized Hermes consumers;
  never restart the controlling T3/Codex session or host as part of this procedure.

## 6. Verify the live result and retain confirmation

- Verify exact source/runtime/assets, unit ownership and enabled state, dashboard
  endpoints, plugin health, database integrity and profile isolation.
- For the current three-profile deployment, verify all six Discord/AgentMail
  connection states. Compare sessions, messages, routing and delivery obligations
  with the stopped-writer checkpoint, accounting for legitimate newer activity.
- Check expected-state monitoring and natural delivery receipts without replaying
  business operations. Preserve pre-existing issues separately from cutover
  failures. Stop or roll back under the approved plan if acceptance fails.
- Take and extract a post-deployment encrypted backup when the rollout scope
  includes it, especially after schema, unit, credential or topology changes.
- Record the outcome and an appropriate later confirmation through existing
  morning triage. Retain the original owning thread and waiting start. Do not
  create a recurring per-upgrade LLM polling schedule. Settlement is separate
  from completion and remains Willy's decision unless explicitly authorized.

## 7. Mandatory cleanup and storage verification

Perform cleanup after successful, failed and cancelled test/rehearsal runs,
before handing back a preparation phase, and again after deployment verification:

1. Save sanitized test logs, failure diagnostics, hashes and the exact-source
   manifest outside disposable scratch. Identify any material still needed for
   a pending decision or rollback; record its owner, path and release condition.
2. Wait for or stop only the recorded test-owned children/process groups. Close
   their database handles and mounts. Do not kill generic Python/Node processes
   or live gateways. Hard-killed runners may leave scratch despite normal cleanup.
3. Inspect and remove only the exact run-owned temporary roots, copied test
   databases, disposable environments and build caches no longer needed. Verify
   ownership, resolved paths, mount boundaries and absence of active users first.
   <!-- no-tmp: ok — explicitly prohibits sweeping shared directories -->
   Never sweep all of `/tmp`, `/var/tmp`, shared caches or wildcard Hermes paths.
   Keep required deployment artifacts and rollback checkpoints until released.
4. Recheck bytes/inodes and confirm no run-owned children, mounts or scratch remain
   unexpectedly. Report cleanup failures and deliberately retained space; do not
   claim cleanup solely because the test runner exited successfully.

## Evidence required before handoff

Record in the task and PR: source/tree and companion pins; test/static/build/audit
receipts with qualifications; copied-state and recovery proof; artifact identity;
temporary-storage baseline/final usage and cleanup outcome; retained material;
and the next approval or external dependency with its owner and return mechanism.
For deployment also record the approved window, backup extraction, stopped-writer
checkpoint, live acceptance and retained confirmation. Secrets stay out of the PR.
