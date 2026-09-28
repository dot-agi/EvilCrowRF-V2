
  

# EvilCrow SDR — Quick Choices ⚡

**macOS:** See [the macOS guide](../docs/macos.md) for the Flutter controller and USB backend,
USB diagnostics, and the firmware fix required for SDR mode.

![screenshot](https://github.com/Senape3000/EvilCrowRF-V2/blob/main/docs/media/img/sdr_launcher.PNG?raw=true)
  ***

If you don't trust using a precompiled executable from some random person on the internet (and who could blame you? 😅), no worries — compile it yourself! Or be brave and use the ready-made EXE.

  

***

  

## Option A — Build locally (for the skeptics) 🛠️

  

Short steps (Windows / cross-platform):

  

1. Open a terminal in this folder (`SDR`).

2. (Recommended) Create \& activate a virtualenv:

```bash

python  -m  venv  .venv

# Windows

.venv\Scripts\activate

# Linux / macOS

source  .venv/bin/activate

```

  

3. Install runtime deps (optional; the builder can auto-install PyInstaller):

```bash

pip  install  -r  requirements.txt

```

  

4. Run the builder GUI and follow prompts (it can auto-install PyInstaller and optionally download UPX):

```bash

python  build_exe.py

# or use the shortcut on Windows

build_exe.bat

```

  

5. The GUI will:

  

- Let you bump/sync versions (`sdr_launcher.py` + modules).

- Offer to install PyInstaller if missing.

- Offer to download UPX (optional compression).

- Produce the final executable in `dist/`.


  
  

***

  

## Option B — Use the prebuilt EXE (for the daring) 🧾

  

- [**Check the latest release of sdr_launcher.exe**](https://github.com/Senape3000/EvilCrowRF-V2/releases/)

  

***

  

## Safety notes 🛡️

  

- If you *don't* trust the prebuilt EXE, use Option A and compile the code yourself.

- If you decide to build locally, inspect the source code and preferably use a clean virtualenv.

- The builder can auto-install tools, but it will always ask for confirmation before installing.

  


***
