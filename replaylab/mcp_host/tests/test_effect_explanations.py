import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from src import server
from src.effect_experiments import edited_source, verified_comparison
from src.visual_replies import publish, _json
from test_shader_numeric_validation import FakeBridge, validation, descriptor, native_blob, packed


def test_local_explanation_cannot_touch_frame_publication(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Local explanation reached global state')
    monkeypatch.setattr(server,'_frame_render_flow_record',forbidden)
    result=json.loads(server.publish_frame_render_flow('any',[],title='Local rim light'))
    assert 'publish_visual_reply' in result['error'] and 'No frame diagram was changed' in result['error']


def test_effect_native_uses_same_later_output_and_restores_prior_mapping():
    bridge=FakeBridge(existing=True)
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    bridge.ctrl.output_values[202]=packed('f',2.)
    result=validation.compare_candidates(bridge,bridge.params)
    assert result.get('effectObservation',{}).get('conclusive') and result['replayRestored'],result
    assert result['effectObservation']['metrics'][0]['maximumAbsoluteError']==pytest.approx(1.)
    assert [c[1]for c in bridge.ctrl.calls if c[0]=='event'].count(80)==3
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_vertex_effect_trial_has_same_restored_output_contract():
    bridge=FakeBridge(existing=True)
    bridge._vertex_geometry_scope=lambda *args:dict(complete=True,matches=[])
    bridge._available_shader_stages=lambda stages:[('vertex','vertex')]
    pipe=bridge.ctrl.GetPipelineState()
    pipe.GetShaderReflection=lambda stage:None
    bridge.ctrl.GetPipelineState=lambda:pipe
    bridge.params['stage']='vertex'
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result.get('effectObservation',{}).get('conclusive') and result['replayRestored'],result
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_vertex_trial_switches_and_restores_a_duplicate_geometry_shader():
    from types import SimpleNamespace as NS

    bridge = FakeBridge(existing=True)
    bridge._available_shader_stages = lambda stages: [('vertex', 'vertex')]
    bridge.params['stage'] = 'vertex'
    bridge.params['effect_observation'] = dict(eventId=80, attachmentIndex=0, roi=[0., 0., 1., 1.])
    original_bytes = native_blob('captured')
    digest = hashlib.sha256(original_bytes).hexdigest()
    bridge.event['expectedOriginalSha256'] = digest
    bridge._vertex_geometry_scope = lambda *args: dict(complete=True, matches=[dict(
        eventId=43, relation='duplicated-geometry', shaderResourceId=8,
        shaderRawSha256=digest, entryPoint='MainPS', depthTarget=97, colorTargets=[])])
    bridge._select_event_for_read = lambda ctrl, event: setattr(ctrl, 'event_id', event)
    pipe = bridge.ctrl.GetPipelineState()
    pipe.GetShader = lambda stage: 8
    pipe.GetShaderReflection = lambda stage: NS(raw=original_bytes, readWriteResources=[])
    bridge.ctrl.GetPipelineState = lambda: pipe
    peer_mapping = {}
    events = []
    replace, remove = bridge._replace_shared_shader_resource, bridge._remove_shared_shader_resource
    read = bridge.ctrl.GetTextureData

    def apply_peer(ctrl, api, original, target):
        if original == 8:
            peer_mapping[original] = target
        else:
            replace(ctrl, api, original, target)

    def restore_peer(ctrl, api, original):
        if original == 8:
            peer_mapping.pop(original, None)
        else:
            remove(ctrl, api, original)

    def observe(resource, subresource):
        events.append((bridge.ctrl.mapping, peer_mapping.get(8)))
        return read(resource, subresource)

    bridge._replace_shared_shader_resource = apply_peer
    bridge._remove_shared_shader_resource = restore_peer
    bridge.ctrl.GetTextureData = observe
    result = validation.compare_candidates(bridge, bridge.params)
    assert result.get('effectObservation', {}).get('conclusive'), result
    assert result['effectObservation']['coordinatedShaderCount'] == 2
    assert len(events) == 3 and all(main == peer for main, peer in events)
    assert events[0] == events[2] and events[0] != events[1]
    assert not peer_mapping and bridge.ctrl.mapping == 999 and not bridge.ctrl.targets


def test_compute_effect_requires_a_separate_storage_observation_route():
    bridge=FakeBridge();bridge.params['stage']='compute'
    bridge.params['effect_observation']=dict(eventId=80,roi=[0.,0.,1.,1.])
    with pytest.raises(ValueError,match='vertex/fragment'):
        validation.compare_candidates(bridge,bridge.params)


def test_effect_failure_also_restores_prior_mapping():
    bridge=FakeBridge(existing=True);bridge.ctrl.observe_failure=2
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result['replayRestored'] and result['sampleCount']==0
    assert bridge.ctrl.mapping==999 and bridge.ctrl.event_id==17 and not bridge.ctrl.targets


def test_later_output_refreshes_binding_instead_of_using_analysis_anchor_cache():
    from copy import copy
    from types import SimpleNamespace as NS
    bridge=FakeBridge()
    later=copy(bridge.ctrl.texture);later.resourceId=88
    output=copy(bridge.ctrl.output);output.resource=88
    bridge.ctrl.GetTextures=lambda:[bridge.ctrl.texture,later]
    bridge.ctrl.GetPipelineState=lambda:NS(GetOutputTargets=lambda:[output if bridge.ctrl.event_id==80 else bridge.ctrl.output],
                                          GetDepthTarget=lambda:bridge.ctrl.depth)
    # This fast replay API intentionally leaves the public pipeline cache stale.
    bridge.ctrl.ReplayFrameEventForAnalysis=lambda event:bridge.ctrl.calls.append(('analysis-only',event))
    bridge.params['effect_observation']=dict(eventId=80,attachmentIndex=0,roi=[0.,0.,1.,1.])
    result=validation.compare_candidates(bridge,bridge.params)
    assert result['metrics']['outputs'][0]['output']['resourceId']==88
    assert not any(call[0]=='analysis-only'for call in bridge.ctrl.calls)


def test_effect_roi_crops_stored_pixels_and_matches_display_flip():
    desc=dict(descriptor('UInt',width=1,pixels=2),height=2)
    first=validation._effect_roi([(desc,bytes([1,2,3,4]))],[0.,0.,.5,.5])
    flipped=validation._effect_roi([(desc,bytes([1,2,3,4]))],[0.,0.,.5,.5],True)
    assert first[0][1]==bytes([1]) and flipped[0][1]==bytes([3])


def test_present_uses_actual_destination_and_rejects_inherited_compute_targets():
    from copy import copy
    from types import SimpleNamespace as NS
    bridge=FakeBridge();dest=copy(bridge.ctrl.texture);dest.resourceId=99
    bridge.ctrl.GetTextures=lambda:[bridge.ctrl.texture,dest]
    bridge.ctrl.GetRootActions=lambda:[NS(flags=256,copyDestination=99,copyDestinationSubresource=NS(mip=0,slice=0))]
    bridge._find_action_by_event=lambda actions,event:actions[0]
    bridge.rd.ActionFlags=NS(Present=256,Drawcall=2)
    result=validation._effect_outputs(bridge,bridge.ctrl,100,{'attachmentIndex':0})
    assert result[0][0]['resourceId']==99
    bridge.ctrl.GetRootActions=lambda:[NS(flags=4)]
    with pytest.raises(ValueError,match='inherited RTVs'):validation._effect_outputs(bridge,bridge.ctrl,100,{})


@pytest.mark.parametrize('edits',[
    [],[dict(old_text='missing',new_text='0')],[dict(old_text='a',new_text='a')],
    [dict(old_text='x',new_text='0',offset=4)]])
def test_effect_edits_reject_ambiguous_or_unbounded_trials(edits):
    with pytest.raises(ValueError):edited_source('a a',edits)


def test_temporary_edits_normalize_windows_newlines():
    assert edited_source('a\r\nb\r\n',[dict(old_text='a\nb',new_text='c\nd')])=='c\nd\n'


def test_model_schema_describes_nested_cards_and_exact_edit_keys():
    from mcp.server.fastmcp.tools.base import Tool
    effect=Tool.from_function(server.run_shader_effect_experiment,structured_output=False).parameters
    visual=Tool.from_function(server.publish_visual_reply).parameters
    assert set(effect['$defs']['ExactSourceEdit']['properties'])=={'old_text','new_text'}
    assert visual['$defs']['Card']['additionalProperties']is False
    assert 'attachmentId'not in visual['$defs']['ImageSource']['properties']


def test_experiment_comparison_rejects_stale_or_wrong_image_pair(tmp_path):
    receipt=dict(schema='bread-shader-effect-experiment/v1',verified=True,captureSessionId='session',
        replayRestored=True,sourcePreserved=True,images=[{'sha256':'a'*64},{'sha256':'b'*64}],
        outcome='changed',roi=[0.,0.,1.,1.],observationEventId=80,replacementScope='shared-shader-resource',
        observation={'effectObservation':{'metrics':[]}})
    raw=_json(receipt);path=tmp_path/(hashlib.sha256(raw).hexdigest()+'.json');path.write_bytes(raw)
    assert verified_comparison(path,{'captureSessionId':'session'},dict(sourceSha256='a'*64),dict(sourceSha256='b'*64))['verified']
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'other'},dict(sourceSha256='a'*64),dict(sourceSha256='b'*64))
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'session'},dict(sourceSha256='b'*64),dict(sourceSha256='a'*64))
    path.write_bytes(raw+b' ')
    with pytest.raises(ValueError):verified_comparison(path,{'captureSessionId':'session'},{},{})


