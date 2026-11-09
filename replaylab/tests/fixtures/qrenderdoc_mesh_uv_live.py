"""Verify UV and historical parameter navigation on an owned replay session."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from PySide2 import QtCore, QtGui, QtWidgets
import renderdoc as rd


class Probe:
    def __init__(self):
        self.root = Path(os.environ['RD_ANNOTATION_RUN'])
        self.repo = Path(os.environ['RD_VISUAL_FIXTURE_ROOT']).parents[2]
        self.app = QtWidgets.QApplication.instance()
        self.targets = sorted((self.root/'local/ReplayLab/ai-visuals/targets').glob('*.json'))
        self.targets = [p for p in self.targets if not json.loads(p.read_text(encoding='utf-8'))['target'].get('kind')]
        self.capture = json.loads(self.targets[0].read_text(encoding='utf-8'))['capture']['path']
        self.deadline = time.monotonic() + 180
        self.keep = []
        self.phase = 'capture'
        self.result = {'ok': False, 'cbuffer': []}

    def finish(self, error=None):
        self.timer.stop()
        self.result.update(ok=not error, error=error, phase=self.phase)
        (self.root/'mesh-uv-result.json').write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding='utf-8')
        # Run native teardown after returning from the Python timer callback.
        window = pyrenderdoc.GetMainWindow().Widget()
        QtCore.QMetaObject.invokeMethod(window, 'close', QtCore.Qt.QueuedConnection)

    def link(self, url):
        browsers = self.window.findChildren(QtWidgets.QTextBrowser)
        self.keep.extend(browsers)
        chat = next((b for b in browsers if b.objectName() == 'aiConversationBrowser'), None)
        if chat is None:
            # The normal transcript handler is attached to its QTextBrowser.
            for b in browsers:
                b.anchorClicked.emit(QtCore.QUrl(url))
        else:
            chat.anchorClicked.emit(QtCore.QUrl(url))

    def start_worker(self, script, name):
        file = self.root/(name+'.py')
        prefix = """import json,sys,os
