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

#include <QByteArray>
#include <QFrame>
#include <QHash>
#include <QJsonArray>
#include <QMap>
#include <QPair>
#include <QSet>
#include <QStringList>
#include <QVector>
#include "Code/Interface/QRDInterface.h"
#include "Code/ReplayLab/ReplayLabIntegration.h"
#include "AIConversationView.h"

class QComboBox;
class QLabel;
class QPlainTextEdit;
class QProcess;
class QPushButton;
class QTimer;
class AIConversationListView;
class AIPendingChatView;
class CaptureContext;

// Native qrenderdoc control surface for ReplayLab's provider-neutral RenderDoc AI host. The
// provider SDKs and managed Agent processes deliberately remain out-of-process so they cannot
// destabilise replay or gain implicit access to ICaptureContext.
class AIAssistant : public QFrame, public ICaptureViewer
{
  Q_DECLARE_TR_FUNCTIONS(AIAssistant)

public:
  explicit AIAssistant(CaptureContext &ctx, QWidget *parent = 0);
  ~AIAssistant();

  QWidget *Widget() { return this; }

  void NotifyIntentQueued(const QString &taskId);
  void ReportError(const QString &message);
  // CaptureContext calls this after Save Capture As so the in-memory thread history follows the
  // permanent RDC path without modifying the RDC itself.
  void RebindCaptureFile(const QString &capturePath);
  void Shutdown();

  // Builds the AI page embedded in qrenderdoc's normal Settings dialog. The returned widget is
  // owned by parent. Connection credentials are never written to the profile file or process
  // command line; the user may keep one in this instance or store it through AgentHost in the
  // operating-system credential vault.
  QWidget *CreateSettingsPage(QWidget *parent);

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override;
  void OnEventChanged(uint32_t eventId) override;

private:
  struct ConversationThread
  {
    QString id;
    QString title;
    qint64 createdMs = 0;
    qint64 updatedMs = 0;
    bool pinned = false;
    bool unread = false;
    QVector<AIChatMessage> messages;
  };

  struct PendingChat
  {
    QString taskId;
    QString messageId;
    QString conversationId;
    QString prompt;
    QString effectivePrompt;
    QString skill;
    QStringList historyExcludeMessageIds;
    bool includeConversation = true;
  };

  void appendSystem(const QString &text, bool error = false);
  QString appendUser(const QString &text, const QString &taskId = QString(),
                     const QString &state = QString(),
                     const QString &conversationId = QString());
  void appendAssistant(const QString &text, const QString &conversationId = QString(),
                       const QString &taskId = QString());
  void renderConversation(bool refreshList = false);
  void refreshConversationList();
  void refreshPendingChats();
  void createConversation();
  void deleteConversation(const QString &conversationId = QString());
  void toggleConversationPinned(const QString &conversationId);
  void selectConversation(const QString &conversationId);
  ConversationThread *conversation(const QString &conversationId = QString());
  const ConversationThread *conversation(const QString &conversationId = QString()) const;
  AIChatMessage *message(const QString &conversationId, const QString &messageId);
  AIChatMessage *taskMessage(const QString &taskId);
  void addTaskActivity(const QString &taskId, const QString &kind, const QString &title,
                       const QString &detail = QString(), bool completed = false,
                       bool error = false, qint64 timestampMs = 0,
                       const QString &activityId = QString());
  void setTaskState(const QString &taskId, const QString &state, bool collapseActivity);
  void updateComposerState();
  void handleTranscriptLink(const QUrl &url);

  void loadConversations();
  void saveConversations();
  void scheduleConversationSave();
  void resetConversations();
  QJsonObject conversationToJSON(const ConversationThread &thread, int *remainingChars) const;
  bool conversationFromJSON(const QJsonObject &object, ConversationThread &thread) const;

  void updateCaptureStatus();
  bool queueNativeIntent(ReplayLabIntegration::Intent intent, const QString &prompt,
                         const QString &focusStage = QString());
  void showShaderIntentMenu(QWidget *anchor);
  void clearCaptureTasks();

  QString findAgentCLI() const;
  QString findBridgeInstance() const;
  QString taskEventRoot() const;
  void configureProcess(QProcess *process,
                        const QString &credentialProfile = QString()) const;
  QByteArray runCLISync(const QStringList &arguments, int timeoutMs, int *exitCode,
                        QString *error, const QString &credentialProfile = QString()) const;
  QByteArray runCLIInputSync(const QStringList &arguments, const QByteArray &input, int timeoutMs,
                             int *exitCode, QString *error,
                             const QString &credentialProfile = QString()) const;

  void refreshProfiles();
  void refreshModelCombo();
  void selectProfile(int index);
  void selectModel(int index);
  bool connectionMutationAllowed(QWidget *parent) const;
  void manageConnections();
  void runDoctor();

