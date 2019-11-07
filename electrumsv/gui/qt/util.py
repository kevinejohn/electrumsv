import os.path
from functools import partial, lru_cache
from typing import Optional, Any

from PyQt5.QtCore import Qt, QCoreApplication, QLocale, QTimer, QModelIndex
from PyQt5.QtGui import QFont, QCursor, QIcon, QColor, QPalette
from PyQt5.QtWidgets import (
    QPushButton, QLabel, QMessageBox, QHBoxLayout, QDialog, QVBoxLayout, QLineEdit, QGroupBox,
    QRadioButton, QFileDialog, QStyledItemDelegate, QTreeWidget, QButtonGroup, QComboBox,
    QHeaderView, QWidget, QStyle, QToolButton, QToolTip, QPlainTextEdit, QTreeWidgetItem,
)
from PyQt5.uic import loadUi

from electrumsv.app_state import app_state
from electrumsv.i18n import _, languages
from electrumsv.paymentrequest import PR_UNPAID, PR_PAID, PR_EXPIRED
from electrumsv.util import resource_path


dialogs = []

pr_icons = {
    PR_UNPAID: "unpaid.png",
    PR_PAID: "icons8-checkmark-green-52.png",
    PR_EXPIRED: "expired.png"
}

pr_tooltips = {
    PR_UNPAID:_('Pending'),
    PR_PAID:_('Paid'),
    PR_EXPIRED:_('Expired')
}

expiration_values = [
    (_('1 hour'), 60*60),
    (_('1 day'), 24*60*60),
    (_('1 week'), 7*24*60*60),
    (_('Never'), None)
]


class EnterButton(QPushButton):
    def __init__(self, text, func):
        QPushButton.__init__(self, text)
        self.func = func
        self.clicked.connect(func)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Return:
            self.func()


class WWLabel(QLabel):
    def __init__ (self, text="", parent=None):
        QLabel.__init__(self, text, parent)
        self.setWordWrap(True)


class HelpLabel(QLabel):

    def __init__(self, text, help_text):
        QLabel.__init__(self, text)
        self.app = QCoreApplication.instance()
        self.font = QFont()
        self.set_help_text(help_text)

    def set_help_text(self, help_text):
        self.help_text = help_text

    def mouseReleaseEvent(self, x):
        QMessageBox.information(self, 'Help', self.help_text)

    def enterEvent(self, event):
        self.font.setUnderline(True)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.PointingHandCursor))
        return QLabel.enterEvent(self, event)

    def leaveEvent(self, event):
        self.font.setUnderline(False)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.ArrowCursor))
        return QLabel.leaveEvent(self, event)


class HelpButton(QPushButton):
    def __init__(self, text, textFormat=Qt.AutoText, title="Help"):
        self.textFormat = textFormat
        self.title = title
        QPushButton.__init__(self, '?')
        self.help_text = text
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(20)
        self.clicked.connect(self.onclick)

    def onclick(self):
        b = QMessageBox()
        b.setIcon(QMessageBox.Information)
        b.setTextFormat(self.textFormat)
        b.setText(self.help_text)
        b.setWindowTitle(self.title)
        b.exec()

class Buttons(QHBoxLayout):
    def __init__(self, *buttons):
        QHBoxLayout.__init__(self)
        self.addStretch(1)
        for b in buttons:
            self.addWidget(b)

class CloseButton(QPushButton):
    def __init__(self, dialog):
        QPushButton.__init__(self, _("Close"))
        self.clicked.connect(dialog.accept)
        self.setDefault(True)

class CopyButton(QPushButton):
    def __init__(self, text_getter, app):
        QPushButton.__init__(self, _("Copy"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))

class CopyCloseButton(QPushButton):
    def __init__(self, text_getter, app, dialog):
        QPushButton.__init__(self, _("Copy and Close"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))
        self.clicked.connect(dialog.close)
        self.setDefault(True)

class OkButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("OK"))
        self.clicked.connect(dialog.accept)
        self.setDefault(True)

class CancelButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("Cancel"))
        self.clicked.connect(dialog.reject)

