/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 The RenderDoc Authors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#pragma once

#include <QHash>
#include <QString>
#include <QUrl>
#include <QVector>
#include <QWidget>
#include <functional>

class AITurnRailWidget;
class QTextBrowser;
class QTimer;

struct AIActivityItem
{
  QString id;
  QString kind;
  QString title;
  QString detail;
  qint64 timestampMs = 0;
  qint64 finishedTimestampMs = 0;
  bool completed = false;
  bool error = false;
};

struct AIChatMessage
{
  QString id;
  QString role;
  QString text;
  QString state;
  QString taskId;
  QString provisionalText;
  qint64 timestampMs = 0;
  qint64 finishedTimestampMs = 0;
  QVector<AIActivityItem> activity;
  bool activityExpanded = false;
};

// Lightweight, read-only transcript used by AIAssistant. The view renders a deliberately bounded
// Markdown subset and never accepts raw HTML or image markup from model output. All links are
// intercepted so qrenderdoc retains control over external and rdoc:// navigation.
class AIConversationView : public QWidget
{
public:
  explicit AIConversationView(QWidget *parent = 0);

  void setMessages(const QVector<AIChatMessage> &messages);
  void clear();
  void scrollToMessage(const QString &messageId);

  void setLinkHandler(std::function<void(const QUrl &)> handler);
  void setActivityToggleHandler(std::function<void(const QString &messageId, bool expanded)> handler);

protected:
  void changeEvent(QEvent *event) override;

private:
  void render(bool preserveScroll = true);
  void activateLink(const QUrl &url);

  QVector<AIChatMessage> m_Messages;
  QHash<QString, QString> m_MessageAnchors;
  AITurnRailWidget *m_TurnRail = NULL;
  QTextBrowser *m_Browser = NULL;
  QTimer *m_ElapsedTimer = NULL;
  std::function<void(const QUrl &)> m_LinkHandler;
  std::function<void(const QString &, bool)> m_ActivityToggleHandler;
  int m_RenderGeneration = 0;
};
