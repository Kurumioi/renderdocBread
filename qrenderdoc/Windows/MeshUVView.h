#pragma once

#include <QCoreApplication>
#include <QImage>
#include <QMatrix4x4>
#include <QOpenGLBuffer>
#include <QOpenGLFunctions>
#include <QOpenGLWindow>
#include <QPainterPath>
#include <QPixmap>
#include <QSet>
#include <QWidget>
#include <array>
#include <functional>
#include <memory>

class QOpenGLShaderProgram;
class QOpenGLTexture;

enum class MeshCameraGesture
{
  None,
  Orbit,
  Pan,
  Dolly
};
MeshCameraGesture meshCameraGesture(int preset, Qt::MouseButtons buttons, Qt::KeyboardModifiers mods);

struct MeshUVGeometry
{
  struct Vertex
  {
    QVector4D position;
    QPointF uv;
    uint32_t index = 0;
  };
  QVector<Vertex> vertices;
  QVector<std::array<int, 3>> triangles;
  QPainterPath wire;
  QRectF bounds;
};

struct MeshUVData
{
  enum class Selection
  {
    Point,
    Edge,
    Face
  };
  std::shared_ptr<const MeshUVGeometry> geometry;
  QImage texture;
  Selection mode = Selection::Face;
  QSet<quint64> selected;
  bool flipTexture = false;

  quint64 key(int triangle, int corner) const;
  int elementCount() const { return mode == Selection::Face ? 1 : 3; }
};

class MeshUVView : public QWidget
{
  Q_DECLARE_TR_FUNCTIONS(MeshUVView)
public:
  MeshUVView(std::shared_ptr<MeshUVData> mesh, QWidget *parent);
  void resetView();
  void geometryChanged();
  void textureChanged();
  void focusSelection();
  void selectAll();
  void selectLinked();
  void clearSelection();
  void setBoxSelect(bool enabled);
  bool handleSelectionShortcut(QKeyEvent *event);
  void selectMesh(const QPointF &point, const QSize &size, const QMatrix4x4 &transform, bool extend);
  void selectMeshBox(const QRectF &rect, const QSize &size, const QMatrix4x4 &transform, bool extend);
  std::function<void()> selectionChanged;
  std::function<void()> boxShortcut;
  std::function<void(int)> modeShortcut;

protected:
  void paintEvent(QPaintEvent *) override;
  void mousePressEvent(QMouseEvent *event) override;
  void mouseMoveEvent(QMouseEvent *event) override;
  void mouseReleaseEvent(QMouseEvent *event) override;
  void wheelEvent(QWheelEvent *event) override;
  void keyPressEvent(QKeyEvent *event) override;

private:
  QTransform atlasTransform() const;
  void selectAt(const QPointF &point, bool extend);
  void selectElement(int triangle, int corner, bool extend);
  void selectBox(const QRectF &box, bool extend, const QVector<QPointF> &points);
  void notifySelection();
  void updateWireCache(double density);

  std::shared_ptr<MeshUVData> m_Mesh;
  QPixmap m_Texture, m_Wire;
  QRectF m_WireBounds;
  QPainterPath m_SelectedFaces;
  QVector<QLineF> m_SelectedEdges;
  QVector<QPointF> m_SelectedPoints;
  bool m_BoxSelect = false;
  bool m_DraggingBox = false;
  bool m_Panning = false;
  QPointF m_Press, m_Last, m_Pan;
  double m_Zoom = 1.0;
  double m_WireDensity = 0.0;
};

// The explicit UV texture mode shares the native camera and mesh data. Static vertex and
// image uploads are independent from the camera and from selection changes.
class MeshUVTextureView : public QOpenGLWindow, protected QOpenGLFunctions
{
public:
  explicit MeshUVTextureView(std::shared_ptr<MeshUVData> mesh);
  ~MeshUVTextureView() override;
  void setTransform(const QMatrix4x4 &transform, bool homogenous, bool wireframe);
  void setSelection(QVector<QVector4D> vertices, unsigned int primitive);
  std::function<void()> unavailable;
  std::function<void(bool, bool)> rendered;

protected:
  void initializeGL() override;
  void paintGL() override;

private:
  void releaseGL();
  std::shared_ptr<MeshUVData> m_Mesh;
  std::shared_ptr<const MeshUVGeometry> m_UploadedGeometry;
  QMatrix4x4 m_Transform;
  std::unique_ptr<QOpenGLShaderProgram> m_Shader;
  std::unique_ptr<QOpenGLTexture> m_Texture;
  QOpenGLBuffer m_Triangles, m_Lines, m_Selected;
  QVector<QVector4D> m_Selection;
  qint64 m_TextureKey = 0;
  int m_TriangleVertices = 0;
  unsigned int m_SelectionPrimitive = GL_TRIANGLES;
  bool m_Homogenous = false, m_Wireframe = false, m_SelectionDirty = true;
};
