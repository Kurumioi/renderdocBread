#include "MeshUVView.h"

#include <QKeyEvent>
#include <QMouseEvent>
#include <QPainter>
#include <QQuaternion>
#include <QWheelEvent>
#include <cmath>
#include <limits>

namespace
{
const QColor SelectionColor(255, 155, 52);

double cross(const QPointF &a, const QPointF &b)
{
  return a.x() * b.y() - a.y() * b.x();
}

double segmentDistance(const QPointF &p, const QPointF &a, const QPointF &b)
{
  const QPointF delta = b - a;
  const double length = QPointF::dotProduct(delta, delta);
  const double t = length > 0.0 ? qBound(0.0, QPointF::dotProduct(p - a, delta) / length, 1.0) : 0.0;
  return QLineF(p, a + t * delta).length();
}

bool barycentric(const QPointF &p, const QPointF &a, const QPointF &b, const QPointF &c,
                 double area, double &u, double &v, double &w)
{
  u = cross(b - p, c - p) / area;
  v = cross(c - p, a - p) / area;
  w = 1.0 - u - v;
  return u >= 0 && v >= 0 && w >= 0;
}
}

MeshCameraGesture meshCameraGesture(int preset, Qt::MouseButtons buttons, Qt::KeyboardModifiers mods)
{
  const bool alt = (mods & Qt::AltModifier) != 0;
  if(preset == 1)    // 3ds Max
  {
    if(buttons != Qt::MiddleButton)
      return MeshCameraGesture::None;
    if(alt && (mods & Qt::ControlModifier))
      return MeshCameraGesture::Dolly;
    return alt ? MeshCameraGesture::Orbit : MeshCameraGesture::Pan;
  }
  if(preset == 2)    // Maya
  {
    if(!alt)
      return MeshCameraGesture::None;
    if(buttons == Qt::LeftButton)
      return MeshCameraGesture::Orbit;
    if(buttons == Qt::MiddleButton)
      return MeshCameraGesture::Pan;
    if(buttons == Qt::RightButton)
      return MeshCameraGesture::Dolly;
    return MeshCameraGesture::None;
  }
  if(buttons == Qt::MiddleButton || (alt && buttons == Qt::LeftButton))
    return MeshCameraGesture::Pan;
  return buttons == Qt::LeftButton ? MeshCameraGesture::Orbit : MeshCameraGesture::None;
}

quint64 MeshUVData::key(int triangle, int corner) const
{
  if(mode == Selection::Face)
    return quint64(triangle);
  const auto &face = triangles[triangle];
  const uint32_t a = vertices[face[corner]].index;
  if(mode == Selection::Point)
    return a;
  const uint32_t b = vertices[face[(corner + 1) % 3]].index;
  return (quint64(qMin(a, b)) << 32) | qMax(a, b);
}

MeshUVView::MeshUVView(std::shared_ptr<MeshUVData> mesh, bool atlas, QWidget *parent)
    : QWidget(parent), m_Mesh(mesh), m_Atlas(atlas)
{
  setFocusPolicy(Qt::StrongFocus);
  setMinimumSize(140, 100);
}

void MeshUVView::resetView()
{
  m_Pan = {};
  m_Zoom = 1.0;
  m_Yaw = m_Pitch = 0.0;
  focusSelection();
}

