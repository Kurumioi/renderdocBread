#include "AIVisualReply.h"
#include "AICodeFormatting.h"

#include <QBuffer>
#include <QCheckBox>
#include <QColorDialog>
#include <QComboBox>
#include <QContextMenuEvent>
#include <QCryptographicHash>
#include <QDialog>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QImageReader>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QSet>
#include <QSlider>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QTextBrowser>
#include <QTextOption>
#include <QToolButton>
#include <QUrlQuery>
#include <QVBoxLayout>
#include <QVector3D>
#include <QWheelEvent>
#include <algorithm>
#include <cmath>
#include <limits>

namespace
{
QByteArray readVerified(const QString &path, const QString &hash, qint64 maximum)
{
  QFileInfo info(path);
  const QString root = QFileInfo(AIVisualReply::storageRoot()).canonicalFilePath();
  if(root.isEmpty() || !info.isFile() || info.isSymLink() || info.size() > maximum ||
     !info.canonicalFilePath().startsWith(root + QLatin1Char('/'), Qt::CaseInsensitive))
    return {};
  QFile file(path);
  if(!file.open(QIODevice::ReadOnly))
    return {};
  const QByteArray data = file.read(maximum + 1);
  if(data.size() > maximum ||
     QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex()) != hash)
    return {};
  return data;
}

QByteArray blob(const QJsonObject &asset, const QString &suffix)
{
  const QString hash = asset.value(QLatin1String("sha256")).toString();
  if(!AIVisualReply::validIdentity(hash) ||
     asset.value(QLatin1String("file")).toString() != hash + suffix)
    return {};
  return readVerified(AIVisualReply::storageRoot() + QLatin1String("/blobs/") + hash + suffix, hash,
                      8 * 1024 * 1024);
}

QImage imageAsset(const QJsonObject &asset)
{
  const QByteArray bytes = blob(asset, QLatin1String(".png"));
  QImage image;
  if(bytes.isEmpty())
    return image;
  QBuffer buffer;
  buffer.setData(bytes);
  buffer.open(QIODevice::ReadOnly);
  QImageReader reader(&buffer, "PNG");
  const QSize size = reader.size();
  if(size.width() <= 0 || size.height() <= 0 || size.width() > 8192 || size.height() > 8192 ||
     qint64(size.width()) * size.height() > 16000000)
    return image;
  reader.setScaledSize(size.scaled(QSize(1600, 1600), Qt::KeepAspectRatio).boundedTo(size));
  return reader.read().convertToFormat(QImage::Format_RGB32);
}

QPointF uvPoint(const QJsonValue &value, const QRectF &area)
{
  const QJsonArray point = value.toArray();
  if(point.size() != 2)
    return area.topLeft();
  return QPointF(area.left() + qBound(0.0, point[0].toDouble(), 1.0) * area.width(),
                 area.top() + qBound(0.0, point[1].toDouble(), 1.0) * area.height());
}

void marks(QPainter &p, const QJsonArray &annotations, const QRectF &area, bool flip = false)
{
  p.save();
  p.setClipRect(area);
  p.setRenderHint(QPainter::Antialiasing);
  auto mapPoint = [&](const QJsonValue &value) {
    QPointF point = uvPoint(value, area);
    if(flip)
      point.setY(area.top() + area.bottom() - point.y());
    return point;
  };
  for(int index = 0; index < qMin(32, annotations.size()); ++index)
  {
    const QJsonObject mark = annotations[index].toObject();
    const QJsonArray points = mark.value(QLatin1String("points")).toArray();
    if(points.isEmpty() || points.size() > 128)
      continue;
    QColor color(mark.value(QLatin1String("color")).toString());
    if(!color.isValid())
      color = Qt::red;
    const QString kind = mark.value(QLatin1String("type")).toString();
    const QPointF a = mapPoint(points.first()), b = mapPoint(points.last());
    QPainterPath path;
    if(kind == QLatin1String("ellipse"))
      path.addEllipse(QRectF(a, b).normalized());
    else if(kind == QLatin1String("rect"))
      path.addRect(QRectF(a, b).normalized());
    else
    {
      path.moveTo(a);
      for(const QJsonValue &point : points)
        path.lineTo(mapPoint(point));
      if(kind == QLatin1String("arrow"))
      {
        const double angle = std::atan2(b.y() - a.y(), b.x() - a.x());
        for(double side : {-0.55, 0.55})
        {
          path.moveTo(b);
          path.lineTo(b - QPointF(std::cos(angle + side), std::sin(angle + side)) * 12);
        }
      }
    }
    p.setBrush(Qt::NoBrush);
    p.setPen(QPen(Qt::black, 5));
    p.drawPath(path);
    p.setPen(QPen(color, 3));
    p.drawPath(path);
    const QString text = mark.value(QLatin1String("text")).toString().left(160);
    if(!text.isEmpty())
    {
      QRectF label =
          p.boundingRect(QRectF(a + QPointF(4, 4), QSizeF(qMin(240.0, area.width() - 8), 60)),
                         Qt::TextWordWrap, text)
              .adjusted(-3, -2, 3, 2);
      label.moveLeft(
          qBound(area.left(), label.left(), qMax(area.left(), area.right() - label.width())));
      label.moveTop(qBound(area.top(), label.top(), qMax(area.top(), area.bottom() - label.height())));
      p.fillRect(label, QColor(0, 0, 0, 190));
      p.setPen(color);
      p.drawText(label.adjusted(3, 2, -3, -2), Qt::TextWordWrap, text);
    }
  }
  p.restore();
}

