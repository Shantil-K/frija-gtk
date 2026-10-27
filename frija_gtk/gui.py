"""GTK 3 graphical interface for frija-cli (replaces Frija's WPF window).

Same layout ideas as the original: Auto / Manual / Legacy tabs, a firmware
info panel, a download button with progress, and a log view. All the actual
work is done by the `frija` package from frija-cli.
"""
import errno
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

import requests  # noqa: E402

from frija import __version__ as CORE_VERSION, core  # noqa: E402
from frija.fus import FUSClient  # noqa: E402

from . import __version__  # noqa: E402

log = logging.getLogger("frija")

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "frija")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
DEFAULTS = {
    "model": "", "csc": "", "imei": "",
    "legacy_model": "", "legacy_csc": "", "legacy_pda": "", "legacy_cscver": "", "legacy_phone": "",
    "output_dir": GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~"),
    "check_crc": True, "decrypt": True, "keep_encrypted": False, "factory": False,
    "mode": "manual",
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(s, f, indent=2)
    except OSError as e:
        log.warning("Could not save settings: %s", e)


def modemmanager_running():
    """Ask systemd, not ModemManager itself: a D-Bus query would start it."""
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    return subprocess.run(["systemctl", "is-active", "--quiet", "ModemManager"], check=False).returncode == 0


def held_by_modemmanager(port, checks=4):
    """True if ModemManager has the phone's port open right now.

    ModemManager locks every port it opens (TIOCEXCL), so while it holds the
    port, opening it as a normal user fails with "Device busy".
    """
    if not sys.platform.startswith("linux") or not modemmanager_running():
        return False
    for _ in range(checks):
        try:
            os.close(os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))
        except OSError as e:
            return e.errno == errno.EBUSY
        time.sleep(0.5)
    return False


