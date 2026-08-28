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

#include "AIAssistant.h"

#include <algorithm>
#include <functional>
#include <memory>
#include <cstring>
#include <QApplication>
#include <QBrush>
#include <QButtonGroup>
#include <QCheckBox>
#include <QClipboard>
#include <QColor>
#include <QComboBox>
#include <QCommandLinkButton>
#include <QCoreApplication>
#include <QCursor>
#include <QDateTime>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QGroupBox>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QIcon>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMessageBox>
#include <QPainter>
#include <QPalette>
#include <QPlainTextEdit>
#include <QPointer>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollBar>
#include <QShortcut>
#include <QSignalBlocker>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QStyle>
#include <QTextBrowser>
#include <QTemporaryFile>
#include <QTimer>
#include <QToolButton>
#include <QTreeWidget>
#include <QUrl>
#include <QUuid>
#include <QVBoxLayout>
#include "Code/CaptureContext.h"
#include "Code/QRDUtils.h"
#include "Code/Resources.h"
#include "AIConversationListView.h"
#include "AIConversationStore.h"
#include "AIPendingChatView.h"
#include "Windows/Dialogs/SettingsDialog.h"

#if defined(Q_OS_WIN)
#include <QtCore/qt_windows.h>
#endif

namespace
{
const qint64 MaxManifestBytes = 64 * 1024;
const qint64 MaxEventReadBytes = 256 * 1024;
const int MaxEventLineBytes = 64 * 1024;
const qint64 MaxEventDrainBytes = 16 * 1024 * 1024;
const int MaxAnswerBytes = 2 * 1024 * 1024;
const int MaxProcessOutputBytes = 8 * 1024 * 1024;
const int MaxPromptBytes = 16 * 1024 * 1024;
const int MaxContextSnapshotBytes = 512 * 1024;
const int MaxClarificationOtherChars = 16 * 1024;
const int MaxDiscoveredModels = 200;
const int MaxModelIdBytes = 512;
const int MaxConversationStoreChars = 4 * 1024 * 1024;
const int MaxConversationMessageChars = 256 * 1024;
const int MaxConversationMessages = 200;
const int MaxConversationThreads = 32;
const int MaxPendingChats = 64;

bool isLowerHexIdentifier(const QString &value, int expectedLength)
{
  if(value.size() != expectedLength)
    return false;
  for(const QChar character : value)
  {
    const ushort code = character.unicode();
    if(!((code >= ushort('0') && code <= ushort('9')) ||
         (code >= ushort('a') && code <= ushort('f'))))
      return false;
  }
  return true;
}

bool decodeContextSnapshot(const QByteArray &encoded, QJsonObject *snapshot = NULL)
{
  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(encoded, &parseError);
  const QJsonObject object = document.isObject() ? document.object() : QJsonObject();
  // i18n-ignore: ReplayLab frozen-context protocol field names and schema identifiers.
  const QJsonObject capture = object.value(lit("capture")).toObject();
  const bool valid = parseError.error == QJsonParseError::NoError &&
                     encoded.size() > 0 && encoded.size() <= MaxContextSnapshotBytes &&
                     object.value(lit("schema")).toString() ==    // i18n-ignore: frozen-context protocol key
                         lit("renderdoc-ultra-current-context/v1") &&    // i18n-ignore: frozen-context schema ID
                     isLowerHexIdentifier(object.value(lit("snapshotId")).toString(), 64) &&    // i18n-ignore: frozen-context protocol key
                     capture.value(lit("loaded")).toBool() &&    // i18n-ignore: frozen-context protocol key
                     isLowerHexIdentifier(
                         capture.value(lit("captureSessionId")).toString(), 32);    // i18n-ignore: frozen-context protocol key
  if(valid && snapshot)
    *snapshot = object;
  return valid;
}

QJsonObject contextCandidateJSON(const QByteArray &encoded, const QString &instanceId)
{
  QJsonObject snapshot;
  if(instanceId.trimmed().isEmpty() || instanceId.size() > 160 ||
     !decodeContextSnapshot(encoded, &snapshot))
    return QJsonObject();

  QJsonObject candidate;
  // i18n-ignore: ReplayLab clarification context-candidate wire-protocol fields.
  candidate[lit("contextInstanceId")] = instanceId;
  candidate[lit("snapshot")] = snapshot;    // i18n-ignore: context-candidate protocol key
  return candidate;
}

bool normalizedTerminalStatus(const QJsonObject &object, QString &status)
{
  // i18n-ignore: ReplayLab result/task-event JSON field name defined by the backend protocol.
  if(!object.contains(lit("terminalStatus")))
  {
    // i18n-ignore: Task lifecycle state identifier used only for control flow.
    status = lit("completed");
    return true;
  }

  // i18n-ignore: ReplayLab result/task-event JSON field name defined by the backend protocol.
  const QJsonValue value = object.value(lit("terminalStatus"));
  if(!value.isString())
    return false;
  status = value.toString();
  // i18n-ignore: Task lifecycle state identifiers used only for control flow.
  return status == lit("completed") || status == lit("needs-user-input") ||
         // i18n-ignore: Task lifecycle state identifier used only for control flow.
         status == lit("blocked");
}

QString htmlText(const QString &text)
{
  QString escaped = text.toHtmlEscaped();
  escaped.replace(QLatin1Char('\n'), lit("<br>"));
  return escaped;
}

enum class AssistantActionIcon
{
  Add,
  Send,
  Stop,
};

QIcon assistantActionIcon(const QWidget *widget, AssistantActionIcon kind)
{
  QPixmap pixmap(18, 18);
  pixmap.fill(Qt::transparent);
  QPainter painter(&pixmap);
  painter.setRenderHint(QPainter::Antialiasing, true);
  const QColor color = widget ? widget->palette().color(QPalette::ButtonText)
                              : QApplication::palette().color(QPalette::ButtonText);
  painter.setPen(QPen(color, 1.7, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
  painter.setBrush(Qt::NoBrush);
  if(kind == AssistantActionIcon::Add)
  {
    painter.drawLine(QPointF(4.5, 9.0), QPointF(13.5, 9.0));
    painter.drawLine(QPointF(9.0, 4.5), QPointF(9.0, 13.5));
  }
  else if(kind == AssistantActionIcon::Send)
  {
    QPainterPath arrow;
    arrow.moveTo(4.5, 9.5);
    arrow.lineTo(9.0, 5.0);
    arrow.lineTo(13.5, 9.5);
    painter.drawPath(arrow);
    painter.drawLine(QPointF(9.0, 5.5), QPointF(9.0, 13.5));
  }
  else if(kind == AssistantActionIcon::Stop)
  {
    painter.setPen(Qt::NoPen);
    painter.setBrush(color);
    painter.drawRoundedRect(QRectF(5.0, 5.0, 8.0, 8.0), 1.5, 1.5);
  }
  return QIcon(pixmap);
}

enum class DiagnosticLevel
{
  Info = 0,
  Success = 1,
  Warning = 2,
  Error = 3,
};

DiagnosticLevel diagnosticLevel(const QString &status)
{
  const QString value = status.trimmed().toLower();
  if(value == lit("ok") || value == lit("success") || value == lit("passed") ||
     value == lit("connected") || value == lit("authenticated"))
    return DiagnosticLevel::Success;
  if(value == lit("warning") || value == lit("warn") || value == lit("skipped") ||
     value == lit("skip") || value == lit("unknown") || value == lit("unavailable"))
    return DiagnosticLevel::Warning;
  if(value == lit("error") || value == lit("failed") || value == lit("failure"))
    return DiagnosticLevel::Error;
  return DiagnosticLevel::Info;
}

QString diagnosticTag(DiagnosticLevel level, const QString &rawStatus = QString())
{
  const QString normalized = rawStatus.trimmed().toLower();
  if(normalized == lit("skip") || normalized == lit("skipped"))
    return QCoreApplication::translate("AIAssistant", "SKIPPED");
  switch(level)
  {
    case DiagnosticLevel::Success:
      return QCoreApplication::translate("AIAssistant", "OK");
    case DiagnosticLevel::Warning:
      return QCoreApplication::translate("AIAssistant", "WARNING");
    case DiagnosticLevel::Error:
      return QCoreApplication::translate("AIAssistant", "ERROR");
    default: return QCoreApplication::translate("AIAssistant", "INFO");
  }
}

QColor diagnosticColor(const QWidget *widget, DiagnosticLevel level)
{
  const QPalette palette = widget ? widget->palette() : QApplication::palette();
  const QColor background = palette.color(QPalette::Base);
  const bool dark = background.lightness() < 128;
  switch(level)
  {
    case DiagnosticLevel::Success: return QColor(dark ? lit("#69DBA5") : lit("#147A50"));
    case DiagnosticLevel::Warning: return QColor(dark ? lit("#FFD166") : lit("#8A5800"));
    case DiagnosticLevel::Error: return QColor(dark ? lit("#FF7B7B") : lit("#B3261E"));
    default: return palette.color(QPalette::Text);
  }
}

void applyDiagnosticPalette(QWidget *widget, DiagnosticLevel level)
{
  if(!widget)
    return;
  QPalette palette = widget->palette();
  const QColor color = diagnosticColor(widget, level);
  palette.setColor(QPalette::WindowText, color);
  palette.setColor(QPalette::Text, color);
  widget->setPalette(palette);
}

QString diagnosticStatusText(DiagnosticLevel level, const QString &message,
                             const QString &rawStatus = QString())
{
  return lit("[") + diagnosticTag(level, rawStatus) + lit("] ") + message;
}

void setDiagnosticStatus(QLabel *label, DiagnosticLevel level, const QString &message,
                         const QString &rawStatus = QString())
{
  if(!label)
    return;
  label->setTextFormat(Qt::PlainText);
  applyDiagnosticPalette(label, level);
  const QString text = diagnosticStatusText(level, message, rawStatus);
  label->setText(text);
  label->setAccessibleName(text);
}

void setDiagnosticStatus(QTextBrowser *browser, DiagnosticLevel level, const QString &message,
                         const QString &rawStatus = QString())
{
  if(!browser)
    return;
  applyDiagnosticPalette(browser, level);
  const QString text = diagnosticStatusText(level, message, rawStatus);
  browser->setPlainText(text);
  browser->setAccessibleName(text);
}

bool safeProviderAuthUrl(const QUrl &url)
{
  const QString scheme = url.scheme().toLower();
  const QString host = url.host().toLower();
  if(!url.isValid() || host.isEmpty())
    return false;
  return scheme == lit("https") ||
         (scheme == lit("http") &&
          (host == lit("127.0.0.1") || host == lit("localhost") || host == lit("::1")));
}

struct SubscriptionProcessState
{
  QPointer<QProcess> process;
  QByteArray tail;
  qint64 outputBytes = 0;
  QStringList discoveredModels;
  QString operation;
  QString failureDetail;
  void *job = NULL;
  bool completionHandled = false;
  bool testTerminalSucceeded = false;
  bool operationTerminalSucceeded = false;
};

void setAuthLinkStatus(QTextBrowser *browser, DiagnosticLevel level, const QString &message,
                       const QUrl &authUrl, const QString &deviceCode)
{
  if(!browser)
    return;
  applyDiagnosticPalette(browser, level);
  const QString tag = diagnosticTag(level).toHtmlEscaped();
  const QString color = diagnosticColor(browser, level).name(QColor::HexRgb).toHtmlEscaped();
  QString contents = lit("<div><b><span style=\"color:%1\">[%2]</span></b> %3")
                         .arg(color, tag, htmlText(message));
  if(!deviceCode.isEmpty())
    contents += QCoreApplication::translate("AIAssistant", "<br>Device code: <code>%1</code>")
                    .arg(deviceCode.toHtmlEscaped());
  if(!authUrl.isEmpty())
  {
    const QString encoded = authUrl.toString(QUrl::FullyEncoded);
    contents += QCoreApplication::translate(
                    "AIAssistant", "<br>Authorization URL: <a href=\"%1\">%2</a>")
                    .arg(encoded.toHtmlEscaped(), encoded.toHtmlEscaped());
  }
  contents += lit("</div>");
  browser->setHtml(contents);
  browser->setAccessibleName(
      diagnosticStatusText(level, message) +
      (deviceCode.isEmpty()
           ? QString()
           : QCoreApplication::translate("AIAssistant", " Device code: %1").arg(deviceCode)) +
      (authUrl.isEmpty()
           ? QString()
           : QCoreApplication::translate("AIAssistant", " Authorization URL: %1")
                 .arg(authUrl.toString(QUrl::FullyEncoded))));
}

QIcon diagnosticIcon(QWidget *widget, DiagnosticLevel level)
{
  QStyle *style = widget ? widget->style() : QApplication::style();
  switch(level)
  {
    case DiagnosticLevel::Success: return style->standardIcon(QStyle::SP_DialogApplyButton);
    case DiagnosticLevel::Warning: return style->standardIcon(QStyle::SP_MessageBoxWarning);
    case DiagnosticLevel::Error: return style->standardIcon(QStyle::SP_MessageBoxCritical);
    default: return style->standardIcon(QStyle::SP_MessageBoxInformation);
  }
}

void showConnectionDiagnostics(QWidget *parent, const QString &title, const QJsonDocument &report,
                               int exitCode, const QString &processDetail)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(title);
  dialog.resize(820, 520);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *summary = new QLabel(&dialog);
  summary->setWordWrap(true);
  layout->addWidget(summary);

  QTreeWidget *checks = new QTreeWidget(&dialog);
  checks->setColumnCount(3);
  checks->setHeaderLabels(QStringList() << QCoreApplication::translate("AIAssistant", "Status")
                                        << QCoreApplication::translate("AIAssistant", "Check")
                                        << QCoreApplication::translate("AIAssistant", "Detail"));
  checks->setRootIsDecorated(false);
  checks->setAlternatingRowColors(true);
  checks->setSelectionMode(QAbstractItemView::SingleSelection);
  checks->header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
  checks->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
  checks->header()->setSectionResizeMode(2, QHeaderView::Stretch);
  layout->addWidget(checks, 1);

  QLabel *detailTitle = new QLabel(QCoreApplication::translate(
      "AIAssistant", "Selected detail (plain text; select or copy as needed):"), &dialog);
  layout->addWidget(detailTitle);
  QTextBrowser *detail = new QTextBrowser(&dialog);
  detail->setOpenExternalLinks(false);
  detail->setMaximumHeight(120);
  layout->addWidget(detail);

  QStringList copyLines;
  DiagnosticLevel overall = DiagnosticLevel::Info;
  bool hasRows = false;
  auto addRow = [&](DiagnosticLevel level, const QString &rawStatus, const QString &name,
                    const QString &message) {
    hasRows = true;
    if(int(level) > int(overall))
      overall = level;
    const QString tag = diagnosticTag(level, rawStatus);
    QTreeWidgetItem *item = new QTreeWidgetItem(checks);
    item->setText(0, tag);
    item->setText(1, name);
    item->setText(2, message);
    item->setIcon(0, diagnosticIcon(checks, level));
    const QBrush brush(diagnosticColor(checks, level));
    item->setForeground(0, brush);
    item->setForeground(1, brush);
    item->setToolTip(2, message);
    item->setData(2, Qt::UserRole, message);
    copyLines << lit("[") + tag + lit("] ") + name + lit(" - ") + message;
  };

  if(report.isArray())
  {
    for(const QJsonValue &entry : report.array())
    {
      if(!entry.isObject())
      {
        addRow(DiagnosticLevel::Warning, lit("warning"),
               QCoreApplication::translate("AIAssistant", "Diagnostic record"),
               QCoreApplication::translate("AIAssistant", "The host returned a non-object row."));
        continue;
      }
      const QJsonObject item = entry.toObject();
      const QString rawStatus = item.value(lit("status")).toString();
      addRow(diagnosticLevel(rawStatus), rawStatus,
             item.value(lit("name")).toString(
                 QCoreApplication::translate("AIAssistant", "Unnamed check")),
             item.value(lit("detail")).toString(
                 QCoreApplication::translate("AIAssistant", "No detail was returned.")));
    }
  }
  else
  {
    addRow(DiagnosticLevel::Error, lit("error"),
           QCoreApplication::translate("AIAssistant", "Diagnostic output"),
           processDetail.isEmpty()
               ? QCoreApplication::translate("AIAssistant",
                                             "The host returned invalid diagnostic data.")
               : processDetail);
  }

  if(!processDetail.isEmpty() && report.isArray())
    addRow(exitCode == 0 ? DiagnosticLevel::Warning : DiagnosticLevel::Error,
           exitCode == 0 ? lit("warning") : lit("error"),
           QCoreApplication::translate("AIAssistant", "Process detail"), processDetail);
  if(exitCode != 0 && overall != DiagnosticLevel::Error)
    addRow(DiagnosticLevel::Error, lit("error"),
           QCoreApplication::translate("AIAssistant", "Process result"),
           QCoreApplication::translate("AIAssistant",
                                       "The diagnostic command did not complete successfully."));
  if(!hasRows)
    addRow(DiagnosticLevel::Warning, lit("warning"),
           QCoreApplication::translate("AIAssistant", "Connection check"),
           QCoreApplication::translate("AIAssistant", "No diagnostic details were returned."));

  QString summaryMessage;
  if(overall == DiagnosticLevel::Error)
    summaryMessage = QCoreApplication::translate("AIAssistant",
                                                 "One or more checks failed. Review the details.");
  else if(overall == DiagnosticLevel::Warning)
    summaryMessage = QCoreApplication::translate(
        "AIAssistant", "Checks completed with warnings or skipped items.");
  else if(overall == DiagnosticLevel::Success)
    summaryMessage = QCoreApplication::translate("AIAssistant", "All reported checks passed.");
  else
    summaryMessage = QCoreApplication::translate("AIAssistant",
                                                 "Diagnostic information was returned.");
  setDiagnosticStatus(summary, overall, summaryMessage);
  dialog.setWindowIcon(diagnosticIcon(&dialog, overall));

  QObject::connect(checks, &QTreeWidget::currentItemChanged, &dialog,
                   [detail](QTreeWidgetItem *current, QTreeWidgetItem *) {
                     detail->setPlainText(current ? current->data(2, Qt::UserRole).toString()
                                                  : QString());
                   });
  if(checks->topLevelItemCount() > 0)
    checks->setCurrentItem(checks->topLevelItem(0));

  QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Close, &dialog);
  QPushButton *copy = buttons->addButton(
      QCoreApplication::translate("AIAssistant", "Copy All Details"),
      QDialogButtonBox::ActionRole);
  QObject::connect(copy, &QPushButton::clicked, &dialog,
                   [copyLines]() { QApplication::clipboard()->setText(copyLines.join(lit("\n"))); });
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  layout->addWidget(buttons);
  dialog.exec();
}

bool safeTaskId(const QString &taskId)
{
  static const QRegularExpression valid(lit("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));
  return valid.match(taskId).hasMatch();
}

QString environmentValue(const char *name)
{
  // qgetenv() exposes a narrow QByteArray and QFile::decodeName() follows the
  // process locale. On Windows that can lose a Chinese user/profile or CLI
  // installation path before QProcess ever gets a chance to use CreateProcessW.
  // QProcessEnvironment reads the native Unicode environment and preserves the
  // exact QString value end to end.
  return QProcessEnvironment::systemEnvironment().value(QString::fromLatin1(name));
}

QSet<QString> knownModelCredentialEnvironments()
{
  // Deny known provider credentials even when the user has not configured that provider in
  // ReplayLab. A managed subscription process must never inherit an unrelated model API key
  // merely because qrenderdoc was launched from a developer shell containing it.
  static const char *const known[] = {
      "OPENAI_API_KEY",       "ANTHROPIC_API_KEY",  "GOOGLE_API_KEY",
      "GEMINI_API_KEY",      "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY",
      "XAI_API_KEY",          "MISTRAL_API_KEY",    "GROQ_API_KEY",
      "DEEPSEEK_API_KEY",    "DASHSCOPE_API_KEY",  "MOONSHOT_API_KEY",
      "MINIMAX_API_KEY",     "ZHIPUAI_API_KEY",    "ARK_API_KEY",
      "HUNYUAN_API_KEY",     "QIANFAN_API_KEY",    "SILICONFLOW_API_KEY",
      "REPLAYLAB_CUSTOM_API_KEY",
  };
  QSet<QString> result;
  for(const char *name : known)
    result.insert(QString::fromLatin1(name));
  return result;
}

QJsonDocument parseJSONOrLastJSONLRecord(const QByteArray &output)
{
  const QByteArray trimmed = output.trimmed();
  QJsonParseError parseError;
  QJsonDocument document = QJsonDocument::fromJson(trimmed, &parseError);
  if(parseError.error == QJsonParseError::NoError && !document.isNull())
    return document;

  // Machine commands normally return one JSON value, while long-running subscription commands
  // use JSONL. Accept the last complete record for status-style calls so a backend may add
  // progress records without forcing qrenderdoc to special-case the transport.
  const QList<QByteArray> lines = trimmed.split('\n');
  for(int i = lines.count() - 1; i >= 0; --i)
  {
    const QByteArray line = lines[i].trimmed();
    if(line.isEmpty())
      continue;
    document = QJsonDocument::fromJson(line, &parseError);
    if(parseError.error == QJsonParseError::NoError && !document.isNull())
      return document;
  }
  return QJsonDocument();
}

void deleteProcessWhenFinished(QProcess *process)
{
  QObject::connect(
      process, static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
      process, &QObject::deleteLater);
}

class AIPromptEdit : public QPlainTextEdit
{
public:
  explicit AIPromptEdit(QWidget *parent = NULL) : QPlainTextEdit(parent) {}

  std::function<void()> send;

protected:
  void keyPressEvent(QKeyEvent *event) override
  {
    if((event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter) &&
       !(event->modifiers() & Qt::ShiftModifier))
    {
      if(send)
        send();
      event->accept();
      return;
    }
    QPlainTextEdit::keyPressEvent(event);
  }
};

struct ProviderCatalogText
{
  const char *source;
  const char *disambiguation;
};

struct ProviderCatalogTranslation
{
  const char *presetId;
  const char *field;
  ProviderCatalogText text;
};

struct ProviderRuntimeTranslation
{
  const char *messageId;
  ProviderCatalogText text;
};

// ReplayLab's provider catalog is runtime JSON, so lupdate cannot discover it on its own. Keep an
// exact (preset ID, field, English source) registry here: known catalog text is translated, while
// changed or third-party text safely falls back to the backend value instead of receiving a stale
// translation intended for a different message.
static const ProviderCatalogTranslation providerCatalogTranslations[] = {
    {"openai", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "OpenAI API", "provider.openai.title")},
    {"anthropic", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Anthropic API", "provider.anthropic.title")},
    {"gemini", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Google Gemini API", "provider.gemini.title")},
    {"vertex", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Google Cloud / Vertex AI", "provider.vertex.title")},
    {"azure-openai", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Azure OpenAI", "provider.azure-openai.title")},
    {"openrouter", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "OpenRouter", "provider.openrouter.title")},
    {"xai", "title", QT_TRANSLATE_NOOP3("AIAssistant", "xAI / Grok", "provider.xai.title")},
    {"mistral", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Mistral AI", "provider.mistral.title")},
    {"groq", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "GroqCloud", "provider.groq.title")},
    {"deepseek", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "DeepSeek", "provider.deepseek.title")},
    {"qwen", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Alibaba Model Studio / Qwen", "provider.qwen.title")},
    {"qwen-coding-plan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Alibaba Model Studio Coding Plan",
                        "provider.qwen-coding-plan.title")},
    {"kimi", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Moonshot / Kimi", "provider.kimi.title")},
    {"minimax", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "MiniMax", "provider.minimax.title")},
    {"glm", "title", QT_TRANSLATE_NOOP3("AIAssistant", "Zhipu GLM", "provider.glm.title")},
    {"doubao", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Volcengine Ark / Doubao", "provider.doubao.title")},
    {"hunyuan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Tencent Hunyuan / TokenHub",
                        "provider.hunyuan.title")},
    {"qianfan", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Baidu Qianfan", "provider.qianfan.title")},
    {"siliconflow", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "SiliconFlow", "provider.siliconflow.title")},
    {"ollama", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Ollama (local)", "provider.ollama.title")},
    {"custom-openai", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Custom OpenAI-compatible endpoint",
                        "provider.custom-openai.title")},
    {"codex", "title",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex / ChatGPT subscription", "provider.codex.title")},

    {"azure-openai", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The profile model is the Azure deployment name. Legacy endpoints require api_version; "
         "Azure v1/Foundry endpoints must omit it.",
         "provider.azure-openai.note")},
    {"groq", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "OpenAI compatibility is partial; run the connection probe for the chosen model.",
         "provider.groq.note")},
    {"qwen", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Standard API keys and Coding Plan keys use their documented endpoints.",
         "provider.qwen.note")},
    {"qwen-coding-plan", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "The Coding Plan is a subscription key with a dedicated endpoint, not an OAuth login. "
         "Use the international endpoint as a custom base URL when applicable.",
         "provider.qwen-coding-plan.note")},
    {"minimax", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Supports metered keys and official Token Plan keys; it is not a browser login.",
         "provider.minimax.note")},
    {"custom-openai", "note",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Capabilities are unknown until probed; no feature is inferred from the name.",
         "provider.custom-openai.note")},
    {"codex", "install_hint",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Install the official Codex CLI: "
                        "https://developers.openai.com/codex/cli/",
                        "provider.codex.install_hint")},
};

// Subscription events are backend protocol data. Translate only stable message IDs; the raw
// English text remains a diagnostic fallback for unknown/third-party messages.
static const ProviderRuntimeTranslation providerRuntimeTranslations[] = {
    {"subscription.cli-detected",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "%1 CLI detected; sign-in state is checked only after approval.",
         "subscription.cli-detected")},
    {"subscription.cli-missing",
     QT_TRANSLATE_NOOP3("AIAssistant", "Required provider CLI was not found: %1\n%2",
                        "subscription.cli-missing")},
    {"subscription.waiting-for-containment",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Waiting for qrenderdoc process containment before starting the provider.",
         "subscription.waiting-for-containment")},
    {"subscription.sign-in-started",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Provider sign-in started inside the qrenderdoc process job.",
         "subscription.sign-in-started")},
    {"subscription.test-started",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Running a bounded no-generation provider connection test.",
         "subscription.test-started")},
    {"subscription.browser-waiting",
     QT_TRANSLATE_NOOP3(
         "AIAssistant", "Waiting for %1 browser/device authorization.",
         "subscription.browser-waiting")},
    {"subscription.browser-complete",
     QT_TRANSLATE_NOOP3("AIAssistant", "Complete provider authorization in the browser.",
                        "subscription.browser-complete")},
    {"subscription.cancelled-before-start",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Subscription operation cancelled before provider startup.",
                        "subscription.cancelled-before-start")},
    {"subscription.start-gate-closed",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Subscription start gate closed before 'start'; no provider process was created.",
         "subscription.start-gate-closed")},
    {"subscription.custom-login-skipped",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "Custom ACP has no guessed login command; running only the configured handshake test.",
         "subscription.custom-login-skipped")},
    {"subscription.acp-test-models",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "ACP initialize/session handshake succeeded without sending a prompt; the session "
         "exposed %1 model choice(s).",
         "subscription.acp-test-models")},
    {"subscription.acp-test-no-models",
     QT_TRANSLATE_NOOP3(
         "AIAssistant",
         "ACP initialize/session handshake succeeded without sending a prompt. This Agent did "
         "not expose a standard model list; leave the model unset to use its configured default, "
         "or enter an exact model only if that CLI supports ACP model selection.",
         "subscription.acp-test-no-models")},
    {"subscription.acp-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1 login command completed; testing ACP now.",
                        "subscription.acp-login-completed")},
    {"subscription.terminal-opened",
     QT_TRANSLATE_NOOP3("AIAssistant",
                        "Complete authentication in the %1 terminal, then exit it.",
                        "subscription.terminal-opened")},
    {"subscription.terminal-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "%1 terminal exited; testing ACP now.",
                        "subscription.terminal-login-completed")},
    {"subscription.codex-signed-out",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex is signed out.",
                        "subscription.codex-signed-out")},
    {"subscription.codex-account-ready",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex account and model catalog are available.",
                        "subscription.codex-account-ready")},
    {"subscription.codex-browser-complete",
     QT_TRANSLATE_NOOP3("AIAssistant", "Complete Codex sign-in in the browser.",
                        "subscription.codex-browser-complete")},
    {"subscription.codex-login-completed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex login completed.",
                        "subscription.codex-login-completed")},
    {"subscription.codex-login-failed",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex login failed.",
                        "subscription.codex-login-failed")},
    {"subscription.codex-account-unauthenticated",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex account is not authenticated.",
                        "subscription.codex-account-unauthenticated")},
    {"subscription.codex-sign-in-test-succeeded",
     QT_TRANSLATE_NOOP3("AIAssistant", "Codex sign-in and model catalog test succeeded.",
                        "subscription.codex-sign-in-test-succeeded")},
};

QString localizeProviderCatalogText(const QString &presetId, const QString &field,
                                    const QString &source)
{
  if(presetId.isEmpty() || source.isEmpty())
    return source;

  for(const ProviderCatalogTranslation &entry : providerCatalogTranslations)
  {
    if(presetId == QLatin1String(entry.presetId) && field == QLatin1String(entry.field) &&
       source == QLatin1String(entry.text.source))
      return QCoreApplication::translate("AIAssistant", entry.text.source,
                                         entry.text.disambiguation);
  }

  return source;
}