void MeshUVView::focusSelection()
{
  if(m_Mesh->triangles.isEmpty())
  {
    update();
    return;
  }
  QVector3D lo, hi;
  bool haveBounds = false;
  for(int face = 0; face < m_Mesh->triangles.size(); ++face)
  {
    for(int vertex = 0; vertex < 3; ++vertex)
    {
      if(!m_Mesh->selected.isEmpty())
      {
        bool selected = m_Mesh->selected.contains(m_Mesh->key(face, vertex));
        if(m_Mesh->mode == MeshUVData::Selection::Edge)
          selected |= m_Mesh->selected.contains(m_Mesh->key(face, (vertex + 2) % 3));
        if(!selected)
          continue;
      }
      const MeshUVData::Vertex &v = m_Mesh->vertices[m_Mesh->triangles[face][vertex]];
      const QVector3D point =
          m_Atlas ? QVector3D(float(v.uv.x()), float(v.uv.y()), 0.0f) : v.position;
      if(!haveBounds)
      {
        lo = hi = point;
        haveBounds = true;
      }
      for(int axis = 0; axis < 3; ++axis)
      {
        lo[axis] = qMin(lo[axis], point[axis]);
        hi[axis] = qMax(hi[axis], point[axis]);
      }
    }
  }
  m_Pan = {};
  m_Zoom = 1.0;
  if(m_Atlas)
  {
    if(!m_Mesh->selected.isEmpty() && haveBounds)
    {
      const QVector3D center = (lo + hi) * .5f;
      m_Zoom = qBound(.05, .85 / qMax(.02, double(qMax(hi.x() - lo.x(), hi.y() - lo.y()))), 30.0);
      m_Pan = QPointF(.5 - center.x(), center.y() - .5) * (qMin(width(), height()) - 24) * m_Zoom;
    }
  }
  else if(haveBounds)
  {
    m_Center = (lo + hi) * .5f;
    const double radius = (hi - lo).length() * .5;
    if(radius > 1e-8)
      m_Radius = radius;
  }
  update();
}

QPointF MeshUVView::atlasPoint(const QPointF &uv) const
{
  const double side = qMax(1, qMin(width(), height()) - 24) * m_Zoom;
  return QRectF(rect()).center() + m_Pan + QPointF(uv.x() - .5, .5 - uv.y()) * side;
}

QPointF MeshUVView::selectionPoint(int triangle, int corner) const
{
  const auto &face = m_Mesh->triangles[triangle];
  const QPointF point = atlasPoint(m_Mesh->vertices[face[corner]].uv);
  if(m_Mesh->mode == MeshUVData::Selection::Point)
    return point;
  const QPointF next = atlasPoint(m_Mesh->vertices[face[(corner + 1) % 3]].uv);
  if(m_Mesh->mode == MeshUVData::Selection::Edge)
    return (point + next) / 2;
  return (point + next + atlasPoint(m_Mesh->vertices[face[(corner + 2) % 3]].uv)) / 3;
}

void MeshUVView::paintAtlas(QPainter &painter)
{
  const QRectF tile(atlasPoint(QPointF(0, 1)), atlasPoint(QPointF(1, 0)));
  painter.fillRect(tile, palette().brush(QPalette::Base));
  if(!m_Mesh->texture.isNull())
    painter.drawImage(tile, m_Mesh->texture.mirrored(false, !m_Mesh->flipTexture));
  painter.setPen(palette().color(QPalette::Mid));
  for(int i = 1; i < 10; ++i)
  {
    const double t = i / 10.0;
    painter.drawLine(atlasPoint(QPointF(t, 0)), atlasPoint(QPointF(t, 1)));
    painter.drawLine(atlasPoint(QPointF(0, t)), atlasPoint(QPointF(1, t)));
  }
  painter.setPen(palette().color(QPalette::Text));
  painter.drawRect(tile);
  painter.drawText(tile.bottomLeft() + QPointF(0, 12), QLatin1String("0, 0"));
  painter.drawText(tile.topRight() + QPointF(-24, -2), QLatin1String("1, 1"));
  for(int index = 0; index < m_Mesh->triangles.size(); ++index)
  {
    const auto &face = m_Mesh->triangles[index];
    QPolygonF polygon;
    for(int row : face)
      polygon << atlasPoint(m_Mesh->vertices[row].uv);
    painter.setPen(QColor(125, 187, 208, 155));
    painter.setBrush(Qt::NoBrush);
    if(m_Mesh->mode == MeshUVData::Selection::Face && m_Mesh->selected.contains(m_Mesh->key(index, 0)))
    {
      painter.setPen(SelectionColor);
      painter.setBrush(QColor(255, 155, 52, 85));
    }
    painter.drawPolygon(polygon);
    if(m_Mesh->mode == MeshUVData::Selection::Face)
      continue;
    for(int corner = 0; corner < 3; ++corner)
    {
      const bool selected = m_Mesh->selected.contains(m_Mesh->key(index, corner));
      painter.setPen(QPen(selected ? SelectionColor : QColor(125, 187, 208), selected ? 3 : 1));
      if(m_Mesh->mode == MeshUVData::Selection::Point)
        painter.drawEllipse(polygon[corner], selected ? 3 : 1.5, selected ? 3 : 1.5);
      else if(selected)
        painter.drawLine(polygon[corner], polygon[(corner + 1) % 3]);
    }
  }
  if(m_DraggingBox)
  {
    painter.setPen(QPen(SelectionColor, 1, Qt::DashLine));
    painter.setBrush(Qt::NoBrush);
    painter.drawRect(QRectF(m_Press, m_Last).normalized());
  }
}

