# League Linux

A local desktop window, command-line launcher and manually started user service
for the macOS League of Legends client through Darling. The runtime comes from
[darling-lol](https://github.com/lucannez64/darling-lol), pinned in
`upstream.lock.json` to the `league-linux-integration` branch of
[a fork](https://github.com/EleChon-pixel/darling-lol/tree/league-linux-integration)
that adds build and signing fixes on top of upstream. This project manages that
runtime; it does not implement a new macOS compatibility layer.

**Experimental:** upstream reports suspensions for unsupported third-party tools.
A successful build or a running launcher does not demonstrate that Riot will
accept an online game session. No anti-cheat patches or login automation are
implemented. The runtime does present one consistent Mac identity to the client
(see [Machine identity](#machine-identity), including what it cannot hide).
Upstream's own runtime includes platform compatibility code; it has not been
comprehensively security-audited.
Not affiliated with or endorsed by Riot Games; see the [disclaimer](#disclaimer).

## Local launcher

Requires Python 3.10+, GTK 4 Python bindings for the window, and a systemd user
session. On Pop!_OS, the existing system Python supplies GTK.

```sh
python3 league_linux.py install
~/.local/bin/league-linux gui
```

The application menu has two entries: **League of Legends** starts the session
in one click, or brings the Riot Client back to the front if it is already
running. Closing the Riot Client's window keeps it running in the background
(on macOS it waits in the menu bar, which Darling lacks); the entry then
restarts the session. Right-click it for *Stop session* or the manager. **League
Linux** opens the manager window. Without a terminal, the play entry reports
start-up problems as desktop notifications. Installation does not start a
game or enable a service at login. Keep this checkout at its current path, since
the entry and service reference it.

## Build the compatibility runtime

The rootless build needs modern Nix (>=2.24), `uidmap` helpers, Bubblewrap,
subordinate UID/GID allocations, and unprivileged user namespaces. Runtime
libraries and compilers are supplied by the pinned Nix package rather than
installed into `/usr/local`. Initial build and game installation can consume
60 GiB or more. Builds default to two jobs to limit RAM use on this laptop.

Install Nix using its [official instructions](https://nixos.org/download/).
The reviewed bootstrap downloads can be found under `.cache/` when prepared
by the developer; they are not executed by installing this launcher.
The Ubuntu `nix-bin` 2.18 package is too old for this integration.
On Pop!_OS/Ubuntu the mapping helpers are provided by `sudo apt-get install uidmap`.
The doctor checks the prerequisites without starting Darling or Riot.

```sh
python3 league_linux.py doctor
sh scripts/fetch-upstream.sh
python3 league_linux.py prepare
```

`prepare` verifies the upstream commit and clean checkout, builds the locked Nix
environment, then compiles and validates Darling using upstream's `--update`
entry point. It does not launch the Riot installer, client, game or a login UI.
It downloads dependencies and invokes the pinned upstream build scripts.
Completed Nix downloads and compilation files are reused on a retry. Upstream
stages its initial source fetch in a temporary checkout, so a failed first fetch
may need to download those sources again. Re-run after fixing the reported error.
Do not run the launcher or build as root.

Runtime builds use a `022` umask because macOS `launchctl` refuses group-writable
`LaunchDaemons` directories. An older prefix created with a `002` umask may
need its affected directory permissions corrected before startup; see the
local status file for this host's repair and remaining setup dependencies.

If Nix repeatedly loses a large dependency download, the optional
`scripts/cache-dependency.py /nix/store/HASH-NAME` helper resumes that official
archive using system curl and imports it through Nix's normal signature and
content verification. Then rerun `prepare`. No trust settings are relaxed.

## Install the client and manage a session

Run the installation from an interactive terminal; upstream asks for your region
and displays Riot's UI. Sign-in and game installation are manual.

```sh
league-linux install-client
league-linux identity   # only for prefixes prepared before the identity layer
league-linux configure --region eune --gpu nvidia
league-linux start      # or: league-linux play (same, with desktop notifications)
league-linux status
league-linux logs --follow
league-linux stop
```

The installer runs in the foreground and exits after its clients are closed.
Subsequent `start` commands use `league-linux-session.service` in your systemd
user manager. Logs are in the user journal. A failed client exits the service;
it is not automatically restarted. Closing the manager window leaves a running
session active. Stop requests upstream's prefix-scoped shutdown, then systemd
collects any descendants remaining in this service's control group. Other games
and independent Darling installations are not targeted.

Settings (`region`, `gpu`, `jobs`) are stored in
`~/.local/share/league-linux/config.json`. An advanced `backend` setting accepts
an absolute path to another rootless Darling launcher, which is used by offline
integration tests. The default backend is the Nix package built by `prepare`.
Region defaults to EUNE; change it to your actual account region before launch.
GPU `auto` leaves selection to the driver; `nvidia` requests PRIME offload.
The `intel` selector currently targets this laptop's Intel 8086:46a6 GPU.

## Machine identity

`prepare` gives the prefix one consistent Mac identity, and `league-linux identity`
reapplies it to an existing prefix (stop the session first). Every guest process
sees a 2019 16-inch MacBook Pro (`MacBookPro16,1`, Intel Core i9-9980HK, Intel UHD
Graphics 630 + AMD Radeon Pro 5500M) on macOS Sonoma 14.7 (23H124), hostname
`MacBook-Pro.local`, through sysctl, IOKit, Metal, `system_profiler` and
`SystemVersion.plist`. The serial and hardware UUID are generated once per prefix
and kept. The guest home has its own `Desktop`, `Documents`, ... folders rather than
links into your Linux home, and the client starts from `/Users/Shared/Riot Games`,
not from a `/Volumes/SystemRoot/home/...` path. The guest environment is built from an
allowlist: desktop-session, shell and developer-tool variables stay on the host.
Applications also do not see `/proc`, `/Volumes/SystemRoot`, Linux mount points,
Linux-only device nodes (`/dev/dri`, `/dev/nvidia*`, `/dev/shm`, `/dev/input`, ...),
Darling's own files (`/usr/libexec/darling`, `/etc/darling-*`), Linux interface names
(they get `lo0`, `en0`, `utunN`) or the host-library variables in their environment.

`CPUID` instructions report the profile's CPU: its signature and brand, its last
basic leaf (0x16), only the leaf-7 features a Coffee Lake part has (no SHA, GFNI,
VAES, AVX-VNNI, PKU, ... from the newer host), and its cache sizes. The same values
come back from sysctl (`hw.l1icachesize` ... `hw.l3cachesize`, `hw.cachelinesize`,
`hw.tbfrequency`, `machdep.cpu.features`, `leaf7_features`, `extfeatures`), along
with `kern.uuid` (per macOS build) and `kern.bootsessionuuid` (per boot). The process
list (`proc_listallpids`, `KERN_PROC_ALL`) holds the session's macOS processes under
their own names, without Darling's helpers.

Only names and IDs are presented; the GPU that renders and the logical CPU count are
your real ones. If a game misbehaves, `league-linux identity --show-host` or
`league-linux identity --host-cpuid` turns those parts off (run `league-linux identity`
again to turn them back on). See upstream `docs/PLATFORM_IDENTITY.md` for
the details.

### What a guest can still tell

The identity covers names, IDs and what the runtime answers; it does not make the
machine a Mac. Measured with a probe that compares against a real MacBookPro16,1
(82 differences remain, down from 120), code inside the prefix can still notice:

- **The real CPU.** Instructions the i9-9980HK lacks (SHA, GFNI, VAES, VPCLMULQDQ,
  SERIALIZE, AVX-VNNI) still execute instead of faulting, `XCR0` enables PKRU state,
  and the TSC ticks at the host's rate. CPUID can only be masked, not the hardware.
- **Emulation cost.** A trapped `CPUID` takes ~3 µs and Mach IPC 30-70 µs, against
  well under 1 µs on a Mac.
- **A thin system.** The IOKit registry has about 10 entries (a Mac: thousands; no
  SMC, battery, NVMe, USB, Bluetooth, HID or framebuffers), there is no dyld shared
  cache (system libraries exist as files), no split System/Data volumes, no Finder,
  Dock, Safari or system fonts, and only the session's few processes run (no
  `kernel_task`, `WindowServer`, ...).
- **Account details.** The user ID is 1000 rather than 501, the login shell is bash,
  and `TMPDIR` is `/private/tmp`.

What the client actually sends can be checked with the audit below.

### Auditing what the client sends

`scripts/telemetry-audit.sh [seconds]` starts the session and records what the
Riot Client, the League client and the game send, until the given time or Ctrl+C,
through mitmproxy (`uv tool install --python 3.12 mitmproxy`).

The session runs in its own network namespace: `pasta` gives it IPv4, and all of its
TCP connections and DNS lookups go through a WireGuard tunnel into mitmproxy, so
clients that ignore proxy settings (login, entitlements, the platform services) are
recorded too. UDP other than DNS, which is the game's own traffic, goes out directly.
`pasta` and `wg` come from nixpkgs automatically; `nsenter`, `ip` and `nft` must be
installed. Runtimes older than this support only a proxy mode
(`LEAGUE_AUDIT_MODE=proxy`), which sees just the clients that honour `https_proxy`.

A dedicated CA is trusted in the guest only for the run (System keychain plus admin
trust settings, so both the Riot Client and Chromium accept it) and removed
afterwards. Hosts that refuse it are pinned: they are listed, and later connections
to them pass through untouched so login and chat keep working. Credentials, tokens and
account names are redacted before anything is written to
`~/.local/share/league-linux/audit/telemetry-*.jsonl`. The summary at the end lists
contacted hosts, endpoints, DNS lookups, non-HTTP connections (endpoints only), the
machine identity fields that were sent, any Linux/Darling/host-hardware telltales, and
connections that did not go through mitmproxy.

While it records, use the client itself (log in, start a practice game). Quitting
from the application menu stops the recording, and a session started from the menu
afterwards is not recorded. Vanguard's macOS gateway encrypts its payloads before TLS,
so its requests are listed but not readable.

## Scope of separation

The app uses a dedicated prefix and runtime workspace under
`~/.local/share/league-linux/`. This avoids mixing files with `~/.darling`.
**It is not a privacy sandbox:** upstream may expose host home files, display,
audio and network access. It is not a whole macOS VM. No Riot account credentials
are collected by this wrapper; Riot's client can persist its own login state in
the prefix. Runtime/client logs can contain personal data; don't share them
unreviewed.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 scripts/service-smoke.py
python3 scripts/runtime-smoke.py
```

The smoke check requires a live systemd user manager. It uses an explicitly fake
backend in a temporary prefix to verify start, status and shutdown and ensure an
unrelated process survives. It never launches Darling, Riot or League. These
checks verify the manager, not gameplay or anti-cheat acceptance.

After `prepare` completes, `runtime-smoke.py` checks the real compatibility
runtime by running macOS `uname` commands and requiring Darwin/x86_64 output.
It uses the normal operation lock and upstream's namespace-local shutdown.
It does not start Riot or League, and refuses an incomplete runtime.

The latest local results are in `docs/LOCAL-STATUS.md`.

## Remove the local integration

Stop the session, then remove the generated user service, the desktop entries
(`league-linux.desktop`, `league-linux-play.desktop`, `league-linux-quit.desktop`) and
`~/.local/bin/league-linux`, and run `systemctl --user daemon-reload`.
Runtime data and the checkout are intentionally preserved for manual removal.
Nix installation/removal is separate and follows its official documentation.

## Disclaimer

League Linux isn't endorsed by Riot Games and doesn't reflect the views or
opinions of Riot Games or anyone officially involved in producing or managing
Riot Games properties. Riot Games, League of Legends and all associated
properties are trademarks or registered trademarks of Riot Games, Inc.

This is an unofficial community project with no affiliation to Riot Games,
Apple or the Darling project. It does not include, modify or redistribute any
Riot software: the Riot client is downloaded from Riot by its own installer, and
you log in through Riot's own window.

Running League of Legends through a compatibility layer is not supported by
Riot. Riot may treat it as a third-party tool, and upstream reports account
suspensions. Before using it, read Riot's
[Terms of Service](https://www.riotgames.com/en/terms-of-service). You use this
software entirely at your own risk; the authors are not responsible for account
actions, bans, data loss or any other consequence of using it.

## License

The launcher in this repository is released under the [MIT License](LICENSE).
The darling-lol runtime it builds is a separate project, fetched into
`upstream/` and not part of this repository. It keeps its own licenses
(GPL-3.0 for its integration tooling; Darling and other upstream components
under their existing licenses).