QString localizeProviderCatalogField(const QJsonObject &preset, const char *field)
{
  // i18n-ignore: ReplayLab provider-catalog JSON field name used only for lookup.
  const QString presetIdKey = lit("id");
  const QString fieldName = QString::fromLatin1(field);
  // The qrenderdoc connection UI intentionally exposes model APIs and Codex subscription only.
  // Hide catalog notes whose sole purpose was to explain why another subscription CLI is absent.
  // i18n-ignore: provider catalog field and preset identifiers used only for filtering.
  if(fieldName == lit("note") && preset.value(presetIdKey).toString() == lit("anthropic"))
    return QString();
  return localizeProviderCatalogText(preset.value(presetIdKey).toString(), fieldName,
                                     preset.value(fieldName).toString());
}

QString localizeProviderRuntimeText(const QString &presetId, const QString &source)
{
  if(presetId.isEmpty() || source.isEmpty())
    return source;

  for(const ProviderCatalogTranslation &entry : providerCatalogTranslations)
  {
    if(presetId == QLatin1String(entry.presetId) && source == QLatin1String(entry.text.source))
      return QCoreApplication::translate("AIAssistant", entry.text.source,
                                         entry.text.disambiguation);
  }

  return source;
}

QString localizeProviderRuntimeMessage(const QString &presetId, const QString &messageId,
                                       const QJsonArray &arguments, const QString &fallback)
{
  if(!messageId.isEmpty())
  {
    for(const ProviderRuntimeTranslation &entry : providerRuntimeTranslations)
    {
      if(messageId != QLatin1String(entry.messageId))
        continue;
      QString result = QCoreApplication::translate("AIAssistant", entry.text.source,
                                                   entry.text.disambiguation);
      const int count = qMin(arguments.size(), 8);
      for(int i = 0; i < count; ++i)
      {
        QString value = arguments.at(i).toVariant().toString().left(512);
        // i18n-ignore: stable ReplayLab runtime-message protocol ID.
        if(messageId == QLatin1String("subscription.cli-missing") && i == 1)
          // i18n-ignore: provider-catalog field identifier used only for localized lookup.
          value = localizeProviderCatalogText(presetId, lit("install_hint"), value);
        result = result.arg(value);
      }
      return result;
    }
  }
  return localizeProviderRuntimeText(presetId, fallback);
}

QString connectionDisplayName(const QJsonObject &row)
{
  const QString title = row.value(lit("title")).toString().trimmed();
  return title.isEmpty() ? row.value(lit("id")).toString() : title;
}

bool supportedQRenderDocConnection(const QJsonObject &object)
{
  // i18n-ignore: ReplayLab provider/profile JSON field name.
  QString presetId = object.value(lit("preset")).toString();
  if(presetId.isEmpty())
    // i18n-ignore: ReplayLab provider-catalog JSON field name.
    presetId = object.value(lit("id")).toString();
  // i18n-ignore: ReplayLab provider kind and preset identifiers used only for filtering.
  const QString kind = object.value(lit("kind")).toString();
  // i18n-ignore: ReplayLab provider protocol field and identifier used only for filtering.
  const QString protocol = object.value(lit("protocol")).toString();
  const QString modelKind = lit("model");    // i18n-ignore: provider kind identifier.
  const QString agentKind = lit("agent");    // i18n-ignore: provider kind identifier.
  const QString codexPreset = lit("codex");    // i18n-ignore: provider preset identifier.
  const QString codexProtocol =
      lit("codex-app-server");    // i18n-ignore: provider protocol identifier.
  return kind == modelKind ||
         (kind == agentKind && presetId == codexPreset && protocol == codexProtocol);
}

QString internalConnectionId()
{
  QString uuid = QUuid::createUuid().toString();
  uuid.remove(QLatin1Char('{'));
  uuid.remove(QLatin1Char('}'));
  return lit("connection-") + uuid;
}
}

AIAssistant::AIAssistant(CaptureContext &ctx, QWidget *parent) : QFrame(parent), m_Ctx(ctx)
{
  setWindowTitle(tr("RenderDoc AI"));
  setMinimumSize(QSize(700, 460));

  QVBoxLayout *root = new QVBoxLayout(this);
  root->setContentsMargins(0, 0, 0, 0);
  root->setSpacing(0);

  QWidget *mainPanel = new QWidget(this);
  root->addWidget(mainPanel);
  QPalette mainPalette = mainPanel->palette();
  mainPalette.setColor(QPalette::Window, mainPalette.color(QPalette::Base));
  mainPanel->setPalette(mainPalette);
  mainPanel->setAutoFillBackground(true);
  QVBoxLayout *mainLayout = new QVBoxLayout(mainPanel);
  mainLayout->setContentsMargins(7, 5, 7, 6);
  mainLayout->setSpacing(4);

  QFrame *conversationBar = new QFrame(mainPanel);
  conversationBar->setFrameShape(QFrame::NoFrame);
  // i18n-ignore: QObject name is an internal stylesheet selector, not user-visible text.
  conversationBar->setObjectName(lit("aiConversationBar"));
  conversationBar->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QFrame#aiConversationBar { border: none; border-bottom: 1px solid palette(mid); }"));
  QHBoxLayout *conversationBarLayout = new QHBoxLayout(conversationBar);
  conversationBarLayout->setContentsMargins(0, 0, 0, 3);
  conversationBarLayout->setSpacing(3);
  m_ConversationList = new AIConversationListView(conversationBar);
  conversationBarLayout->addWidget(m_ConversationList, 1);
  QPushButton *newConversation = new QPushButton(conversationBar);
  newConversation->setFlat(true);
  newConversation->setFixedSize(30, 30);
  newConversation->setIcon(assistantActionIcon(newConversation, AssistantActionIcon::Add));
  newConversation->setToolTip(tr("New conversation"));
  newConversation->setAccessibleName(tr("New conversation"));
  conversationBarLayout->addWidget(newConversation, 0, Qt::AlignTop);
  mainLayout->addWidget(conversationBar);

  QHBoxLayout *connectionRow = new QHBoxLayout();
  m_Status = new QLabel(this);
  setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Starting..."));
  m_Status->setMinimumWidth(110);
  connectionRow->addWidget(m_Status);
  connectionRow->addStretch(1);
  QToolButton *connections = new QToolButton(this);
  connections->setAutoRaise(true);
  connections->setFixedSize(30, 30);
  connections->setIcon(Icons::cog());
  connections->setToolTip(tr("AI Settings"));
  connections->setAccessibleName(tr("AI Settings"));
  connectionRow->addWidget(connections);
  mainLayout->addLayout(connectionRow);

  m_Context = new QLabel(this);
  m_Context->setTextInteractionFlags(Qt::TextSelectableByMouse);
  mainLayout->addWidget(m_Context);

  QHBoxLayout *skills = new QHBoxLayout();
  QPushButton *explain = new QPushButton(tr("Explain Event"), this);
  QPushButton *pixel = new QPushButton(tr("Inspect Pixel"), this);
  QPushButton *shader = new QPushButton(tr("Explain Shader"), this);
  QPushButton *performance = new QPushButton(tr("Performance"), this);
  skills->addWidget(explain);
  skills->addWidget(pixel);
  skills->addWidget(shader);
  skills->addWidget(performance);
  skills->addStretch(1);
  mainLayout->addLayout(skills);

  m_Transcript = new AIConversationView(mainPanel);
  m_Transcript->setLinkHandler([this](const QUrl &url) { handleTranscriptLink(url); });
  m_Transcript->setActivityToggleHandler([this](const QString &messageId, bool expanded) {
    ConversationThread *thread = conversation();
    if(!thread)
      return;
    for(AIChatMessage &item : thread->messages)
    {
      if(item.id == messageId)
      {
        item.activityExpanded = expanded;
        saveConversations();
        break;
      }
    }
  });
  mainLayout->addWidget(m_Transcript, 1);

  QFrame *composer = new QFrame(mainPanel);
  composer->setFrameShape(QFrame::StyledPanel);
  QVBoxLayout *composerLayout = new QVBoxLayout(composer);
  composerLayout->setContentsMargins(8, 7, 8, 7);
  composerLayout->setSpacing(5);
  m_PendingChats = new AIPendingChatView(composer);
  m_PendingChats->setAdjustDirectionHandler(
      [this](const QString &taskId) { adjustQueuedChat(taskId); });
  m_PendingChats->setEditHandler([this](const QString &taskId) { editQueuedChat(taskId); });
  m_PendingChats->setDeleteHandler(
      [this](const QString &taskId) { deleteQueuedChat(taskId); });
  composerLayout->addWidget(m_PendingChats);

  m_ClarificationPanel = new QFrame(composer);
  m_ClarificationPanel->setFrameShape(QFrame::StyledPanel);
  // i18n-ignore: QObject name is an internal stylesheet selector, not user-visible text.
  m_ClarificationPanel->setObjectName(lit("aiClarificationPanel"));
  m_ClarificationPanel->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QFrame#aiClarificationPanel { border: 1px solid palette(mid); "
          "border-radius: 6px; background: palette(alternate-base); }"));
  QVBoxLayout *clarificationLayout = new QVBoxLayout(m_ClarificationPanel);
  clarificationLayout->setContentsMargins(8, 7, 8, 7);
  clarificationLayout->setSpacing(5);
  m_ClarificationQuestion = new QLabel(m_ClarificationPanel);
  m_ClarificationQuestion->setWordWrap(true);
  m_ClarificationQuestion->setTextInteractionFlags(Qt::TextSelectableByMouse);
  clarificationLayout->addWidget(m_ClarificationQuestion);
  m_ClarificationOptions = new QWidget(m_ClarificationPanel);
  m_ClarificationOptionsLayout = new QVBoxLayout(m_ClarificationOptions);
  m_ClarificationOptionsLayout->setContentsMargins(0, 0, 0, 0);
  m_ClarificationOptionsLayout->setSpacing(3);
  clarificationLayout->addWidget(m_ClarificationOptions);
  QHBoxLayout *clarificationSubmitRow = new QHBoxLayout();
  clarificationSubmitRow->setContentsMargins(0, 0, 0, 0);
  clarificationSubmitRow->addStretch(1);
  m_ClarificationSubmit = new QPushButton(tr("Continue"), m_ClarificationPanel);
  m_ClarificationSubmit->setEnabled(false);
  clarificationSubmitRow->addWidget(m_ClarificationSubmit);
  clarificationLayout->addLayout(clarificationSubmitRow);
  m_ClarificationPanel->setVisible(false);
  composerLayout->addWidget(m_ClarificationPanel);

  AIPromptEdit *promptEdit = new AIPromptEdit(composer);
  m_Prompt = promptEdit;
  m_Prompt->setMaximumBlockCount(200);
  m_Prompt->setMaximumHeight(100);
  m_Prompt->setFrameStyle(QFrame::NoFrame);
  m_Prompt->setPlaceholderText(
      tr("Ask RenderDoc AI... (Enter sends; Shift+Enter inserts a new line)"));
  composerLayout->addWidget(m_Prompt);

  QHBoxLayout *sendRow = new QHBoxLayout();
  m_ProfileCombo = new QComboBox(this);
  m_ProfileCombo->setToolTip(tr("AI connection"));
  m_ProfileCombo->setMinimumContentsLength(12);
  m_ProfileCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  m_ModelCombo = new QComboBox(this);
  m_ModelCombo->setToolTip(tr("Model used for this request"));
  m_ModelCombo->setMinimumContentsLength(12);
  m_ModelCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
  m_QueueStatus = new QLabel(this);
  m_QueueStatus->setTextInteractionFlags(Qt::TextSelectableByMouse);
  m_Cancel = new QPushButton(composer);
  m_Cancel->setIcon(assistantActionIcon(m_Cancel, AssistantActionIcon::Stop));
  m_Cancel->setToolTip(tr("Stop the active chat request."));
  m_Cancel->setAccessibleName(tr("Stop"));
  m_Cancel->setFixedSize(36, 36);
  m_Cancel->setFlat(true);
  m_Cancel->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QPushButton { border: 1px solid palette(mid); border-radius: 18px; "
          "background: transparent; } QPushButton:hover { background: palette(midlight); }"));
  m_Cancel->setVisible(false);
  m_Send = new QPushButton(composer);
  m_Send->setToolTip(tr("Send"));
  m_Send->setAccessibleName(tr("Send"));
  m_Send->setFixedSize(36, 36);
  m_Send->setFlat(true);
  QPalette sendPalette = m_Send->palette();
  sendPalette.setColor(QPalette::ButtonText, sendPalette.color(QPalette::HighlightedText));
  m_Send->setPalette(sendPalette);
  m_Send->setIcon(assistantActionIcon(m_Send, AssistantActionIcon::Send));
  m_Send->setStyleSheet(
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QPushButton { border: none; border-radius: 18px; "
          "background: palette(highlight); color: palette(highlighted-text); } "
          "QPushButton:hover { background: palette(link); } "
          "QPushButton:disabled { background: palette(mid); }"));
  sendRow->setContentsMargins(0, 0, 0, 0);
  sendRow->setSpacing(6);
  sendRow->addWidget(m_QueueStatus, 0, Qt::AlignBottom);
  sendRow->addStretch(1);
  sendRow->addWidget(m_ProfileCombo, 0, Qt::AlignBottom);
  sendRow->addWidget(m_ModelCombo, 0, Qt::AlignBottom);
  sendRow->addWidget(m_Cancel, 0, Qt::AlignBottom);
  sendRow->addWidget(m_Send, 0, Qt::AlignBottom);
  composerLayout->addLayout(sendRow);
  mainLayout->addWidget(composer);

  connect(connections, &QToolButton::clicked, this, [this]() { manageConnections(); });
  connect(m_ProfileCombo, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
          this, [this](int index) { selectProfile(index); });
  connect(m_ModelCombo, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
          this, [this](int index) { selectModel(index); });
  connect(explain, &QPushButton::clicked, this, [this]() {
    StartCaptureSkill(tr("Explain current event"),
                      lit("renderdoc-explain-event"),    // i18n-ignore: internal Skill identifier
                      QString(),
                      lit("explain-current-event"));    // i18n-ignore: stable UI intent identifier
  });
  connect(pixel, &QPushButton::clicked, this, [this]() {
    StartCaptureSkill(tr("Inspect picked pixel"),
                      lit("renderdoc-explain-event"),    // i18n-ignore: internal Skill identifier
                      QString(),
                      lit("inspect-picked-pixel"));    // i18n-ignore: stable UI intent identifier
  });
  connect(shader, &QPushButton::clicked, this,
          [this, shader]() { showShaderIntentMenu(shader); });
  connect(performance, &QPushButton::clicked, this, [this]() {
    StartCaptureSkill(
        tr("Diagnose the current frame's GPU performance using capture evidence."),
        lit("renderdoc-explain-event"));    // i18n-ignore: internal Skill identifier
  });
  connect(newConversation, &QPushButton::clicked, this, [this]() { createConversation(); });
  connect(m_ClarificationSubmit, &QPushButton::clicked, this,
          [this]() { submitClarification(); });
  m_ConversationList->setSelectHandler(
      [this](const QString &conversationId) { selectConversation(conversationId); });
  m_ConversationList->setPinHandler(
      [this](const QString &conversationId, bool) { toggleConversationPinned(conversationId); });
  m_ConversationList->setDeleteHandler(
      [this](const QString &conversationId) { deleteConversation(conversationId); });
  connect(m_Send, &QPushButton::clicked, this, [this]() {
    const QString prompt = m_Prompt->toPlainText().trimmed();
    if(!prompt.isEmpty())
    {
      m_Prompt->clear();
      startChat(prompt);
    }
  });
  promptEdit->send = [this]() { m_Send->click(); };
  connect(m_Cancel, &QPushButton::clicked, this,
          [this]() { cancelChat(lit("cancelled"), true); });

  m_EventTimer = new QTimer(this);
  m_EventTimer->setInterval(250);
  connect(m_EventTimer, &QTimer::timeout, this, [this]() { pollTaskEvents(); });

  m_ConversationSaveTimer = new QTimer(this);
  m_ConversationSaveTimer->setSingleShot(true);
  connect(m_ConversationSaveTimer, &QTimer::timeout, this, [this]() { saveConversations(); });

  m_ConversationRenderTimer = new QTimer(this);
  m_ConversationRenderTimer->setSingleShot(true);
  m_ConversationRenderTimer->setInterval(60);
  connect(m_ConversationRenderTimer, &QTimer::timeout, this,
          [this]() { renderConversation(); });

  const bool captureWasLoaded = m_Ctx.IsCaptureLoaded();
  m_CLIPath = findAgentCLI();
  refreshProfiles();
  updateCaptureStatus();
  m_Ctx.AddCaptureViewer(this);
  if(!captureWasLoaded)
    loadConversations();
  updateComposerState();

  if(m_CLIPath.isEmpty())
    appendSystem(tr("AI host not found. Set REPLAYLAB_AI_CLI or install the bundled ReplayLab host."),
                 true);
  else
    appendSystem(tr("RenderDoc AI is embedded here. Provider processes remain isolated from replay."));
}

AIAssistant::~AIAssistant()
{
  Shutdown();
  forceStopProcessesForDestruction();
  m_Ctx.RemoveCaptureViewer(this);
  m_Ctx.BuiltinWindowClosed(this);
}

void AIAssistant::Shutdown()
{
  if(m_ShuttingDown)
    return;
  m_ShuttingDown = true;
  if(m_EventTimer)
    m_EventTimer->stop();
  if(m_ConversationRenderTimer)
    m_ConversationRenderTimer->stop();
  // CaptureContext tears down the bridge extensions immediately after this returns. Stop the
  // active per-task process now so no callback can race extension destruction.
  forceStopProcessesForDestruction();
  cancelChat(QString(), false);
  saveConversations();
}

void AIAssistant::appendSystem(const QString &text, bool error)
{
  ConversationThread *thread = conversation();
  if(!thread)
    return;
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("system");
  item.text = text;
  item.state = error ? lit("error") : lit("status");
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  if(thread->id == m_ActiveConversationId)
    thread->unread = false;
  renderConversation();
}

QString AIAssistant::appendUser(const QString &text, const QString &taskId, const QString &state,
                                const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return QString();
  // Any accepted user turn resolves the currently displayed clarification. The prompt still
  // travels through startChat/StartCaptureSkill so its RenderDoc context is frozen normally.
  clearClarification(thread->id);
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("user");
  item.text = text;
  item.taskId = taskId;
  item.state = state.isEmpty() ? lit("queued") : state;
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  item.activityExpanded = item.state == lit("queued") || item.state == lit("running");
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  if(thread->id == m_ActiveConversationId)
    thread->unread = false;
  if(thread->title == tr("New conversation") || thread->title.trimmed().isEmpty())
  {
    QString title = text.simplified();
    if(title.size() > 52)
      title = title.left(49) + lit("...");
    if(!title.isEmpty())
      thread->title = title;
  }
  renderConversation(true);
  saveConversations();
  return item.id;
}

void AIAssistant::appendAssistant(const QString &text, const QString &conversationId,
                                  const QString &taskId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  AIChatMessage item;
  item.id = newMessageId();
  item.role = lit("assistant");
  item.text = text;
  item.taskId = taskId;
  item.state = lit("completed");
  item.timestampMs = QDateTime::currentMSecsSinceEpoch();
  item.finishedTimestampMs = item.timestampMs;
  thread->messages.push_back(item);
  thread->updatedMs = item.timestampMs;
  thread->unread = thread->id != m_ActiveConversationId;
  renderConversation(true);
  scheduleConversationSave();
}

AIAssistant::ConversationThread *AIAssistant::conversation(const QString &conversationId)
{
  const QString selected = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  for(ConversationThread &thread : m_Conversations)
    if(thread.id == selected)
      return &thread;
  return NULL;
}

const AIAssistant::ConversationThread *AIAssistant::conversation(
    const QString &conversationId) const
{
  const QString selected = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  for(const ConversationThread &thread : m_Conversations)
    if(thread.id == selected)
      return &thread;
  return NULL;
}

AIChatMessage *AIAssistant::message(const QString &conversationId, const QString &messageId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return NULL;
  for(AIChatMessage &item : thread->messages)
    if(item.id == messageId)
      return &item;
  return NULL;
}

AIChatMessage *AIAssistant::taskMessage(const QString &taskId)
{
  return message(m_TaskConversationIds.value(taskId), m_TaskMessageIds.value(taskId));
}

void AIAssistant::renderConversation(bool refreshList)
{
  if(m_ConversationRenderTimer && m_ConversationRenderTimer->isActive())
    m_ConversationRenderTimer->stop();
  if(refreshList)
    refreshConversationList();
  const ConversationThread *thread = conversation();
  if(m_Transcript)
  {
    if(thread)
      m_Transcript->setMessages(thread->messages);
    else
      m_Transcript->clear();
  }
  updateComposerState();
}

void AIAssistant::refreshConversationList()
{
  if(!m_ConversationList)
    return;

  QSet<QString> runningConversationIds;
  for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
    if(m_RunningTasks.contains(it.key()))
      runningConversationIds.insert(it.value());

  QVector<const ConversationThread *> ordered;
  ordered.reserve(m_Conversations.size());
  for(const ConversationThread &thread : m_Conversations)
    ordered.push_back(&thread);
  std::stable_sort(ordered.begin(), ordered.end(),
                   [&runningConversationIds,
                    this](const ConversationThread *a, const ConversationThread *b) {
                     if(a->pinned != b->pinned)
                       return a->pinned;
                     const bool aRunning = runningConversationIds.contains(a->id);
                     const bool bRunning = runningConversationIds.contains(b->id);
                     if(aRunning != bRunning)
                       return aRunning;
                     const bool aUnread = a->unread && a->id != m_ActiveConversationId;
                     const bool bUnread = b->unread && b->id != m_ActiveConversationId;
                     if(aUnread != bUnread)
                       return aUnread;
                     if(a->updatedMs != b->updatedMs)
                       return a->updatedMs > b->updatedMs;
                     if(a->createdMs != b->createdMs)
                       return a->createdMs > b->createdMs;
                     return a->id < b->id;
                   });

  QVector<AIConversationListItem> items;
  items.reserve(ordered.size());
  for(const ConversationThread *thread : ordered)
  {
    AIConversationListItem item;
    item.id = thread->id;
    item.title = thread->title;
    item.pinned = thread->pinned;
    item.unread = thread->unread && thread->id != m_ActiveConversationId;
    item.running = runningConversationIds.contains(thread->id);
    items.push_back(item);
  }
  m_ConversationList->setActiveId(m_ActiveConversationId);
  m_ConversationList->setItems(items);
}

void AIAssistant::refreshPendingChats()
{
  if(!m_PendingChats)
    return;
  QVector<AIPendingChatItem> items;
  for(const PendingChat &request : m_ChatQueue)
  {
    if(request.conversationId != m_ActiveConversationId)
      continue;
    AIPendingChatItem item;
    item.id = request.taskId;
    item.text = request.prompt;
    item.canAdjust = !m_ChatTaskId.isEmpty() &&
                     !m_CompletedTasks.contains(m_ChatTaskId) &&
                     m_ActiveChatConversationId == request.conversationId;
    items.push_back(item);
  }
  m_PendingChats->setItems(items);
}

void AIAssistant::resetConversations()
{
  m_Conversations.clear();
  ConversationThread thread;
  thread.id = lit("conversation-") + newTaskId();
  thread.title = tr("New conversation");
  thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
  m_Conversations.push_back(thread);
  m_ActiveConversationId = thread.id;
  refreshConversationList();
  renderConversation();
}

void AIAssistant::createConversation()
{
  if(m_Conversations.size() >= MaxConversationThreads)
  {
    QSet<QString> protectedIds;
    protectedIds.insert(m_ActiveConversationId);
    protectedIds.insert(m_ActiveChatConversationId);
    for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
      protectedIds.insert(it.value());
    for(const PendingChat &queued : m_ChatQueue)
      protectedIds.insert(queued.conversationId);

    int removable = -1;
    for(int i = 0; i < m_Conversations.size(); ++i)
    {
      if(m_Conversations[i].messages.isEmpty() && !m_Conversations[i].pinned &&
         !protectedIds.contains(m_Conversations[i].id))
      {
        removable = i;
        break;
      }
    }
    if(removable < 0)
    {
      appendSystem(tr("This capture already has %1 conversations. Delete one before creating "
                      "another.")
                       .arg(MaxConversationThreads),
                   true);
      return;
    }
    m_Conversations.remove(removable);
  }

  ConversationThread thread;
  thread.id = lit("conversation-") + newTaskId();
  thread.title = tr("New conversation");
  thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
  m_Conversations.push_back(thread);
  selectConversation(thread.id);
  saveConversations();
}

void AIAssistant::deleteConversation(const QString &conversationId)
{
  const QString targetId = conversationId.isEmpty() ? m_ActiveConversationId : conversationId;
  if(targetId.isEmpty() || !conversation(targetId))
    return;
  auto hasRunningTask = [this, targetId]() {
    for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
      if(it.value() == targetId && m_RunningTasks.contains(it.key()))
        return true;
    return false;
  };
  auto reportRunningTask = [this, targetId]() {
    if(targetId != m_ActiveConversationId)
      selectConversation(targetId);
    appendSystem(tr("Stop the active request before deleting this conversation."), true);
  };
  if(hasRunningTask())
  {
    reportRunningTask();
    return;
  }
  if(QMessageBox::question(this, tr("Delete conversation"),
                           tr("Delete this conversation and its stored context?")) !=
     QMessageBox::Yes)
    return;
  // QMessageBox runs a nested event loop. A queued turn can start while the confirmation is
  // visible, so revalidate both the target and its runtime state before mutating the vector.
  if(!conversation(targetId))
    return;
  if(hasRunningTask())
  {
    reportRunningTask();
    return;
  }

  for(int i = m_ChatQueue.size() - 1; i >= 0; --i)
    if(m_ChatQueue[i].conversationId == targetId)
    {
      const QString taskId = m_ChatQueue[i].taskId;
      m_UserPrompts.remove(taskId);
      m_TaskConversationIds.remove(taskId);
      m_TaskMessageIds.remove(taskId);
      m_ChatQueue.remove(i);
    }
  for(int i = 0; i < m_Conversations.size(); ++i)
  {
    if(m_Conversations[i].id == targetId)
    {
      m_Conversations.remove(i);
      break;
    }
  }
  if(m_Conversations.isEmpty())
  {
    resetConversations();
  }
  else
  {
    if(m_ActiveConversationId == targetId)
    {
      const ConversationThread *latest = &m_Conversations.front();
      for(const ConversationThread &thread : m_Conversations)
        if(thread.updatedMs > latest->updatedMs)
          latest = &thread;
      m_ActiveConversationId = latest->id;
    }
    refreshConversationList();
    renderConversation();
  }
  saveConversations();
}

void AIAssistant::toggleConversationPinned(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  thread->pinned = !thread->pinned;
  refreshConversationList();
  saveConversations();
}

void AIAssistant::selectConversation(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return;
  m_ActiveConversationId = conversationId;
  const bool clearedUnread = thread->unread;
  thread->unread = false;
  refreshConversationList();
  renderConversation();
  if(clearedUnread)
    saveConversations();
  else
    scheduleConversationSave();
}

void AIAssistant::addTaskActivity(const QString &taskId, const QString &kind,
                                  const QString &title, const QString &detail, bool completed,
                                  bool error, qint64 timestampMs, const QString &activityId)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return;
  const qint64 eventTimestampMs =
      timestampMs > 0 ? timestampMs : QDateTime::currentMSecsSinceEpoch();
  AIActivityItem activity;
  activity.id = activityId.left(160);
  activity.kind = kind;
  activity.title = title;
  activity.detail = detail;
  activity.timestampMs = eventTimestampMs;
  // An error flag also marks attention/approval states, which can still be live. Only
  // completed activities have a stable end timestamp.
  activity.finishedTimestampMs = completed ? eventTimestampMs : 0;
  activity.completed = completed;
  activity.error = error;
  bool updated = false;
  for(int i = item->activity.size() - 1; i >= 0; --i)
  {
    AIActivityItem &existing = item->activity[i];
    const bool identityMatches =
        !activity.id.isEmpty() ? existing.id == activity.id
                               // i18n-ignore: Activity kind is a backend protocol identifier used for matching.
                               : (activity.kind != lit("tool") ||
                                  existing.title == activity.title);
    if(existing.kind == activity.kind && identityMatches && !existing.completed)
    {
      if(existing.id.isEmpty())
        existing.id = activity.id;
      existing.title = activity.title;
      existing.detail = activity.detail;
      if(existing.timestampMs <= 0)
        existing.timestampMs = activity.timestampMs;
      existing.finishedTimestampMs = activity.finishedTimestampMs;
      existing.completed = activity.completed;
      existing.error = activity.error;
      updated = true;
      break;
    }
  }
  if(!updated)
    item->activity.push_back(activity);
  item->activityExpanded = true;
  renderConversation();
  scheduleConversationSave();
}

void AIAssistant::setTaskState(const QString &taskId, const QString &state,
                               bool collapseActivity)
{
  AIChatMessage *item = taskMessage(taskId);
  if(!item)
    return;
  item->state = state;
  item->activityExpanded = !collapseActivity;
  if(collapseActivity)
    item->finishedTimestampMs = QDateTime::currentMSecsSinceEpoch();
  // i18n-ignore: Task lifecycle state identifiers are used only for control flow.
  else if(state == lit("queued") || state == lit("planning") || state == lit("running") ||
          // i18n-ignore: Task lifecycle state identifier is used only for control flow.
          state == lit("finalizing"))
    item->finishedTimestampMs = 0;
  // Keep a terminal failure/cancellation summary visible. Only successful turns replace their
  // provisional stream with a separate authoritative assistant message.
  if(collapseActivity &&
     // i18n-ignore: Task lifecycle state identifiers used only for control flow.
     (state == lit("completed") || state == lit("needs-user-input") ||
      // i18n-ignore: Task lifecycle state identifier used only for control flow.
      state == lit("blocked")))
    item->provisionalText.clear();
  renderConversation();
  scheduleConversationSave();
}

