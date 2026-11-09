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
    comparison_marks=[{'type':'ellipse','points':[[.2,.2],[.6,.6]],'color':'#2060ff'}]
    comparison_labels={}
    if os.environ.get('BREAD_NATIVE_COMPARE_MANIFEST'):
        manifest=Path(os.environ['BREAD_NATIVE_COMPARE_MANIFEST'])
        captured=json.loads(manifest.read_text(encoding='utf-8'))['cards'][0]
        first=manifest.parent/'blobs'/captured['image']['file']
        second=manifest.parent/'blobs'/captured['secondImage']['file']
        comparison_marks=captured.get('annotations',[])
        comparison_labels={k:captured[k]for k in ('beforeLabel','afterLabel')if k in captured}
    paired={'type':'mesh-uv','title':'Paired','image':{'path':str(first)},'secondImage':{'path':str(second)},
            'detailImage':{'path':str(second)},'secondDetailImage':{'path':str(first)}}
    if os.environ.get('BREAD_NATIVE_PAIR_EVIDENCE'):
        paired=json.loads(Path(os.environ['BREAD_NATIVE_PAIR_EVIDENCE']).read_text(encoding='utf-8'))['feature']['visualCard']
    root=tmp_path/'ReplayLab/ai-visuals'
    receipt=publish('Visual test',[
        {'type':'compare','title':'Comparison (Restored)','image':{'path':str(first)},'secondImage':{'path':str(second)},
         **comparison_labels,'annotations':comparison_marks,
         'links':[{'label':'EID 77','url':'rdoc://event/77'}]},
        {'type':'mesh','title':'Material','primitive':'sphere','material':{'environmentColor':[.1,.2,1.0]},
         'meshEffect':{'type':'inverted-hull','amount':.08,'color':'#2060ff'}},
        {'type':'diagram','title':'Stages','nodes':[{'id':'a','text':'Geometry','eventId':77},{'id':'b','text':'Color','eventId':88,'details':'Use the [incoming slopes](rdoc://shader/test?line=7) from the [water texture](rdoc://resource/271).','calculation':'pattern = XZ * weights.y\n        + XY * weights.z + ZY * weights.x\nnormal = normalize(input)',
         'links':[{'label':'Shader calculation','url':'rdoc://shader/test?line=7'}],
         'codeBlocks':[dict(title='Initialize and calculate',workspacePath='C:/workspace',expectedSourceSha256='a'*64,startLine=12,endLine=31)]}],
         'edges':[['a','b']],'edgeLabels':['Carry slopes'],'edgeDetails':['Pass the slopes into lighting.']},
        paired,
        {'type':'code','title':'Key calculation','caption':'A hidden caption must not overlap code','workspacePath':'C:/workspace','expectedSourceSha256':'a'*64,'startLine':12,'endLine':31}
    ],{'capture':{'captureSessionId':'capture-test'}},capture_image=lambda _:None,
       capture_mesh=lambda _:None,authorize_file=Path,root=root,read_code=lambda _:dict(
        text='float drop = input;\n// ****\nfloat finalColor = drop;',example=False,startLine=12,endLine=31,
        lineNumbers=[12,0,31],mappings=[dict(displayLine=3,sourceLine=12,column=7,symbol='drop',sourceSymbol='drop')],
        language='hlsl',url='rdoc://shader/test?line=12&sourceSha256='+('a'*64)))
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
 result["stageCodeHasItsOwnFollowingPage"]=selector&&selector->count()==6;
 auto *stack=reply.findChild<QStackedWidget *>();
 auto views=reply.findChildren<QWidget *>("aiVisualImageCanvas");
 VisualCanvas *image=nullptr,*diagram=nullptr;
 for(auto *widget:views) {auto *canvas=static_cast<VisualCanvas *>(widget);if(canvas->card["type"]=="compare")image=canvas;else if(canvas->card["type"]=="diagram")diagram=canvas;}
 auto grab=[&](QWidget *widget){app.processEvents();return widget->grab().toImage();};
 auto *pageSlider=reply.findChild<QSlider *>("aiVisualPageSlider");
 pageSlider->setValue(3);result["headerSliderSelectsCard"]=selector->currentIndex()==3;
 auto *paired=static_cast<VisualCanvas *>(stack->currentWidget()->findChild<QWidget *>("aiVisualImageCanvas"));
 result["pairedNativeViews"]=paired&&!paired->first.isNull()&&!paired->second.isNull();
 QJsonObject plainPair=paired->card;plainPair.remove("detailImage");plainPair.remove("secondDetailImage");
 VisualCanvas oldPairCanvas(paired->card,nullptr),plainPairCanvas(plainPair,nullptr);
 oldPairCanvas.resize(700,320);plainPairCanvas.resize(700,320);
 result["pairedViewsHaveNoThumbnailOverlays"]=grab(&oldPairCanvas)==grab(&plainPairCanvas);
 if(argc>3)grab(paired).save(QString::fromLocal8Bit(argv[3])+".paired.png");
 selector->setCurrentIndex(4);auto *codeView=stack->currentWidget()->findChild<QTextBrowser *>("aiVisualCode");
 auto finalToken=codeView->document()->find("finalColor");auto dropToken=codeView->document()->find("drop",finalToken);
 auto omitted=codeView->document()->find("****");
 result["sparseCodeRowsKeepOriginalSourceLines"]=QUrlQuery(QUrl(finalToken.charFormat().anchorHref())).queryItemValue("line")=="31"&&!omitted.charFormat().isAnchor();
 const QUrlQuery dropTarget(QUrl(dropToken.charFormat().anchorHref()));
 result["codeSymbolsNavigateToOwnVerifiedPosition"]=dropTarget.queryItemValue("line")=="12"&&dropTarget.queryItemValue("column")=="7"&&dropTarget.queryItemValue("symbol")=="drop";
 bool captionHidden=true;for(auto *label:stack->currentWidget()->findChildren<QLabel *>())if(label->text()=="A hidden caption must not overlap code")captionHidden&=label->isHidden();
 result["codeCaptionDoesNotOverlapHint"]=captionHidden;
 QJsonObject oldExample{{"text","float weight = input;"},{"example",true},{"startLine",1},
   {"url","rdoc://shader/test"},{"lineNumbers",QJsonArray{0}},
   {"mappings",QJsonArray{QJsonObject{{"displayLine",1},{"sourceLine",42},{"symbol","weight"},{"sourceSymbol","actual"},{"column",7}}}}};
 QTextDocument oldCode;oldCode.setHtml(codeHtml(oldExample,reply.palette()));
 result["legacyMappedExampleRecoversRowWithoutRewritingHistory"]=oldCode.toPlainText().contains("42")&&
   QUrlQuery(QUrl(oldCode.find("input").charFormat().anchorHref())).queryItemValue("line")=="42";
 selector->setCurrentIndex(0);result["selectorSynchronizesHeaderSlider"]=pageSlider->value()==0;
 result["operationalSuffixHidden"]=selector->itemText(0)=="Comparison";
 image->mode=0;QImage before=grab(image);image->mode=1;image->update();QImage after=grab(image);
 result["abReallyChangesPixels"]=before!=after;
 auto *abToggle=stack->currentWidget()->findChild<QToolButton *>("aiVisualToggleAB");
 const auto abText=abToggle->text();const auto abSize=abToggle->sizeHint();
 image->mode=2;abToggle->click();const bool firstSwitch=image->mode==0;
 const QImage toggledBefore=grab(image);abToggle->click();const QImage toggledAfter=grab(image);
 result["oneButtonTogglesBothImages"]=firstSwitch&&image->mode==1&&toggledBefore!=toggledAfter&&
   stack->currentWidget()->findChildren<QToolButton *>("aiVisualToggleAB").size()==1&&
   abToggle->text()==abText&&abToggle->sizeHint()==abSize;
 auto *difference=stack->currentWidget()->findChild<QToolButton *>("aiVisualDifference");
 difference->click();
 const QImage delta=image->differenceImage();
 bool matchingDelta=delta.size()==image->first.size();
 for(int y=0;y<delta.height();++y)for(int x=0;x<delta.width();++x)
   matchingDelta&=(qGray(delta.pixel(x,y))!=0)==(image->first.pixel(x,y)!=image->second.pixel(x,y));
 result["differenceModeHighlightsOnlyChangedPixels"]=image->mode==VisualCanvas::DifferenceMode&&
   difference->isChecked()&&matchingDelta&&grab(image)!=before&&grab(image)!=after;
 image->mode=2;auto *slider=reply.findChild<QSlider *>("aiVisualDivider");slider->setValue(20);QImage low=grab(image);
 slider->setValue(80);result["wipeReallyChangesPixels"]=low!=grab(image)&&image->property("visualWipeRatio").toDouble()==.8;
 // Track changed source pixels away from the divider and labels. Moving only
 // the white divider must never count as a successful A/B wipe.
 auto sourcePixels=[&](const QImage &a,const QImage &b,const QImage &left,const QImage &right){
  int changed=0;bool correct=true;
  for(int y=qMin(64,a.height()/4);y<a.height()-4;++y)
   for(int x=a.width()*35/100;x<a.width()*65/100;++x)
    if(a.pixel(x,y)!=b.pixel(x,y)){++changed;correct&=left.pixel(x,y)==b.pixel(x,y)&&right.pixel(x,y)==a.pixel(x,y);}
  return changed>0&&correct;
 };
 result["wipeDisplaysCorrectSourcePixels"]=sourcePixels(before,after,low,grab(image));
 image->flipY=true;image->mode=0;const QImage flippedA=grab(image);image->mode=1;const QImage flippedB=grab(image);
 slider->setValue(20);const QImage flippedLow=grab(image);slider->setValue(80);
 result["flippedWipeDisplaysCorrectSourcePixels"]=sourcePixels(flippedA,flippedB,flippedLow,grab(image));
 image->flipY=false;
 QRect changedRegion;
 for(int y=0;y<image->first.height();++y)for(int x=0;x<image->first.width();++x)
   if(image->first.pixel(x,y)!=image->second.pixel(x,y))changedRegion|=QRect(x,y,1,1);
 image->focus=QRectF(double(changedRegion.x())/image->first.width(),double(changedRegion.y())/image->first.height(),
   double(changedRegion.width())/image->first.width(),double(changedRegion.height())/image->first.height());
 image->zoom=1.2;image->pan={9,-5};
 image->mode=0;const QImage focusedA=grab(image);image->mode=1;const QImage focusedB=grab(image);
 slider->setValue(20);const QImage focusedLow=grab(image);slider->setValue(80);
 result["focusedPannedWipeDisplaysCorrectSourcePixels"]=sourcePixels(focusedA,focusedB,focusedLow,grab(image));
 image->focus=QRectF();image->zoom=1;image->pan={};
 QJsonObject mark{{"type","ellipse"},{"color","#ff4050"},{"points",QJsonArray{QJsonArray{.2,.2},QJsonArray{.8,.8}}}};
 QJsonObject sharedCard{{"type","compare"},{"annotations",QJsonArray{mark}}};
 VisualCanvas markedPair(sharedCard,nullptr);markedPair.resize(400,200);markedPair.mode=3;
 markedPair.first=QImage(100,100,QImage::Format_RGB32);markedPair.first.fill(Qt::gray);
 markedPair.second=markedPair.first;
 auto countMarkPixels=[&](const QImage &im,int start,int end,bool red){
  int count=0;for(int y=30;y<im.height();++y)for(int x=start;x<end;++x){const QColor c(im.pixel(x,y));
   count+=red?(c.red()>160&&c.red()>c.green()*1.5&&c.red()>c.blue()*1.5):(c.blue()>160&&c.blue()>c.red()*1.5&&c.blue()>c.green()*1.5);}
  return count;
 };
 QImage shared=grab(&markedPair);
 result["sideBySideSharesRegionMarksOnBothImages"]=countMarkPixels(shared,0,200,true)>100&&countMarkPixels(shared,200,400,true)>100;
 markedPair.card["secondAnnotations"]=markedPair.card["annotations"];markedPair.card.remove("annotations");
 shared=grab(&markedPair);result["secondOnlyRegionMarkAlsoAppearsOnBothImages"]=countMarkPixels(shared,0,200,true)>100&&countMarkPixels(shared,200,400,true)>100;
 markedPair.card["annotations"]=QJsonArray{mark};mark["color"]="#2060ff";markedPair.card["secondAnnotations"]=QJsonArray{mark};
 const QImage explicitMarks=grab(&markedPair);
 result["explicitPerImageMarksRemainDistinct"]=countMarkPixels(explicitMarks,0,200,true)>100&&countMarkPixels(explicitMarks,200,400,false)>100&&countMarkPixels(explicitMarks,200,400,true)==0;
 // The mark painter must preserve an outer clip as well as the image painter.
 markedPair.mode=2;markedPair.ratio=.5;const QImage clippedMarks=grab(&markedPair);
 result["wipeClipsPerImageMarks"]=countMarkPixels(clippedMarks,200,400,true)==0&&countMarkPixels(clippedMarks,200,400,false)>20;
 if(argc>3){slider->setValue(50);image->focus=QRectF(.39,.10,.22,.23);grab(image).save(QString::fromLocal8Bit(argv[3])+".compare.png");image->focus=QRectF();}
 image->mode=1;slider->setValue(30);result["sliderReturnsFromSingleImageToWipe"]=image->mode==2&&image->property("visualCompareMode").toInt()==2;
 image->mode=0;image->zoom=1.5;image->pan={15,10};image->update();result["zoomPanReallyChangesPixels"]=grab(image)!=before;
 image->zoom=1;image->pan={};const QImage unflipped=grab(image);
 image->flipY=true;image->update();result["previewYFlipChangesPixels"]=grab(image)!=unflipped;
 auto wheel=[&](VisualCanvas *view,const QPointF &pos,int step){
   QWheelEvent event(pos,view->mapToGlobal(pos.toPoint()),{},QPoint(0,step),step,
                     Qt::Vertical,Qt::NoButton,Qt::NoModifier);
   QApplication::sendEvent(view,&event);
 };
 auto anchored=[&](const QRectF &viewport,const QPointF &eventPos,const QPointF &anchor){
   const QRectF old=image->area(viewport,image->first);
   const QPointF uv((anchor.x()-old.left())/old.width(),(anchor.y()-old.top())/old.height());
   wheel(image,eventPos,120);
   const QRectF changed=image->area(viewport,image->first);
   const QPointF actual=changed.topLeft()+QPointF(uv.x()*changed.width(),uv.y()*changed.height());
   return changed.width()>old.width()&&(anchor-actual).manhattanLength()<.01;
 };
 const QRectF viewport(image->rect());
 image->focus=QRectF(.2,.25,.55,.4);image->pan={12,-8};image->zoom=1.4;
 const QPointF cursor(qRound(viewport.width()*.7),qRound(viewport.height()*.35));
 result["wheelAnchorsFocusedFlippedImage"]=anchored(viewport,cursor,cursor);
 image->mode=3;
 const QRectF rightPane(viewport.width()/2,0,viewport.width()/2,viewport.height());
 result["wheelAnchorsTheHoveredComparisonPane"]=anchored(rightPane,cursor,cursor);
 image->mode=0;
 result["wheelOutsideViewportUsesCenter"]=anchored(viewport,QPointF(-8,-5),viewport.center());
 image->focus={};image->pan={};image->zoom=1;image->flipY=false;
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
 QApplication::sendEvent(diagram,&node);QMouseEvent release(QEvent::MouseButtonRelease,node.localPos(),Qt::LeftButton,Qt::NoButton,Qt::NoModifier);QApplication::sendEvent(diagram,&release);
 auto *notes=stack->currentWidget()->findChild<QTextBrowser *>("aiVisualFlowNotes");
 result["diagramSelectionShowsCalculation"]=notes&&notes->toPlainText().contains("incoming slopes")&&notes->toPlainText().contains("normal = normalize")&&diagram->selectedNode==1;
 result["nodeExplanationHasInlineSourceLink"]=notes->document()->find("incoming slopes").charFormat().anchorHref()=="rdoc://shader/test?line=7";
 result["legacyTextureLinkUsesStageEvent"]=notes->document()->find("water texture").charFormat().anchorHref()=="rdoc://resource/271?eid=88"&&
   visualResourceLink(QUrl("rdoc://texture/271?eid=77"),88).toString()=="rdoc://texture/271?eid=77";
 result["stageDetailUsesOneTerminatedLinePerStatement"]=notes->toPlainText().contains(
   "pattern = XZ * weights.y + XY * weights.z + ZY * weights.x;\nnormal = normalize(input);");
 auto *stageSource=stack->currentWidget()->findChild<QToolButton *>("aiVisualStageSource");
 const bool concise=notes&&!notes->toPlainText().contains("finalColor")&&notes->toPlainText().contains("normal = normalize(input)");
 stageSource->click();auto *stageCode=stack->currentWidget()->findChild<QTextBrowser *>("aiVisualCode");
 result["nodeKeepsExpressionAndOpensDetailedCodeLater"]=concise&&selector->currentIndex()==5&&stageCode&&
   stageCode->toPlainText().contains("Initialize and calculate")&&
   QUrlQuery(QUrl(stageCode->document()->find("finalColor").charFormat().anchorHref())).queryItemValue("line")=="31"&&
   !stageCode->document()->find("****").charFormat().isAnchor();
 selector->setCurrentIndex(2);
 auto *flowPanels=stack->currentWidget()->findChild<QSplitter *>("aiVisualFlowPanels");
 result["stageDetailsCanBeResizedForLongCode"]=flowPanels&&flowPanels->count()==2&&notes->maximumHeight()>180;
 result["explanationLinksEscapeOtherMarkup"]=!explanationHtml("<img src=x> [bad](javascript:alert(1))").contains("<img")&&
   !explanationHtml("[bad](rdoc://unrecognized/x)").contains("href=");
 QTextDocument adjacentLinks;
 adjacentLinks.setHtml(explanationHtml(QString::fromUtf8(
   u8"[屏幕深度](rdoc://texture/50613?eid=5832&kind=srv)并线性化，再读取[遮挡深度图](rdoc://texture/6153?eid=5832&kind=srv)。")));
 result["adjacentChineseProseDoesNotBecomePartOfTheLink"]=
   adjacentLinks.find(QString::fromUtf8(u8"屏幕深度")).charFormat().anchorHref()=="rdoc://texture/50613?eid=5832&kind=srv"&&
   adjacentLinks.find(QString::fromUtf8(u8"遮挡深度图")).charFormat().anchorHref()=="rdoc://texture/6153?eid=5832&kind=srv"&&
   adjacentLinks.toPlainText().contains(QString::fromUtf8(u8"并线性化，再读取"));
 QMouseEvent dbl(QEvent::MouseButtonDblClick,node.localPos(),Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);QApplication::sendEvent(diagram,&dbl);
 result["diagramNavigates"]=destination=="rdoc://event/88";
 const auto mid=diagram->diagramTransform().map(diagram->diagramWire(0).pointAtPercent(.5));
 QMouseEvent edgePress(QEvent::MouseButtonPress,mid,Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
 QMouseEvent edgeRelease(QEvent::MouseButtonRelease,mid,Qt::LeftButton,Qt::NoButton,Qt::NoModifier);
 QApplication::sendEvent(diagram,&edgePress);QApplication::sendEvent(diagram,&edgeRelease);
 result["diagramConnectionHasItsOwnExplanation"]=notes->toPlainText().contains("Pass the slopes into lighting.")&&diagram->selectedEdge==0&&!notes->toPlainText().contains("1. ");
 QTimer::singleShot(0,[&](){auto *menu=qobject_cast<QMenu *>(QApplication::activePopupWidget());if(menu){for(QAction *action:menu->actions())if(action->text()=="Shader calculation")action->trigger();menu->close();}});
 QContextMenuEvent context(QContextMenuEvent::Mouse,node.localPos().toPoint(),diagram->mapToGlobal(node.localPos().toPoint()));
 QApplication::sendEvent(diagram,&context);result["diagramNodeHasSourceNavigation"]=destination=="rdoc://shader/test?line=7";
 QFile manifest(AIVisualReply::storageRoot()+"/"+QString::fromLocal8Bit(argv[1])+".json");manifest.open(QIODevice::ReadOnly);
 const auto object=QJsonDocument::fromJson(manifest.readAll()).object();
 auto revised=object;auto revisedCards=revised["cards"].toArray();auto revisedCard=revisedCards[0].toObject();
 revisedCard["links"]=QJsonArray{QJsonObject{{"label","EID 77"},{"url","rdoc://event/88"}}};
 revisedCards[0]=revisedCard;revised["cards"]=revisedCards;
 const auto revisedBytes=QJsonDocument(revised).toJson(QJsonDocument::Compact);
 const auto revisedId=QString::fromLatin1(QCryptographicHash::hash(revisedBytes,QCryptographicHash::Sha256).toHex());
 QFile revisedFile(AIVisualReply::storageRoot()+"/"+revisedId+".json");revisedFile.open(QIODevice::WriteOnly);revisedFile.write(revisedBytes);revisedFile.close();
 const QJsonArray revisions{QJsonObject{{"id",QString::fromLocal8Bit(argv[1])}},QJsonObject{{"id",revisedId}}};
 result["correctedLinksShowOnlyLatestPresentation"]=AIVisualReply::visibleIdentities(revisions)==QStringList{revisedId};
 revised["title"]="Another explanation";const auto distinctBytes=QJsonDocument(revised).toJson(QJsonDocument::Compact);
 const auto distinctId=QString::fromLatin1(QCryptographicHash::hash(distinctBytes,QCryptographicHash::Sha256).toHex());
 QFile distinctFile(AIVisualReply::storageRoot()+"/"+distinctId+".json");distinctFile.open(QIODevice::WriteOnly);distinctFile.write(distinctBytes);distinctFile.close();
 result["oneViewerUsesFinalCompleteBundle"]=AIVisualReply::visibleIdentities(QJsonArray{revisions[0],QJsonObject{{"id",distinctId}}})==QStringList{distinctId}&&QFile::exists(AIVisualReply::storageRoot()+"/"+QString::fromLocal8Bit(argv[1])+".json");
 auto interleaved=object;auto interleavedCards=interleaved["cards"].toArray();
 auto laterCompare=interleavedCards[0].toObject();laterCompare["title"]="Later comparison";
 laterCompare["experiment"]=QJsonObject{{"verified",true},{"roi",QJsonArray{.2,.2,.5,.6}}};
 interleavedCards.append(laterCompare);interleaved["cards"]=interleavedCards;
 const QByteArray interleavedBytes=QJsonDocument(interleaved).toJson(QJsonDocument::Compact);
 const QString interleavedId=QString::fromLatin1(QCryptographicHash::hash(interleavedBytes,QCryptographicHash::Sha256).toHex());
 QFile interleavedFile(AIVisualReply::storageRoot()+"/"+interleavedId+".json");interleavedFile.open(QIODevice::WriteOnly);interleavedFile.write(interleavedBytes);interleavedFile.close();
 AIVisualReply interleavedReply(interleavedId,{},nullptr);
 auto *ordered=interleavedReply.findChild<QComboBox *>();
 result["legacyComparisonsAreGroupedStably"]=ordered&&ordered->itemText(0)=="Comparison"&&
   ordered->itemText(1)=="Later comparison"&&ordered->itemText(3)=="Stages";
 interleavedReply.resize(700,500);interleavedReply.show();app.processEvents();
 auto *compareStack=interleavedReply.findChild<QStackedWidget *>();
 auto *firstPage=compareStack->widget(0),*nextPage=compareStack->widget(1);
 auto *nextView=nextPage->findChild<QWidget *>("aiVisualImageCanvas");
 firstPage->findChild<QToolButton *>("aiVisualDifference")->click();
 interleavedReply.findChild<QSlider *>("aiVisualPageSlider")->setValue(1);
 const bool keepsDifference=compareStack->currentIndex()==1&&nextView->property("visualCompareMode").toInt()==4&&
   nextPage->findChild<QToolButton *>("aiVisualDifference")->isChecked();
 QToolButton *flipButton=nullptr;
 for(auto *button:nextPage->findChildren<QToolButton *>())if(button->text()=="Flip Y")flipButton=button;
 auto *focusButton=nextPage->findChild<QToolButton *>("aiVisualFocusRegion");
 result["flipFollowsFocusOnSameRow"]=flipButton&&focusButton&&flipButton->x()>focusButton->x()&&
   qAbs(flipButton->geometry().center().y()-focusButton->geometry().center().y())<=1;
 auto *nextAB=nextPage->findChild<QToolButton *>("aiVisualToggleAB");
 nextAB->click();nextAB->click();ordered->setCurrentIndex(0);
 const bool keepsB=firstPage->findChild<QWidget *>("aiVisualImageCanvas")->property("visualCompareMode").toInt()==1&&
   firstPage->findChild<QToolButton *>("aiVisualToggleAB")->isChecked();
 firstPage->findChild<QSlider *>("aiVisualDivider")->setValue(35);ordered->setCurrentIndex(3);ordered->setCurrentIndex(1);
 result["comparisonModeSurvivesCardChanges"]=keepsDifference&&keepsB&&nextView->property("visualCompareMode").toInt()==2;
 interleavedReply.hide();
 QJsonObject fieldNode{{"id","a"},{"text","Field"},{"image",object["cards"].toArray()[0].toObject()["image"]},
   {"secondImage",object["cards"].toArray()[0].toObject()["secondImage"]},{"imageLabel","2D"},{"secondImageLabel","3D"},
   {"calculation",QString::fromUtf8("float2 shape = P.scale;\nfloat value = smoothstep(0, 1, radius\xc2\xb7weight);\nfloat result = normalize(veryLongInputIdentifierThatMustWrapAndKeepTheFinalToken);")}};
 VisualCanvas fieldDiagram(QJsonObject{{"type","diagram"},{"nodes",QJsonArray{fieldNode}}},nullptr);
 fieldDiagram.nodeImages[0]=QImage(300,200,QImage::Format_ARGB32);fieldDiagram.nodeImages[0].fill(QColor(30,60,100,17));
 fieldDiagram.nodeSecondImages[0]=QImage(300,200,QImage::Format_RGB32);fieldDiagram.nodeSecondImages[0].fill(QColor(170,90,20));
 fieldDiagram.resize(700,450);const QImage fields=grab(&fieldDiagram);
 int bluePixels=0,orangePixels=0;
 for(int y=0;y<fields.height();++y)for(int x=0;x<fields.width();++x){const QColor c(fields.pixel(x,y));
   bluePixels+=c.blue()>70&&c.blue()>c.red()*1.5;orangePixels+=c.red()>120&&c.green()>60&&c.blue()<50;}
 result["diagramShowsIndependentFieldAndMesh"]=bluePixels>1000&&orangePixels>1000;
 const QString calculation=fieldNode["calculation"].toString();
 fieldNode.remove("calculation");fieldDiagram.card["nodes"]=QJsonArray{fieldNode};
 result["diagramNodeDoesNotPaintCalculation"]=fields==grab(&fieldDiagram);
 QTextDocument expression;expression.setHtml(calculationHtml(calculation,reply.palette()));
 result["detailPseudocodeUsesSyntaxColors"]=expression.toPlainText().contains("P.scale;\nfloat value")&&
   expression.toPlainText().contains("radius * weight")&&
   expression.find("float2").charFormat().foreground()!=expression.find("smoothstep").charFormat().foreground();
 const auto boxes=fieldDiagram.diagramBoxes();const auto transform=fieldDiagram.diagramTransform();
 const auto centered=transform.mapRect(boxes[0]);
 result["diagramNodesFitAndCenter"]=qAbs(centered.center().x()-350)<1&&qAbs(centered.center().y()-225)<1&&centered.height()<440;
 const QPointF channelButton=transform.map(fieldDiagram.diagramChannelRect(fieldDiagram.diagramImageBounds(0,0,boxes[0])).center());
 QMouseEvent channelPress(QEvent::MouseButtonPress,channelButton,Qt::LeftButton,Qt::LeftButton,Qt::NoModifier);
 QMouseEvent channelRelease(QEvent::MouseButtonRelease,channelButton,Qt::LeftButton,Qt::NoButton,Qt::NoModifier);
 QApplication::sendEvent(&fieldDiagram,&channelPress);QApplication::sendEvent(&fieldDiagram,&channelRelease);
 auto *channelMenu=fieldDiagram.findChild<QMenu *>("aiDiagramChannelMenu");
 if(channelMenu) {
   auto checks=channelMenu->findChildren<QCheckBox *>();
   for(auto *check:checks) if(check->text()=="R"||check->text()=="G"||check->text()=="B"||check->text()=="A")check->click();
   const QRgb alpha=fieldDiagram.diagramChannelImage(0,0).pixel(0,0);
   for(auto *check:checks)if(check->text()=="R")check->click();
   result["nodeImageChannelsAreIndependent"]=alpha==qRgb(17,17,17)&&
     fieldDiagram.diagramChannelImage(0,0).pixel(0,0)==qRgba(30,0,0,17)&&
     fieldDiagram.diagramChannelImage(0,1).pixel(0,0)==qRgb(170,90,20);
   channelMenu->close();
 } else result["nodeImageChannelsAreIndependent"]=false;
 QJsonArray fanNodes;
 for(const QString &id:{QString("source"),QString("upper"),QString("middle"),QString("lower"),QString("result")})
   fanNodes.append(QJsonObject{{"id",id},{"text",id},{"description",id=="middle"?QString("Tall\n").repeated(18):QString("Role")}});
 const QJsonArray fanEdges{QJsonArray{"source","upper"},QJsonArray{"source","middle"},QJsonArray{"source","lower"},
   QJsonArray{"upper","result"},QJsonArray{"middle","result"},QJsonArray{"lower","result"}};
 VisualCanvas fan(QJsonObject{{"type","diagram"},{"nodes",fanNodes},{"edges",fanEdges}},nullptr);
 fan.resize(650,350);bool eachWire=true;
 for(int i=0;i<fanEdges.size();++i)eachWire&=fan.diagramEdgeAt(fan.diagramTransform().map(fan.diagramWire(i).pointAtPercent(.5)))==i;
 result["convergingWiresSelectNearestCurve"]=eachWire;
 fieldNode["text"]=QString("Long node title ").repeated(12);
 fieldNode["imageLabel"]=QString("ShaderTextureCalculation ").repeated(8);
 fieldNode["description"]=QString("A clear explanation with multiple complete lines. ").repeated(15);
 VisualCanvas wrappedDiagram(QJsonObject{{"type","diagram"},{"nodes",QJsonArray{fieldNode}}},nullptr);
 const double textHeight=wrappedDiagram.diagramTextHeight(QString("line\n").repeated(12)+"LAST",180);
 QImage textImage(180,int(std::ceil(textHeight)),QImage::Format_RGB32);textImage.fill(Qt::white);
 {QPainter textPainter(&textImage);textPainter.setFont(wrappedDiagram.font());textPainter.setPen(Qt::black);
  wrappedDiagram.diagramText(textPainter,textImage.rect(),QString("line\n").repeated(12)+"LAST");}
 int finalLineInk=0;for(int y=qMax(0,textImage.height()-wrappedDiagram.fontMetrics().height());y<textImage.height();++y)
  for(int x=0;x<textImage.width();++x)finalLineInk+=textImage.pixelColor(x,y).lightness()<100;
 result["diagramTextWrapsBeyondSixLinesWithoutClipping"]=finalLineInk>10&&wrappedDiagram.diagramBoxes()[0].height()>boxes[0].height();
 result["diagramImageLabelsWrapInsteadOfEliding"]=wrappedDiagram.diagramLabelsHeight(0,298)>wrappedDiagram.fontMetrics().height()*2;
 QJsonObject legacy=object;auto legacyCards=legacy["cards"].toArray();QJsonObject legacyMesh=legacyCards[1].toObject();legacyMesh.remove("meshEffect");legacyCards[1]=legacyMesh;legacy["cards"]=legacyCards;
 auto identical=legacyCards[0].toObject();identical["secondImage"]=identical["image"];legacyCards[0]=identical;legacy["cards"]=legacyCards;
 const QByteArray legacyBytes=QJsonDocument(legacy).toJson(QJsonDocument::Compact);const QString legacyId=QString::fromLatin1(QCryptographicHash::hash(legacyBytes,QCryptographicHash::Sha256).toHex());
 QFile legacyFile(AIVisualReply::storageRoot()+"/"+legacyId+".json");legacyFile.open(QIODevice::WriteOnly);legacyFile.write(legacyBytes);legacyFile.close();
 AIVisualReply oldReply(legacyId,{},nullptr);auto *oldMesh=static_cast<MeshCanvas *>(oldReply.findChild<QWidget *>("aiVisualMeshCanvas"));
 result["legacyMeshMessageStillLoads"]=oldMesh&&oldMesh->faces.size()==768&&oldMesh->effectType=="none";
 for(auto *widget:oldReply.findChildren<QWidget *>("aiVisualImageCanvas")) {
   auto *canvas=static_cast<VisualCanvas *>(widget);
   if(canvas->card["type"]=="compare")result["legacyIdenticalComparisonBecomesSingleImage"]=canvas->identicalPair&&canvas->second.isNull()&&canvas->mode==0;
 }
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
