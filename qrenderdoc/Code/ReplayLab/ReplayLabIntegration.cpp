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
#include <QFileInfo>
#include <QMenu>
#include <QScopedValueRollback>
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
          .absoluteFilePath(
              lit("integrations/replaylab/python"));    // i18n-ignore: fixed runtime path

  PythonContext::ProcessExtensionWork([this, &packageRoot]() {
    // The bundled bridge intentionally reuses the stable extension callback ABI. Mark the
    // synchronous registration scope so its application-owned menus do not receive plugin icons;
    // optional extensions loaded through CaptureContext::LoadExtension retain the default.
    QScopedValueRollback<bool> registrationScope(m_Context.m_RegisteringFirstPartyIntegration,
                                                  true);

    // A built-in integration has its own native lifetime bucket. It must never inherit pending
    // QObject wrappers from a previously failed extension load.
    for(QObject *object : m_Context.m_PendingExtensionObjects)
      delete object;
    m_Context.m_PendingExtensionObjects.clear();

    m_LastError = PythonContext::LoadInternalIntegration(m_Context, IntegrationModule, packageRoot);
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

QString ReplayLabIntegration::CurrentContextJSON(QString *error) const
{
  return CurrentContextJSON(QString(), error);
}

QString ReplayLabIntegration::CurrentContextJSON(const QString &focusStage, QString *error) const
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

  return PythonContext::InvokeInternalIntegration(IntegrationModule, "current_native_context_json",
                                                  {focusStage}, error);
}

QString ReplayLabIntegration::BeginCurrentContextFreezeJSON(const QString &focusStage,
                                                            QString *error) const
{
  return BeginCurrentContextFreezeJSON(focusStage, -1, -1, QString(), error);
}

QString ReplayLabIntegration::BeginCurrentContextFreezeJSON(const QString &focusStage,
                                                            qint64 expectedCurrentEventId,
                                                            qint64 expectedSelectedEventId,
                                                            const QString &expectedCaptureSessionId,
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
      IntegrationModule, "begin_native_context_freeze_json",
      {focusStage, expectedCurrentEventId > 0 ? QString::number(expectedCurrentEventId) : QString(),
       expectedSelectedEventId >= 0 ? QString::number(expectedSelectedEventId) : QString(),
       expectedCaptureSessionId},
      error);
}

QString ReplayLabIntegration::PollCurrentContextFreezeJSON(const QString &jobId, QString *error) const
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
      IntegrationModule, "poll_native_context_freeze_json", {jobId}, error);
}

QString ReplayLabIntegration::AbandonCurrentContextFreezeJSON(const QString &jobId, QString *error) const
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
      IntegrationModule, "abandon_native_context_freeze_json", {jobId}, error);
}

QString ReplayLabIntegration::ValidateCurrentContextFreezeResultPath(const QString &jobId,
                                                                     const QString &resultPath,
                                                                     QString *error) const
{
  if(error)
    error->clear();

  const auto isLowerHex32 = [](const QString &value) {
    if(value.size() != 32)
      return false;
    for(const QChar character : value)
    {
      const ushort code = character.unicode();
      if(!((code >= ushort('0') && code <= ushort('9')) ||
           (code >= ushort('a') && code <= ushort('f'))))
        return false;
    }
    return true;
  };

  const QFileInfo suppliedInfo(resultPath);
  const QString normalisedPath =
      QDir::cleanPath(QDir::fromNativeSeparators(suppliedInfo.absoluteFilePath()));
  const QByteArray localAppData = qgetenv("LOCALAPPDATA");
  const QString localRoot =
      localAppData.isEmpty() ? QDir::tempPath() : QString::fromLocal8Bit(localAppData);
  // i18n-ignore: fixed internal ReplayLab IPC directory, never shown to the user.
  const QString instancesRelativePath = lit("ReplayLab/ipc-v2/instances");
  const QString instancesRoot = QDir::cleanPath(
      QDir::fromNativeSeparators(QDir(localRoot).absoluteFilePath(instancesRelativePath)));

  const QDir jobDirectory = suppliedInfo.dir();
  const QFileInfo jobDirectoryInfo(jobDirectory.absolutePath());
  const QDir instanceDirectory = jobDirectoryInfo.dir();
  const QFileInfo instanceDirectoryInfo(instanceDirectory.absolutePath());
  const QString instanceName = instanceDirectory.dirName();
  const QString instancePrefix =
      QString::number(QCoreApplication::applicationPid()) + QLatin1Char('-');
  const QString instanceNonce = instanceName.mid(instancePrefix.size());
  // i18n-ignore: fixed internal ReplayLab IPC job path, never shown to the user.
  const QString jobRelativePath = lit("native-context-jobs/%1.json").arg(jobId);
  const QString expectedPath = QDir::cleanPath(QDir::fromNativeSeparators(
      QDir(instanceDirectory.absolutePath()).absoluteFilePath(jobRelativePath)));

#if defined(Q_OS_WIN)
  const Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
  const Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif

  // i18n-ignore: fixed internal ReplayLab IPC result-file extension, never shown to the user.
  const QString resultExtension = lit(".json");
  // i18n-ignore: fixed internal ReplayLab IPC job-directory name, never shown to the user.
  const QString jobDirectoryName = lit("native-context-jobs");
  const bool valid =
      isLowerHex32(jobId) && suppliedInfo.isAbsolute() &&
      suppliedInfo.fileName() == jobId + resultExtension &&
      jobDirectory.dirName() == jobDirectoryName && instanceName.startsWith(instancePrefix) &&
      isLowerHex32(instanceNonce) &&
      instanceDirectoryInfo.dir().absolutePath().compare(instancesRoot, pathCase) == 0 &&
      normalisedPath.compare(expectedPath, pathCase) == 0 && jobDirectoryInfo.exists() &&
      jobDirectoryInfo.isDir() && !jobDirectoryInfo.isSymLink() && instanceDirectoryInfo.exists() &&
      instanceDirectoryInfo.isDir() && !instanceDirectoryInfo.isSymLink() &&
      (!suppliedInfo.exists() || (suppliedInfo.isFile() && !suppliedInfo.isSymLink()));
  if(!valid)
  {
    if(error)
      *error = tr("The AI context result path is invalid.");
    return QString();
  }

  return normalisedPath;
}