  void ensureWorker();
  void restartWorker();
  void stopWorker(bool restartAfterStop = false);
  bool stopWorkerForProfileMutation(QString *error);
  void workerStopTimedOut();
  void workerFinished(QProcess *process);
  void forceStopProcessesForDestruction();
  bool attachKillOnCloseJob(QProcess *process, void *&jobHandle, QString *error) const;
  void closeKillOnCloseJob(void *&jobHandle) const;

  QString newTaskId() const;
  QString newMessageId() const;
  QString conversationPrompt(const QString &prompt, const QString &conversationId,
                             const QStringList &excludeMessageIds = QStringList()) const;
  void startChat(const QString &prompt, const QString &skill = QString(),
                 bool includeConversation = true);
  void launchChat(PendingChat request);
  void startNextChat();
  void adjustQueuedChat(const QString &taskId);
  void editQueuedChat(const QString &taskId);
  void deleteQueuedChat(const QString &taskId);
  void cancelChat(const QString &terminalState = QString(), bool continueQueue = true);
  void finishChatProcess(int exitCode);
  void drainChatTaskEvents(const QString &taskId, int exitCode, qint64 remainingBytes);
  void finishChatResult(const QString &taskId, int exitCode);

  void watchTask(const QString &taskId, const QString &userPrompt = QString(),
                 const QString &conversationId = QString(),
                 const QString &messageId = QString());
  void pollTaskEvents();
  qint64 pollTaskEvent(const QString &taskId, qint64 maxReadBytes, bool *atEnd = NULL);
  void consumeTaskEvent(const QString &taskId, const QByteArray &line);
  void finishTask(const QString &taskId, const QString &kind, const QString &summary,
                  qint64 timestampMs = 0);

  CaptureContext &m_Ctx;

  QLabel *m_Status = NULL;
  QLabel *m_Context = NULL;
  QLabel *m_QueueStatus = NULL;
  AIConversationListView *m_ConversationList = NULL;
  AIPendingChatView *m_PendingChats = NULL;
  QComboBox *m_ProfileCombo = NULL;
  QComboBox *m_ModelCombo = NULL;
  AIConversationView *m_Transcript = NULL;
  QPlainTextEdit *m_Prompt = NULL;
  QPushButton *m_Send = NULL;
  QPushButton *m_Cancel = NULL;

  QTimer *m_SupervisorTimer = NULL;
  QTimer *m_EventTimer = NULL;
  QTimer *m_WorkerStopTimer = NULL;
  QTimer *m_ConversationSaveTimer = NULL;
  QTimer *m_ConversationRenderTimer = NULL;
  QProcess *m_Worker = NULL;
  QProcess *m_ChatProcess = NULL;
  void *m_WorkerJob = NULL;
  void *m_ChatJob = NULL;

  QString m_CLIPath;
  QString m_InstanceId;
  QString m_ActiveProfile;
  QString m_ActiveKeyEnv;
  QString m_ActiveModel;
  QString m_ActiveConversationId;
  QString m_ActiveChatConversationId;
  QString m_ActiveChatMessageId;
  QString m_ChatTaskId;
  QString m_CaptureConversationPath;
  QString m_PendingIntentPrompt;
  QString m_LastWorkerError;
  QByteArray m_ChatStdout;
  QByteArray m_ChatStderr;
  bool m_ProfileComboLoading = false;
  bool m_ConnectionDialogOpen = false;
  bool m_RestartWorkerPending = false;
  bool m_WorkerStopping = false;
  bool m_ShuttingDown = false;
  int m_WorkerStopPhase = 0;
  int m_WorkerFailures = 0;
  qint64 m_NextWorkerStartMs = 0;

  // Indexed by stable connection id, not environment-variable name. This holds only explicit
  // session credentials; durable credentials remain in the OS vault and never enter this map.
  QMap<QString, QString> m_SessionSecrets;
  QHash<QString, QString> m_ProfileKeyEnvs;
  QHash<QString, QStringList> m_ProfileModels;
  QHash<QString, QString> m_ProfileDefaultModels;
  QHash<QString, QStringList> m_ProfileRunInputSchemas;
  QJsonArray m_ProfileRows;
  QSet<QString> m_KnownTasks;
  QSet<QString> m_RunningTasks;
  QSet<QString> m_CompletedTasks;
  QHash<QString, qint64> m_TaskOffsets;
  QHash<QString, qint64> m_TaskSequences;
  QHash<QString, QByteArray> m_TaskTails;
  QHash<QString, QString> m_TaskTexts;
  QHash<QString, QString> m_UserPrompts;
  QHash<QString, QString> m_TaskConversationIds;
  QHash<QString, QString> m_TaskMessageIds;
  QVector<ConversationThread> m_Conversations;
  QVector<PendingChat> m_ChatQueue;
  PendingChat m_ActiveChatRequest;
  bool m_LoadingConversations = false;
};