class VisualCanvas : public QWidget
{
public:
  QJsonObject card;
  QImage first, second;
  QVector<QImage> nodeImages;
  int mode = 2;
  double ratio = .5, zoom = 1;
  QRectF focus;
  QPointF pan, last;
  bool dragging = false;
  bool flipY = false;
  std::function<void(int)> ratioChanged;
  std::function<void(uint32_t)> nodeClicked;
  std::function<void(const QUrl &)> linkClicked;
  VisualCanvas(const QJsonObject &description, QWidget *parent) : QWidget(parent), card(description)
  {
    // i18n-ignore: stable native canvas identifier.
    setObjectName(QLatin1String("aiVisualImageCanvas"));
    setMinimumSize(100, 160);
    setMouseTracking(true);
    first = imageAsset(card.value(QLatin1String("image")).toObject());
    second = imageAsset(card.value(QLatin1String("secondImage")).toObject());
    // Old reference manifests recorded an explicit orientation without applying it.
    auto orientLegacyReference = [](QImage &img, const QJsonObject &asset) {
      if(!asset.contains(QLatin1String("presentationFlipApplied")) &&
         asset.value(QLatin1String("provenance")).toString() == QLatin1String("reference") &&
         asset.value(QLatin1String("source")).toObject().value(QLatin1String("flipY")).toBool())
        img = img.mirrored(false, true);
    };
    orientLegacyReference(first, card.value(QLatin1String("image")).toObject());
    orientLegacyReference(second, card.value(QLatin1String("secondImage")).toObject());
    const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
    for(int i = 0; i < qMin(12, nodes.size()); ++i)
      nodeImages.push_back(imageAsset(nodes[i].toObject().value(QLatin1String("image")).toObject()));
    setProperty("visualWipeRatio", ratio);
  }
  QRectF area(const QRectF &bounds, const QImage &image) const
  {
    if(focus.isValid())
    {
      const double scale = qMin(bounds.width() / (image.width() * focus.width()),
                                bounds.height() / (image.height() * focus.height())) *
                           zoom;
      const QSizeF size(image.width() * scale, image.height() * scale);
      const QPointF center(focus.center().x(), flipY ? 1.0 - focus.center().y() : focus.center().y());
      return QRectF(
          bounds.center() - QPointF(center.x() * size.width(), center.y() * size.height()) + pan,
          size);
    }
    QSizeF size = QSizeF(image.size()).scaled(bounds.size(), Qt::KeepAspectRatio) * zoom;
    return QRectF(bounds.center() - QPointF(size.width() / 2, size.height() / 2) + pan, size);
  }
  void draw(QPainter &p, const QImage &image, const QJsonArray &ink, const QRectF &bounds)
  {
    if(image.isNull())
      return;
    const QRectF fitted = area(bounds, image);
    p.save();
    p.setClipRect(bounds);
    if(flipY)
    {
      p.save();
      p.translate(0, fitted.top() + fitted.bottom());
      p.scale(1, -1);
      p.drawImage(fitted, image);
      p.restore();
    }
    else
      p.drawImage(fitted, image);
    marks(p, ink, fitted, flipY);
    p.restore();
  }
  QVector<QRectF> diagramBoxes() const
  {
    const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
    if(nodes.isEmpty() || nodes.size() > 12)
      return {};
    QHash<QString, int> ids;
    for(int i = 0; i < nodes.size(); ++i)
      ids.insert(nodes[i].toObject().value(QLatin1String("id")).toString(), i);
    QVector<QPair<int, int>> links;
    QVector<int> incoming(nodes.size()), column(nodes.size());
    for(const QJsonValue &value : card.value(QLatin1String("edges")).toArray())
    {
      const QJsonArray edge = value.toArray();
      if(edge.size() != 2)
        continue;
      const int from = ids.value(edge[0].toString(), -1), to = ids.value(edge[1].toString(), -1);
      if(from < 0 || to < 0)
        continue;
      links.push_back(qMakePair(from, to));
      ++incoming[to];
    }
    QVector<int> pending;
    for(int i = 0; i < incoming.size(); ++i)
      if(incoming[i] == 0)
        pending.push_back(i);
    for(int i = 0; i < pending.size(); ++i)
    {
      for(const auto &edge : links)
      {
        if(edge.first != pending[i])
          continue;
        column[edge.second] = qMax(column[edge.second], column[edge.first] + 1);
        if(--incoming[edge.second] == 0)
          pending.push_back(edge.second);
      }
    }
    if(pending.size() != nodes.size() || links.isEmpty())
      for(int i = 0; i < column.size(); ++i)
        column[i] = i % qMin(3, column.size());
    const int columns = *std::max_element(column.begin(), column.end()) + 1;
    const double columnWidth = qMax(142.0, width() / double(columns));
    QVector<int> counts(columns), row(columns);
    for(int x : column)
      ++counts[x];
    QVector<QRectF> boxes;
    for(int x : column)
    {
      const double rowHeight = qMax(118.0, height() / double(counts[x]));
      boxes.push_back(
          QRectF(x * columnWidth + 18, row[x]++ * rowHeight + 8, columnWidth - 36, rowHeight - 16));
    }
    return boxes;
  }
  int diagramNodeAt(const QPointF &position) const
  {
    const QVector<QRectF> boxes = diagramBoxes();
    const QPointF point = (position - pan) / zoom;
    for(int i = 0; i < boxes.size(); ++i)
      if(boxes[i].contains(point))
        return i;
    return -1;
  }
protected:
  void contextMenuEvent(QContextMenuEvent *event) override
  {
    if(card.value(QLatin1String("type")).toString() != QLatin1String("diagram"))
      return;
    const int index = diagramNodeAt(event->pos());
    if(index < 0)
      return;
    const QJsonObject node = card.value(QLatin1String("nodes")).toArray()[index].toObject();
    QMenu menu(this);
    const uint32_t eid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
    if(eid)
      connect(menu.addAction(QCoreApplication::translate("AIVisualReply", "EID %1").arg(eid)),
              &QAction::triggered, this, [this, eid]() {
                if(nodeClicked)
                  nodeClicked(eid);
              });
    for(const QJsonValue &value : node.value(QLatin1String("links")).toArray())
    {
      const QJsonObject link = value.toObject();
      const QUrl url(link.value(QLatin1String("url")).toString());
      if(url.scheme() == QLatin1String("rdoc"))
        connect(menu.addAction(link.value(QLatin1String("label")).toString()), &QAction::triggered,
                this, [this, url]() {
                  if(linkClicked)
                    linkClicked(url);
                });
    }
    if(!menu.isEmpty())
      menu.exec(event->globalPos());
  }
  void paintEvent(QPaintEvent *) override
  {
    QPainter p(this);
    p.fillRect(rect(), palette().color(QPalette::Dark));
    p.setRenderHint(QPainter::SmoothPixmapTransform);
    const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
    if(card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      if(nodes.isEmpty() || nodes.size() > 12)
        return;
      p.translate(pan);
      p.scale(zoom, zoom);
      const QVector<QRectF> boxes = diagramBoxes();
      QHash<QString, int> ids;
      for(int i = 0; i < nodes.size(); ++i)
      {
        ids[nodes[i].toObject().value(QLatin1String("id")).toString()] = i;
      }
      p.setRenderHint(QPainter::Antialiasing);
      const QJsonArray edges = card.value(QLatin1String("edges")).toArray();
      for(int edgeIndex = 0; edgeIndex < qMin(24, edges.size()); edgeIndex++)
      {
        const QJsonArray e = edges[edgeIndex].toArray();
        if(e.size() != 2)
          continue;
        const int i = ids.value(e[0].toString(), -1), j = ids.value(e[1].toString(), -1);
        if(i < 0 || j < 0)
          continue;
        const QPointF a = boxes[i].center(), b = boxes[j].center(), delta = b - a;
        if(delta.manhattanLength() < 1)
          continue;
        auto edgePoint = [&](const QRectF &box, const QPointF &direction) {
          const double factor = qMin(box.width() * .5 / qMax(.001, std::abs(direction.x())),
                                     box.height() * .5 / qMax(.001, std::abs(direction.y())));
          return box.center() + direction * factor;
        };
        QPointF start = edgePoint(boxes[i], delta), end = edgePoint(boxes[j], -delta);
        QPainterPath wire;
        double angle = std::atan2(delta.y(), delta.x());
        if(boxes[i].right() < boxes[j].left())
        {
          start = QPointF(boxes[i].right(), a.y());
          end = QPointF(boxes[j].left(), b.y());
          const double bend = (start.x() + end.x()) * .5;
          wire.moveTo(start);
          wire.cubicTo(QPointF(bend, start.y()), QPointF(bend, end.y()), end);
          angle = 0;
        }
        else
        {
          wire.moveTo(start);
          wire.lineTo(end);
        }
        p.setPen(QPen(palette().color(QPalette::Highlight), 2));
        p.drawPath(wire);
        for(double side : {-.5, .5})
          p.drawLine(end, end - QPointF(std::cos(angle + side), std::sin(angle + side)) * 8);
        const QJsonArray labels = card.value(QLatin1String("edgeLabels")).toArray();
        if(edgeIndex < labels.size() && !labels[edgeIndex].toString().isEmpty())
        {
          const QPointF center = (start + end) * .5;
          QRectF label(center.x() - 11, center.y() - 25, 22, 22);
          p.fillRect(label, palette().brush(QPalette::Dark));
          p.drawText(label, Qt::AlignCenter, QString::number(edgeIndex + 1));
        }
      }
      for(int i = 0; i < nodes.size(); ++i)
      {
        p.setBrush(palette().brush(QPalette::Base));
        p.setPen(palette().color(QPalette::Mid));
        p.drawRoundedRect(boxes[i], 6, 6);
        p.setPen(palette().color(QPalette::Text));
        p.save();
        p.setClipRect(boxes[i].adjusted(4, 3, -4, -3));
        const QJsonObject node = nodes[i].toObject();
        QRectF content = boxes[i].adjusted(6, 5, -6, -5);
        const int line = p.fontMetrics().height() + 3;
        p.drawText(QRectF(content.left(), content.top(), content.width(), line), Qt::AlignCenter,
                   p.fontMetrics().elidedText(node.value(QLatin1String("text")).toString(),
                                              Qt::ElideRight, int(content.width())));
        content.setTop(content.top() + line + 2);
        if(i < nodeImages.size() && !nodeImages[i].isNull())
        {
          const double imageHeight = qBound(24.0, content.height() - 2 * line, 180.0);
          const QRectF bounds(content.left(), content.top(), content.width(), imageHeight);
          const QSizeF size = QSizeF(nodeImages[i].size()).scaled(bounds.size(), Qt::KeepAspectRatio);
          p.drawImage(QRectF(bounds.center() - QPointF(size.width() / 2, size.height() / 2), size),
                      nodeImages[i]);
          content.setTop(content.top() + imageHeight + 4);
        }
        const QStringList notes = {node.value(QLatin1String("calculation")).toString(),
                                   node.value(QLatin1String("description")).toString()};
        QTextOption noteFormat(Qt::AlignCenter);
        noteFormat.setWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
        p.drawText(content, notes.join(QLatin1Char('\n')).trimmed(), noteFormat);
        p.restore();
      }
      return;
    }
    if(first.isNull() ||
       (card.value(QLatin1String("type")).toString() == QLatin1String("compare") && second.isNull()))
    {
      p.setPen(palette().color(QPalette::Text));
      p.drawText(
          rect(), Qt::AlignCenter,
          QCoreApplication::translate("AIVisualReply", "Image data is unavailable or changed."));
      return;
    }
    const QJsonArray ink = card.value(QLatin1String("annotations")).toArray();
    const QJsonArray ink2 = card.value(QLatin1String("secondAnnotations")).toArray();
    if(second.isNull())
      draw(p, first, ink, rect());
    else if(mode == 0)
      draw(p, first, ink, rect());
    else if(mode == 1)
      draw(p, second, ink2, rect());
    else if(mode == 3)
    {
      draw(p, first, ink, QRectF(0, 0, width() / 2.0, height()));
      draw(p, second, ink2, QRectF(width() / 2.0, 0, width() / 2.0, height()));
    }
    else
    {
      draw(p, second, ink2, rect());
      p.save();
      p.setClipRect(QRectF(0, 0, width() * ratio, height()));
      draw(p, first, ink, rect());
      p.restore();
      p.setPen(QPen(Qt::white, 2));
      p.drawLine(QPointF(width() * ratio, 0), QPointF(width() * ratio, height()));
    }
  }
  void mousePressEvent(QMouseEvent *e) override
  {
    if(card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      dragging = true;
      last = e->localPos();
      setProperty("visualDiagramPress", e->localPos());
      return;
    }
    dragging = true;
    last = e->localPos();
    mouseMoveEvent(e);
  }
  void mouseMoveEvent(QMouseEvent *e) override
  {
    if(!dragging)
      return;
    if(card.value(QLatin1String("type")).toString() == QLatin1String("diagram"))
    {
      pan += e->localPos() - last;
      last = e->localPos();
      update();
      return;
    }
    if(!second.isNull() && mode == 2 && !(e->buttons() & Qt::MiddleButton))
    {
      ratio = qBound(0.0, e->localPos().x() / qMax(1, width()), 1.0);
      setProperty("visualWipeRatio", ratio);
      if(ratioChanged)
        ratioChanged(int(ratio * 100));
    }
    else
      pan += e->localPos() - last;
    last = e->localPos();
    update();
  }
  void mouseReleaseEvent(QMouseEvent *e) override
  {
    if(dragging && card.value(QLatin1String("type")).toString() == QLatin1String("diagram") &&
       (e->localPos() - property("visualDiagramPress").toPointF()).manhattanLength() < 4)
    {
      const QJsonArray nodes = card.value(QLatin1String("nodes")).toArray();
      const int i = diagramNodeAt(e->localPos());
      if(i >= 0)
      {
        const QJsonObject node = nodes[i].toObject();
        const uint32_t eid = uint32_t(node.value(QLatin1String("eventId")).toDouble());
        if(eid && nodeClicked)
          nodeClicked(eid);
        else if(linkClicked && !node.value(QLatin1String("links")).toArray().isEmpty())
          linkClicked(QUrl(node.value(QLatin1String("links"))
                               .toArray()
                               .first()
                               .toObject()
                               .value(QLatin1String("url"))
                               .toString()));
      }
    }
    dragging = false;
  }
  void wheelEvent(QWheelEvent *e) override
  {
    const bool diagram = card.value(QLatin1String("type")).toString() == QLatin1String("diagram");
    zoom = qBound(diagram ? .4 : 1.0, zoom * std::pow(1.2, e->angleDelta().y() / 120.0), 8.0);
    if(zoom == 1)
      pan = {};
    update();
    e->accept();
  }
};

