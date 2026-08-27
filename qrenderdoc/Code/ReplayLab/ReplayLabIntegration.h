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

#include <QCoreApplication>
#include <QString>
#include <QStringList>

class CaptureContext;

// Stable C++ boundary for RenderDoc's first-party ReplayLab runtime. The replay bridge remains in
// the embedded Python 3.6 process during this migration, but it is loaded from an application-owned
// path and is not an extension/plugin. Provider SDKs and Agent processes remain isolated sidecars.
class ReplayLabIntegration
{
  Q_DECLARE_TR_FUNCTIONS(ReplayLabIntegration)

public:
  enum class Intent
  {
    ExplainCurrentEvent,
    InspectPickedPixel,
    ReverseEditSelectedShader,
  };

  explicit ReplayLabIntegration(CaptureContext &context);

  QString Initialize();
  bool IsAvailable() const { return m_Available; }
  QString LastError() const { return m_LastError; }

  QString QueueIntent(Intent intent, const QString &focusStage, QString *error) const;
  QStringList AvailableShaderStages(QString *error = NULL) const;

private:
  static QString IntentId(Intent intent);

  CaptureContext &m_Context;
  bool m_Available = false;
  QString m_LastError;
};
