"""Exercise the actual bounded native image/mesh renderer with Qt offscreen."""
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest

from replaylab_agent.agent_report import TaskReportStore  # initializes canonical Host import root
from src.visual_replies import publish

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform!='win32',reason='Windows Qt/MSVC fixture')
def test_native_visuals_compare_mesh_material_navigation_and_integrity(tmp_path):
    from setuptools._distutils.ccompiler import new_compiler
    first=tmp_path/'a.png';second=tmp_path/'b.png'
    asymmetric = Image.new('RGB',(300,200),(30,60,100))
    asymmetric.paste((110,20,15),(0,0,300,80))
    asymmetric.save(first)
    Image.new('RGB',(300,200),(170,90,20)).save(second)
    root=tmp_path/'ReplayLab/ai-visuals'
    receipt=publish('Visual test',[
        {'type':'compare','title':'Comparison','image':{'path':str(first)},'secondImage':{'path':str(second)},
         'annotations':[{'type':'ellipse','points':[[.2,.2],[.6,.6]],'color':'#2060ff'}],
         'links':[{'label':'EID 77','url':'rdoc://event/77'}]},
        {'type':'mesh','title':'Material','primitive':'sphere','material':{'environmentColor':[.1,.2,1.0]},
         'meshEffect':{'type':'inverted-hull','amount':.08,'color':'#2060ff'}},
        {'type':'diagram','title':'Stages','nodes':[{'id':'a','text':'Geometry','eventId':77},{'id':'b','text':'Color','eventId':88,
         'links':[{'label':'Shader calculation','url':'rdoc://shader/test?line=7'}]}],
         'edges':[['a','b']]}
    ],{'capture':{'captureSessionId':'capture-test'}},capture_image=lambda _:None,
       capture_mesh=lambda _:None,authorize_file=Path,root=root)
    probe=tmp_path/'visuals.cpp'
    probe.write_text(r'''
#include <QApplication>
#include <cstdio>
#include <QFile>
#include <QJsonDocument>
#include <QTimer>
#include "Windows/AI/AIVisualReply.cpp"
int main(int argc,char **argv) {
 QApplication app(argc,argv);QJsonObject result;QString destination;QJsonObject origin;
 AIVisualReply reply(QString::fromLocal8Bit(argv[1]),[&](const QUrl &url,const QJsonObject &capture){destination=url.toString();origin=capture;});
 reply.resize(700,500);reply.ensurePolished();reply.layout()->activate();app.processEvents();
 auto *selector=reply.findChild<QComboBox *>();
 result["threeCards"]=selector&&selector->count()==3;
 auto views=reply.findChildren<QWidget *>("aiVisualImageCanvas");
 VisualCanvas *image=nullptr,*diagram=nullptr;
 for(auto *widget:views) {auto *canvas=static_cast<VisualCanvas *>(widget);if(canvas->card["type"]=="compare")image=canvas;else if(canvas->card["type"]=="diagram")diagram=canvas;}
 auto grab=[&](QWidget *widget){app.processEvents();return widget->grab().toImage();};
 image->mode=0;QImage before=grab(image);image->mode=1;image->update();QImage after=grab(image);
 result["abReallyChangesPixels"]=before!=after;
 image->mode=2;auto *slider=reply.findChild<QSlider *>("aiVisualDivider");slider->setValue(20);QImage low=grab(image);
 slider->setValue(80);result["wipeReallyChangesPixels"]=low!=grab(image)&&image->property("visualWipeRatio").toDouble()==.8;
 image->mode=0;image->zoom=1.5;image->pan={15,10};image->update();result["zoomPanReallyChangesPixels"]=grab(image)!=before;
 image->zoom=1;image->pan={};const QImage unflipped=grab(image);
 image->flipY=true;image->update();result["previewYFlipChangesPixels"]=grab(image)!=unflipped;
 for(auto *button:reply.findChildren<QPushButton *>())if(button->text()=="EID 77")button->click();
 result["sourceScopedNavigation"]=destination=="rdoc://event/77"&&origin["captureSessionId"]=="capture-test";
 selector->setCurrentIndex(1);app.processEvents();
 auto *mesh=static_cast<MeshCanvas *>(reply.findChild<QWidget *>("aiVisualMeshCanvas"));
 result["realTriangles"]=mesh&&mesh->faces.size()==768;
 const QImage initial=grab(mesh);
 auto *roughness=reply.findChild<QSlider *>("aiVisualRoughness");roughness->setValue(95);
 result["roughnessChangesRender"]=grab(mesh)!=initial;
 const QImage noEnv=grab(mesh);reply.findChild<QSlider *>("aiVisualEnvironmentStrength")->setValue(180);
 result["environmentChangesRender"]=grab(mesh)!=noEnv;
 const QImage start=grab(mesh);
 QMouseEvent press(QEvent::MouseButtonPress,QPointF(100,100),Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
 QMouseEvent move(QEvent::MouseMove,QPointF(180,135),Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
 QApplication::sendEvent(mesh,&press);QApplication::sendEvent(mesh,&move);
 result["orbitChangesRender"]=mesh->property("visualMeshYaw").toDouble()!=0&&grab(mesh)!=start;
 mesh->shading=1;mesh->update();const QImage normals=grab(mesh);mesh->shading=2;mesh->update();
 result["normalAndWireframeModes"]=normals!=grab(mesh);
 mesh->shading=0;
 const QImage noEffect=mesh->renderImage(300,240,false),outlined=mesh->renderImage(300,240,true);
 int newPixels=0;bool interiorPreserved=true;const QRgb background=mesh->palette().color(QPalette::Dark).rgb();
 for(int y=0;y<240;y++)for(int x=0;x<300;x++) {
  if(noEffect.pixel(x,y)==background&&outlined.pixel(x,y)!=background)newPixels++;
  if(noEffect.pixel(x,y)!=background&&noEffect.pixel(x,y)!=outlined.pixel(x,y))interiorPreserved=false;
 }
 result["hullAddsSilhouetteWithoutPaintingOverSurface"]=newPixels>10&&interiorPreserved;
 auto *amount=reply.findChild<QDoubleSpinBox *>("aiVisualEffectAmount");amount->setValue(0);
 result["zeroHullMatchesDisabled"]=mesh->renderImage(300,240,true)==noEffect;
 amount->setValue(.12);reply.findChild<QCheckBox *>("aiVisualMeshCompare")->setChecked(true);
 result["interactiveEffectAndComparison"]=mesh->effectAmount==.12&&mesh->compare;
 QJsonObject waveCard{{"primitive","plane"},{"meshEffect",QJsonObject{{"type","sine-displacement"},{"amount",.15}}}};
 MeshCanvas wave(waveCard,nullptr);const QImage waveBefore=wave.renderImage(300,240,false),waveAfter=wave.renderImage(300,240,true);
 bool heightSurface=true;
 for(int i=0;i<wave.vertices.size();i++)heightSurface&=wave.vertices[i].y()==0&&wave.normals[i].y()>.99f;
 result["waveDisplacesPerpendicularToXZSurface"]=heightSurface&&wave.pitch!=0;
 wave.phase=1.7;
 result["vertexMotionUsesTessellatedGeometry"]=wave.vertices.size()>100&&waveBefore!=waveAfter&&waveAfter!=wave.renderImage(300,240,true);
 bool block=false;QTextDocument colors;
 colors.setHtml("<a href='rdoc://shader/test?line=7'>"+AICodeFormatting::highlightedSourceLine("float width = Shade(normal); // note","hlsl",block)+"</a>");
 auto tokenColor=[&](const QString &word){QTextCursor cursor=colors.find(word);return cursor.charFormat().foreground().color();};
 result["linkedCodeKeepsTokenColors"]=tokenColor("float")!=tokenColor("width")&&tokenColor("width")!=tokenColor("Shade")&&tokenColor("Shade")!=tokenColor("// note")&&colors.find("width").charFormat().isAnchor();
 AICodeFormatting::highlightedSourceLine("/* comment","hlsl",block);
 result["multilineCommentsRetainState"]=block&&AICodeFormatting::highlightedSourceLine("still comment */ float value;","hlsl",block).contains("source-comment")&&!block;
 if(argc>3)reply.grab().save(QString::fromLocal8Bit(argv[3]));
 selector->setCurrentIndex(2);app.processEvents();
 diagram->resize(600,250);
 QMouseEvent node(QEvent::MouseButtonPress,QPointF(diagram->width()*.75,diagram->height()*.5),Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
 QApplication::sendEvent(diagram,&node);QMouseEvent release(QEvent::MouseButtonRelease,node.localPos(),Qt::LeftButton,Qt::NoButton,Qt::NoModifier);QApplication::sendEvent(diagram,&release);result["diagramNavigates"]=destination=="rdoc://event/88";
 QTimer::singleShot(0,[&](){auto *menu=qobject_cast<QMenu *>(QApplication::activePopupWidget());if(menu){for(QAction *action:menu->actions())if(action->text()=="Shader calculation")action->trigger();menu->close();}});
 QContextMenuEvent context(QContextMenuEvent::Mouse,node.localPos().toPoint(),diagram->mapToGlobal(node.localPos().toPoint()));
 QApplication::sendEvent(diagram,&context);result["diagramNodeHasSourceNavigation"]=destination=="rdoc://shader/test?line=7";
 QFile manifest(AIVisualReply::storageRoot()+"/"+QString::fromLocal8Bit(argv[1])+".json");manifest.open(QIODevice::ReadOnly);
 const auto object=QJsonDocument::fromJson(manifest.readAll()).object();
 QJsonObject legacy=object;auto legacyCards=legacy["cards"].toArray();QJsonObject legacyMesh=legacyCards[1].toObject();legacyMesh.remove("meshEffect");legacyCards[1]=legacyMesh;legacy["cards"]=legacyCards;
 const QByteArray legacyBytes=QJsonDocument(legacy).toJson(QJsonDocument::Compact);const QString legacyId=QString::fromLatin1(QCryptographicHash::hash(legacyBytes,QCryptographicHash::Sha256).toHex());
 QFile legacyFile(AIVisualReply::storageRoot()+"/"+legacyId+".json");legacyFile.open(QIODevice::WriteOnly);legacyFile.write(legacyBytes);legacyFile.close();
 AIVisualReply oldReply(legacyId,{},nullptr);auto *oldMesh=static_cast<MeshCanvas *>(oldReply.findChild<QWidget *>("aiVisualMeshCanvas"));
 result["legacyMeshMessageStillLoads"]=oldMesh&&oldMesh->faces.size()==768&&oldMesh->effectType=="none";
 auto asset=object["cards"].toArray()[0].toObject()["image"].toObject();
 const QString path=AIVisualReply::storageRoot()+"/blobs/"+asset["file"].toString();
 QFile corrupt(path);corrupt.open(QIODevice::WriteOnly);corrupt.write("corrupt");corrupt.close();
 result["changedBlobRejected"]=imageAsset(asset).isNull();
 result["pathTraversalRejected"]=blob(QJsonObject{{"sha256",QString(64,'a')},{"file","../outside.png"}},".png").isEmpty();
 QFile out(QString::fromLocal8Bit(argv[2]));out.open(QIODevice::WriteOnly);out.write(QJsonDocument(result).toJson());
}
''',encoding='utf-8')
    qt=ROOT/'qrenderdoc/3rdparty/qt';compiler=new_compiler(compiler='msvc')
    objects=compiler.compile([str(probe)],output_dir=str(tmp_path),
        include_dirs=[str(ROOT/'qrenderdoc'),str(qt/'include'),*[str(qt/'include'/n)for n in ('QtCore','QtGui','QtWidgets')]],
        extra_postargs=['/EHsc','/std:c++14','/MD','/DQT_NO_DEBUG','/utf-8'])
    executable=tmp_path/'visuals.exe'
    compiler.link_executable(objects,str(executable.with_suffix('')),libraries=['Qt5Widgets','Qt5Gui','Qt5Core'],
                             library_dirs=[str(qt/'x64/lib')],target_lang='c++')
    env=dict(os.environ,LOCALAPPDATA=str(tmp_path))
    env['PATH']=str(ROOT/'x64/Development')+os.pathsep+env.get('PATH','')
    env['QT_QPA_PLATFORM_PLUGIN_PATH']=str(qt/'x64/plugins/platforms')
    report=tmp_path/'results.json'
    preview=Path(os.environ.get('BREAD_VISUAL_PREVIEW',str(tmp_path/'preview.png')))
    try:
        subprocess.run([str(executable),receipt['id'],str(report),str(preview)],env=env,check=True,capture_output=True,
                   timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        pytest.fail(str(error.stderr))
    values=json.loads(report.read_text())
    assert values and all(values.values()),{k:v for k,v in values.items()if not v}
