#pragma once

#include <QCoreApplication>
#include <QJsonObject>
#include <QUrl>
#include <QWidget>
#include <functional>

// A native, offline visual answer. Only a hash-addressed Host manifest and its
// verified PNG/geometry blobs are loaded; model HTML/URLs never enter a renderer.
class AIVisualReply : public QWidget
{
  Q_DECLARE_TR_FUNCTIONS(AIVisualReply)
public:
  using Navigate = std::function<void(const QUrl &, const QJsonObject &)>;
  AIVisualReply(const QString &identity, Navigate navigate, QWidget *parent = NULL);
  static bool validIdentity(const QString &identity);
  static QString storageRoot();
  static QJsonObject navigationTarget(const QString &identity);
  static int inlineHeight(const QString &identity);
};