def test_thumbnail_flow_and_saved_source_card_keep_evidence(tmp_path):
    image=tmp_path/'node.png';Image.new('RGB',(8,8),'blue').save(image)
    result=publish('Effect',[
        {'type':'diagram','title':'Flow','nodes':[{'id':'a','text':'Before','image':{'path':str(image)},'calculation':'x * rim'},
                                                 {'id':'b','text':'After','eventId':80}],
         'edges':[['a','b']],'edgeLabels':['Compositing']},
        {'type':'code','title':'Actual source','workspacePath':'managed','expectedSourceSha256':'a'*64,'startLine':3,'endLine':4}
    ],{},capture_image=lambda _:None,capture_mesh=lambda _:None,authorize_file=Path,
    read_code=lambda spec:dict(text='float rim = 0;\nreturn rim;',startLine=spec.startLine,url='rdoc://shader/id?line=3'),root=tmp_path/'visuals')
    manifest=json.loads(Path(result['path']).read_bytes())
    assert manifest['cards'][0]['nodes'][0]['image']['sha256']
    assert manifest['cards'][0]['edgeLabels']==['Compositing']
    assert manifest['cards'][1]['code']['startLine']==3


def test_new_tools_declare_effects_and_long_replay_scope():
    from replaylab_agent.policy import tool_call_effects
    from src.host_runtime import LONG_TOOLS,REPLAY_TOUCH_TOOLS
    assert tool_call_effects('run_shader_effect_experiment',{})==frozenset({'workspace','replay'})
    assert {'run_shader_effect_experiment','publish_visual_reply','get_cbuffer_parameter_link'}<=LONG_TOOLS&REPLAY_TOUCH_TOOLS


