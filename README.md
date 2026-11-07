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
accept an online game session. No anti-cheat patches, identity masking or login
automation are implemented in this wrapper. Upstream's own runtime includes
platform compatibility code; it has not been comprehensively security-audited.
Not affiliated with or endorsed by Riot Games; see the [disclaimer](#disclaimer).

## Local launcher

Requires Python 3.10+, GTK 4 Python bindings for the window, and a systemd user
session. On Pop!_OS, the existing system Python supplies GTK.

```sh
python3 league_linux.py install
~/.local/bin/league-linux gui
```

The application menu has two entries: **League of Legends** starts the session
in one click (right-click it for *Stop session* or the manager), and **League
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

Stop the session, then remove the generated user service, both desktop entries
(`league-linux.desktop`, `league-linux-play.desktop`) and
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