bool AIAssistant::clarificationFromJSON(const QJsonObject &object, const QString &fallbackId,
                                        ClarificationPrompt &clarification) const
{
  clarification = ClarificationPrompt();
  if(object.isEmpty())
    return false;

  // i18n-ignore: ReplayLab clarification protocol field names and enum values.
  const QJsonValue questionValue = object.value(lit("question"));    // i18n-ignore: clarification protocol key
  const QJsonValue modeValue = object.value(lit("selectionMode"));    // i18n-ignore: clarification protocol key
  const QJsonValue optionsValue = object.value(lit("options"));    // i18n-ignore: clarification protocol key
  const QJsonValue allowOtherValue = object.value(lit("allowOther"));    // i18n-ignore: clarification protocol key
  if(!questionValue.isString() || !modeValue.isString() || !optionsValue.isArray() ||
     !allowOtherValue.isBool())
    return false;

  const QString question = questionValue.toString().trimmed();
  const QString selectionMode = modeValue.toString();
  // i18n-ignore: ReplayLab clarification selection-mode enum values.
  if(question.isEmpty() || question.size() > 2400 ||
     (selectionMode != lit("single") &&    // i18n-ignore: clarification protocol enum
      selectionMode != lit("multiple")))    // i18n-ignore: clarification protocol enum
    return false;

  const QJsonArray optionRows = optionsValue.toArray();
  if(optionRows.size() < 2 || optionRows.size() > 6)
    return false;
  const QRegularExpression safeId(
      lit("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"));    // i18n-ignore: protocol ID validation regex
  QSet<QString> optionIds;
  QVector<ClarificationOption> options;
  options.reserve(optionRows.size());
  for(const QJsonValue &value : optionRows)
  {
    if(!value.isObject())
      return false;
    const QJsonObject row = value.toObject();
    // i18n-ignore: ReplayLab clarification protocol field names.
    if(!row.value(lit("id")).isString() || !row.value(lit("label")).isString())
      return false;
    const QString id = row.value(lit("id")).toString();    // i18n-ignore: clarification protocol key
    const QString label = row.value(lit("label")).toString().simplified();    // i18n-ignore: clarification protocol key
    const QJsonValue descriptionValue = row.value(lit("description"));    // i18n-ignore: clarification protocol key
    if(!descriptionValue.isUndefined() && !descriptionValue.isString())
      return false;
    const QString description = descriptionValue.toString().trimmed();
    if(!safeId.match(id).hasMatch() || optionIds.contains(id) || label.isEmpty() ||
       label.size() > 240 || description.size() > 600)
      return false;
    optionIds.insert(id);
    options.push_back({id, label, description});
  }

  QString id;
  // i18n-ignore: ReplayLab clarification protocol field name.
  const QJsonValue idValue = object.value(lit("id"));
  if(idValue.isString())
    id = idValue.toString();
  else if(!idValue.isUndefined() && !idValue.isNull())
    return false;
  if(id.isEmpty())
    id = lit("clarification-") + fallbackId.left(48);    // i18n-ignore: internal protocol ID prefix
  if(!safeId.match(id).hasMatch())
    return false;

  QString submitLabel;
  // i18n-ignore: ReplayLab clarification protocol field name.
  const QJsonValue submitLabelValue = object.value(lit("submitLabel"));
  if(submitLabelValue.isString())
    submitLabel = submitLabelValue.toString().simplified();
  else if(!submitLabelValue.isUndefined() && !submitLabelValue.isNull())
    return false;
  if(submitLabel.size() > 64)
    return false;

  clarification.id = id;
  clarification.question = question;
  clarification.selectionMode = selectionMode;
  clarification.options = options;
  clarification.allowOther = allowOtherValue.toBool();
  clarification.submitLabel = submitLabel;

  // Persisted native clarifications may carry the exact frozen origin. Backend events normally do
  // not: bindClarificationOrigin() attaches the trusted active request after protocol validation.
  // i18n-ignore: ReplayLab persisted clarification protocol field names.
  const QJsonValue originSnapshotValue = object.value(lit("originContextSnapshot"));
  const QJsonValue originInstanceValue =
      object.value(lit("originContextInstanceId"));    // i18n-ignore: persisted clarification protocol key
  if(!originSnapshotValue.isUndefined() || !originInstanceValue.isUndefined())
  {
    if(!originSnapshotValue.isObject() || !originInstanceValue.isString())
      return false;
    const QByteArray encodedOrigin =
        QJsonDocument(originSnapshotValue.toObject()).toJson(QJsonDocument::Compact);
    const QString originInstance = originInstanceValue.toString();
    if(originInstance.trimmed().isEmpty() || originInstance.size() > 160 ||
       !decodeContextSnapshot(encodedOrigin))
      return false;
    clarification.originContextSnapshot = encodedOrigin;
    clarification.originContextInstanceId = originInstance;
  }
  return clarification.isValid();
}

QJsonObject AIAssistant::clarificationToJSON(
    const ClarificationPrompt &clarification) const
{
  QJsonObject object;
  // i18n-ignore: Persisted ReplayLab clarification protocol field names.
  object[lit("id")] = clarification.id;    // i18n-ignore: clarification protocol key
  object[lit("question")] = clarification.question;    // i18n-ignore: clarification protocol key
  object[lit("selectionMode")] = clarification.selectionMode;    // i18n-ignore: clarification protocol key
  object[lit("allowOther")] = clarification.allowOther;    // i18n-ignore: clarification protocol key
  if(!clarification.submitLabel.isEmpty())
    object[lit("submitLabel")] = clarification.submitLabel;    // i18n-ignore: clarification protocol key
  QJsonArray options;
  for(const ClarificationOption &option : clarification.options)
  {
    QJsonObject row;
    row[lit("id")] = option.id;    // i18n-ignore: clarification protocol key
    row[lit("label")] = option.label;    // i18n-ignore: clarification protocol key
    if(!option.description.isEmpty())
      row[lit("description")] = option.description;    // i18n-ignore: clarification protocol key
    options.push_back(row);
  }
  object[lit("options")] = options;    // i18n-ignore: clarification protocol key
  const QJsonObject origin = contextCandidateJSON(clarification.originContextSnapshot,
                                                  clarification.originContextInstanceId);
  if(!origin.isEmpty())
  {
    // i18n-ignore: Persisted ReplayLab clarification protocol field names.
    object[lit("originContextSnapshot")] = origin.value(lit("snapshot"));
    object[lit("originContextInstanceId")] =    // i18n-ignore: persisted clarification protocol key
        clarification.originContextInstanceId;
  }
  return object;
}

void AIAssistant::bindClarificationOrigin(ClarificationPrompt &clarification,
                                          const QString &taskId) const
{
  if(decodeContextSnapshot(clarification.originContextSnapshot) &&
     !clarification.originContextInstanceId.trimmed().isEmpty())
    return;
  if(m_ActiveChatRequest.taskId == taskId &&
     decodeContextSnapshot(m_ActiveChatRequest.contextSnapshot) &&
     !m_ActiveChatRequest.contextInstanceId.trimmed().isEmpty())
  {
    clarification.originContextSnapshot = m_ActiveChatRequest.contextSnapshot;
    clarification.originContextInstanceId = m_ActiveChatRequest.contextInstanceId;
  }
}

void AIAssistant::updateClarificationUI()
{
  if(!m_ClarificationPanel || !m_ClarificationOptionsLayout)
    return;

  const ConversationThread *thread = conversation();
  const ClarificationPrompt *clarification =
      thread && thread->clarification.isValid() ? &thread->clarification : NULL;
  const QString key = clarification ? thread->id + lit("\n") + clarification->id : QString();
  if(clarification && key == m_VisibleClarificationKey)
  {
    m_ClarificationPanel->setVisible(true);
    updateClarificationSubmitState();
    return;
  }

  delete m_ClarificationButtonGroup;
  m_ClarificationButtonGroup = NULL;
  m_ClarificationButtons.clear();
  m_ClarificationOtherEdit = NULL;
  while(QLayoutItem *item = m_ClarificationOptionsLayout->takeAt(0))
  {
    delete item->widget();
    delete item;
  }
  m_VisibleClarificationKey.clear();

  if(!clarification)
  {
    m_ClarificationPanel->setVisible(false);
    return;
  }

  m_VisibleClarificationKey = key;
  m_ClarificationQuestion->setText(clarification->question);
  m_ClarificationButtonGroup = new QButtonGroup(m_ClarificationPanel);
  // i18n-ignore: ReplayLab clarification selection-mode enum value.
  m_ClarificationButtonGroup->setExclusive(clarification->selectionMode == lit("single"));
  const QString choiceStyle =
      // i18n-ignore: Qt stylesheet syntax contains no user-visible text.
      lit("QCommandLinkButton { border: 1px solid palette(mid); border-radius: 5px; "
          "padding: 5px; text-align: left; } "
          "QCommandLinkButton:hover { background: palette(midlight); } "
          "QCommandLinkButton:checked { border: 2px solid palette(highlight); "
          "background: palette(base); }");
  for(const ClarificationOption &option : clarification->options)
  {
    QCommandLinkButton *choice = new QCommandLinkButton(option.label, m_ClarificationOptions);
    choice->setDescription(option.description);
    choice->setAccessibleName(option.label);
    choice->setAccessibleDescription(option.description);
    choice->setCheckable(true);
    choice->setStyleSheet(choiceStyle);
    // i18n-ignore: Dynamic QObject properties are internal clarification protocol state.
    choice->setProperty("clarificationOptionId", option.id);
    choice->setProperty("clarificationOptionLabel", option.label);
    choice->setProperty("clarificationOther", false);
    m_ClarificationButtonGroup->addButton(choice);
    m_ClarificationButtons.push_back(choice);
    m_ClarificationOptionsLayout->addWidget(choice);
    connect(choice, &QCommandLinkButton::toggled, this,
            [this](bool) { updateClarificationSubmitState(); });
  }

  if(clarification->allowOther)
  {
    QCommandLinkButton *other = new QCommandLinkButton(tr("Other"), m_ClarificationOptions);
    other->setDescription(tr("Enter a different answer."));
    other->setAccessibleName(tr("Other"));
    other->setAccessibleDescription(tr("Enter a different answer."));
    other->setCheckable(true);
    other->setStyleSheet(choiceStyle);
    other->setProperty("clarificationOther", true);
    m_ClarificationButtonGroup->addButton(other);
    m_ClarificationButtons.push_back(other);
    m_ClarificationOptionsLayout->addWidget(other);
    m_ClarificationOtherEdit = new QLineEdit(m_ClarificationOptions);
    m_ClarificationOtherEdit->setPlaceholderText(tr("Enter another answer"));
    m_ClarificationOtherEdit->setMaxLength(MaxClarificationOtherChars);
    m_ClarificationOtherEdit->setEnabled(false);
    m_ClarificationOptionsLayout->addWidget(m_ClarificationOtherEdit);
    connect(other, &QCommandLinkButton::toggled, this, [this](bool checked) {
      if(m_ClarificationOtherEdit)
      {
        m_ClarificationOtherEdit->setEnabled(checked);
        if(checked)
          m_ClarificationOtherEdit->setFocus(Qt::OtherFocusReason);
      }
      updateClarificationSubmitState();
    });
    connect(m_ClarificationOtherEdit, &QLineEdit::textChanged, this,
            [this, other](const QString &text) {
              if(!text.trimmed().isEmpty() && !other->isChecked())
                other->setChecked(true);
              updateClarificationSubmitState();
            });
  }

  QString submitText = tr("Continue");
  // submitLabel is a provider hint, never unrestricted UI text. Localise only the stable values
  // known by qrenderdoc and fall back to the canonical Continue label for unknown values.
  if(clarification->submitLabel.compare(lit("Confirm"), Qt::CaseInsensitive) == 0)    // i18n-ignore: submitLabel protocol enum
    submitText = tr("Confirm");
  else if(clarification->submitLabel.compare(lit("Submit"), Qt::CaseInsensitive) == 0)    // i18n-ignore: submitLabel protocol enum
    submitText = tr("Submit");
  m_ClarificationSubmit->setText(submitText);
  m_ClarificationPanel->setVisible(true);
  updateClarificationSubmitState();
}

void AIAssistant::updateClarificationSubmitState()
{
  if(!m_ClarificationSubmit)
    return;
  bool anySelected = false;
  bool selectedOtherIsEmpty = false;
  for(QCommandLinkButton *choice : m_ClarificationButtons)
  {
    if(!choice || !choice->isChecked())
      continue;
    anySelected = true;
    if(choice->property("clarificationOther").toBool() &&
       (!m_ClarificationOtherEdit || m_ClarificationOtherEdit->text().trimmed().isEmpty()))
      selectedOtherIsEmpty = true;
  }
  m_ClarificationSubmit->setEnabled(anySelected && !selectedOtherIsEmpty);
}

void AIAssistant::submitClarification()
{
  const ConversationThread *thread = conversation();
  if(!thread || !thread->clarification.isValid())
    return;

  QStringList selected;
  QStringList selectedIds;
  QString otherText;
  for(QCommandLinkButton *choice : m_ClarificationButtons)
  {
    if(!choice || !choice->isChecked())
      continue;
    if(choice->property("clarificationOther").toBool())
    {
      if(m_ClarificationOtherEdit)
        otherText = m_ClarificationOtherEdit->text().trimmed();
    }
    else
    {
      const QString optionId = choice->property("clarificationOptionId").toString();    // i18n-ignore: internal clarification property
      const QString label = choice->property("clarificationOptionLabel").toString().simplified();
      if(!optionId.isEmpty() && !label.isEmpty())
      {
        selectedIds.push_back(optionId);
        selected.push_back(label);
      }
    }
  }

  QString answer;
  // i18n-ignore: ReplayLab clarification selection-mode enum value.
  if(thread->clarification.selectionMode == lit("single"))
  {
    if(!otherText.isEmpty())
      answer = otherText;
    else if(selected.size() == 1)
      answer = tr("My choice: %1").arg(selected.front());
  }
  else
  {
    if(!otherText.isEmpty())
      selected.push_back(otherText);
    if(!selected.isEmpty())
    {
      QStringList rows;
      for(const QString &item : selected)
        rows.push_back(lit("- ") + item);
      answer = tr("My choices:\n%1").arg(rows.join(QLatin1Char('\n')));
    }
  }
  if(!answer.trimmed().isEmpty())
  {
    ClarificationResponse response;
    response.clarificationId = thread->clarification.id;
    response.optionIds = selectedIds;
    response.otherText = otherText;
    response.originContextSnapshot = thread->clarification.originContextSnapshot;
    response.originContextInstanceId = thread->clarification.originContextInstanceId;
    // Keep the readable answer in the transcript. Stable IDs and origin/current context candidates
    // travel separately in replaylab-conversation/v2 for intent-aware resolution by AgentHost.
    startChat(answer, QString(), true, QString(), response);
  }
}

void AIAssistant::clearClarification(const QString &conversationId)
{
  ConversationThread *thread = conversation(conversationId);
  if(!thread || !thread->clarification.isValid())
    return;
  thread->clarification = ClarificationPrompt();
  if(thread->id == m_ActiveConversationId)
  {
    m_VisibleClarificationKey.clear();
    updateClarificationUI();
  }
  scheduleConversationSave();
}

void AIAssistant::updateComposerState()
{
  if(!m_QueueStatus)
    return;
  QString status;
  if(!m_ChatTaskId.isEmpty())
    status = tr("Running");
  const int captureTaskCount =
      qMax(0, m_RunningTasks.size() - (m_RunningTasks.contains(m_ChatTaskId) ? 1 : 0));
  if(captureTaskCount > 0)
    status += (status.isEmpty() ? QString() : lit(" | ")) +
              tr("%1 capture action(s) running").arg(captureTaskCount);
  if(!m_ChatQueue.isEmpty())
    status += (status.isEmpty() ? QString() : lit(" | ")) +
              tr("%1 queued").arg(m_ChatQueue.size());
  m_QueueStatus->setText(status);
  refreshPendingChats();
  if(m_Cancel)
  {
    const bool canStopChat = !m_ChatTaskId.isEmpty() && m_ChatProcess &&
                             m_ChatProcess->state() != QProcess::NotRunning;
    m_Cancel->setVisible(canStopChat);
    m_Cancel->setEnabled(canStopChat);
    m_Cancel->setToolTip(tr("Stop the active chat request."));
  }
  if(m_Send)
    m_Send->setEnabled(true);
  updateClarificationUI();
}

void AIAssistant::handleTranscriptLink(const QUrl &url)
{
  const QString scheme = url.scheme().toLower();
  if(scheme == lit("http") || scheme == lit("https"))
  {
    QDesktopServices::openUrl(url);
    return;
  }
  if(scheme != lit("rdoc") || !m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("The link is not an allowed RenderDoc navigation target."), true);
    return;
  }

  const QString target = url.host().toLower();
  QString value = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
  if(target == lit("event") || target == lit("eid"))
  {
    bool ok = false;
    const uint32_t eid = value.toUInt(&ok);
    if(ok && eid > 0)
    {
      m_Ctx.SetEventID({}, eid, eid);
      return;
    }
  }
  else if(target == lit("resource") || target == lit("texture") || target == lit("buffer"))
  {
    value.remove(lit("ResourceId::"), Qt::CaseInsensitive);
    bool ok = false;
    const qulonglong raw = value.toULongLong(&ok);
    if(ok && raw != 0)
    {
      ResourceId id;
      static_assert(sizeof(id) == sizeof(raw), "ResourceId wire size changed");
      memcpy(&id, &raw, sizeof(id));
      if(target == lit("texture"))
      {
        m_Ctx.GetTextureViewer()->ViewTexture(id, CompType::Typeless, true);
        m_Ctx.ShowTextureViewer();
        return;
      }
      if(target == lit("buffer"))
      {
        const BufferDescription *buffer = m_Ctx.GetBuffer(id);
        if(buffer)
        {
          m_Ctx.ViewBuffer(0, buffer->length, id, QString());
          return;
        }
      }
      IResourceInspector *inspector = m_Ctx.GetResourceInspector();
      inspector->Inspect(id);
      m_Ctx.RaiseDockWindow(inspector->Widget());
      return;
    }
  }
  else if(target == lit("panel"))
  {
    const QString panel = value.toLower();
    if(panel == lit("pipeline"))
      m_Ctx.ShowPipelineViewer();
    else if(panel == lit("texture"))
      m_Ctx.ShowTextureViewer();
    else if(panel == lit("resources"))
      m_Ctx.RaiseDockWindow(m_Ctx.GetResourceInspector()->Widget());
    else
      appendSystem(tr("Unknown RenderDoc panel link: %1").arg(value), true);
    return;
  }
  appendSystem(tr("The RenderDoc link target is invalid."), true);
}

QJsonObject AIAssistant::conversationToJSON(const ConversationThread &thread,
                                            int *remainingChars) const
{
  QJsonObject object;
  object[lit("id")] = thread.id;
  object[lit("title")] = thread.title.left(256);
  object[lit("createdUnixMs")] = QString::number(thread.createdMs);
  object[lit("updatedUnixMs")] = QString::number(thread.updatedMs);
  object[lit("pinned")] = thread.pinned;
  object[lit("unread")] = thread.unread;
  if(thread.clarification.isValid())
  {
    const QJsonObject clarificationObject = clarificationToJSON(thread.clarification);
    // UTF-8 bytes conservatively count against the UTF-16 character budget. This keeps a
    // persisted origin snapshot from evicting newer transcript rows beyond the store bound.
    const int clarificationChars =
        QJsonDocument(clarificationObject).toJson(QJsonDocument::Compact).size();
    if(clarificationChars <= *remainingChars)
    {
      // i18n-ignore: Persisted ReplayLab clarification protocol field name.
      object[lit("clarification")] = clarificationObject;
      *remainingChars -= clarificationChars;
    }
  }
  QJsonArray messages;
  QVector<QJsonObject> selectedMessages;
  const int first = qMax(0, thread.messages.size() - MaxConversationMessages);
  // Allocate the bounded store from newest to oldest. Prepending the selected rows restores
  // chronological display order without allowing old history to evict the latest turn.
  for(int i = thread.messages.size() - 1; i >= first && *remainingChars > 0; --i)
  {
    const AIChatMessage &item = thread.messages[i];
    if(item.role != lit("user") && item.role != lit("assistant"))
      continue;
    QJsonObject row;
    row[lit("id")] = item.id;
    row[lit("role")] = item.role;
    const int textLimit = qMin(MaxConversationMessageChars, *remainingChars);
    row[lit("text")] = item.text.left(textLimit);
    *remainingChars -= row.value(lit("text")).toString().size();
    const int provisionalLimit = qMin(MaxConversationMessageChars, qMax(0, *remainingChars));
    row[lit("provisionalText")] = item.provisionalText.left(provisionalLimit);
    *remainingChars -= row.value(lit("provisionalText")).toString().size();
    row[lit("state")] = item.state;
    row[lit("taskId")] = item.taskId;
    row[lit("timestampUnixMs")] = QString::number(item.timestampMs);
    // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
    row[lit("finishedUnixMs")] = QString::number(item.finishedTimestampMs);
    row[lit("activityExpanded")] = item.activityExpanded;
    QJsonArray activities;
    QVector<QJsonObject> selectedActivities;
    const int activityFirst = qMax(0, item.activity.size() - 64);
    for(int j = item.activity.size() - 1; j >= activityFirst && *remainingChars > 0; --j)
    {
      const AIActivityItem &activity = item.activity[j];
      QJsonObject activityRow;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("id")] = activity.id.left(160);
      activityRow[lit("kind")] = activity.kind.left(64);
      const int titleLimit = qMin(1000, qMax(0, *remainingChars));
      activityRow[lit("title")] = activity.title.left(titleLimit);
      *remainingChars -= activityRow.value(lit("title")).toString().size();
      const int detailLimit = qMin(4000, qMax(0, *remainingChars));
      activityRow[lit("detail")] = activity.detail.left(detailLimit);
      *remainingChars -= activityRow.value(lit("detail")).toString().size();
      activityRow[lit("completed")] = activity.completed;
      activityRow[lit("error")] = activity.error;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("timestampUnixMs")] = QString::number(activity.timestampMs);
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activityRow[lit("finishedUnixMs")] = QString::number(activity.finishedTimestampMs);
      selectedActivities.prepend(activityRow);
    }
    for(const QJsonObject &activityRow : selectedActivities)
      activities.push_back(activityRow);
    row[lit("activity")] = activities;
    selectedMessages.prepend(row);
  }
  for(const QJsonObject &row : selectedMessages)
    messages.push_back(row);
  object[lit("messages")] = messages;
  return object;
}

bool AIAssistant::conversationFromJSON(const QJsonObject &object, ConversationThread &thread) const
{
  thread.id = object.value(lit("id")).toString();
  thread.title = object.value(lit("title")).toString().left(256);
  thread.createdMs = object.value(lit("createdUnixMs")).toString().toLongLong();
  thread.updatedMs = object.value(lit("updatedUnixMs")).toString().toLongLong();
  thread.pinned = object.value(lit("pinned")).toBool();
  thread.unread = object.value(lit("unread")).toBool();
  if(!safeTaskId(thread.id) || !object.value(lit("messages")).isArray())
    return false;
  if(thread.title.isEmpty())
    thread.title = tr("Conversation");
  // i18n-ignore: Persisted ReplayLab clarification protocol field name.
  const QJsonValue clarificationValue = object.value(lit("clarification"));
  if(clarificationValue.isObject())
  {
    ClarificationPrompt clarification;
    if(clarificationFromJSON(clarificationValue.toObject(), thread.id, clarification))
      thread.clarification = clarification;
  }
  const QJsonArray messages = object.value(lit("messages")).toArray();
  const int first = qMax(0, messages.size() - MaxConversationMessages);
  for(int i = first; i < messages.size(); ++i)
  {
    if(!messages[i].isObject())
      continue;
    const QJsonObject row = messages[i].toObject();
    AIChatMessage item;
    item.id = row.value(lit("id")).toString();
    item.role = row.value(lit("role")).toString();
    item.text = row.value(lit("text")).toString().left(MaxConversationMessageChars);
    item.provisionalText =
        row.value(lit("provisionalText")).toString().left(MaxConversationMessageChars);
    item.state = row.value(lit("state")).toString();
    item.taskId = row.value(lit("taskId")).toString();
    item.timestampMs = row.value(lit("timestampUnixMs")).toString().toLongLong();
    // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
    item.finishedTimestampMs = row.value(lit("finishedUnixMs")).toString().toLongLong();
    item.activityExpanded = row.value(lit("activityExpanded")).toBool();
    if(!safeTaskId(item.id) || (item.role != lit("user") && item.role != lit("assistant")))
      continue;
    const bool interruptedOnLoad =
        item.state == lit("queued") || item.state == lit("running") ||
        item.state == lit("planning") || item.state == lit("finalizing");
    if(interruptedOnLoad)
    {
      item.state = lit("interrupted");
      item.activityExpanded = false;
    }
    const QJsonArray activities = row.value(lit("activity")).toArray();
    const int activityFirst = qMax(0, activities.size() - 64);
    for(int j = activityFirst; j < activities.size(); ++j)
    {
      if(!activities[j].isObject())
        continue;
      const QJsonObject activityRow = activities[j].toObject();
      AIActivityItem activity;
      // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
      activity.id = activityRow.value(lit("id")).toString().left(160);
      activity.kind = activityRow.value(lit("kind")).toString().left(64);
      activity.title = activityRow.value(lit("title")).toString().left(1000);
      activity.detail = activityRow.value(lit("detail")).toString().left(4000);
      activity.completed = activityRow.value(lit("completed")).toBool();
      activity.error = activityRow.value(lit("error")).toBool();
      activity.timestampMs =
          // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
          activityRow.value(lit("timestampUnixMs")).toString().toLongLong();
      activity.finishedTimestampMs =
          // i18n-ignore: Persisted conversation JSON field name; changing it would break compatibility.
          activityRow.value(lit("finishedUnixMs")).toString().toLongLong();
      if(interruptedOnLoad && !activity.completed)
      {
        activity.completed = true;
        activity.error = true;
        activity.finishedTimestampMs = QDateTime::currentMSecsSinceEpoch();
      }
      item.activity.push_back(activity);
    }
    if(interruptedOnLoad)
    {
      AIActivityItem interrupted;
      interrupted.kind = lit("interrupted");
      interrupted.title = tr("Interrupted when the capture closed");
      interrupted.timestampMs = QDateTime::currentMSecsSinceEpoch();
      interrupted.finishedTimestampMs = interrupted.timestampMs;
      interrupted.completed = true;
      item.activity.push_back(interrupted);
      item.finishedTimestampMs = interrupted.timestampMs;
    }
    thread.messages.push_back(item);
  }
  return true;
}

void AIAssistant::loadConversations()
{
  m_LoadingConversations = true;
  m_Conversations.clear();
  m_ChatQueue.clear();
  m_ActiveConversationId.clear();
  QString storeError;
  QJsonArray interruptedQueue;
  if(!m_CaptureConversationPath.isEmpty())
  {
    QByteArray encoded;
    if(AIConversationStore::Load(m_CaptureConversationPath, &encoded, &storeError) &&
       encoded.size() <= MaxConversationStoreChars)
    {
      if(!encoded.isEmpty())
      {
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(encoded, &parseError);
        if(parseError.error != QJsonParseError::NoError || !document.isObject())
        {
          storeError = tr("The stored conversation payload is invalid JSON.");
        }
        else
        {
          const QJsonObject root = document.object();
          if(root.value(lit("schema")).toString() != lit("replaylab-ai-conversations/v2"))
          {
            storeError = tr("The stored conversation payload uses an unsupported schema.");
          }
          else
          {
            m_ActiveConversationId = root.value(lit("activeConversationId")).toString();
            interruptedQueue = root.value(lit("queuedMessages")).toArray();
            const QJsonArray threads = root.value(lit("conversations")).toArray();
            for(int i = 0; i < threads.size() && m_Conversations.size() < MaxConversationThreads;
                ++i)
            {
              if(!threads[i].isObject())
                continue;
              ConversationThread thread;
              if(conversationFromJSON(threads[i].toObject(), thread))
                m_Conversations.push_back(thread);
            }
          }
        }
      }
    }
    else if(encoded.size() > MaxConversationStoreChars && storeError.isEmpty())
    {
      storeError = tr("The stored conversation payload exceeds the 4 MiB limit.");
    }
  }
  if(m_Conversations.isEmpty())
  {
    ConversationThread thread;
    thread.id = lit("conversation-") + newTaskId();
    thread.title = tr("New conversation");
    thread.createdMs = thread.updatedMs = QDateTime::currentMSecsSinceEpoch();
    m_Conversations.push_back(thread);
    m_ActiveConversationId = thread.id;
  }
  if(!conversation(m_ActiveConversationId))
    m_ActiveConversationId = m_Conversations.back().id;
  const int queuedFirst = qMax(0, interruptedQueue.size() - MaxPendingChats);
  for(int i = queuedFirst; i < interruptedQueue.size(); ++i)
  {
    if(!interruptedQueue[i].isObject())
      continue;
    const QJsonObject row = interruptedQueue[i].toObject();
    const QString taskId = row.value(lit("taskId")).toString();
    const QString conversationId = row.value(lit("conversationId")).toString();
    const QString prompt = row.value(lit("prompt")).toString().left(MaxConversationMessageChars);
    ConversationThread *thread = conversation(conversationId);
    if(!thread || !safeTaskId(taskId) || prompt.trimmed().isEmpty())
      continue;
    const QString messageId = lit("message-") + taskId;
    bool duplicate = false;
    for(const AIChatMessage &existing : thread->messages)
      duplicate |= existing.id == messageId;
    if(duplicate)
      continue;
    AIChatMessage item;
    item.id = messageId;
    item.role = lit("user");
    item.text = prompt;
    item.taskId = taskId;
    item.state = lit("interrupted");
    item.timestampMs = row.value(lit("queuedUnixMs")).toString().toLongLong();
    if(item.timestampMs <= 0)
      item.timestampMs = thread->updatedMs;
    item.finishedTimestampMs = qMax(item.timestampMs, thread->updatedMs);
    AIActivityItem activity;
    activity.kind = lit("interrupted");
    activity.title = tr("Queued message was not run before the previous session ended");
    if(row.value(lit("truncated")).toBool())
      activity.detail = tr("The recovery copy was truncated to fit the local history limit.");
    activity.timestampMs = item.finishedTimestampMs;
    activity.finishedTimestampMs = item.finishedTimestampMs;
    activity.completed = true;
    item.activity.push_back(activity);
    thread->messages.push_back(item);
    thread->updatedMs = qMax(thread->updatedMs, item.timestampMs);
  }
  if(ConversationThread *active = conversation(m_ActiveConversationId))
    active->unread = false;
  m_LoadingConversations = false;
  refreshConversationList();
  renderConversation();
  if(!storeError.isEmpty())
    appendSystem(tr("The conversation history for this capture could not be loaded: %1")
                     .arg(storeError),
                 true);
}