struct Face
{
  int a, b, c;
};
class MeshCanvas : public QWidget
{
public:
  QVector<QVector3D> vertices;
  QVector<QVector3D> normals;
  QVector<QPointF> uvs;
  QVector<Face> faces;
  QImage texture;
  QVector3D base = QVector3D(.65f, .68f, .75f);
  QVector3D environment = QVector3D(.2f, .2f, .2f);
  double environmentStrength = .25;
  double roughness = .4, metalness = 0, yaw = 0, pitch = 0, distance = 3.2;
  int shading = 0;
  QString effectType;
  double effectAmount = .04, frequency = 6, phase = 0;
  QColor effectColor = QColor(16, 16, 24);
  bool effectEnabled = true, compare = false, wavePlane = false;
  bool dragging = false;
  QPoint last;
  MeshCanvas(const QJsonObject &card, QWidget *parent) : QWidget(parent)
  {
    // i18n-ignore: stable native renderer identifier.
    setObjectName(QLatin1String("aiVisualMeshCanvas"));
    setMinimumSize(100, 100);
    const QJsonObject effect = card.value(QLatin1String("meshEffect")).toObject();
    effectType = effect.value(QLatin1String("type")).toString(QLatin1String("none"));
    effectAmount = qBound(0.0, effect.value(QLatin1String("amount")).toDouble(.04), .25);
    frequency = qBound(.1, effect.value(QLatin1String("frequency")).toDouble(6), 20.0);
    phase = qBound(-100.0, effect.value(QLatin1String("phase")).toDouble(), 100.0);
    effectEnabled = effect.value(QLatin1String("enabled")).toBool(true);
    const QColor ink(effect.value(QLatin1String("color")).toString());
    if(ink.isValid())
      effectColor = ink;
    const QJsonObject material = card.value(QLatin1String("material")).toObject();
    const QJsonArray rgb = material.value(QLatin1String("baseColor")).toArray();
    if(rgb.size() == 3)
      base = QVector3D(float(rgb[0].toDouble()), float(rgb[1].toDouble()), float(rgb[2].toDouble()));
    const QJsonArray env = material.value(QLatin1String("environmentColor")).toArray();
    if(env.size() == 3)
      environment =
          QVector3D(float(env[0].toDouble()), float(env[1].toDouble()), float(env[2].toDouble()));
    environmentStrength =
        qBound(0.0, material.value(QLatin1String("environmentStrength")).toDouble(.25), 2.0);
    roughness = qBound(.02, material.value(QLatin1String("roughness")).toDouble(.4), 1.0);
    metalness = qBound(0.0, material.value(QLatin1String("metalness")).toDouble(), 1.0);
    texture = imageAsset(material.value(QLatin1String("texture")).toObject());
    QJsonObject mesh = QJsonDocument::fromJson(blob(card.value(QLatin1String("geometry")).toObject(),
                                                    QLatin1String(".json")))
                           .object();
    const QJsonArray v = mesh.value(QLatin1String("vertices")).toArray(),
                     f = mesh.value(QLatin1String("triangles")).toArray();
    if(v.size() > 0 && v.size() <= 16384 && f.size() <= 16384)
    {
      for(const QJsonValue &item : v)
      {
        const QJsonArray p = item.toArray();
        if(p.size() != 3)
        {
          vertices.clear();
          break;
        }
        QVector3D point(float(p[0].toDouble()), float(p[1].toDouble()), float(p[2].toDouble()));
        if(!std::isfinite(point.x()) || !std::isfinite(point.y()) || !std::isfinite(point.z()))
        {
          vertices.clear();
          break;
        }
        vertices.push_back(point);
      }
      if(!vertices.isEmpty())
        for(const QJsonValue &item : f)
        {
          const QJsonArray p = item.toArray();
          if(p.size() != 3)
            continue;
          const int a = p[0].toInt(-1), b = p[1].toInt(-1), c = p[2].toInt(-1);
          if(a >= 0 && b >= 0 && c >= 0 && a < vertices.size() && b < vertices.size() &&
             c < vertices.size())
            faces.push_back({a, b, c});
        }
      const QJsonArray tex = mesh.value(QLatin1String("uvs")).toArray();
      if(tex.size() == vertices.size())
        for(const QJsonValue &item : tex)
        {
          const QJsonArray p = item.toArray();
          if(p.size() != 2 || !std::isfinite(p[0].toDouble()) || !std::isfinite(p[1].toDouble()) ||
             std::abs(p[0].toDouble()) > 1e6 || std::abs(p[1].toDouble()) > 1e6)
          {
            uvs.clear();
            break;
          }
          uvs.push_back(QPointF(p[0].toDouble(), p[1].toDouble()));
        }
    }
    const QString primitive = card.value(QLatin1String("primitive")).toString();
    if(vertices.isEmpty() && !primitive.isEmpty())
    {
      if(primitive == QLatin1String("sphere"))
      {
        for(int y = 0; y <= 16; y++)
          for(int x = 0; x <= 24; x++)
          {
            const double a = x * 2 * 3.14159265359 / 24, b = y * 3.14159265359 / 16;
            vertices.push_back(QVector3D(float(std::sin(b) * std::cos(a)), float(std::cos(b)),
                                         float(std::sin(b) * std::sin(a))));
            uvs.push_back(QPointF(x / 24.0, y / 16.0));
          }
        for(int y = 0; y < 16; y++)
          for(int x = 0; x < 24; x++)
          {
            const int a = y * 25 + x;
            faces.push_back({a, a + 1, a + 25});
            faces.push_back({a + 1, a + 26, a + 25});
          }
      }
      else if(primitive == QLatin1String("plane"))
      {
        wavePlane = effectType == QLatin1String("sine-displacement");
        const int steps = wavePlane ? 24 : 1;
        for(int y = 0; y <= steps; y++)
          for(int x = 0; x <= steps; x++)
          {
            const float horizontal = 2 * x / float(steps) - 1, vertical = 2 * y / float(steps) - 1;
            vertices.push_back(wavePlane ? QVector3D(horizontal, 0, -vertical)
                                         : QVector3D(horizontal, vertical, 0));
            uvs.push_back(QPointF(x / double(steps), y / double(steps)));
          }
        for(int y = 0; y < steps; y++)
          for(int x = 0; x < steps; x++)
          {
            const int a = y * (steps + 1) + x;
            faces.push_back({a, a + 1, a + steps + 2});
            faces.push_back({a, a + steps + 2, a + steps + 1});
          }
        if(wavePlane)
        {
          yaw = .25;
          pitch = .65;
        }
      }
      else
      {
        vertices = {{-1, -1, -1}, {1, -1, -1}, {1, 1, -1}, {-1, 1, -1},
                    {-1, -1, 1},  {1, -1, 1},  {1, 1, 1},  {-1, 1, 1}};
        faces = {{0, 1, 2}, {0, 2, 3}, {4, 6, 5}, {4, 7, 6}, {0, 4, 5}, {0, 5, 1},
                 {2, 6, 7}, {2, 7, 3}, {0, 3, 7}, {0, 7, 4}, {1, 5, 6}, {1, 6, 2}};
        for(Face &face : faces)
          std::swap(face.b, face.c);
      }
    }
    if(!vertices.isEmpty())
    {
      QVector3D lo = vertices[0], hi = lo;
      for(const QVector3D &p : vertices)
        for(int k = 0; k < 3; k++)
        {
          lo[k] = qMin(lo[k], p[k]);
          hi[k] = qMax(hi[k], p[k]);
        }
      const QVector3D center = (lo + hi) * .5f;
      const float span = qMax((hi - lo).length(), .000001f) * .5f;
      for(QVector3D &p : vertices)
        p = (p - center) / span;
    }
    normals.resize(vertices.size());
    for(const Face &face : faces)
    {
      const QVector3D normal = QVector3D::crossProduct(vertices[face.b] - vertices[face.a],
                                                       vertices[face.c] - vertices[face.a]);
      normals[face.a] += normal;
      normals[face.b] += normal;
      normals[face.c] += normal;
    }
    for(int i = 0; i < normals.size(); i++)
      normals[i] = (primitive == QLatin1String("sphere") ? vertices[i] : normals[i]).normalized();
    setProperty("visualMeshYaw", yaw);
    setProperty("visualMeshTriangleCount", faces.size());
  }
  void resetView()
  {
    yaw = wavePlane ? .25 : 0;
    pitch = wavePlane ? .65 : 0;
    distance = 3.2;
    setProperty("visualMeshYaw", yaw);
    update();
  }
  QImage renderImage(int w, int h, bool enabled) const
  {
    if(w <= 0 || h <= 0)
      return QImage();
    QImage img(w, h, QImage::Format_RGB32);
    img.fill(palette().color(QPalette::Dark));
    QVector<float> depth(w * h, std::numeric_limits<float>::max());
    const bool hullEnabled = enabled && effectType == QLatin1String("inverted-hull");
    QVector<QVector3D> transformed, projected;
    const double cy = std::cos(yaw), sy = std::sin(yaw), cp = std::cos(pitch), sp = std::sin(pitch);
    auto project = [&](bool hull) {
      transformed.clear();
      projected.clear();
      for(int index = 0; index < vertices.size(); index++)
      {
        QVector3D p = vertices[index];
        if(enabled && effectType == QLatin1String("sine-displacement"))
          p.setY(p.y() + float(std::sin(p.x() * frequency + phase) * effectAmount));
        if(hull)
          p += normals[index] * float(effectAmount);
        const double x = cy * p.x() + sy * p.z(), z = -sy * p.x() + cy * p.z(),
                     y = cp * p.y() - sp * z;
        const double zz = distance - (sp * p.y() + cp * z);
        transformed.push_back(QVector3D(float(x), float(y), float(zz)));
        projected.push_back(QVector3D(float(w * .5 + x * h * .95 / zz),
                                      float(h * .5 - y * h * .95 / zz), float(zz)));
      }
    };
    const QVector3D light = QVector3D(-.4f, .7f, -1).normalized(),
                    halfLight = (light + QVector3D(0, 0, -1)).normalized();
    for(int pass = 0; pass < (hullEnabled ? 2 : 1); pass++)
    {
      const bool hull = pass == 1;
      project(hull);
      for(const Face &face : faces)
      {
        const QVector3D a = projected[face.a], b = projected[face.b], c = projected[face.c];
        const float denominator =
            (b.y() - c.y()) * (a.x() - c.x()) + (c.x() - b.x()) * (a.y() - c.y());
        if(std::abs(denominator) < .0001f || (hull && denominator < 0))
          continue;
        QVector3D normal = QVector3D::crossProduct(transformed[face.b] - transformed[face.a],
                                                   transformed[face.c] - transformed[face.a])
                               .normalized();
        if(normal.z() > 0)
          normal = -normal;
        if(shading == 2)
        {
          QPainter line(&img);
          line.setPen(hull ? effectColor : QColor(130, 215, 240));
          line.drawPolygon(QPolygonF({a.toPointF(), b.toPointF(), c.toPointF()}));
          continue;
        }
        const float diffuse = qMax(0.0f, QVector3D::dotProduct(normal, light));
        const float spec = float(std::pow(qMax(0.0f, QVector3D::dotProduct(normal, halfLight)),
                                          2 + 120 * (1 - roughness) * (1 - roughness)) *
                                 (.3 + .7 * metalness));
        const int x0 = qBound(0, int(std::floor(qMin(a.x(), qMin(b.x(), c.x())))), w - 1),
                  x1 = qBound(0, int(std::ceil(qMax(a.x(), qMax(b.x(), c.x())))), w - 1);
        const int y0 = qBound(0, int(std::floor(qMin(a.y(), qMin(b.y(), c.y())))), h - 1),
                  y1 = qBound(0, int(std::ceil(qMax(a.y(), qMax(b.y(), c.y())))), h - 1);
        for(int y = y0; y <= y1; y++)
        {
          QRgb *row = reinterpret_cast<QRgb *>(img.scanLine(y));
          for(int x = x0; x <= x1; x++)
          {
            const float u =
                ((b.y() - c.y()) * (x + .5f - c.x()) + (c.x() - b.x()) * (y + .5f - c.y())) /
                denominator;
            const float v = ((c.y() - a.y()) * (x + .5f - c.x()) +
                             (a.x() - c.x()) * (y + .5f - c.y())) /
                            denominator,
                        t = 1 - u - v;
            if(u < 0 || v < 0 || t < 0)
              continue;
            const float iz = u / a.z() + v / b.z() + t / c.z(), z = 1 / iz;
            if(z >= depth[y * w + x])
              continue;
            depth[y * w + x] = z;
            QVector3D color = base;
            if(!texture.isNull() && uvs.size() == vertices.size())
            {
              QPointF uv = (uvs[face.a] * (u / a.z()) + uvs[face.b] * (v / b.z()) +
                            uvs[face.c] * (t / c.z())) /
                           iz;
              const double tx = uv.x() - std::floor(uv.x()), ty = uv.y() - std::floor(uv.y());
              QRgb tex =
                  texture.pixel(qBound(0, int(tx * texture.width()), texture.width() - 1),
                                qBound(0, int((1 - ty) * texture.height()), texture.height() - 1));
              color = QVector3D(qRed(tex) / 255.f, qGreen(tex) / 255.f, qBlue(tex) / 255.f) * color;
            }
            color = shading == 1
                        ? (normal + QVector3D(1, 1, 1)) * .5f
                        : color * float(.18 + diffuse * (1 - .65 * metalness)) +
                              QVector3D(spec, spec, spec) +
                              environment * float(environmentStrength * (.15 + .85 * metalness) *
                                                  (1 - .6 * roughness));
            if(hull)
              color = QVector3D(float(effectColor.redF()), float(effectColor.greenF()),
                                float(effectColor.blueF()));
            row[x] = qRgb(qBound(0, int(color.x() * 255), 255), qBound(0, int(color.y() * 255), 255),
                          qBound(0, int(color.z() * 255), 255));
          }
        }
      }
    }
    return img;
  }
protected:
  void paintEvent(QPaintEvent *) override
  {
    const int w = qMin(width(), 900), h = qMin(height(), 600);
    if(w <= 0 || h <= 0)
      return;
    QPainter painter(this);
    if(vertices.isEmpty() || faces.isEmpty())
    {
      painter.fillRect(rect(), palette().color(QPalette::Dark));
      painter.setPen(palette().color(QPalette::Text));
      painter.drawText(
          rect(), Qt::AlignCenter,
          QCoreApplication::translate("AIVisualReply", "Geometry data is unavailable or changed."));
      return;
    }
    if(!compare)
      painter.drawImage(rect(), renderImage(w, h, effectEnabled));
    else
    {
      painter.drawImage(QRect(0, 0, width() / 2, height()), renderImage(w / 2, h, false));
      painter.drawImage(QRect(width() / 2, 0, width() - width() / 2, height()),
                        renderImage(w - w / 2, h, effectEnabled));
      painter.setPen(palette().color(QPalette::Text));
      painter.drawLine(width() / 2, 0, width() / 2, height());
      painter.drawText(QRect(6, 4, width() / 2 - 12, 24), Qt::AlignLeft,
                       QCoreApplication::translate("AIVisualReply", "Before"));
      painter.drawText(QRect(width() / 2 + 6, 4, width() / 2 - 12, 24), Qt::AlignLeft,
                       QCoreApplication::translate("AIVisualReply", "After"));
    }
  }
  void mousePressEvent(QMouseEvent *e) override
  {
    dragging = true;
    last = e->pos();
  }
  void mouseMoveEvent(QMouseEvent *e) override
  {
    if(!dragging)
      return;
    const QPoint delta = e->pos() - last;
    last = e->pos();
    yaw += delta.x() * .012;
    pitch = qBound(-1.5, pitch + delta.y() * .012, 1.5);
    setProperty("visualMeshYaw", yaw);
    update();
  }
  void mouseReleaseEvent(QMouseEvent *) override { dragging = false; }
  void wheelEvent(QWheelEvent *e) override
  {
    distance = qBound(1.6, distance * std::pow(.85, e->angleDelta().y() / 120.0), 12.0);
    update();
    e->accept();
  }
};
}

