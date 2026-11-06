"""Visual answer contracts: provenance, immutable snapshots, bounds and task routing."""
import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from replaylab_agent.agent_report import TaskReportStore
from src.visual_replies import Card, canonical_mesh, obj_mesh, publish


def test_snapshots_preserve_provenance_marks_and_material_without_touching_sources(tmp_path):
    source=tmp_path/'source.png';Image.new('RGB',(96,64),(20,40,90)).save(source)
    original=source.read_bytes()
    capture=tmp_path/'test.rdc';capture.write_bytes(b'capture')
    ctx={'capture':{'loaded':True,'local':True,'path':str(capture),'captureSessionId':'session'}}
    calls=[]
    def acquire(spec):calls.append(spec.model_dump());return {'path':str(source)}
    cards=[{'type':'compare','title':'Stages','image':{'eventId':12},'secondImage':{'path':str(source)},
            'annotations':[{'type':'arrow','points':[[.9,.2],[.3,.5]],'color':'#2060ff','text':'Blue tip'}]},
           {'type':'mesh','title':'Material principle','primitive':'sphere',
            'material':{'environmentColor':[.1,.2,1.0],'environmentStrength':.5}}]
    args=dict(capture_image=acquire,capture_mesh=lambda _:None,authorize_file=Path,root=tmp_path/'visuals')
    receipt=publish('Answer',cards,ctx,**args)
    raw=Path(receipt['path']).read_bytes();manifest=json.loads(raw)
    assert hashlib.sha256(raw).hexdigest()==receipt['id']
    assert publish('Answer',cards,ctx,**args)['id']==receipt['id']
    a,b=manifest['cards'][0]['image'],manifest['cards'][0]['secondImage']
    assert a['provenance']=='captured' and b['provenance']=='reference'
    assert manifest['cards'][0]['annotations'][0]['points'][-1]==[.3,.5]
    assert manifest['cards'][1]['material']['environmentColor']==[.1,.2,1.0]
    assert a['sha256']==b['sha256'] and len(calls)==2
    assert source.read_bytes()==original and capture.read_bytes()==b'capture'
    assert manifest['replayModified'] is False and receipt['applied'] is False
    assert manifest['capture']['size']==7
    blob=tmp_path/'visuals/blobs'/a['file']
    assert hashlib.sha256(blob.read_bytes()).hexdigest()==a['sha256']
    source.write_bytes(b'changed')
    assert blob.is_file() and Path(receipt['path']).read_bytes()==raw


@pytest.mark.parametrize('data',[
    {'type':'image','title':'x','image':{'path':'x','eventId':1}},
    {'type':'image','title':'x','image':{'eventId':1},'annotations':[{'type':'arrow','points':[[.2,.2]]}]},
    {'type':'image','title':'x','image':{'eventId':1},'annotations':[{'type':'pen','points':[[1.1,0]]}]},
    {'type':'mesh','title':'x','primitive':'sphere','eventId':1},
    {'type':'mesh','title':'x','primitive':'sphere','material':{'roughness':float('nan')}},
    {'type':'diagram','title':'x','nodes':[{'id':'a','text':'x'}],'edges':[['a','missing']]},
    {'type':'diagram','title':'x','nodes':[{'id':'a','text':'x'}],'links':[{'label':'x','url':'https://example.com'}]},
])
def test_rejects_ambiguous_sources_invalid_marks_and_unbounded_input(data):
    with pytest.raises(ValueError):Card.model_validate(data)


def test_mesh_topology_and_obj_uvs_are_bounded_and_honestly_scoped():
    mesh={'positions':[[1,2,3,4],[2,3,4,5],[3,4,5,6],[4,5,6,7]],'indices':[0,1,2,3],
          'topology':'TriangleStrip','coordinateSpace':'clip','viewport':{'width':200,'height':100},'complete':False}
    result=canonical_mesh(mesh)
    assert result['triangles']==[[0,1,2],[2,1,3]]
    assert result['vertices'][0]==[2.,2.,4.] and result['space']=='approximate-view' and not result['complete']
    obj=obj_mesh(b'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf -4/-4 -3/-3 -2/-2 -1/-1\n')
    assert obj['triangles']==[[0,1,2],[0,2,3]] and obj['uvs'][-1]==[0.,1.]
    with pytest.raises(ValueError):canonical_mesh(dict(mesh,indices=[0,1,99]))
    with pytest.raises(ValueError):obj_mesh(b'v nan 0 0\nf 1 1 1')


def test_task_registration_merges_across_store_instances(tmp_path):
    first=TaskReportStore('visual-task',tmp_path,conversation_id='conversation')
    second=TaskReportStore('visual-task',tmp_path,conversation_id='conversation')
    assert first.register_reply_visual('a'*64)
    assert second.register_reply_visual('b'*64)
    assert first.register_reply_visual('a'*64)
    state=json.loads((tmp_path/'visual-task.report-state.json').read_text(encoding='utf-8'))
    assert {row['id']for row in state['replyVisuals']}=={'a'*64,'b'*64}
    assert state['conversationId']=='conversation'
    with pytest.raises(ValueError):first.register_reply_visual('../wrong')


def test_mesh_effects_and_node_navigation_are_optional_bounded_extensions():
    old=Card.model_validate({'type':'mesh','title':'Old saved message','primitive':'sphere'})
    assert old.meshEffect.type=='none'
    demo=Card.model_validate({'type':'mesh','title':'Outline','primitive':'sphere',
                              'meshEffect':{'type':'inverted-hull','amount':.08}})
    assert demo.meshEffect.enabled and demo.meshEffect.amount==.08
    with pytest.raises(ValueError):Card.model_validate(dict(type='mesh',title='x',primitive='sphere',meshEffect={'type':'arbitrary-shader','source':'unsafe'}))
    with pytest.raises(ValueError):Card.model_validate(dict(type='mesh',title='x',primitive='sphere',meshEffect={'type':'inverted-hull','amount':1e20}))
    with pytest.raises(ValueError):Card.model_validate(dict(type='diagram',title='x',nodes=[dict(id='a',text='x',links=[dict(label='x',url='file:///outside')])]))


def test_files_are_authorized_and_mesh_evidence_is_capture_bound(tmp_path):
    def denied(path):raise ValueError('Outside evidence roots')
    with pytest.raises(ValueError,match='Outside'):
        publish('x',[{'type':'image','title':'x','image':{'path':'secret.png'}}],{},
                capture_image=lambda _:None,capture_mesh=lambda _:None,authorize_file=denied,root=tmp_path)
    with pytest.raises(ValueError,match='capture/EID'):
        publish('x',[{'type':'mesh','title':'x','eventId':4}],{'capture':{'captureSessionId':'new'}},
                capture_image=lambda _:None,capture_mesh=lambda _:dict(eventId=4,captureSessionId='old'),
                authorize_file=denied,root=tmp_path)