from pathlib import Path
root=Path(os.environ['RD_ANNOTATION_RUN'])
repo=Path(os.environ['RD_VISUAL_FIXTURE_ROOT']).parents[2]
sys.path.insert(0,str(repo/'replaylab/mcp_host'))
from src.ipc_client import IPCClient
c=IPCClient(timeout=110)
"""
        file.write_text(prefix + script, encoding='utf-8')
        self.worker_log = (self.root/(name+'.log')).open('w', encoding='utf-8')
        self.worker = subprocess.Popen([str(self.repo/'replaylab/.venv/Scripts/python.exe'), '-X', 'utf8', str(file)],
            stdout=self.worker_log, stderr=subprocess.STDOUT, creationflags=0x08000000)

    def click(self, widget, position):
        self.app.sendEvent(widget, QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonPress, position,
            QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier))
        self.app.sendEvent(widget, QtGui.QMouseEvent(
            QtCore.QEvent.MouseButtonRelease, position,
            QtCore.Qt.LeftButton, QtCore.Qt.NoButton, QtCore.Qt.NoModifier))

    def key(self, widget, key, modifiers=QtCore.Qt.NoModifier):
        self.app.sendEvent(widget, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, key, modifiers))

    def drag(self, widget, start, end, during):
        for event_type, pos, button, buttons in (
                (QtCore.QEvent.MouseButtonPress, start, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton),
                (QtCore.QEvent.MouseMove, end, QtCore.Qt.NoButton, QtCore.Qt.LeftButton),
                (QtCore.QEvent.MouseButtonRelease, end, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)):
            screen = QtCore.QPointF(widget.mapToGlobal(pos.toPoint()))
            self.app.sendEvent(widget, QtGui.QMouseEvent(
                event_type, pos, screen, button, buttons, QtCore.Qt.NoModifier))
            if event_type == QtCore.QEvent.MouseMove:
                during()

    def timings(self, repaint):
        repaint()
        samples = []
        for _ in range(25):
            start = time.perf_counter()
            repaint()
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        return dict(medianMs=round(samples[12], 3), p95Ms=round(samples[23], 3))

    def verify_native_selection(self, view, atlas, native, mode, wireframe):
        viewport = native.findChild(QtWidgets.QWidget)
        assert viewport and viewport.isVisible()
        self.keep.append(viewport)
        width, height = viewport.width(), viewport.height()
        assert width > 60 and height > 60

        def hit(widget):
            width, height = widget.width(), widget.height()
            # Try the middle first, then a grid across the actual viewport.
            coordinates = sorted(
                ((x / 20, y / 20) for x in range(1, 20) for y in range(1, 20)),
                key=lambda pt: abs(pt[0] - .5) + abs(pt[1] - .5))
            for x, y in coordinates:
                position = QtCore.QPointF(width * x, height * y)
                self.click(widget, position)
                if atlas.property('uvSelectionCount'):
                    return position
            raise RuntimeError('No triangle hit in ' + widget.objectName())

        checks = self.result['checks']
        native_mode = mode.currentIndex()
        for name, vertices in (('Face', 3), ('Point', 6), ('Edge', 2)):
            button = view.findChild(QtWidgets.QToolButton, 'meshUV' + name)
            self.keep.append(button)
            assert not button.icon().isNull() and button.toolTip() and button.isVisible()
            button.click()
            atlas_hit = hit(atlas)
            assert native.property('uvHighlightedVertices') == vertices
            assert mode.currentIndex() == native_mode and native.isVisible()
            self.key(atlas, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
            native_hit = hit(viewport)
            assert atlas.property('uvSelectionCount') == 1
            assert native.property('uvHighlightedVertices') == vertices
            assert mode.currentIndex() == native_mode and native.isVisible()
            checks[name] = dict(uvToMesh=True, meshToUV=True, nativeViewportPreserved=True,
                                uvPixel=[atlas_hit.x(), atlas_hit.y()],
                                meshPixel=[native_hit.x(), native_hit.y()])

        zooms = []
        for dx, dy, px, py in ((0, 120, 0, 0), (120, 0, 0, 0), (0, 0, 0, 40)):
            pos = QtCore.QPointF(atlas.rect().center())
            event = QtGui.QWheelEvent(pos, QtCore.QPointF(atlas.mapToGlobal(pos.toPoint())),
                                     QtCore.QPoint(px, py), QtCore.QPoint(dx, dy),
                                     dy or dx, QtCore.Qt.Vertical if dy else QtCore.Qt.Horizontal,
                                     QtCore.Qt.NoButton, QtCore.Qt.NoModifier)
            self.app.sendEvent(atlas, event)
            assert event.isAccepted()
            zooms.append(atlas.property('uvZoom'))
        assert 1.0 < zooms[0] < zooms[1] < zooms[2]
        checks['zoom'] = zooms

        wireframe.setChecked(False)
        assert native.isVisible() and not wireframe.isChecked()
        wireframe.setChecked(True)
        assert native.isVisible() and wireframe.isChecked()
        checks['wireframe'] = True

        splitter = view.findChild(QtWidgets.QSplitter, 'meshUVSplitter')
        assert splitter and not splitter.opaqueResize()
        handle = splitter.handle(1)
        self.keep.extend([splitter, handle])
        sizes = splitter.sizes()
        start = QtCore.QPointF(handle.rect().center())

        def verify_drag():
            assert splitter.sizes() == sizes

        self.drag(handle, start, start - QtCore.QPointF(30, 0), verify_drag)
        assert splitter.sizes() != sizes
        splitter.setSizes(sizes)
        checks['splitterResizesOnRelease'] = True
        self.result['atlasPaint'] = self.timings(atlas.repaint)
        self.result['atlasPaint']['size'] = [atlas.width(), atlas.height()]

        self.key(viewport, QtCore.Qt.Key_3)
        self.key(viewport, QtCore.Qt.Key_B)
        self.drag(viewport, QtCore.QPointF(1, 1),
                  QtCore.QPointF(width - 1, height - 1), lambda: None)
        assert atlas.property('uvSelectionCount') > 1 and native.isVisible()
        checks['meshBoxSelection'] = True
        self.key(viewport, QtCore.Qt.Key_A, QtCore.Qt.AltModifier)
        self.key(viewport, QtCore.Qt.Key_B)

    def tick(self):
        try:
            if time.monotonic() > self.deadline:
                raise RuntimeError('Timed out at ' + self.phase)
            if self.phase == 'capture':
                bridge = sys.modules.get('renderdoc_mcp_bridge')
                if not bridge:
                    return
                bridge._cancel_project_auto_activation()
                bridge._schedule_project_auto_activation = lambda: None
                pyrenderdoc.LoadCapture(self.capture, rd.ReplayOptions(), self.capture, False, True)
                self.phase = 'chat'
            elif self.phase == 'chat':
                if not pyrenderdoc.IsCaptureLoaded():
                    return
                self.window = pyrenderdoc.GetMainWindow().Widget()
                self.keep.append(self.window)
                view = pyrenderdoc.GetMeshPreview().Widget()
                toggle = view.findChild(QtWidgets.QToolButton, 'meshUVToggle')
                self.keep.extend([view, toggle])
                assert toggle.isChecked()
                self.result['uvDefaultOpen'] = True
                self.window.findChild(QtWidgets.QAction, 'action_AI_Assistant').trigger()
                self.phase = 'cbuffer-link'
            elif self.phase == 'cbuffer-link':
                if not self.targets:
                    if (self.root/'uv-link.json').is_file():
                        self.open_uv_link()
                        return
                    self.start_worker("""
