"""PyQt6 window for Paired Stops on TopstepX.

Form matches the NT8 AddOn's five rows + two new TP/SL fields. Dark palette
by default. SignalR events arrive on a background thread and are marshalled
into the UI thread via pyqtSignal before any status/state update.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import settings as settings_mod
from .client import Account, TopstepXClient
from .pair_manager import PairManager

logger = logging.getLogger(__name__)


def apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    bg = QColor(30, 30, 30)
    text = QColor(240, 240, 240)
    base = QColor(42, 42, 42)
    hl = QColor(70, 140, 220)
    p.setColor(QPalette.ColorRole.Window, bg)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, bg)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(55, 55, 55))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.Highlight, hl)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    app.setPalette(p)


class PairedStopsWindow(QMainWindow):
    # Emitted when a SignalR order event arrives on a background thread.
    # Slot runs on the Qt main thread via Qt's auto-connection semantics.
    _order_event_signal = pyqtSignal(dict)
    _status_signal = pyqtSignal(str, bool)

    def __init__(self, client: TopstepXClient):
        super().__init__()
        self._client = client
        self._settings = settings_mod.load()
        self._accounts: List[Account] = []
        self._manager: Optional[PairManager] = None

        self.setWindowTitle("Paired Stops — TopstepX")
        self.resize(460, 420)
        if self._settings.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        # --- Form ---
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(6)

        self.account_combo = QComboBox()
        self.instrument_edit = QLineEdit(self._settings.instrument_name)
        self.offset_edit = QLineEdit(f"{self._settings.offset_points:g}")
        self.quantity_edit = QLineEdit(str(self._settings.quantity))
        self.tp_edit = QLineEdit(f"{self._settings.take_profit_points:g}")
        self.sl_edit = QLineEdit(f"{self._settings.stop_loss_points:g}")
        self.always_on_top_cb = QCheckBox()
        self.always_on_top_cb.setChecked(self._settings.always_on_top)

        form.addRow("Account", self.account_combo)
        form.addRow("Instrument", self.instrument_edit)
        form.addRow("Offset (pts)", self.offset_edit)
        form.addRow("Quantity", self.quantity_edit)
        form.addRow("Take Profit (pts)", self.tp_edit)
        form.addRow("Stop Loss (pts)", self.sl_edit)
        form.addRow("Always on top", self.always_on_top_cb)

        layout.addLayout(form)

        # --- Buttons ---
        buttons = QHBoxLayout()
        self.place_btn = QPushButton("Place Paired Stops")
        self.cancel_btn = QPushButton("Cancel Pair")
        buttons.addWidget(self.place_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        layout.addStretch()

        # --- Status strip ---
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # --- Wiring ---
        self.place_btn.clicked.connect(self._on_place)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.account_combo.currentIndexChanged.connect(self._on_account_changed)

        self.instrument_edit.editingFinished.connect(self._persist_settings)
        self.offset_edit.editingFinished.connect(self._persist_settings)
        self.quantity_edit.editingFinished.connect(self._persist_settings)
        self.tp_edit.editingFinished.connect(self._persist_settings)
        self.sl_edit.editingFinished.connect(self._persist_settings)
        self.always_on_top_cb.stateChanged.connect(self._on_always_on_top_toggled)

        # Signals that marshal background-thread events to the UI thread.
        self._order_event_signal.connect(self._handle_order_event_main)
        self._status_signal.connect(self._set_status)

        # --- Init sequence ---
        try:
            self._client.connect()
            self._populate_accounts()
        except Exception as ex:
            QMessageBox.critical(self, "Login failed", str(ex))
            raise

        # Manager reports via the thread-safe signal so it can be called from
        # either thread.
        self._manager = PairManager(
            client=self._client,
            report=lambda msg, err: self._status_signal.emit(msg, err),
        )

        # Subscribe to order events on the selected account. Resubscribe if
        # the user changes account.
        self._subscribe_current_account()

        # Warm up the live-quote subscription for the configured instrument so
        # the first Place click gets a fresh price instead of the minute-bar
        # fallback.
        try:
            symbol = self._settings.instrument_name.strip()
            if symbol:
                contract = self._client.lookup_contract(symbol)
                self._client.subscribe_contract_quotes(contract.id)
        except Exception as ex:
            logger.warning("Could not warm up quote subscription: %s", ex)

        # Session-reset ticker (every 60s on the UI thread).
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(60_000)
        self._session_timer.timeout.connect(self._on_session_tick)
        self._session_timer.start()

    # ------------------------------------------------------------------
    # Account handling
    # ------------------------------------------------------------------
    def _populate_accounts(self) -> None:
        self._accounts = self._client.list_accounts()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        for a in self._accounts:
            self.account_combo.addItem(a.name, userData=a.id)
        self.account_combo.blockSignals(False)
        if self._settings.account_name:
            idx = self.account_combo.findText(self._settings.account_name)
            if idx >= 0:
                self.account_combo.setCurrentIndex(idx)

    def _selected_account_id(self) -> Optional[str]:
        data = self.account_combo.currentData()
        return str(data) if data else None

    def _subscribe_current_account(self) -> None:
        acct_id = self._selected_account_id()
        if not acct_id:
            return
        try:
            self._client.subscribe_order_events(
                account_id=acct_id,
                on_order=lambda ev: self._order_event_signal.emit(ev or {}),
                on_connect=lambda: self._status_signal.emit("SignalR connected.", False),
                on_disconnect=lambda: self._status_signal.emit(
                    "SignalR disconnected — attempting reconnect.", True
                ),
            )
        except Exception as ex:
            self._status_signal.emit(f"SignalR subscribe failed: {ex}", True)

    def _on_always_on_top_toggled(self, _state: int) -> None:
        on = self.always_on_top_cb.isChecked()
        self._settings.always_on_top = on
        settings_mod.save(self._settings)
        # Changing WindowFlags requires re-show. Qt hides the window on
        # setWindowFlag, so we explicitly show() again afterward.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def _on_account_changed(self, _idx: int) -> None:
        acct = self.account_combo.currentText()
        self._settings.account_name = acct
        settings_mod.save(self._settings)
        # Note: signalrcore doesn't expose clean "unsubscribe then resubscribe"
        # for a new account; simplest is that the user restarts the app to
        # switch accounts. Log it so the behavior isn't surprising.
        self._set_status(
            f"Account set to {acct}. Restart the app for the order feed to track this account.",
            False,
        )

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _on_place(self) -> None:
        acct_id = self._selected_account_id()
        if not acct_id:
            self._set_status("No account selected.", True)
            return
        try:
            offset = float(self.offset_edit.text())
            qty = int(self.quantity_edit.text())
            tp_pts = float(self.tp_edit.text())
            sl_pts = float(self.sl_edit.text())
        except ValueError:
            self._set_status("Offset / Quantity / TP / SL must be numbers.", True)
            return

        self._persist_settings()

        assert self._manager is not None
        self._manager.place_pair(
            account_id=acct_id,
            instrument_symbol=self.instrument_edit.text().strip(),
            offset_points=offset,
            quantity=qty,
            tp_points=tp_pts,
            sl_points=sl_pts,
            pair_tag_prefix=self._settings.pair_tag_prefix,
        )

    def _on_cancel(self) -> None:
        assert self._manager is not None
        self._manager.cancel_pair()

    def _on_session_tick(self) -> None:
        if self._manager is not None:
            self._manager.check_session_reset()

    # ------------------------------------------------------------------
    # Signal slots (UI thread)
    # ------------------------------------------------------------------
    @pyqtSlot(dict)
    def _handle_order_event_main(self, event: dict) -> None:
        # Called on the UI thread. The manager is thread-safe; we can call
        # on_order_event from here directly — the signal already marshalled us.
        if self._manager is not None:
            try:
                self._manager.on_order_event(event)
            except Exception as ex:
                logger.exception("Order-event handling failed")
                self._set_status(f"Event error: {ex}", True)

    @pyqtSlot(str, bool)
    def _set_status(self, message: str, is_error: bool) -> None:
        self.status_label.setText(message)
        color = "#e57373" if is_error else "#e0e0e0"   # soft red / off-white
        self.status_label.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _persist_settings(self) -> None:
        self._settings.instrument_name = self.instrument_edit.text().strip()
        try:
            self._settings.offset_points = float(self.offset_edit.text())
        except ValueError:
            pass
        try:
            self._settings.quantity = int(self.quantity_edit.text())
        except ValueError:
            pass
        try:
            self._settings.take_profit_points = float(self.tp_edit.text())
        except ValueError:
            pass
        try:
            self._settings.stop_loss_points = float(self.sl_edit.text())
        except ValueError:
            pass
        settings_mod.save(self._settings)

    def closeEvent(self, event) -> None:
        try:
            self._session_timer.stop()
        except Exception:
            pass
        try:
            self._client.stop()
        except Exception:
            pass
        settings_mod.save(self._settings)
        super().closeEvent(event)
