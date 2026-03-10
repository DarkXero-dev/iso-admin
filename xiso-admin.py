#!/usr/bin/env python3
"""XeroISO Admin Control Panel"""

import sys
import os
import re
import json
import math
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QGroupBox, QMessageBox,
    QSizePolicy, QDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QLinearGradient, QRadialGradient, QPen,
)

# ── Server constants (IP never shown in UI) ────────────────────────────────────
VPS_HOST     = "172.233.214.202"
VPS_USER     = "root"
VPS          = f"{VPS_USER}@{VPS_HOST}"
MAINT_SCRIPT = "/Docker/xeroiso/maintenance.sh"
CODES_FILE   = "/Docker/xeroiso/codez.json"
DOCKER_CTR   = "xero-main"

SSH_BASE = ["ssh", "-q",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new",
            VPS]

# ── Remote gen script (piped via stdin) ────────────────────────────────────────
GEN_SCRIPT = r"""#!/bin/bash
CODES_FILE="/Docker/xeroiso/codez.json"
LOCAL_EDIT="/Docker/xeroiso/codez.json.edit"
DOCKER_CONTAINER="xero-main"
PREFIX="KDE"
EMAIL="$1"
FORCE="${2:-no}"

[[ ! -f "$CODES_FILE" ]] && echo '{}' > "$CODES_FILE"

if ! jq empty "$CODES_FILE" >/dev/null 2>&1; then
    echo "ERROR:codez.json is invalid JSON" >&2; exit 1
fi

existing=$(jq -r --arg e "$EMAIL" \
    'to_entries[] | select(.value.email == $e) | .key' \
    "$CODES_FILE" 2>/dev/null | head -1)

if [[ -n "$existing" && "$FORCE" != "force" ]]; then
    echo "EXISTING:$existing"; exit 0
fi

CODE="${PREFIX}-$(tr -dc 'A-Z0-9' </dev/urandom | head -c8)"
while jq -e --arg c "$CODE" 'has($c)' "$CODES_FILE" >/dev/null 2>&1; do
    CODE="${PREFIX}-$(tr -dc 'A-Z0-9' </dev/urandom | head -c8)"
done

if ! jq --arg code "$CODE" --arg product "kde" --arg email "$EMAIL" \
    '. + {($code): {product: $product, email: $email}}' \
    "$CODES_FILE" > "$LOCAL_EDIT"; then
    echo "ERROR:Failed to write codez.json" >&2; exit 1
fi

mv "$LOCAL_EDIT" "$CODES_FILE"
docker restart "$DOCKER_CONTAINER" >/dev/null 2>&1
echo "CODE:$CODE"
"""