QImage MeshUVView::rasterize(const QSize &size) const
{
  QImage image(size, QImage::Format_RGB32);
  image.fill(palette().color(QPalette::Dark));
  if(size.isEmpty())
    return image;
  QVector<float> depth(size.width() * size.height(), std::numeric_limits<float>::max());
  const QQuaternion rotation = QQuaternion::fromEulerAngles(float(m_Pitch), float(m_Yaw), 0);
  const double scale = qMin(size.width(), size.height()) * .95;
  QVector<QVector3D> view;
  QVector<QPointF> projected;
  view.reserve(m_Mesh->vertices.size());
  projected.reserve(m_Mesh->vertices.size());
  for(const MeshUVData::Vertex &vertex : m_Mesh->vertices)
  {
    QVector3D p = rotation.rotatedVector((vertex.position - m_Center) / float(m_Radius));
    p.setZ(qMax(.02f, float(3.1 / m_Zoom) - p.z()));
    view.push_back(p);
    projected.push_back(QPointF(size.width() * .5 + p.x() * scale / p.z(),
                                size.height() * .5 - p.y() * scale / p.z()) +
                        m_Pan);
  }
  const bool textured = m_Mesh->showTexture && !m_Mesh->texture.isNull();
  for(int index = 0; index < m_Mesh->triangles.size(); ++index)
  {
    const auto &face = m_Mesh->triangles[index];
    const QPointF a = projected[face[0]], b = projected[face[1]], c = projected[face[2]];
    const double area = cross(b - a, c - a);
    if(std::abs(area) < .001)
      continue;
    const int left = qBound(0, int(qMin(a.x(), qMin(b.x(), c.x()))), size.width() - 1);
    const int right = qBound(0, int(std::ceil(qMax(a.x(), qMax(b.x(), c.x())))), size.width() - 1);
    const int top = qBound(0, int(qMin(a.y(), qMin(b.y(), c.y()))), size.height() - 1);
    const int bottom = qBound(0, int(std::ceil(qMax(a.y(), qMax(b.y(), c.y())))), size.height() - 1);
    const QVector3D normal =
        QVector3D::normal(view[face[1]] - view[face[0]], view[face[2]] - view[face[0]]);
    const int shade = 75 + int(115 * std::abs(normal.z()));
    const bool selected[] = {m_Mesh->selected.contains(m_Mesh->key(index, 0)),
                             m_Mesh->selected.contains(m_Mesh->key(index, 1)),
                             m_Mesh->selected.contains(m_Mesh->key(index, 2))};
    for(int y = top; y <= bottom; ++y)
    {
      QRgb *pixels = reinterpret_cast<QRgb *>(image.scanLine(y));
      for(int x = left; x <= right; ++x)
      {
        const QPointF point(x + .5, y + .5);
        double u, v, w;
        if(!barycentric(point, a, b, c, area, u, v, w))
          continue;
        u /= view[face[0]].z();
        v /= view[face[1]].z();
        w /= view[face[2]].z();
        const float z = float(1.0 / (u + v + w));
        const int pixel = y * size.width() + x;
        if(z >= depth[pixel])
          continue;
        depth[pixel] = z;
        QRgb color = qRgb(shade, shade, shade);
        if(textured)
        {
          const QPointF uv = (m_Mesh->vertices[face[0]].uv * u + m_Mesh->vertices[face[1]].uv * v +
                              m_Mesh->vertices[face[2]].uv * w) *
                             z;
          const double tx = uv.x() - std::floor(uv.x());
          const double ty = uv.y() - std::floor(uv.y());
          int row = qBound(0, int(ty * m_Mesh->texture.height()), m_Mesh->texture.height() - 1);
          if(m_Mesh->flipTexture)
            row = m_Mesh->texture.height() - 1 - row;
          color = m_Mesh->texture.pixel(int(tx * m_Mesh->texture.width()), row);
        }
        bool highlight = selected[0];
        if(m_Mesh->mode == MeshUVData::Selection::Point)
          highlight = (selected[0] && QLineF(point, a).length() <= 4) ||
                      (selected[1] && QLineF(point, b).length() <= 4) ||
                      (selected[2] && QLineF(point, c).length() <= 4);
        if(m_Mesh->mode == MeshUVData::Selection::Edge)
          highlight = (selected[0] && segmentDistance(point, a, b) <= 2) ||
                      (selected[1] && segmentDistance(point, b, c) <= 2) ||
                      (selected[2] && segmentDistance(point, c, a) <= 2);
        pixels[x] = highlight ? SelectionColor.rgb() : color;
      }
    }
  }
  return image;
}

