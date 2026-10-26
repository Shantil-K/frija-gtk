# frija-gtk

A GTK 3 window for **[frija-cli](https://github.com/Shantil-K/frija-cli)**, the Samsung firmware downloader. This repo has only
the interface. Looking up, downloading, verifying and decrypting firmware is all done
by the `frija` package from frija-cli.

It has the same **Auto / Manual / Legacy** tabs as the original Windows Frija:

- **Auto**: detects a Samsung phone plugged in over USB and reads its model, CSC and serial.
- **Manual**: enter the model, CSC and IMEI or serial number.
- **Legacy**: enter the model, CSC and a specific firmware version.

Use the ☰ menu for settings (CRC check, decrypt, keep encrypted file, factory firmware). **Stop** pauses a download, and **Download** resumes it. Fields and
settings are saved to `~/.config/frija/settings.json`.

This front end is aimed at Linux desktops. On Windows, use frija-cli directly. GTK does
run on Windows through MSYS2, but that setup is untested.

## Requirements

- **frija-cli** (and its `requests` / `cryptography` dependencies)
- **GTK 3 Python bindings (PyGObject)**. Install them from your distro, not pip:

  | Distro | Package |
  |---|---|
  | Debian / Ubuntu / Mint | `sudo apt install python3-gi gir1.2-gtk-3.0` |
  | Fedora | `sudo dnf install python3-gobject gtk3` |
  | Arch / Manjaro | `sudo pacman -S python-gobject gtk3` |
  | openSUSE | `sudo zypper install python3-gobject-Gdk typelib-1_0-Gtk-3_0` |

  These usually come with the system Python. pyenv and conda Pythons usually don't have
  them.

## Running

**Without installing.** Clone both repos side by side:

```sh
git clone https://github.com/Shantil-K/frija-cli.git
git clone https://github.com/Shantil-K/frija-gtk.git
```


```
somewhere/
├── frija-cli/
└── frija-gtk/
```

```sh
./frija-gtk/frija-gui.sh
./frija-gtk/install-desktop-entry.sh    # optional: add "Frija" to your app menu
```

`frija-gui.sh` finds `../frija-cli` automatically (or an installed frija-cli) and picks a
Python that has GTK.

**Installed.** This also installs frija-cli:

```sh
pip install --user git+https://github.com/Shantil-K/frija-gtk.git
frija-gtk
```

If you install into a virtualenv, create it with `--system-site-packages` so it can see
the distro's PyGObject.

## USB (Auto tab)

See frija-cli's README. In short: be in the `dialout` group, and stop ModemManager
(`sudo systemctl stop ModemManager`) if the phone isn't detected.

## Credits

- **[Frija](https://github.com/SlackingVeteran/frija)** by wssyncmldm / SlackingVeteran:
  the original Windows tool whose interface this app follows.
- **[samloader](https://github.com/nlscc/samloader)** (GPL-3.0): an earlier open-source
  implementation of Samsung's firmware server protocol.

## Disclaimer

This project is not affiliated with, endorsed by or connected to Samsung, or to the
original Frija developers. "Samsung" and "Galaxy" are trademarks of Samsung Electronics.

It only downloads firmware that Samsung's own servers provide for the device details
you enter. Samsung can change those servers at any time, and this tool may stop working.
Use it at your own risk and in line with the laws and terms that apply to you. As the
licence says, it comes with no warranty.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