void AIAssistant::saveConversations()
{
  if(m_LoadingConversations || m_CaptureConversationPath.isEmpty())
    return;
  // Keep a conservative byte margin for UTF-8 conversion, JSON structure and escaping.
  int remainingChars = MaxConversationStoreChars / 4 - 8192;
  QJsonObject root;
  root[lit("schema")] = lit("replaylab-ai-conversations/v2");
  root[lit("activeConversationId")] = m_ActiveConversationId;
  root[lit("savedUnixMs")] = QString::number(QDateTime::currentMSecsSinceEpoch());
  // Pending prompts have not entered the transcript yet. Reserve bounded space for them before
  // historical messages so a crash cannot silently discard the user's next requested work.
  QJsonArray queuedMessages;
  QVector<const PendingChat *> persistedQueue;
  const int pendingFirst = qMax(0, m_ChatQueue.size() - MaxPendingChats);
  for(int i = pendingFirst; i < m_ChatQueue.size(); ++i)
  {
    const PendingChat &request = m_ChatQueue[i];
    if(safeTaskId(request.taskId) && conversation(request.conversationId) &&
       !request.prompt.trimmed().isEmpty())
      persistedQueue.push_back(&request);
  }
  int pendingBudget = qMin(MaxConversationMessageChars, remainingChars / 3);
  for(int i = 0; i < persistedQueue.size() && pendingBudget > 0; ++i)
  {
    const PendingChat &request = *persistedQueue[i];
    const int requestsLeft = persistedQueue.size() - i;
    const int promptLimit =
        qMin(MaxConversationMessageChars, qMax(1, pendingBudget / requestsLeft));
    QJsonObject row;
    row[lit("taskId")] = request.taskId;
    row[lit("conversationId")] = request.conversationId;
    row[lit("prompt")] = request.prompt.left(promptLimit);
    row[lit("truncated")] = request.prompt.size() > promptLimit;
    const int usedChars = row.value(lit("prompt")).toString().size();
    pendingBudget -= usedChars;
    remainingChars -= usedChars;
    const ConversationThread *thread = conversation(request.conversationId);
    row[lit("queuedUnixMs")] = QString::number(thread ? thread->updatedMs : 0);
    queuedMessages.push_back(row);
  }
  root[lit("queuedMessages")] = queuedMessages;

  QJsonArray threads;
  QSet<QString> liveConversationIds;
  for(auto it = m_TaskConversationIds.cbegin(); it != m_TaskConversationIds.cend(); ++it)
    if(m_RunningTasks.contains(it.key()))
      liveConversationIds.insert(it.value());
  for(const PendingChat &request : m_ChatQueue)
    liveConversationIds.insert(request.conversationId);
  QVector<const ConversationThread *> orderedThreads;
  orderedThreads.reserve(m_Conversations.size());
  for(const ConversationThread &thread : m_Conversations)
    orderedThreads.push_back(&thread);
  std::stable_sort(
      orderedThreads.begin(), orderedThreads.end(),
      [this, &liveConversationIds](const ConversationThread *a,
                                   const ConversationThread *b) {
        const bool aActive = a->id == m_ActiveConversationId;
        const bool bActive = b->id == m_ActiveConversationId;
        if(aActive != bActive)
          return aActive;
        const bool aLive = liveConversationIds.contains(a->id);
        const bool bLive = liveConversationIds.contains(b->id);
        if(aLive != bLive)
          return aLive;
        if(a->pinned != b->pinned)
          return a->pinned;
        if(a->updatedMs != b->updatedMs)
          return a->updatedMs > b->updatedMs;
        return a->createdMs > b->createdMs;
      });
  const int threadCount = qMin(MaxConversationThreads, orderedThreads.size());
  for(int i = 0; i < threadCount && remainingChars > 0; ++i)
    threads.push_back(conversationToJSON(*orderedThreads[i], &remainingChars));
  root[lit("conversations")] = threads;
  const QByteArray encoded = QJsonDocument(root).toJson(QJsonDocument::Compact);
  if(encoded.size() <= MaxConversationStoreChars)
  {
    QString error;
    if(!AIConversationStore::Save(m_CaptureConversationPath, encoded, &error))
      qWarning() << "Could not persist ReplayLab AI conversations:" << error;
  }
  else
  {
    qWarning() << "Could not persist ReplayLab AI conversations: encoded payload exceeds 4 MiB";
  }
}

void AIAssistant::scheduleConversationSave()
{
  if(!m_LoadingConversations && !m_CaptureConversationPath.isEmpty() &&
     m_ConversationSaveTimer)
  {
    // Make the first mutation durable immediately. Further bursts are coalesced so streaming
    // status updates do not repeatedly serialize the complete local-store document.
    if(!m_ConversationSaveTimer->isActive())
      saveConversations();
    m_ConversationSaveTimer->start(500);
  }
}

void AIAssistant::updateCaptureStatus()
{
  if(m_Ctx.IsCaptureLoaded())
  {
    const rdcstr captureFilename = m_Ctx.GetCaptureFilename();
    m_Context->setText(tr("Capture: %1 | current EID %2 | selected EID %3")
                           .arg(QString::fromUtf8(captureFilename.c_str(),
                                                  int(captureFilename.size())))
                           .arg(m_Ctx.CurEvent())
                           .arg(m_Ctx.CurSelectedEvent()));
  }
  else
  {
    m_Context->setText(tr("No capture loaded | general chat is available"));
  }
}

void AIAssistant::showShaderIntentMenu(QWidget *anchor)
{
  if(!m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("This action requires an open capture."), true);
    return;
  }

  QString error;
  const QStringList stages = m_Ctx.ReplayLab().AvailableShaderStages(&error);
  if(stages.isEmpty())
  {
    appendSystem(error.isEmpty()
                     ? tr("No stage-scoped shader AI actions are available for this capture.")
                     : error,
                 true);
    return;
  }

  QMenu menu(this);
  for(const QString &stage : stages)
  {
    // Stage names are protocol identifiers and intentionally remain untranslated.
    QAction *action = menu.addAction(stage);
    connect(action, &QAction::triggered, this, [this, stage]() {
      StartCaptureSkill(tr("Reverse/Edit selected shader with AI"),
                        lit("renderdoc-shader-workbench"),    // i18n-ignore: internal Skill identifier
                        stage,
                        lit("reverse-edit-selected-shader"));    // i18n-ignore: stable UI intent identifier
    });
  }

  const QPoint position = anchor ? anchor->mapToGlobal(QPoint(0, anchor->height()))
                                 : QCursor::pos();
  menu.exec(position);
}

QString AIAssistant::findAgentCLI() const
{
  const QString configured = environmentValue("REPLAYLAB_AI_CLI").trimmed();
  if(!configured.isEmpty() && QFileInfo(configured).isFile())
    return QFileInfo(configured).absoluteFilePath();

  const QString appDir = QCoreApplication::applicationDirPath();
  QStringList candidates;
#if defined(Q_OS_WIN)
  candidates << QDir(appDir).filePath(lit("replaylab-ai.exe"));
  candidates << QDir(appDir).filePath(lit("replaylab/replaylab-ai.exe"));
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/Scripts/replaylab-ai.exe"));
#else
  candidates << QDir(appDir).filePath(lit("replaylab-ai"));
  candidates << QDir(appDir).filePath(lit("replaylab/replaylab-ai"));
  candidates << QDir(appDir).filePath(lit("../../replaylab/.venv/bin/replaylab-ai"));
#endif
  for(const QString &candidate : candidates)
  {
    QFileInfo info(QDir::cleanPath(candidate));
    if(info.isFile())
      return info.absoluteFilePath();
  }

  return QStandardPaths::findExecutable(lit("replaylab-ai"));
}

QString AIAssistant::findBridgeInstance() const
{
  QString local = environmentValue("LOCALAPPDATA");
  if(local.isEmpty())
    local = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
  QDir instances(QDir(local).filePath(lit("ReplayLab/ipc-v2/instances")));
  if(!instances.exists())
    return QString();

  QString bestId;
  qint64 bestHeartbeat = 0;
  const qint64 now = QDateTime::currentMSecsSinceEpoch();
  const qint64 currentPid = QCoreApplication::applicationPid();
  const QFileInfoList directories =
      instances.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Time);
  for(const QFileInfo &directory : directories)
  {
    QFile manifest(QDir(directory.absoluteFilePath()).filePath(lit("instance.json")));
    if(!manifest.open(QIODevice::ReadOnly) || manifest.size() <= 0 ||
       manifest.size() > MaxManifestBytes)
      continue;
    const QJsonDocument document = QJsonDocument::fromJson(manifest.readAll());
    if(!document.isObject())
      continue;
    const QJsonObject object = document.object();
    const qint64 pid = object.value(lit("pid")).toVariant().toLongLong();
    const qint64 heartbeat = object.value(lit("heartbeatUnixMs")).toVariant().toLongLong();
    const QString instanceId = object.value(lit("instanceId")).toString();
    if(object.value(lit("schema")).toString() != lit("replaylab-bridge-instance/v1") ||
       object.value(lit("protocolVersion")).toInt() != 2 ||
       directory.fileName() != instanceId || pid != currentPid ||
       object.value(lit("stopped")).toBool() ||
       !object.value(lit("captureLoaded")).toBool() || !safeTaskId(instanceId) ||
       heartbeat <= 0 || heartbeat > now + 5000 || now - heartbeat > 5000)
      continue;
    if(heartbeat > bestHeartbeat)
    {
      bestHeartbeat = heartbeat;
      bestId = instanceId;
    }
  }
  return bestId;
}

QString AIAssistant::taskEventRoot() const
{
  QString local = environmentValue("LOCALAPPDATA");
  if(local.isEmpty())
    local = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
  return QDir(local).filePath(lit("ReplayLab/ai-tasks"));
}

void AIAssistant::configureProcess(QProcess *process, const QString &credentialProfile) const
{
  QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
  const QProcessEnvironment inherited = environment;

  // A qrenderdoc session can hold credentials for several model APIs. Give each AgentHost process
  // only the key selected by its exact profile; managed subscription profiles receive none of
  // these model credentials. The subscription connector applies a second process allowlist.
  QSet<QString> credentialNames = knownModelCredentialEnvironments();
  for(auto it = m_ProfileKeyEnvs.cbegin(); it != m_ProfileKeyEnvs.cend(); ++it)
    if(!it.value().isEmpty())
      credentialNames.insert(it.value().toUpper());
  for(const QString &name : environment.keys())
    if(credentialNames.contains(name.toUpper()))
      environment.remove(name);

  const QString selectedName = m_ProfileKeyEnvs.value(credentialProfile);
  if(!selectedName.isEmpty())
  {
    if(m_SessionSecrets.contains(credentialProfile))
      environment.insert(selectedName, m_SessionSecrets.value(credentialProfile));
    else if(inherited.contains(selectedName))
      environment.insert(selectedName, inherited.value(selectedName));
  }

  // ReplayLab's stdout/stderr and JSONL protocols are UTF-8. On Windows, a
  // redirected Python process otherwise selects the active ANSI code page
  // (commonly CP936 on Chinese systems), while this class deliberately decodes
  // all process output with QString::fromUtf8. Force a deterministic encoding
  // so localized failures and non-ASCII capture/profile/workspace paths survive
  // the QProcess boundary. Non-Python Codex commands safely ignore these.
  environment.insert(lit("PYTHONUTF8"), lit("1"));
  environment.insert(lit("PYTHONIOENCODING"), lit("utf-8:strict"));
  process->setProcessEnvironment(environment);
#if defined(Q_OS_WIN)
  process->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *args) {
    // CREATE_NO_WINDOW. Keep per-task provider helpers invisible behind qrenderdoc.
    args->flags |= 0x08000000UL;
  });
#endif
}

bool AIAssistant::attachKillOnCloseJob(QProcess *process, void *&jobHandle, QString *error) const
{
  if(error)
    error->clear();
  closeKillOnCloseJob(jobHandle);

#if defined(Q_OS_WIN)
  if(!process || process->state() == QProcess::NotRunning || process->processId() == 0)
  {
    if(error)
      *error = tr("The AI host did not expose a running process for containment.");
    return false;
  }

  HANDLE job = CreateJobObjectW(NULL, NULL);
  if(!job)
  {
    if(error)
      *error = tr("Could not create an AI process job (Windows error %1).")
                   .arg(qulonglong(GetLastError()));
    return false;
  }

  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
  limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
  if(!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)))
  {
    const DWORD code = GetLastError();
    CloseHandle(job);
    if(error)
      *error = tr("Could not configure the AI process job (Windows error %1).")
                   .arg(qulonglong(code));
    return false;
  }

  HANDLE child = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE |
                                 PROCESS_QUERY_LIMITED_INFORMATION,
                             FALSE, DWORD(process->processId()));
  if(!child)
  {
    const DWORD code = GetLastError();
    CloseHandle(job);
    if(error)
      *error = tr("Could not open the AI host for containment (Windows error %1).")
                   .arg(qulonglong(code));
    return false;
  }

  const BOOL assigned = AssignProcessToJobObject(job, child);
  const DWORD assignError = assigned ? ERROR_SUCCESS : GetLastError();
  CloseHandle(child);
  if(!assigned)
  {
    CloseHandle(job);
    if(error)
      *error = tr("Could not contain the AI host process tree (Windows error %1).")
                   .arg(qulonglong(assignError));
    return false;
  }

  jobHandle = job;
#else
  Q_UNUSED(process);
  if(error)
    *error = tr("The AI runtime currently requires Windows process-tree containment.");
  return false;
#endif
  return true;
}

void AIAssistant::closeKillOnCloseJob(void *&jobHandle) const
{
#if defined(Q_OS_WIN)
  if(jobHandle)
    CloseHandle(static_cast<HANDLE>(jobHandle));
#endif
  jobHandle = NULL;
}

QByteArray AIAssistant::runCLISync(const QStringList &arguments, int timeoutMs, int *exitCode,
                                   QString *error, const QString &credentialProfile) const
{
  if(exitCode)
    *exitCode = -1;
  if(error)
    error->clear();
  if(m_CLIPath.isEmpty())
  {
    if(error)
      *error = tr("ReplayLab AI host was not found");
    return QByteArray();
  }

  QProcess process;
  configureProcess(&process, credentialProfile);
  process.start(m_CLIPath, arguments, QIODevice::ReadOnly);
  if(!process.waitForStarted(3000))
  {
    if(error)
      *error = process.errorString();
    return QByteArray();
  }
  void *jobHandle = NULL;
  QString containmentError;
  if(!attachKillOnCloseJob(&process, jobHandle, &containmentError))
  {
    process.kill();
    process.waitForFinished(2000);
    if(error)
      *error = containmentError;
    return QByteArray();
  }
  if(!process.waitForFinished(timeoutMs))
  {
    closeKillOnCloseJob(jobHandle);
    process.kill();
    process.waitForFinished(2000);
    if(error)
      *error = tr("Command timed out");
    return QByteArray();
  }
  closeKillOnCloseJob(jobHandle);
  const bool normalExit = process.exitStatus() == QProcess::NormalExit;
  const int effectiveExitCode = normalExit ? process.exitCode() : -1;
  if(exitCode)
    *exitCode = effectiveExitCode;
  const QByteArray stderrText = process.readAllStandardError();
  if((!normalExit || process.exitCode() != 0) && error)
  {
    *error = QString::fromUtf8(stderrText.left(MaxEventLineBytes)).trimmed();
    if(error->isEmpty())
      *error = normalExit ? tr("Command failed with exit code %1").arg(process.exitCode())
                          : tr("Command terminated unexpectedly");
  }
  if(!normalExit)
    return QByteArray();
  return process.readAllStandardOutput().left(MaxProcessOutputBytes);
}

QByteArray AIAssistant::runCLIInputSync(const QStringList &arguments, const QByteArray &input,
                                        int timeoutMs, int *exitCode, QString *error,
                                        const QString &credentialProfile) const
{
  if(exitCode)
    *exitCode = -1;
  if(error)
    error->clear();
  if(m_CLIPath.isEmpty())
  {
    if(error)
      *error = tr("ReplayLab AI host was not found");
    return QByteArray();
  }
  if(input.size() > MaxPromptBytes)
  {
    if(error)
      *error = tr("Command input exceeds the 16 MiB safety limit");
    return QByteArray();
  }

  QProcess process;
  configureProcess(&process, credentialProfile);
  process.start(m_CLIPath, arguments, QIODevice::ReadWrite);
  if(!process.waitForStarted(3000))
  {
    if(error)
      *error = process.errorString();
    return QByteArray();
  }
  void *jobHandle = NULL;
  QString containmentError;
  if(!attachKillOnCloseJob(&process, jobHandle, &containmentError))
  {
    process.kill();
    process.waitForFinished(2000);
    if(error)
      *error = containmentError;
    return QByteArray();
  }
  if(!input.isEmpty() && process.write(input) != input.size())
  {
    closeKillOnCloseJob(jobHandle);
    process.kill();
    process.waitForFinished(2000);
    if(error)
      *error = tr("Could not send protected command input");
    return QByteArray();
  }
  process.closeWriteChannel();
  if(!process.waitForFinished(timeoutMs))
  {
    closeKillOnCloseJob(jobHandle);
    process.kill();
    process.waitForFinished(2000);
    if(error)
      *error = tr("Command timed out");
    return QByteArray();
  }
  closeKillOnCloseJob(jobHandle);
  const bool normalExit = process.exitStatus() == QProcess::NormalExit;
  const int effectiveExitCode = normalExit ? process.exitCode() : -1;
  if(exitCode)
    *exitCode = effectiveExitCode;
  const QByteArray stderrText = process.readAllStandardError();
  if((!normalExit || process.exitCode() != 0) && error)
  {
    *error = QString::fromUtf8(stderrText.left(MaxEventLineBytes)).trimmed();
    if(error->isEmpty())
      *error = normalExit ? tr("Command failed with exit code %1").arg(process.exitCode())
                          : tr("Command terminated unexpectedly");
  }
  if(!normalExit)
    return QByteArray();
  return process.readAllStandardOutput().left(MaxProcessOutputBytes);
}

void AIAssistant::refreshProfiles()
{
  m_CLIPath = findAgentCLI();
  QSignalBlocker blocker(m_ProfileCombo);
  m_ProfileComboLoading = true;
  const QString previousProfile = m_ActiveProfile;
  const QString previousModel = m_ActiveModel;
  m_ProfileCombo->clear();
  m_ActiveProfile.clear();
  m_ActiveKeyEnv.clear();
  m_ActiveModel.clear();
  m_ProfileKeyEnvs.clear();
  m_ProfileModels.clear();
  m_ProfileDefaultModels.clear();
  m_ProfileRunInputSchemas.clear();
  m_ProfileRows = QJsonArray();

  int exitCode = -1;
  QString error;
  const QJsonDocument document =
      QJsonDocument::fromJson(runCLISync({lit("profile"), lit("list"), lit("--json")}, 5000,
                                        &exitCode, &error));
  if(exitCode == 0 && document.isArray())
  {
    int persistedDefaultIndex = -1;
    int previousIndex = -1;
    const QJsonArray rows = document.array();
    for(const QJsonValue &value : rows)
    {
      const QJsonObject row = value.toObject();
      if(!supportedQRenderDocConnection(row))
        continue;
      m_ProfileRows.append(row);
      const QString id = row.value(lit("id")).toString();    // i18n-ignore: provider protocol key
      if(id.isEmpty())
        continue;
      const QString kind = row.value(lit("kind")).toString();
      const QString loopOwner = row.value(lit("loopOwner")).toString();
      const QString keyEnvironment = row.value(lit("apiKeyEnv")).toString();
      m_ProfileKeyEnvs[id] = keyEnvironment;
      QStringList enabledModels;
      for(const QJsonValue &modelValue : row.value(lit("models")).toArray())
        if(!modelValue.toString().trimmed().isEmpty())
          enabledModels << modelValue.toString().trimmed();
      const QString defaultModel =
          row.value(lit("defaultModel")).toString(row.value(lit("model")).toString());
      if(enabledModels.isEmpty() && !defaultModel.isEmpty())
        enabledModels << defaultModel;
      m_ProfileModels[id] = enabledModels;
      m_ProfileDefaultModels[id] = defaultModel;
      QStringList runInputSchemas;
      for(const QJsonValue &schemaValue :
          row.value(lit("runInputSchemas")).toArray())    // i18n-ignore: AgentHost JSON field name
        if(!schemaValue.toString().trimmed().isEmpty())
          runInputSchemas << schemaValue.toString().trimmed();
      m_ProfileRunInputSchemas[id] = runInputSchemas;
      const QString displayName = connectionDisplayName(row);
      const QString label =
          loopOwner == lit("replaylab") || kind == lit("model")
              ? tr("%1 | API").arg(displayName)
              : tr("%1 | Subscription").arg(displayName);
      m_ProfileCombo->addItem(label, id);
      m_ProfileCombo->setItemData(m_ProfileCombo->count() - 1, keyEnvironment, Qt::UserRole + 1);
      if(id == previousProfile)
        previousIndex = m_ProfileCombo->count() - 1;
      if(row.value(lit("active")).toBool())
        persistedDefaultIndex = m_ProfileCombo->count() - 1;
    }
    int selectedIndex = previousIndex >= 0 ? previousIndex : persistedDefaultIndex;
    if(selectedIndex < 0 && m_ProfileCombo->count() > 0)
      selectedIndex = 0;
    if(selectedIndex >= 0)
    {
      m_ProfileCombo->setCurrentIndex(selectedIndex);
      m_ActiveProfile = m_ProfileCombo->currentData().toString();
      m_ActiveKeyEnv = m_ProfileCombo->currentData(Qt::UserRole + 1).toString();
      if(m_ActiveProfile == previousProfile &&
         m_ProfileModels.value(m_ActiveProfile).contains(previousModel))
        m_ActiveModel = previousModel;
      else
        m_ActiveModel = m_ProfileDefaultModels.value(m_ActiveProfile);
    }
  }
  if(m_ProfileCombo->count() == 0)
  {
    m_ProfileCombo->addItem(tr("No connection configured"), QString());
    if(!error.isEmpty())
      m_ProfileCombo->setToolTip(error);
  }
  m_ProfileComboLoading = false;
  refreshModelCombo();
  refreshConnectionStatus();
}

void AIAssistant::refreshModelCombo()
{
  if(!m_ModelCombo)
    return;
  QSignalBlocker blocker(m_ModelCombo);
  m_ModelCombo->clear();
  const QStringList models = m_ProfileModels.value(m_ActiveProfile);
  for(const QString &model : models)
    m_ModelCombo->addItem(model, model);
  if(m_ModelCombo->count() == 0)
  {
    m_ModelCombo->addItem(tr("Provider default"), QString());
    m_ModelCombo->setEnabled(false);
    m_ActiveModel.clear();
    return;
  }
  m_ModelCombo->setEnabled(true);
  int index = m_ModelCombo->findData(m_ActiveModel);
  if(index < 0)
    index = m_ModelCombo->findData(m_ProfileDefaultModels.value(m_ActiveProfile));
  if(index < 0)
    index = 0;
  m_ModelCombo->setCurrentIndex(index);
  m_ActiveModel = m_ModelCombo->currentData().toString();
}

void AIAssistant::selectProfile(int index)
{
  if(m_ProfileComboLoading || index < 0)
    return;
  const QString profile = m_ProfileCombo->itemData(index).toString();
  if(profile.isEmpty() || profile == m_ActiveProfile)
    return;
  if(!m_RunningTasks.isEmpty())
  {
    appendSystem(tr("Wait for active AI tasks before switching connections."), true);
    QSignalBlocker blocker(m_ProfileCombo);
    const int activeIndex = m_ProfileCombo->findData(m_ActiveProfile);
    if(activeIndex >= 0)
      m_ProfileCombo->setCurrentIndex(activeIndex);
    return;
  }

  // The picker beside Send is session-scoped. Persisting the default is an explicit operation in
  // Settings > AI, so exploratory model/provider switching never rewrites user configuration.
  m_ActiveProfile = profile;
  m_ActiveKeyEnv = m_ProfileCombo->itemData(index, Qt::UserRole + 1).toString();
  m_ActiveModel = m_ProfileDefaultModels.value(profile);
  refreshModelCombo();
  appendSystem(tr("Active connection: %1").arg(m_ProfileCombo->itemText(index)));
  refreshConnectionStatus();
}

void AIAssistant::selectModel(int index)
{
  if(m_ProfileComboLoading || index < 0 || !m_ModelCombo)
    return;
  const QString model = m_ModelCombo->itemData(index).toString();
  if(model == m_ActiveModel)
    return;
  if(!m_RunningTasks.isEmpty())
  {
    appendSystem(tr("Wait for active AI tasks before switching models."), true);
    QSignalBlocker blocker(m_ModelCombo);
    const int activeIndex = m_ModelCombo->findData(m_ActiveModel);
    if(activeIndex >= 0)
      m_ModelCombo->setCurrentIndex(activeIndex);
    return;
  }
  m_ActiveModel = model;
  appendSystem(model.isEmpty() ? tr("Using the provider's default model.")
                               : tr("Model for new tasks: %1").arg(model));
  refreshConnectionStatus();
}

bool AIAssistant::connectionMutationAllowed(QWidget *parent) const
{
  if(m_RunningTasks.isEmpty())
    return true;

  QMessageBox::warning(parent, tr("AI Connections"),
                       tr("Wait for active AI tasks before changing connections."));
  return false;
}