class MessageBoxMixin(object):
    def top_level_window_recurse(self, window=None):
        window = window or self
        classes = (WindowModalDialog, QMessageBox)
        for n, child in enumerate(window.children()):
            # Test for visibility as old closed dialogs may not be GC-ed
            if isinstance(child, classes) and child.isVisible():
                return self.top_level_window_recurse(child)
        return window

    def top_level_window(self):
        return self.top_level_window_recurse()

    def question(self, msg, parent=None, title=None, icon=None):
        Yes, No = QMessageBox.Yes, QMessageBox.No
        return self.msg_box(icon or QMessageBox.Question,
                            parent, title or '',
                            msg, buttons=Yes|No, defaultButton=No) == Yes

    def show_warning(self, msg, parent=None, title=None):
        return self.msg_box(QMessageBox.Warning, parent,
                            title or _('Warning'), msg)

    def show_error(self, msg, parent=None):
        return self.msg_box(QMessageBox.Warning, parent,
                            _('Error'), msg)

    def show_critical(self, msg, parent=None, title=None):
        return self.msg_box(QMessageBox.Critical, parent,
                            title or _('Critical Error'), msg)

    def show_message(self, msg, parent=None, title=None):
        return self.msg_box(QMessageBox.Information, parent,
                            title or _('Information'), msg)

    def msg_box(self, icon, parent, title, text, buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.NoButton):
        parent = parent or self.top_level_window()
        d = QMessageBox(icon, title, str(text), buttons, parent)
        d.setWindowModality(Qt.WindowModal)
        d.setDefaultButton(defaultButton)
        return d.exec_()


class MessageBox:

    @classmethod
    def question(cls, msg, parent=None, title=None, icon=None):
        Yes, No = QMessageBox.Yes, QMessageBox.No
        return cls.msg_box(icon or QMessageBox.Question, parent, title or '',
                           msg, buttons=Yes|No, defaultButton=No) == Yes

    @classmethod
    def show_warning(cls, msg, parent=None, title=None):
        return cls.msg_box(QMessageBox.Warning, parent, title or _('Warning'), msg)

    @classmethod
    def show_error(cls, msg, parent=None, title=None):
        return cls.msg_box(QMessageBox.Warning, parent, title or _('Error'), msg)

    @classmethod
    def msg_box(cls, icon, parent, title, text, buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.NoButton):
        d = QMessageBox(icon, title, str(text), buttons, parent)
        d.setDefaultButton(defaultButton)
        return d.exec_()


class UntrustedMessageDialog(QDialog):
    def __init__(self, parent, title, description, exception):
        QDialog.__init__(self, parent)
        self.setWindowTitle(title)
        self.setMinimumSize(500, 280)
        self.setMaximumSize(1000, 400)
        vbox = QVBoxLayout(self)
        text_label = QLabel(description)
        text_label.setWordWrap(True)
        vbox.addWidget(text_label)
        text_label = QLabel(_(
            "The server returned the following message, which may or may not help describe "
            "the problem.  A malicious server may return misleading messages, so act on it "
            "at your own risk.  In particular, do not download software from any links "
            "provided; the official ElectrumSV website is only https://electrumsv.io/."
        ))
        text_label.setWordWrap(True)
        vbox.addWidget(text_label)
        text_edit = QPlainTextEdit(str(exception))
        text_edit.setEnabled(False)
        text_edit.setMaximumHeight(100)
        vbox.addWidget(text_edit)
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CloseButton(self)))
        self.setLayout(vbox)

class WindowModalDialog(QDialog, MessageBoxMixin):
    '''Handy wrapper; window modal dialogs are better for our multi-window
    daemon model as other wallet windows can still be accessed.'''
    def __init__(self, parent, title=None):
        QDialog.__init__(self, parent)
        self.setWindowModality(Qt.WindowModal)
        if title:
            self.setWindowTitle(title)


class WaitingDialog(WindowModalDialog):
    '''Shows a please wait dialog whilst runnning a task.  It is not
    necessary to maintain a reference to this dialog.'''
    def __init__(self, parent, message, func, *args, on_done=None):
        assert parent
        super().__init__(parent, _("Please wait"))
        vbox = QVBoxLayout(self)
        vbox.addWidget(QLabel(message))

        def _on_done(future):
            self.accept()
            on_done(future)
        future = app_state.app.run_in_thread(func, *args, on_done=_on_done)
        self.accepted.connect(future.cancel)
        self.show()


def line_dialog(parent, title, label, ok_label, default=None):
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(500)
    l = QVBoxLayout()
    dialog.setLayout(l)
    l.addWidget(QLabel(label))
    txt = QLineEdit()
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.text()

