#include "MeshUVView.h"

#include <QApplication>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QPainter>
#include <QWheelEvent>
#include <QtMath>
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

bool barycentric(const QPointF &p, const QPointF &a, const QPointF &b, const QPointF &c, double &u,
                 double &v, double &w)
{
  const double area = cross(b - a, c - a);
  if(std::abs(area) < 1e-9)
    return false;
  u = cross(b - p, c - p) / area;
  v = cross(c - p, a - p) / area;
  w = 1.0 - u - v;
  return u >= 0 && v >= 0 && w >= 0;
}

struct ProjectedMesh
{
  ProjectedMesh(const MeshUVGeometry &geometry, const QSize &size, const QMatrix4x4 &transform)
  {
    points.reserve(geometry.vertices.size());
    depths.reserve(geometry.vertices.size());
    for(const auto &vertex : geometry.vertices)
    {
      const QVector4D p = transform * vertex.position;
      if(p.w() <= 0 || !std::isfinite(p.w()))
      {
        points.push_back({});
        depths.push_back(std::numeric_limits<double>::infinity());
        continue;
      }
      points.push_back(QPointF((p.x() / p.w() + 1.0) * size.width() * .5,
                               (1.0 - p.y() / p.w()) * size.height() * .5));
      depths.push_back(p.z() / p.w());
    }
  }
  QVector<QPointF> points;
  QVector<double> depths;
};
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
  const auto &face = geometry->triangles[triangle];
  const uint32_t a = geometry->vertices[face[corner]].index;
  if(mode == Selection::Point)
    return a;
  const uint32_t b = geometry->vertices[face[(corner + 1) % 3]].index;
  return (quint64(qMin(a, b)) << 32) | qMax(a, b);
}

MeshUVView::MeshUVView(std::shared_ptr<MeshUVData> mesh, QWidget *parent)
    : QWidget(parent), m_Mesh(mesh)
{
  setFocusPolicy(Qt::StrongFocus);
  setMinimumSize(140, 100);
  setAttribute(Qt::WA_OpaquePaintEvent);
}

void MeshUVView::resetView()
{
  m_Pan = {};
  m_Zoom = 1.0;
  setProperty("uvZoom", m_Zoom);
  update();
}

void MeshUVView::geometryChanged()
{
  m_Wire = QPixmap();
  m_WireDensity = 0.0;
  setProperty("uvTriangleCount", m_Mesh->geometry ? m_Mesh->geometry->triangles.size() : 0);
  clearSelection();
}

void MeshUVView::textureChanged()
{
  m_Texture = QPixmap::fromImage(m_Mesh->texture.mirrored(false, !m_Mesh->flipTexture));
  setProperty("uvTextureReady", !m_Texture.isNull());
  update();
}

void MeshUVView::focusSelection()
{
  QRectF bounds = m_SelectedFaces.boundingRect();
  for(const QLineF &edge : m_SelectedEdges)
    bounds |= QRectF(edge.p1(), edge.p2()).normalized().adjusted(-.001, -.001, .001, .001);
  for(const QPointF &p : m_SelectedPoints)
    bounds |= QRectF(p - QPointF(.001, .001), QSizeF(.002, .002));
  if(bounds.isEmpty() && m_Mesh->geometry)
    bounds = m_Mesh->geometry->bounds;
  if(bounds.isEmpty())
  {
    resetView();
    return;
  }
  m_Zoom = qBound(.01, .85 / qMax(.02, qMax(bounds.width(), bounds.height())), 40.0);
  m_Pan = QPointF(.5 - bounds.center().x(), bounds.center().y() - .5);
  setProperty("uvZoom", m_Zoom);
  update();
}

QTransform MeshUVView::atlasTransform() const
{
  const double side = qMax(1, qMin(width(), height()) - 24) * m_Zoom;
  const QPointF origin = QRectF(rect()).center() + (m_Pan + QPointF(-.5, .5)) * side;
  return QTransform(side, 0, 0, -side, origin.x(), origin.y());
}

