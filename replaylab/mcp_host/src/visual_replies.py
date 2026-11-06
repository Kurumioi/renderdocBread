"""Immutable, bounded visual answers. Presentation never changes capture/replay.

Only content-addressed local PNG/JSON assets reach the native chat renderer.
Captured sources are acquired by the Host; file references and material demos
remain explicitly illustrative. Native clients independently verify every hash.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
_HEX = re.compile(r"^[0-9a-f]{64}$")


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImageSource(Spec):
    eventId: int = Field(default=0, ge=0, le=0xffffffff)
    path: str = Field(default="", max_length=32768)
    attachmentKind: Literal["rtv", "dsv", "uav", "srv", "present"] = "rtv"
    attachmentIndex: int = Field(default=0, ge=0, le=4095)
    channel: Literal["rgba", "r", "g", "b", "a", "depth"] = "rgba"
    mip: int = Field(default=0, ge=0, le=31)
    slice: int = Field(default=0, ge=0, le=0xffffffff)
    sample: int = Field(default=0, ge=0, le=0xffffffff)
    stage: Literal['fragment','vertex','geometry','compute','hull','domain','task','mesh'] = 'fragment'
    flipY: bool | None = None
    rangeMode: Literal["native", "explicit", "visible-distribution"] = "native"
    blackPoint: float | None = None
    whitePoint: float | None = None

    @model_validator(mode="after")
    def source(self):
        if bool(self.eventId) == bool(self.path):
            raise ValueError("Choose exactly one captured eventId or evidence-file path")
        if any(x is not None and not math.isfinite(x) for x in (self.blackPoint,self.whitePoint)):
            raise ValueError("Display points must be finite")
        return self


class Mark(Spec):
    type: Literal["ellipse", "rect", "arrow", "label", "pen"]
    points: list[list[float]] = Field(min_length=1, max_length=128)
    color: str = Field(default="#ff4050", pattern=r"^#[a-fA-F0-9]{6}$")
    text: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def coordinates(self):
        if any(len(p)!=2 or any(not math.isfinite(v) or not 0<=v<=1 for v in p) for p in self.points):
            raise ValueError("Annotation points must be normalized to the saved image, top-left origin")
        if self.type in {"ellipse","rect","arrow"} and len(self.points)!=2:
            raise ValueError("Circle, rectangle and arrow require two points; the arrow tip is last")
        return self


class Link(Spec):
    label: str = Field(min_length=1,max_length=160)
    url: str = Field(max_length=8192)

    @model_validator(mode="after")
    def navigation(self):
        u=urlsplit(self.url)
        if (u.scheme!="rdoc" or u.netloc not in {"event","eid","resource","texture","buffer","shader","mesh","cbuffer","uv"}
                or any(ord(c)<32 for c in self.url+self.label)):
            raise ValueError("Visual links must be RenderDoc event/resource/Shader navigation")
        return self


class Material(Spec):
    baseColor: list[float] = Field(default_factory=lambda:[.65,.68,.75], min_length=3,max_length=3)
    roughness: float = Field(default=.4,ge=.02,le=1)
    metalness: float = Field(default=0,ge=0,le=1)
    environmentColor: list[float] = Field(default_factory=lambda:[.2,.2,.2],min_length=3,max_length=3)
    environmentStrength: float = Field(default=.25,ge=0,le=2)
    texture: ImageSource | None = None

    @model_validator(mode="after")
    def finite(self):
        if any(not math.isfinite(v) or not 0<=v<=1 for v in self.baseColor+self.environmentColor):
            raise ValueError("Material baseColor must be three finite values in 0..1")
        return self


class Node(Spec):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    text: str = Field(min_length=1,max_length=240)
    eventId: int = Field(default=0,ge=0,le=0xffffffff)
    image: ImageSource | None = None
    calculation: str = Field(default='',max_length=400)
    description: str = Field(default='',max_length=400)
    links: list[Link] = Field(default_factory=list,max_length=4)


class MeshEffect(Spec):
    type: Literal['none','inverted-hull','sine-displacement'] = 'none'
    amount: float = Field(default=.04,ge=0,le=.25)
    frequency: float = Field(default=6.,ge=.1,le=20)
    phase: float = Field(default=0.,ge=-100,le=100)
    color: str = Field(default='#101018',pattern=r'^#[a-fA-F0-9]{6}$')
    enabled: bool = True


class Card(Spec):
    type: Literal["image","compare","mesh","diagram","code"]
    title: str = Field(min_length=1,max_length=160)
    caption: str = Field(default="",max_length=1600)
    image: ImageSource | None = None
    secondImage: ImageSource | None = None
    beforeLabel: str = Field(default="",max_length=120)
    afterLabel: str = Field(default="",max_length=120)
    annotations: list[Mark] = Field(default_factory=list,max_length=32)
    secondAnnotations: list[Mark] = Field(default_factory=list,max_length=32)
    eventId: int = Field(default=0,ge=0,le=0xffffffff)
    instance: int = Field(default=0,ge=0,le=0xffffffff)
    stage: Literal["vs-out","gs-out"] = "vs-out"
    meshFile: str = Field(default="",max_length=32768)
    primitive: Literal["sphere","cube","plane"] | None = None
    material: Material = Field(default_factory=Material)
    meshEffect: MeshEffect = Field(default_factory=MeshEffect)
    links: list[Link] = Field(default_factory=list,max_length=6)
    nodes: list[Node] = Field(default_factory=list,max_length=12)
    edges: list[list[str]] = Field(default_factory=list,max_length=24)
    edgeLabels: list[str] = Field(default_factory=list,max_length=24)
    workspacePath: str = Field(default='',max_length=32768)
    sourceFile: str = Field(default='',max_length=2048)
    startLine: int = Field(default=1,ge=1,le=10000000)
    endLine: int = Field(default=1,ge=1,le=10000000)
    expectedSourceSha256: str = Field(default='',max_length=64)
    exampleCode: str = Field(default='',max_length=16000)
    experimentReceipt: str = Field(default='',max_length=32768)

    @model_validator(mode="after")
    def shape(self):
        if self.type in {"image","compare"} and self.image is None:
            raise ValueError("Image cards require an image source")
        if self.type=="compare" and self.secondImage is None:
            raise ValueError("Comparison cards require both sources")
        if self.type=="mesh" and sum((bool(self.eventId),bool(self.meshFile),bool(self.primitive)))!=1:
            raise ValueError("Mesh needs one eventId, exported meshFile, or illustrative primitive")
        ids={n.id for n in self.nodes}
        if len(ids)!=len(self.nodes) or any(len(e)!=2 or any(x not in ids for x in e) for e in self.edges):
            raise ValueError("Diagram edges must connect unique declared node IDs")
        if self.type=="diagram" and not self.nodes:
            raise ValueError("Diagram requires nodes")
        if self.edgeLabels and (len(self.edgeLabels)!=len(self.edges) or any(len(v)>240 for v in self.edgeLabels)):
            raise ValueError('Edge labels must match edges and contain at most 240 characters')
        if self.type=='code' and (not self.workspacePath or not _HEX.fullmatch(self.expectedSourceSha256)
                                  or not self.startLine<=self.endLine<self.startLine+120):
            raise ValueError('Code cards need an exact saved-source hash and a range of at most 120 lines')
        if sum(len(m.points) for m in self.annotations+self.secondAnnotations)>1024:
            raise ValueError("Visual annotation point budget exceeded")
        return self


def root_path() -> Path:
    local=os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    return (Path(local) if local else Path.home()/".local/share")/"ReplayLab/ai-visuals"


def _write(path: Path, data: bytes):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Visual storage must not be redirected")
    if path.is_file() and path.read_bytes()==data:return
    fd,name=tempfile.mkstemp(prefix=".visual-",dir=str(path.parent))
    try:
        with os.fdopen(fd,"wb") as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def _json(obj):
    return json.dumps(obj,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":")).encode("utf-8")


def save_navigation_target(target: dict, capture: dict) -> dict:
    """Mint a durable navigation identity from Host-observed reflection only."""
    identity=dict(capture)
    if identity.get('local') and identity.get('path'):
        st=Path(identity['path']).stat();identity.update(size=st.st_size,mtimeMs=st.st_mtime_ns//1_000_000)
    data=_json({'schema':'bread-ai-navigation/v1','capture':identity,'target':target})
    digest=hashlib.sha256(data).hexdigest()
    _write(root_path()/'targets'/(digest+'.json'),data)
    kind = 'uv' if target.get('kind') == 'uv' else 'cbuffer'
    return {'id':digest,'url':f'rdoc://{kind}/{digest}','target':target}


def _asset(root: Path, data: bytes, suffix: str):
    if len(data)>MAX_ASSET_BYTES:raise ValueError("Visual asset exceeds 8 MiB")
    digest=hashlib.sha256(data).hexdigest()
    _write(root/"blobs"/(digest+suffix),data)
    return {"sha256":digest,"file":digest+suffix,"bytes":len(data)}


def _triangles(indices,topology):
    strip=[]
    for index in indices:
        if index is None:strip=[];continue
        strip.append(index)
        if topology=="TriangleList" and len(strip)==3:yield strip;strip=[]
        elif topology=="TriangleStrip" and len(strip)>=3:
            a,b,c=strip[-3:];yield [b,a,c] if len(strip)%2==0 else [a,b,c]
        elif topology=="TriangleFan" and len(strip)>=3:yield [strip[0],strip[-2],strip[-1]]


def canonical_mesh(mesh: dict) -> dict:
    """Use real output positions, with an explicitly approximate view reconstruction."""
    positions=mesh.get("positions",[])
    if not 1<=len(positions)<=16384:raise ValueError("No bounded captured geometry")
    clip=mesh.get("coordinateSpace")=="clip"
    viewport=mesh.get("viewport") or {}
    aspect=float(viewport.get("width") or 1)/max(1,float(viewport.get("height") or 1))
    vertices=[]
    for p in positions:
        if p is None:vertices.append([0.,0.,0.]);continue
        if len(p) not in (3,4) or any(not isinstance(v,(int,float)) or not math.isfinite(v) or abs(v)>1e12 for v in p):
            raise ValueError("Invalid captured positions")
        # Clip W carries perspective depth. Projection scale/FOV is not known;
        # this is a geometry illustration, never a pixel-coverage/shading proof.
        vertices.append([float(p[0])*aspect,float(p[1])*(-1 if mesh.get("flipY") else 1),
                         float(p[3] if clip and len(p)==4 else p[2])])
    indices=mesh.get("indices",[])
    if len(indices)>49152:raise ValueError("Geometry index budget exceeded")
    if any(i is not None and (isinstance(i,bool) or not isinstance(i,int) or not 0<=i<len(vertices)) for i in indices):
        raise ValueError("Invalid geometry indices")
    triangles=list(_triangles(indices,mesh.get("topology")))
    if not triangles:raise ValueError("This interactive preview needs triangle geometry")
    return {"vertices":vertices,"triangles":triangles,"space":"approximate-view" if clip else "shader-output",
            "complete":bool(mesh.get("complete")),"sourceStage":mesh.get("stage"),
            "instance":mesh.get("instance",0),"limitations":mesh.get("limitations",[])}


def obj_mesh(data: bytes) -> dict:
    positions=[];texcoords=[];vertices=[];uvs=[];triangles=[];mapping={}
    for line in data.decode("utf-8",errors="strict").splitlines():
        words=line.split()
        if not words or words[0].startswith("#"):continue
        if words[0]=="v":
            p=[float(x)for x in words[1:4]]
            if len(p)!=3 or any(not math.isfinite(x) or abs(x)>1e12 for x in p):raise ValueError("Invalid OBJ position")
            positions.append(p)
        elif words[0]=="vt":texcoords.append([float(x)for x in words[1:3]])
        elif words[0]=="f":
            face=[]
            for token in words[1:]:
                fields=token.split("/");pi=int(fields[0]);pi=pi-1 if pi>0 else len(positions)+pi
                ti=int(fields[1]) if len(fields)>1 and fields[1] else 0
                ti=ti-1 if ti>0 else len(texcoords)+ti if ti<0 else -1
                if not 0<=pi<len(positions):raise ValueError("Invalid OBJ face index")
                key=(pi,ti)
                if key not in mapping:
                    mapping[key]=len(vertices);vertices.append(positions[pi])
                    uv=texcoords[ti] if 0<=ti<len(texcoords) else [0.,0.]
                    if len(uv)!=2 or any(not math.isfinite(x) or abs(x)>1e6 for x in uv):raise ValueError("Invalid OBJ UV")
                    uvs.append(uv)
                face.append(mapping[key])
            triangles.extend([[face[0],face[i],face[i+1]]for i in range(1,len(face)-1)])
        if len(positions)>65536 or len(texcoords)>65536 or len(vertices)>16384 or len(triangles)>16384:
            raise ValueError("Exported OBJ exceeds interactive mesh limits")
    if not vertices or not triangles:raise ValueError("No OBJ triangles")
    return {"vertices":vertices,"uvs":uvs,"triangles":triangles,"space":"object","complete":True}


def publish(title: str, cards: list[dict], context: dict, *, capture_image: Callable,
            capture_mesh: Callable, authorize_file: Callable, root: Path | None = None,
            read_code: Callable | None = None) -> dict:
    if not isinstance(title,str) or not 1<=len(title)<=160:raise ValueError("Visual answer needs a short title")
    if not 1<=len(cards)<=6:raise ValueError("A visual answer needs 1..6 focused cards")
    specs=[Card.model_validate(c)for c in cards]
    root=root or root_path()
    capture=dict(context.get("capture") or {})
    if capture.get("loaded") and capture.get("local") and capture.get("path"):
        st=Path(capture['path']).stat();capture.update(size=st.st_size,mtimeMs=st.st_mtime_ns//1_000_000)
    def image(source: ImageSource):
        metadata={}
        if source.eventId:
            metadata=capture_image(source)
            if not metadata.get("path"):raise ValueError("The captured image was not saved")
            path=Path(metadata['path']);provenance="captured"
        else:path=authorize_file(source.path);provenance="reference"
        if path.stat().st_size>MAX_ASSET_BYTES:raise ValueError("Source image exceeds 8 MiB")
        data=path.read_bytes()
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in {"PNG","JPEG","WEBP"} or im.mode not in {'RGB','RGBA','P','L','LA'} or not 1<=im.width<=8192 or not 1<=im.height<=8192 or im.width*im.height>16_000_000:
                raise ValueError("Unsupported or oversized visual image")
            im.load()
            im = im.convert("RGBA")
            flip = source.flipY
            if flip is None and metadata.get('resourceId'):
                selected = context.get('texture') or {}
                flip = (str(selected.get('resourceId')) == str(metadata['resourceId'])
                        and selected.get('flipY') is True)
            if flip:
                im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            output = io.BytesIO()
            im.save(output,format="PNG")
            result=_asset(root,output.getvalue(),".png")
            result.update(width=im.width,height=im.height,provenance=provenance,
                          source=dict(source.model_dump(exclude={'path'}), flipY=bool(flip)),
                          presentationFlipApplied=bool(flip),
                          sourceSha256=hashlib.sha256(data).hexdigest())
            return result
    built=[]
    for spec in specs:
        card=spec.model_dump(exclude={'image','secondImage','meshFile','material','workspacePath','sourceFile',
                                      'expectedSourceSha256','experimentReceipt','exampleCode'})
        if spec.image:card['image']=image(spec.image)
        if spec.secondImage:card['secondImage']=image(spec.secondImage)
        if spec.type=='diagram':
            card['nodes']=[]
            for node in spec.nodes:
                saved=node.model_dump(exclude={'image'})
                if node.image:
                    saved['image']=image(node.image)
                    if not saved['eventId'] and node.image.eventId:saved['eventId']=node.image.eventId
                card['nodes'].append(saved)
        if spec.type=='code':
            if read_code is None:raise ValueError('Saved Shader source reader unavailable')
            card['code']=read_code(spec)
        if spec.experimentReceipt:
            from .effect_experiments import verified_comparison
            card['experiment']=verified_comparison(authorize_file(spec.experimentReceipt),capture,
                                                    card.get('image',{}),card.get('secondImage',{}))
        if spec.type=='mesh':
            material=spec.material.model_dump(exclude={'texture'})
            if spec.material.texture:material['texture']=image(spec.material.texture)
            card['material']=material
            if spec.eventId:
                mesh=capture_mesh(spec)
                if mesh.get('eventId')!=spec.eventId or mesh.get('captureSessionId')!=capture.get('captureSessionId'):
                    raise ValueError("Mesh evidence does not match this capture/EID")
                geometry=canonical_mesh(mesh)
            elif spec.meshFile:
                path=authorize_file(spec.meshFile)
                if path.stat().st_size>MAX_ASSET_BYTES:raise ValueError("Mesh file exceeds 8 MiB")
                geometry=obj_mesh(path.read_bytes()) if path.suffix.lower()=='.obj' else canonical_mesh(json.loads(path.read_text(encoding='utf-8')))
            else:geometry=None
            if geometry:card['geometry']=_asset(root,_json(geometry),'.json');card['geometryInfo']={k:v for k,v in geometry.items()if k not in {'vertices','triangles','uvs'}}
        built.append(card)
    manifest={'schema':'bread-ai-visual/v1','title':title,'capture':capture,'cards':built,
              'replayModified':False,'materialIsIllustrative':True}
    data=_json(manifest)
    if len(data)>MAX_MANIFEST_BYTES:raise ValueError("Visual manifest is too large")
    digest=hashlib.sha256(data).hexdigest();_write(root/(digest+'.json'),data)
    return {'schema':'bread-ai-visual-receipt/v1','id':digest,'path':str(root/(digest+'.json')),
            'cardCount':len(built),'applied':False}