def test_effect_tool_delivers_both_pixels_and_readable_slider_sources(tmp_path,monkeypatch):
    from src import effect_experiments
    monkeypatch.setattr(server,'_EVENT_TEXTURE_EVIDENCE_ROOT',tmp_path)
    folder=server._event_texture_evidence_root()/'shader-effect-experiments'/'trial'
    server._secure_event_artifact_directory(folder,tmp_path)
    images=[]
    for name,color in [('before','white'),('after','blue')]:
        path=folder/(name+'-display.png');Image.new('RGB',(1600,900),color).save(path)
        images.append(dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        assert server._within_evidence_roots(str(path))==path
    result=dict(ok=True,images=images,roi=[.5,.2,.55,.3],receiptPath='receipt')
    monkeypatch.setattr(effect_experiments,'run',lambda *args,**kwargs:result)
    blocks=server.run_shader_effect_experiment('workspace','a'*64,[],2111)
    assert json.loads(blocks[0])['receiptPath']=='receipt'
    assert len([b for b in blocks if not isinstance(b,str)])==4
    previews=[json.loads(b)for b in blocks[1:]if isinstance(b,str)]
    assert [(p['leg'],p['view'])for p in previews]==[
        ('before','context'),('before','region'),('after','context'),('after','region')]
    assert all(max(p['width'],p['height'])<=960 and p['inlineImage']=='included in this result'for p in previews)