QWidget *AIAssistant::CreateSettingsPage(QWidget *parent)
{
  QWidget *page = new QWidget(parent);
  // The normal MainWindow Settings entry does not pass through manageConnections(). Mark the
  // shared configuration surface busy for its whole lifetime so profile edits and subscription
  // setup cannot race active task state.
  m_ConnectionDialogOpen = true;
  connect(page, &QObject::destroyed, this, [this]() {
    m_ConnectionDialogOpen = false;
    if(!m_ShuttingDown)
      refreshConnectionStatus();
  });
  QVBoxLayout *root = new QVBoxLayout(page);
  root->setContentsMargins(8, 8, 8, 8);

  QLabel *intro = new QLabel(
      tr("Add model APIs or Agent subscriptions here. API connections use the same RenderDoc "
         "Agent planner, Skills and evidence checks. Managed subscriptions use the same task "
         "contract around the vendor Agent. API keys can be stored securely in Windows Credential "
         "Manager or kept only for the current qrenderdoc session."),
      page);
  intro->setWordWrap(true);
  root->addWidget(intro);

  QHBoxLayout *body = new QHBoxLayout();
  QListWidget *connections = new QListWidget(page);
  connections->setObjectName(lit("AIConnectionList"));
  connections->setMinimumWidth(250);
  body->addWidget(connections, 1);

  QVBoxLayout *actions = new QVBoxLayout();
  QPushButton *add = new QPushButton(tr("Add..."), page);
  QPushButton *edit = new QPushButton(tr("Edit..."), page);
  QPushButton *remove = new QPushButton(tr("Remove"), page);
  QPushButton *makeDefault = new QPushButton(tr("Set as Default"), page);
  QPushButton *check = new QPushButton(tr("Check Connection"), page);
  QPushButton *selfTest = new QPushButton(tr("Run Protocol Self-Test"), page);
  actions->addWidget(add);
  actions->addWidget(edit);
  actions->addWidget(remove);
  actions->addSpacing(8);
  actions->addWidget(makeDefault);
  actions->addWidget(check);
  actions->addWidget(selfTest);
  actions->addStretch(1);
  body->addLayout(actions);

  QGroupBox *detailsBox = new QGroupBox(tr("Connection details"), page);
  QVBoxLayout *detailsLayout = new QVBoxLayout(detailsBox);
  QLabel *details = new QLabel(tr("Select a connection, or add one to get started."), detailsBox);
  details->setWordWrap(true);
  details->setTextInteractionFlags(Qt::TextSelectableByMouse);
  detailsLayout->addWidget(details);
  detailsLayout->addStretch(1);
  body->addWidget(detailsBox, 2);
  root->addLayout(body, 1);

  QLabel *footer = new QLabel(
      tr("The connection name is for display. qrenderdoc creates an internal stable identifier "
         "automatically. Models enabled here appear beside Send in the RenderDoc AI panel. The "
         "first connection becomes the default automatically. Protocol Self-Test uses only "
         "local fake peers and no account; use Fetch Models for a real API check, or Edit then "
         "Connect/Test for a real subscription check."),
      page);
  footer->setWordWrap(true);
  root->addWidget(footer);

  auto profileById = [this](const QString &id) {
    for(const QJsonValue &value : m_ProfileRows)
      if(value.toObject().value(lit("id")).toString() == id)
        return value.toObject();
    return QJsonObject();
  };

  std::function<void()> updateSelection = [=]() {
    const bool selected = connections->currentItem() != NULL;
    edit->setEnabled(selected);
    remove->setEnabled(selected);
    makeDefault->setEnabled(selected);
    check->setEnabled(selected);
    if(!selected)
    {
      details->setText(tr("Select a connection, or add one to get started."));
      return;
    }
    const QJsonObject row = profileById(connections->currentItem()->data(Qt::UserRole).toString());
    const bool api = row.value(lit("kind")).toString() == lit("model");
    const QStringList models = m_ProfileModels.value(row.value(lit("id")).toString());
    QString text = tr("<b>%1</b><br>Type: %2<br>Provider: %3<br>Endpoint: %4<br>"
                      "Default model: %5<br>Enabled models: %6")
                       .arg(connectionDisplayName(row).toHtmlEscaped(),
                            api ? tr("Model API") : tr("Agent subscription"),
                            row.value(lit("preset")).toString().toHtmlEscaped(),
                            row.value(lit("endpoint")).toString().isEmpty()
                                ? tr("Managed by provider")
                                : row.value(lit("endpoint")).toString().toHtmlEscaped(),
                            m_ProfileDefaultModels.value(row.value(lit("id")).toString()).isEmpty()
                                ? tr("Provider default")
                                : m_ProfileDefaultModels.value(row.value(lit("id")).toString())
                                      .toHtmlEscaped(),
                            models.isEmpty() ? tr("Provider default")
                                             : models.join(lit(", ")).toHtmlEscaped());
    if(row.value(lit("active")).toBool())
      text += tr("<br><b>Default connection</b>");
    details->setText(text);
  };

  std::function<void(const QString &)> reloadList = [=](const QString &preferred) {
    connections->clear();
    int preferredRow = -1;
    int activeRow = -1;
    for(const QJsonValue &value : m_ProfileRows)
    {
      const QJsonObject row = value.toObject();
      const QString id = row.value(lit("id")).toString();
      const bool api = row.value(lit("kind")).toString() == lit("model");
      QString label = connectionDisplayName(row);
      label += api ? tr("  -  API") : tr("  -  Subscription");
      if(row.value(lit("active")).toBool())
        label.prepend(lit("* "));
      QListWidgetItem *item = new QListWidgetItem(label, connections);
      item->setData(Qt::UserRole, id);
      if(id == preferred)
        preferredRow = connections->count() - 1;
      if(row.value(lit("active")).toBool())
        activeRow = connections->count() - 1;
    }
    const int row = preferredRow >= 0 ? preferredRow : activeRow;
    if(row >= 0)
      connections->setCurrentRow(row);
    else if(connections->count() > 0)
      connections->setCurrentRow(0);
    updateSelection();
  };

  connect(connections, &QListWidget::itemSelectionChanged, page, updateSelection);

  std::function<void(const QJsonObject &)> editConnection =
      [this, page, reloadList](const QJsonObject &existing) {
        int presetExit = -1;
        QString presetError;
        const QJsonDocument presetDocument = QJsonDocument::fromJson(
            runCLISync({lit("presets"), lit("--json")}, 5000, &presetExit, &presetError));
        if(presetExit != 0 || !presetDocument.isArray())
        {
          QMessageBox::critical(page, tr("AI Connections"),
                                presetError.isEmpty() ? tr("Could not load AI providers.")
                                                      : presetError);
          return;
        }
        QJsonArray presets;
        for(const QJsonValue &value : presetDocument.array())
        {
          const QJsonObject preset = value.toObject();
          if(supportedQRenderDocConnection(preset))
            presets.append(preset);
        }
        const bool editing = !existing.isEmpty();
        QString existingId = existing.value(lit("id")).toString();
        bool profileExists = editing;
        bool profileSavedDuringDialog = false;

        QDialog dialog(page);
        dialog.setWindowTitle(editing ? tr("Edit AI Connection") : tr("Add AI Connection"));
        dialog.resize(680, 650);
        QVBoxLayout *dialogRoot = new QVBoxLayout(&dialog);
        QFormLayout *general = new QFormLayout();
        QLineEdit *name = new QLineEdit(&dialog);
        name->setPlaceholderText(tr("For example: Work OpenAI or Personal Codex"));
        QComboBox *mode = new QComboBox(&dialog);
        mode->addItem(tr("Model API"), lit("model"));
        mode->addItem(QCoreApplication::translate("AIAssistant",
                                                  "Codex / ChatGPT subscription",
                                                  "provider.codex.title"),
                      lit("agent"));    // i18n-ignore: provider kind stored as item data.
        QCheckBox *defaultConnection =
            new QCheckBox(tr("Make default after saving"), &dialog);
        general->addRow(tr("Connection name"), name);
        general->addRow(tr("Connection type"), mode);
        general->addRow(QString(), defaultConnection);
        dialogRoot->addLayout(general);

        QStackedWidget *modePages = new QStackedWidget(&dialog);
        QWidget *apiPage = new QWidget(modePages);
        QFormLayout *apiForm = new QFormLayout(apiPage);
        QComboBox *apiProvider = new QComboBox(apiPage);
        QLineEdit *baseUrl = new QLineEdit(apiPage);
        baseUrl->setPlaceholderText(tr("Provider API base URL"));
        QLineEdit *apiKey = new QLineEdit(apiPage);
        apiKey->setEchoMode(QLineEdit::Password);
        apiKey->setPlaceholderText(editing ? tr("Leave blank to keep the existing credential")
                                           : tr("Enter the provider API key"));
        QCheckBox *rememberKey = new QCheckBox(
            tr("Securely remember this key in Windows Credential Manager"), apiPage);
        rememberKey->setChecked(true);
        QLabel *credentialStatus = new QLabel(apiPage);
        credentialStatus->setWordWrap(true);
        credentialStatus->setTextInteractionFlags(Qt::TextSelectableByMouse);
        setDiagnosticStatus(
            credentialStatus, DiagnosticLevel::Info,
            editing ? tr("Checking for a saved credential...")
                    : tr("No saved credential yet. Enter a key to save it securely."));
        QPushButton *forgetCredential = new QPushButton(tr("Forget Saved Key"), apiPage);
        forgetCredential->setEnabled(false);
        QHBoxLayout *credentialStatusLayout = new QHBoxLayout();
        credentialStatusLayout->setContentsMargins(0, 0, 0, 0);
        credentialStatusLayout->addWidget(credentialStatus, 1);
        credentialStatusLayout->addWidget(forgetCredential);
        QWidget *credentialStatusWidget = new QWidget(apiPage);
        credentialStatusWidget->setLayout(credentialStatusLayout);
        QLineEdit *azureVersion = new QLineEdit(apiPage);
        azureVersion->setPlaceholderText(tr("Required only by legacy Azure deployment URLs"));
        QLineEdit *vertexProject = new QLineEdit(apiPage);
        vertexProject->setPlaceholderText(tr("Google Cloud project ID"));
        QLineEdit *vertexLocation = new QLineEdit(apiPage);
        vertexLocation->setPlaceholderText(tr("For example: us-central1 or global"));
        QLabel *apiHint = new QLabel(apiPage);
        apiHint->setWordWrap(true);
        apiForm->addRow(tr("Provider"), apiProvider);
        apiForm->addRow(tr("API address"), baseUrl);
        apiForm->addRow(tr("API key"), apiKey);
        apiForm->addRow(QString(), rememberKey);
        apiForm->addRow(tr("Saved key"), credentialStatusWidget);
        apiForm->addRow(tr("Azure API version"), azureVersion);
        apiForm->addRow(tr("Vertex project"), vertexProject);
        apiForm->addRow(tr("Vertex location"), vertexLocation);
        apiForm->addRow(QString(), apiHint);
        modePages->addWidget(apiPage);

        QWidget *agentPage = new QWidget(modePages);
        QFormLayout *agentForm = new QFormLayout(agentPage);
        QComboBox *agentProvider = new QComboBox(agentPage);
        QTextBrowser *authStatus = new QTextBrowser(agentPage);
        authStatus->setTextInteractionFlags(Qt::TextBrowserInteraction |
                                            Qt::TextSelectableByKeyboard);
        authStatus->setOpenExternalLinks(false);
        authStatus->setOpenLinks(false);
        authStatus->setFrameShape(QFrame::NoFrame);
        authStatus->setMinimumHeight(58);
        authStatus->setMaximumHeight(110);
        setDiagnosticStatus(
            authStatus, DiagnosticLevel::Info,
            tr("Save this connection, then reopen it here to sign in with the provider."));
        QHBoxLayout *loginButtons = new QHBoxLayout();
        QComboBox *authFlow = new QComboBox(agentPage);
        authFlow->addItem(tr("Browser sign-in"), lit("browser"));
        authFlow->addItem(tr("Device code"), lit("device"));
        QPushButton *connectSubscription =
            new QPushButton(tr("Connect / Sign in and Test"), agentPage);
        QPushButton *cancelSubscription = new QPushButton(tr("Cancel"), agentPage);
        cancelSubscription->setVisible(false);
        loginButtons->addWidget(authFlow);
        loginButtons->addWidget(connectSubscription);
        loginButtons->addWidget(cancelSubscription);
        loginButtons->addStretch(1);
        QWidget *loginWidget = new QWidget(agentPage);
        loginWidget->setLayout(loginButtons);
        QLabel *agentHint = new QLabel(agentPage);
        agentHint->setWordWrap(true);
        agentForm->addRow(tr("Provider"), agentProvider);
        agentForm->addRow(tr("Authentication"), authStatus);
        agentForm->addRow(QString(), loginWidget);
        agentForm->addRow(QString(), agentHint);
        modePages->addWidget(agentPage);
        dialogRoot->addWidget(modePages);

        QGroupBox *modelsBox = new QGroupBox(tr("Models available in the chat selector"), &dialog);
        QVBoxLayout *modelsRoot = new QVBoxLayout(modelsBox);
        QLabel *modelHint = new QLabel(
            tr("Fetch the provider's model list, then enable only the models you want to switch "
               "between. You can also add an exact model or deployment name manually."),
            modelsBox);
        modelHint->setWordWrap(true);
        modelsRoot->addWidget(modelHint);
        QListWidget *models = new QListWidget(modelsBox);
        models->setMinimumHeight(120);
        modelsRoot->addWidget(models, 1);
        QHBoxLayout *modelActions = new QHBoxLayout();
        QPushButton *fetchModels = new QPushButton(tr("Fetch Models"), modelsBox);
        QLineEdit *customModel = new QLineEdit(modelsBox);
        customModel->setPlaceholderText(tr("Exact model or deployment name"));
        QPushButton *addModel = new QPushButton(tr("Add"), modelsBox);
        modelActions->addWidget(fetchModels);
        modelActions->addWidget(customModel, 1);
        modelActions->addWidget(addModel);
        modelsRoot->addLayout(modelActions);
        QFormLayout *defaultModelForm = new QFormLayout();
        QComboBox *defaultModel = new QComboBox(modelsBox);
        defaultModelForm->addRow(tr("Default model"), defaultModel);
        modelsRoot->addLayout(defaultModelForm);
        dialogRoot->addWidget(modelsBox, 1);

        QDialogButtonBox *buttons =
            new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, &dialog);
        dialogRoot->addWidget(buttons);
        connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);

        auto presetObject = [&presets](const QString &id) {
          for(const QJsonValue &value : presets)
            if(value.toObject().value(lit("id")).toString() == id)
              return value.toObject();
          return QJsonObject();
        };
        auto populateProviders = [&]() {
          const QString kind = mode->currentData().toString();
          QComboBox *combo = kind == lit("model") ? apiProvider : agentProvider;
          const QString wanted = existing.value(lit("preset")).toString();
          QSignalBlocker blocker(combo);
          combo->clear();
          int selected = -1;
          for(const QJsonValue &value : presets)
          {
            const QJsonObject preset = value.toObject();
            if(preset.value(lit("kind")).toString() != kind)
              continue;
            combo->addItem(localizeProviderCatalogField(preset, "title"),
                           preset.value(lit("id")).toString());
            if(preset.value(lit("id")).toString() == wanted)
              selected = combo->count() - 1;
          }
          if(selected >= 0)
            combo->setCurrentIndex(selected);
          modePages->setCurrentIndex(kind == lit("model") ? 0 : 1);
        };
        auto selectedPresetId = [&]() {
          return mode->currentData().toString() == lit("model")
                     ? apiProvider->currentData().toString()
                     : agentProvider->currentData().toString();
        };
        QString lastPresetId;
        bool hasStoredCredential = false;
        bool credentialStoreSupported = true;
        bool subscriptionAuthenticated = false;
        auto refreshCredentialStatus = [&]() {
          if(!editing)
          {
            hasStoredCredential = false;
            credentialStoreSupported = true;
            setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                tr("No saved credential yet. Enter a key to save it securely."));
            forgetCredential->setEnabled(false);
            return;
          }
          int credentialExit = -1;
          QString credentialError;
          const QJsonDocument result = QJsonDocument::fromJson(runCLISync(
              {lit("credential"), lit("status"), lit("--profile"), existingId, lit("--json")},
              5000, &credentialExit, &credentialError));
          if(credentialExit != 0 || !result.isObject())
          {
            hasStoredCredential = false;
            setDiagnosticStatus(
                credentialStatus, DiagnosticLevel::Warning,
                credentialError.isEmpty() ? tr("Saved credential status is unavailable.")
                                           : credentialError);
            forgetCredential->setEnabled(false);
            return;
          }
          const QJsonObject payload = result.object();
          const QString state = payload.value(lit("status")).toString();
          credentialStoreSupported = payload.value(lit("supported")).toBool(true);
          hasStoredCredential = payload.value(lit("stored")).toBool() ||
                                state == lit("stored") || state == lit("available");
          if(!credentialStoreSupported)
            setDiagnosticStatus(
                credentialStatus, DiagnosticLevel::Warning,
                tr("Secure credential storage is not available on this platform; the key can be "
                   "used for this session only."));
          else if(hasStoredCredential)
            setDiagnosticStatus(
                credentialStatus, DiagnosticLevel::Success,
                tr("A key is securely saved in Windows Credential Manager. Leave the key field "
                   "empty to keep it, or enter a new key to replace it."));
          else if(m_SessionSecrets.contains(existingId))
            setDiagnosticStatus(
                credentialStatus, DiagnosticLevel::Warning,
                tr("A session-only key is active. Save with secure remembering enabled to move "
                   "it into Windows Credential Manager."));
          else
            setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                                tr("No key is saved in Windows Credential Manager."));
          forgetCredential->setEnabled(hasStoredCredential);
          rememberKey->setEnabled(credentialStoreSupported);
          if(!credentialStoreSupported)
            rememberKey->setChecked(false);
          rememberKey->setChecked(hasStoredCredential || rememberKey->isChecked());
        };
        auto deleteStoredCredential = [&](const QString &profile, QString *deleteError) {
          int deleteExit = -1;
          runCLISync({lit("credential"), lit("delete"), lit("--profile"), profile, lit("--json")},
                     5000, &deleteExit, deleteError);
          return deleteExit == 0;
        };
        connect(forgetCredential, &QPushButton::clicked, &dialog, [&]() {
          if(!editing || !connectionMutationAllowed(&dialog))
            return;
          if(QMessageBox::question(
                 &dialog, tr("Forget Saved API Key"),
                 tr("Remove this connection's key from Windows Credential Manager?")) !=
             QMessageBox::Yes)
            return;
          QString forgetError;
          if(!deleteStoredCredential(existingId, &forgetError))
          {
            QMessageBox::critical(
                &dialog, tr("Could not forget API key"),
                forgetError.isEmpty() ? tr("Windows Credential Manager rejected the request.")
                                      : forgetError);
            return;
          }
          m_SessionSecrets.remove(existingId);
          hasStoredCredential = false;
          rememberKey->setChecked(false);
          forgetCredential->setEnabled(false);
          setDiagnosticStatus(credentialStatus, DiagnosticLevel::Warning,
                              tr("No key is saved in Windows Credential Manager."));
          apiKey->clear();
        });
        auto updateProvider = [&]() {
          const QString currentPresetId = selectedPresetId();
          const bool providerSelectionChanged = !lastPresetId.isEmpty() &&
                                                lastPresetId != currentPresetId;
          if(providerSelectionChanged)
          {
            // Model ids and typed credentials are provider-scoped. Never carry either across a
            // provider change merely because the stable connection id is retained.
            models->clear();
            defaultModel->clear();
            customModel->clear();
            apiKey->clear();
            hasStoredCredential = false;
            credentialStoreSupported = true;
            rememberKey->setEnabled(true);
            setDiagnosticStatus(
                credentialStatus, DiagnosticLevel::Info,
                tr("Save this connection before storing a provider credential."));
            forgetCredential->setEnabled(false);
            subscriptionAuthenticated = false;
          }
          lastPresetId = currentPresetId;
          const QJsonObject preset = presetObject(currentPresetId);
          if(mode->currentData().toString() == lit("model"))
          {
            connectSubscription->setEnabled(false);
            modelHint->setText(
                tr("Fetch the provider's model list, then enable only the models you want to "
                   "switch between. You can also add an exact model or deployment name manually."));
            if(!editing)
            {
              QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
              if(saveButton)
                saveButton->setText(tr("Save"));
            }
            fetchModels->setEnabled(true);
            if(!editing || providerSelectionChanged)
              baseUrl->setText(editing &&
                                       existing.value(lit("preset")).toString() == currentPresetId
                                   ? existing.value(lit("endpoint")).toString()
                                   : preset.value(lit("baseUrl")).toString());
            const bool usesAPIKey = preset.value(lit("auth")).toString() == lit("api-key");
            apiKey->setEnabled(usesAPIKey);
            rememberKey->setVisible(usesAPIKey);
            credentialStatusWidget->setVisible(usesAPIKey);
            const bool azure = selectedPresetId() == lit("azure-openai");
            const QString endpoint = baseUrl->text().trimmed().toLower();
            const bool azureV1 = endpoint.endsWith(lit("/v1")) ||
                                 endpoint.contains(lit("/openai/v1")) ||
                                 endpoint.contains(lit("services.ai.azure.com"));
            azureVersion->setVisible(azure && !azureV1);
            apiForm->labelForField(azureVersion)->setVisible(azure && !azureV1);
            if(azureV1)
              azureVersion->clear();
            const bool vertex = selectedPresetId() == lit("vertex");
            vertexProject->setVisible(vertex);
            vertexLocation->setVisible(vertex);
            apiForm->labelForField(vertexProject)->setVisible(vertex);
            apiForm->labelForField(vertexLocation)->setVisible(vertex);
            apiHint->setText(localizeProviderCatalogField(preset, "note"));
          }
          else
          {
            connectSubscription->setEnabled(true);
            loginWidget->setVisible(true);
            authFlow->setVisible(true);
            connectSubscription->setText(tr("Connect / Sign in and Test"));
            connectSubscription->setToolTip(
                profileExists
                    ? tr("Start the provider's official authentication flow and then run a "
                         "no-prompt connection handshake.")
                    : tr("Save this connection and immediately start the provider's official "
                         "authentication and no-prompt handshake."));
            if(!editing)
            {
              QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
              if(saveButton)
                saveButton->setText(tr("Save and Connect"));
            }
            fetchModels->setEnabled(editing);
            modelHint->setText(
                tr("Fetch the provider's model list, then enable only the models you want to "
                   "switch between. You can also add an exact model or deployment name manually."));
            agentHint->setText(localizeProviderCatalogField(preset, "note"));
          }
        };
        auto updateDefaultModels = [&]() {
          const QString previous = defaultModel->currentData().toString();
          QSignalBlocker blocker(defaultModel);
          defaultModel->clear();
          for(int i = 0; i < models->count(); ++i)
          {
            QListWidgetItem *item = models->item(i);
            if(item->checkState() == Qt::Checked)
              defaultModel->addItem(item->text(), item->text());
          }
          int index = defaultModel->findData(previous);
          if(index < 0)
            index = defaultModel->findData(
                existing.value(lit("defaultModel")).toString(existing.value(lit("model")).toString()));
          if(index < 0 && defaultModel->count() > 0)
            index = 0;
          defaultModel->setCurrentIndex(index);
        };
        auto addModelItem = [&](const QString &model, bool enabled) {
          const QString trimmed = model.trimmed();
          if(trimmed.isEmpty())
            return;
          for(int i = 0; i < models->count(); ++i)
          {
            if(models->item(i)->text() == trimmed)
            {
              if(enabled)
                models->item(i)->setCheckState(Qt::Checked);
              return;
            }
          }
          QListWidgetItem *item = new QListWidgetItem(trimmed, models);
          item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
          item->setCheckState(enabled ? Qt::Checked : Qt::Unchecked);
        };

        connect(mode, static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged),
                &dialog, [&](int) {
                  populateProviders();
                  updateProvider();
                  fetchModels->setText(tr("Fetch Models"));
                });
        connect(apiProvider,
                static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), &dialog,
                [&](int) { updateProvider(); });
        connect(baseUrl, &QLineEdit::textChanged, &dialog, [&](const QString &) {
          if(selectedPresetId() == lit("azure-openai"))
            updateProvider();
        });
        connect(agentProvider,
                static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), &dialog,
                [&](int) { updateProvider(); });
        connect(models, &QListWidget::itemChanged, &dialog,
                [&](QListWidgetItem *) { updateDefaultModels(); });
        connect(addModel, &QPushButton::clicked, &dialog, [&]() {
          addModelItem(customModel->text(), true);
          customModel->clear();
          updateDefaultModels();
        });
        connect(customModel, &QLineEdit::returnPressed, addModel, &QPushButton::click);

        connect(fetchModels, &QPushButton::clicked, &dialog, [&]() {
          QStringList arguments = {lit("models")};
          QByteArray protectedInput;
          QString credentialProfile;
          if(profileExists && apiKey->text().isEmpty())
          {
            arguments << lit("--profile") << existingId;
            credentialProfile = existingId;
          }
          else
          {
            arguments << lit("--preset") << selectedPresetId();
            if(mode->currentData().toString() == lit("model") &&
               !baseUrl->text().trimmed().isEmpty())
              arguments << lit("--base-url") << baseUrl->text().trimmed();
            if(!apiKey->text().isEmpty())
            {
              arguments << lit("--api-key-stdin");
              protectedInput = apiKey->text().toUtf8();
            }
          }
          arguments << lit("--json");
          fetchModels->setEnabled(false);
          fetchModels->setText(tr("Fetching..."));
          int modelExit = -1;
          QString modelError;
          const QJsonDocument result = QJsonDocument::fromJson(
              runCLIInputSync(arguments, protectedInput, 25000, &modelExit, &modelError,
                              credentialProfile));
          fetchModels->setEnabled(true);
          fetchModels->setText(tr("Fetch Models"));
          if(modelExit != 0 || !result.isObject())
          {
            QMessageBox::warning(&dialog, tr("Model discovery"),
                                 modelError.isEmpty()
                                     ? tr("The provider did not return a model list. You can add "
                                          "an exact model name manually.")
                                     : modelError);
            return;
          }
          const QJsonObject payload = result.object();
          const QStringList enabledModels = m_ProfileModels.value(existingId);
          QSet<QString> enabled;
          for(const QString &enabledModel : enabledModels)
            enabled.insert(enabledModel);
          models->clear();
          for(const QJsonValue &value : payload.value(lit("models")).toArray())
            addModelItem(value.toString(),
                         enabled.contains(value.toString()) || (!editing && models->count() == 0));
          if(mode->currentData().toString() == lit("model") &&
             !payload.value(lit("endpoint")).toString().isEmpty())
            baseUrl->setText(payload.value(lit("endpoint")).toString());
          updateDefaultModels();
          if(profileSavedDuringDialog && models->count() > 0)
          {
            const QString discoveredDefault = defaultModel->currentData().toString();
            QStringList modelArguments = {lit("profile"), lit("models"), existingId};
            for(int i = 0; i < models->count(); ++i)
              if(models->item(i)->checkState() == Qt::Checked)
                modelArguments << lit("--allowed-model") << models->item(i)->text();
            if(!discoveredDefault.isEmpty())
              modelArguments << lit("--default-model") << discoveredDefault;
            modelArguments << lit("--json");
            int persistExit = -1;
            QString persistError;
            runCLISync(modelArguments, 7000, &persistExit, &persistError);
            if(persistExit != 0)
              setDiagnosticStatus(
                  authStatus, DiagnosticLevel::Warning,
                  persistError.isEmpty() ? tr("Connected, but the discovered model selection "
                                                "could not be saved.")
                                         : persistError);
            else
              refreshProfiles();
          }
        });

        QUrl currentAuthUrl;
        QString currentDeviceCode;
        std::shared_ptr<SubscriptionProcessState> activeAuthOperation;
        bool dialogClosing = false;
        connect(&dialog, &QDialog::finished, &dialog,
                [&](int) { dialogClosing = true; });
        connect(authStatus, &QTextBrowser::anchorClicked, &dialog, [&](const QUrl &url) {
          if(!safeProviderAuthUrl(url))
          {
            setDiagnosticStatus(authStatus, DiagnosticLevel::Error,
                                tr("The provider returned an unsafe authorization URL. It was not "
                                   "opened."));
            return;
          }
          if(!QDesktopServices::openUrl(url))
            setAuthLinkStatus(
                authStatus, DiagnosticLevel::Warning,
                tr("The system browser could not be opened automatically. Use the verified link "
                   "below to continue sign-in."),
                url, currentDeviceCode);
          else
            setAuthLinkStatus(
                authStatus, DiagnosticLevel::Info,
                tr("The verified sign-in link was opened in your browser. The link remains "
                   "available below if you need to copy it."),
                url, currentDeviceCode);
        });
        auto updateAuthStatus = [&]() {
          if(!profileExists)
            return;
          int authExit = -1;
          QString authError;
          const QJsonDocument status = parseJSONOrLastJSONLRecord(
              runCLISync({lit("subscription"), lit("status"), lit("--profile"), existingId,
                          lit("--json")},
                         7000, &authExit, &authError, existingId));
          if(authExit == 0 && status.isObject())
          {
            const QJsonObject value = status.object();
            const QJsonObject data = value.value(lit("data")).toObject();
            const QString state = value.value(lit("status")).toString(
                data.value(lit("status")).toString());
            // i18n-ignore: ReplayLab status JSON field names; known values are localized below.
            const QString rawMessage = value.value(lit("message")).toString(
                data.value(lit("message")).toString(value.value(lit("installHint")).toString(
                    data.value(lit("installHint")).toString())));
            // i18n-ignore: Stable ReplayLab runtime-message protocol fields.
            const QString messageId =
                value.value(lit("messageId")).toString(    // i18n-ignore: ReplayLab protocol key
                    data.value(lit("messageId")).toString());    // i18n-ignore: ReplayLab protocol key
            QJsonArray messageArgs =
                value.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
            if(messageArgs.isEmpty())
              messageArgs =
                  data.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
            const QString message = localizeProviderRuntimeMessage(
                selectedPresetId(), messageId, messageArgs, rawMessage);
            const bool installed = value.contains(lit("installed"))
                                       ? value.value(lit("installed")).toBool()
                                       : data.value(lit("installed")).toBool(true);
            // Preserve a successful connection/test from this UI session unless the backend
            // provides an explicit positive or negative authentication result.
            if(!installed)
              subscriptionAuthenticated = false;
            else if(value.value(lit("authenticated")).isBool())
              subscriptionAuthenticated = value.value(lit("authenticated")).toBool();
            else if(data.value(lit("authenticated")).isBool())
              subscriptionAuthenticated = data.value(lit("authenticated")).toBool();
            else if(state == lit("authenticated") || state == lit("connected"))
              subscriptionAuthenticated = true;
            else if(state == lit("unauthenticated") || state == lit("disconnected"))
              subscriptionAuthenticated = false;
            QString summary;
            if(!installed)
              summary = message.isEmpty()
                            ? tr("The official provider CLI is not installed. Use the provider's "
                                 "official installation instructions, then try again.")
                            : message;
            else if(subscriptionAuthenticated)
              summary = message.isEmpty() ? tr("Connected and authenticated") : message;
            else
              summary = message.isEmpty()
                            ? (state.isEmpty() ? tr("Ready to connect") : state)
                            : message;
            QJsonObject account = value.value(lit("account")).toObject();
            if(account.isEmpty())
              account = data.value(lit("account")).toObject();
            const QString accountName = account.value(lit("email")).toString(
                account.value(lit("name")).toString());
            if(!accountName.isEmpty())
              summary += tr("\nAccount: %1").arg(accountName);
            setDiagnosticStatus(authStatus,
                                !installed ? DiagnosticLevel::Error
                                           : subscriptionAuthenticated
                                                 ? DiagnosticLevel::Success
                                                 : DiagnosticLevel::Warning,
                                summary, state);
            connectSubscription->setText(
                subscriptionAuthenticated
                    ? tr("Test Connection")
                    : tr("Connect / Sign in and Test"));
          }
          else
          {
            subscriptionAuthenticated = false;
            setDiagnosticStatus(
                authStatus, DiagnosticLevel::Warning,
                authError.isEmpty() ? tr("Authentication status unavailable") : authError);
          }
        };
        auto startSubscriptionOperation = [&](const QString &operation, const QString &flow) {
          if(!profileExists || activeAuthOperation || !connectionMutationAllowed(&dialog))
            return;
          const std::shared_ptr<SubscriptionProcessState> state =
              std::make_shared<SubscriptionProcessState>();
          state->operation = operation;
          QProcess *processObject = new QProcess(&dialog);
          state->process = processObject;
          activeAuthOperation = state;
          configureProcess(processObject, existingId);
          connectSubscription->setEnabled(false);
          cancelSubscription->setVisible(true);
          cancelSubscription->setEnabled(true);
          currentAuthUrl.clear();
          currentDeviceCode.clear();
          setDiagnosticStatus(authStatus, DiagnosticLevel::Info,
                              operation == lit("test") ? tr("Testing provider connection...")
                                                        : tr("Starting sign-in..."));

          const auto stopForFailure = [this, state, authStatus,
                                       cancelSubscription](const QString &detail) {
            if(state->failureDetail.isEmpty())
              state->failureDetail = detail;
            if(state->completionHandled)
              return;
            setDiagnosticStatus(authStatus, DiagnosticLevel::Error, detail);
            cancelSubscription->setEnabled(false);
            closeKillOnCloseJob(state->job);
            const QPointer<QProcess> process = state->process;
            if(process && process->state() != QProcess::NotRunning)
              process->kill();
          };

          const auto consumeOutput = [&, state, stopForFailure](const QByteArray &chunk) {
            if(chunk.isEmpty())
              return;
            if(chunk.size() > MaxProcessOutputBytes ||
               state->outputBytes > MaxProcessOutputBytes - chunk.size() ||
               chunk.size() > MaxProcessOutputBytes - state->tail.size())
            {
              stopForFailure(
                  tr("Provider setup returned too much data; the contained process was stopped."));
              return;
            }
            state->outputBytes += chunk.size();
            state->tail += chunk;
            for(;;)
            {
              const int newline = state->tail.indexOf('\n');
              if(newline < 0)
                break;
              if(newline > MaxEventLineBytes)
              {
                stopForFailure(
                    tr("Provider setup returned an oversized event; the contained process was "
                       "stopped."));
                return;
              }
              const QJsonDocument event = QJsonDocument::fromJson(state->tail.left(newline));
              state->tail.remove(0, newline + 1);
              if(!event.isObject())
                continue;
              const QJsonObject object = event.object();
              const QJsonObject data = object.value(lit("data")).toObject();
              const QString kind = object.value(lit("kind")).toString(
                  object.value(lit("event")).toString(object.value(lit("type")).toString()));
              const QString protocolStatus =
                  object.value(lit("status")).toString(data.value(lit("status")).toString());
              // i18n-ignore: ReplayLab event JSON field names; known values are localized below.
              const QString rawMessage = object.value(lit("message")).toString(
                  data.value(lit("message")).toString());
              // i18n-ignore: ReplayLab event JSON field name used only to select a translation.
              const QString eventPresetKey = lit("preset");
              const QString eventPreset = object.value(eventPresetKey).toString(
                  data.value(eventPresetKey).toString(selectedPresetId()));
              // i18n-ignore: Stable ReplayLab runtime-message protocol fields.
              const QString messageId =
                  object.value(lit("messageId")).toString(    // i18n-ignore: ReplayLab protocol key
                      data.value(lit("messageId")).toString());    // i18n-ignore: ReplayLab protocol key
              QJsonArray messageArgs =
                  object.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
              if(messageArgs.isEmpty())
                messageArgs =
                    data.value(lit("messageArgs")).toArray();    // i18n-ignore: ReplayLab protocol key
              const QString message = localizeProviderRuntimeMessage(
                  eventPreset, messageId, messageArgs, rawMessage);
              if(!message.isEmpty())
                setDiagnosticStatus(authStatus, DiagnosticLevel::Info, message);
              const QString url = object.value(lit("authUrl")).toString(
                  object.value(lit("url")).toString(
                      data.value(lit("authUrl")).toString(data.value(lit("url")).toString())));
              const QString code = object.value(lit("userCode")).toString(
                  object.value(lit("code")).toString(
                      data.value(lit("userCode")).toString(data.value(lit("code")).toString())));
              if(!url.isEmpty() || !code.isEmpty())
              {
                currentDeviceCode = code;
                if(!url.isEmpty())
                {
                  const QUrl authUrl(url);
                  if(safeProviderAuthUrl(authUrl))
                  {
                    currentAuthUrl = authUrl;
                    const bool opened = QDesktopServices::openUrl(authUrl);
                    setAuthLinkStatus(
                        authStatus,
                        opened ? DiagnosticLevel::Info : DiagnosticLevel::Warning,
                        opened
                            ? tr("Complete sign-in in your browser. The verified link remains "
                                 "available below if you need to open or copy it again.")
                            : tr("The system browser could not be opened. Open or copy the "
                                 "verified link below to continue sign-in."),
                        currentAuthUrl, currentDeviceCode);
                  }
                  else
                  {
                    currentAuthUrl.clear();
                    setDiagnosticStatus(
                        authStatus, DiagnosticLevel::Error,
                        code.isEmpty()
                            ? tr("The provider returned an unsafe authorization URL; it was not "
                                 "opened.")
                            : tr("The provider returned an unsafe authorization URL; it was not "
                                 "opened.\nDevice code: %1")
                                  .arg(code));
                  }
                }
                else
                  setAuthLinkStatus(authStatus, DiagnosticLevel::Info,
                                    tr("Enter the device code in the provider sign-in flow."),
                                    QUrl(), currentDeviceCode);
              }
              if(object.value(lit("interactiveTerminalRequired")).toBool() ||
                 data.value(lit("interactiveTerminalRequired")).toBool())
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    tr("The provider requires interactive setup. ReplayLab is opening a clearly "
                       "named terminal inside the same contained process job."));
              if(object.value(lit("terminalOpened")).toBool() ||
                 data.value(lit("terminalOpened")).toBool() || kind == lit("terminalOpened"))
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    tr("Interactive provider terminal opened. Complete the prompts there; this "
                       "window will wait and test the connection automatically."));

              QJsonArray discovered = object.value(lit("models")).toArray();
              if(discovered.isEmpty())
                discovered = data.value(lit("models")).toArray();
              if(!discovered.isEmpty())
              {
                QStringList boundedModels;
                QSet<QString> seenModels;
                for(const QJsonValue &modelValue : discovered)
                {
                  const QString model = modelValue.toString().trimmed();
                  if(model.isEmpty() || model.toUtf8().size() > MaxModelIdBytes ||
                     seenModels.contains(model))
                    continue;
                  seenModels.insert(model);
                  boundedModels << model;
                  if(boundedModels.count() >= MaxDiscoveredModels)
                    break;
                }
                state->discoveredModels = boundedModels;
                QSet<QString> enabled;
                for(const QString &enabledModel : m_ProfileModels.value(existingId))
                  enabled.insert(enabledModel);
                models->clear();
                for(const QString &model : boundedModels)
                  addModelItem(model, enabled.contains(model) || models->count() == 0);
                updateDefaultModels();
              }

              const QJsonValue authenticatedValue =
                  object.value(lit("authenticated")).isBool()
                      ? object.value(lit("authenticated"))
                      : data.value(lit("authenticated"));
              const QJsonValue testCompletedValue =
                  object.value(lit("testCompleted")).isBool()
                      ? object.value(lit("testCompleted"))
                      : data.value(lit("testCompleted"));
              const bool explicitlyAuthenticated =
                  authenticatedValue.isBool() && authenticatedValue.toBool();
              const bool explicitlyTested =
                  testCompletedValue.isBool() && testCompletedValue.toBool();
              if(kind == lit("testCompleted"))
              {
                state->testTerminalSucceeded = protocolStatus == lit("ok") &&
                                               explicitlyAuthenticated && explicitlyTested;
                if(state->operation == lit("test"))
                  state->operationTerminalSucceeded = state->testTerminalSucceeded;
                if(!state->testTerminalSucceeded && state->failureDetail.isEmpty())
                  state->failureDetail =
                      message.isEmpty() ? tr("The provider connection test did not succeed.")
                                        : message;
              }
              else if(kind == lit("subscriptionCompleted") &&
                      state->operation == lit("connect"))
              {
                state->operationTerminalSucceeded = protocolStatus == lit("connected") &&
                                                    explicitlyAuthenticated && explicitlyTested;
                if(!state->operationTerminalSucceeded && state->failureDetail.isEmpty())
                  state->failureDetail =
                      message.isEmpty() ? tr("Provider sign-in did not complete successfully.")
                                        : message;
              }

              if(kind == lit("loginCompleted") || kind == lit("connectCompleted"))
                setDiagnosticStatus(
                    authStatus, DiagnosticLevel::Info,
                    message.isEmpty() ? tr("Provider sign-in completed; testing access...")
                                      : message);
              else if(kind == lit("subscriptionCompleted") || kind == lit("testCompleted"))
              {
                const bool eventSucceeded =
                    kind == lit("testCompleted") ? state->testTerminalSucceeded
                                                 : state->operationTerminalSucceeded;
                setDiagnosticStatus(
                    authStatus,
                    eventSucceeded ? DiagnosticLevel::Info : DiagnosticLevel::Error,
                    message.isEmpty()
                        ? eventSucceeded
                              ? tr("Connection completed; verifying provider access...")
                              : tr("The provider did not report a successful connection test.")
                        : message,
                    protocolStatus);
              }
            }
            if(state->tail.size() > MaxEventLineBytes)
              stopForFailure(
                  tr("Provider setup returned an unterminated oversized event; the contained "
                     "process was stopped."));
          };

          const auto completeOperation =
              [&, state, consumeOutput](int exitCode, QProcess::ExitStatus exitStatus,
                                        const QString &startError) {
                if(state->completionHandled)
                  return;
                state->completionHandled = true;
                const QPointer<QProcess> process = state->process;
                QString standardError = startError;
                if(process)
                {
                  consumeOutput(process->readAllStandardOutput());
                  const QString processError = QString::fromUtf8(
                      process->readAllStandardError().left(MaxEventLineBytes)).trimmed();
                  if(standardError.isEmpty())
                    standardError = processError;
                  disconnect(process, NULL, &dialog, NULL);
                }
                closeKillOnCloseJob(state->job);
                if(activeAuthOperation == state)
                  activeAuthOperation.reset();
                state->process = NULL;
                if(process)
                  process->deleteLater();

                const bool normalExit = exitStatus == QProcess::NormalExit;
                const bool processSucceeded = normalExit && exitCode == 0;
                const bool protocolSucceeded = state->testTerminalSucceeded &&
                                               state->operationTerminalSucceeded &&
                                               state->failureDetail.isEmpty();
                const bool succeeded = processSucceeded && protocolSucceeded;
                subscriptionAuthenticated = succeeded;
                if(dialogClosing)
                  return;

                connectSubscription->setEnabled(true);
                cancelSubscription->setVisible(false);
                if(!succeeded)
                {
                  QString detail = state->failureDetail;
                  if(detail.isEmpty())
                    detail = standardError;
                  if(detail.isEmpty() && !normalExit)
                    detail = tr("Provider setup terminated unexpectedly.");
                  if(detail.isEmpty() && exitCode != 0)
                    detail = tr("Provider setup failed with exit code %1.").arg(exitCode);
                  if(detail.isEmpty())
                    detail = state->operation == lit("connect")
                                 ? tr("Provider setup exited without both a successful "
                                      "testCompleted event and subscriptionCompleted event.")
                                 : tr("Provider test exited without a successful testCompleted "
                                      "event.");
                  setDiagnosticStatus(authStatus, DiagnosticLevel::Error, detail);
                }
                else
                {
                  updateAuthStatus();
                  if(profileSavedDuringDialog && !state->discoveredModels.isEmpty())
                  {
                    QString selectedDefault = defaultModel->currentData().toString();
                    if(!state->discoveredModels.contains(selectedDefault))
                    {
                      const QString existingDefault = m_ProfileDefaultModels.value(existingId);
                      selectedDefault = state->discoveredModels.contains(existingDefault)
                                            ? existingDefault
                                            : state->discoveredModels.first();
                    }
                    QStringList modelArguments = {lit("profile"), lit("models"), existingId};
                    for(const QString &model : state->discoveredModels)
                      modelArguments << lit("--allowed-model") << model;
                    modelArguments << lit("--default-model") << selectedDefault << lit("--json");
                    int persistExit = -1;
                    QString persistError;
                    runCLISync(modelArguments, 7000, &persistExit, &persistError);
                    if(persistExit != 0)
                    {
                      const QString warning =
                          persistError.isEmpty()
                              ? tr("The provider connected, but its discovered model list could "
                                   "not be saved. Reopen this connection to retry.")
                              : tr("The provider connected, but its discovered model list could "
                                   "not be saved: %1")
                                    .arg(persistError);
                      setDiagnosticStatus(authStatus, DiagnosticLevel::Warning, warning);
                      QMessageBox::warning(&dialog, tr("Model list not saved"), warning);
                    }
                    else
                    {
                      refreshProfiles();
                    }
                  }
                  // Codex app-server exposes an explicit model list after authentication; manual
                  // entry remains available when a particular model is not advertised.
                  if(!profileSavedDuringDialog && fetchModels->isEnabled())
                    fetchModels->click();
                }
                if(profileSavedDuringDialog)
                {
                  QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
                  if(saveButton)
                  {
                    saveButton->setText(tr("Close"));
                    saveButton->setEnabled(true);
                  }
                }
              };

          connect(processObject, &QProcess::started, &dialog, [this, state, stopForFailure]() {
            const QPointer<QProcess> process = state->process;
            if(!process || state->completionHandled)
              return;
            QString containmentError;
            if(!attachKillOnCloseJob(process, state->job, &containmentError))
            {
              stopForFailure(containmentError);
              return;
            }
            // Subscription connect/test are start-gated: the Python host cannot start any vendor
            // process until qrenderdoc contains this exact host and explicitly releases the gate.
            if(process->write("start\n") != qint64(sizeof("start\n") - 1))
              stopForFailure(tr("Could not release the contained sign-in process."));
          });
          connect(processObject, &QProcess::readyReadStandardOutput, &dialog,
                  [state, consumeOutput]() {
                    const QPointer<QProcess> process = state->process;
                    if(process && !state->completionHandled)
                      consumeOutput(process->readAllStandardOutput());
                  });
          connect(processObject,
                  static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
                  &dialog, [completeOperation](int exitCode, QProcess::ExitStatus exitStatus) {
                    completeOperation(exitCode, exitStatus, QString());
                  });
          connect(processObject, &QProcess::errorOccurred, &dialog,
                  [state, completeOperation](QProcess::ProcessError processError) {
                    if(processError != QProcess::FailedToStart || state->completionHandled)
                      return;
                    const QPointer<QProcess> process = state->process;
                    completeOperation(-1, QProcess::CrashExit,
                                      process ? process->errorString()
                                              : QCoreApplication::translate(
                                                    "AIAssistant",
                                                    "Provider setup could not be started."));
                  });
          QStringList subscriptionArguments = {
              lit("subscription"), operation, lit("--profile"), existingId,
              lit("--start-gated"), lit("--json")};
          if(operation == lit("connect"))
            subscriptionArguments << lit("--flow") << flow;
          processObject->start(m_CLIPath, subscriptionArguments, QIODevice::ReadWrite);
        };
        connect(connectSubscription, &QPushButton::clicked, &dialog,
                [&]() {
                  if(!profileExists)
                  {
                    QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
                    if(saveButton && saveButton->isEnabled())
                      saveButton->click();
                    return;
                  }
                  startSubscriptionOperation(subscriptionAuthenticated ? lit("test")
                                                                       : lit("connect"),
                                             authFlow->currentData().toString());
                });
        connect(cancelSubscription, &QPushButton::clicked, &dialog, [&]() {
          const std::shared_ptr<SubscriptionProcessState> state = activeAuthOperation;
          const QPointer<QProcess> process = state ? state->process : QPointer<QProcess>();
          if(process && !state->completionHandled)
          {
            cancelSubscription->setEnabled(false);
            setDiagnosticStatus(authStatus, DiagnosticLevel::Info,
                                tr("Cancelling connection setup..."));
            process->write("cancel\n");
          }
        });

        if(editing)
        {
          // Hydrate the complete selected profile as one transaction. In particular, do not let
          // currentIndexChanged/textChanged handlers interpret programmatic field restoration as
          // a user changing provider: those handlers intentionally replace provider-scoped
          // values with preset defaults.
          const QSignalBlocker modeBlocker(mode);
          const QSignalBlocker apiProviderBlocker(apiProvider);
          const QSignalBlocker agentProviderBlocker(agentProvider);
          const QSignalBlocker baseUrlBlocker(baseUrl);
          const QSignalBlocker modelsBlocker(models);
          const QSignalBlocker authFlowBlocker(authFlow);

          name->setText(connectionDisplayName(existing));
          QString kind = existing.value(lit("kind")).toString();
          if(kind.isEmpty())
            kind = presetObject(existing.value(lit("preset")).toString())
                       .value(lit("kind"))
                       .toString();
          const int kindIndex = mode->findData(kind);
          mode->setCurrentIndex(kindIndex >= 0 ? kindIndex : 0);
          mode->setEnabled(false);
          populateProviders();

          const bool active = existing.value(lit("active")).toBool();
          defaultConnection->setChecked(active);
          if(active)
          {
            defaultConnection->setEnabled(false);
            defaultConnection->setToolTip(
                tr("Choose Set as Default on another connection to change the default."));
          }

          QString endpoint = existing.value(lit("endpoint")).toString();
          if(endpoint.isEmpty())
            endpoint = existing.value(lit("baseUrl")).toString();
          baseUrl->setText(endpoint);
          const QJsonObject options = existing.value(lit("options")).toObject();
          azureVersion->setText(options.value(lit("api_version")).toString());
          vertexProject->setText(options.value(lit("project")).toString());
          vertexLocation->setText(options.value(lit("location")).toString());

          QString savedAuthFlow = options.value(lit("subscription_auth_flow")).toString();
          if(savedAuthFlow.isEmpty())
            savedAuthFlow = lit("browser");
          const int authFlowIndex = authFlow->findData(savedAuthFlow);
          authFlow->setCurrentIndex(authFlowIndex >= 0 ? authFlowIndex : 0);

          for(const QJsonValue &modelValue : existing.value(lit("models")).toArray())
            addModelItem(modelValue.toString(), true);
          if(models->count() == 0)
          {
            const QString legacyModel = existing.value(lit("defaultModel"))
                                            .toString(existing.value(lit("model")).toString());
            addModelItem(legacyModel, true);
          }

          // Establish the restored preset as the baseline before enabling the interactive update
          // path. This makes the following refresh visibility-only instead of a provider change.
          lastPresetId = selectedPresetId();
          updateProvider();
          updateDefaultModels();
        }
        else
        {
          populateProviders();
          updateProvider();
          name->setFocus();
        }
        modePages->setCurrentIndex(mode->currentData().toString() == lit("model") ? 0 : 1);
        connectSubscription->setEnabled(mode->currentData().toString() == lit("agent"));
        if(editing && existing.value(lit("kind")).toString() == lit("agent"))
          updateAuthStatus();
        if(editing && existing.value(lit("kind")).toString() == lit("model") &&
           presetObject(existing.value(lit("preset")).toString())
                   .value(lit("auth"))
                   .toString() == lit("api-key"))
          refreshCredentialStatus();

        connect(buttons, &QDialogButtonBox::accepted, &dialog, [&]() {
          if(profileSavedDuringDialog)
          {
            dialog.accept();
            return;
          }
          if(!connectionMutationAllowed(&dialog))
            return;
          const QString displayName = name->text().trimmed();
          if(displayName.isEmpty())
          {
            QMessageBox::warning(&dialog, tr("AI Connection"),
                                 tr("Enter a connection name."));
            return;
          }
          const QString presetId = selectedPresetId();
          const QJsonObject preset = presetObject(presetId);
          if(presetId.isEmpty())
            return;
          if(presetId == lit("azure-openai") && azureVersion->isVisible() &&
             azureVersion->text().trimmed().isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("Azure OpenAI"),
                tr("Legacy Azure deployment URLs require an API version. Azure v1/Foundry URLs "
                   "do not use this field."));
            return;
          }
          QStringList enabledModels;
          for(int i = 0; i < models->count(); ++i)
            if(models->item(i)->checkState() == Qt::Checked)
              enabledModels << models->item(i)->text();
          const QString chosenDefault = defaultModel->currentData().toString();
          if(!chosenDefault.isEmpty() && !enabledModels.contains(chosenDefault))
            enabledModels << chosenDefault;
          if(mode->currentData().toString() == lit("model") && chosenDefault.isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("AI model required"),
                tr("Fetch models or add an exact model/deployment name, enable it, and choose a "
                   "default model."));
            return;
          }

          const QString id = editing ? existingId : internalConnectionId();
          const bool presetChanged =
              editing && existing.value(lit("preset")).toString() != presetId;
          const bool usesAPIKey =
              mode->currentData().toString() == lit("model") &&
              preset.value(lit("auth")).toString() == lit("api-key");
          if(presetChanged && usesAPIKey && apiKey->text().isEmpty())
          {
            QMessageBox::warning(
                &dialog, tr("API key required"),
                tr("Enter the key for the newly selected provider. Credentials are never reused "
                   "across providers."));
            return;
          }
          const bool clearingLegacyAzureVersion =
              editing && presetId == lit("azure-openai") && !azureVersion->isVisible() &&
              !existing.value(lit("options")).toObject().value(lit("api_version")).toString().isEmpty();
          QStringList arguments = {lit("profile"), editing ? lit("update") : lit("add"), id,
                                   lit("--preset"), presetId, lit("--title"), displayName};
          if(presetChanged || clearingLegacyAzureVersion)
          {
            // Rebuild every provider-scoped field from this dialog. Incremental update would keep
            // stale command/options/models/key metadata when changing provider or endpoint style.
            arguments << lit("--replace-provider");
          }
          if(mode->currentData().toString() == lit("model"))
          {
            if(!baseUrl->text().trimmed().isEmpty())
              arguments << lit("--base-url") << baseUrl->text().trimmed();
            const QString keyEnvironment = preset.value(lit("apiKeyEnv")).toString();
            if(!keyEnvironment.isEmpty())
              arguments << lit("--api-key-env") << keyEnvironment;
            arguments << lit("--capability") << lit("tools");
            if(presetId == lit("azure-openai") && azureVersion->isVisible() &&
               !azureVersion->text().trimmed().isEmpty())
              arguments << lit("--option")
                        << (lit("api_version=") + azureVersion->text().trimmed());
            if(presetId == lit("vertex"))
            {
              if(!vertexProject->text().trimmed().isEmpty())
                arguments << lit("--option")
                          << (lit("project=") + vertexProject->text().trimmed());
              if(!vertexLocation->text().trimmed().isEmpty())
                arguments << lit("--option")
                          << (lit("location=") + vertexLocation->text().trimmed());
            }
          }
          else
          {
            arguments << lit("--option")
                      << (lit("subscription_auth_flow=") +
                          authFlow->currentData().toString());
          }
          for(const QString &model : enabledModels)
            arguments << lit("--allowed-model") << model;
          if(!chosenDefault.isEmpty())
            arguments << lit("--default-model") << chosenDefault;
          if(defaultConnection->isChecked())
            arguments << lit("--select");
          arguments << lit("--json");

          int saveExit = -1;
          QString saveError;
          runCLISync(arguments, 10000, &saveExit, &saveError);
          if(saveExit != 0)
          {
            QMessageBox::critical(&dialog, tr("Could not save AI connection"),
                                  saveError.isEmpty() ? tr("The AI host rejected the settings.")
                                                      : saveError);
            return;
          }
          QString credentialWarning;
          bool credentialUpdated = true;
          if(usesAPIKey && rememberKey->isChecked())
          {
            const bool sameProvider =
                !editing || existing.value(lit("preset")).toString() == presetId;
            // A session secret is scoped by stable profile id *and* provider. The dialog only
            // clears its visible field while editing; do not reuse the old provider's hidden
            // session secret when the same id is rebuilt for a different provider. Cancelling the
            // dialog leaves the old mapping untouched, as expected.
            const QString keyToStore =
                !apiKey->text().isEmpty()
                    ? apiKey->text()
                    : sameProvider ? m_SessionSecrets.value(id) : QString();
            if(!keyToStore.isEmpty())
            {
              int storeExit = -1;
              runCLIInputSync({lit("credential"), lit("store"), lit("--profile"), id,
                               lit("--api-key-stdin"), lit("--json")},
                              keyToStore.toUtf8(), 7000, &storeExit, &credentialWarning);
              credentialUpdated = storeExit == 0;
              if(credentialUpdated)
                m_SessionSecrets.remove(id);
              else
                // Keep a typed replacement usable for this qrenderdoc session when the OS vault
                // is unavailable or rejects the write. It still never enters argv or profile JSON.
                m_SessionSecrets[id] = keyToStore;
            }
            // An empty field deliberately keeps an existing saved credential. For a new
            // connection with no key, the profile is still saved but diagnostics will explain
            // that authentication is missing.
          }
          else
          {
            QString deleteError;
            if(!deleteStoredCredential(id, &deleteError))
            {
              credentialUpdated = false;
              credentialWarning = deleteError;
            }
            if(usesAPIKey && !apiKey->text().isEmpty())
              m_SessionSecrets[id] = apiKey->text();
            else
              m_SessionSecrets.remove(id);
          }
          if(!credentialUpdated)
          {
            QMessageBox::warning(
                &dialog, tr("Connection saved; credential not updated"),
                credentialWarning.isEmpty()
                    ? tr("The connection was saved, but Windows Credential Manager could not be "
                         "updated. Reopen this connection to retry or use a session-only key.")
                    : credentialWarning);
          }
          if(defaultConnection->isChecked())
          {
            m_ActiveProfile = id;
            m_ActiveModel = chosenDefault;
          }
          refreshProfiles();
          reloadList(id);
          if(!editing && mode->currentData().toString() == lit("agent"))
          {
            // New subscription profiles are checked immediately after their non-secret metadata
            // is saved. Keep this dialog open as a contained connection wizard.
            existingId = id;
            profileExists = true;
            profileSavedDuringDialog = true;
            name->setEnabled(false);
            mode->setEnabled(false);
            apiProvider->setEnabled(false);
            agentProvider->setEnabled(false);
            defaultConnection->setEnabled(false);
            QPushButton *saveButton = buttons->button(QDialogButtonBox::Save);
            if(saveButton)
            {
              saveButton->setText(tr("Connecting..."));
              saveButton->setEnabled(false);
            }
            startSubscriptionOperation(lit("connect"), authFlow->currentData().toString());
            return;
          }
          refreshConnectionStatus();
          dialog.accept();
        });

        dialog.exec();
        dialogClosing = true;
        const std::shared_ptr<SubscriptionProcessState> closingOperation = activeAuthOperation;
        if(closingOperation)
        {
          const QPointer<QProcess> process = closingOperation->process;
          if(process && process->state() != QProcess::NotRunning)
          {
            process->write("cancel\n");
            process->closeWriteChannel();
            if(!process->waitForFinished(1000))
            {
              closeKillOnCloseJob(closingOperation->job);
              process->kill();
              process->waitForFinished(1000);
            }
          }
          if(!closingOperation->completionHandled)
          {
            closingOperation->completionHandled = true;
            closeKillOnCloseJob(closingOperation->job);
            if(activeAuthOperation == closingOperation)
              activeAuthOperation.reset();
            if(process)
            {
              disconnect(process, NULL, &dialog, NULL);
              closingOperation->process = NULL;
              if(process->state() == QProcess::NotRunning)
                process->deleteLater();
              else
              {
                process->setParent(QCoreApplication::instance());
                deleteProcessWhenFinished(process);
              }
            }
          }
          closeKillOnCloseJob(closingOperation->job);
        }
      };

  connect(add, &QPushButton::clicked, page, [=]() {
    if(!connectionMutationAllowed(page))
      return;
    editConnection(QJsonObject());
  });
  connect(edit, &QPushButton::clicked, page, [=]() {
    if(!connectionMutationAllowed(page))
      return;
    if(connections->currentItem())
      editConnection(profileById(connections->currentItem()->data(Qt::UserRole).toString()));
  });
  connect(connections, &QListWidget::itemDoubleClicked, page,
          [=](QListWidgetItem *) { edit->click(); });
  connect(remove, &QPushButton::clicked, page, [=]() {
    if(!connectionMutationAllowed(page))
      return;
    if(!connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    const QString displayName = connectionDisplayName(profileById(id));
    if(QMessageBox::question(page, tr("Remove AI Connection"),
                             tr("Remove '%1'? Its saved and session-only credentials will also be "
                                "forgotten.")
                                 .arg(displayName)) != QMessageBox::Yes)
      return;
    int exitCode = -1;
    QString error;
    int credentialExit = -1;
    QString credentialError;
    runCLISync({lit("credential"), lit("delete"), lit("--profile"), id, lit("--json")}, 5000,
               &credentialExit, &credentialError);
    if(credentialExit != 0)
    {
      QMessageBox::critical(
          page, tr("Remove failed"),
          credentialError.isEmpty()
              ? tr("The saved credential could not be removed, so the connection was kept.")
              : credentialError);
      return;
    }
    runCLISync({lit("profile"), lit("remove"), id, lit("--json")}, 7000, &exitCode, &error);
    if(exitCode != 0)
    {
      QMessageBox::critical(page, tr("Remove failed"), error);
      return;
    }
    m_SessionSecrets.remove(id);
    refreshProfiles();
    reloadList(QString());
    refreshConnectionStatus();
  });
  connect(makeDefault, &QPushButton::clicked, page, [=]() {
    if(!connectionMutationAllowed(page))
      return;
    if(!connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    int exitCode = -1;
    QString error;
    runCLISync({lit("profile"), lit("select"), id, lit("--json")}, 5000, &exitCode, &error);
    if(exitCode != 0)
    {
      QMessageBox::critical(page, tr("Default connection"), error);
      return;
    }
    m_ActiveProfile = id;
    m_ActiveModel = m_ProfileDefaultModels.value(id);
    refreshProfiles();
    reloadList(id);
    refreshConnectionStatus();
  });
  connect(check, &QPushButton::clicked, page, [=]() {
    if(!connections->currentItem())
      return;
    const QString id = connections->currentItem()->data(Qt::UserRole).toString();
    int exitCode = -1;
    QString error;
    const QJsonDocument report = QJsonDocument::fromJson(runCLISync(
        {lit("doctor"), lit("--profile"), id, lit("--json")}, 15000, &exitCode, &error, id));
    showConnectionDiagnostics(page, tr("AI Connection Check"), report, exitCode, error);
  });
  connect(selfTest, &QPushButton::clicked, page, [=]() {
    selfTest->setEnabled(false);
    selfTest->setText(tr("Running Self-Test..."));
    int exitCode = -1;
    QString error;
    const QJsonDocument report = QJsonDocument::fromJson(
        runCLISync({lit("self-test"), lit("--json")}, 30000, &exitCode, &error));
    selfTest->setText(tr("Run Protocol Self-Test"));
    selfTest->setEnabled(true);
    showConnectionDiagnostics(page, tr("AI Protocol Self-Test"), report, exitCode, error);
  });

  reloadList(m_ActiveProfile);
  return page;
}

void AIAssistant::manageConnections()
{
  if(m_ConnectionDialogOpen)
    return;

  m_ConnectionDialogOpen = true;
  SettingsDialog settings(m_Ctx, this);
  settings.focusPage(lit("AI"));
  RDDialog::show(&settings);
  m_ConnectionDialogOpen = false;
  refreshProfiles();
  refreshConnectionStatus();
}

// Kept temporarily as a source-compatible fallback for downstream forks that still invoke the
// former one-shot editor. qrenderdoc itself now exposes only the reusable Settings > AI page.
void AIAssistant::runDoctor()
{
  if(m_ActiveProfile.isEmpty())
  {
    appendSystem(tr("Add or select an AI connection first."), true);
    return;
  }
  int exitCode = -1;
  QString error;
  const QByteArray output = runCLISync(
      {lit("doctor"), lit("--profile"), m_ActiveProfile, lit("--json")}, 10000, &exitCode,
      &error, m_ActiveProfile);
  const QJsonDocument document = QJsonDocument::fromJson(output);
  showConnectionDiagnostics(this, tr("AI Connection Check"), document, exitCode, error);
}

void AIAssistant::refreshConnectionStatus()
{
  if(m_ShuttingDown || m_ConnectionDialogOpen)
    return;
  if(m_CLIPath.isEmpty())
    m_CLIPath = findAgentCLI();
  if(m_CLIPath.isEmpty())
    setDiagnosticStatus(m_Status, DiagnosticLevel::Error, tr("Host missing"));
  else if(m_ActiveProfile.isEmpty())
    setDiagnosticStatus(m_Status, DiagnosticLevel::Warning, tr("Add connection"));
  else if(m_Ctx.IsCaptureLoaded() && findBridgeInstance().isEmpty())
    setDiagnosticStatus(m_Status, DiagnosticLevel::Warning, tr("Waiting for bridge"));
  else
    setDiagnosticStatus(m_Status, DiagnosticLevel::Success, tr("Chat ready"));
}

void AIAssistant::forceStopProcessesForDestruction()
{
  auto stopContainedProcess = [](QProcess *process) {
    if(!process)
      return;
    if(process->state() != QProcess::NotRunning)
    {
      process->kill();
      process->waitForFinished(1000);
    }
    if(process->state() == QProcess::NotRunning)
      delete process;
    else
    {
      // The Job has already issued a tree-wide kill. Keep a rare, slow-to-signal QProcess out of
      // the panel's QObject destructor and delete it when Windows reports the terminated parent.
      process->setParent(QCoreApplication::instance());
      deleteProcessWhenFinished(process);
    }
  };

  QProcess *chatProcess = m_ChatProcess;
  m_ChatProcess = NULL;
  if(chatProcess)
  {
    disconnect(chatProcess, NULL, this, NULL);
    closeKillOnCloseJob(m_ChatJob);
    stopContainedProcess(chatProcess);
  }
  closeKillOnCloseJob(m_ChatJob);
  clearChatContextFile();
}

QString AIAssistant::newTaskId() const
{
  QString value = QUuid::createUuid().toString();
  value.remove(QLatin1Char('{'));
  value.remove(QLatin1Char('}'));
  value.remove(QLatin1Char('-'));
  return value;
}

QString AIAssistant::newMessageId() const
{
  return lit("message-") + newTaskId();
}

QString AIAssistant::conversationPrompt(const QString &prompt, const QString &conversationId,
                                        const QStringList &excludeMessageIds,
                                        const ClarificationResponse &clarificationResponse) const
{
  const ConversationThread *thread = conversation(conversationId);
  if(!thread)
    return prompt;
  QVector<const AIChatMessage *> history;
  for(const AIChatMessage &item : thread->messages)
  {
    if(excludeMessageIds.contains(item.id) || item.role == lit("system"))
      continue;
    if(item.role == lit("user") &&
       (item.state == lit("queued") || item.state == lit("running") ||
        item.state == lit("planning") || item.state == lit("finalizing")))
      continue;
    if(item.role == lit("user") || item.role == lit("assistant"))
      history.push_back(&item);
  }

  // A separately installed older AgentHost does not understand the typed v2
  // envelope. Negotiate the input schema through `profile list`; otherwise use
  // the exact legacy English protocol (never a translated protocol literal).
  // i18n-ignore: versioned AgentHost wire-protocol identifier.
  if(!m_ProfileRunInputSchemas.value(m_ActiveProfile)
          .contains(lit("replaylab-conversation/v2")))    // i18n-ignore: AgentHost wire schema ID
  {
    // i18n-ignore: exact legacy AgentHost protocol prefix; translating it broke routing.
    const QString prefix =
        // i18n-ignore: exact legacy AgentHost wire-protocol prefix; translating it breaks routing.
        lit("Continue this RenderDoc AI conversation. Treat the final USER block as the new "
            "request; earlier blocks are conversation history, not tool evidence.\n\n");
    // A delimiter inside the current request is ambiguous in v1. Preserve the
    // exact request as ordinary input instead of misrouting a fabricated final block.
    if(prompt.contains(lit("\n\nUSER:\n")))    // i18n-ignore: legacy protocol delimiter
      return prompt;
    const QString current = lit("USER:\n") + prompt;    // i18n-ignore: legacy role marker
    QString rendered = prefix + current;
    QStringList blocks;
    for(int i = history.size() - 1; i >= 0; --i)
    {
      const AIChatMessage *item = history[i];
      // i18n-ignore: legacy AgentHost protocol role markers.
      const QString marker = item->role == lit("user") ? lit("USER:\n") : lit("ASSISTANT:\n");
      blocks.prepend(marker + item->text);
      const QString candidate = prefix + blocks.join(lit("\n\n")) + lit("\n\n") + current;
      if(candidate.toUtf8().size() > MaxPromptBytes)
      {
        blocks.removeFirst();
        break;
      }
      rendered = candidate;
    }
    return rendered;
  }

  QJsonObject request;
  // i18n-ignore: JSON role and field values are ReplayLab protocol identifiers.
  request[lit("role")] = lit("user");
  request[lit("content")] = prompt;    // i18n-ignore: AgentHost wire-protocol key

  QJsonArray selected;
  QJsonObject envelope;
  // i18n-ignore: schema and JSON keys are the versioned AgentHost wire protocol.
  envelope[lit("schema")] = lit("replaylab-conversation/v2");
  envelope[lit("conversationId")] = thread->id;    // i18n-ignore: AgentHost wire-protocol key
  envelope[lit("request")] = request;    // i18n-ignore: AgentHost wire-protocol key
  envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
  // i18n-ignore: AgentHost wire-protocol key for bounded-history accounting
  envelope[lit("omittedMessageCount")] = history.size();

  if(clarificationResponse.isValid())
  {
    QJsonObject response;
    // i18n-ignore: ReplayLab structured clarification-response wire-protocol fields.
    response[lit("schema")] = lit("replaylab-clarification-response/v1");
    response[lit("clarificationId")] =    // i18n-ignore: clarification-response protocol key
        clarificationResponse.clarificationId;
    QJsonArray optionIds;
    for(const QString &optionId : clarificationResponse.optionIds)
      optionIds.push_back(optionId);
    response[lit("optionIds")] = optionIds;    // i18n-ignore: clarification-response protocol key
    response[lit("otherText")] =    // i18n-ignore: clarification-response protocol key
        clarificationResponse.otherText;
    envelope[lit("clarificationResponse")] = response;    // i18n-ignore: conversation protocol key

    QJsonObject candidates;
    candidates[lit("schema")] =    // i18n-ignore: context-candidate protocol key
        lit("replaylab-context-candidates/v1");    // i18n-ignore: context-candidate schema ID
    const QJsonObject origin =
        contextCandidateJSON(clarificationResponse.originContextSnapshot,
                             clarificationResponse.originContextInstanceId);
    const QJsonObject current =
        contextCandidateJSON(clarificationResponse.currentContextSnapshot,
                             clarificationResponse.currentContextInstanceId);
    if(!origin.isEmpty())
      candidates[lit("origin")] = origin;    // i18n-ignore: context-candidate protocol key
    if(!current.isEmpty())
      candidates[lit("current")] = current;    // i18n-ignore: context-candidate protocol key
    if(candidates.size() > 1)
      envelope[lit("contextCandidates")] = candidates;    // i18n-ignore: conversation protocol key
  }

  // Keep one contiguous newest-first suffix of complete messages. The 16 MiB
  // transport gate remains authoritative; semantic token budgeting and
  // compaction happen in AgentHost for the selected model.
  for(int i = history.size() - 1; i >= 0; --i)
  {
    const AIChatMessage *item = history[i];
    QJsonObject message;
    // i18n-ignore: role values and keys are machine-readable protocol identifiers.
    message[lit("id")] = item->id;
    message[lit("role")] = item->role;    // i18n-ignore: AgentHost wire-protocol key
    message[lit("content")] = item->text;    // i18n-ignore: AgentHost wire-protocol key
    selected.insert(0, message);
    envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
    envelope[lit("omittedMessageCount")] = i;    // i18n-ignore: AgentHost wire-protocol key
    if(QJsonDocument(envelope).toJson(QJsonDocument::Compact).size() > MaxPromptBytes)
    {
      selected.removeAt(0);
      envelope[lit("messages")] = selected;    // i18n-ignore: AgentHost wire-protocol key
      envelope[lit("omittedMessageCount")] = i + 1;    // i18n-ignore: AgentHost wire-protocol key
      break;
    }
  }
  return QString::fromUtf8(QJsonDocument(envelope).toJson(QJsonDocument::Compact));
}

bool AIAssistant::freezeChatContext(PendingChat &request, QString *error)
{
  request.contextSnapshot.clear();
  request.contextInstanceId.clear();
  if(error)
    error->clear();
  if(!m_Ctx.IsCaptureLoaded())
    return true;

  const QString instance = findBridgeInstance();
  if(instance.isEmpty())
  {
    if(error)
      *error = tr("The capture bridge is not ready yet.");
    return false;
  }

  QString bridgeError;
  const QByteArray encoded =
      m_Ctx.ReplayLab().CurrentContextJSON(request.focusStage, &bridgeError).toUtf8();
  if(!decodeContextSnapshot(encoded))
  {
    if(!bridgeError.isEmpty())
      // i18n-ignore: developer diagnostic written only to the debug log.
      qWarning() << "Could not freeze ReplayLab AI context:" << bridgeError;
    if(error)
      *error = tr("The current RenderDoc state could not be frozen for this AI request.");
    return false;
  }

  request.contextSnapshot = encoded;
  request.contextInstanceId = instance;
  return true;
}

void AIAssistant::clearChatContextFile()
{
  if(m_ChatContextFile.isEmpty())
    return;
  const QString path = m_ChatContextFile;
  m_ChatContextFile.clear();
  if(!QFile::remove(path))
    // i18n-ignore: developer diagnostic written only to the debug log.
    qWarning() << "Could not remove temporary ReplayLab AI context file:" << path;
}

void AIAssistant::StartCaptureSkill(const QString &prompt, const QString &skill,
                                    const QString &focusStage, const QString &intentKind)
{
  startChat(prompt, skill, false, focusStage, ClarificationResponse(), intentKind);
}

void AIAssistant::startChat(const QString &prompt, const QString &skill, bool includeConversation,
                            const QString &focusStage,
                            const ClarificationResponse &clarificationResponse,
                            const QString &intentKind)
{
  if(m_CLIPath.isEmpty())
  {
    appendSystem(tr("ReplayLab AI host was not found."), true);
    return;
  }
  if(m_ActiveProfile.isEmpty())
  {
    appendSystem(tr("Add or select an AI connection first."), true);
    return;
  }

  if(!skill.isEmpty() && !m_Ctx.IsCaptureLoaded())
  {
    appendSystem(tr("This action requires an open capture."), true);
    return;
  }

  if(prompt.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The request exceeds the 16 MiB safety limit."), true);
    return;
  }
  if(m_ChatQueue.size() >= MaxPendingChats)
  {
    appendSystem(tr("The pending-message queue is full. Remove a queued message before sending "
                    "another."),
                 true);
    return;
  }

  ConversationThread *thread = conversation(m_ActiveConversationId);
  if(!thread)
    return;
  ClarificationResponse effectiveClarification = clarificationResponse;
  if(!effectiveClarification.isValid() && thread->clarification.isValid())
  {
    // A typed reply is still a response to the visible question even when the user does not click
    // Other first. Preserve it verbatim as otherText and let AgentHost determine whether it
    // answers the question, changes the goal, or requires an origin/current context choice.
    effectiveClarification.clarificationId = thread->clarification.id;
    effectiveClarification.otherText = prompt.left(MaxClarificationOtherChars);
    effectiveClarification.originContextSnapshot = thread->clarification.originContextSnapshot;
    effectiveClarification.originContextInstanceId =
        thread->clarification.originContextInstanceId;
  }

  PendingChat request;
  request.taskId = newTaskId();
  request.conversationId = m_ActiveConversationId;
  request.prompt = prompt;
  request.effectivePrompt = prompt;
  request.skill = skill;
  request.focusStage = focusStage;
  request.intentKind = intentKind;
  request.clarificationResponse = effectiveClarification;
  request.includeConversation = includeConversation;
  QString contextError;
  if(!freezeChatContext(request, &contextError))
  {
    appendSystem(contextError, true);
    return;
  }
  if(request.clarificationResponse.isValid())
  {
    request.clarificationResponse.currentContextSnapshot = request.contextSnapshot;
    request.clarificationResponse.currentContextInstanceId = request.contextInstanceId;
  }
  clearClarification(request.conversationId);
  thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
  thread->unread = false;
  if(thread->title == tr("New conversation") || thread->title.trimmed().isEmpty())
  {
    QString title = prompt.simplified();
    if(title.size() > 52)
      title = title.left(49) + lit("...");
    if(!title.isEmpty())
      thread->title = title;
  }
  m_ChatQueue.push_back(request);
  refreshConversationList();
  updateComposerState();
  scheduleConversationSave();
  if(m_ChatTaskId.isEmpty() &&
     (!m_ChatProcess || m_ChatProcess->state() == QProcess::NotRunning))
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::startNextChat()
{
  if(!m_ChatTaskId.isEmpty() ||
     (m_ChatProcess && m_ChatProcess->state() != QProcess::NotRunning))
    return;
  while(!m_ChatQueue.isEmpty())
  {
    const PendingChat request = m_ChatQueue.front();
    m_ChatQueue.remove(0);
    if(conversation(request.conversationId) &&
       (request.messageId.isEmpty() || message(request.conversationId, request.messageId)))
    {
      launchChat(request);
      return;
    }
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
  }
  updateComposerState();
}

void AIAssistant::launchChat(PendingChat request)
{
  clearChatContextFile();
  if(request.messageId.isEmpty())
    request.messageId =
        appendUser(request.prompt, request.taskId, lit("queued"), request.conversationId);
  if(!request.messageId.isEmpty())
  {
    m_TaskConversationIds[request.taskId] = request.conversationId;
    m_TaskMessageIds[request.taskId] = request.messageId;
  }
  AIChatMessage *chatMessage = message(request.conversationId, request.messageId);
  if(!chatMessage)
  {
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
    return;
  }

  auto failBeforeStart = [this, chatMessage, request](const QString &failure) {
    clearChatContextFile();
    chatMessage->state = lit("failed");
    chatMessage->provisionalText = failure;
    chatMessage->finishedTimestampMs = QDateTime::currentMSecsSinceEpoch();
    AIActivityItem item;
    item.kind = lit("failed");
    item.title = tr("Could not start");
    item.detail = failure;
    item.timestampMs = chatMessage->finishedTimestampMs;
    item.finishedTimestampMs = chatMessage->finishedTimestampMs;
    item.completed = true;
    item.error = true;
    chatMessage->activity.push_back(item);
    chatMessage->activityExpanded = false;
    m_UserPrompts.remove(request.taskId);
    unwatchTask(request.taskId);
    m_RunningTasks.remove(request.taskId);
    m_TaskOffsets.remove(request.taskId);
    m_TaskSequences.remove(request.taskId);
    m_TaskTails.remove(request.taskId);
    m_TaskTexts.remove(request.taskId);
    m_TaskConversationIds.remove(request.taskId);
    m_TaskMessageIds.remove(request.taskId);
    renderConversation();
    saveConversations();
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
  };

  QString instance;
  if(!request.contextSnapshot.isEmpty())
  {
    if(!m_Ctx.IsCaptureLoaded())
    {
      failBeforeStart(tr("This action requires an open capture."));
      return;
    }
    instance = findBridgeInstance();
    if(instance.isEmpty() || instance != request.contextInstanceId)
    {
      failBeforeStart(tr("The capture bridge is not ready yet."));
      return;
    }
    m_InstanceId = instance;
  }
  else if(m_Ctx.IsCaptureLoaded())
  {
    failBeforeStart(tr("The current RenderDoc state could not be frozen for this AI request."));
    return;
  }
  if(!request.skill.isEmpty() && !m_Ctx.IsCaptureLoaded())
  {
    failBeforeStart(tr("This action requires an open capture."));
    return;
  }

  QStringList excludedMessages = request.historyExcludeMessageIds;
  excludedMessages.push_back(request.messageId);
  const QString submittedPrompt =
      request.includeConversation
          ? conversationPrompt(request.effectivePrompt, request.conversationId, excludedMessages,
                               request.clarificationResponse)
          : request.effectivePrompt;
  const QByteArray promptBytes = submittedPrompt.toUtf8();
  if(promptBytes.size() > MaxPromptBytes)
  {
    failBeforeStart(tr("The request plus conversation context exceeds the 16 MiB safety limit."));
    return;
  }
  QStringList arguments = {lit("run"), lit("--profile"), m_ActiveProfile, lit("--prompt-stdin"),
                           lit("--task-id"), request.taskId, lit("--json")};
  if(!m_ActiveModel.isEmpty())
    arguments << lit("--model") << m_ActiveModel;
  if(!request.skill.isEmpty())
    arguments << lit("--skill") << request.skill;
  if(!request.intentKind.isEmpty())
    // i18n-ignore: Stable internal CLI option; never shown as UI text.
    arguments << lit("--intent-kind") << request.intentKind;
  if(!request.contextSnapshot.isEmpty())
  {
    const QString temporaryRoot =
        QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    QTemporaryFile contextFile(
        QDir(temporaryRoot).filePath(lit("ReplayLabAIContext-XXXXXX.json")));    // i18n-ignore: fixed temporary filename
    if(!contextFile.open() ||
       contextFile.write(request.contextSnapshot) != request.contextSnapshot.size() ||
       !contextFile.flush())
    {
      failBeforeStart(tr("The current RenderDoc state could not be frozen for this AI request."));
      return;
    }
    m_ChatContextFile = contextFile.fileName();
    contextFile.close();
    contextFile.setAutoRemove(false);
    arguments << lit("--context-file") << m_ChatContextFile << lit("--instance-id") << instance;    // i18n-ignore: CLI flags
  }

  // Retain the effective request (including earlier direction adjustments) so another steer can
  // restart the same work without dropping its original objective.
  watchTask(request.taskId, request.effectivePrompt, request.conversationId, request.messageId);
  m_ChatTaskId = request.taskId;
  m_ActiveChatConversationId = request.conversationId;
  m_ActiveChatMessageId = request.messageId;
  m_ActiveChatRequest = request;
  chatMessage->state = lit("running");
  chatMessage->activityExpanded = true;
  addTaskActivity(request.taskId, lit("running"), tr("Starting Agent"));
  refreshConversationList();
  m_ChatStdout.clear();
  m_ChatStderr.clear();
  m_ChatProcess = new QProcess(this);
  QProcess *chatProcess = m_ChatProcess;
  configureProcess(chatProcess, m_ActiveProfile);
  connect(chatProcess, &QProcess::readyReadStandardOutput, this, [this, chatProcess]() {
    const QByteArray outputChunk = chatProcess->readAllStandardOutput();
    if(chatProcess == m_ChatProcess && m_ChatStdout.size() < MaxProcessOutputBytes)
      m_ChatStdout += outputChunk.left(MaxProcessOutputBytes - m_ChatStdout.size());
  });
  connect(chatProcess, &QProcess::readyReadStandardError, this, [this, chatProcess]() {
    const QByteArray errorChunk = chatProcess->readAllStandardError();
    if(chatProcess == m_ChatProcess && m_ChatStderr.size() < MaxEventLineBytes)
      m_ChatStderr += errorChunk.left(MaxEventLineBytes - m_ChatStderr.size());
  });
  connect(chatProcess,
          static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished), this,
          [this, chatProcess](int exitCode, QProcess::ExitStatus exitStatus) {
            if(chatProcess == m_ChatProcess)
            {
              if(exitStatus != QProcess::NormalExit && m_ChatStderr.isEmpty())
                m_ChatStderr = tr("AI request process terminated unexpectedly.").toUtf8();
              finishChatProcess(exitStatus == QProcess::NormalExit ? exitCode : -1);
            }
          });
  chatProcess->start(m_CLIPath, arguments, QIODevice::ReadWrite);
  if(!chatProcess->waitForStarted(3000))
  {
    m_ChatStderr = chatProcess->errorString().toUtf8();
    finishChatProcess(-1);
    return;
  }

  // `run --prompt-stdin` cannot launch a provider until it receives this prompt. Contain the
  // AgentHost process first so every subsequently-created Codex/MCP child inherits the Job.
  QString jobError;
  if(!attachKillOnCloseJob(chatProcess, m_ChatJob, &jobError))
  {
    m_ChatStderr = jobError.toUtf8();
    disconnect(chatProcess, NULL, this, NULL);
    chatProcess->kill();
    chatProcess->waitForFinished(1000);
    finishChatProcess(-1);
    return;
  }

  qint64 promptOffset = 0;
  while(promptOffset < promptBytes.size())
  {
    const qint64 written = chatProcess->write(promptBytes.constData() + promptOffset,
                                              promptBytes.size() - promptOffset);
    if(written <= 0)
    {
      m_ChatStderr = tr("Failed to send the request to the AI host.").toUtf8();
      disconnect(chatProcess, NULL, this, NULL);
      chatProcess->kill();
      chatProcess->waitForFinished(1000);
      finishChatProcess(-1);
      return;
    }
    promptOffset += written;
  }
  chatProcess->closeWriteChannel();
  updateComposerState();
  setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Thinking..."));
}

void AIAssistant::adjustQueuedChat(const QString &taskId)
{
  int queuedIndex = -1;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId)
    {
      queuedIndex = i;
      break;
    }
  }
  if(queuedIndex < 0)
    return;

  const PendingChat directionRequest = m_ChatQueue[queuedIndex];
  if(m_ChatTaskId.isEmpty() || m_ActiveChatConversationId != directionRequest.conversationId)
  {
    appendSystem(tr("Adjust direction is available when this conversation has an active AI "
                    "request. The message remains queued."),
                 true);
    return;
  }

  const QString direction = directionRequest.prompt.trimmed();
  PendingChat request = m_ActiveChatRequest;
  request.taskId = directionRequest.taskId;
  request.messageId.clear();
  request.conversationId = m_ActiveChatConversationId;
  request.prompt = direction;
  // The direction message is a new user action. Bind the restarted turn to
  // the state frozen when that queued direction was sent, not to the older
  // request which is being superseded.
  request.contextSnapshot = directionRequest.contextSnapshot;
  request.contextInstanceId = directionRequest.contextInstanceId;
  request.clarificationResponse = directionRequest.clarificationResponse;
  if(!request.historyExcludeMessageIds.contains(m_ActiveChatMessageId))
    request.historyExcludeMessageIds.push_back(m_ActiveChatMessageId);
  QString partialWork = m_TaskTexts.value(m_ChatTaskId).trimmed();
  if(partialWork.isEmpty())
  {
    const AIChatMessage *activeMessage =
        message(m_ActiveChatConversationId, m_ActiveChatMessageId);
    if(activeMessage)
      partialWork = activeMessage->provisionalText.trimmed();
  }
  request.effectivePrompt = m_ActiveChatRequest.effectivePrompt;
  if(!partialWork.isEmpty())
  {
    request.effectivePrompt +=
        tr("\n\nDraft work completed before the interruption (preserve useful findings, "
           "but re-check them against the new direction):\n") +
        partialWork.left(256 * 1024);
  }
  request.effectivePrompt +=
      tr("\n\nAdditional direction from the user (re-plan and continue from the preserved "
         "context):\n") +
      direction;
  const QString submittedPrompt =
      request.includeConversation
          ? conversationPrompt(request.effectivePrompt, request.conversationId,
                               request.historyExcludeMessageIds,
                               request.clarificationResponse)
          : request.effectivePrompt;
  if(direction.toUtf8().size() > MaxPromptBytes ||
     submittedPrompt.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The adjusted request plus conversation context exceeds the 16 MiB safety "
                    "limit. The current request was not interrupted."),
                 true);
    return;
  }

  m_ChatQueue.remove(queuedIndex);
  m_ChatQueue.prepend(request);
  addTaskActivity(m_ChatTaskId, lit("steered"), tr("Direction adjusted"),
                  tr("Preserving completed work, interrupting the current turn, and re-planning "
                     "with the queued clarification."),
                  true);
  updateComposerState();
  cancelChat(lit("superseded"), true);
}