void MeshUVView::updateWireCache(double density)
{
  // Rasterize the static overlay into discrete zoom levels. Small pans and resizes just blit it.
  const QRectF bounds = m_Mesh->geometry->bounds;
  const double span = qMax(bounds.width(), bounds.height());
  const double level = qMin(2044.0, qMax(128.0, std::pow(2.0, std::ceil(std::log2(density * span)))));
  if(level == m_WireDensity)
    return;
  m_WireDensity = level;
  const double scale = level / span;
  m_WireBounds = bounds.adjusted(-2 / scale, -2 / scale, 2 / scale, 2 / scale);
  m_Wire = QPixmap(QSize(qCeil(m_WireBounds.width() * scale), qCeil(m_WireBounds.height() * scale)));
  m_Wire.fill(Qt::transparent);
  QPainter cache(&m_Wire);
  cache.setRenderHint(QPainter::Antialiasing);
  cache.scale(m_Wire.width() / m_WireBounds.width(), m_Wire.height() / m_WireBounds.height());
  cache.translate(-m_WireBounds.topLeft());
  cache.setPen(QPen(QColor(125, 187, 208, 170), 0));
  cache.drawPath(m_Mesh->geometry->wire);
}

void MeshUVView::paintEvent(QPaintEvent *)
{
  QPainter painter(this);
  painter.fillRect(rect(), palette().brush(QPalette::Dark));
  if(!m_Mesh->geometry || m_Mesh->geometry->triangles.isEmpty())
  {
    painter.setPen(palette().color(QPalette::Text));
    painter.drawText(rect(), Qt::AlignCenter | Qt::TextWordWrap,
                     tr("No UV triangles for this stage and channel."));
    return;
  }

  const QTransform transform = atlasTransform();
  const QRectF tile = transform.mapRect(QRectF(0, 0, 1, 1));
  painter.fillRect(tile, palette().brush(QPalette::Base));
  if(!m_Texture.isNull())
    painter.drawPixmap(tile, m_Texture, QRectF(m_Texture.rect()));
  painter.setPen(palette().color(QPalette::Mid));
  for(int i = 1; i < 10; ++i)
  {
    const double t = i / 10.0;
    painter.drawLine(transform.map(QPointF(t, 0)), transform.map(QPointF(t, 1)));
    painter.drawLine(transform.map(QPointF(0, t)), transform.map(QPointF(1, t)));
  }
  painter.setPen(palette().color(QPalette::Text));
  painter.drawRect(tile);
  painter.drawText(tile.bottomLeft() + QPointF(0, 12), QLatin1String("0, 0"));
  painter.drawText(tile.topRight() + QPointF(-24, -2), QLatin1String("1, 1"));

  updateWireCache(transform.m11() * devicePixelRatioF());
  painter.save();
  painter.setTransform(transform);
  painter.drawPixmap(m_WireBounds, m_Wire, QRectF(m_Wire.rect()));
  painter.setPen(QPen(SelectionColor, 0));
  painter.setBrush(QColor(255, 155, 52, 85));
  painter.drawPath(m_SelectedFaces);
  QPen pen(SelectionColor, 2);
  pen.setCosmetic(true);
  painter.setPen(pen);
  painter.drawLines(m_SelectedEdges);
  pen.setWidth(6);
  pen.setCapStyle(Qt::RoundCap);
  painter.setPen(pen);
  painter.drawPoints(m_SelectedPoints);
  painter.restore();

  if(m_DraggingBox)
  {
    painter.setPen(QPen(SelectionColor, 1, Qt::DashLine));
    painter.setBrush(Qt::NoBrush);
    painter.drawRect(QRectF(m_Press, m_Last).normalized());
  }
}

