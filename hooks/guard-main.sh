#!/usr/bin/env bash
# Refuse a push that moves main, unless it comes from the integrator.
#
# Several agents now work on this code - Claude Code sessions and Codex - each
# on its own branch. main is what both machines pull and what deploy.ps1 ships,
# so one integrator merges into it and deploys. This makes that the default
# rather than a convention: an agent that pushes main by habit is stopped here,
# before the suite runs, instead of shipping to two machines.
#
# Reads git's pre-push input, one line per ref being pushed:
#   <local ref> <local sha> <remote ref> <remote sha>
#
# The integrator sets PALETTE_INTEGRATOR=1; deploy.ps1 does it for its own push.
# `git push --no-verify` skips every hook, this one included - for emergencies.

while read -r local_ref local_sha remote_ref remote_sha; do
  if [ "$remote_ref" = "refs/heads/main" ] && [ "${PALETTE_INTEGRATOR:-}" != "1" ]; then
    echo "pre-push: refusing to push main - only the integrator moves it." >&2
    echo "          Push a branch instead (git push -u origin <role>/<subject>)" >&2
    echo "          and hand it over. Integrators: set PALETTE_INTEGRATOR=1." >&2
    exit 1
  fi
done
exit 0
