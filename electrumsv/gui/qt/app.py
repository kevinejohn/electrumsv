# ElectrumSV - lightweight Bitcoin client
# Copyright (C) 2019 ElectrumSV developers
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

'''ElectrumSV application.'''

import datetime
from functools import partial
import signal
import sys
import threading
from typing import Optional

from aiorpcx import run_in_thread
import PyQt5.QtCore as QtCore
from PyQt5.QtCore import pyqtSignal, QObject, QTimer
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QWidget

from electrumsv.app_state import app_state
from electrumsv.contacts import ContactEntry, ContactIdentity
from electrumsv.exceptions import UserCancelled, UserQuit
from electrumsv.i18n import _, set_language
from electrumsv.logs import logs
from electrumsv.wallet import Abstract_Wallet, ParentWallet

from . import dialogs
from .cosigner_pool import CosignerPool
from .main_window import ElectrumWindow
from .exception_window import Exception_Hook
from .installwizard import InstallWizard, GoBack
from .label_sync import LabelSync
from .log_window import SVLogWindow, SVLogHandler
from .network_dialog import NetworkDialog
from .util import ColorScheme, read_QIcon, MessageBox, get_default_language


logger = logs.get_logger('app')


class OpenFileEventFilter(QObject):
    def __init__(self, windows):
        super().__init__()
        self.windows = windows

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.FileOpen:
            if len(self.windows) >= 1:
                self.windows[0].pay_to_URI(event.url().toString())
                return True
        return False


