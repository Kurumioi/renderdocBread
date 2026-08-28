"""IDE-style workspace for reviewed, organised ReplayLab HLSL.

The panel deliberately has no independent filesystem write authority. Its tree
and read-only snapshots come from the fixed live-edit registry. Every save goes
to the authenticated singleton Host, where the exact in-memory live-edit job
owns the source CAS, compile, ABI validation, RenderDoc replacement and
last-good state. There is no direct-disk fallback when that Host is unavailable.

This module remains Python 3.6/PySide2 compatible for qrenderdoc's embedded
interpreter. It is named ``project_*.py`` so all user-visible text is extracted
into qrenderdoc's ordinary Qt translation catalog.
"""

from __future__ import print_function

import hashlib
import importlib.util
import json
import os
import queue
import re
import threading


try:
    from . import shader_console as _shader_console_protocol
    from . import shader_editor_client as _shader_save_client_protocol
except (ImportError, ValueError):
    _shader_console_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_console_protocol",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "shader_console.py"))
    if _shader_console_spec is None or _shader_console_spec.loader is None:
        raise ImportError("Unable to load the ReplayLab Shader Console protocol")
    _shader_console_protocol = importlib.util.module_from_spec(_shader_console_spec)
    _shader_console_spec.loader.exec_module(_shader_console_protocol)
    _shader_client_spec = importlib.util.spec_from_file_location(
        "_replaylab_shader_workspace_save_client",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "shader_editor_client.py"))
    if _shader_client_spec is None or _shader_client_spec.loader is None:
        raise ImportError("Unable to load the ReplayLab Shader save client")
    _shader_save_client_protocol = importlib.util.module_from_spec(
        _shader_client_spec)
    _shader_client_spec.loader.exec_module(_shader_save_client_protocol)


try:
    from PySide2 import QtCore, QtGui, QtWidgets
    HAS_QT = True
except ImportError:  # pragma: no cover - contract tests use the non-Qt surface
    QtCore = None
    QtGui = None
    QtWidgets = None
    HAS_QT = False


POLL_INTERVAL_MS = 2000
FILESYSTEM_COALESCE_MS = 40
SAVE_RESULT_POLL_MS = 30
SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 10.0
MAX_OPEN_DOCUMENTS = 32
SETTINGS_KEY = "ReplayLab/ShaderWorkspace/state-v1"
TOKEN_RE = re.compile(r"^[0-9a-f]{32,64}$")

STAGE_ABBREVIATIONS = {
    "vertex": "VS",
    "hull": "HS",
    "tess_control": "HS",
    "domain": "DS",
    "tess_eval": "DS",
    "geometry": "GS",
    "fragment": "PS",
    "pixel": "PS",
    "compute": "CS",
    "task": "TS",
    "amplification": "AS",
    "mesh": "MS",
}


def _normal_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path or ""))))


def _format_message(translated, *arguments):
    values = tuple(str(value) for value in arguments)

    def _replace(match):
        index = int(match.group(1))
        return values[index - 1] if 0 < index <= len(values) else match.group(0)

    return re.sub(r"%([1-9][0-9]*)", _replace, str(translated))


def _latest_event(events, names):
    selected = None
    accepted = set(names)
    for event in events or []:
        if isinstance(event, dict) and event.get("type") in accepted:
            if selected is None or int(event.get("sequence", 0)) > int(
                    selected.get("sequence", 0)):
                selected = event
    return selected


def _classify_commit_unknown(base_hash, submitted_hash, disk_hash):
    """Classify an observed disk hash after an uncertain Host save."""

    if disk_hash == submitted_hash:
        return "committed"
    if disk_hash == base_hash:
        return "pending"
    return "external"


def _save_worker_loop(save_client, task_queue, result_queue, stop_event):
    """Run complete Host save calls without ever touching a Qt object."""

    while True:
        task = task_queue.get()
        if task is None:
            return
        if stop_event.is_set():
            continue
        pending_request = getattr(save_client, "request_pending", None)
        while callable(pending_request):
            try:
                if not pending_request():
                    break
            except Exception:
                break
            if stop_event.wait(0.05):
                return
        outcome = {"task": task}
        try:
            outcome["result"] = save_client.save(
                task["liveEditId"], task["authorizationId"],
                task["workspaceId"], task["baseHash"], task["sourceText"])
            outcome["kind"] = "saved"
        except Exception as exc:
            if isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveConflict):
                outcome["kind"] = "conflict"
            elif isinstance(
                    exc,
                    _shader_save_client_protocol.ReplayLabShaderSaveUncertain):
                outcome["kind"] = "unknown"
            else:
                outcome["kind"] = "error"
            outcome["error"] = str(exc)
        result_queue.put(outcome)


def _join_save_worker(worker, timeout_seconds, request_pending):
    """Bound shutdown latency and prove that the Python worker has stopped."""

    if worker is None:
        return True
    if worker is threading.current_thread():
        raise RuntimeError(
            "ReplayLab Shader Workspace save worker cannot join itself")
    timeout = max(0.0, float(timeout_seconds))
    worker.join(timeout)
    if not worker.is_alive():
        return True
    try:
        transport_pending = bool(request_pending())
    except Exception:
        transport_pending = True
    # i18n-ignore: developer-only shutdown diagnostic printed by the bridge.
    raise RuntimeError(
        "ReplayLab Shader Workspace save worker did not stop within %.3f seconds "
        "(Host request pending: %s)" %
        (timeout, "yes" if transport_pending else "no"))