void AIAssistant::editQueuedChat(const QString &taskId)
{
  QString edited;
  if(m_PendingChats)
  {
    for(const AIPendingChatItem &item : m_PendingChats->items())
    {
      if(item.id == taskId)
      {
        edited = item.text.trimmed();
        break;
      }
    }
  }
  if(edited.isEmpty())
    return;
  if(edited.toUtf8().size() > MaxPromptBytes)
  {
    appendSystem(tr("The edited request exceeds the 16 MiB safety limit."), true);
    // The inline editor owns only a draft copy. Restore the authoritative queued text when the
    // validation gate rejects that draft.
    updateComposerState();
    return;
  }

  for(PendingChat &request : m_ChatQueue)
  {
    if(request.taskId != taskId)
      continue;
    if(request.clarificationResponse.isValid() && edited.size() > MaxClarificationOtherChars)
    {
      appendSystem(
          tr("The edited clarification response exceeds the 16,384-character safety limit."),
          true);
      updateComposerState();
      return;
    }
    PendingChat updated = request;
    updated.prompt = edited;
    updated.effectivePrompt = edited;
    updated.historyExcludeMessageIds.clear();
    if(updated.clarificationResponse.isValid())
    {
      // Once the readable reply is edited it is no longer safe to claim that it represents the
      // originally clicked option IDs. Preserve the clarification identity and origin candidate,
      // but serialize the edited text as the sole free-form answer.
      updated.clarificationResponse.optionIds.clear();
      updated.clarificationResponse.otherText = edited;
    }
    QString contextError;
    if(!freezeChatContext(updated, &contextError))
    {
      appendSystem(contextError, true);
      updateComposerState();
      return;
    }
    if(updated.clarificationResponse.isValid())
    {
      // Editing re-freezes the task at the current capture/EID. Keep the structured envelope and
      // the --context-file snapshot on the same candidate so AgentHost cannot bind tools to stale
      // UI state.
      updated.clarificationResponse.currentContextSnapshot = updated.contextSnapshot;
      updated.clarificationResponse.currentContextInstanceId = updated.contextInstanceId;
    }
    request = updated;
    if(ConversationThread *thread = conversation(request.conversationId))
    {
      thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
      if(thread->messages.isEmpty())
      {
        QString title = edited.simplified();
        if(title.size() > 52)
          title = title.left(49) + lit("...");
        thread->title = title.isEmpty() ? tr("New conversation") : title;
      }
    }
    refreshConversationList();
    updateComposerState();
    saveConversations();
    return;
  }
}