def ssh(remote_cmd, stdin_input=None, timeout=60):
    cmd = SSH_BASE + ([remote_cmd] if isinstance(remote_cmd, str) else remote_cmd)
    r = subprocess.run(cmd, input=stdin_input, capture_output=True,
                       text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


# ── Worker threads ─────────────────────────────────────────────────────────────

class ConnCheckWorker(QThread):
    done = pyqtSignal(bool)

    def run(self):
        try:
            _, _, rc = ssh("echo ok", timeout=8)
            self.done.emit(rc == 0)
        except Exception:
            self.done.emit(False)


class SetupKeyWorker(QThread):
    progress = pyqtSignal(str)
    done     = pyqtSignal(str)   # "OK" or "ERROR:..."

    def __init__(self, user: str, password: str):
        super().__init__()
        self.user     = user
        self.password = password

    def run(self):
        try:
            # 1. Ensure ~/.ssh dir
            ssh_dir  = os.path.expanduser("~/.ssh")
            key_path = os.path.join(ssh_dir, "id_ed25519")
            pub_path = key_path + ".pub"
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)

            # 2. Generate key pair if missing
            if not os.path.exists(key_path):
                self.progress.emit("Generating SSH key pair…")
                r = subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", key_path,
                     "-N", "", "-q"],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    self.done.emit(f"ERROR:ssh-keygen failed: {r.stderr.strip()}")
                    return

            with open(pub_path) as f:
                pubkey = f.read().strip()

            self.progress.emit("Copying public key to server…")

            # 3. Push pubkey using paramiko (preferred) or sshpass fallback
            try:
                import paramiko  # type: ignore
                self._push_paramiko(pubkey)
            except ImportError:
                self._push_sshpass(pub_path)

            self.done.emit("OK")

        except Exception as exc:
            self.done.emit(f"ERROR:{exc}")

    def _push_paramiko(self, pubkey: str):
        import paramiko  # type: ignore
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(VPS_HOST, username=self.user,
                       password=self.password, timeout=15)
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo {pubkey!r} >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys && "
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
        )
        _, stdout, _ = client.exec_command(cmd)
        stdout.channel.recv_exit_status()
        client.close()

    def _push_sshpass(self, pub_path: str):
        sshpass = subprocess.run(["which", "sshpass"],
                                 capture_output=True, text=True)
        if sshpass.returncode != 0:
            raise RuntimeError(
                "Neither python-paramiko nor sshpass is installed.\n"
                "Install one:  sudo pacman -S python-paramiko\n"
                "          or: sudo pacman -S sshpass"
            )
        r = subprocess.run(
            ["sshpass", "-p", self.password, "ssh-copy-id",
             "-i", pub_path,
             "-o", "StrictHostKeyChecking=accept-new",
             f"{self.user}@{VPS_HOST}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or "ssh-copy-id failed")


class GenWorker(QThread):
    done = pyqtSignal(str)

    def __init__(self, email: str, force: bool):
        super().__init__()
        self.email = email
        self.force = force

    def run(self):
        try:
            stdout, stderr, rc = ssh(
                ["bash", "-s", "--", self.email, "force" if self.force else "no"],
                stdin_input=GEN_SCRIPT, timeout=60,
            )
            if rc != 0:
                self.done.emit(f"ERROR:{stderr or stdout or 'SSH failed'}")
            elif stdout.startswith(("CODE:", "EXISTING:", "ERROR:")):
                self.done.emit(stdout)
            else:
                self.done.emit(f"ERROR:Unexpected output: {stdout!r}")
        except subprocess.TimeoutExpired:
            self.done.emit("ERROR:Connection timed out")
        except Exception as exc:
            self.done.emit(f"ERROR:{exc}")


class MaintWorker(QThread):
    done = pyqtSignal(str)

    def __init__(self, action: str | None = None):
        super().__init__()
        self.action = action

    def run(self):
        try:
            if self.action:
                ssh(f"TERM=dumb bash {MAINT_SCRIPT} {self.action}", timeout=15)
            stdout, stderr, _ = ssh(
                f"TERM=dumb bash {MAINT_SCRIPT} status", timeout=15
            )
            combined = stdout + stderr
            if "currently ON" in combined:
                self.done.emit("ON")
            elif "currently OFF" in combined:
                self.done.emit("OFF")
            else:
                self.done.emit(f"ERROR:Unexpected: {combined!r}")
        except subprocess.TimeoutExpired:
            self.done.emit("ERROR:Connection timed out")
        except Exception as exc:
            self.done.emit(f"ERROR:{exc}")


class FetchCodesWorker(QThread):
    done = pyqtSignal(str)

    def run(self):
        try:
            stdout, stderr, rc = ssh(f"cat {CODES_FILE}", timeout=15)
            if rc != 0:
                self.done.emit(f"ERROR:{stderr or 'Failed to read codes file'}")
                return
            json.loads(stdout)
            self.done.emit(stdout)
        except subprocess.TimeoutExpired:
            self.done.emit("ERROR:Connection timed out")
        except json.JSONDecodeError as exc:
            self.done.emit(f"ERROR:Invalid JSON from VPS: {exc}")
        except Exception as exc:
            self.done.emit(f"ERROR:{exc}")


class SaveCodesWorker(QThread):
    done = pyqtSignal(str)

    def __init__(self, codes: dict):
        super().__init__()
        self.codes = codes

    def run(self):
        try:
            payload   = json.dumps(self.codes, indent=2, ensure_ascii=False)
            write_cmd = (f"cat > {CODES_FILE} && "
                         f"docker restart {DOCKER_CTR} >/dev/null 2>&1")
            _, stderr, rc = ssh(["bash", "-c", write_cmd],
                                stdin_input=payload, timeout=60)
            if rc != 0:
                self.done.emit(f"ERROR:{stderr or 'Write/restart failed'}")
            else:
                self.done.emit("OK")
        except subprocess.TimeoutExpired:
            self.done.emit("ERROR:Connection timed out")
        except Exception as exc:
            self.done.emit(f"ERROR:{exc}")


# ── Connect Dialog ─────────────────────────────────────────────────────────────

class ConnectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Server")
        self.setFixedWidth(400)
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        vl = QVBoxLayout(self)
        vl.setSpacing(10)

        info = QLabel(
            "Set up SSH key authentication for this machine.\n"
            "Your public key will be installed on the server so\n"
            "no password is needed for future connections."
        )
        info.setWordWrap(True)
        vl.addWidget(info)

        vl.addWidget(QFrame(frameShape=QFrame.Shape.HLine))

        form = QVBoxLayout()
        form.setSpacing(6)

        form.addWidget(QLabel("Username:"))
        self.user_input = QLineEdit(VPS_USER)
        form.addWidget(self.user_input)

        form.addWidget(QLabel("Password:"))
        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setPlaceholderText("Server password")
        self.pass_input.returnPressed.connect(self._on_connect)
        form.addWidget(self.pass_input)

        vl.addLayout(form)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        vl.addWidget(self.status_lbl)

        vl.addWidget(QFrame(frameShape=QFrame.Shape.HLine))

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.connect_btn = QPushButton("Connect && Save Key")
        self.connect_btn.setDefault(True)
        self.connect_btn.clicked.connect(self._on_connect)
        btn_row.addWidget(self.connect_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        vl.addLayout(btn_row)

    def _on_connect(self):
        user     = self.user_input.text().strip()
        password = self.pass_input.text()
        if not user or not password:
            self.status_lbl.setText("Username and password are required.")
            return

        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Working…")
        self.status_lbl.setText("Connecting…")

        self._worker = SetupKeyWorker(user, password)
        self._worker.progress.connect(self.status_lbl.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, result: str):
        if result == "OK":
            self.status_lbl.setStyleSheet("color: #55aa55;")
            self.status_lbl.setText("✓  SSH key installed. Connection ready.")
            self.connect_btn.setText("Close")
            self.connect_btn.setEnabled(True)
            self.connect_btn.clicked.disconnect()
            self.connect_btn.clicked.connect(self.accept)
        else:
            msg = result[6:] if result.startswith("ERROR:") else result
            self.status_lbl.setStyleSheet("color: #e05555;")
            self.status_lbl.setText(f"Error: {msg}")
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect && Save Key")


# ── Animated Header ────────────────────────────────────────────────────────────

class AnimatedHeader(QWidget):
    connect_clicked = pyqtSignal()

    _H = 100

    # connection state: None = checking, True = connected, False = disconnected
    _DOT_CHECKING     = QColor(160, 160, 160, 200)
    _DOT_CONNECTED    = QColor( 60, 210,  90, 255)
    _DOT_DISCONNECTED = QColor(215,  65,  65, 255)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

        self._phase      = 0.0
        self._led_tick   = 0
        self._connected: bool | None = None   # None = checking

        # Connect button (top-right, always visible)
        self._btn = QPushButton("Connect", self)
        self._btn.setFlat(True)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self.connect_clicked)
        self._btn.setFixedSize(90, 26)

        _t = QTimer(self)
        _t.timeout.connect(self._tick)
        _t.start(33)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Bottom-right of the header, level with the status line
        self._btn.move(self.width() - self._btn.width() - 12,
                       self._H - self._btn.height() - 10)

    def set_connected(self, state: bool | None):
        self._connected = state
        if state is None:
            self._btn.setText("Connect")
            self._btn.setEnabled(False)
        elif state:
            self._btn.setText("Disconnect")
            self._btn.setEnabled(True)
        else:
            self._btn.setText("Connect")
            self._btn.setEnabled(True)
        self.update()

    def _tick(self):
        self._phase    = (self._phase + 0.05) % (math.pi * 200)
        self._led_tick += 1
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        w, h = self.width(), self._H

        # Background gradient → transparent at bottom
        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.00, QColor(22,  7, 52, 240))
        bg.setColorAt(0.65, QColor(16,  5, 40, 195))
        bg.setColorAt(1.00, QColor( 8,  2, 22,   0))
        p.fillRect(0, 0, w, h, bg)

        # Sweeping shimmer on top edge
        sx = (math.sin(self._phase * 0.22) * 0.5 + 0.5) * w
        sh = QLinearGradient(sx - 130, 0, sx + 130, 0)
        sh.setColorAt(0.0, QColor(150, 70, 255,  0))
        sh.setColorAt(0.5, QColor(180, 90, 255, 85))
        sh.setColorAt(1.0, QColor(150, 70, 255,  0))
        p.fillRect(0, 0, w, 2, sh)

        # Server icon position
        cx, cy = 62, h // 2 - 2

        # Pulse rings
        for i in range(3):
            t     = (self._phase + i * (math.pi * 2 / 3)) % (math.pi * 2)
            alpha = int(60 * (math.sin(t) + 1) / 2)
            r     = 26 + i * 10 + 4 * math.sin(t)
            pen   = QPen(QColor(150, 70, 255, alpha))
            pen.setWidthF(1.2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

        # Server chassis
        self._draw_server(p, cx - 20, cy - 26, 40, 52)

        # Title
        p.setFont(QFont("JetBrains Mono", 15, QFont.Weight.Bold))
        p.setPen(QColor(235, 222, 255, 245))
        p.drawText(cx + 42, cy - 6, "XeroLinux ISO Admin Panel")

        # Connection status dot + label
        tx, ty = cx + 42, cy + 15
        dot_col = (self._DOT_CHECKING if self._connected is None
                   else self._DOT_CONNECTED if self._connected
                   else self._DOT_DISCONNECTED)

        p.setFont(QFont("JetBrains Mono", 8))
        fm   = p.fontMetrics()
        dotw = fm.horizontalAdvance("●") + 4
        p.setPen(dot_col)
        p.drawText(tx, ty, "●")

        status_text = ("Checking…" if self._connected is None
                       else "Connected" if self._connected
                       else "No Connection")
        p.setPen(QColor(150, 105, 215, 175))
        p.drawText(tx + dotw, ty, status_text)

    # ── Server icon ───────────────────────────────────────────────────────────

    def _draw_server(self, p: QPainter, x, y, bw, bh):
        p.setBrush(QColor(48, 16, 105))
        p.setPen(QPen(QColor(115, 65, 200), 1))
        p.drawRoundedRect(QRectF(x, y, bw, bh), 4, 4)

        slot_h = (bh - 14) / 3.0
        for i in range(3):
            sy = y + 7 + i * slot_h
            p.setBrush(QColor(22, 8, 58))
            p.setPen(QPen(QColor(80, 45, 155), 1))
            p.drawRoundedRect(QRectF(x + 4, sy + 1, bw - 14, slot_h - 3), 2, 2)

            led_cx = x + bw - 7
            led_cy = sy + slot_h / 2
            led_on = (self._led_tick // 22 + i) % 4 != 0

            if led_on:
                glow = QRadialGradient(QPointF(led_cx, led_cy), 5)
                glow.setColorAt(0, QColor(50, 230, 110, 190))
                glow.setColorAt(1, QColor(30, 200,  80,   0))
                p.setBrush(glow)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(led_cx, led_cy), 5, 5)

            p.setBrush(QColor(40, 215, 95) if led_on else QColor(20, 60, 35))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(led_cx, led_cy), 2.2, 2.2)


# ── Manage Codes Dialog ────────────────────────────────────────────────────────

COL_CODE   = 0
COL_EMAIL  = 1
COL_DELETE = 2


class ManageCodesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Access Codes")
        self.setMinimumSize(640, 460)
        self.setSizeGripEnabled(True)
        self._fetch_worker = None
        self._save_worker  = None
        self._build_ui()
        self._load()

    def _build_ui(self):
        vl = QVBoxLayout(self)
        vl.setSpacing(10)

        # Search bar
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter by code or email…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_input)
        vl.addLayout(search_row)

        # Table
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Code", "Email", ""])
        self.table.horizontalHeader().setSectionResizeMode(
            COL_CODE,  QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            COL_EMAIL, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            COL_DELETE, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_DELETE, 80)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        vl.addWidget(self.table)

        self.status_lbl = QLabel("")
        vl.addWidget(self.status_lbl)

        vl.addWidget(QFrame(frameShape=QFrame.Shape.HLine))

        btn_row = QHBoxLayout()
        self.reload_btn = QPushButton("↻  Reload from VPS")
        self.reload_btn.clicked.connect(self._load)
        btn_row.addWidget(self.reload_btn)
        btn_row.addStretch()

        self.entry_count = QLabel("")
        btn_row.addWidget(self.entry_count)
        btn_row.addSpacing(12)

        self.save_btn = QPushButton("Save && Restart Container")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.save_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        vl.addLayout(btn_row)

    def _load(self):
        self.table.setRowCount(0)
        self.save_btn.setEnabled(False)
        self.reload_btn.setEnabled(False)
        self._set_status("Loading codes from VPS…")
        self._fetch_worker = FetchCodesWorker()
        self._fetch_worker.done.connect(self._on_fetch_done)
        self._fetch_worker.start()

    def _on_fetch_done(self, result: str):
        self.reload_btn.setEnabled(True)
        if result.startswith("ERROR:"):
            self._set_status(f"Error: {result[6:]}", error=True)
            return
        try:
            data = json.loads(result)
        except json.JSONDecodeError as exc:
            self._set_status(f"JSON parse error: {exc}", error=True)
            return

        self.table.setRowCount(0)
        for code, info in sorted(data.items()):
            self._add_row(code, info.get("email", ""))

        self._apply_filter(self.search_input.text())
        self.save_btn.setEnabled(True)
        self._set_status("")

    def _add_row(self, code: str, email: str):
        row = self.table.rowCount()
        self.table.insertRow(row)

        code_item = QTableWidgetItem(code)
        code_item.setFont(QFont("monospace"))
        code_item.setFlags(code_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, COL_CODE, code_item)
        self.table.setItem(row, COL_EMAIL, QTableWidgetItem(email))

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(lambda _, b=del_btn: self._delete_row(b))
        self.table.setCellWidget(row, COL_DELETE, del_btn)

    def _delete_row(self, btn: QPushButton):
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, COL_DELETE) is btn:
                code = self.table.item(row, COL_CODE).text()
                ans = QMessageBox.question(
                    self, "Delete Entry",
                    f"Remove code  {code}  from the file?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans == QMessageBox.StandardButton.Yes:
                    self.table.removeRow(row)
                    self._update_count()
                break

    def _update_count(self):
        visible = sum(not self.table.isRowHidden(r)
                      for r in range(self.table.rowCount()))
        total = self.table.rowCount()
        if visible == total:
            self.entry_count.setText(f"{total} entr{'y' if total == 1 else 'ies'}")
        else:
            self.entry_count.setText(f"{visible} of {total} shown")

    def _apply_filter(self, text: str):
        needle = text.strip().lower()
        for row in range(self.table.rowCount()):
            code  = (self.table.item(row, COL_CODE)  or QTableWidgetItem()).text().lower()
            email = (self.table.item(row, COL_EMAIL) or QTableWidgetItem()).text().lower()
            self.table.setRowHidden(
                row, bool(needle) and needle not in code and needle not in email)
        self._update_count()

    def _save(self):
        count = self.table.rowCount()
        ans = QMessageBox.question(
            self, "Save Changes",
            f"Write {count} entr{'y' if count == 1 else 'ies'} to codez.json "
            f"and restart the Docker container?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        codes = {
            self.table.item(r, COL_CODE).text(): {
                "product": "kde",
                "email":   self.table.item(r, COL_EMAIL).text().strip(),
            }
            for r in range(count)
        }

        self.save_btn.setEnabled(False)
        self.reload_btn.setEnabled(False)
        self._set_status("Saving to VPS and restarting container…")

        self._save_worker = SaveCodesWorker(codes)
        self._save_worker.done.connect(self._on_save_done)
        self._save_worker.start()

    def _on_save_done(self, result: str):
        self.reload_btn.setEnabled(True)
        self.save_btn.setEnabled(True)
        if result == "OK":
            self._set_status("✓ Saved and container restarted.")
        else:
            self._set_status(f"Error: {result[6:]}", error=True)

    def _set_status(self, msg: str, error: bool = False):
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color: #e05555;" if error else "")


# ── Main window ────────────────────────────────────────────────────────────────

class XeroAdminWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("XeroISO Admin")
        self.setMinimumWidth(500)
        self._maint_status: bool | None = None
        self._gen_worker:   GenWorker   | None = None
        self._maint_worker: MaintWorker | None = None
        self._conn_worker:  ConnCheckWorker | None = None
        self._build_ui()
        self._maint_refresh()
        self._conn_check()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        self.header = AnimatedHeader()
        self.header.connect_clicked.connect(self._open_connect)
        root.addWidget(self.header)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setSpacing(12)
        bl.setContentsMargins(16, 14, 16, 16)
        bl.addWidget(self._make_codegen_group())
        bl.addWidget(self._make_maintenance_group())
        bl.addStretch()
        root.addWidget(body)

    # ── Connection ─────────────────────────────────────────────────────────────

    def _conn_check(self):
        self.header.set_connected(None)
        self._conn_worker = ConnCheckWorker()
        self._conn_worker.done.connect(self.header.set_connected)
        self._conn_worker.start()

    def _open_connect(self):
        if self.header._connected:
            # Already connected — just re-ping to refresh status
            self._conn_check()
        else:
            dlg = ConnectDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._conn_check()

    # ── Code Generator ─────────────────────────────────────────────────────────

    def _make_codegen_group(self) -> QGroupBox:
        grp = QGroupBox("Access Code Generator")
        vl  = QVBoxLayout(grp)
        vl.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("user@example.com")
        self.email_input.returnPressed.connect(self._on_generate)
        row.addWidget(QLabel("Email:"))
        row.addWidget(self.email_input, 1)
        self.gen_btn = QPushButton("Generate")
        self.gen_btn.setDefault(True)
        self.gen_btn.clicked.connect(self._on_generate)
        row.addWidget(self.gen_btn)
        vl.addLayout(row)

        self.result_box = QWidget()
        rl = QVBoxLayout(self.result_box)
        rl.setContentsMargins(0, 4, 0, 0)
        rl.setSpacing(4)

        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("Generated Code:"))

        self.code_label = QLabel("")
        f = self.code_label.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.code_label.setFont(f)
        self.code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        code_row.addWidget(self.code_label, 1)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedWidth(70)
        self.copy_btn.clicked.connect(self._copy_code)
        code_row.addWidget(self.copy_btn)
        rl.addLayout(code_row)

        self.code_for_label = QLabel("")
        rl.addWidget(self.code_for_label)
        self.result_box.setVisible(False)
        vl.addWidget(self.result_box)

        self.gen_status = QLabel("")
        vl.addWidget(self.gen_status)

        mgr_row = QHBoxLayout()
        mgr_row.addStretch()
        manage_btn = QPushButton("Manage Codes…")
        manage_btn.setFlat(True)
        manage_btn.clicked.connect(self._open_manage)
        mgr_row.addWidget(manage_btn)
        vl.addLayout(mgr_row)

        return grp

    # ── Maintenance ────────────────────────────────────────────────────────────

    def _make_maintenance_group(self) -> QGroupBox:
        grp = QGroupBox("Maintenance Mode")
        vl  = QVBoxLayout(grp)
        vl.setSpacing(8)

        srow = QHBoxLayout()
        srow.setSpacing(8)

        self.maint_dot = QLabel("●")
        self.maint_dot.setFixedWidth(18)
        srow.addWidget(self.maint_dot)

        self.maint_label = QLabel("Checking…")
        srow.addWidget(self.maint_label, 1)

        self.toggle_btn = QPushButton("…")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setFixedWidth(100)
        self.toggle_btn.clicked.connect(self._toggle_maintenance)
        srow.addWidget(self.toggle_btn)
        vl.addLayout(srow)

        self.maint_msg = QLabel("")
        vl.addWidget(self.maint_msg)

        refresh_btn = QPushButton("↻  Refresh Status")
        refresh_btn.setFlat(True)
        refresh_btn.clicked.connect(self._maint_refresh)
        vl.addWidget(refresh_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        return grp

    # ── Code generator logic ───────────────────────────────────────────────────

    def _on_generate(self):
        email = self.email_input.text().strip().lower()
        if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
            self.gen_status.setText("Invalid email address.")
            self.email_input.setFocus()
            return
        self.result_box.setVisible(False)
        self.gen_status.setText("Connecting to VPS…")
        self.gen_btn.setEnabled(False)
        self.gen_btn.setText("Working…")
        self._run_gen(email, force=False)

    def _run_gen(self, email: str, force: bool):
        self._gen_worker = GenWorker(email, force)
        self._gen_worker.done.connect(lambda r: self._on_gen_done(r, email))
        self._gen_worker.start()

    def _on_gen_done(self, result: str, email: str):
        self.gen_btn.setEnabled(True)
        self.gen_btn.setText("Generate")
        if result.startswith("CODE:"):
            self.code_label.setText(result[5:])
            self.code_for_label.setText(f"For: {email}")
            self.result_box.setVisible(True)
            self.gen_status.setText("✓ Code saved · Docker container restarted.")
        elif result.startswith("EXISTING:"):
            self.gen_status.setText("")
            ans = QMessageBox.question(
                self, "Email Already Has a Code",
                f"This email already has an access code:\n\n"
                f"    {result[9:]}\n\nGenerate a brand-new code anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                self.gen_status.setText("Generating replacement code…")
                self.gen_btn.setEnabled(False)
                self.gen_btn.setText("Working…")
                self._run_gen(email, force=True)
        elif result.startswith("ERROR:"):
            self.gen_status.setText(f"Error: {result[6:]}")

    def _copy_code(self):
        code = self.code_label.text()
        if not code:
            return
        QApplication.clipboard().setText(code)
        self.copy_btn.setText("Copied!")
        self.copy_btn.setEnabled(False)
        QTimer.singleShot(2200, lambda: (
            self.copy_btn.setText("Copy"),
            self.copy_btn.setEnabled(True),
        ))

    def _open_manage(self):
        ManageCodesDialog(self).exec()

    # ── Maintenance logic ──────────────────────────────────────────────────────

    def _maint_refresh(self, action: str | None = None):
        if self._maint_worker and self._maint_worker.isRunning():
            return
        self.maint_dot.setStyleSheet("")
        self.maint_label.setText("Checking…")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setText("…")
        self.maint_msg.setText("")
        self._maint_worker = MaintWorker(action)
        self._maint_worker.done.connect(self._on_maint_done)
        self._maint_worker.start()

    def _on_maint_done(self, result: str):
        if result == "ON":
            self._maint_status = True
            self.maint_dot.setStyleSheet("color: #e05555;")
            self.maint_label.setText("Maintenance: ON")
            self.toggle_btn.setText("Turn Off")
            self.toggle_btn.setEnabled(True)
            self.maint_msg.setText("")
        elif result == "OFF":
            self._maint_status = False
            self.maint_dot.setStyleSheet("color: #55aa55;")
            self.maint_label.setText("Maintenance: OFF")
            self.toggle_btn.setText("Turn On")
            self.toggle_btn.setEnabled(True)
            self.maint_msg.setText("")
        elif result.startswith("ERROR:"):
            self._maint_status = None
            self.maint_dot.setStyleSheet("color: #e05555;")
            self.maint_label.setText("Connection failed")
            self.maint_msg.setText(result[6:])
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("—")

    def _toggle_maintenance(self):
        if self._maint_status is None:
            return
        action = "off" if self._maint_status else "on"
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.setText("Working…")
        self.maint_msg.setText(f"Turning maintenance {'ON' if action == 'on' else 'OFF'}…")
        self._maint_refresh(action=action)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    win = XeroAdminWindow()
    win.resize(600, 400)

    screen = app.primaryScreen().availableGeometry()
    win.move(
        screen.x() + (screen.width()  - win.width())  // 2,
        screen.y() + (screen.height() - win.height()) // 2,
    )
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