def text_dialog(parent, title, label, ok_label, default=None, allow_multi=False):
    from .qrtextedit import ScanQRTextEdit
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(500)
    l = QVBoxLayout()
    dialog.setLayout(l)
    l.addWidget(QLabel(label))
    txt = ScanQRTextEdit(allow_multi=allow_multi)
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.toPlainText()

class ChoicesLayout(object):
    def __init__(self, msg, choices, on_clicked=None, checked_index=0):
        vbox = QVBoxLayout()
        if len(msg) > 50:
            vbox.addWidget(WWLabel(msg))
            msg = ""
        gb2 = QGroupBox(msg)
        vbox.addWidget(gb2)

        vbox2 = QVBoxLayout()
        gb2.setLayout(vbox2)

        self.group = group = QButtonGroup()
        for i,c in enumerate(choices):
            button = QRadioButton(gb2)
            button.setText(c)
            vbox2.addWidget(button)
            group.addButton(button)
            group.setId(button, i)
            if i==checked_index:
                button.setChecked(True)

        if on_clicked:
            group.buttonClicked.connect(partial(on_clicked, self))

        self.vbox = vbox

    def layout(self):
        return self.vbox

    def selected_index(self):
        return self.group.checkedId()

def address_combo(addresses):
    addr_combo = QComboBox()
    addr_combo.addItems(addr.to_string() for addr in addresses)
    addr_combo.setCurrentIndex(0)

    hbox = QHBoxLayout()
    hbox.addWidget(QLabel(_('Address to sweep to:')))
    hbox.addWidget(addr_combo)
    return hbox, addr_combo


def filename_field(config, defaultname, select_msg):
    vbox = QVBoxLayout()
    gb = QGroupBox(_("Format"))
    gbox = QHBoxLayout()
    b1 = QRadioButton(gb)
    b1.setText(_("CSV"))
    b1.setChecked(True)
    b2 = QRadioButton(gb)
    b2.setText(_("JSON"))
    gbox.addWidget(b1)
    gbox.addWidget(b2)
    gb.setLayout(gbox)
    vbox.addWidget(gb)

    hbox = QHBoxLayout()

    directory = config.get('io_dir', os.path.expanduser('~'))
    path = os.path.join( directory, defaultname )
    filename_e = QLineEdit()
    filename_e.setText(path)

    def func():
        text = filename_e.text()
        _filter = ("*.csv" if text.endswith(".csv") else
                   "*.json" if text.endswith(".json") else
                   None)
        p, __ = QFileDialog.getSaveFileName(None, select_msg, text, _filter)
        if p:
            filename_e.setText(p)

    button = QPushButton(_('File'))
    button.clicked.connect(func)
    hbox.addWidget(button)
    hbox.addWidget(filename_e)
    vbox.addLayout(hbox)

    def set_csv(v):
        text = filename_e.text()
        text = text.replace(".json",".csv") if v else text.replace(".csv",".json")
        filename_e.setText(text)

    b1.clicked.connect(lambda: set_csv(True))
    b2.clicked.connect(lambda: set_csv(False))

    return vbox, filename_e, b1

class ElectrumItemDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        return self.parent().createEditor(parent, option, index)