void AIAssistant::deleteQueuedChat(const QString &taskId)
{
  QString conversationId;
  for(int i = 0; i < m_ChatQueue.size(); ++i)
  {
    if(m_ChatQueue[i].taskId == taskId)
    {
      conversationId = m_ChatQueue[i].conversationId;
      m_ChatQueue.remove(i);
      break;
    }
  }
  if(conversationId.isEmpty())
    return;

  m_UserPrompts.remove(taskId);
  unwatchTask(taskId);
  m_RunningTasks.remove(taskId);
  m_TaskOffsets.remove(taskId);
  m_TaskSequences.remove(taskId);
  m_TaskTails.remove(taskId);
  m_TaskTexts.remove(taskId);
  m_TaskConversationIds.remove(taskId);
  m_TaskMessageIds.remove(taskId);
  if(ConversationThread *thread = conversation(conversationId))
  {
    thread->updatedMs = QDateTime::currentMSecsSinceEpoch();
    bool hasOtherPending = false;
    for(const PendingChat &pending : m_ChatQueue)
      hasOtherPending |= pending.conversationId == conversationId;
    if(thread->messages.isEmpty() && !hasOtherPending)
      thread->title = tr("New conversation");
  }
  refreshConversationList();
  updateComposerState();
  saveConversations();
  if(m_ChatTaskId.isEmpty())
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::cancelChat(const QString &terminalState, bool continueQueue)
{
  if(!m_ChatProcess && m_ChatTaskId.isEmpty())
    return;
  const QString taskId = m_ChatTaskId;
  QProcess *chatProcess = m_ChatProcess;
  m_ChatProcess = NULL;
  if(chatProcess)
  {
    disconnect(chatProcess, NULL, this, NULL);
    // Job close is the tree-wide cancellation. QProcess::kill alone only targets AgentHost.
    closeKillOnCloseJob(m_ChatJob);
    if(chatProcess->state() != QProcess::NotRunning)
      chatProcess->kill();
    if(chatProcess->state() == QProcess::NotRunning)
      chatProcess->deleteLater();
    else
    {
      chatProcess->setParent(QCoreApplication::instance());
      connect(chatProcess,
              static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
              chatProcess,
              [chatProcess](int, QProcess::ExitStatus) { chatProcess->deleteLater(); });
    }
  }
  closeKillOnCloseJob(m_ChatJob);
  clearChatContextFile();
  m_ChatTaskId.clear();
  m_ActiveChatConversationId.clear();
  m_ActiveChatMessageId.clear();
  m_ActiveChatRequest = PendingChat();
  m_ChatStdout.clear();
  m_ChatStderr.clear();
  if(!taskId.isEmpty() && !m_CompletedTasks.contains(taskId))
  {
    m_CompletedTasks.insert(taskId);
    unwatchTask(taskId);
    m_RunningTasks.remove(taskId);
    const QString state = terminalState.isEmpty() ? lit("interrupted") : terminalState;
    addTaskActivity(taskId, state,
                    state == lit("superseded") ? tr("Interrupted for new direction")
                                                : tr("Request stopped"),
                    QString(), true, false);
    setTaskState(taskId, state, true);
    m_UserPrompts.remove(taskId);
    m_TaskOffsets.remove(taskId);
    m_TaskSequences.remove(taskId);
    m_TaskTails.remove(taskId);
    m_TaskTexts.remove(taskId);
    m_TaskConversationIds.remove(taskId);
    m_TaskMessageIds.remove(taskId);
  }
  refreshConversationList();
  updateComposerState();
  saveConversations();
  refreshConnectionStatus();
  if(continueQueue)
    QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::clearCaptureTasks()
{
  const int taskCount = m_KnownTasks.size();
  const QString directChatTask = m_ChatTaskId;
  cancelChat(lit("interrupted"), false);
  const qint64 interruptedMs = QDateTime::currentMSecsSinceEpoch();
  for(const PendingChat &request : m_ChatQueue)
  {
    ConversationThread *thread = conversation(request.conversationId);
    if(!thread || request.prompt.trimmed().isEmpty())
      continue;
    AIChatMessage item;
    item.id = lit("message-") + request.taskId;
    item.role = lit("user");
    item.text = request.prompt;
    item.taskId = request.taskId;
    item.state = lit("interrupted");
    item.timestampMs = interruptedMs;
    item.finishedTimestampMs = interruptedMs;
    AIActivityItem activity;
    activity.kind = lit("interrupted");
    activity.title = tr("Queued message was not run before the capture closed");
    activity.timestampMs = interruptedMs;
    activity.finishedTimestampMs = interruptedMs;
    activity.completed = true;
    item.activity.push_back(activity);
    thread->messages.push_back(item);
    thread->updatedMs = interruptedMs;
  }
  m_ChatQueue.clear();

  for(const QString &taskId : m_KnownTasks)
    m_CompletedTasks.insert(taskId);
  if(m_CompletedTasks.size() > 512)
  {
    m_CompletedTasks.clear();
    if(!directChatTask.isEmpty())
      m_CompletedTasks.insert(directChatTask);
    for(const QString &taskId : m_KnownTasks)
      m_CompletedTasks.insert(taskId);
  }

  m_KnownTasks.clear();
  updateEventTimer();
  m_RunningTasks.clear();
  m_TaskOffsets.clear();
  m_TaskSequences.clear();
  m_TaskTails.clear();
  m_TaskTexts.clear();
  m_UserPrompts.clear();
  m_TaskConversationIds.clear();
  m_TaskMessageIds.clear();

  if(taskCount > 0)
    appendSystem(tr("Capture closed; discarded %1 capture-bound AI task(s).").arg(taskCount));
  saveConversations();
  updateComposerState();
}

void AIAssistant::finishChatProcess(int exitCode)
{
  const QString taskId = m_ChatTaskId;
  if(m_ChatProcess)
  {
    const QByteArray finalOutput = m_ChatProcess->readAllStandardOutput();
    const QByteArray finalError = m_ChatProcess->readAllStandardError();
    if(m_ChatStdout.size() < MaxProcessOutputBytes)
      m_ChatStdout += finalOutput.left(MaxProcessOutputBytes - m_ChatStdout.size());
    if(m_ChatStderr.size() < MaxEventLineBytes)
      m_ChatStderr += finalError.left(MaxEventLineBytes - m_ChatStderr.size());
    closeKillOnCloseJob(m_ChatJob);
    m_ChatProcess->deleteLater();
    m_ChatProcess = NULL;
  }
  closeKillOnCloseJob(m_ChatJob);
  updateComposerState();
  drainChatTaskEvents(taskId, exitCode, MaxEventDrainBytes);
}

void AIAssistant::drainChatTaskEvents(const QString &taskId, int exitCode,
                                      qint64 remainingBytes)
{
  if(taskId.isEmpty() || taskId != m_ChatTaskId)
    return;
  if(m_CompletedTasks.contains(taskId))
  {
    finishChatResult(taskId, exitCode);
    return;
  }

  bool atEnd = false;
  const qint64 readBytes =
      pollTaskEvent(taskId, qMin(MaxEventReadBytes, remainingBytes), &atEnd);
  remainingBytes -= readBytes;
  if(m_CompletedTasks.contains(taskId))
  {
    finishChatResult(taskId, exitCode);
    return;
  }

  if(!atEnd && readBytes > 0 && remainingBytes > 0)
  {
    // Drain one bounded disk chunk per event-loop turn. Large legal answers can produce both
    // streamed deltas and a final reset stream; 16 MiB covers that protocol worst case without a
    // long synchronous UI stall.
    QTimer::singleShot(0, this, [this, taskId, exitCode, remainingBytes]() {
      drainChatTaskEvents(taskId, exitCode, remainingBytes);
    });
    return;
  }

  if(atEnd)
  {
    const QByteArray tail = m_TaskTails.take(taskId).trimmed();
    if(!tail.isEmpty() && tail.size() <= MaxEventLineBytes)
      consumeTaskEvent(taskId, tail);
  }
  finishChatResult(taskId, exitCode);
}

void AIAssistant::finishChatResult(const QString &taskId, int exitCode)
{
  if(taskId.isEmpty() || taskId != m_ChatTaskId)
    return;

  if(!taskId.isEmpty() && !m_CompletedTasks.contains(taskId))
  {
    const QJsonDocument document = QJsonDocument::fromJson(m_ChatStdout.left(MaxProcessOutputBytes));
    const QJsonObject resultObject = document.isObject() ? document.object() : QJsonObject();
    // i18n-ignore: ReplayLab CLI result JSON field name defined by the backend protocol.
    const QString result = resultObject.value(lit("text")).toString();
    QString terminalStatus;
    const bool validTerminal = normalizedTerminalStatus(resultObject, terminalStatus);
    // The TaskEvent stream is authoritative. This stdout branch is only a compatibility fallback
    // when a provider exits before qrenderdoc observes its terminal event.
    if(validTerminal && terminalStatus == lit("needs-user-input"))    // i18n-ignore: task lifecycle state
    {
      ClarificationPrompt clarification;
      // i18n-ignore: ReplayLab result JSON field name defined by the backend protocol.
      if(clarificationFromJSON(resultObject.value(lit("clarification")).toObject(), taskId,
                               clarification))
      {
        bindClarificationOrigin(clarification, taskId);
        if(ConversationThread *thread = conversation(m_TaskConversationIds.value(taskId)))
          thread->clarification = clarification;
      }
    }
    if(exitCode == 0 && !result.isEmpty() && validTerminal)
      finishTask(taskId, terminalStatus, result);
    else if(exitCode == 0 && !result.isEmpty())
      // i18n-ignore: Task lifecycle state identifier used only for control flow.
      finishTask(taskId, lit("failed"), tr("AI result contained an invalid terminal status."));
    else
      finishTask(taskId, lit("failed"),
                 QString::fromUtf8(m_ChatStderr.left(MaxEventLineBytes)).trimmed());
  }
  clearChatContextFile();
  m_ChatTaskId.clear();
  m_ActiveChatConversationId.clear();
  m_ActiveChatMessageId.clear();
  m_ActiveChatRequest = PendingChat();
  m_ChatStdout.clear();
  m_ChatStderr.clear();
  updateComposerState();
  scheduleConversationSave();
  refreshConnectionStatus();
  QTimer::singleShot(0, this, [this]() { startNextChat(); });
}

void AIAssistant::watchTask(const QString &taskId, const QString &userPrompt,
                            const QString &conversationId, const QString &messageId)
{
  if(!safeTaskId(taskId))
  {
    appendSystem(tr("Ignored an invalid AI task identifier."), true);
    return;
  }
  m_KnownTasks.insert(taskId);
  updateEventTimer();
  m_RunningTasks.insert(taskId);
  m_TaskOffsets[taskId] = 0;
  m_TaskSequences[taskId] = 0;
  m_TaskTails.remove(taskId);
  m_TaskTexts.remove(taskId);
  if(!userPrompt.isEmpty())
    m_UserPrompts[taskId] = userPrompt;
  if(!conversationId.isEmpty())
    m_TaskConversationIds[taskId] = conversationId;
  if(!messageId.isEmpty())
    m_TaskMessageIds[taskId] = messageId;
}

void AIAssistant::unwatchTask(const QString &taskId)
{
  m_KnownTasks.remove(taskId);
  updateEventTimer();
}

void AIAssistant::updateEventTimer()
{
  if(!m_EventTimer)
    return;

  if(!m_ShuttingDown && !m_KnownTasks.isEmpty())
  {
    if(!m_EventTimer->isActive())
      m_EventTimer->start();
  }
  else if(m_EventTimer->isActive())
  {
    m_EventTimer->stop();
  }
}

void AIAssistant::ReportError(const QString &message)
{
  appendSystem(message.isEmpty() ? tr("The RenderDoc AI action failed without a message.") : message,
               true);
}

void AIAssistant::RebindCaptureFile(const QString &capturePath)
{
  const QString normalized = AIConversationStore::NormalizedCapturePath(capturePath);
  if(normalized.isEmpty() ||
     normalized == AIConversationStore::NormalizedCapturePath(m_CaptureConversationPath))
    return;

  // Retain the old key as a recoverable copy, then atomically write the same in-memory history
  // under the new RDC key. Future mutations use only the new path.
  saveConversations();
  m_CaptureConversationPath = capturePath;
  saveConversations();
}

void AIAssistant::pollTaskEvents()
{
  const QStringList tasks = m_KnownTasks.values();
  for(const QString &taskId : tasks)
    pollTaskEvent(taskId, MaxEventReadBytes);
}

qint64 AIAssistant::pollTaskEvent(const QString &taskId, qint64 maxReadBytes, bool *atEnd)
{
  if(atEnd)
    *atEnd = true;
  if(!safeTaskId(taskId) || maxReadBytes <= 0)
    return 0;

  QFile file(QDir(taskEventRoot()).filePath(taskId + lit(".jsonl")));
  if(!file.open(QIODevice::ReadOnly))
    return 0;
  qint64 offset = m_TaskOffsets.value(taskId, 0);
  if(file.size() < offset)
  {
    offset = 0;
    m_TaskTails.remove(taskId);
    m_TaskSequences[taskId] = 0;
  }
  if(!file.seek(offset))
    return 0;

  const QByteArray chunk = file.read(qMin(maxReadBytes, file.size() - offset));
  m_TaskOffsets[taskId] = file.pos();
  if(atEnd)
    *atEnd = file.pos() >= file.size();
  QByteArray pending = m_TaskTails.value(taskId) + chunk;
  int newline = -1;
  while((newline = pending.indexOf('\n')) >= 0)
  {
    const QByteArray line = pending.left(newline).trimmed();
    pending.remove(0, newline + 1);
    if(!line.isEmpty() && line.size() <= MaxEventLineBytes)
      consumeTaskEvent(taskId, line);
    if(!m_KnownTasks.contains(taskId))
      break;
  }
  if(pending.size() > MaxEventLineBytes)
  {
    appendSystem(tr("AI task event exceeded the safe line limit."), true);
    pending.clear();
  }
  if(m_KnownTasks.contains(taskId))
    m_TaskTails[taskId] = pending;
  return chunk.size();
}

void AIAssistant::consumeTaskEvent(const QString &taskId, const QByteArray &line)
{
  QJsonParseError parseError;
  const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
  if(parseError.error != QJsonParseError::NoError || !document.isObject())
    return;
  const QJsonObject event = document.object();
  if(event.value(lit("schema")).toString() != lit("replaylab-ai-task-event/v1") ||
     event.value(lit("taskId")).toString() != taskId)
    return;
  const qint64 sequence = event.value(lit("sequence")).toVariant().toLongLong();
  if(sequence <= m_TaskSequences.value(taskId, 0))
    return;
  m_TaskSequences[taskId] = sequence;

  const QString kind = event.value(lit("kind")).toString();
  const QString summary = event.value(lit("summary")).toString();
  const QJsonObject eventData = event.value(lit("data")).toObject();
  // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
  qint64 eventTimestampMs = event.value(lit("timestampUnixMs")).toVariant().toLongLong();
  if(eventTimestampMs <= 0)
    eventTimestampMs = QDateTime::currentMSecsSinceEpoch();
  if(kind == lit("queued"))
  {
    // A queued event starts a fresh attempt. Discard every provisional delta from the previous
    // attempt before accepting the new stream.
    m_TaskTexts.remove(taskId);
    setTaskState(taskId, lit("queued"), false);
    QString detail = summary;
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString backend = event.value(lit("backend")).toString();
    if(!backend.isEmpty())
      detail += (detail.isEmpty() ? QString() : lit(" | ")) + tr("Backend: %1").arg(backend);
    // i18n-ignore: Activity kind is a backend protocol identifier; its visible title is translated.
    addTaskActivity(taskId, lit("queued"), tr("Queued"), detail.left(4000), false, false,
                    eventTimestampMs);
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Queued..."));
  }
  else if(kind == lit("message-delta"))
  {
    // Planned specialists may stream concurrently. Their draft text is evidence for the
    // reviewer, not an authoritative assistant reply, and interleaving it would produce a
    // misleading transcript. The structured running events above still expose their live work.
    if(!eventData.value(lit("planTaskId")).toString().isEmpty() &&
       eventData.value(lit("stream")).toString() != lit("final"))
      return;
    QString &answer = m_TaskTexts[taskId];
    if(eventData.value(lit("reset")).toBool())
      answer.clear();
    if(answer.size() < MaxAnswerBytes)
      answer += event.value(lit("text")).toString().left(MaxAnswerBytes - answer.size());
    AIChatMessage *item = taskMessage(taskId);
    if(item)
    {
      item->provisionalText = answer;
      item->state = lit("running");
      item->activityExpanded = true;
      // A single poll can contain many token events. Coalesce full QTextDocument rebuilds to
      // keep long conversations responsive while still presenting live output at ~16 fps.
      if(m_ConversationRenderTimer && !m_ConversationRenderTimer->isActive())
        m_ConversationRenderTimer->start();
      else if(!m_ConversationRenderTimer)
        renderConversation();
    }
  }
  else if(kind == lit("tool-started"))
  {
    const QString tool = event.value(lit("tool")).toString();
    QString detail = tr("Running");
    QStringList argumentNames;
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    for(const QJsonValue &name : eventData.value(lit("argumentNames")).toArray())
      if(name.isString() && !name.toString().trimmed().isEmpty())
        argumentNames.push_back(name.toString().trimmed().left(128));
    if(!argumentNames.isEmpty())
      detail += lit("\n") + tr("Arguments: %1").arg(argumentNames.join(lit(", ")));
    // i18n-ignore: Activity kind is a backend protocol identifier; the tool name is displayed separately.
    addTaskActivity(taskId, lit("tool"), tool, detail.left(4000), false, false,
                    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                    eventTimestampMs, eventData.value(lit("activityId")).toString());
  }
  else if(kind == lit("tool-completed"))
  {
    const QString tool = event.value(lit("tool")).toString();
    const bool failed = eventData.value(lit("status")).toString() == lit("failed");
    QString detail = failed ? tr("Failed") : tr("Completed");
    const QString errorType = eventData.value(lit("errorType")).toString();
    if(failed && !errorType.isEmpty())
      detail += lit(" | ") + errorType;
    // i18n-ignore: Activity kind is a backend protocol identifier; the tool name is displayed separately.
    addTaskActivity(taskId, lit("tool"), tool, detail, true, failed, eventTimestampMs,
                    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                    eventData.value(lit("activityId")).toString());
  }
  else if(kind == lit("planning"))
  {
    setTaskState(taskId, lit("planning"), false);
    QString plannerDetail;
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString strategy = eventData.value(lit("strategy")).toString();
    if(!strategy.isEmpty())
      plannerDetail = tr("Strategy: %1").arg(strategy);
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    if(eventData.contains(lit("parallel")))
      plannerDetail += (plannerDetail.isEmpty() ? QString() : lit(" | ")) +
                       // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                       (eventData.value(lit("parallel")).toBool() ? tr("Parallel")
                                                                    : tr("Sequential"));
    addTaskActivity(taskId, lit("planner"),
                    summary.isEmpty() ? tr("Planning request") : summary,
                    // i18n-ignore: Stable planner activity identifier used for backend event correlation.
                    plannerDetail.left(4000), false, false, eventTimestampMs, lit("planner"));
    const QJsonArray tasks = eventData.value(lit("tasks")).toArray();
    for(const QJsonValue &taskValue : tasks)
    {
      if(!taskValue.isObject())
        continue;
      const QJsonObject task = taskValue.toObject();
      const QString taskSummary = task.value(lit("summary")).toString();
      const QString skill = task.value(lit("skill")).toString();
      const QString planTaskId = task.value(lit("id")).toString();
      addTaskActivity(taskId, lit("specialist-") + planTaskId,
                      taskSummary.isEmpty() ? planTaskId : taskSummary, skill, false, false,
                      eventTimestampMs, planTaskId);
    }
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Planning..."));
  }
  else if(kind == lit("running"))
  {
    setTaskState(taskId, lit("running"), false);
    const QString phase = eventData.value(lit("phase")).toString();
    const QString planTaskId = eventData.value(lit("planTaskId")).toString();
    const QString skill = eventData.value(lit("skill")).toString();
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString explicitActivityId = eventData.value(lit("activityId")).toString();
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    QString title = eventData.value(lit("activityTitle")).toString();
    // i18n-ignore: Task-event phase identifier used only for localized label selection.
    if(phase == lit("context-compaction"))
      title = tr("Compacting context");
    if(title.isEmpty())
      // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
      title = eventData.value(lit("activitySummary")).toString();
    if(title.isEmpty())
      title = summary;
    if(title.isEmpty())
      title = phase == lit("reviewer") ? tr("Reviewing and synthesizing") : tr("Agent working");
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    QString detail = eventData.value(lit("activityDetail")).toString();
    // i18n-ignore: Task-event phase identifier used only for localized detail selection.
    if(phase == lit("context-compaction"))
      detail = tr("Generation %1: %2 to %3 estimated tokens")
                   // i18n-ignore: ReplayLab task-event JSON field names defined by the backend protocol.
                   .arg(eventData.value(lit("contextGeneration")).toInt())
                   // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                   .arg(eventData.value(lit("estimatedTokensBefore")).toInt())
                   // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
                   .arg(eventData.value(lit("estimatedTokensAfter")).toInt());
    if(detail.isEmpty())
      detail = skill;
    const int current = eventData.value(lit("progressCurrent")).toInt();
    const int total = eventData.value(lit("progressTotal")).toInt();
    if(total > 0)
      detail += (detail.isEmpty() ? QString() : lit(" | ")) +
                tr("%1/%2").arg(current).arg(total);
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    const QString activityStatus = eventData.value(lit("status")).toString();
    // i18n-ignore: Task-event status identifier used only for control flow.
    const bool failed = activityStatus == lit("failed");
    // i18n-ignore: Task-event status identifier used only for control flow.
    const bool completed = activityStatus == lit("completed") || failed ||
                           // i18n-ignore: Task-event status identifier used only for control flow.
                           activityStatus == lit("declined") ||
                           // i18n-ignore: Task-event status identifier used only for control flow.
                           activityStatus == lit("cancelled");
    // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
    QString activityKind = eventData.value(lit("activityKind")).toString();
    if(activityKind.isEmpty())
      // i18n-ignore: Default activity kind is a backend protocol identifier.
      activityKind = phase.isEmpty() ? lit("running") : phase;
    if(!planTaskId.isEmpty() && explicitActivityId.isEmpty())
      activityKind += lit("-") + planTaskId;
    const QString activityId = explicitActivityId.isEmpty() ? planTaskId : explicitActivityId;
    addTaskActivity(taskId, activityKind.left(64), title.left(1000), detail.left(4000),
                    completed, failed, eventTimestampMs, activityId);
    setDiagnosticStatus(m_Status, DiagnosticLevel::Info, tr("Thinking..."));
  }
  else if(kind == lit("approval-required"))
  {
    addTaskActivity(taskId, lit("approval"), tr("Approval required"),
                    summary.isEmpty() ? tr("This action requires approval.") : summary, false,
                    true, eventTimestampMs);
  }
  else if(kind == lit("completed") || kind == lit("failed") || kind == lit("cancelled"))
  {
    if(kind == lit("completed"))
    {
      QString terminalStatus;
      if(normalizedTerminalStatus(eventData, terminalStatus))
      {
        if(terminalStatus == lit("needs-user-input"))    // i18n-ignore: task lifecycle state
        {
          ClarificationPrompt clarification;
          // i18n-ignore: ReplayLab task-event JSON field name defined by the backend protocol.
          if(clarificationFromJSON(eventData.value(lit("clarification")).toObject(), taskId,
                                   clarification))
          {
            bindClarificationOrigin(clarification, taskId);
            if(ConversationThread *thread = conversation(m_TaskConversationIds.value(taskId)))
              thread->clarification = clarification;
          }
        }
        finishTask(taskId, terminalStatus, summary, eventTimestampMs);
      }
      else
        // i18n-ignore: Task lifecycle state identifier used only for control flow.
        finishTask(taskId, lit("failed"), tr("AI result contained an invalid terminal status."),
                   eventTimestampMs);
    }
    else
    {
      finishTask(taskId, kind, summary, eventTimestampMs);
    }
  }
}

void AIAssistant::finishTask(const QString &taskId, const QString &kind, const QString &summary,
                             qint64 timestampMs)
{
  if(m_CompletedTasks.contains(taskId))
    return;
  m_CompletedTasks.insert(taskId);
  if(m_CompletedTasks.size() > 512)
  {
    m_CompletedTasks.clear();
    // finishChatProcess() polls TaskEvents before consulting this tombstone. Preserve the task
    // which caused compaction or its CLI JSON fallback would render the same answer a second time.
    m_CompletedTasks.insert(taskId);
  }

  const QString accumulated = m_TaskTexts.take(taskId);
  // Streaming deltas are provisional. A failed/cancelled terminal must never present a partial
  // model answer as the authoritative error; use only the terminal summary in that case.
  // i18n-ignore: Task lifecycle state identifiers used only for control flow.
  const bool answerTerminal = kind == lit("completed") || kind == lit("needs-user-input") ||
                              // i18n-ignore: Task lifecycle state identifier used only for control flow.
                              kind == lit("blocked");
  const QString result = answerTerminal && !accumulated.isEmpty() ? accumulated : summary;
  const QString conversationId = m_TaskConversationIds.value(taskId);
  const qint64 terminalTimestampMs =
      timestampMs > 0 ? timestampMs : QDateTime::currentMSecsSinceEpoch();
  // A provider heartbeat or streamed item can be the last event before the terminal turn event.
  // Close every remaining live row so a completed transcript never keeps saying "running".
  if(AIChatMessage *taskItem = taskMessage(taskId))
  {
    for(AIActivityItem &activity : taskItem->activity)
    {
      if(activity.completed)
        continue;
      activity.completed = true;
      activity.finishedTimestampMs = terminalTimestampMs;
      // i18n-ignore: Task lifecycle state identifier is used only for control flow.
      if(kind == lit("failed"))
        activity.error = true;
    }
  }
  if(answerTerminal)
  {
    QString title = tr("Completed");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    if(kind == lit("needs-user-input"))
      title = tr("Waiting for input");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("blocked"))
      title = tr("Blocked");
    // i18n-ignore: Activity kind is a backend protocol identifier; its visible title is translated.
    addTaskActivity(taskId, kind, title, QString(), true, false, terminalTimestampMs);
    setTaskState(taskId, kind, true);
    if(AIChatMessage *item = taskMessage(taskId))
      item->finishedTimestampMs = terminalTimestampMs;
    QString fallback = tr("Task completed.");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    if(kind == lit("needs-user-input"))
      fallback = tr("More information is required to continue.");
    // i18n-ignore: Task lifecycle state identifier used only for label selection.
    else if(kind == lit("blocked"))
      fallback = tr("The AI task is blocked.");
    appendAssistant(result.isEmpty() ? fallback : result, conversationId, taskId);
  }
  else
  {
    const QString state = kind == lit("cancelled") ? lit("cancelled") : lit("failed");
    AIChatMessage *item = taskMessage(taskId);
    if(item)
      item->provisionalText = result.isEmpty() ? tr("AI task failed without a message.") : result;
    addTaskActivity(taskId, state,
                    state == lit("cancelled") ? tr("Cancelled") : tr("Failed"), result, true,
                    // i18n-ignore: Task lifecycle state identifier is used only for error classification.
                    state != lit("cancelled"), terminalTimestampMs);
    setTaskState(taskId, state, true);
    if(item)
      item->finishedTimestampMs = terminalTimestampMs;
    if(ConversationThread *thread = conversation(conversationId))
      thread->unread = thread->id != m_ActiveConversationId;
  }
  m_UserPrompts.remove(taskId);
  unwatchTask(taskId);
  m_RunningTasks.remove(taskId);
  m_TaskOffsets.remove(taskId);
  m_TaskSequences.remove(taskId);
  m_TaskTails.remove(taskId);
  m_TaskConversationIds.remove(taskId);
  m_TaskMessageIds.remove(taskId);
  renderConversation(true);
  // Terminal output is the authoritative conversation state; persist it synchronously so an
  // immediate capture close cannot leave only the earlier queued/running local snapshot.
  saveConversations();
  scheduleConversationSave();
  if(m_RunningTasks.isEmpty())
  {
    setDiagnosticStatus(m_Status, DiagnosticLevel::Success,
                        m_Ctx.IsCaptureLoaded() ? tr("Connected") : tr("Chat ready"));
  }
}

void AIAssistant::OnCaptureLoaded()
{
  // A scratch/general-chat turn must never leak into a newly opened capture's conversation
  // namespace. Stop it, discard its queue, then hydrate this RDC's app-local history.
  clearCaptureTasks();
  const rdcstr captureFilename = m_Ctx.GetCaptureFilename();
  m_CaptureConversationPath =
      QString::fromUtf8(captureFilename.c_str(), int(captureFilename.size()));
  updateCaptureStatus();
  m_InstanceId.clear();
  loadConversations();
  refreshConnectionStatus();
}

void AIAssistant::OnCaptureClosed()
{
  if(m_ConversationSaveTimer)
    m_ConversationSaveTimer->stop();
  clearCaptureTasks();
  // CaptureContext has already cleared its filename before this callback. The cached key keeps
  // the final interrupted/running state bound to the capture that owned it.
  saveConversations();
  m_CaptureConversationPath.clear();
  m_InstanceId.clear();
  resetConversations();
  updateCaptureStatus();
}

void AIAssistant::OnSelectedEventChanged(uint32_t)
{
  updateCaptureStatus();
}

void AIAssistant::OnEventChanged(uint32_t)
{
  updateCaptureStatus();
}
