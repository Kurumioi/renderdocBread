#pragma once

#include <QCoreApplication>
#include <QImage>
#include <QSet>
#include <QVector3D>
#include <QWidget>
#include <array>
#include <functional>
#include <memory>

enum class MeshCameraGesture
{
  None,
  Orbit,
  Pan,
  Dolly
};
MeshCameraGesture meshCameraGesture(int preset, Qt::MouseButtons buttons, Qt::KeyboardModifiers mods);

struct MeshUVData
{
  enum class Selection
  {
    Point,
    Edge,
    Face
  };
  struct Vertex
  {
    QVector3D position;
    QPointF uv;
    uint32_t index = 0;
  };
  QVector<Vertex> vertices;
  QVector<std::array<int, 3>> triangles;
  QImage texture;
  Selection mode = Selection::Face;
  QSet<quint64> selected;
  bool showTexture = false;
  bool flipTexture = false;

  quint64 key(int triangle, int corner) const;
  int elementCount() const { return mode == Selection::Face ? 1 : 3; }
};

class MeshUVView : public QWidget
{
  Q_DECLARE_TR_FUNCTIONS(MeshUVView)
public:
  MeshUVView(std::shared_ptr<MeshUVData> mesh, bool atlas, QWidget *parent);
  void resetView();
  void focusSelection();
  void selectAll();
  void selectLinked();
  void clearSelection();
  void setBoxSelect(bool enabled);
  std::function<void()> selectionChanged;
  std::function<int()> cameraPreset;

protected:
  void paintEvent(QPaintEvent *) override;
  void mousePressEvent(QMouseEvent *event) override;
  void mouseMoveEvent(QMouseEvent *event) override;
  void mouseReleaseEvent(QMouseEvent *event) override;
  void wheelEvent(QWheelEvent *event) override;
  void keyPressEvent(QKeyEvent *event) override;

private:
  QPointF atlasPoint(const QPointF &uv) const;
  QPointF selectionPoint(int triangle, int corner) const;
  void selectAt(const QPointF &point, bool extend);
  void paintAtlas(QPainter &painter);
  QImage rasterize(const QSize &size) const;
  void notifySelection();

  std::shared_ptr<MeshUVData> m_Mesh;
  bool m_Atlas;
  bool m_BoxSelect = false;
  bool m_DraggingBox = false;
  MeshCameraGesture m_Gesture = MeshCameraGesture::None;
  QPointF m_Press, m_Last, m_Pan;
  QVector3D m_Center;
  double m_Zoom = 1.0;
  double m_Radius = 1.0;
  double m_Yaw = 0.0;
  double m_Pitch = 0.0;
};
