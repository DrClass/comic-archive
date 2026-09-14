# DECISIONS.md — Comic Archive Decisions

1. **Custom lightweight app instead of heavily customizing Komga/Kavita.** Irregular archive structures and the corrective importer/workspace are core requirements.

2. **Source archives are immutable.** Import/workspace never reorganizes original files; commit copies into managed storage.

3. **Managed storage is normalized and ID-based.** `library/series/<series-id>/groups/<group-id>/000001.ext` decouples logical metadata from unreliable source layout.

4. **Preserve original media formats, including safe PDF-contained page images.** JPG/JPEG/PNG/GIF/MP4 are copied. Simple image-only PDF pages directly preserve embedded JPEG/PNG data when safe; other PDF pages render to high-quality JPEG. General conversion is not desired. CBZ is not currently required.

5. **Extras exist at issue and series level.** Database ownership supports both.

6. **Missing issue numbers are valid.** Numeric gaps can be intentional and annotated.

7. **Direct issues and child sub-series may coexist under one series.** Never make these mutually exclusive.

8. **Virtual workspace is the authoritative correction layer.** Scanner suggestions are editable; filesystem structure is not treated as final truth.

9. **Synthetic logical nodes/groups are allowed without touching source.** Needed for archives whose physical hierarchy does not match desired library hierarchy.

10. **Scanner is one-pass/indexed.** Downstream workspace logic should not recursively rewalk source after scanning; repeated filesystem/path work caused severe CPU scaling problems.

11. **Immutable scan membership is separate from mutable workspace semantics.** Scanner-folder->media membership survives semantic cache invalidation.

12. **Automatic ownership is cached separately from explicit page targets.** Explicit moves override automatic ownership while original automatic owner remains cheaply available.

13. **Classification-only edits defer expensive ownership rebuilds.** Validation/staging forces authoritative ownership. This fixed large-comic click freezes.

14. **Workspace media view is paginated at 250 rows.** Prevents rendering thousands of pages into one response.

15. **Modern browser upload is raw streamed, not multipart.** Old multipart upload caused multi-GB memory growth. Current max file 16 GiB, 4 MiB write buffer, browser concurrency 6.

16. **Uploads are restart-safe.** Persist state and reconcile with completed files; `.part` means incomplete.

17. **Upload state is checkpointed periodically, not after every file.** Avoids repeatedly serializing a growing path list for thousands of files.

18. **Import/bulk sessions are recoverable for 12 hours.** Durable JSON state under staging; restore rescans source and reapplies logical edits.

19. **SQLite remains the database and is synchronous.** Simplicity fits the private single-instance app. Current import coordination assumes one Uvicorn process.

20. **Schema initialization belongs at app/CLI boundaries, not normal reads.** Request-time migration/backfill work previously caused overhead and database locks.

21. **Private/account-gated by default.** No signup; Argon2; CSRF; admin-only imports/edits/maintenance; session-version invalidation; secure-cookie option.

22. **Reading completion is based on primary issue pages.** Extras do not block completion; progress is per-user.

23. **Individual media removal is soft by default.** `active=0` is reversible.

24. **Destructive deletion is limited to managed library content.** Original source is never deleted.

25. **Duplicate detection uses SHA-256/fingerprints and commit staging IDs are idempotent.** Explicit override exists where appropriate.

26. **Thumbnail generation is best-effort.** A derivative failure must not fail a valid import.

27. **JPEG thumbnail optimization uses decoder downsampling, not parallel workers.** Profiling showed thumbnail CPU dominance; `Image.draft()` gave a large speedup without concurrent high-memory decodes. Parallel thumbnailing was deliberately deferred because of prior memory problems.

28. **Performance work must be measured.** Prefer removing asymptotic/repeated work and adding operation-count regressions over speculative micro-optimization.

29. **Application logs have a dedicated rotating file.** A shared application logger hierarchy captures scanner/PDF and importer diagnostics in `logs/comic-archive.log` beside the database by default. CLI options configure the file and level; errors still reach stderr/journald. File logging failures fall back to stderr. This replaces the earlier stderr-only diagnostics decision.

30. **Long background I/O uses a custom daemon-thread/future bridge where appropriate.** Long threadpool work previously contributed to pytest TestClient teardown hangs; short bounded upload writes may still use off-event-loop helpers.

31. **UI is functional/simple rather than design-heavy.** Responsiveness and archive workflows take priority.

32. **Structural refactor preceded algorithm changes.** Milestone 42 separated routes/services; Milestones 43+ optimized isolated services. Structural relocation is now complete.

33. **`web.py` remains an assembly layer.** Do not grow it back into a feature monolith.

34. **Preserve CLI usability.** Scanner/review/stage/commit/edit/user/thumbnail/history/server capabilities should not become unnecessarily HTTP-only.

35. **Tagging/downloads are later product work.** Do not destabilize current import validation for them.

36. **Next optimization is driven by the next large real import.** Observe upload/scan/workspace/validation/hash/copy/thumbnail/finalize timing before choosing another target.

37. **Series authorization covers all series POSTs.** The old predicate protected missing-issue edits but missed deletion. Central middleware now covers the series mutation family; GET browsing remains available to readers. Keep exact-title confirmation and CSRF as separate checks.

38. **Thumbnail failure isolation is centralized.** Catch ordinary generation exceptions in `create_thumbnail` so commit, on-demand access and rebuilding share behavior. Atomic sibling-file replacement prevents partial JPEGs being mistaken for existing derivatives. Do not broaden the commit transaction's exception suppression to hide copy/hash/database failures, or swallow process-control exceptions.

39. **Disk reconciliation replaces saved completion state.** Additive merging retained missing files and could falsely permit finalization. Assign only after successful enumeration and reject incomplete scans. Do not rescan on every upload/status request: that would reintroduce large-folder overhead. Live-session external deletion detection was deliberately left outside this fix.

40. **Windows portability fixes preserve production path semantics.** Use `.as_posix()` for path display assertions and native expected strings for legacy staged identifiers. Globally rewriting separators or changing scanner model types solely to satisfy tests was rejected because metadata/ownership keys and persisted records rely on existing representations.

41. **Logging is shared and bounded.** `logging_config.py` configures `comic_archive` and `uvicorn` at app/CLI boundaries, uses UTC timestamps, INFO default, 10 MiB rotation and five backups, and mirrors errors to stderr. File errors fall back to stderr. Configuration is process-wide; no multi-worker file-rotation guarantee is made. CLI output remains console output, and arbitrary third-party/root logging is not captured globally. The dedicated file simplifies sharing diagnostics without extracting journald records.

42. **Outstanding product choices are not settled decisions.** Full cancellation is requested, but bulk scope (current series, remaining batch, or both) is open. Server-filesystem import should be hidden or removed from the UI, but backend/CLI removal has not been approved. Manual read-status controls and an admin import cleanup panel are requested, not implemented. Keep the complete requested behavior in PROJECT_STATUS.md and TODO.md.

43. **A complete source handoff includes Markdown and uncommitted work.** Historical update ZIPs sometimes excluded Markdown; that convention does not apply to a standalone handoff. The working tree, not the old HEAD commit alone, is the current deliverable.
