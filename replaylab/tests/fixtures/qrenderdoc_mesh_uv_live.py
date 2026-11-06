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
        pyrenderdoc.CloseCapture()
        QtCore.QTimer.singleShot(100, self.app.quit)

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
                self.window.findChild(QtWidgets.QAction, 'action_AI_Assistant').trigger()
                self.phase = 'cbuffer-link'
            elif self.phase == 'cbuffer-link':
                if not self.targets:
                    self.start_worker("""
from src.visual_replies import save_navigation_target
ctx=c.call('get_current_renderdoc_context',{})
choices=c.call('get_mesh_uv_navigation_target',dict(event_id=1291,stage='vs-in',channel='?',pair=0))
channel=next((x for x in choices['channelChoices'] if 'uv' in x.lower() or 'texcoord' in x.lower()),'_input1')
texture=next((rid for rid in choices['textureResourceIds'] if c.call('get_texture_info',dict(resource_id=rid))['w']>=512),choices['textureResourceIds'][0])
observed=c.call('get_mesh_uv_navigation_target',dict(event_id=1291,stage='vs-in',channel=channel,pair=0,texture_resource_id=texture))
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
                link = json.loads((self.root/'uv-link.json').read_text(encoding='utf-8'))
                self.link(link['url'])
                self.result['uvTarget'] = link['target']
                self.phase = 'uv-view'
                self.uv_started = time.monotonic()
            elif self.phase == 'uv-view':
                view = pyrenderdoc.GetMeshPreview().Widget()
                atlas = view.findChild(QtWidgets.QWidget, 'meshUVCanvas')
                mesh = view.findChild(QtWidgets.QWidget, 'meshUVModel')
                self.keep.extend([view, atlas, mesh])
                if atlas is None or not atlas.property('uvTextureReady') or not atlas.property('uvTriangleCount'):
                    if time.monotonic() - self.uv_started > 20:
                        self.result['uvPending'] = dict(visible=view.isVisible(), atlasVisible=atlas.isVisible() if atlas else False,
                            triangleCount=atlas.property('uvTriangleCount') if atlas else None,
                            textureReady=atlas.property('uvTextureReady') if atlas else None)
                        raise RuntimeError('UV data did not reach both previews')
                    return
                assert int(pyrenderdoc.CurEvent()) == 1291 and atlas.isVisible() and mesh.isVisible()
                channel = view.findChild(QtWidgets.QComboBox, 'meshUVChannels')
                texture = view.findChild(QtWidgets.QComboBox, 'meshUVTextures')
                self.keep.extend([channel, texture])
                self.result.update(triangleCount=atlas.property('uvTriangleCount'), channel=channel.currentText(),
                                   texture=texture.currentText(), textureChoices=texture.count())
                assert channel.currentIndex() >= 0 and texture.currentIndex() > 0
                checks = self.result['checks'] = {}
                for name in ('Point', 'Edge', 'Face'):
                    buttons = view.findChildren(QtWidgets.QToolButton)
                    self.keep.extend(buttons)
                    label = QtCore.QCoreApplication.translate('BufferViewer', name)
                    button = next(b for b in buttons if b.text() == label)
                    button.click()
                    original = mesh.grab().toImage()
                    self.app.sendEvent(atlas, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_A, QtCore.Qt.NoModifier))
                    checks[name] = bool(atlas.property('uvSelectionCount')) and original != mesh.grab().toImage()
                    self.app.sendEvent(atlas, QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_A, QtCore.Qt.AltModifier))
                view.grab().save(str(self.root/'mesh-uv.png'))
                assert all(checks.values()), checks
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

    def start(self):
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(250)


runner = Probe()
sys.modules['__main__']._bread_uv_probe = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.start)