def stop_modemmanager():
    """Stop ModemManager. pkexec shows the desktop's password window. Returns an error or None."""
    cmd = ["systemctl", "stop", "ModemManager"]
    if shutil.which("pkexec"):
        cmd = ["pkexec"] + cmd
    r = subprocess.run(cmd, check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode in (126, 127) and cmd[0] == "pkexec":
        return "the password prompt was cancelled"
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip() or f"systemctl exited with code {r.returncode}"
    return None


def human_gb(n):
    return f"{n / 2**30:.2f} GB"


class GuiLogHandler(logging.Handler):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        GLib.idle_add(self.window.append_log, msg, record.levelno)


def labeled_grid(rows):
    """rows: list of (label, widget). Returns a Gtk.Grid."""
    g = Gtk.Grid(column_spacing=12, row_spacing=8)
    for i, (text, w) in enumerate(rows):
        lab = Gtk.Label(label=text, xalign=1)
        lab.get_style_context().add_class("dim-label")
        g.attach(lab, 0, i, 1, 1)
        w.set_hexpand(True)
        g.attach(w, 1, i, 1, 1)
    return g


def entry(text="", placeholder=""):
    e = Gtk.Entry(text=text)
    e.set_placeholder_text(placeholder)
    return e


def value_label():
    lab = Gtk.Label(label="—", xalign=0, selectable=True)
    lab.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    return lab


class FrijaWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Frija")
        self.set_default_size(720, 760)
        self.settings = load_settings()
        self.client = None
        self.fw = None
        self.device = None
        self.busy = False
        self.cancel_event = threading.Event()
        self._stopping = False   # True: Stop (delete the file); False: Pause (keep it)
        self._paused_dest = None  # partial file left by Pause, so Stop can still discard it
        self._last_ui = 0.0
        self._probing_port = None
        self._failed_port = None
        self._mm_stopped_for = None  # port we already stopped ModemManager for (once per plug-in)

        self._build_header()
        self._build_body()
        self._apply_settings()

        self.handler = GuiLogHandler(self)
        logging.getLogger("frija").addHandler(self.handler)
        logging.getLogger("frija").setLevel(logging.INFO)

        self.connect("delete-event", self.on_close)
        GLib.timeout_add_seconds(2, self.poll_devices)
        log.info("Frija GTK %s (frija-cli %s) ready", __version__, CORE_VERSION)

    # ------------------------------------------------------------ layout

    def _build_header(self):
        hb = Gtk.HeaderBar(show_close_button=True, title="Frija", subtitle="Samsung firmware downloader")
        self.set_titlebar(hb)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack)
        hb.set_custom_title(switcher)

        menu_btn = Gtk.MenuButton()
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.set_tooltip_text("Settings")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        title = Gtk.Label(label="<b>Settings</b>", use_markup=True, xalign=0)
        box.pack_start(title, False, False, 0)
        self.opt_crc = Gtk.CheckButton(label="Verify CRC32 after download")
        self.opt_decrypt = Gtk.CheckButton(label="Decrypt after download")
        self.opt_keep = Gtk.CheckButton(label="Keep encrypted file after decrypting")
        self.opt_factory = Gtk.CheckButton(label="Factory firmware (Binary Nature)")
        for w in (self.opt_crc, self.opt_decrypt, self.opt_keep, self.opt_factory):
            box.pack_start(w, False, False, 0)
        self.opt_decrypt.connect("toggled", lambda b: self.opt_keep.set_sensitive(b.get_active()))
        box.pack_start(Gtk.Separator(), False, False, 4)
        about = Gtk.ModelButton(text="About Frija")
        about.connect("clicked", self.on_about)
        box.pack_start(about, False, False, 0)
        box.show_all()
        pop.add(box)
        menu_btn.set_popover(pop)
        hb.pack_end(menu_btn)

    def _build_body(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.add(outer)

        # --- device input (stack pages)
        self.stack.add_titled(self._page_auto(), "auto", "Auto")
        self.stack.add_titled(self._page_manual(), "manual", "Manual")
        self.stack.add_titled(self._page_legacy(), "legacy", "Legacy")
        self.inputs_frame = Gtk.Frame(label=" Device ")
        self.inputs_frame.add(self.stack)
        outer.pack_start(self.inputs_frame, False, False, 0)

        self.check_btn = Gtk.Button(label="Check Update")
        self.check_btn.get_style_context().add_class("suggested-action")
        self.check_btn.connect("clicked", self.on_check)
        bb = Gtk.Box(spacing=6)
        bb.pack_end(self.check_btn, False, False, 0)
        outer.pack_start(bb, False, False, 0)

        # --- firmware info
        self.info = {k: value_label() for k in ("model", "version", "os", "size", "filename", "modified", "type")}
        grid = labeled_grid([
            ("Model", self.info["model"]), ("Version", self.info["version"]), ("OS", self.info["os"]),
            ("Size", self.info["size"]), ("Filename", self.info["filename"]),
            ("Last modified", self.info["modified"]), ("Type", self.info["type"]),
        ])
        grid.set_margin_top(10); grid.set_margin_bottom(12); grid.set_margin_start(12); grid.set_margin_end(12)
        self.fw_frame = Gtk.Frame(label=" Firmware ")
        self.fw_frame.add(grid)
        self.fw_frame.set_sensitive(False)
        outer.pack_start(self.fw_frame, False, False, 0)

        # --- destination + download
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Save to"), False, False, 0)
        self.folder_btn = Gtk.FileChooserButton(title="Choose download folder",
                                                action=Gtk.FileChooserAction.SELECT_FOLDER)
        row.pack_start(self.folder_btn, True, True, 0)
        self.download_btn = Gtk.Button(label="Download")
        self.download_btn.get_style_context().add_class("suggested-action")
        self.download_btn.set_sensitive(False)
        self.download_btn.connect("clicked", self.on_download)
        row.pack_start(self.download_btn, False, False, 0)
        self.pause_btn = Gtk.Button(label="Pause", tooltip_text="Pause the download. Press Download to resume.")
        self.pause_btn.set_sensitive(False)
        self.pause_btn.connect("clicked", self.on_pause)
        row.pack_start(self.pause_btn, False, False, 0)
        self.stop_btn = Gtk.Button(label="Stop", tooltip_text="Cancel the download and delete the partial file.")
        self.stop_btn.get_style_context().add_class("destructive-action")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", self.on_stop)
        row.pack_start(self.stop_btn, False, False, 0)
        outer.pack_start(row, False, False, 0)

        # --- progress
        self.progress = Gtk.ProgressBar(show_text=True, text="")
        outer.pack_start(self.progress, False, False, 0)
        srow = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        srow.pack_start(self.spinner, False, False, 0)
        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        srow.pack_start(self.status, True, True, 0)
        self.speed = Gtk.Label(label="", xalign=1)
        srow.pack_end(self.speed, False, False, 0)
        outer.pack_start(srow, False, False, 0)

        # --- log
        self.logbuf = Gtk.TextBuffer()
        self.tag_warn = self.logbuf.create_tag("warn", foreground="#c67a00")
        self.tag_err = self.logbuf.create_tag("err", foreground="#d33")
        tv = Gtk.TextView(buffer=self.logbuf, editable=False, cursor_visible=False, monospace=True,
                          wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=6, right_margin=6)
        self.logview = tv
        sw = Gtk.ScrolledWindow(min_content_height=140)
        sw.add(tv)
        exp = Gtk.Expander(label="Log", expanded=True)
        exp.add(sw)
        outer.pack_start(exp, True, True, 0)

    def _page_auto(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        top = Gtk.Box(spacing=10)
        self.dev_icon = Gtk.Image.new_from_icon_name("phone-symbolic", Gtk.IconSize.DIALOG)
        top.pack_start(self.dev_icon, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.dev_title = Gtk.Label(label="<b>No device connected</b>", use_markup=True, xalign=0)
        self.dev_sub = Gtk.Label(label="Connect a Samsung phone over USB (unlocked, MTP mode)", xalign=0, wrap=True)
        self.dev_sub.get_style_context().add_class("dim-label")
        tb.pack_start(self.dev_title, False, False, 0)
        tb.pack_start(self.dev_sub, False, False, 0)
        top.pack_start(tb, True, True, 0)
        self.rescan_btn = Gtk.Button(label="Rescan")
        self.rescan_btn.connect("clicked", self.on_rescan)
        top.pack_end(self.rescan_btn, False, False, 0)
        box.pack_start(top, False, False, 0)

        self.dev = {k: value_label() for k in ("model", "csc", "serial", "firmware")}
        box.pack_start(labeled_grid([
            ("Model", self.dev["model"]), ("CSC", self.dev["csc"]),
            ("Serial", self.dev["serial"]), ("Firmware", self.dev["firmware"]),
        ]), False, False, 0)
        return box

    def _page_manual(self):
        self.m_model = entry(placeholder="e.g. SM-S928B")
        self.m_csc = entry(placeholder="e.g. EUX, XAA, BTU")
        self.m_imei = entry(placeholder="IMEI or serial number of the device")
        g = labeled_grid([("Model", self.m_model), ("CSC", self.m_csc), ("IMEI / Serial", self.m_imei)])
        g.set_margin_top(12); g.set_margin_bottom(12); g.set_margin_start(12); g.set_margin_end(12)
        for e in (self.m_model, self.m_csc, self.m_imei):
            e.connect("activate", self.on_check)
        return g

    def _page_legacy(self):
        self.l_model = entry(placeholder="e.g. SM-S928B")
        self.l_csc = entry(placeholder="e.g. EUX")
        self.l_pda = entry(placeholder="e.g. S928BXXU1AXA1")
        self.l_cscver = entry(placeholder="e.g. S928BOXM1AXA1")
        self.l_phone = entry(placeholder="e.g. S928BXXU1AXA1")
        g = labeled_grid([("Model", self.l_model), ("CSC", self.l_csc), ("PDA version", self.l_pda),
                          ("CSC version", self.l_cscver), ("Phone version", self.l_phone)])
        g.set_margin_top(12); g.set_margin_bottom(12); g.set_margin_start(12); g.set_margin_end(12)
        for e in (self.l_model, self.l_csc, self.l_pda, self.l_cscver, self.l_phone):
            e.connect("activate", self.on_check)
        return g

    # ------------------------------------------------------------ settings

    def _apply_settings(self):
        s = self.settings
        self.m_model.set_text(s["model"]); self.m_csc.set_text(s["csc"]); self.m_imei.set_text(s["imei"])
        self.l_model.set_text(s["legacy_model"]); self.l_csc.set_text(s["legacy_csc"])
        self.l_pda.set_text(s["legacy_pda"]); self.l_cscver.set_text(s["legacy_cscver"])
        self.l_phone.set_text(s["legacy_phone"])
        self.folder_btn.set_current_folder(s["output_dir"] if os.path.isdir(s["output_dir"]) else os.path.expanduser("~"))
        self.opt_crc.set_active(s["check_crc"]); self.opt_decrypt.set_active(s["decrypt"])
        self.opt_keep.set_active(s["keep_encrypted"]); self.opt_keep.set_sensitive(s["decrypt"])
        self.opt_factory.set_active(s["factory"])
        self.show_all()
        self.stack.set_visible_child_name(s["mode"] if s["mode"] in ("auto", "manual", "legacy") else "manual")

    def _collect_settings(self):
        self.settings.update(
            model=self.m_model.get_text(), csc=self.m_csc.get_text(), imei=self.m_imei.get_text(),
            legacy_model=self.l_model.get_text(), legacy_csc=self.l_csc.get_text(),
            legacy_pda=self.l_pda.get_text(), legacy_cscver=self.l_cscver.get_text(),
            legacy_phone=self.l_phone.get_text(),
            output_dir=self.folder_btn.get_filename() or self.settings["output_dir"],
            check_crc=self.opt_crc.get_active(), decrypt=self.opt_decrypt.get_active(),
            keep_encrypted=self.opt_keep.get_active(), factory=self.opt_factory.get_active(),
            mode=self.stack.get_visible_child_name(),
        )

    def on_close(self, *_):
        if self.busy:
            if not self.confirm("A download is running", "Pause it and quit? You can resume it later."):
                return True
            self.cancel_event.set()
        self._collect_settings()
        save_settings(self.settings)
        return False

    # ------------------------------------------------------------ UI helpers

    def append_log(self, msg, level):
        end = self.logbuf.get_end_iter()
        tags = [self.tag_err] if level >= logging.ERROR else [self.tag_warn] if level >= logging.WARNING else []
        self.logbuf.insert_with_tags(end, msg + "\n", *tags)
        mark = self.logbuf.create_mark(None, self.logbuf.get_end_iter(), False)
        self.logview.scroll_mark_onscreen(mark)
        self.logbuf.delete_mark(mark)
        return False

    def set_busy(self, busy, status=None):
        self.busy = busy
        self.inputs_frame.set_sensitive(not busy)
        self.check_btn.set_sensitive(not busy)
        self.folder_btn.set_sensitive(not busy)
        self.download_btn.set_sensitive(not busy and self.fw is not None)
        self.pause_btn.set_sensitive(busy)
        self.stop_btn.set_sensitive(busy or self._paused_dest is not None)
        (self.spinner.start if busy else self.spinner.stop)()
        if status is not None:
            self.status.set_text(status)

    def error(self, title, text=""):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.CLOSE, text=title)
        if text:
            d.format_secondary_text(text)
        d.run(); d.destroy()

    def confirm(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text=title)
        d.format_secondary_text(text)
        r = d.run(); d.destroy()
        return r == Gtk.ResponseType.YES

    def run_bg(self, work, done):
        """Run work() in a thread; then call done(result, exc) on the GTK thread."""
        def target():
            try:
                res, exc = work(), None
            except Exception as e:  # noqa: BLE001 - reported to the user
                res, exc = None, e
            GLib.idle_add(done, res, exc)
        threading.Thread(target=target, daemon=True).start()

    def progress_cb(self, label):
        t0 = time.time()
        start = {"bytes": None}

        def cb(done, total):
            now = time.time()
            if start["bytes"] is None:
                start["bytes"] = done
            if now - self._last_ui < 0.25 and done < total:
                return
            self._last_ui = now
            frac = done / total if total else 0
            speed = (done - start["bytes"]) / max(now - t0, 1e-6) / 2**20
            GLib.idle_add(self._set_progress, frac, f"{label} {human_gb(done)} of {human_gb(total)}",
                          f"{speed:.1f} MB/s" if label == "Downloaded" else "")
        return cb

    def _set_progress(self, frac, text, speed):
        self.progress.set_fraction(frac)
        self.progress.set_text(f"{frac * 100:.0f}%  —  {text}")
        self.speed.set_text(speed)
        return False

    # ------------------------------------------------------------ auto mode

    def poll_devices(self):
        if self.stack.get_visible_child_name() == "auto" and not self.busy:
            self.probe_device()
        return True

    def probe_device(self, force=False):
        from frija import modem  # only needed for the Auto tab

        ports = modem.find_ports()
        if not ports:
            self._failed_port = None
            self._mm_stopped_for = None  # unplugged: reset for the next phone
            if self.device or force:
                self.device = None
                self._show_device(None, "Connect a Samsung phone over USB (unlocked, MTP mode)")
            return
        port = ports[0]
        if not force and port in (self._probing_port, self._failed_port, self.device and self.device.port):
            return
        self._probing_port = port
        self.dev_title.set_markup("<b>Reading device…</b>")
        self.dev_sub.set_text(port)

        def work():
            try:
                return modem.read_device_info(port)
            except Exception as e:  # noqa: BLE001 - shown to the user
                e.held_by_mm = held_by_modemmanager(port)  # check right away, while it still holds it
                raise

        def done(dev, exc):
            self._probing_port = None
            if exc and exc.held_by_mm and port != self._mm_stopped_for:
                self._mm_stopped_for = port  # only once per plug-in, so a cancelled prompt doesn't repeat
                self.auto_stop_modemmanager(port)
            elif exc:
                self.device = None
                self._failed_port = port  # don't retry until replugged or Rescan
                if exc.held_by_mm:
                    msg = "ModemManager is using the phone's port and couldn't be stopped. Press Rescan to try again."
                elif "did not return" in str(exc):
                    msg = "The phone didn't answer. Unlock it and keep the screen on, then press Rescan."
                else:
                    msg = str(exc)
                self._show_device(None, msg, title="Could not read device")
                log.error("%s: %s", port, msg)
            else:
                self._failed_port = None
                self.device = dev
                self._show_device(dev)
                log.info("Device connected on %s: %s / %s", port, dev.model, dev.csc)
            return False
        self.run_bg(work, done)

    def on_rescan(self, *_):
        self._mm_stopped_for = None  # allow another automatic ModemManager stop
        self.probe_device(force=True)

    def auto_stop_modemmanager(self, port):
        self._probing_port = port  # keep the poller away while we stop it
        self._show_device(None, "ModemManager is holding the phone's port. Stopping it "
                                "(your system may ask for your password)…", title="Freeing the USB port")
        log.info("%s: ModemManager is holding the port, stopping it", port)

        def done(err, exc):
            self._probing_port = None
            err = err or (str(exc) if exc else None)
            if err:
                self._failed_port = port
                log.error("Could not stop ModemManager: %s", err)
                self._show_device(None, "ModemManager is using the phone's port and couldn't be stopped "
                                        f"({err}). Press Rescan to try again.", title="Could not read device")
                return False
            log.info("ModemManager stopped. Reading the phone again…")
            GLib.timeout_add(1000, lambda: self.probe_device(force=True) and False)
            return False
        self.run_bg(stop_modemmanager, done)

    def _show_device(self, dev, sub="", title="No device connected"):
        if dev:
            self.dev_title.set_markup(f"<b>{GLib.markup_escape_text(dev.model)}</b>")
            self.dev_sub.set_text(f"Connected on {dev.port}")
            vals = {"model": dev.model, "csc": dev.csc, "serial": dev.serial, "firmware": dev.firmware.split("/")[0]}
        else:
            self.dev_title.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
            self.dev_sub.set_text(sub)
            vals = {}
        for k, lab in self.dev.items():
            lab.set_text(vals.get(k) or "—")

    # ------------------------------------------------------------ check

    def on_check(self, *_):
        if self.busy:
            return
        mode = self.stack.get_visible_child_name()
        if mode == "auto":
            if not self.device:
                self.error("No device connected", "Connect your phone over USB, or use the Manual tab.")
                return
            args = (self.device.model, self.device.csc, self.device.serial, None)
        elif mode == "manual":
            args = (self.m_model.get_text(), self.m_csc.get_text(), self.m_imei.get_text(), None)
        else:
            args = (self.l_model.get_text(), self.l_csc.get_text(), "",
                    (self.l_pda.get_text(), self.l_cscver.get_text(), self.l_phone.get_text()))
        self._collect_settings()
        save_settings(self.settings)
        self.fw = None
        self._paused_dest = None  # a paused file stays on disk; Download offers to resume it
        self._show_fw(None)
        self.set_busy(True, "Checking for firmware…")
        self.progress.set_fraction(0); self.progress.set_text(""); self.speed.set_text("")
        factory = self.opt_factory.get_active()

        def work():
            client = FUSClient()
            q = core.build_query(*args)
            return client, core.check(client, q, factory=factory)

        def done(res, exc):
            if exc:
                self.set_busy(False, "")
                msg = f"Network error: {exc}" if isinstance(exc, requests.RequestException) else str(exc)
                log.error(msg)
                self.error("Firmware not found", msg)
            else:
                self.client, self.fw = res
                self._show_fw(self.fw)
                self.set_busy(False, "Latest firmware found")
            return False
        self.run_bg(work, done)

    def _show_fw(self, fw):
        self.fw_frame.set_sensitive(fw is not None)
        if not fw:
            for lab in self.info.values():
                lab.set_text("—")
            return
        name = f"{fw.model} ({fw.display_name})" if fw.display_name else fw.model
        vals = {"model": name, "version": fw.display_version, "os": fw.os,
                "size": f"{fw.size_mb:,.2f} MB ({human_gb(fw.size_bytes)})", "filename": fw.filename,
                "modified": fw.last_modified, "type": "Factory" if fw.binary_nature == 1 else "Home"}
        for k, lab in self.info.items():
            lab.set_text(vals[k] or "—")
            lab.set_tooltip_text(vals[k] or None)

    # ------------------------------------------------------------ download

    def on_download(self, *_):
        if self.busy or not self.fw:
            return
        folder = self.folder_btn.get_filename()
        if not folder or not os.path.isdir(folder):
            self.error("Choose a download folder first")
            return
        dest = os.path.join(folder, self.fw.filename)
        if os.path.exists(dest) and os.path.getsize(dest) != self.fw.size_bytes:
            choice = self.ask_resume(dest)
            if choice == "cancel":
                return
            if choice == "overwrite":
                os.remove(dest)
                log.info("Overwriting file")
            else:
                log.info("Resuming download")

        self._collect_settings()
        save_settings(self.settings)
        self.cancel_event.clear()
        self._stopping = False
        self._paused_dest = None
        self.set_busy(True, "Downloading…")
        fw, client = self.fw, self.client
        check_crc, do_decrypt, keep = self.opt_crc.get_active(), self.opt_decrypt.get_active(), self.opt_keep.get_active()
        cancel = self.cancel_event.is_set

        def status(text):
            GLib.idle_add(self.status.set_text, text)

        def work():
            status("Downloading firmware…")
            core.download(client, fw, dest, self.progress_cb("Downloaded"), cancel)
            if check_crc:
                status("Verifying CRC32…")
                core.verify_crc(fw, dest, self.progress_cb("Verified"), cancel)
            if not do_decrypt:
                return dest
            status("Decrypting firmware…")
            return core.decrypt(dest, core.decrypt_key_for(fw, dest), keep, self.progress_cb("Decrypted"), cancel)

        def done(path, exc):
            self.speed.set_text("")
            if isinstance(exc, core.Cancelled) and self._stopping:
                self._discard(dest)
                self.set_busy(False, "Stopped")
                log.info("Download stopped and partial file deleted")
                return False
            if isinstance(exc, core.Cancelled):
                self._paused_dest = dest if os.path.exists(dest) else None
                self.set_busy(False, "Paused. Press Download to resume.")
                log.info("Download paused")
                return False
            self.set_busy(False)
            if exc:
                msg = f"Network error: {exc}" if isinstance(exc, requests.RequestException) else str(exc)
                self.status.set_text("Failed")
                log.error(msg)
                self.error("Download failed", msg + "\n\nPress Download again to resume.")
            else:
                self.progress.set_fraction(1)
                self.status.set_text("Done")
                self.finished(path, decrypted=do_decrypt)
            return False
        self.run_bg(work, done)

    def ask_resume(self, dest):
        have = os.path.getsize(dest)
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text="File already exists")
        d.format_secondary_text(f"{os.path.basename(dest)}\n{human_gb(have)} of {human_gb(self.fw.size_bytes)} "
                                "already downloaded. Resume the download or start over?")
        d.add_buttons("Cancel", 0, "Overwrite", 1, "Resume", 2)
        d.set_default_response(2)
        r = d.run(); d.destroy()
        return {1: "overwrite", 2: "resume"}.get(r, "cancel")

    def on_pause(self, *_):
        self._stopping = False
        self.cancel_event.set()
        self.status.set_text("Pausing…")
        self.pause_btn.set_sensitive(False)

    def on_stop(self, *_):
        if not self.confirm("Stop the download?",
                            "The partly downloaded file will be deleted. To continue later, use Pause instead."):
            return
        if self.busy:
            self._stopping = True
            self.cancel_event.set()
            self.status.set_text("Stopping…")
            self.pause_btn.set_sensitive(False)
            self.stop_btn.set_sensitive(False)
        elif self._paused_dest:
            self._discard(self._paused_dest)
            self.set_busy(False, "Stopped")
            log.info("Paused download deleted")

    def _discard(self, path):
        self._paused_dest = None
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not delete %s: %s", path, e)
        self.progress.set_fraction(0)
        self.progress.set_text("")

    def finished(self, path, decrypted):
        what = "downloaded and decrypted" if decrypted else "downloaded"
        log.info("Firmware successfully %s", what)
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.NONE, text=f"Firmware successfully {what}")
        d.format_secondary_text(path)
        d.add_buttons("Close", 0, "Open folder", 1)
        r = d.run(); d.destroy()
        if r == 1:
            try:
                Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(os.path.dirname(path)), None)
            except GLib.Error as e:
                log.warning("Could not open folder: %s", e)

    def on_about(self, *_):
        d = Gtk.AboutDialog(transient_for=self, modal=True, program_name="Frija",
                            version=f"{__version__} (frija-cli {CORE_VERSION})", logo_icon_name="phone",
                            comments="GTK front end for frija-cli, a port of the Frija Samsung firmware downloader.\n"
                                     "Downloads firmware directly from Samsung's FUS servers.")
        d.run(); d.destroy()


class FrijaApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.frija.Gtk", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.get_active_window() or FrijaWindow(self)
        win.present()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    FrijaApp().run(None)


if __name__ == "__main__":
    main()
