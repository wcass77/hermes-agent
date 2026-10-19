---
title: Ordinary output policy
---

`security.ordinary_output` controls ordinary tool output independently of the
existing diagnostic redactor. The default, `redact`, preserves prior behavior.
The opt-in `fidelity` policy preserves credential-shaped fixtures and source text
in ordinary terminal, file-read and search results:

```yaml
security:
  redact_secrets: true
  ordinary_output: fidelity
```

The choice is cached per profile when first used. Restart the owning process to
apply a change. A shell environment variable cannot change it mid-conversation.
Neither changing this setting nor exporting a session rewrites historical replay
or the cached conversation prefix.

Fidelity retains exact registered vault-value suppression, secret-file and
environment-dump protection, and targeted exception-diagnostic guards. Browser
private-input boundaries, approval previews, authentication errors and outbound
human messages keep their dedicated protections. Newly written memory and
log/export derivatives are sanitized independently.

Foreground results, background poll/wait/log results, model completion
notifications and full-output spill files use the ordinary policy. Existing
capture limits, ANSI stripping, truncation markers, private permissions and
sandbox publication/readback checks still apply. Masked file reads retain their
write-back coverage guard.

Completed background results in fidelity mode have one private recovery record
under the profile's `state/process-results/` (directory 0700, record 0600).
`logs/process-results/` holds a separately sanitized diagnostic derivative.
Recovery only admits the owning conversation or its compression lineage. It
never substitutes the diagnostic copy for missing private recovery. Records
retain the existing seven-day / 64-result bound. Plugin output transformations
are applied before protection and are not reapplied to recovered output.

SessionDB and private spill/recovery files can consequently contain sensitive
ordinary text. They are private reasoning state. CLI `sessions export`, `/save`,
TUI/Desktop saves and dashboard exports create sanitized derivatives by default,
including metadata and tool arguments. Export labels identify `sanitized` or
explicit `raw` output. `--no-redact` is available for explicitly requested raw
CLI exports or `/save`; review such files before sharing. Pattern sanitization
is best effort, not anonymization.

To roll back the policy, set `ordinary_output: redact` and restart the owning
process. This changes future output; it does not erase prior private state.
Live activation and service restarts are separate operational steps from source
integration and testing.