void MeshUVView::selectAt(const QPointF &point, bool extend)
{
  if(!m_Mesh->geometry)
    return;
  const auto &geometry = *m_Mesh->geometry;
  const QTransform transform = atlasTransform();
  int found = -1, element = 0;
  double distance = 8.0;
  for(int index = 0; index < geometry.triangles.size(); ++index)
  {
    const auto &face = geometry.triangles[index];
    const QPointF a = transform.map(geometry.vertices[face[0]].uv);
    const QPointF b = transform.map(geometry.vertices[face[1]].uv);
    const QPointF c = transform.map(geometry.vertices[face[2]].uv);
    const QPointF points[] = {a, b, c};
    if(m_Mesh->mode == MeshUVData::Selection::Face)
    {
      double u, v, w;
      if(barycentric(point, a, b, c, u, v, w))
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
  selectElement(found, element, extend);
}

void MeshUVView::selectMesh(const QPointF &point, const QSize &size, const QMatrix4x4 &transform,
                            bool extend)
{
  if(!m_Mesh->geometry)
    return;
  const auto &geometry = *m_Mesh->geometry;
  const ProjectedMesh mesh(geometry, size, transform);
  double depth = 1.0;
  int triangle = -1;
  for(int i = 0; i < geometry.triangles.size(); ++i)
  {
    const auto &face = geometry.triangles[i];
    const double a = mesh.depths[face[0]], b = mesh.depths[face[1]], c = mesh.depths[face[2]];
    double u, v, w;
    if(std::isfinite(a) && std::isfinite(b) && std::isfinite(c) &&
       barycentric(point, mesh.points[face[0]], mesh.points[face[1]], mesh.points[face[2]], u, v, w))
    {
      const double hit = u * a + v * b + w * c;
      if(hit >= 0.0 && hit < depth)
      {
        depth = hit;
        triangle = i;
      }
    }
  }

  int corner = 0;
  if(triangle >= 0 && m_Mesh->mode != MeshUVData::Selection::Face)
  {
    const auto &face = geometry.triangles[triangle];
    double closest = std::numeric_limits<double>::infinity();
    for(int i = 0; i < 3; ++i)
    {
      const QPointF &a = mesh.points[face[i]], &b = mesh.points[face[(i + 1) % 3]];
      const double distance = m_Mesh->mode == MeshUVData::Selection::Point
                                  ? QLineF(point, a).length()
                                  : segmentDistance(point, a, b);
      if(distance < closest)
      {
        closest = distance;
        corner = i;
      }
    }
  }
  selectElement(triangle, corner, extend);
}

void MeshUVView::selectElement(int triangle, int corner, bool extend)
{
  if(!extend)
    m_Mesh->selected.clear();
  if(triangle >= 0)
  {
    const quint64 key = m_Mesh->key(triangle, corner);
    if(extend && m_Mesh->selected.contains(key))
      m_Mesh->selected.remove(key);
    else
      m_Mesh->selected.insert(key);
  }
  notifySelection();
}

void MeshUVView::notifySelection()
{
  m_SelectedFaces = QPainterPath();
  m_SelectedFaces.setFillRule(Qt::WindingFill);
  m_SelectedEdges.clear();
  m_SelectedPoints.clear();
  if(m_Mesh->geometry && !m_Mesh->selected.isEmpty())
  {
    const auto &geometry = *m_Mesh->geometry;
    for(int i = 0; i < geometry.triangles.size(); ++i)
    {
      const auto &face = geometry.triangles[i];
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
      {
        if(!m_Mesh->selected.contains(m_Mesh->key(i, corner)))
          continue;
        const QPointF a = geometry.vertices[face[corner]].uv;
        if(m_Mesh->mode == MeshUVData::Selection::Point)
          m_SelectedPoints.push_back(a);
        else if(m_Mesh->mode == MeshUVData::Selection::Edge)
          m_SelectedEdges.push_back(QLineF(a, geometry.vertices[face[(corner + 1) % 3]].uv));
        else
        {
          m_SelectedFaces.moveTo(a);
          const QPointF b = geometry.vertices[face[1]].uv, c = geometry.vertices[face[2]].uv;
          const bool ccw = cross(b - a, c - a) >= 0.0;
          m_SelectedFaces.lineTo(ccw ? b : c);
          m_SelectedFaces.lineTo(ccw ? c : b);
          m_SelectedFaces.closeSubpath();
        }
      }
    }
  }
  setProperty("uvSelectionCount", m_Mesh->selected.size());
  update();
  if(selectionChanged)
    selectionChanged();
}

void MeshUVView::selectAll()
{
  if(m_Mesh->geometry)
    for(int face = 0; face < m_Mesh->geometry->triangles.size(); ++face)
      for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
        m_Mesh->selected.insert(m_Mesh->key(face, corner));
  notifySelection();
}

void MeshUVView::selectLinked()
{
  if(!m_Mesh->geometry || m_Mesh->selected.isEmpty())
    return;
  const auto &geometry = *m_Mesh->geometry;
  QMultiHash<uint32_t, int> adjacent;
  QVector<int> pending;
  QSet<int> visited;
  for(int face = 0; face < geometry.triangles.size(); ++face)
  {
    for(int row : geometry.triangles[face])
      adjacent.insert(geometry.vertices[row].index, face);
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
    for(int row : geometry.triangles[face])
    {
      const uint32_t vertex = geometry.vertices[row].index;
      if(visitedVertices.contains(vertex))
        continue;
      visitedVertices.insert(vertex);
      const auto end = adjacent.constEnd();
      for(auto other = adjacent.constFind(vertex); other != end && other.key() == vertex; ++other)
        if(!visited.contains(other.value()))
        {
          visited.insert(other.value());
          pending.push_back(other.value());
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
  m_Panning = event->button() == Qt::MiddleButton;
  m_DraggingBox = event->button() == Qt::LeftButton && m_BoxSelect;
}

void MeshUVView::mouseMoveEvent(QMouseEvent *event)
{
  const QPointF delta = event->localPos() - m_Last;
  m_Last = event->localPos();
  if(m_Panning)
    m_Pan += delta / atlasTransform().m11();
  if(m_Panning || m_DraggingBox)
    update();
}

void MeshUVView::selectBox(const QRectF &box, bool extend, const QVector<QPointF> &points)
{
  if(!extend)
    m_Mesh->selected.clear();
  const auto &geometry = *m_Mesh->geometry;
  for(int i = 0; i < geometry.triangles.size(); ++i)
  {
    const auto &face = geometry.triangles[i];
    for(int corner = 0; corner < m_Mesh->elementCount(); ++corner)
    {
      QPointF p = points[face[corner]];
      if(m_Mesh->mode == MeshUVData::Selection::Edge)
        p = (p + points[face[(corner + 1) % 3]]) / 2;
      else if(m_Mesh->mode == MeshUVData::Selection::Face)
        p = (p + points[face[1]] + points[face[2]]) / 3;
      if(std::isfinite(p.x()) && std::isfinite(p.y()) && box.contains(p))
        m_Mesh->selected.insert(m_Mesh->key(i, corner));
    }
  }
  notifySelection();
}

void MeshUVView::selectMeshBox(const QRectF &rect, const QSize &size, const QMatrix4x4 &transform,
                               bool extend)
{
  if(!m_Mesh->geometry)
    return;
  ProjectedMesh mesh(*m_Mesh->geometry, size, transform);
  for(int i = 0; i < mesh.points.size(); ++i)
    if(!(mesh.depths[i] >= 0 && mesh.depths[i] <= 1))
      mesh.points[i] = QPointF(qQNaN(), qQNaN());
  selectBox(rect, extend, mesh.points);
}

void MeshUVView::mouseReleaseEvent(QMouseEvent *event)
{
  if(event->button() == Qt::MiddleButton)
    m_Panning = false;
  if(event->button() != Qt::LeftButton)
    return;
  const bool extend = (event->modifiers() & Qt::ShiftModifier) != 0;
  const QRectF box = QRectF(m_Press, event->localPos()).normalized();
  const bool useBox = m_DraggingBox && box.width() + box.height() > 4;
  m_DraggingBox = false;
  if(useBox && m_Mesh->geometry)
  {
    const QTransform transform = atlasTransform();
    QVector<QPointF> points;
    points.reserve(m_Mesh->geometry->vertices.size());
    for(const auto &vertex : m_Mesh->geometry->vertices)
      points.push_back(transform.map(vertex.uv));
    selectBox(box, extend, points);
  }
  else
    selectAt(event->localPos(), extend);
  update();
}

void MeshUVView::wheelEvent(QWheelEvent *event)
{
  const QPoint pixels = event->pixelDelta(), angle = event->angleDelta();
  const double steps = !pixels.isNull() ? (pixels.y() ? pixels.y() : pixels.x()) / 40.0
                                        : (angle.y() ? angle.y() : angle.x()) / 120.0;
  if(steps != 0.0)
  {
    const double before = atlasTransform().m11();
    m_Zoom = qBound(.01, m_Zoom * std::pow(1.2, steps), 50.0);
    const double after = atlasTransform().m11();
#if QT_VERSION >= QT_VERSION_CHECK(5, 14, 0)
    const QPointF offset = event->position() - QRectF(rect()).center();
#else
    const QPointF offset = event->posF() - QRectF(rect()).center();
#endif
    m_Pan += offset * (1.0 / after - 1.0 / before);
    setProperty("uvZoom", m_Zoom);
    update();
  }
  event->accept();
}

bool MeshUVView::handleSelectionShortcut(QKeyEvent *event)
{
  const Qt::KeyboardModifiers mods = event->modifiers() & ~Qt::KeypadModifier;
  const int key = event->key();
  if(mods == Qt::NoModifier && key >= Qt::Key_1 && key <= Qt::Key_3 && modeShortcut)
    modeShortcut(key - Qt::Key_1);
  else if(mods == Qt::NoModifier && key == Qt::Key_A)
    selectAll();
  else if(mods == Qt::AltModifier && key == Qt::Key_A)
    clearSelection();
  else if(mods == Qt::NoModifier && key == Qt::Key_L)
    selectLinked();
  else if(mods == Qt::NoModifier && key == Qt::Key_B && boxShortcut)
    boxShortcut();
  else
    return false;
  event->accept();
  return true;
}

void MeshUVView::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_F && event->modifiers() == Qt::NoModifier)
  {
    focusSelection();
    event->accept();
  }
  else if(!handleSelectionShortcut(event))
    QWidget::keyPressEvent(event);
}

MeshUVTextureView::MeshUVTextureView(std::shared_ptr<MeshUVData> mesh) : m_Mesh(mesh)
{
  QSurfaceFormat fmt = format();
  fmt.setDepthBufferSize(24);
  setFormat(fmt);
}

MeshUVTextureView::~MeshUVTextureView()
{
  if(context())
  {
    disconnect(context(), nullptr, this, nullptr);
    releaseGL();
  }
}

void MeshUVTextureView::releaseGL()
{
  makeCurrent();
  m_Triangles.destroy();
  m_Lines.destroy();
  m_Selected.destroy();
  m_Texture.reset();
  m_Shader.reset();
  m_TextureKey = 0;
  m_UploadedGeometry.reset();
  m_SelectionDirty = true;
  doneCurrent();
}

void MeshUVTextureView::initializeGL()
{
  initializeOpenGLFunctions();
  connect(context(), &QOpenGLContext::aboutToBeDestroyed, this, &MeshUVTextureView::releaseGL,
          Qt::DirectConnection);
  m_Shader.reset(new QOpenGLShaderProgram);
  const char *vertex =
      "attribute highp vec4 position; attribute highp vec2 uv;"
      "uniform highp mat4 transform; uniform bool homogenous;"
      "varying highp vec2 texcoord;"
      "void main() {"
      "  gl_Position = transform * vec4(position.xyz, homogenous ? position.w : 1.0);"
      "  gl_Position.z = gl_Position.z * 2.0 - gl_Position.w;"
      "  gl_PointSize = 7.0; texcoord = uv;"
      "}";
  const char *fragment =
      "uniform sampler2D image; uniform lowp vec4 color; uniform bool textured, flipY;"
      "varying highp vec2 texcoord;"
      "void main() {"
      "  highp vec2 coord = fract(texcoord);"
      "  if (flipY) coord.y = 1.0 - coord.y;"
      "  gl_FragColor = textured ? texture2D(image, coord) : color;"
      "}";
  if(!m_Shader->addShaderFromSourceCode(QOpenGLShader::Vertex, vertex) ||
     !m_Shader->addShaderFromSourceCode(QOpenGLShader::Fragment, fragment) || !m_Shader->link() ||
     !m_Triangles.create() || !m_Lines.create() || !m_Selected.create())
  {
    m_Shader.reset();
    if(unavailable)
      unavailable();
    return;
  }
  if(!context()->isOpenGLES())
    glEnable(0x8642);    // GL_PROGRAM_POINT_SIZE
}

void MeshUVTextureView::setTransform(const QMatrix4x4 &transform, bool homogenous, bool wireframe)
{
  m_Transform = transform;
  m_Homogenous = homogenous;
  m_Wireframe = wireframe;
  update();
}

void MeshUVTextureView::setSelection(QVector<QVector4D> vertices, unsigned int primitive)
{
  m_Selection = std::move(vertices);
  m_SelectionPrimitive = primitive;
  m_SelectionDirty = true;
  update();
}

void MeshUVTextureView::paintGL()
{
  const QColor background = QApplication::palette().color(QPalette::Dark);
  glClearColor(background.redF(), background.greenF(), background.blueF(), 1);
  glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
  if(!m_Shader || !m_Mesh->geometry || m_Mesh->geometry->triangles.isEmpty())
    return;

  struct Vertex
  {
    QVector4D position;
    QVector2D uv;
  };
  if(m_UploadedGeometry != m_Mesh->geometry)
  {
    m_UploadedGeometry = m_Mesh->geometry;
    QVector<Vertex> triangles;
    QVector<QVector4D> lines;
    triangles.reserve(m_UploadedGeometry->triangles.size() * 3);
    lines.reserve(m_UploadedGeometry->triangles.size() * 6);
    for(const auto &face : m_UploadedGeometry->triangles)
    {
      for(int corner = 0; corner < 3; ++corner)
      {
        const auto &v = m_UploadedGeometry->vertices[face[corner]];
        triangles.push_back({v.position, QVector2D(v.uv)});
        lines.push_back(v.position);
        lines.push_back(m_UploadedGeometry->vertices[face[(corner + 1) % 3]].position);
      }
    }
    m_TriangleVertices = triangles.size();
    m_Triangles.bind();
    m_Triangles.allocate(triangles.constData(), triangles.size() * sizeof(Vertex));
    m_Lines.bind();
    m_Lines.allocate(lines.constData(), lines.size() * sizeof(QVector4D));
  }
  if(m_TextureKey != m_Mesh->texture.cacheKey())
  {
    m_TextureKey = m_Mesh->texture.cacheKey();
    m_Texture.reset();
    if(!m_Mesh->texture.isNull())
    {
      // QImage's first row is sent to V=0, matching the UV atlas.
      m_Texture.reset(new QOpenGLTexture(m_Mesh->texture, QOpenGLTexture::DontGenerateMipMaps));
      m_Texture->setMinMagFilters(QOpenGLTexture::Linear, QOpenGLTexture::Linear);
      m_Texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    }
  }
  if(m_SelectionDirty)
  {
    m_SelectionDirty = false;
    m_Selected.bind();
    m_Selected.allocate(m_Selection.constData(), m_Selection.size() * sizeof(QVector4D));
  }

  glEnable(GL_DEPTH_TEST);
  glDepthFunc(GL_LEQUAL);
  m_Shader->bind();
  m_Shader->setUniformValue("transform", m_Transform);
  m_Shader->setUniformValue("homogenous", m_Homogenous);
  m_Shader->setUniformValue("flipY", m_Mesh->flipTexture);
  m_Shader->setUniformValue("image", 0);
  m_Shader->setUniformValue("textured", m_Texture != nullptr);
  m_Shader->setUniformValue("color", QVector4D(.65f, .65f, .65f, 1));
  if(m_Texture)
    m_Texture->bind(0);

  m_Triangles.bind();
  const int position = m_Shader->attributeLocation("position");
  const int uv = m_Shader->attributeLocation("uv");
  m_Shader->enableAttributeArray(position);
  m_Shader->enableAttributeArray(uv);
  m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(Vertex));
  m_Shader->setAttributeBuffer(uv, GL_FLOAT, offsetof(Vertex, uv), 2, sizeof(Vertex));
  glDrawArrays(GL_TRIANGLES, 0, m_TriangleVertices);
  m_Shader->disableAttributeArray(uv);
  m_Shader->setUniformValue("textured", false);
  if(m_Wireframe)
  {
    m_Lines.bind();
    m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(QVector4D));
    m_Shader->setUniformValue("color", QVector4D(.18f, .18f, .18f, 1));
    glDrawArrays(GL_LINES, 0, m_TriangleVertices * 2);
  }
  if(!m_Selection.isEmpty())
  {
    m_Selected.bind();
    m_Shader->setAttributeBuffer(position, GL_FLOAT, 0, 4, sizeof(QVector4D));
    m_Shader->setUniformValue("color", QVector4D(1.0f, .61f, .20f, 1));
    glDrawArrays(m_SelectionPrimitive, 0, m_Selection.size());
  }
  m_Shader->disableAttributeArray(position);
  QOpenGLBuffer::release(QOpenGLBuffer::VertexBuffer);
  m_Shader->release();
  if(m_Texture)
    m_Texture->release();
  if(rendered)
    rendered(m_Texture != nullptr, m_Wireframe);
}
