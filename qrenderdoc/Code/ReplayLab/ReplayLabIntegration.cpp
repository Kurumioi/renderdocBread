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

#include "ReplayLabIntegration.h"

#include <QCoreApplication>
#include <QDir>
#include <QMenu>
#include "Code/CaptureContext.h"
#include "Code/pyrenderdoc/PythonContext.h"
#include "Windows/MainWindow.h"

namespace
{
const rdcstr IntegrationModule = "renderdoc_mcp_bridge";
}

ReplayLabIntegration::ReplayLabIntegration(CaptureContext &context) : m_Context(context)
{
}

QString ReplayLabIntegration::Initialize()
{
  if(m_Available)
    return QString();

#if !defined(Q_OS_WIN)
  // The packaged runtime currently relies on Windows Job Objects to contain AgentHost and every
  // provider/MCP descendant. Do not expose a weaker parent-only kill contract on other platforms.
  m_LastError = tr("The AI runtime currently requires Windows process-tree containment.");
  return m_LastError;
#endif

  const QString packageRoot =
      QDir(QCoreApplication::applicationDirPath())
          .absoluteFilePath(lit("integrations/replaylab/python"));    // i18n-ignore: fixed runtime path

  PythonContext::ProcessExtensionWork([this, &packageRoot]() {
    // A built-in integration has its own native lifetime bucket. It must never inherit pending
    // QObject wrappers from a previously failed extension load.
    for(QObject *object : m_Context.m_PendingExtensionObjects)
      delete object;
    m_Context.m_PendingExtensionObjects.clear();

    m_LastError =
        PythonContext::LoadInternalIntegration(m_Context, IntegrationModule, packageRoot);
    if(m_LastError.isEmpty())
    {
      m_Context.m_ReplayLabIntegrationObjects.swap(m_Context.m_PendingExtensionObjects);
      m_Available = true;
    }
    else
    {
      for(QObject *object : m_Context.m_PendingExtensionObjects)
        delete object;
      m_Context.m_PendingExtensionObjects.clear();
    }
  });

  for(QAction *action : m_Context.m_MainWindow->GetMenuActions())
    m_Context.CleanMenu(action);
  m_Context.m_RegisteredMenuItems.removeAll(NULL);

  return m_LastError;
}

QString ReplayLabIntegration::IntentId(Intent intent)
{
  // Protocol identifiers are intentionally not translated.
  switch(intent)
  {
    case Intent::ExplainCurrentEvent:
      return lit("explain-current-event");    // i18n-ignore: stable intent protocol identifier
    case Intent::InspectPickedPixel:
      return lit("inspect-picked-pixel");    // i18n-ignore: stable intent protocol identifier
    case Intent::ReverseEditSelectedShader:
      return lit("reverse-edit-selected-shader");    // i18n-ignore: stable intent protocol identifier
  }

  return QString();
}

QString ReplayLabIntegration::QueueIntent(Intent intent, const QString &focusStage,
                                          QString *error) const
{
  if(error)
    error->clear();
  if(!m_Available)
  {
    if(error)
      *error = m_LastError.isEmpty() ? tr("The built-in RenderDoc AI integration is unavailable.")
                                    : m_LastError;
    return QString();
  }

  return PythonContext::InvokeInternalIntegration(
      IntegrationModule, "enqueue_native_intent", {IntentId(intent), focusStage}, error);
}

QStringList ReplayLabIntegration::AvailableShaderStages(QString *error) const
{
  if(error)
    error->clear();
  if(!m_Available)
  {
    if(error)
      *error = m_LastError.isEmpty() ? tr("The built-in RenderDoc AI integration is unavailable.")
                                    : m_LastError;
    return {};
  }

  const QString stages = PythonContext::InvokeInternalIntegration(
      IntegrationModule, "available_native_shader_stages", {}, error);
  return stages.split(QLatin1Char(','), QString::SkipEmptyParts);
}