from src.visual_replies import save_navigation_target
ctx=c.call('get_current_renderdoc_context',{})
observed=c.call('get_mesh_uv_navigation_target',dict(event_id=1291,stage='vs-in',
    channel='_input1',pair=0,texture_resource_id='4047'))
link=save_navigation_target(observed['target'],ctx['capture'])
(root/'uv-link.json').write_text(json.dumps(link),encoding='utf-8')
""", 'uv-link-worker')
                    self.phase = 'uv-link'
                    return
                self.target = self.targets.pop(0)
                self.expected = json.loads(self.target.read_text(encoding='utf-8'))['target']
                pyrenderdoc.SetEventID([], 1326, 1326, True)
                self.link('rdoc://cbuffer/' + self.target.stem)
                self.phase = 'cbuffer-check'
            elif self.phase == 'cbuffer-check':
                widgets = self.window.findChildren(QtWidgets.QWidget)
                self.keep.extend(widgets)
                view = next((w for w in widgets if w.property('cbufferNavigationStatus') == 'selected' and
                             w.property('cbufferNavigationPath') == self.expected['path']), None)
                if view is None:
                    return
                assert int(pyrenderdoc.CurEvent()) == self.expected['eventId']
                self.result['cbuffer'].append(self.expected)
                self.phase = 'cbuffer-link'
            elif self.phase == 'uv-link':
                if self.worker.poll() is None:
                    return
                self.worker_log.close()
                if self.worker.returncode:
                    raise RuntimeError((self.root/'uv-link-worker.log').read_text(encoding='utf-8'))
                self.open_uv_link()
            elif self.phase == 'uv-view':
                view = pyrenderdoc.GetMeshPreview().Widget()
                atlas = view.findChild(QtWidgets.QWidget, 'meshUVCanvas')
                mesh = view.findChild(QtWidgets.QWidget, 'meshUVTextureView')
                self.keep.extend([view, atlas, mesh])
                if (atlas is None or not atlas.property('uvTextureReady') or
                        not atlas.property('uvTriangleCount') or mesh is None or
                        not mesh.property('uvTextureRendered')):
                    if time.monotonic() - self.uv_started > 25:
                        self.result['uvPending'] = dict(
                            visible=view.isVisible(), atlasVisible=atlas.isVisible() if atlas else False,
                            triangleCount=atlas.property('uvTriangleCount') if atlas else None,
                            textureReady=atlas.property('uvTextureReady') if atlas else None,
                            gpuRendered=mesh.property('uvTextureRendered') if mesh else None)
                        raise RuntimeError('UV data did not reach both previews')
                    return
                channel = view.findChild(QtWidgets.QComboBox, 'meshUVChannels')
                texture = view.findChild(QtWidgets.QComboBox, 'meshUVTextures')
                mode = view.findChild(QtWidgets.QComboBox, 'visualisation')
                native = view.findChild(QtWidgets.QWidget, 'render')
                wireframe = view.findChild(QtWidgets.QToolButton, 'wireframeRender')
                self.keep.extend([channel, texture, mode, native, wireframe])
                if int(pyrenderdoc.CurEvent()) != 1291 or not atlas.isVisible() or not mesh.isVisible():
                    if time.monotonic() - self.uv_started < 8:
                        return
                    self.result['uvVisibility'] = dict(
                        event=int(pyrenderdoc.CurEvent()), view=view.isVisible(),
                        atlas=atlas.isVisible(), mesh=mesh.isVisible(), visualisation=mode.currentText())
                    raise RuntimeError('UV navigation did not expose the linked mesh/texture mode')
                assert not native.isVisible()
                self.result.update(triangleCount=atlas.property('uvTriangleCount'), channel=channel.currentText(),
                                   texture=texture.currentText(), textureChoices=texture.count())
                assert channel.currentIndex() >= 0 and texture.currentIndex() > 0
                wireframe.setChecked(False)
                self.native_state = (view, atlas, native, mode, wireframe)
                self.textured_view = mesh
                self.phase = 'texture-wire-off'
            elif self.phase == 'texture-wire-off':
                if self.textured_view.property('uvWireframeRendered'):
                    return
                self.native_state[-1].setChecked(True)
                self.phase = 'texture-wire-on'
            elif self.phase == 'texture-wire-on':
                if not self.textured_view.property('uvWireframeRendered'):
                    return
                view, atlas, native, mode, wireframe = self.native_state
                assert self.textured_view.isVisible() and not native.isVisible()
                self.result['checks'] = dict(gpuTexture=True, texturedWireframe=True)
                wireframe.setChecked(False)
                mode.setCurrentIndex(int(rd.Visualisation.Solid))
                self.phase = 'native-view'
            elif self.phase == 'native-view':
                view, atlas, native, mode, wireframe = self.native_state
                assert native.isVisible()
                self.verify_native_selection(view, atlas, native, mode, wireframe)
                view.grab().save(str(self.root/'mesh-uv.png'))
                self.start_worker("""
data=c.call('inspect_vertex_effect_scope',dict(event_id=1310,observation_event_id=1654))
(root/'vertex-scope.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
""", 'vertex-scope-worker')
                self.phase = 'vertex-scope'
            elif self.phase == 'vertex-scope':
                if self.worker.poll() is None:
                    return
                self.worker_log.close()
                if self.worker.returncode:
                    raise RuntimeError((self.root/'vertex-scope-worker.log').read_text(encoding='utf-8'))
                self.result['vertexScope'] = json.loads((self.root/'vertex-scope.json').read_text(encoding='utf-8'))
                self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def open_uv_link(self):
        link = json.loads((self.root/'uv-link.json').read_text(encoding='utf-8'))
        self.link(link['url'])
        self.result['uvTarget'] = link['target']
        self.phase = 'uv-view'
        self.uv_started = time.monotonic()

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)


runner = Probe()
sys.modules['__main__']._bread_uv_probe = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
