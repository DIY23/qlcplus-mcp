# QLC+ on macOS — things that cost us a day

Notes collected while building and driving the `agent-api` branch on macOS (x86_64, 15.8).
Nothing here is project-specific; all of it was measured against QLC+ 5.2.2.

## Running a build-tree binary
A binary straight out of the build tree sees **no resources and no I/O plugins**, because both are
resolved relative to the *executable*: resources at `<exe dir>/../Resources/<Name>`, plugins from
`<exe dir>/../PlugIns`. `tests/make_dev_bundle.sh` assembles a runnable bundle:

    Contents/MacOS/qlcplus-qml      the binary
    Contents/PlugIns/*.dylib        every I/O plugin (the loader does NOT recurse)
    Contents/Resources/<Name>       symlinks to the source tree's resources
    Contents/Resources/qlcplus.icns + a real CFBundleShortVersionString

Launch it through LaunchServices, not by exec'ing the inner binary:

    open -na /tmp/QLC+Agent.app --args -w --web-port 9977 -o /path/show.qxw

Exec'ing `Contents/MacOS/qlcplus-qml` directly works, but macOS then treats it as a bare
executable — no app identity, no Dock icon — and if the bundle's `Info.plist` still points at
`qlcplus.icns` that was never copied in, there is no icon to show either.

Launch flags: `-w` (web access), `--web-port <n>` — **long form only**, `-wp <n>` is parsed as `-w`
plus an unknown `-p` and the app refuses to start. `-o <file.qxw>` opens a project.

## User fixture profiles are FLAT in their folder
`QLCFixtureDefCache::load()` reads files **directly** in the user fixture directory
(`~/Library/Application Support/QLC+/Fixtures/`) — no recursion. A per-manufacturer subfolder is
ignored, with only a `Unrecognized fixture extension` warning. Generated profiles must be written
flat as `<Manufacturer>-<Model>.qxf`. The cache is read at startup, so a new profile needs an app
restart before `addFixture` can use it.

## Art-Net: output line 0 is loopback on macOS
The Art-Net plugin enumerates one output line per IPv4 address, sorted, so **line 0 is
`127.0.0.1`** and line 1 is the real NIC (e.g. `192.168.0.108`, broadcast `192.168.0.255`).
Patching a universe to line 0 "succeeds" and opens output on loopback — DMX then goes nowhere
useful and a consumer on the same machine may ignore it. Always confirm in the log:

    [ArtNet] Open output on address : "192.168.0.108"

## Generated fixture profiles (tools/generate_qxf.py)
Converts a channel-map JSON (one entry per manufacturer/model/mode with an ORDERED channel list)
into `.qxf` files. It writes channel **names, order and count** and infers channel *groups* from
the names. It deliberately does **not** invent capabilities or presets: a wrong preset silently
changes how QLC+ interprets a channel. Consequences to expect: dropdowns show raw DMX values
instead of named gobo/colour slots, and the `Type` element is guessed from the model name (cosmetic
— it affects the icon and library filters only).
