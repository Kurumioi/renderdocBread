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
class QButtonGroup;
class QCommandLinkButton;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QProcess;
class QPushButton;
class QTimer;
class QVBoxLayout;
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

  void ReportError(const QString &message);
  void StartCaptureSkill(const QString &prompt, const QString &skill,
                         const QString &focusStage = QString(),
                         const QString &intentKind = QString());
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
  struct ClarificationOption
  {
    QString id;
    QString label;
    QString description;
  };

  struct ClarificationPrompt
  {
    QString id;
    QString question;
    QString selectionMode;
    QVector<ClarificationOption> options;
    bool allowOther = false;
    QString submitLabel;
    // The context which produced this question remains a candidate when the user answers later.
    // The native UI also freezes the then-current state, so AgentHost can resolve the user's
    // intent instead of silently rebinding the follow-up to whichever EID happens to be selected.
    QByteArray originContextSnapshot;
    QString originContextInstanceId;

    bool isValid() const
    {
      return !id.isEmpty() && !question.isEmpty() &&
             (selectionMode == QLatin1String("single") ||
              selectionMode == QLatin1String("multiple")) &&
             options.size() >= 2 && options.size() <= 6;
    }
  };

  struct ClarificationResponse
  {
    QString clarificationId;
    QStringList optionIds;
    QString otherText;
    QByteArray originContextSnapshot;
    QString originContextInstanceId;
    QByteArray currentContextSnapshot;
    QString currentContextInstanceId;

    bool isValid() const
    {
      return !clarificationId.isEmpty() && (!optionIds.isEmpty() || !otherText.isEmpty());
    }
  };

  struct ConversationThread
  {
    QString id;
    QString title;
    qint64 createdMs = 0;
    qint64 updatedMs = 0;
    bool pinned = false;
    bool unread = false;
    QVector<AIChatMessage> messages;
    ClarificationPrompt clarification;
  };

  struct PendingChat
  {
    QString taskId;
    QString messageId;
    QString conversationId;
    QString prompt;
    QString effectivePrompt;
    QString skill;
    QString focusStage;
    QString intentKind;
    QStringList historyExcludeMessageIds;
    QByteArray contextSnapshot;
    QString contextInstanceId;
    ClarificationResponse clarificationResponse;
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
  bool clarificationFromJSON(const QJsonObject &object, const QString &fallbackId,
                             ClarificationPrompt &clarification) const;
  QJsonObject clarificationToJSON(const ClarificationPrompt &clarification) const;
  void bindClarificationOrigin(ClarificationPrompt &clarification, const QString &taskId) const;
  void updateClarificationUI();
  void updateClarificationSubmitState();
  void submitClarification();
  void clearClarification(const QString &conversationId);
  void handleTranscriptLink(const QUrl &url);

  void loadConversations();
  void saveConversations();
  void scheduleConversationSave();
  void resetConversations();
  QJsonObject conversationToJSON(const ConversationThread &thread, int *remainingChars) const;
  bool conversationFromJSON(const QJsonObject &object, ConversationThread &thread) const;

  void updateCaptureStatus();
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

  void refreshConnectionStatus();
  void forceStopProcessesForDestruction();
  bool attachKillOnCloseJob(QProcess *process, void *&jobHandle, QString *error) const;
  void closeKillOnCloseJob(void *&jobHandle) const;

  QString newTaskId() const;
  QString newMessageId() const;
  QString conversationPrompt(const QString &prompt, const QString &conversationId,
                             const QStringList &excludeMessageIds = QStringList(),
                             const ClarificationResponse &clarificationResponse =
                                 ClarificationResponse()) const;
  void startChat(const QString &prompt, const QString &skill = QString(),
                 bool includeConversation = true, const QString &focusStage = QString(),
                 const ClarificationResponse &clarificationResponse = ClarificationResponse(),
                 const QString &intentKind = QString());
  bool freezeChatContext(PendingChat &request, QString *error);
  void clearChatContextFile();
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
  void unwatchTask(const QString &taskId);
  void updateEventTimer();
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
  QFrame *m_ClarificationPanel = NULL;
  QLabel *m_ClarificationQuestion = NULL;
  QWidget *m_ClarificationOptions = NULL;
  QVBoxLayout *m_ClarificationOptionsLayout = NULL;
  QButtonGroup *m_ClarificationButtonGroup = NULL;
  QLineEdit *m_ClarificationOtherEdit = NULL;
  QPushButton *m_ClarificationSubmit = NULL;
  QVector<QCommandLinkButton *> m_ClarificationButtons;
  QString m_VisibleClarificationKey;
  QPlainTextEdit *m_Prompt = NULL;
  QPushButton *m_Send = NULL;
  QPushButton *m_Cancel = NULL;

  QTimer *m_EventTimer = NULL;
  QTimer *m_ConversationSaveTimer = NULL;
  QTimer *m_ConversationRenderTimer = NULL;
  QProcess *m_ChatProcess = NULL;
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
  QString m_ChatContextFile;
  QString m_CaptureConversationPath;
  QByteArray m_ChatStdout;
  QByteArray m_ChatStderr;
  bool m_ProfileComboLoading = false;
  bool m_ConnectionDialogOpen = false;
  bool m_ShuttingDown = false;

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