void MeshUVView::paintEvent(QPaintEvent *)
{
  QPainter painter(this);
  painter.fillRect(rect(), palette().brush(QPalette::Dark));
  if(m_Mesh->triangles.isEmpty())
  {
    painter.setPen(palette().color(QPalette::Text));
    painter.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap,
                     tr("No UV triangles for this stage and channel."));
    return;
  }
  if(m_Atlas)
    paintAtlas(painter);
  else
    painter.drawImage(rect(), rasterize(size().boundedTo(QSize(800, 600))));
}

void MeshUVView::selectAt(const QPointF &point, bool extend)
{
  int found = -1, element = 0;
  double distance = 8.0;
  for(int index = 0; index < m_Mesh->triangles.size(); ++index)
  {
    const auto &face = m_Mesh->triangles[index];
    const QPointF a = atlasPoint(m_Mesh->vertices[face[0]].uv);
    const QPointF b = atlasPoint(m_Mesh->vertices[face[1]].uv);
    const QPointF c = atlasPoint(m_Mesh->vertices[face[2]].uv);
    const QPointF points[] = {a, b, c};
    if(m_Mesh->mode == MeshUVData::Selection::Face)
    {
      const double area = cross(b - a, c - a);
      double u, v, w;
      if(std::abs(area) > .001 && barycentric(point, a, b, c, area, u, v, w))
        found = index;
      continue;
    }
    for(int corner = 0; corner < 3; ++corner)
    {
      const double score = m_Mesh->mode == MeshUVData::Selection::Point
                               ? QLineF(point, points[corner]).length()
                               : segmentDistance(point, points[corner], points[(corner + 1) % 3]);
      if(score < distance)
      {
        distance = score;
        found = index;
        element = corner;
      }
    }
  }
  if(!extend)
    m_Mesh->selected.clear();
  if(found >= 0)
  {
    const quint64 key = m_Mesh->key(found, element);
    if(extend && m_Mesh->selected.contains(key))
      m_Mesh->selected.remove(key);
    else
      m_Mesh->selected.insert(key);
  }
  notifySelection();
}

void MeshUVView::notifySelection()
{
  setProperty("uvSelectionCount", m_Mesh->selected.size());
  update();
  if(selectionChanged)
    selectionChanged();
}

void MeshUVView::selectAll()
{
  for(int face = 0; face < m_Mesh->triangles.size(); ++face)
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      m_Mesh->selected.insert(m_Mesh->key(face, corner));
  notifySelection();
}

