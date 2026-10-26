#!/usr/bin/env bash
# Open the Frija window without installing anything.
# Needs:  - frija-cli, either installed or checked out next to this folder (../frija-cli)
#         - a Python with GTK 3 bindings (PyGObject); the system Python usually has them
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PP="$DIR"
[ -d "$DIR/../frija-cli/frija" ] && PP="$PP:$(cd "$DIR/../frija-cli" && pwd)"
PP="$PP${PYTHONPATH:+:$PYTHONPATH}"

for py in python3 /usr/bin/python3; do
  if PYTHONPATH="$PP" "$py" -c 'import gi; gi.require_version("Gtk", "3.0"); import frija' 2>/dev/null; then
    PYTHONPATH="$PP" exec "$py" -m frija_gtk "$@"
  fi
done

if ! /usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
  echo "GTK 3 Python bindings not found. Install them, e.g.: sudo apt install python3-gi gir1.2-gtk-3.0" >&2
else
  echo "frija-cli not found. Install it (pip install --user frija-cli) or clone it next to this folder as ../frija-cli" >&2
fi
exit 1
