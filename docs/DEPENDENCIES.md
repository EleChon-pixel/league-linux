# Runtime dependencies and local installation

The manager uses the system Python/GTK 4 bindings. The compatibility runtime
uses `darling-lol` at the revision in `upstream.lock.json`. Its own `flake.lock`
pins Nixpkgs and its manifest pins the Darwin source components and patches.
Neither `prepare` nor normal launches update those pins automatically.

For this Pop!_OS host:

- The user installed Ubuntu `uidmap` and `libsubid4` to supply subordinate-ID
  mapping helpers. Existing `/etc/subuid` and `/etc/subgid` allocations were used.
- Nix 2.35.4 was installed in single-user mode into a user-owned `/nix` directory.
  The official installer used `--no-daemon --no-modify-profile --no-channel-add`.
  No Nix system daemon or shell startup modification was requested.
- Official archive:
  `https://releases.nixos.org/nix/nix-2.35.4/nix-2.35.4-x86_64-linux.tar.xz`
- Verified SHA-256:
  `ff66236d2f7e2a21f3350e73668e021ac6a8881ef277a38e16e952bfb22718c3`
- The exact rootless namespace probe passed:
  `unshare --user --map-auto --map-current-user --setuid=0 --setgid=0 true`.
- Both the Intel Iris Xe and NVIDIA RTX 3050 Laptop GPU report Vulkan 1.4.
  This is host capability evidence, not a successful Darling render test.

The wrapper locates `~/.nix-profile/bin/nix` directly; editing `.profile` is not
needed. Its per-app runtime package symlink roots the Nix environment in the
store. Do not remove this or upstream's internal build roots while using the
runtime. Download/build data is intentionally preserved if compilation fails.

This integration has a separate prefix and workspace, but upstream's namespaces
are not a privacy sandbox for the host home directory. See the README scope.