class SVApplication(QApplication):

    # Signals need to be on a QObject
    create_new_window_signal = pyqtSignal(str, object)
    cosigner_received_signal = pyqtSignal(object, object)
    labels_changed_signal = pyqtSignal(object, object)
    window_opened_signal = pyqtSignal(object)
    window_closed_signal = pyqtSignal(object)
    # Async tasks
    async_tasks_done = pyqtSignal()
    # Logging
    new_category = pyqtSignal(str)
    new_log = pyqtSignal(object)
    # Preferences updates
    fiat_ccy_changed = pyqtSignal()
    custom_fee_changed = pyqtSignal()
    op_return_enabled_changed = pyqtSignal()
    num_zeros_changed = pyqtSignal()
    base_unit_changed = pyqtSignal()
    fiat_history_changed = pyqtSignal()
    fiat_balance_changed = pyqtSignal()
    update_check_signal = pyqtSignal(bool, object)
    # Contact events
    contact_added_signal = pyqtSignal(object, object)
    contact_removed_signal = pyqtSignal(object)
    identity_added_signal = pyqtSignal(object, object)
    identity_removed_signal = pyqtSignal(object, object)

    def __init__(self, argv):
        super().__init__(argv)
        self.windows = []
        self.log_handler = SVLogHandler()
        self.log_window = None
        self.net_dialog = None
        self.timer = QTimer()
        self.exception_hook = None
        # A floating point number, e.g. 129.1
        self.dpi = self.primaryScreen().physicalDotsPerInch()

        # init tray
        self.dark_icon = app_state.config.get("dark_icon", False)
        self.tray = QSystemTrayIcon(self._tray_icon(), None)
        self.tray.setToolTip('ElectrumSV')
        self.tray.activated.connect(self._tray_activated)
        self._build_tray_menu()
        self.tray.show()

        # FIXME Fix what.. what needs to be fixed here?
        set_language(app_state.config.get('language', get_default_language()))

        logs.add_handler(self.log_handler)
        self._start()

    def _start(self):
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_X11InitThreads)
        if hasattr(QtCore.Qt, "AA_ShareOpenGLContexts"):
            QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
        if hasattr(QGuiApplication, 'setDesktopFileName'):
            QGuiApplication.setDesktopFileName('electrum-sv.desktop')
        self.setWindowIcon(read_QIcon("electrum-sv.png"))
        self.installEventFilter(OpenFileEventFilter(self.windows))
        self.create_new_window_signal.connect(self.start_new_window)
        self.async_tasks_done.connect(app_state.async_.run_pending_callbacks)
        self.num_zeros_changed.connect(partial(self._signal_all, 'on_num_zeros_changed'))
        self.fiat_ccy_changed.connect(partial(self._signal_all, 'on_fiat_ccy_changed'))
        self.base_unit_changed.connect(partial(self._signal_all, 'on_base_unit_changed'))
        self.fiat_history_changed.connect(partial(self._signal_all, 'on_fiat_history_changed'))
        # Toggling of showing addresses in the fiat preferences.
        self.fiat_balance_changed.connect(partial(self._signal_all, 'on_fiat_balance_changed'))
        self.update_check_signal.connect(partial(self._signal_all, 'on_update_check'))
        ColorScheme.update_from_widget(QWidget())

    def _signal_all(self, method, *args):
        for window in self.windows:
            getattr(window, method)(*args)

    def _close(self):
        for window in self.windows:
            window.close()

    def close_window(self, window):
        app_state.daemon.stop_wallet_at_path(window.parent_wallet.get_storage_path())
        self.windows.remove(window)
        self.window_closed_signal.emit(window)
        self._build_tray_menu()
        # save wallet path of last open window
        if not self.windows:
            app_state.config.save_last_wallet(window.parent_wallet)
            self._last_window_closed()

    def _build_tray_menu(self):
        # Avoid immediate GC of old menu when window closed via its action
        if self.tray.contextMenu() is None:
            m = QMenu()
            self.tray.setContextMenu(m)
        else:
            m = self.tray.contextMenu()
            m.clear()
        for window in self.windows:
            submenu = m.addMenu(window.parent_wallet.name())
            submenu.addAction(_("Show/Hide"), window.show_or_hide)
            submenu.addAction(_("Close"), window.close)
        m.addAction(_("Dark/Light"), self._toggle_tray_icon)
        m.addSeparator()
        m.addAction(_("Exit ElectrumSV"), self._close)
        self.tray.setContextMenu(m)

    def _tray_icon(self):
        if self.dark_icon:
            return read_QIcon('electrumsv_dark_icon.png')
        else:
            return read_QIcon('electrumsv_light_icon.png')

    def _toggle_tray_icon(self):
        self.dark_icon = not self.dark_icon
        app_state.config.set_key("dark_icon", self.dark_icon, True)
        self.tray.setIcon(self._tray_icon())

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            if all([w.is_hidden() for w in self.windows]):
                for w in self.windows:
                    w.bring_to_top()
            else:
                for w in self.windows:
                    w.hide()

    def new_window(self, path, uri=None):
        # Use a signal as can be called from daemon thread
        self.create_new_window_signal.emit(path, uri)

    def show_network_dialog(self, parent):
        if not app_state.daemon.network:
            parent.show_warning(_('You are using ElectrumSV in offline mode; restart '
                                  'ElectrumSV if you want to get connected'), title=_('Offline'))
            return
        if self.net_dialog:
            self.net_dialog.on_update()
            self.net_dialog.show()
            self.net_dialog.raise_()
            return
        self.net_dialog = NetworkDialog(app_state.daemon.network, app_state.config)
        self.net_dialog.show()

    def show_log_viewer(self):
        if self.log_window is None:
            self.log_window = SVLogWindow(None, self.log_handler)
        self.log_window.show()

    def _last_window_closed(self):
        for dialog in (self.net_dialog, self.log_window):
            if dialog:
                dialog.accept()

    def _maybe_choose_server(self):
        # Show network dialog if config does not exist
        if app_state.daemon.network and app_state.config.get('auto_connect') is None:
            try:
                wizard = InstallWizard()
                wizard.init_network(app_state.daemon.network)
                wizard.terminate()
            except Exception as e:
                if not isinstance(e, (UserCancelled, GoBack)):
                    logger.exception("")
                self.quit()

    def on_label_change(self, wallet: Abstract_Wallet, name: str, text: str) -> None:
        self.label_sync.set_label(wallet, name, text)

    def _create_window_for_wallet(self, parent_wallet: ParentWallet):
        w = ElectrumWindow(parent_wallet)
        self.windows.append(w)
        self._build_tray_menu()
        self._register_wallet_events(parent_wallet)
        self.window_opened_signal.emit(w)
        return w

    def _register_wallet_events(self, wallet: ParentWallet) -> None:
        wallet.contacts._on_contact_added = self._on_contact_added
        wallet.contacts._on_contact_removed = self._on_contact_removed
        wallet.contacts._on_identity_added = self._on_identity_added
        wallet.contacts._on_identity_removed = self._on_identity_removed

    def _on_identity_added(self, contact: ContactEntry, identity: ContactIdentity) -> None:
        self.identity_added_signal.emit(contact, identity)

    def _on_identity_removed(self, contact: ContactEntry, identity: ContactIdentity) -> None:
        self.identity_removed_signal.emit(contact, identity)

    def _on_contact_added(self, contact: ContactEntry, identity: ContactIdentity) -> None:
        self.contact_added_signal.emit(contact, identity)

    def _on_contact_removed(self, contact: ContactEntry) -> None:
        self.contact_removed_signal.emit(contact)

    def get_wallet_window(self, path: str) -> Optional[ElectrumWindow]:
        for w in self.windows:
            if w.parent_wallet.get_storage_path() == path:
                return w

    def get_wallet_window_by_id(self, wallet_id: int) -> Optional[ElectrumWindow]:
        for w in self.windows:
            for child_wallet in w.parent_wallet.get_child_wallets():
                if child_wallet.get_id() == wallet_id:
                    return w

    def start_new_window(self, path, uri, is_startup=False):
        '''Raises the window for the wallet if it is open.  Otherwise
        opens the wallet and creates a new window for it.'''
        for w in self.windows:
            if w.parent_wallet.get_storage_path() == path:
                w.bring_to_top()
                break
        else:
            try:
                parent_wallet = app_state.daemon.load_wallet(path, None)
                if not parent_wallet:
                    wizard = InstallWizard()
                    try:
                        if wizard.select_storage(path, is_startup=is_startup):
                            parent_wallet = wizard.run_and_get_wallet()
                    except UserQuit:
                        pass
                    except UserCancelled:
                        pass
                    except GoBack as e:
                        logger.error('[start_new_window] Exception caught (GoBack) %s', e)
                    finally:
                        wizard.terminate()
                    if not parent_wallet:
                        return
                    app_state.daemon.start_wallet(parent_wallet)
            except Exception as e:
                logger.exception("")
                error_str = str(e)
                if '2fa' in error_str:
                    error_str = _('2FA wallets are not supported')
                msg = '\n'.join((_('Cannot load wallet "{}"').format(path), error_str))
                MessageBox.show_error(msg)
                return
            w = self._create_window_for_wallet(parent_wallet)
        if uri:
            w.pay_to_URI(uri)
        w.bring_to_top()
        w.setWindowState(w.windowState() & ~QtCore.Qt.WindowMinimized | QtCore.Qt.WindowActive)

        # this will activate the window
        w.activateWindow()
        return w

    def update_check(self) -> None:
        if (not app_state.config.get('check_updates', True) or
                app_state.config.get("offline", False)):
            return

        def f():
            import requests
            try:
                response = requests.request(
                    'GET', "https://electrumsv.io/release.json",
                    headers={'User-Agent' : 'ElectrumSV'}, timeout=10)
                result = response.json()
                self._on_update_check(True, result)
            except Exception:
                self._on_update_check(False, sys.exc_info())

        t = threading.Thread(target=f)
        t.setDaemon(True)
        t.start()

    def _on_update_check(self, success: bool, result: dict) -> None:
        if success:
            when_checked = datetime.datetime.now().astimezone().isoformat()
            app_state.config.set_key('last_update_check', result)
            app_state.config.set_key('last_update_check_time', when_checked, True)
        self.update_check_signal.emit(success, result)

    def initial_dialogs(self) -> None:
        '''Suppressible dialogs that are shown when first opening the app.'''
        dialogs.show_named('welcome-ESV-1.3.0a1')
        # This needs to be reworked or removed, as non-advanced users aren't sure whether
        # it is safe, and likely many people aren't quite sure if it should be done.
        # old_items = []
        # headers_path = os.path.join(app_state.config.path, 'blockchain_headers')
        # if os.path.exists(headers_path):
        #     old_items.append((_('the file "blockchain_headers"'), os.remove, headers_path))
        # forks_dir = os.path.join(app_state.config.path, 'forks')
        # if os.path.exists(forks_dir):
        #     old_items.append((_('the directory "forks/"'), shutil.rmtree, forks_dir))
        # if old_items:
        #     main_text = _('Delete the following obsolete items in <br>{}?'
        #                   .format(app_state.config.path))
        #     info_text = '<ul>{}</ul>'.format(''.join('<li>{}</li>'.format(text)
        #                                              for text, *rest in old_items))
        #     if dialogs.show_named('delete-obsolete-headers', main_text=main_text,
        #                           info_text=info_text):
        #         try:
        #             for _text, rm_func, *args in old_items:
        #                 rm_func(*args)
        #         except OSError as e:
        #             logger.exception('deleting obsolete files')
        #             dialogs.error_dialog(_('Error deleting files:'), info_text=str(e))

    def event_loop_started(self) -> None:
        self.cosigner_pool = CosignerPool()
        self.label_sync = LabelSync()
        if app_state.config.get("show_crash_reporter", default=True):
            self.exception_hook = Exception_Hook(self)
        self.timer.start()
        signal.signal(signal.SIGINT, lambda *args: self.quit())
        self.initial_dialogs()
        self._maybe_choose_server()
        app_state.config.open_last_wallet()
        path = app_state.config.get_wallet_path()
        if not self.start_new_window(path, app_state.config.get('url'), is_startup=True):
            self.quit()

    def run_app(self) -> None:
        when_started = datetime.datetime.now().astimezone().isoformat()
        app_state.config.set_key('previous_start_time', app_state.config.get("start_time"))
        app_state.config.set_key('start_time', when_started, True)
        self.update_check()

        threading.current_thread().setName('GUI')
        self.timer.setSingleShot(False)
        self.timer.setInterval(500)  # msec
        self.timer.timeout.connect(app_state.device_manager.timeout_clients)

        QTimer.singleShot(0, self.event_loop_started)
        self.exec_()

        logs.remove_handler(self.log_handler)
        # Shut down the timer cleanly
        self.timer.stop()
        # clipboard persistence
        # see http://www.mail-archive.com/pyqt@riverbankcomputing.com/msg17328.html
        event = QtCore.QEvent(QtCore.QEvent.Clipboard)
        self.sendEvent(self.clipboard(), event)
        self.tray.hide()

    def run_coro(self, coro, *args, on_done=None):
        '''Run a coroutine.  on_done, if given, is passed the future containing the reuslt or
        exception, and is guaranteed to be called in the context of the GUI thread.
        '''
        def task_done(future):
            self.async_tasks_done.emit()

        future = app_state.async_.spawn(coro, *args, on_done=on_done)
        future.add_done_callback(task_done)
        return future

    def run_in_thread(self, func, *args, on_done=None):
        '''Run func(*args) in a thread.  on_done, if given, is passed the future containing the
        reuslt or exception, and is guaranteed to be called in the context of the GUI
        thread.
        '''
        return self.run_coro(run_in_thread, func, *args, on_done=on_done)
