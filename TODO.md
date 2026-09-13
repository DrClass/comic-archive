# Comic Archive TODO

All discrepancies from the Milestone 45 handoff source review have been resolved
and covered by regression tests.

## Requested improvements and fixes

- [ ] **Cancel an upload or import with confirmation and immediate cleanup.**
  Stop active work and clean up remaining temporary upload/staging/PDF-cache
  files after confirmation, while preserving original source files and already
  committed library content. Coordinate cleanup with active workers so they
  cannot recreate files after cancellation. Review the existing upload-cancel
  behavior and extend it to the full import workflow.
  **Open decision:** for bulk imports, cancel only the current series, all
  remaining series, or offer both choices.

- [ ] **Allow users to manually set reading status.**
  Add a direct Mark read action for comics read elsewhere. Issue-level Mark
  unread already exists for read/in-progress issues via `/progress/{issue_id}/reset`;
  review discoverability and desired whole-series scope rather than duplicating it. Keep status per-user and maintain consistent
  issue/series aggregates. Define the controls and scope for issues versus
  whole series during implementation.

- [ ] **Remove or hide server-side filesystem import from the web UI.**
  Make browser uploads the visible import workflow. Decide whether to hide the
  existing server-folder controls or remove their web routes as well; retain
  existing functionality until that scope is agreed.

- [ ] **Add an admin panel for active imports and forced expiration/cleanup.**
  Show in-progress imports and allow an administrator to force an import to
  expire and clean up its temporary files regardless of current status.
  Coordinate with active workers and preserve original sources and committed
  content. Use administrator enforcement and CSRF protection.

- [ ] **Fix the validation button remaining locked after an error.**
  Clicking "Validate and continue to confirmation" while an error exists can
  leave the button disabled until the page is refreshed. Reproduce the issue,
  restore the button after unsuccessful validation, and keep workspace edits
  intact so users can correct the error and retry without refreshing.

The recommended next development task is the validation-button lockup above.
PROJECT_STATUS.md records the full handoff and implementation starting points.

## Next performance task

The next performance task remains a large real import with phase timing and
memory capture. Collect the configured application log and relevant rotated
backups; choose optimizations from that evidence.
