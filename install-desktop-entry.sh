#!/usr/bin/env bash
# Add Frija to your desktop's app menu (current user only).
# Undo with: rm ~/.local/share/applications/frija.desktop
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/frija.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Frija
Comment=Samsung firmware downloader (GTK front end for frija-cli)
Exec="$DIR/frija-gui.sh"
Icon=phone
Terminal=false
Categories=Utility;
DESK
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed: Frija should now appear in your app menu."