void MeshUVView::selectLinked()
{
  QMultiHash<uint32_t, int> adjacent;
  QVector<int> pending;
  QSet<int> visited;
  for(int face = 0; face < m_Mesh->triangles.size(); ++face)
  {
    for(int row : m_Mesh->triangles[face])
      adjacent.insert(m_Mesh->vertices[row].index, face);
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      if(m_Mesh->selected.contains(m_Mesh->key(face, corner)) && !visited.contains(face))
      {
        visited.insert(face);
        pending.push_back(face);
      }
  }
  QSet<uint32_t> visitedVertices;
  for(int i = 0; i < pending.size(); ++i)
  {
    const int face = pending[i];
    for(int row : m_Mesh->triangles[face])
    {
      const uint32_t vertex = m_Mesh->vertices[row].index;
      if(visitedVertices.contains(vertex))
        continue;
      visitedVertices.insert(vertex);
      for(int other : adjacent.values(vertex))
        if(!visited.contains(other))
        {
          visited.insert(other);
          pending.push_back(other);
        }
    }
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      m_Mesh->selected.insert(m_Mesh->key(face, corner));
  }
  notifySelection();
}

void MeshUVView::clearSelection()
{
  m_Mesh->selected.clear();
  notifySelection();
}

void MeshUVView::setBoxSelect(bool enabled)
{
  m_BoxSelect = enabled;
  setCursor(enabled ? Qt::CrossCursor : Qt::ArrowCursor);
}

void MeshUVView::mousePressEvent(QMouseEvent *event)
{
  setFocus();
  m_Press = m_Last = event->localPos();
  if(m_Atlas)
  {
    if(event->button() == Qt::MiddleButton)
      m_Gesture = MeshCameraGesture::Pan;
    if(event->button() == Qt::LeftButton)
      m_DraggingBox = m_BoxSelect;
  }
  else
    m_Gesture =
        meshCameraGesture(cameraPreset ? cameraPreset() : 0, event->buttons(), event->modifiers());
}

void MeshUVView::mouseMoveEvent(QMouseEvent *event)
{
  const QPointF delta = event->localPos() - m_Last;
  m_Last = event->localPos();
  if(m_Gesture == MeshCameraGesture::Pan)
    m_Pan += delta;
  if(m_Gesture == MeshCameraGesture::Orbit)
  {
    m_Yaw += delta.x() * .6;
    m_Pitch = qBound(-89.0, m_Pitch + delta.y() * .6, 89.0);
  }
  if(m_Gesture == MeshCameraGesture::Dolly)
  {
    const double amount = cameraPreset && cameraPreset() == 1 ? -delta.y() : delta.x() + delta.y();
    m_Zoom = qBound(.1, m_Zoom * std::exp(amount * .01), 3.0);
  }
  update();
}

void MeshUVView::mouseReleaseEvent(QMouseEvent *event)
{
  m_Gesture = MeshCameraGesture::None;
  if(!m_Atlas || event->button() != Qt::LeftButton)
    return;
  const bool extend = (event->modifiers() & Qt::ShiftModifier) != 0;
  if(m_DraggingBox)
  {
    const QRectF box = QRectF(m_Press, event->localPos()).normalized();
    if(!extend)
      m_Mesh->selected.clear();
    for(int face = 0; face < m_Mesh->triangles.size(); ++face)
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
        if(box.contains(selectionPoint(face, corner)))
          m_Mesh->selected.insert(m_Mesh->key(face, corner));
    m_DraggingBox = false;
    notifySelection();
  }
  else
    selectAt(event->localPos(), extend);
}

void MeshUVView::wheelEvent(QWheelEvent *event)
{
  m_Zoom = qBound(m_Atlas ? .05 : .1, m_Zoom * std::pow(1.2, event->angleDelta().y() / 120.0),
                  m_Atlas ? 50.0 : 3.0);
  update();
  event->accept();
}

void MeshUVView::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_F)
    focusSelection();
  else if(m_Atlas && event->key() == Qt::Key_A)
  {
    if(event->modifiers() & Qt::AltModifier)
      clearSelection();
    else
      selectAll();
  }
  else if(m_Atlas && event->key() == Qt::Key_L)
    selectLinked();
  else if(m_Atlas && event->key() == Qt::Key_B)
    setBoxSelect(!m_BoxSelect);
  else
  {
    QWidget::keyPressEvent(event);
    return;
  }
  event->accept();
}
