# Local status — 2026-11-06

Project: `~/Development/repos/league-linux` on the development machine.

This is the local launcher for the macOS League client through Darling.
The runtime source is pinned to `0b74916` on the `league-linux-integration` branch
of https://github.com/EleChon-pixel/darling-lol (upstream `cf1fc55` plus the fixes
below).

## 2026-11-06: integration branch and signing

- Installed `rcodesign` 0.29.0 (official release, checksum verified) in
  `~/.local/bin`; `dc-codesign` now builds inside the Nix environment.
- Committed the two local upstream branches (`fix/sip-mutation-gaps`,
  `improve/qualification-and-client-fixes`) and merged them into
  `league-linux-integration`, checked out in `upstream/darling-lol` and pushed to
  the EleChon-pixel/darling-lol fork.
- Fixes made during the rebuild: preflight accepts giflib by header and library
  (nixpkgs ships no `libgif.pc`), the flake adds `openssl.dev`, and
  `install-runtime.sh` passes OpenSSL paths to Nix's wrapped clang.
- Cleared the remaining warnings: the flake now builds with LLVM 22 (the version
  Iridium/Indium are tested with; headers and libLLVM-22 match) and ships
  `xdpyinfo` and `xdg-user-dirs`; `build.sh` passes `LLVM_DIR` from the active
  `llvm-config` so a cached LLVM 21 path cannot break configuration.
  Preflight: 13 checks passed, no warnings. Upstream tests: 545 passed, 1 skipped.
- `python3 league_linux.py prepare` passed. The installer signed 1,307 runtime
  files (SDK and prefix overlays, 0 failures) and synced the trust store.
  Evidence: `verification/prepare-integration.log`.
- `python3 scripts/runtime-smoke.py` passed (Darwin/x86_64).
  Evidence: `verification/runtime-smoke-integration.log`.
- Removed the unused `~/.darling` experiment prefix and the obsolete pre-signing
  backups.

## Continue

Next is the interactive client install, run from a terminal:
`python3 league_linux.py install-client`. Riot shows its own login; no account
automation is provided. Gameplay and online sessions remain untested.

## Earlier notes (2026-11-05)

### Recovery and verification

- Recovered the earlier session and existing build files. Its last log stopped
  at 15% without a recorded compiler failure.
- Resumed with `python3 league_linux.py prepare`; the existing Nix package and
  compilation outputs are reused. The build uses two jobs.
- All 12 manager unit tests passed, including an actual child-process directory
  creation check under an inherited `0002` umask.
- The live user-service smoke check passed with a fake backend: start, display
  environment delivery, clean stop, and preservation of an unrelated process.
  Evidence: `verification/service-smoke.log`.
- Added `scripts/runtime-smoke.py` to check real Darwin/x86_64 command output
  and use upstream's namespace-local shutdown. Syntax validation passed; its
  operation lock correctly refuses to overlap the running build.

### Startup correction

The base runtime compiled and installed completely. Its first startup failed
because `System/Library/LaunchDaemons` was group-writable (`0775`). The loader's
permission check refuses that mode, so it never loaded shellspawn. Correcting
the directory to `0755` in both the installed base and its existing prefix copy
allowed the macOS `/usr/bin/true` command to execute. The installer then built
and installed the guest root certificate store.

The wrapper now uses a child-process `022` umask for runtime builds, so fresh
installation directories do not inherit this desktop's group-writable default.
The parent's umask is preserved. This does not automatically repair old prefixes.

### Runtime result

The additional runtime libraries installed and upstream readiness validation
passed. `python3 scripts/runtime-smoke.py` passed with actual guest output
`LEAGUE_LINUX_GUEST=Darwin` and `LEAGUE_LINUX_ARCH=x86_64`; namespace-local
shutdown returned zero. Evidence: `verification/runtime-smoke.log`.
Installation output is in `verification/runtime-install-retry.log`. The completed
base build is in `verification/build-resume.log`; prior output remains in
`verification/build-runtime.log`.
No Riot client is installed in the dedicated prefix, and no gameplay or online
session acceptance has been tested.

The installer reported a missing `tools/dc-codesign/dc-codesign` executable.
Its signing script also requires `rcodesign` from apple-codesign. Runtime
readiness does not check these dependencies, so its passed gate does not prove
that hardened client images will load. Resolve signing dependencies before
treating client installation as ready. The runtime probe needs neither tool.

Nonfatal bootstrap diagnostics also report missing `xdg-user-dir`. Preflight
warns about LLVM 21 (upstream tests overlays with 22/23) and missing `xdpyinfo`.
Hardware graphics presentation has not yet been verified.

### Next steps as of 2026-11-05 (since done)

No build or diagnostic session is intentionally left running. Rebuild with
`python3 league_linux.py prepare` if necessary, then rerun
`python3 scripts/runtime-smoke.py`. The signing dependencies above remain the
next setup task; the upstream checkout was confirmed equal to remote HEAD at
the revision listed above during this session.

Client installation is a later interactive step: `league-linux install-client`.