class MyTreeWidget(QTreeWidget):

    def __init__(self, parent, create_menu, headers, stretch_column=None,
                 editable_columns=None):
        QTreeWidget.__init__(self, parent)
        self.parent = parent
        self.config = self.parent.config
        self.stretch_column = stretch_column
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(create_menu)
        self.setUniformRowHeights(True)
        # extend the syntax for consistency
        self.addChild = self.addTopLevelItem
        self.insertChild = self.insertTopLevelItem

        # Control which columns are editable
        self.editor = None
        self.pending_update = False
        if editable_columns is None:
            editable_columns = [stretch_column]
        self.editable_columns = editable_columns
        self.setItemDelegate(ElectrumItemDelegate(self))
        self.itemDoubleClicked.connect(self.on_doubleclick)
        self.update_headers(headers)
        self.current_filter = ""

    def update_headers(self, headers):
        self.setColumnCount(len(headers))
        self.setHeaderLabels(headers)
        self.header().setStretchLastSection(False)
        for col in range(len(headers)):
            sm = (QHeaderView.Stretch if col == self.stretch_column
                  else QHeaderView.ResizeToContents)
            self.header().setSectionResizeMode(col, sm)

    def editItem(self, item, column):
        if column in self.editable_columns:
            self.editing_itemcol = (item, column, item.text(column))
            # Calling setFlags causes on_changed events for some reason
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            QTreeWidget.editItem(self, item, column)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)

    def keyPressEvent(self, event):
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ] and self.editor is None:
            self.on_activated(self.currentItem(), self.currentColumn())
        else:
            QTreeWidget.keyPressEvent(self, event)

    def permit_edit(self, item, column):
        return (column in self.editable_columns
                and self.on_permit_edit(item, column))

    def on_permit_edit(self, item, column):
        return True

    def on_doubleclick(self, item, column):
        if self.permit_edit(item, column):
            self.editItem(item, column)

    def on_activated(self, item, column):
        # on 'enter' we show the menu
        pt = self.visualItemRect(item).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    def createEditor(self, parent, option, index):
        self.editor = QStyledItemDelegate.createEditor(self.itemDelegate(),
                                                       parent, option, index)
        self.editor.editingFinished.connect(self.editing_finished)
        return self.editor

    def editing_finished(self):
        # Long-time QT bug - pressing Enter to finish editing signals
        # editingFinished twice.  If the item changed the sequence is
        # Enter key:  editingFinished, on_change, editingFinished
        # Mouse: on_change, editingFinished
        # This mess is the cleanest way to ensure we make the
        # on_edited callback with the updated item
        if self.editor:
            (item, column, prior_text) = self.editing_itemcol
            if self.editor.text() == prior_text:
                self.editor = None  # Unchanged - ignore any 2nd call
            elif item.text(column) == prior_text:
                pass # Buggy first call on Enter key, item not yet updated
            else:
                # What we want - the updated item
                self.on_edited(*self.editing_itemcol)
                self.editor = None

            # Now do any pending updates
            if self.editor is None and self.pending_update:
                self.pending_update = False
                self.on_update()

    def on_edited(self, item, column, prior):
        '''Called only when the text actually changes'''
        wallet_id, key = item.data(0, Qt.UserRole)
        text = item.text(column)
        wallet = self.parent.parent_wallet.get_wallet_for_account(wallet_id)
        wallet.set_label(key, text)
        self.parent.history_view.update_tx_labels()

    def update(self):
        # Defer updates if editing
        if self.editor:
            self.pending_update = True
        else:
            self.setUpdatesEnabled(False)
            scroll_pos_val = self.verticalScrollBar().value() # save previous scroll bar position
            self.on_update()
            def restoreScrollBar():
                self.updateGeometry()
                self.verticalScrollBar().setValue(scroll_pos_val) # restore scroll bar to previous
                self.setUpdatesEnabled(True)
            # need to do this from a timer some time later due to Qt quirks
            QTimer.singleShot(1.0, restoreScrollBar)
        if self.current_filter:
            self.filter(self.current_filter)

    def on_update(self):
        pass

    def get_leaves(self, root):
        child_count = root.childCount()
        if child_count == 0:
            yield root
        for i in range(child_count):
            item = root.child(i)
            for x in self.get_leaves(item):
                yield x

    def filter(self, p):
        columns = self.__class__.filter_columns
        p = p.lower()
        self.current_filter = p
        for item in self.get_leaves(self.invisibleRootItem()):
            item.setHidden(all([item.text(column).lower().find(p) == -1
                                for column in columns]))


class ButtonsWidget(QWidget):

    def __init__(self):
        super().__init__()
        self.buttons = []

    def resizeButtons(self):
        frameWidth = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth)
        x = self.rect().right() - frameWidth
        y = self.rect().top() + frameWidth
        for button in self.buttons:
            sz = button.sizeHint()
            x -= sz.width()
            button.move(x, y)

    def addButton(self, icon_name, on_click, tooltip):
        button = QToolButton(self)
        button.setIcon(read_QIcon(icon_name))
        button.setStyleSheet("QToolButton { border: none; hover {border: 1px} "
                             "pressed {border: 1px} padding: 0px; }")
        button.setVisible(True)
        button.setToolTip(tooltip)
        button.setCursor(QCursor(Qt.PointingHandCursor))
        button.clicked.connect(on_click)
        self.buttons.append(button)
        return button

    def addCopyButton(self, app):
        self.app = app
        self.addButton("copy.png", self.on_copy, _("Copy to clipboard"))

    def on_copy(self):
        self.app.clipboard().setText(self.text())
        QToolTip.showText(QCursor.pos(), _("Text copied to clipboard"), self)