if HAS_QT:
    class _WorkspaceRoot(QtWidgets.QWidget):
        def __init__(self, owner):
            super(_WorkspaceRoot, self).__init__()
            self.owner = owner

        @QtCore.Slot(result=bool)
        def checkAllowClose(self):
            # ToolWindowManager queries this exact Qt meta-method before it
            # removes and directly deletes a dock tab; that path does not send
            # QWidget.closeEvent.
            return self.owner is None or self.owner._confirm_panel_close()

        def closeEvent(self, event):
            if self.checkAllowClose():
                event.accept()
                super(_WorkspaceRoot, self).closeEvent(event)
            else:
                event.ignore()


    class _HLSLHighlighter(QtGui.QSyntaxHighlighter):
        def __init__(self, document, palette):
            super(_HLSLHighlighter, self).__init__(document)
            self.rules = []

            keyword_format = QtGui.QTextCharFormat()
            keyword_format.setForeground(QtGui.QColor("#569cd6"))
            keyword_format.setFontWeight(QtGui.QFont.Bold)
            keywords = (
                "break", "case", "cbuffer", "centroid", "column_major", "const",
                "continue", "default", "discard", "do", "else", "export", "extern",
                "false", "for", "groupshared", "if", "in", "inline", "inout",
                "interface", "namespace", "nointerpolation", "out", "precise",
                "return", "row_major", "sample", "shared", "static", "struct",
                "switch", "true", "typedef", "uniform", "volatile", "while",
            )
            self.rules.append((
                QtCore.QRegExp(r"\b(?:%s)\b" % "|".join(keywords)), keyword_format))

            type_format = QtGui.QTextCharFormat()
            type_format.setForeground(QtGui.QColor("#4ec9b0"))
            self.rules.append((QtCore.QRegExp(
                r"\b(?:bool|double|dword|float|half|int|min16float|min16int|"
                r"min16uint|min10float|uint|void)(?:[1-4](?:x[1-4])?)?\b|"
                r"\b(?:Buffer|ByteAddressBuffer|ConsumeStructuredBuffer|InputPatch|"
                r"OutputPatch|RWBuffer|RWByteAddressBuffer|RWStructuredBuffer|"
                r"SamplerComparisonState|SamplerState|StructuredBuffer|Texture[1-3]D|"
                r"TextureCube|RWTexture[1-3]D)(?:Array)?\b"), type_format))

            number_format = QtGui.QTextCharFormat()
            number_format.setForeground(QtGui.QColor("#b5cea8"))
            self.rules.append((QtCore.QRegExp(
                r"\b(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
                r"[fFhHuUlL]*)\b"), number_format))

            string_format = QtGui.QTextCharFormat()
            string_format.setForeground(QtGui.QColor("#ce9178"))
            self.rules.append((QtCore.QRegExp(r'"(?:\\.|[^"\\])*"'), string_format))

            preprocessor_format = QtGui.QTextCharFormat()
            preprocessor_format.setForeground(QtGui.QColor("#c586c0"))
            self.rules.append((QtCore.QRegExp(r"^\s*#[^\n]*"), preprocessor_format))

            semantic_format = QtGui.QTextCharFormat()
            semantic_format.setForeground(QtGui.QColor("#9cdcfe"))
            self.rules.append((QtCore.QRegExp(
                r":\s*[A-Z][A-Z0-9_]*(?:\d+)?\b"), semantic_format))

            self.comment_format = QtGui.QTextCharFormat()
            self.comment_format.setForeground(QtGui.QColor("#6a9955"))
            self.single_comment = QtCore.QRegExp(r"//[^\n]*")
            self.comment_start = QtCore.QRegExp(r"/\*")
            self.comment_end = QtCore.QRegExp(r"\*/")

        def highlightBlock(self, text):
            for expression, text_format in self.rules:
                index = expression.indexIn(text)
                while index >= 0:
                    length = expression.matchedLength()
                    self.setFormat(index, max(length, 1), text_format)
                    index = expression.indexIn(text, index + max(length, 1))

            index = self.single_comment.indexIn(text)
            if index >= 0:
                self.setFormat(index, len(text) - index, self.comment_format)

            self.setCurrentBlockState(0)
            start = 0 if self.previousBlockState() == 1 else self.comment_start.indexIn(text)
            while start >= 0:
                end = self.comment_end.indexIn(text, start + 2)
                if end < 0:
                    self.setCurrentBlockState(1)
                    length = len(text) - start
                else:
                    length = end - start + self.comment_end.matchedLength()
                self.setFormat(start, length, self.comment_format)
                if end < 0:
                    break
                start = self.comment_start.indexIn(text, start + length)


    class _LineNumberArea(QtWidgets.QWidget):
        def __init__(self, editor):
            super(_LineNumberArea, self).__init__(editor)
            self.editor = editor

        def sizeHint(self):
            return QtCore.QSize(self.editor.line_number_width(), 0)

        def paintEvent(self, event):
            self.editor.paint_line_numbers(event)


    class _HLSLEditor(QtWidgets.QPlainTextEdit):
        saveRequested = QtCore.Signal()

        def __init__(self, parent=None):
            super(_HLSLEditor, self).__init__(parent)
            self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            self.setTabStopWidth(4 * QtGui.QFontMetrics(
                QtGui.QFontDatabase.systemFont(
                    QtGui.QFontDatabase.FixedFont)).horizontalAdvance(" "))
            self.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            self.setCenterOnScroll(True)
            self.line_area = _LineNumberArea(self)
            self.blockCountChanged.connect(self._update_line_area_width)
            self.updateRequest.connect(self._update_line_area)
            self.cursorPositionChanged.connect(self._update_selections)
            self._diagnostic_lines = {}
            self._highlighter = _HLSLHighlighter(self.document(), self.palette())
            self._update_line_area_width(0)

        def line_number_width(self):
            digits = max(2, len(str(max(1, self.blockCount()))))
            return 12 + self.fontMetrics().horizontalAdvance("9") * digits

        def _update_line_area_width(self, _count):
            self.setViewportMargins(self.line_number_width(), 0, 0, 0)

        def _update_line_area(self, rect, dy):
            if dy:
                self.line_area.scroll(0, dy)
            else:
                self.line_area.update(0, rect.y(), self.line_area.width(), rect.height())
            if rect.contains(self.viewport().rect()):
                self._update_line_area_width(0)

        def resizeEvent(self, event):
            super(_HLSLEditor, self).resizeEvent(event)
            contents = self.contentsRect()
            self.line_area.setGeometry(QtCore.QRect(
                contents.left(), contents.top(), self.line_number_width(), contents.height()))

        def paint_line_numbers(self, event):
            painter = QtGui.QPainter(self.line_area)
            painter.fillRect(event.rect(), self.palette().alternateBase())
            block = self.firstVisibleBlock()
            number = block.blockNumber()
            top = int(self.blockBoundingGeometry(block).translated(
                self.contentOffset()).top())
            bottom = top + int(self.blockBoundingRect(block).height())
            painter.setPen(self.palette().color(QtGui.QPalette.PlaceholderText))
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible() and bottom >= event.rect().top():
                    painter.drawText(
                        0, top, self.line_area.width() - 5, self.fontMetrics().height(),
                        QtCore.Qt.AlignRight, str(number + 1))
                block = block.next()
                top = bottom
                bottom = top + int(self.blockBoundingRect(block).height())
                number += 1

        def set_diagnostic_lines(self, problems):
            values = {}
            for problem in problems or []:
                line = int(problem.get("line", 0) or 0)
                severity = str(problem.get("severity", "error") or "error")
                if line > 0 and (line not in values or severity == "error"):
                    values[line] = severity
            self._diagnostic_lines = values
            self._update_selections()

        def _update_selections(self):
            selections = []
            if not self.isReadOnly():
                current = QtWidgets.QTextEdit.ExtraSelection()
                current.format.setBackground(self.palette().alternateBase())
                current.format.setProperty(QtGui.QTextFormat.FullWidthSelection, True)
                current.cursor = self.textCursor()
                current.cursor.clearSelection()
                selections.append(current)
            document = self.document()
            for line, severity in sorted(self._diagnostic_lines.items()):
                block = document.findBlockByNumber(line - 1)
                if not block.isValid():
                    continue
                selection = QtWidgets.QTextEdit.ExtraSelection()
                color = QtGui.QColor("#5a1d1d" if severity == "error" else "#5a4b16")
                color.setAlpha(110)
                selection.format.setBackground(color)
                selection.format.setProperty(QtGui.QTextFormat.FullWidthSelection, True)
                selection.cursor = QtGui.QTextCursor(block)
                selections.append(selection)
            self.setExtraSelections(selections)

        def go_to_line(self, line, column=0):
            block = self.document().findBlockByNumber(max(0, int(line) - 1))
            if not block.isValid():
                return
            cursor = QtGui.QTextCursor(block)
            if int(column or 0) > 1:
                cursor.movePosition(
                    QtGui.QTextCursor.Right, QtGui.QTextCursor.MoveAnchor,
                    int(column) - 1)
            self.setTextCursor(cursor)
            self.centerCursor()
            self.setFocus(QtCore.Qt.OtherFocusReason)

        def keyPressEvent(self, event):
            if event.matches(QtGui.QKeySequence.Save):
                self.saveRequested.emit()
                event.accept()
                return
            super(_HLSLEditor, self).keyPressEvent(event)


    def _pin_icon(palette, pinned):
        pixmap = QtGui.QPixmap(16, 16)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.translate(8.0, 7.5)
        color = palette.color(QtGui.QPalette.ButtonText)
        color.setAlpha(235 if pinned else 175)
        painter.setPen(QtGui.QPen(
            color, 1.8 if pinned else 1.5, QtCore.Qt.SolidLine,
            QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        if pinned:
            fill = QtGui.QColor(color)
            fill.setAlpha(55)
            painter.setBrush(fill)
        else:
            painter.setBrush(QtCore.Qt.NoBrush)
        body = QtGui.QPainterPath()
        body.moveTo(-4.5, -5.0)
        body.lineTo(4.5, -5.0)
        body.lineTo(2.7, -1.7)
        body.lineTo(2.1, 1.0)
        body.lineTo(-2.1, 1.0)
        body.lineTo(-2.7, -1.7)
        body.closeSubpath()
        painter.drawPath(body)
        painter.drawLine(QtCore.QPointF(0.0, 1.0), QtCore.QPointF(0.0, 5.5))
        painter.drawPoint(QtCore.QPointF(0.0, 6.0))
        painter.end()
        return QtGui.QIcon(pixmap)


    class _DocumentTabBar(QtWidgets.QTabBar):
        pinRequested = QtCore.Signal(str)
        closeDocumentRequested = QtCore.Signal(str)
        closeOthersRequested = QtCore.Signal(str)

        def __init__(self, parent=None):
            super(_DocumentTabBar, self).__init__(parent)
            self.setDocumentMode(True)
            self.setMovable(True)
            self.setTabsClosable(True)
            self.setElideMode(QtCore.Qt.ElideMiddle)
            self.setExpanding(False)
            self.tabCloseRequested.connect(self._close_index)
            self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.customContextMenuRequested.connect(self._context_menu)

        def _close_index(self, index):
            workspace_id = str(self.tabData(index) or "")
            if workspace_id:
                self.closeDocumentRequested.emit(workspace_id)

        def configure_pin_button(self, index, workspace_id, pinned):
            button = QtWidgets.QToolButton(self)
            button.setAutoRaise(True)
            button.setFixedSize(20, 20)
            button.setIcon(_pin_icon(self.palette(), pinned))
            if pinned:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Unpin tab"))
            else:
                button.setToolTip(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Pin tab"))
            button.clicked.connect(
                lambda _checked=False, value=workspace_id: self.pinRequested.emit(value))
            self.setTabButton(index, QtWidgets.QTabBar.LeftSide, button)

        def _context_menu(self, point):
            index = self.tabAt(point)
            if index < 0:
                return
            workspace_id = str(self.tabData(index) or "")
            menu = QtWidgets.QMenu(self)
            close_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close tab"))
            others_action = menu.addAction(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Close other tabs"))
            menu.addSeparator()
            pin_button = self.tabButton(index, QtWidgets.QTabBar.LeftSide)
            pin_action = menu.addAction(
                pin_button.toolTip() if pin_button is not None else
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Pin tab"))
            selected = menu.exec_(self.mapToGlobal(point))
            if selected is close_action:
                self.closeDocumentRequested.emit(workspace_id)
            elif selected is others_action:
                self.closeOthersRequested.emit(workspace_id)
            elif selected is pin_action:
                self.pinRequested.emit(workspace_id)

        def mouseReleaseEvent(self, event):
            if event.button() == QtCore.Qt.MiddleButton:
                index = self.tabAt(event.pos())
                if index >= 0:
                    self._close_index(index)
                    event.accept()
                    return
            super(_DocumentTabBar, self).mouseReleaseEvent(event)


    class ReplayLabShaderWorkspacePanel(object):
        """Docked tree, multi-document HLSL editor and compile/apply status."""

        def __init__(self, ctx, qrd, closed_callback=None, registry_path=None,
                     save_client=None):
            self.ctx = ctx
            self.qrd = qrd
            self.closed_callback = closed_callback
            self.registry_path = str(registry_path or "")
            self._save_client = (
                save_client or
                _shader_save_client_protocol.ReplayLabShaderSaveClient())
            self._save_tasks = queue.Queue()
            self._save_results = queue.Queue()
            self._save_stop = threading.Event()
            self._save_worker = None
            self._save_worker_stopped = False
            self._next_document_token = 0
            self._next_save_token = 0
            self._alive = True
            self._docked = False
            self._shutting_down = False
            self._snapshot = None
            self._workspaces = {}
            self._documents = {}
            self._tree_items = {}
            self._watched_paths = [self.registry_path] if self.registry_path else []
            self._last_disk_signature = None
            self._loading_documents = set()
            self._reordering_tabs = False
            self._restored = False
            self._tree_signature = None
            self._settings = QtCore.QSettings()
            state = self._load_state()
            self._pinned = list(state.get("pinned", []))
            self._restore_open = list(state.get("open", []))
            self._restore_active = str(state.get("active", "") or "")

            self.top = _WorkspaceRoot(self)
            self.top.setObjectName("replaylabShaderWorkspace")
            self.top.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            self.top.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"))
            self.top.destroyed.connect(self._destroyed)

            outer = QtWidgets.QVBoxLayout(self.top)
            outer.setContentsMargins(4, 4, 4, 4)
            header = QtWidgets.QHBoxLayout()
            title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader Workspace"), self.top)
            title_font = title.font()
            title_font.setBold(True)
            title.setFont(title_font)
            header.addWidget(title)
            subtitle = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "Reviewed organized HLSL · Ctrl+S saves and automatically compiles/replaces"),
                self.top)
            # ``mid`` becomes nearly indistinguishable from the window color
            # in several dark themes. Use the theme's readable text role for
            # this small explanatory line.
            subtitle.setStyleSheet("color: palette(text);")
            header.addWidget(subtitle)
            header.addStretch(1)
            self.save_button = QtWidgets.QPushButton(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save"), self.top)
            self.save_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save the current Shader (Ctrl+S)"))
            self.save_button.clicked.connect(self._save_current)
            header.addWidget(self.save_button)
            self.save_all_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Save All"), self.top)
            self.save_all_button.setToolTip(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save all modified Shader tabs"))
            self.save_all_button.clicked.connect(self._save_all)
            header.addWidget(self.save_all_button)
            self.refresh_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Refresh"), self.top)
            self.refresh_button.clicked.connect(lambda _checked=False: self.refresh(True))
            header.addWidget(self.refresh_button)
            outer.addLayout(header)

            self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self.top)
            self.file_tree = QtWidgets.QTreeWidget(self.main_splitter)
            self.file_tree.setObjectName("replaylabShaderWorkspaceTree")
            self.file_tree.setHeaderLabels((QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Shader files"),))
            self.file_tree.setMinimumWidth(210)
            self.file_tree.setUniformRowHeights(True)
            self.file_tree.itemClicked.connect(self._tree_item_clicked)
            self.file_tree.itemActivated.connect(self._tree_item_clicked)
            self.main_splitter.addWidget(self.file_tree)

            right = QtWidgets.QWidget(self.main_splitter)
            right_layout = QtWidgets.QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            self.tab_bar = _DocumentTabBar(right)
            self.tab_bar.currentChanged.connect(self._tab_changed)
            self.tab_bar.tabMoved.connect(self._tab_moved)
            self.tab_bar.pinRequested.connect(self._toggle_pin)
            self.tab_bar.closeDocumentRequested.connect(self._close_document)
            self.tab_bar.closeOthersRequested.connect(self._close_other_documents)
            right_layout.addWidget(self.tab_bar)

            self.editor_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical, right)
            self.editor_stack = QtWidgets.QStackedWidget(self.editor_splitter)
            empty = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects",
                "No editable HLSL workspaces are available. Use Reverse/Edit with AI on a supported Shader to create one."),
                self.editor_stack)
            empty.setAlignment(QtCore.Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setMargin(24)
            self.empty_page = empty
            self.editor_stack.addWidget(empty)
            self.editor_splitter.addWidget(self.editor_stack)

            problems_box = QtWidgets.QWidget(self.editor_splitter)
            problems_layout = QtWidgets.QVBoxLayout(problems_box)
            problems_layout.setContentsMargins(0, 0, 0, 0)
            problems_title = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Problems"), problems_box)
            problems_title_font = problems_title.font()
            problems_title_font.setBold(True)
            problems_title.setFont(problems_title_font)
            problems_layout.addWidget(problems_title)
            self.problems = QtWidgets.QTreeWidget(problems_box)
            self.problems.setHeaderLabels((
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Line"),
                QtCore.QCoreApplication.translate("ReplayLabProjects", "Message"),
            ))
            self.problems.setRootIsDecorated(False)
            self.problems.setAlternatingRowColors(True)
            self.problems.header().setSectionResizeMode(
                0, QtWidgets.QHeaderView.ResizeToContents)
            self.problems.header().setSectionResizeMode(
                1, QtWidgets.QHeaderView.Stretch)
            self.problems.itemActivated.connect(self._problem_activated)
            self.problems.itemDoubleClicked.connect(self._problem_activated)
            problems_layout.addWidget(self.problems)
            self.editor_splitter.addWidget(problems_box)
            self.editor_splitter.setStretchFactor(0, 4)
            self.editor_splitter.setStretchFactor(1, 1)
            self.editor_splitter.setSizes((680, 150))
            right_layout.addWidget(self.editor_splitter)
            self.main_splitter.addWidget(right)
            self.main_splitter.setStretchFactor(0, 0)
            self.main_splitter.setStretchFactor(1, 1)
            self.main_splitter.setSizes((250, 1050))
            outer.addWidget(self.main_splitter, 1)

            status_row = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Waiting for organized HLSL workspaces"), self.top)
            self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            status_row.addWidget(self.status_label, 1)
            self.reload_button = QtWidgets.QPushButton(
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Reload"), self.top)
            self.reload_button.setVisible(False)
            self.reload_button.clicked.connect(self._reload_current)
            status_row.addWidget(self.reload_button)
            outer.addLayout(status_row)

            self.file_watcher = QtCore.QFileSystemWatcher(self.top)
            self.file_watcher.fileChanged.connect(self._filesystem_changed)
            self.file_watcher.directoryChanged.connect(self._filesystem_changed)
            self.change_timer = QtCore.QTimer(self.top)
            self.change_timer.setSingleShot(True)
            self.change_timer.setInterval(FILESYSTEM_COALESCE_MS)
            self.change_timer.timeout.connect(lambda: self.refresh(True))
            self.poll_timer = QtCore.QTimer(self.top)
            self.poll_timer.setInterval(POLL_INTERVAL_MS)
            self.poll_timer.timeout.connect(self.refresh)
            self.poll_timer.start()
            self.save_result_timer = QtCore.QTimer(self.top)
            self.save_result_timer.setInterval(SAVE_RESULT_POLL_MS)
            self.save_result_timer.timeout.connect(self._drain_save_results)
            self._save_worker = threading.Thread(
                target=_save_worker_loop,
                args=(self._save_client, self._save_tasks, self._save_results,
                      self._save_stop),
                name="ReplayLabShaderWorkspaceSave")
            self._save_worker.daemon = True
            self._save_worker.start()

            self.close_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence.Close, self.top)
            self.close_shortcut.activated.connect(self._close_current)
            self.save_all_shortcut = QtWidgets.QShortcut(
                QtGui.QKeySequence("Ctrl+Shift+S"), self.top)
            self.save_all_shortcut.activated.connect(self._save_all)
            self.refresh(True)

        def _load_state(self):
            raw = self._settings.value(SETTINGS_KEY, "")
            try:
                value = json.loads(str(raw or ""))
            except (TypeError, ValueError):
                return {}
            if not isinstance(value, dict):
                return {}
            clean = {}
            for field in ("open", "pinned"):
                values = value.get(field, [])
                clean[field] = [
                    str(item).lower() for item in values
                    if isinstance(item, str) and TOKEN_RE.fullmatch(item.lower())
                ][:MAX_OPEN_DOCUMENTS]
            active = str(value.get("active", "") or "").lower()
            clean["active"] = active if TOKEN_RE.fullmatch(active) else ""
            return clean

        def _save_state(self):
            if self.tab_bar is None:
                return
            opened = [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]
            active = ""
            if self.tab_bar.currentIndex() >= 0:
                active = str(self.tab_bar.tabData(self.tab_bar.currentIndex()) or "")
            value = {
                "open": opened,
                "pinned": [item for item in self._pinned if item in self._documents],
                "active": active,
            }
            self._settings.setValue(
                SETTINGS_KEY, json.dumps(value, separators=(",", ":")))

        def _stop_save_worker(
                self, timeout_seconds=SAVE_WORKER_SHUTDOWN_TIMEOUT_SECONDS):
            if self._save_worker_stopped:
                worker = self._save_worker
            else:
                self._save_worker_stopped = True
                self._save_stop.set()
                self._save_tasks.put(None)
                worker = self._save_worker
            stopped = _join_save_worker(
                worker, timeout_seconds, self._save_transport_pending)
            if stopped:
                self._save_worker = None
            return stopped

        def _destroyed(self, _object=None):
            if not self._alive:
                return
            self._alive = False
            if not self._shutting_down:
                try:
                    self._stop_save_worker()
                except Exception as exc:
                    # i18n-ignore: developer-only shutdown diagnostic.
                    print("[ReplayLab] Shader Workspace shutdown error: %s" %
                          str(exc))
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
            except Exception:
                pass
            callback = self.closed_callback
            self.closed_callback = None
            self.top = None
            if callback:
                callback()

        def _confirm_panel_close(self):
            if self._shutting_down:
                return True
            for workspace_id in list(self._tab_workspace_ids()):
                document = self._documents.get(workspace_id)
                if document and document.get("dirty"):
                    if not self._confirm_close_document(document):
                        return False
            self._save_state()
            try:
                # ToolWindowManager can directly delete an accepted dock tab. Prove the Python
                # worker is quiescent before allowing that irreversible ownership transition.
                self._stop_save_worker()
            except Exception as exc:
                # i18n-ignore: developer-only shutdown diagnostic; the translated status below is
                # the user-visible close refusal.
                print("[ReplayLab] Shader Workspace close deferred: %s" % str(exc))
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The Shader Workspace is still finishing a save. Wait for it to complete, then close the workspace again."))
                return False
            return True

        def show(self):
            if not self._alive or self.top is None:
                raise RuntimeError("ReplayLab Shader Workspace has already been closed")
            if not self._docked:
                self.ctx.AddDockWindow(
                    self.top, self.qrd.DockReference.MainToolArea, None, 0.72)
                self._docked = True
            self.ctx.RaiseDockWindow(self.top)

        def shutdown(self):
            if not self._alive:
                return
            self._shutting_down = True
            self._save_state()
            try:
                self._stop_save_worker()
            except Exception:
                # Keep the live widget and its bridge-owned controller reachable so a later stop()
                # can retry after the bounded Host request finishes. Closing here would trigger
                # destroyed/closed_callback and irreversibly lose the only worker reference.
                self._shutting_down = False
                raise
            try:
                self.poll_timer.stop()
                self.change_timer.stop()
                self.save_result_timer.stop()
                subscriptions = (
                    self.file_watcher.files() + self.file_watcher.directories())
                if subscriptions:
                    self.file_watcher.removePaths(subscriptions)
            except Exception:
                pass
            widget = self.top
            if widget is not None:
                widget.close()
                widget.deleteLater()

        def _disk_signature(self):
            values = []
            for path in self._watched_paths:
                try:
                    info = os.stat(path)
                    values.append((
                        _normal_path(path), getattr(info, "st_mtime_ns", 0),
                        int(info.st_size), int(info.st_mode)))
                except OSError:
                    values.append((_normal_path(path), -1, -1, -1))
            return tuple(values)

        def _reset_watched_paths(self, paths):
            current = self.file_watcher.files() + self.file_watcher.directories()
            if current:
                try:
                    self.file_watcher.removePaths(current)
                except Exception:
                    pass
            files = []
            directories = []
            for path in paths:
                if os.path.isfile(path):
                    files.append(path)
                elif os.path.isdir(path):
                    directories.append(path)
            if files:
                self.file_watcher.addPaths(files)
            if directories:
                self.file_watcher.addPaths(directories)

        def _filesystem_changed(self, _path):
            if not self.change_timer.isActive():
                self.change_timer.start(FILESYSTEM_COALESCE_MS)

        def refresh(self, force=False):
            if not self._alive:
                return
            signature = self._disk_signature()
            has_unknown_commit = any(
                document.get("commitUnknown")
                for document in self._documents.values())
            if (not force and not has_unknown_commit and
                    signature == self._last_disk_signature):
                return
            snapshot = _shader_console_protocol.load_console_snapshot(
                self.registry_path)
            self._snapshot = snapshot
            previous_workspace_ids = set(self._workspaces)
            eligible = [
                workspace for workspace in snapshot.get("workspaces", [])
                if workspace.get("editorEligible") is True
            ]
            self._workspaces = dict(
                (str(workspace["workspaceId"]), workspace)
                for workspace in eligible)
            newly_available_ids = [
                str(workspace["workspaceId"]) for workspace in eligible
                if str(workspace["workspaceId"]) not in previous_workspace_ids
            ]
            tree_signature = tuple(
                (workspace["workspaceId"], workspace.get("captureId", ""),
                 workspace.get("stage", ""), workspace.get("entryPoint", ""),
                 workspace.get("sourcePath", ""))
                for workspace in eligible)
            if tree_signature != self._tree_signature:
                self._tree_signature = tree_signature
                self._render_tree(eligible)
            watched = []
            watched_keys = set()
            for path in [self.registry_path] + list(snapshot.get("watchedPaths", [])):
                key = _normal_path(path) if path else ""
                if key and key not in watched_keys:
                    watched_keys.add(key)
                    watched.append(path)
            # Keep the fixed registry path even before it exists. Polling its
            # missing-file signature is what notices the first Reverse/Edit
            # authorization created after qrenderdoc startup.
            self._watched_paths = watched
            self._reset_watched_paths(self._watched_paths)
            self._last_disk_signature = self._disk_signature()

            if not self._restored:
                self._restored = True
                restore_ids = []
                for workspace_id in self._pinned + self._restore_open:
                    if workspace_id not in restore_ids:
                        restore_ids.append(workspace_id)
                for workspace_id in restore_ids[:MAX_OPEN_DOCUMENTS]:
                    if workspace_id in self._workspaces:
                        self._open_workspace(workspace_id, quiet=True, select=False)
                if not self._documents and eligible:
                    self._open_workspace(
                        str(eligible[0]["workspaceId"]), quiet=True, select=False)
                active_index = self._tab_index(self._restore_active)
                selected_index = (
                    active_index if active_index >= 0 else
                    (0 if self.tab_bar.count() else -1))
                self.tab_bar.setCurrentIndex(selected_index)
                self._tab_changed(selected_index)
            elif newly_available_ids:
                # A Reverse/Edit flow can finish while this dock is already
                # open. Surface the newly reviewed HLSL immediately without a
                # second Window/View action or a manual tree click.
                self._open_workspace(newly_available_ids[0], quiet=True)

            for workspace_id in list(self._documents):
                workspace = self._workspaces.get(workspace_id)
                if workspace is None:
                    self._documents[workspace_id]["available"] = False
                    continue
                self._documents[workspace_id]["available"] = True
                self._documents[workspace_id]["workspace"] = workspace
                self._refresh_document_from_disk(workspace_id)
            self._refresh_current_view()

        def _render_tree(self, workspaces):
            self.file_tree.clear()
            self._tree_items = {}
            captures = {}
            stages = {}
            for workspace in workspaces:
                capture_id = str(workspace.get("captureId", "") or "")
                capture_key = capture_id or "unknown"
                capture_item = captures.get(capture_key)
                if capture_item is None:
                    label = _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Capture %1"),
                        capture_id[:12] if capture_id else
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Unknown"))
                    capture_item = QtWidgets.QTreeWidgetItem((label,))
                    capture_item.setToolTip(0, capture_id)
                    captures[capture_key] = capture_item
                    self.file_tree.addTopLevelItem(capture_item)
                stage = str(workspace.get("stage", "") or "").lower()
                stage_key = (capture_key, stage)
                stage_item = stages.get(stage_key)
                if stage_item is None:
                    abbreviation = STAGE_ABBREVIATIONS.get(stage, stage.upper())
                    stage_item = QtWidgets.QTreeWidgetItem((abbreviation,))
                    stages[stage_key] = stage_item
                    capture_item.addChild(stage_item)
                file_name = os.path.basename(str(workspace.get("sourcePath", "") or ""))
                entry_point = str(workspace.get("entryPoint", "") or "")
                leaf = QtWidgets.QTreeWidgetItem((
                    "%s · %s" % (entry_point, file_name),))
                workspace_id = str(workspace.get("workspaceId", "") or "")
                leaf.setData(0, QtCore.Qt.UserRole, workspace_id)
                leaf.setToolTip(0, str(workspace.get("sourcePath", "") or ""))
                stage_item.addChild(leaf)
                self._tree_items[workspace_id] = leaf
            self.file_tree.expandAll()

        def _tree_item_clicked(self, item, _column=0):
            workspace_id = str(item.data(0, QtCore.Qt.UserRole) or "")
            if workspace_id:
                self._open_workspace(workspace_id)

        def _tab_workspace_ids(self):
            return [
                str(self.tab_bar.tabData(index) or "")
                for index in range(self.tab_bar.count())
            ]

        def _tab_index(self, workspace_id):
            for index in range(self.tab_bar.count()):
                if str(self.tab_bar.tabData(index) or "") == str(workspace_id or ""):
                    return index
            return -1

        def _tab_title(self, document):
            title = "%s · %s" % (
                document.get("entryPoint", ""), document.get("fileName", ""))
            return title + (" *" if document.get("dirty") else "")

        def _insert_tab(self, workspace_id, document):
            pinned = workspace_id in self._pinned
            insertion = self.tab_bar.count()
            if pinned:
                insertion = sum(
                    1 for item in self._tab_workspace_ids() if item in self._pinned)
            index = self.tab_bar.insertTab(insertion, self._tab_title(document))
            self.tab_bar.setTabData(index, workspace_id)
            self.tab_bar.setTabToolTip(index, document.get("sourcePath", ""))
            self.tab_bar.configure_pin_button(index, workspace_id, pinned)
            return index

        def _open_workspace(self, workspace_id, quiet=False, select=True):
            workspace_id = str(workspace_id or "")
            index = self._tab_index(workspace_id)
            if index >= 0:
                if select:
                    self.tab_bar.setCurrentIndex(index)
                return True
            workspace = self._workspaces.get(workspace_id)
            if workspace is None:
                return False
            if len(self._documents) >= MAX_OPEN_DOCUMENTS:
                closed = False
                for candidate in self._tab_workspace_ids():
                    document = self._documents.get(candidate)
                    if (candidate not in self._pinned and document and
                            not document.get("dirty")):
                        closed = self._close_document(candidate, prompt=False)
                        if closed:
                            break
                if not closed:
                    if not quiet:
                        QtWidgets.QMessageBox.warning(
                            self.top,
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects", "Could not open Shader"),
                            QtCore.QCoreApplication.translate(
                                "ReplayLabProjects",
                                "Close an unmodified tab before opening another Shader."))
                    return False
            try:
                opened = _shader_console_protocol.load_authorized_source_document(
                    self.registry_path, workspace.get("authorizationId", ""), workspace_id)
            except Exception as exc:
                if not quiet:
                    QtWidgets.QMessageBox.warning(
                        self.top,
                        QtCore.QCoreApplication.translate(
                            "ReplayLabProjects", "Could not open Shader"), str(exc))
                return False

            editor = _HLSLEditor(self.editor_stack)
            editor.setObjectName("replaylabShaderEditor_%s" % workspace_id)
            self._loading_documents.add(workspace_id)
            editor.setPlainText(opened.get("text", ""))
            editor.document().setModified(False)
            self._loading_documents.discard(workspace_id)
            editor.textChanged.connect(
                lambda value=workspace_id: self._document_text_changed(value))
            editor.document().modificationChanged.connect(
                lambda modified, value=workspace_id:
                self._document_changed(value, modified))
            editor.saveRequested.connect(
                lambda value=workspace_id: self._save_document(value))
            self._next_document_token += 1
            document = {
                "workspaceId": workspace_id,
                "documentToken": self._next_document_token,
                "authorizationId": opened.get("authorizationId", ""),
                "workspace": workspace,
                "sourcePath": opened.get("sourcePath", ""),
                "fileName": opened.get("fileName", ""),
                "entryPoint": opened.get("entryPoint", ""),
                "editor": editor,
                "rawSha256": opened.get("rawSha256", ""),
                "modifiedNs": opened.get("modifiedNs", 0),
                "size": opened.get("size", 0),
                "newline": self._newline_style(opened.get("text", "")),
                "baselineEditorText": editor.toPlainText(),
                "textRevision": 0,
                "dirty": False,
                "conflict": False,
                "saveInFlight": False,
                "commitUnknown": False,
                "pendingSave": None,
                "saveQueued": False,
                "closeAfterSave": False,
                "available": True,
            }
            self._documents[workspace_id] = document
            self.editor_stack.addWidget(editor)
            index = self._insert_tab(workspace_id, document)
            editor.setReadOnly(workspace.get("autoApplyReady") is not True)
            if select:
                self.tab_bar.setCurrentIndex(index)
                # insertTab makes the first tab current before tabData exists,
                # so its immediate currentChanged callback cannot resolve the
                # document. Activate explicitly even when the index stayed 0.
                self._tab_changed(index)
            self._save_state()
            return True

        def _document_text_changed(self, workspace_id):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            document["textRevision"] = int(
                document.get("textRevision", 0)) + 1
            self._sync_document_dirty(document)

        def _document_changed(self, workspace_id, _modified=None):
            if workspace_id in self._loading_documents:
                return
            document = self._documents.get(workspace_id)
            if document is None:
                return
            self._sync_document_dirty(document)

        def _sync_document_dirty(self, document):
            pending = document.get("pendingSave")
            if (pending is not None and
                    (document.get("saveInFlight") or
                     document.get("commitUnknown"))):
                baseline = pending.get("submittedEditorText", "")
            else:
                baseline = document.get("baselineEditorText", "")
            document["dirty"] = (
                document["editor"].toPlainText() != baseline)
            workspace_id = document["workspaceId"]
            self._update_tab(workspace_id)
            if self._current_workspace_id() == workspace_id:
                self._refresh_current_view()

        def _update_tab(self, workspace_id):
            index = self._tab_index(workspace_id)
            document = self._documents.get(workspace_id)
            if index >= 0 and document:
                self.tab_bar.setTabText(index, self._tab_title(document))
                self.tab_bar.configure_pin_button(
                    index, workspace_id, workspace_id in self._pinned)

        def _current_workspace_id(self):
            index = self.tab_bar.currentIndex()
            return str(self.tab_bar.tabData(index) or "") if index >= 0 else ""

        def _current_document(self):
            return self._documents.get(self._current_workspace_id())

        def _tab_changed(self, index):
            if index >= 0:
                workspace_id = str(self.tab_bar.tabData(index) or "")
                document = self._documents.get(workspace_id)
                if document:
                    self.editor_stack.setCurrentWidget(document["editor"])
                    tree_item = self._tree_items.get(workspace_id)
                    if tree_item is not None:
                        self.file_tree.setCurrentItem(tree_item)
            else:
                self.editor_stack.setCurrentWidget(self.empty_page)
            self._save_state()
            self._refresh_current_view()

        def _tab_moved(self, _from, _to):
            if self._reordering_tabs:
                return
            # Pinned tabs stay as one stable group at the left edge.
            self._reordering_tabs = True
            try:
                ordered = self._tab_workspace_ids()
                desired = ([item for item in ordered if item in self._pinned] +
                           [item for item in ordered if item not in self._pinned])
                if ordered != desired:
                    current = self._current_workspace_id()
                    for destination, workspace_id in enumerate(desired):
                        source = self._tab_index(workspace_id)
                        if source != destination:
                            self.tab_bar.moveTab(source, destination)
                    self.tab_bar.setCurrentIndex(self._tab_index(current))
                pinned = set(self._pinned)
                self._pinned = [
                    item for item in self._tab_workspace_ids() if item in pinned]
            finally:
                self._reordering_tabs = False
            self._save_state()

        def _toggle_pin(self, workspace_id):
            workspace_id = str(workspace_id or "")
            if workspace_id not in self._documents:
                return
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            else:
                self._pinned.append(workspace_id)
            current = self._current_workspace_id()
            document = self._documents[workspace_id]
            index = self._tab_index(workspace_id)
            self.tab_bar.removeTab(index)
            self._insert_tab(workspace_id, document)
            selected_index = self._tab_index(current)
            self.tab_bar.setCurrentIndex(selected_index)
            self._tab_changed(selected_index)
            self._save_state()

        def _confirm_close_document(self, document):
            if not document.get("dirty"):
                if (document.get("saveInFlight") or
                        document.get("commitUnknown")):
                    document["closeAfterSave"] = True
                    return False
                return True
            message = _format_message(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Save changes to %1 before closing?"),
                document.get("fileName", ""))
            box = QtWidgets.QMessageBox(self.top)
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setWindowTitle(QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Unsaved Shader"))
            box.setText(message)
            box.setStandardButtons(
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard |
                QtWidgets.QMessageBox.Cancel)
            answer = box.exec_()
            if answer == QtWidgets.QMessageBox.Save:
                document["closeAfterSave"] = True
                if not self._save_document(document["workspaceId"]):
                    document["closeAfterSave"] = False
                return False
            return answer == QtWidgets.QMessageBox.Discard

        def _close_document(self, workspace_id, prompt=True):
            workspace_id = str(workspace_id or "")
            document = self._documents.get(workspace_id)
            if document is None:
                return True
            if prompt and not self._confirm_close_document(document):
                return False
            index = self._tab_index(workspace_id)
            if index >= 0:
                self.tab_bar.removeTab(index)
            self.editor_stack.removeWidget(document["editor"])
            document["editor"].deleteLater()
            self._documents.pop(workspace_id, None)
            if workspace_id in self._pinned:
                self._pinned.remove(workspace_id)
            if not self._documents:
                self.editor_stack.setCurrentWidget(self.empty_page)
            self._save_state()
            self._refresh_current_view()
            return True

        def _close_current(self):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                self._close_document(workspace_id)

        def _close_other_documents(self, workspace_id):
            for candidate in list(self._tab_workspace_ids()):
                if candidate != workspace_id and not self._close_document(candidate):
                    break

        def _save_current(self, _checked=False):
            workspace_id = self._current_workspace_id()
            if workspace_id:
                return self._save_document(workspace_id)
            return True

        def _save_all(self, _checked=False):
            for workspace_id in list(self._tab_workspace_ids()):
                document = self._documents.get(workspace_id)
                if document and document.get("dirty"):
                    if not self._save_document(workspace_id):
                        self.tab_bar.setCurrentIndex(self._tab_index(workspace_id))
                        return False
            return True

        def _save_document(self, workspace_id):
            document = self._documents.get(workspace_id)
            if document is None:
                return False
            workspace = self._workspaces.get(workspace_id)
            if workspace is None or not document.get("available"):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Workspace authorization is no longer available."))
                return False
            if workspace.get("autoApplyReady") is not True:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Live edit is not running. Restart Reverse/Edit with AI before saving."))
                return False
            if document.get("conflict"):
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
                return False
            if (document.get("saveInFlight") or
                    document.get("commitUnknown")):
                if document.get("dirty"):
                    document["saveQueued"] = True
                return True
            if not document.get("dirty"):
                return True
            source_text = self._document_source_text(document)
            try:
                submitted_hash = hashlib.sha256(
                    source_text.encode("utf-8", errors="strict")).hexdigest()
            except UnicodeEncodeError as exc:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"), str(exc))
                return False
            live_edit = workspace.get("liveEdit") or {}
            self._next_save_token += 1
            task = {
                "saveToken": self._next_save_token,
                "documentToken": document["documentToken"],
                "workspaceId": workspace_id,
                "liveEditId": str(live_edit.get("liveEditId", "") or ""),
                "authorizationId": document.get("authorizationId", ""),
                "baseHash": document.get("rawSha256", ""),
                "submittedHash": submitted_hash,
                "sourceText": source_text,
                "submittedEditorText": document["editor"].toPlainText(),
                "textRevision": int(document.get("textRevision", 0)),
            }
            document["pendingSave"] = task
            document["saveInFlight"] = True
            document["commitUnknown"] = False
            document["saveQueued"] = False
            # The submitted undo revision is the provisional Qt clean point.
            # A definite failure restores dirty state; a success (or observed
            # commit after timeout) leaves Undo anchored at the saved revision.
            document["editor"].document().setModified(False)
            self._sync_document_dirty(document)
            self._save_tasks.put(task)
            if not self.save_result_timer.isActive():
                self.save_result_timer.start()
            self._save_state()
            self._refresh_current_view()
            return True

        def _drain_save_results(self):
            if not self._alive or self.top is None:
                return
            refresh_unknown = False
            while True:
                try:
                    outcome = self._save_results.get_nowait()
                except queue.Empty:
                    break
                task = outcome.get("task") or {}
                workspace_id = str(task.get("workspaceId", "") or "")
                document = self._documents.get(workspace_id)
                if (document is None or
                        document.get("documentToken") !=
                        task.get("documentToken")):
                    continue
                pending = document.get("pendingSave") or {}
                if pending.get("saveToken") != task.get("saveToken"):
                    continue
                document["saveInFlight"] = False
                kind = outcome.get("kind")
                if kind == "saved":
                    result = outcome.get("result") or {}
                    if result.get("rawSha256") != task.get("submittedHash"):
                        self._finish_failed_save(
                            document, outcome.get("error", ""), False)
                    else:
                        self._promote_submitted_save(
                            document, result,
                            result.get("changed") is True, True)
                elif kind == "unknown":
                    document["commitUnknown"] = True
                    self._sync_document_dirty(document)
                    refresh_unknown = True
                else:
                    self._finish_failed_save(
                        document, outcome.get("error", ""),
                        kind == "conflict")
            if refresh_unknown and self._alive:
                self.refresh(True)
            if not any(
                    document.get("saveInFlight")
                    for document in self._documents.values()):
                self.save_result_timer.stop()

        def _finish_failed_save(self, document, message, conflict):
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["conflict"] = bool(conflict)
            self._sync_document_dirty(document)
            document["editor"].document().setModified(
                bool(document.get("dirty")))
            if self._alive and self.top is not None:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not save Shader"),
                    str(message or ""))
            self._save_state()
            self._refresh_current_view()

        def _save_transport_pending(self):
            checker = getattr(self._save_client, "request_pending", None)
            if not callable(checker):
                return False
            try:
                return bool(checker())
            except Exception:
                return True

        def _resolve_uncommitted_save(self, document):
            workspace_id = document["workspaceId"]
            retry = bool(document.get("saveQueued"))
            close_after_save = bool(document.get("closeAfterSave"))
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["conflict"] = False
            self._sync_document_dirty(document)
            document["editor"].document().setModified(
                bool(document.get("dirty")))
            if document.get("dirty") and (retry or close_after_save):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()

        def _promote_submitted_save(self, document, metadata, changed,
                                    continue_queued):
            task = document.get("pendingSave")
            if task is None:
                return
            workspace_id = document["workspaceId"]
            document["rawSha256"] = task["submittedHash"]
            document["baselineEditorText"] = task["submittedEditorText"]
            document["modifiedNs"] = metadata.get("modifiedNs", 0)
            document["size"] = metadata.get("size", 0)
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["conflict"] = False
            document["available"] = True
            queued = bool(document.get("saveQueued"))
            document["saveQueued"] = False
            close_after_save = bool(document.get("closeAfterSave"))
            self._sync_document_dirty(document)
            if not document.get("dirty"):
                document["editor"].document().setModified(False)
            if self._current_workspace_id() == workspace_id:
                if changed:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Saved; waiting for compile and replacement…"))
                else:
                    self.status_label.setText(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No changes to save"))
            self._save_state()
            if (continue_queued and queued and document.get("dirty") and
                    not document.get("conflict")):
                if not self._save_document(workspace_id):
                    document["closeAfterSave"] = False
            elif close_after_save and not document.get("dirty"):
                self._close_document(workspace_id, prompt=False)
            else:
                document["closeAfterSave"] = False
                self._refresh_current_view()

        @staticmethod
        def _document_source_text(document):
            source_text = document["editor"].toPlainText()
            newline = document.get("newline", "\n")
            if newline != "\n":
                source_text = source_text.replace("\r\n", "\n").replace(
                    "\r", "\n").replace("\n", newline)
            return source_text

        @staticmethod
        def _newline_style(text):
            value = str(text or "")
            if "\r\n" in value:
                return "\r\n"
            if "\r" in value:
                return "\r"
            return "\n"

        def _refresh_document_from_disk(self, workspace_id):
            document = self._documents.get(workspace_id)
            workspace = self._workspaces.get(workspace_id)
            if document is None or workspace is None:
                return
            document["editor"].setReadOnly(
                workspace.get("autoApplyReady") is not True)
            # The worker result is authoritative while the Host request still
            # has a known outcome. Filesystem notification can arrive first.
            if document.get("saveInFlight"):
                return
            commit_unknown = bool(
                document.get("commitUnknown") and
                document.get("pendingSave") is not None)
            try:
                info = os.stat(document.get("sourcePath", ""))
                modified_ns = getattr(info, "st_mtime_ns", int(
                    info.st_mtime * 1000000000))
                if (not commit_unknown and
                        modified_ns == document.get("modifiedNs") and
                        int(info.st_size) == int(document.get("size", 0))):
                    return
                opened = _shader_console_protocol.load_authorized_source_document(
                    self.registry_path, workspace.get("authorizationId", ""), workspace_id)
            except Exception:
                document["available"] = False
                return
            disk_hash = opened.get("rawSha256")
            if commit_unknown:
                task = document.get("pendingSave") or {}
                classification = _classify_commit_unknown(
                    task.get("baseHash"), task.get("submittedHash"), disk_hash)
                if classification == "committed":
                    self._promote_submitted_save(
                        document, opened,
                        task.get("submittedHash") != task.get("baseHash"), True)
                elif classification == "pending":
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    if self._save_transport_pending():
                        self._sync_document_dirty(document)
                    else:
                        self._resolve_uncommitted_save(document)
                else:
                    document["commitUnknown"] = False
                    document["pendingSave"] = None
                    document["saveQueued"] = False
                    document["closeAfterSave"] = False
                    document["conflict"] = True
                    self._sync_document_dirty(document)
                    document["editor"].document().setModified(
                        bool(document.get("dirty")))
                return
            if disk_hash == document.get("rawSha256"):
                document["modifiedNs"] = opened.get("modifiedNs", 0)
                document["size"] = opened.get("size", 0)
                return
            if document.get("dirty"):
                try:
                    editor_hash = hashlib.sha256(
                        self._document_source_text(document).encode(
                            "utf-8", errors="strict")).hexdigest()
                except UnicodeEncodeError:
                    editor_hash = ""
                if editor_hash == disk_hash:
                    document["rawSha256"] = disk_hash
                    document["baselineEditorText"] = (
                        document["editor"].toPlainText())
                    document["modifiedNs"] = opened.get("modifiedNs", 0)
                    document["size"] = opened.get("size", 0)
                    document["conflict"] = False
                    self._sync_document_dirty(document)
                    document["editor"].document().setModified(False)
                    self._save_state()
                    return
                document["conflict"] = True
                return
            self._apply_loaded_document(document, opened)

        def _apply_loaded_document(self, document, opened):
            workspace_id = document["workspaceId"]
            self._loading_documents.add(workspace_id)
            cursor = document["editor"].textCursor()
            line = cursor.blockNumber() + 1
            column = cursor.positionInBlock() + 1
            document["editor"].setPlainText(opened.get("text", ""))
            self._loading_documents.discard(workspace_id)
            document["rawSha256"] = opened.get("rawSha256", "")
            document["modifiedNs"] = opened.get("modifiedNs", 0)
            document["size"] = opened.get("size", 0)
            document["newline"] = self._newline_style(opened.get("text", ""))
            document["baselineEditorText"] = document["editor"].toPlainText()
            document["dirty"] = False
            document["conflict"] = False
            document["saveInFlight"] = False
            document["commitUnknown"] = False
            document["pendingSave"] = None
            document["saveQueued"] = False
            document["closeAfterSave"] = False
            document["available"] = True
            document["editor"].document().setModified(False)
            document["editor"].go_to_line(line, column)
            self._update_tab(workspace_id)

        def _reload_current(self, _checked=False):
            document = self._current_document()
            if document is None:
                return
            if document.get("dirty"):
                answer = QtWidgets.QMessageBox.question(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Reload Shader"),
                    _format_message(QtCore.QCoreApplication.translate(
                        "ReplayLabProjects",
                        "Discard local changes and reload %1 from disk?"),
                        document.get("fileName", "")),
                    QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Cancel)
                if answer != QtWidgets.QMessageBox.Discard:
                    return
            workspace = self._workspaces.get(document["workspaceId"])
            if workspace is None:
                return
            try:
                opened = _shader_console_protocol.load_authorized_source_document(
                    self.registry_path, workspace.get("authorizationId", ""),
                    document["workspaceId"])
            except Exception as exc:
                QtWidgets.QMessageBox.warning(
                    self.top,
                    QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "Could not open Shader"), str(exc))
                return
            self._apply_loaded_document(document, opened)
            self._refresh_current_view()

        def _workspace_problems(self, workspace):
            watch = workspace.get("watch") or {}
            events = watch.get("events", [])
            failed = _latest_event(events, ("compileFailed",))
            succeeded = _latest_event(events, ("compileSucceeded",))
            if failed is None or (succeeded is not None and int(
                    succeeded.get("sequence", 0)) > int(failed.get("sequence", 0))):
                return []
            sequence = int(failed.get("sequence", 0))
            values = [
                entry for entry in workspace.get("entries", [])
                if (int(entry.get("cursorSequence", 0)) == sequence and
                    entry.get("severity") in ("error", "warning") and
                    int(entry.get("line", 0)) > 0)
            ]
            if values:
                return values
            return [{
                "severity": "error",
                "line": 0,
                "column": 0,
                "text": failed.get("message") or
                QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Shader compilation failed"),
            }]

        def _problem_activated(self, item, _column=0):
            line = int(item.data(0, QtCore.Qt.UserRole) or 0)
            column = int(item.data(1, QtCore.Qt.UserRole) or 0)
            document = self._current_document()
            if document and line > 0:
                document["editor"].go_to_line(line, column)

        def _status_text(self, workspace):
            if workspace is None:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for organized HLSL workspaces")
            live = workspace.get("liveEdit") or {}
            watch = workspace.get("watch") or {}
            if workspace.get("autoApplyReady") is not True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Live edit is not running; this Shader is read-only")
            if watch.get("compileInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiling…")
            if live.get("pendingApply") is True or live.get("applyInProgress") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Validating ABI and applying…")
            if watch.get("pending") is True:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…")
            watch_failed = _latest_event(watch.get("events", []), ("compileFailed",))
            watch_ok = _latest_event(watch.get("events", []), ("compileSucceeded",))
            if watch_failed is not None and (watch_ok is None or int(
                    watch_failed.get("sequence", 0)) > int(watch_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Compilation failed; the last good replacement remains active")
            apply_failed = _latest_event(
                live.get("events", []), ("applyRejected", "applyFailed", "liveEditFailed"))
            apply_ok = _latest_event(
                live.get("events", []), ("applySucceeded", "applyReused"))
            if apply_failed is not None and (apply_ok is None or int(
                    apply_failed.get("sequence", 0)) > int(apply_ok.get("sequence", 0))):
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "Replacement failed; the last good replacement remains active")
            if int(live.get("activeRevision", 0) or 0) > 0:
                return QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Compiled and applied")
            return QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "Watching saves")

        def _refresh_current_view(self):
            document = self._current_document()
            if document is None:
                self.save_button.setEnabled(False)
                self.save_all_button.setEnabled(any(
                    item.get("dirty") and not item.get("conflict") and
                    not item.get("saveInFlight") and
                    not item.get("commitUnknown")
                    for item in self._documents.values()))
                self.reload_button.setVisible(False)
                self.problems.clear()
                self.status_label.setText(self._status_text(None))
                return
            workspace = self._workspaces.get(document["workspaceId"])
            self.save_button.setEnabled(bool(
                document.get("dirty") and workspace and
                workspace.get("autoApplyReady") is True and
                not document.get("conflict") and
                not document.get("saveInFlight") and
                not document.get("commitUnknown")))
            self.save_all_button.setEnabled(any(
                item.get("dirty") and not item.get("conflict") and
                not item.get("saveInFlight") and
                not item.get("commitUnknown")
                for item in self._documents.values()))
            self.reload_button.setVisible(bool(document.get("conflict")))
            if document.get("conflict"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects",
                    "The source changed on disk. Reload it before saving to avoid overwriting another edit."))
            elif (document.get("saveInFlight") or
                  document.get("commitUnknown")):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Waiting for source to stabilize…"))
            elif document.get("dirty"):
                self.status_label.setText(QtCore.QCoreApplication.translate(
                    "ReplayLabProjects", "Modified"))
            else:
                self.status_label.setText(self._status_text(workspace))

            problems = self._workspace_problems(workspace or {})
            self.problems.clear()
            for problem in problems:
                line = int(problem.get("line", 0) or 0)
                text_value = str(problem.get("text", "") or "")
                item = QtWidgets.QTreeWidgetItem((str(line) if line else "", text_value))
                item.setData(0, QtCore.Qt.UserRole, line)
                item.setData(1, QtCore.Qt.UserRole, int(problem.get("column", 0) or 0))
                if problem.get("severity") == "error":
                    item.setForeground(1, QtGui.QBrush(QtGui.QColor("#e06c75")))
                self.problems.addTopLevelItem(item)
            document["editor"].set_diagnostic_lines(problems)
            if not problems:
                placeholder = QtWidgets.QTreeWidgetItem((
                    "", QtCore.QCoreApplication.translate(
                        "ReplayLabProjects", "No compilation problems")))
                placeholder.setFlags(placeholder.flags() & ~QtCore.Qt.ItemIsSelectable)
                self.problems.addTopLevelItem(placeholder)


else:
    class ReplayLabShaderWorkspacePanel(object):
        def __init__(self, _ctx, _qrd, closed_callback=None, registry_path=None,
                     save_client=None):
            del closed_callback, registry_path, save_client
            raise RuntimeError("PySide2 Qt is required for ReplayLab Shader Workspace")


__all__ = ["HAS_QT", "ReplayLabShaderWorkspacePanel"]
