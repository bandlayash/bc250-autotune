---
name: release
description: Cut a new release of bc250-autotune — decide the version bump, update mcp_server/pyproject.toml, tag it, and push so the release.yml GitHub Actions workflow publishes the GitHub Release with auto-generated notes. Use when the user asks to "cut a release", "make a release", "bump the version", "tag a release", or invokes /release.
---

# Cutting a release

This repo has no package registry to publish to — a release here just means:
a version bump, a `vX.Y.Z` git tag on `main`, and a GitHub Release with notes.
Pushing the tag is what triggers `.github/workflows/release.yml`, which
creates the GitHub Release automatically — you never create the Release by
hand.

The single source of truth for the version is `version` in
`mcp_server/pyproject.toml`. The release workflow refuses to run if the tag
doesn't match it, so keep them in sync.

## Steps

1. **Preconditions.** Confirm the working tree is clean, you're on `main`,
   and `main` is up to date with `origin/main` and green on CI (check the
   latest run for the current `main` HEAD). Never tag a commit that hasn't
   been through CI on `main`.

2. **Survey what changed.** Find the last tag (`git describe --tags
   --abbrev=0`) and list commits since then (`git log <last-tag>..HEAD
   --oneline`). Read the actual diffs for anything touching:
   - `mcp_server/bc250_mcp/safety_envelope.yaml` or other thermal/voltage
     safety thresholds
   - governor backend selection or control logic
   - benchmark methodology (what "stock" vs "tuned" means, the cooldown
     gate, abort thresholds)

   Call these out explicitly in your summary to the user — safety-relevant
   behavior changes matter more here than most changelog entries, since
   people run this against real hardware.

3. **Pick the version bump** and confirm it with the user before proceeding:
   - **patch** — bug fixes, doc/README clarifications, no behavior change
   - **minor** — new features, new governor/hardware support, backward-compatible
   - **major** — breaking config/CLI changes, or a safety-relevant default
     changes in a way existing users should notice before upgrading

   If it's ambiguous from the commit log, ask rather than guess.

4. **Bump the version.** Edit `version` in `mcp_server/pyproject.toml` to the
   new `X.Y.Z` (no `v` prefix in the file). Commit it through the repo's
   normal flow — a branch + PR if that's how this repo takes changes,
   or directly if the user says to:

   ```
   git commit -m "Bump version to vX.Y.Z"
   ```

5. **Confirm before tagging and pushing** — tagging and pushing a release is
   externally visible and hard to fully undo (others may already have
   fetched the tag by the time you'd delete it). Show the user the version,
   the safety-relevant changes you found in step 2, and get an explicit go
   before running:

   ```
   git tag vX.Y.Z
   git push origin main
   git push origin vX.Y.Z
   ```

6. **Report back** the tag pushed and that `release.yml` will publish the
   GitHub Release shortly; check the Actions run and hand the user the
   Release URL once it completes. Don't draft release notes yourself — the
   workflow auto-generates them from merged PRs / commits, per GitHub's
   standard release-notes generation.

## What NOT to do

- Don't create the GitHub Release by hand (via API or `gh release create`)
  — that's the workflow's job; doing it manually risks a mismatched or
  duplicate release.
- Don't bump the version without telling the user what safety-relevant
  changes (if any) landed since the last release.
- Don't tag a commit that isn't on `main` and isn't green on CI.
- Don't invent a version number without checking `mcp_server/pyproject.toml`
  and the latest tag first — they should already agree on the current
  released version.
