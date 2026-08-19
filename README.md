# KiCad JLCPCB Importer

<!-- markdownlint-disable MD013 -->

Thanks and credit to the original project that inspired this work:
[Bouni/bouni-kicad-repository](https://github.com/Bouni/bouni-kicad-repository)

- Supported platforms: Linux, macOS, and Windows
- Supported KiCad versions: KiCad 10 and newer
- Integration: KiCad IPC API (`kipy`), running out of process; the legacy
  in-process `pcbnew.ActionPlugin` entrypoint is not used.
- Python-level CI runs on all three platforms. KiCad GUI/runtime behavior is
  validated separately because KiCad is not installed in CI.

## What it does

- Search the LCSC/JLCPCB catalog
- Import symbols, footprints, and 3D models via the built-in EasyEDA Pro backend
- In `KiCad` output mode: try matching built-in KiCad symbols/footprints first (common R/C/L/diode/transistor/IC), then fallback to EasyEDA conversion
- Choose where to store generated libraries: Project or System (KiCad 10 3rd‑party locations)
- Auto‑update project library tables (sym‑lib‑table / fp‑lib‑table) and fix 3D paths
- Configurable library prefix (empty by default) and project library folder (default: `libraries`)

### KiCad-first mapping settings

The default output mode is `general.lib_format = "kicad"`:

- `general.kicad_builtin_first` (default `true`) enables built-in KiCad lookup before EasyEDA fallback.
- Symbol index cache is persisted in the directory assigned by KiCad's IPC API with TTL.
- `general.kicad_symbol_index_ttl_sec` (default `86400`) controls symbol index cache lifetime.
- `general.kicad_symbol_index_max_libs` limits number of symbol libraries used for index building.
- `general.kicad_footprint_fuzzy_max_libs` / `general.kicad_footprint_fuzzy_max_files_per_lib` limit fuzzy footprint scanning.
- `general.kicad_symbol_map` maps logical component kinds (`resistor`, `capacitor`, `bjt`, `ic`, ...) to preferred symbol refs like `Device:R` or `Transistor_BJT:Q_NPN_BCE`.
- `general.kicad_footprint_map` can pin preferred footprint refs or libraries (supports `${package}` placeholder), for example:
  - `["Resistor_SMD:R_${package}_*"]`
  - `["Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"]`

## Requirements and limitations

- Enable the IPC API in KiCad under `Preferences → Preferences → Plugins`.
- KiCad's Plugin and Content Manager creates the plugin environment and installs
  `requirements.txt` before launch. This includes `kicad-python`, wxPython, and
  OpenCascade bindings used for STEP normalization.
- KiCad currently does not provide an IPC API to force refresh or reload library tables (symbols/footprints) at runtime.
- After importing a component (and updating `sym-lib-table` / `fp-lib-table` on disk), restart KiCad or reopen the project to see newly added libraries in browsers and pickers.
- In System storage mode, KiCad auto‑scans configured 3rd‑party folders and applies your configured nickname prefix, but visibility in the current session may still require a restart.

## Known issues

- Footprints may not appear in the library browsers due to KiCad's `fp-info-cache`.
  - Workaround: after import, close KiCad, delete `fp-info-cache` in the project folder, then start KiCad again.

Screenshots

- Search: `images/search.png`

![Search](images/search.png)

- Part details: `images/details.png`

![Part details](images/details.png)

- Settings: `images/settings.png`

![Settings](images/settings.png)

- Symbol library: `images/symbol_library.png`

![Symbol library](images/symbol_library.png)

- Symbol properties: `images/symbol_properties.png`

![Symbol properties](images/symbol_properties.png)

## Installation 💾

### KiCAD PCM

Add my custom repo to *the Plugin and Content Manager*, the URL is:

```sh
https://raw.githubusercontent.com/NikolayMurha/kicad-repository/main/repository.json
```

![PCM Repository](images/pcm_repository.png)

From there you can install the plugin via the GUI.

### Git (development)

Clone this repository into KiCad 10's IPC plugin folder. Unlike old Action
Plugins, it must not be installed under `scripting/plugins`.

#### Linux

```sh
cd /home/{username}/.local/share/kicad/10.0/plugins
git clone https://github.com/NikolayMurha/kicad-jlcpcb-importer.git
```

#### macOS

```sh
cd ~/Documents/KiCad/10.0/plugins
git clone https://github.com/NikolayMurha/kicad-jlcpcb-importer.git
```

#### Windows (PowerShell)

```powershell
cd "$env:APPDATA\kicad\10.0\plugins"
git clone https://github.com/NikolayMurha/kicad-jlcpcb-importer.git
```

You may need to create the `plugins` folder if it does not exist. Restart KiCad
after cloning so it discovers `plugin.json` and creates the managed Python
environment.

## Usage 🥳

To access the plugin choose `Tools → External Plugins → JLCPCB Importer` from the *PCB Editor* menus

Checkout this screencast, it shows quickly how to use this plugin:

> Note: usage instructions are intentionally omitted for now.

## System library locations (KiCad 10)

When you choose System as the storage location, generated libraries are placed under KiCad’s 3rd‑party folders.

- macOS
  - `/Users/{user}/Documents/KiCad/10.0/3rdparty/symbols/{plugin_dir_name}`
  - `/Users/{user}/Documents/KiCad/10.0/3rdparty/footprints/{plugin_dir_name}`
  - `/Users/{user}/Documents/KiCad/10.0/3rdparty/3dmodels/{plugin_dir_name}`

- Linux
  - `~/.local/share/kicad/10.0/3rdparty/symbols/{plugin_dir_name}`
  - `~/.local/share/kicad/10.0/3rdparty/footprints/{plugin_dir_name}`
  - `~/.local/share/kicad/10.0/3rdparty/3dmodels/{plugin_dir_name}`

- Windows
  - `%APPDATA%\kicad\10.0\3rdparty\symbols\{plugin_dir_name}`
  - `%APPDATA%\kicad\10.0\3rdparty\footprints\{plugin_dir_name}`
  - `%APPDATA%\kicad\10.0\3rdparty\3dmodels\{plugin_dir_name}`

## Icons

This plugin makes use of a lot of icons from the excellent [Material Design Icons](https://materialdesignicons.com/)

## Development

1. Fork repo
2. Git clone forked repo
3. Install pre-commit `pip install pre-commit`
4. Setup pre-commit `pre-commit run`
5. Create feature branch `git switch -c my-awesome-feature`
6. Make your changes
7. Commit your changes `git commit -m "Awesome new feature"`
8. Push to GitHub `git push`
9. Create PR

Make sure you make use of pre-commit hooks in order to format everything nicely with `black`
In the near future I'll add `ruff` / `pylint` and possibly other pre-commit-hooks that enforce nice and clean code style.

## Release versioning

Plugin releases use calendar versions in the exact form `YYYY.MM.DD`, without a
`v` prefix. Only one release is created per calendar date.

Release procedure:

1. Set the root `VERSION` file to the release date.
2. Merge the release commit into `main` and wait for CI to pass.
3. Create a tag with the same value as `VERSION`, pointing at that exact commit.
4. Publish a GitHub Release for the tag.

The PCM workflow checks out the tag rather than a branch, validates the calendar
date, verifies that `VERSION` matches the tag, and then publishes
`KiCAD-PCM-YYYY.MM.DD.zip`. A manual PCM workflow run also requires an existing
release tag and packages that tag only.

## Notes

- This fork focuses on searching LCSC parts and managing KiCad library assignments with a bundled EasyEDA Pro importer.
- Fabrication (Gerber/CPL/BOM) features from the original tool are not included.

## Python dependencies

Runtime packages are declared in `requirements.txt`. KiCad 10 installs them into
an isolated environment for this IPC plugin. The repository no longer vendors
or updates packages in a plugin-local `lib/` directory at runtime.

New database and symbol-index caches use the persistent plugin directory
provided by KiCad's IPC API. An existing `<plugin>/jlcpcb` database is reused in
place so upgrades do not duplicate a multi-gigabyte cache.

## Standalone mode

Allows the plugin UI to be started without KiCAD, enabling debugging with an IDE like pycharm / vscode.

Standalone mode is under development.

### Standalone limitations

- All board / footprint / value data are hardcoded stubs, see standalone_impl.py

### How to use

To use the plugin in standlone mode you'll need to identify three pieces of information specific to your Kicad version, plugin path, and OS.

#### Python

The `{KiCad python}` should be used, this can be found at different locations depending on your system:

- macOS: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3`
- Linux: `/usr/bin/python3`
- Windows: `C:\Program Files\KiCad\{version}\bin\python.exe`

#### Command line

- Change to the repository root.
- Run `__main__.py` with a Python environment containing wxPython and the other
  packages from `requirements.txt`.

For example:

```sh
cd {kicad-jlcpcb-importer repository}
{kicad_python} __main__.py
```

For example on Mac:

```sh
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3 __main__.py
```

For example on Linux:

```sh
python3 __main__.py
```

For example on Windows PowerShell:

```powershell
& "C:\Program Files\KiCad\10.0\bin\python.exe" __main__.py
```

#### IDE

- Configure the command line as `{kicad_python} __main__.py`.
- Set the working directory to the repository root.

If using PyCharm or JetBrains IDEs, set the interpreter to KiCad's python, `{Kicad python}` and under 'run configuration' select Python.

Select `__main__.py` as the script path.