bool AIVisualReply::validIdentity(const QString &id)
{
  static const QRegularExpression expression(QLatin1String("^[0-9a-f]{64}$"));
  return expression.match(id).hasMatch();
}

QString AIVisualReply::storageRoot()
{
  QString root = QString::fromLocal8Bit(qgetenv("LOCALAPPDATA"));
  if(root.isEmpty())
    root = QString::fromLocal8Bit(qgetenv("XDG_DATA_HOME"));
  if(root.isEmpty())
    root = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
  return QDir::cleanPath(root + QLatin1String("/ReplayLab/ai-visuals"));
}

QJsonObject AIVisualReply::navigationTarget(const QString &identity)
{
  if(!validIdentity(identity))
    return {};
  const QJsonObject result =
      QJsonDocument::fromJson(readVerified(storageRoot() + QLatin1String("/targets/") + identity +
                                               QLatin1String(".json"),
                                           identity, 256 * 1024))
          .object();
  return result.value(QLatin1String("schema")).toString() == QLatin1String("bread-ai-navigation/v1")
             ? result
             : QJsonObject();
}

int AIVisualReply::inlineHeight(const QString &identity)
{
  if(!validIdentity(identity))
    return 300;
  const QJsonObject manifest =
      QJsonDocument::fromJson(
          readVerified(storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"),
                       identity, 256 * 1024))
          .object();
  int height = 300;
  for(const QJsonValue &value : manifest.value(QLatin1String("cards")).toArray())
  {
    const QString kind = value.toObject().value(QLatin1String("type")).toString();
    height = qMax(height, kind == QLatin1String("mesh")   ? 500
                          : kind == QLatin1String("code") ? 320
                                                          : 400);
  }
  return manifest.value(QLatin1String("cards")).toArray().size() == 1 &&
                 manifest.value(QLatin1String("cards"))
                         .toArray()[0]
                         .toObject()
                         .value(QLatin1String("type"))
                         .toString() == QLatin1String("diagram")
             ? 320
             : height;
}