class ButtonsLineEdit(QLineEdit, ButtonsWidget):
    def __init__(self, text=''):
        QLineEdit.__init__(self, text, None)
        self.buttons = []

    def resizeEvent(self, e):
        o = QLineEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o

class ButtonsTextEdit(QPlainTextEdit, ButtonsWidget):
    def __init__(self, text=None):
        QPlainTextEdit.__init__(self, text)
        self.setText = self.setPlainText
        self.text = self.toPlainText
        self.buttons = []

    def resizeEvent(self, e):
        o = QPlainTextEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o


class ColorSchemeItem:
    def __init__(self, fg_color, bg_color):
        self.colors = (fg_color, bg_color)

    def _get_color(self, background):
        return self.colors[(int(background) + int(ColorScheme.dark_scheme)) % 2]

    def as_stylesheet(self, background=False):
        css_prefix = "background-" if background else ""
        color = self._get_color(background)
        return "QWidget {{ {}color:{}; }}".format(css_prefix, color)

    def as_color(self, background=False):
        color = self._get_color(background)
        return QColor(color)


class ColorScheme:
    dark_scheme = False

    GREEN = ColorSchemeItem("#117c11", "#8af296")
    RED = ColorSchemeItem("#7c1111", "#f18c8c")
    BLUE = ColorSchemeItem("#123b7c", "#8cb3f2")
    DEFAULT = ColorSchemeItem("black", "white")

    @staticmethod
    def has_dark_background(widget):
        brightness = sum(widget.palette().color(QPalette.Background).getRgb()[0:3])
        return brightness < (255*3/2)

    @staticmethod
    def update_from_widget(widget):
        if ColorScheme.has_dark_background(widget):
            ColorScheme.dark_scheme = True


class SortableTreeWidgetItem(QTreeWidgetItem):
    DataRole = Qt.UserRole + 1

    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        self_data = self.data(column, self.DataRole)
        other_data = other.data(column, self.DataRole)
        if None not in (self_data, other_data):
            # We have set custom data to sort by
            return self_data < other_data
        try:
            # Is the value something numeric?
            self_text = self.text(column).replace(',', '')
            other_text = other.text(column).replace(',', '')
            return float(self_text) < float(other_text)
        except ValueError:
            # If not, we will just do string comparison
            return self.text(column) < other.text(column)


def update_fixed_tree_height(tree: QTreeWidget, maximum_height=None):
    # We can't always rely on the manually set maximum height sticking.
    # It's possible the setting of the fixed height explicitly replaces it.
    if maximum_height is None:
        maximum_height = tree.maximumHeight()

    tree_model = tree.model()
    cell_index = tree_model.index(0, 1)
    row_height = tree.rowHeight(cell_index)
    if row_height == 0:
        row_height = tree.header().height()
    row_count = tree_model.rowCount()
    table_height = row_height * row_count
    if maximum_height > 5:
        table_height = min(table_height, maximum_height)
    if tree.header().isVisible:
        table_height += tree.header().height() + 2
    tree.setFixedHeight(table_height)


def protected(func):
    '''Password request wrapper.  The password is passed to the function
    as the 'password' named argument.  "None" indicates either an
    unencrypted wallet, or the user cancelled the password request.
    An empty input is passed as the empty string.'''
    def request_password(self, *args, **kwargs):
        main_window = self
        if 'wallet_id' in kwargs:
            main_window = app_state.app.get_wallet_window_by_id(kwargs['wallet_id'])

        parent = main_window.top_level_window()
        password: Optional[str] = None
        while main_window.parent_wallet.has_password():
            password = main_window.password_dialog(parent=parent)
            if password is None:
                # User cancelled password input
                return
            try:
                main_window.parent_wallet.check_password(password)
                break
            except Exception as e:
                main_window.show_error(str(e), parent=parent)
                continue

        kwargs['password'] = password
        return func(self, *args, **kwargs)
    return request_password


def icon_path(icon_basename):
    return resource_path('icons', icon_basename)

def read_qt_ui(ui_name):
    return loadUi(resource_path("ui", ui_name))

@lru_cache()
def read_QIcon(icon_basename):
    return QIcon(icon_path(icon_basename))

def get_source_index(model_index: QModelIndex, klass: Any):
    model = model_index.model()
    while model is not None and not isinstance(model, klass):
        model_index = model.mapToSource(model_index)
        model = model_index.model()
    return model_index

def get_default_language():
    name = QLocale.system().name()
    return name if name in languages else 'en_UK'