AIVisualReply::AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent)
    : QWidget(parent)
{
  // i18n-ignore: stable object identifier for native UI and regression checks.
  setObjectName(QLatin1String("aiVisualReply"));
  setProperty("visualIdentity", identity);
  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(5, 3, 5, 3);
  layout->setSpacing(3);
  const QByteArray manifestBytes =
      validIdentity(identity)
          ? readVerified(storageRoot() + QLatin1Char('/') + identity + QLatin1String(".json"),
                         identity, 256 * 1024)
          : QByteArray();
  const QJsonObject manifest = QJsonDocument::fromJson(manifestBytes).object();
  const QJsonArray cards = manifest.value(QLatin1String("cards")).toArray();
  if(manifest.value(QLatin1String("schema")).toString() != QLatin1String("bread-ai-visual/v1") ||
     cards.isEmpty() || cards.size() > 6)
  {
    layout->addWidget(new QLabel(
        tr("Visual explanation unavailable. Its saved data is missing or changed."), this));
    return;
  }
  const QJsonObject capture = manifest.value(QLatin1String("capture")).toObject();
  QHBoxLayout *header = new QHBoxLayout();
  QComboBox *selector = new QComboBox(this);
  selector->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
  selector->setMinimumWidth(80);
  for(const QJsonValue &value : cards)
    selector->addItem(value.toObject().value(QLatin1String("title")).toString().left(160));
  header->addWidget(selector, 1);
  QPushButton *expand = new QPushButton(tr("Expand"), this);
  header->addWidget(expand);
  layout->addLayout(header);
  connect(expand, &QPushButton::clicked, this, [this, identity, navigate, selector]() {
    QDialog *dialog = new QDialog(window());
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(tr("Visual explanation"));
    dialog->resize(1000, 760);
    QVBoxLayout *body = new QVBoxLayout(dialog);
    AIVisualReply *reply = new AIVisualReply(identity, navigate, dialog);
    if(QComboBox *combo = reply->findChild<QComboBox *>())
      combo->setCurrentIndex(selector->currentIndex());
    body->addWidget(reply);
    dialog->show();
  });
  QStackedWidget *stack = new QStackedWidget(this);
  layout->addWidget(stack, 1);
  for(const QJsonValue &value : cards)
  {
    const QJsonObject card = value.toObject();
    QWidget *page = new QWidget(stack);
    QVBoxLayout *body = new QVBoxLayout(page);
    body->setContentsMargins(0, 0, 0, 0);
    body->setSpacing(3);
    const QString kind = card.value(QLatin1String("type")).toString();
    if(kind != QLatin1String("image") && kind != QLatin1String("compare") &&
       kind != QLatin1String("mesh") && kind != QLatin1String("diagram") &&
       kind != QLatin1String("code"))
    {
      body->addWidget(new QLabel(tr("This visual card is invalid."), page));
      stack->addWidget(page);
      continue;
    }
    QLabel *caption = new QLabel(card.value(QLatin1String("caption")).toString().left(1600), page);
    caption->setTextFormat(Qt::PlainText);
    caption->setWordWrap(true);
    caption->setMaximumHeight(44);
    caption->setToolTip(caption->text());
    if(!caption->text().isEmpty())
      body->addWidget(caption);
    if(kind == QLatin1String("code"))
    {
      const QJsonObject code = card.value(QLatin1String("code")).toObject();
      const bool example = code.value(QLatin1String("example")).toBool();
      QLabel *hint = new QLabel(
          example ? tr("Example code. Click to locate the corresponding Shader calculation.")
                  : tr("Saved %1 source; click a line to open Shader Workspace")
                        .arg(code.value(QLatin1String("language")).toString()),
          page);
      hint->setWordWrap(true);
      body->addWidget(hint);
      QTextBrowser *source = new QTextBrowser(page);
      source->setObjectName(QLatin1String("aiVisualCode"));
      source->setOpenLinks(false);
      source->setOpenExternalLinks(false);
      QString html = QLatin1String(
          "<table width=\"100%\" cellpadding=\"3\" style=\"font-family:Consolas,monospace;\">");
      const QStringList lines =
          code.value(QLatin1String("text")).toString().left(16000).split(QLatin1Char('\n'));
      bool blockComment = false;
      for(int i = 0; i < qMin(120, lines.size()); ++i)
      {
        QUrl url(code.value(QLatin1String("url")).toString());
        QUrlQuery query(url);
        const int startLine = code.value(QLatin1String("startLine")).toInt(1);
        const QString number = QString::number(example ? i + 1 : startLine + i);
        query.removeAllQueryItems(QLatin1String("line"));
        query.addQueryItem(QLatin1String("line"),
                           QString::number(example ? startLine : startLine + i));
        url.setQuery(query);
        const QString href = url.toString(QUrl::FullyEncoded).toHtmlEscaped();
        html += QLatin1String("<tr><td width=\"40\"><a href=\"") + href + QLatin1String("\">") +
                number +
                QLatin1String(
                    "</a></td><td style=\"white-space:pre;\"><a style=\"text-decoration:none;\" "
                    "href=\"") +
                href + QLatin1String("\">") +
                AICodeFormatting::highlightedSourceLine(
                    lines[i], code.value(QLatin1String("language")).toString(), blockComment,
                    source->palette()) +
                QLatin1String("</a></td></tr>");
      }
      source->setHtml(html + QLatin1String("</table>"));
      body->addWidget(source, 1);
      connect(source, &QTextBrowser::anchorClicked, page, [navigate, capture](const QUrl &url) {
        if(navigate)
          navigate(url, capture);
      });
    }
    else if(kind == QLatin1String("mesh"))
    {
      const bool effectDemo = card.value(QLatin1String("meshEffect"))
                                  .toObject()
                                  .value(QLatin1String("type"))
                                  .toString(QLatin1String("none")) != QLatin1String("none");
      QLabel *badge = new QLabel(effectDemo ? tr("Algorithm demonstration; replay unchanged")
                                            : tr("Material illustration; replay unchanged"),
                                 page);
      badge->setWordWrap(true);
      body->addWidget(badge);
      const QJsonObject info = card.value(QLatin1String("geometryInfo")).toObject();
      if(!info.isEmpty())
      {
        QLabel *scope = new QLabel(
            info.value(QLatin1String("space")).toString() == QLatin1String("approximate-view")
                ? tr("Captured geometry; approximate projection")
                : tr("Exported geometry"),
            page);
        if(!info.value(QLatin1String("complete")).toBool())
          scope->setText(tr("Partial captured geometry"));
        body->addWidget(scope);
      }
      MeshCanvas *mesh = new MeshCanvas(card, page);
      body->addWidget(mesh, 1);
      QWidget *materialPanel = new QWidget(page);
      QVBoxLayout *materialLayout = new QVBoxLayout(materialPanel);
      materialLayout->setContentsMargins(0, 0, 0, 0);
      materialLayout->setSpacing(3);
      body->addWidget(materialPanel);
      materialPanel->setVisible(!effectDemo);
      QHBoxLayout *controls = new QHBoxLayout();
      QComboBox *mode = new QComboBox(page);
      mode->addItems({tr("Material"), tr("Normals"), tr("Wireframe")});
      controls->addWidget(mode);
      connect(mode, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), mesh,
              [mesh](int n) {
                mesh->shading = n;
                mesh->update();
              });
      QLabel *name = new QLabel(tr("Roughness"), page);
      controls->addWidget(name);
      QSlider *roughness = new QSlider(Qt::Horizontal, page);
      roughness->setRange(2, 100);
      roughness->setValue(int(mesh->roughness * 100));
      controls->addWidget(roughness, 1);
      // i18n-ignore: stable object identifier for native UI checks.
      roughness->setObjectName(QLatin1String("aiVisualRoughness"));
      connect(roughness, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->roughness = n / 100.0;
        mesh->setProperty("visualRoughness", mesh->roughness);
        mesh->update();
      });
      materialLayout->addLayout(controls);
      QHBoxLayout *materialControls = new QHBoxLayout();
      materialControls->addWidget(new QLabel(tr("Metalness"), page));
      QSlider *metalness = new QSlider(Qt::Horizontal, page);
      metalness->setRange(0, 100);
      metalness->setValue(int(mesh->metalness * 100));
      materialControls->addWidget(metalness, 1);
      connect(metalness, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->metalness = n / 100.0;
        mesh->update();
      });
      QPushButton *color = new QPushButton(tr("Base color"), page);
      materialControls->addWidget(color);
      connect(color, &QPushButton::clicked, mesh, [mesh, page]() {
        const QColor selected = QColorDialog::getColor(
            QColor::fromRgbF(mesh->base.x(), mesh->base.y(), mesh->base.z()), page,
            QCoreApplication::translate("AIVisualReply", "Illustrative base color"));
        if(selected.isValid())
        {
          mesh->base =
              QVector3D(float(selected.redF()), float(selected.greenF()), float(selected.blueF()));
          mesh->update();
        }
      });
      QPushButton *reset = new QPushButton(tr("Reset view"), page);
      materialControls->addWidget(reset);
      connect(reset, &QPushButton::clicked, mesh, [mesh]() { mesh->resetView(); });
      materialLayout->addLayout(materialControls);
      QHBoxLayout *environmentControls = new QHBoxLayout();
      environmentControls->addWidget(new QLabel(tr("Environment strength"), page));
      QSlider *envStrength = new QSlider(Qt::Horizontal, page);
      envStrength->setRange(0, 200);
      envStrength->setValue(int(mesh->environmentStrength * 100));
      environmentControls->addWidget(envStrength, 1);
      // i18n-ignore: stable object identifier for native UI checks.
      envStrength->setObjectName(QLatin1String("aiVisualEnvironmentStrength"));
      connect(envStrength, &QSlider::valueChanged, mesh, [mesh](int n) {
        mesh->environmentStrength = n / 100.0;
        mesh->update();
      });
      materialLayout->addLayout(environmentControls);
      if(effectDemo)
      {
        QHBoxLayout *effectControls = new QHBoxLayout();
        QCheckBox *enabled = new QCheckBox(mesh->effectType == QLatin1String("inverted-hull")
                                               ? tr("Inverted hull")
                                               : tr("Vertex displacement"),
                                           page);
        // i18n-ignore: stable control identifiers for bounded preview parameters.
        enabled->setObjectName(QLatin1String("aiVisualEffectEnabled"));
        enabled->setChecked(mesh->effectEnabled);
        effectControls->addWidget(enabled);
        connect(enabled, &QCheckBox::toggled, mesh, [mesh](bool on) {
          mesh->effectEnabled = on;
          mesh->setProperty("visualMeshEffectEnabled", on);
          mesh->update();
        });
        effectControls->addWidget(new QLabel(mesh->effectType == QLatin1String("inverted-hull")
                                                 ? tr("Extrusion width")
                                                 : tr("Displacement amount"),
                                             page));
        QDoubleSpinBox *amount = new QDoubleSpinBox(page);
        amount->setRange(0, .25);
        amount->setDecimals(3);
        amount->setSingleStep(.005);
        amount->setValue(mesh->effectAmount);
        amount->setObjectName(QLatin1String("aiVisualEffectAmount"));
        amount->setToolTip(tr("Preview units; this control does not edit the capture."));
        effectControls->addWidget(amount);
        connect(amount, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh, [mesh](double n) {
          mesh->effectAmount = n;
          mesh->setProperty("visualMeshEffectAmount", n);
          mesh->update();
        });
        QCheckBox *comparison = new QCheckBox(tr("Side by side"), page);
        comparison->setObjectName(QLatin1String("aiVisualMeshCompare"));
        effectControls->addWidget(comparison);
        connect(comparison, &QCheckBox::toggled, mesh, [mesh](bool on) {
          mesh->compare = on;
          mesh->setProperty("visualMeshCompare", on);
          mesh->update();
        });
        QToolButton *details = new QToolButton(page);
        details->setText(tr("Preview settings"));
        details->setCheckable(true);
        effectControls->addWidget(details);
        connect(details, &QToolButton::toggled, materialPanel, &QWidget::setVisible);
        body->addLayout(effectControls);
        if(mesh->effectType == QLatin1String("inverted-hull"))
        {
          QPushButton *outlineColor = new QPushButton(tr("Outline color"), page);
          connect(outlineColor, &QPushButton::clicked, mesh, [mesh, page]() {
            const QColor selected = QColorDialog::getColor(
                mesh->effectColor, page,
                QCoreApplication::translate("AIVisualReply", "Outline color"));
            if(selected.isValid())
            {
              mesh->effectColor = selected;
              mesh->update();
            }
          });
          materialLayout->addWidget(outlineColor);
        }
        if(mesh->effectType == QLatin1String("sine-displacement"))
        {
          QHBoxLayout *wave = new QHBoxLayout();
          wave->addWidget(new QLabel(tr("Frequency"), page));
          QDoubleSpinBox *frequency = new QDoubleSpinBox(page);
          frequency->setRange(.1, 20);
          frequency->setSingleStep(.1);
          frequency->setValue(mesh->frequency);
          wave->addWidget(frequency);
          connect(frequency, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh,
                  [mesh](double n) {
                    mesh->frequency = n;
                    mesh->update();
                  });
          wave->addWidget(new QLabel(tr("Phase"), page));
          QDoubleSpinBox *phase = new QDoubleSpinBox(page);
          phase->setRange(-100, 100);
          phase->setSingleStep(.1);
          phase->setValue(mesh->phase);
          phase->setObjectName(QLatin1String("aiVisualEffectPhase"));
          wave->addWidget(phase);
          connect(phase, QOverload<double>::of(&QDoubleSpinBox::valueChanged), mesh, [mesh](double n) {
            mesh->phase = n;
            mesh->update();
          });
          materialLayout->addLayout(wave);
        }
        QLabel *scope = new QLabel(tr("Preview normals and units are illustrative; use the linked "
                                      "Shader/CBuffer for capture controls."),
                                   page);
        scope->setWordWrap(true);
        materialLayout->addWidget(scope);
        // i18n-ignore: these are the exact bounded preview calculations, not UI prose or captured source.
        const QString formula =
            mesh->effectType == QLatin1String("inverted-hull")
                ? QLatin1String("position += normalize(normal) * width; // Cull Front")
                : QLatin1String("position.y += sin(position.x * frequency + phase) * amount;");
        bool comment = false;
        QLabel *calculation = new QLabel(page);
        calculation->setTextFormat(Qt::RichText);
        calculation->setWordWrap(true);
        calculation->setText(QLatin1String("<code>") +
                             AICodeFormatting::highlightedSourceLine(formula, QLatin1String("hlsl"),
                                                                     comment, page->palette()) +
                             QLatin1String("</code>"));
        materialLayout->addWidget(calculation);
      }
      mesh->setToolTip(
          tr("Drag to rotate; wheel to zoom. Material controls affect this illustration only."));
    }
    else
    {
      VisualCanvas *view = new VisualCanvas(card, page);
      body->addWidget(view, 1);
      view->nodeClicked = [navigate, capture](uint32_t eid) {
        if(eid && navigate)
          navigate(QUrl(QLatin1String("rdoc://event/") + QString::number(eid)), capture);
      };
      view->linkClicked = [navigate, capture](const QUrl &url) {
        if(navigate)
          navigate(url, capture);
      };
      view->setToolTip(tr("Wheel to zoom; drag to move the image or comparison divider."));
      if(kind == QLatin1String("image") || kind == QLatin1String("compare"))
      {
        QToolButton *flip = new QToolButton(page);
        flip->setText(tr("Flip Y"));
        flip->setCheckable(true);
        body->addWidget(flip, 0, Qt::AlignRight);
        connect(flip, &QToolButton::toggled, view, [view](bool on) {
          view->flipY = on;
          view->pan = {};
          view->update();
        });
      }
      if(kind == QLatin1String("compare"))
      {
        QHBoxLayout *controls = new QHBoxLayout();
        const QString names[] = {tr("Before"), tr("After"), tr("Wipe"), tr("Side by side")};
        for(int i = 0; i < 4; i++)
        {
          QToolButton *button = new QToolButton(page);
          button->setText(names[i]);
          controls->addWidget(button);
          if(i == 2 && view->first.size() != view->second.size())
            button->setEnabled(false);
          connect(button, &QToolButton::clicked, view, [view, i]() {
            view->mode = i;
            view->setProperty("visualCompareMode", i);
            view->update();
          });
        }
        QSlider *divider = new QSlider(Qt::Horizontal, page);
        divider->setRange(0, 100);
        divider->setValue(50);
        controls->addWidget(divider, 1);
        // i18n-ignore: stable object identifier for native UI checks.
        divider->setObjectName(QLatin1String("aiVisualDivider"));
        view->ratioChanged = [divider](int n) { divider->setValue(n); };
        connect(divider, &QSlider::valueChanged, view, [view](int n) {
          view->ratio = n / 100.0;
          view->setProperty("visualWipeRatio", view->ratio);
          view->update();
        });
        if(view->first.size() != view->second.size())
          view->mode = 3;
        body->addLayout(controls);
        const QJsonObject experiment = card.value(QLatin1String("experiment")).toObject();
        const QJsonArray roi = experiment.value(QLatin1String("roi")).toArray();
        if(experiment.value(QLatin1String("verified")).toBool() && roi.size() == 4)
        {
          const double left = qBound(0.0, roi[0].toDouble(), 1.0),
                       top = qBound(0.0, roi[1].toDouble(), 1.0);
          const double right = qBound(left, roi[2].toDouble(), 1.0),
                       bottom = qBound(top, roi[3].toDouble(), 1.0);
          const double dx = qMax(.01, (right - left) * .5), dy = qMax(.01, (bottom - top) * .5);
          const QRectF region(QPointF(qMax(0.0, left - dx), qMax(0.0, top - dy)),
                              QPointF(qMin(1.0, right + dx), qMin(1.0, bottom + dy)));
          if(right > left && bottom > top)
          {
            QToolButton *focus = new QToolButton(page);
            focus->setText(tr("Focus region"));
            focus->setCheckable(true);
            // i18n-ignore: stable object identifier for comparison focus checks.
            focus->setObjectName(QLatin1String("aiVisualFocusRegion"));
            controls->addWidget(focus);
            connect(focus, &QToolButton::toggled, view, [view, region](bool checked) {
              view->focus = checked ? region : QRectF();
              view->zoom = 1;
              view->pan = {};
              view->setProperty("visualRegionFocused", checked);
              view->update();
            });
            focus->setChecked(true);
          }
        }
        QLabel *labels = new QLabel(experiment.value(QLatin1String("verified")).toBool()
                                        ? tr("Controlled A/B/A experiment; replay restored")
                                        : tr("Stage/reference comparison; an isolated effect "
                                             "toggle requires separate evidence."),
                                    page);
        labels->setWordWrap(true);
        body->addWidget(labels);
        const QString firstHash =
            card.value(QLatin1String("image")).toObject().value(QLatin1String("sha256")).toString();
        if(!firstHash.isEmpty() && firstHash == card.value(QLatin1String("secondImage"))
                                                    .toObject()
                                                    .value(QLatin1String("sha256"))
                                                    .toString())
          body->addWidget(new QLabel(tr("The two saved previews have identical pixels."), page));
        QString before = card.value(QLatin1String("beforeLabel")).toString(),
                after = card.value(QLatin1String("afterLabel")).toString();
        if(experiment.value(QLatin1String("verified")).toBool())
        {
          if(before.isEmpty())
            before = tr("Baseline calculation");
          if(after.isEmpty())
            after = tr("Experimental calculation");
        }
        if(!before.isEmpty() || !after.isEmpty())
        {
          QLabel *description = new QLabel(tr("A: %1    B: %2").arg(before, after), page);
          description->setTextFormat(Qt::PlainText);
          description->setWordWrap(true);
          description->setMaximumHeight(44);
          description->setToolTip(description->text());
          body->addWidget(description);
        }
      }
      else if(kind == QLatin1String("image"))
      {
        QLabel *badge = new QLabel(
            card.value(QLatin1String("image")).toObject().value(QLatin1String("provenance")).toString() ==
                    QLatin1String("captured")
                ? tr("Captured RT")
                : tr("Reference image"),
            page);
        body->addWidget(badge);
      }
      else
      {
        body->addWidget(new QLabel(
            tr("Click a node to navigate; right-click for Shader and parameter links."), page));
        QStringList explanations;
        const QJsonArray labels = card.value(QLatin1String("edgeLabels")).toArray();
        for(int i = 0; i < labels.size(); i++)
          if(!labels[i].toString().isEmpty())
            // i18n-ignore: numeric diagram edge identifiers precede complete model-authored text.
            explanations.push_back(QString::number(i + 1) + QLatin1String(". ") +
                                   labels[i].toString());
        if(!explanations.isEmpty())
        {
          QTextBrowser *notes = new QTextBrowser(page);
          notes->setPlainText(explanations.join(QLatin1Char('\n')));
          // i18n-ignore: stable native identifier for the diagram's edge legend.
          notes->setObjectName(QLatin1String("aiVisualFlowNotes"));
          notes->setMaximumHeight(qMin(100, explanations.size() * 22 + 8));
          body->addWidget(notes);
        }
      }
    }
    QGridLayout *links = new QGridLayout();
    auto addLink = [this, page, links, navigate, capture](const QString &label, const QUrl &url) {
      if(url.scheme() != QLatin1String("rdoc"))
        return;
      QPushButton *button = new QPushButton(label, page);
      const int count = links->count();
      links->addWidget(button, count / 2, count % 2);
      QObject::connect(button, &QPushButton::clicked, page, [navigate, capture, url]() {
        if(navigate)
          navigate(url, capture);
      });
    };
    QSet<uint32_t> events;
    for(const QJsonObject &source :
        {card, card.value(QLatin1String("experiment")).toObject(),
         card.value(QLatin1String("image")).toObject().value(QLatin1String("source")).toObject(),
         card.value(QLatin1String("secondImage")).toObject().value(QLatin1String("source")).toObject()})
    {
      const uint32_t eid =
          uint32_t(source.value(QLatin1String("eventId"))
                       .toDouble(source.value(QLatin1String("observationEventId")).toDouble()));
      if(eid && !events.contains(eid))
      {
        events.insert(eid);
        addLink(tr("EID %1").arg(eid), QUrl(QLatin1String("rdoc://event/") + QString::number(eid)));
      }
    }
    const QJsonArray extraLinks = card.value(QLatin1String("links")).toArray();
    for(int linkIndex = 0; linkIndex < qMin(6, extraLinks.size()); ++linkIndex)
    {
      const QJsonObject link = extraLinks[linkIndex].toObject();
      addLink(link.value(QLatin1String("label")).toString().left(160),
              QUrl(link.value(QLatin1String("url")).toString()));
    }
    body->addLayout(links);
    stack->addWidget(page);
  }
  connect(selector, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), stack,
          &QStackedWidget::setCurrentIndex);
}
