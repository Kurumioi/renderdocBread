/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
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

#include "ShaderCodeEditor.h"

#include <QAction>
#include <QApplication>
#include <QClipboard>
#include <QCursor>
#include <QInputDialog>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QMenu>
#include <QMimeData>
#include <QToolTip>
#include <QToolButton>
#include <QStyle>
#include <QTimer>
#include <QVBoxLayout>

#include "Code/CodeEditorService.h"
#include "Code/QRDUtils.h"
#include "Code/ScintillaSyntax.h"
#include "Widgets/FindReplace.h"
#include "scintilla/include/Scintilla.h"
#include "scintilla/include/qt/ScintillaEdit.h"

namespace
{
const int MinimumLineNumberDigits = 3;
const int SourceAnchorMarker = 23;
}

ShaderCodeEditor::ShaderCodeEditor(CodeEditorService *services, QWidget *parent)
    : QWidget(parent), m_Services(services)
{
  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(0);

  QHBoxLayout *navigation = new QHBoxLayout();
  navigation->setContentsMargins(0, 0, 0, 0);
  QToolButton *back = new QToolButton(this);
  back->setObjectName(QStringLiteral("shaderCodeBack")); // i18n-ignore: automation id.
  back->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
  back->setToolTip(tr("Navigate Back (Alt+Left)"));
  back->setAutoRaise(true);
  QToolButton *forward = new QToolButton(this);
  forward->setObjectName(QStringLiteral("shaderCodeForward")); // i18n-ignore: automation id.
  forward->setIcon(style()->standardIcon(QStyle::SP_ArrowForward));
  forward->setToolTip(tr("Navigate Forward (Alt+Right)"));
  forward->setAutoRaise(true);
  navigation->addWidget(back);
  navigation->addWidget(forward);
  navigation->addStretch(1);
  layout->addLayout(navigation);
  QObject::connect(back, &QToolButton::clicked, this, [this]() {
    if(m_Services) m_Services->navigateHistory(m_Editor, true);
  });
  QObject::connect(forward, &QToolButton::clicked, this, [this]() {
    if(m_Services) m_Services->navigateHistory(m_Editor, false);
  });
  auto updateNavigation = [this, back, forward]() {
    back->setEnabled(m_Services && m_Services->canNavigateHistory(true));
    forward->setEnabled(m_Services && m_Services->canNavigateHistory(false));
  };
  QTimer *navigationTimer = new QTimer(this);
  navigationTimer->setInterval(200);
  QObject::connect(navigationTimer, &QTimer::timeout, this, updateNavigation);
  navigationTimer->start();
  updateNavigation();

  m_FindReplace = new FindReplace(this);
  m_FindReplace->allowUserModeChange(false);
  m_FindReplace->hide();
  layout->addWidget(m_FindReplace);

  m_Editor = new ScintillaEdit(this);
  m_Editor->setObjectName(QStringLiteral("shaderCodeEditor"));    // i18n-ignore: automation id.
  m_Editor->setMarginLeft(4.0);
  m_Editor->setMarginTypeN(0, SC_MARGIN_NUMBER);
  m_Editor->markerDefine(SourceAnchorMarker, SC_MARK_EMPTY);
  m_Editor->setMarginWidthN(1, 0);
  m_Editor->setMarginWidthN(2, 0);
  m_Editor->styleSetFont(STYLE_DEFAULT, Formatter::FixedFont().family().toUtf8().constData());
  m_Editor->styleSetSize(STYLE_DEFAULT, Formatter::FixedFont().pointSize());
  m_Editor->setCodePage(SC_CP_UTF8);
  m_Editor->setTabWidth(4);
  m_Editor->setUseTabs(false);
  m_Editor->setWrapMode(SC_WRAP_NONE);
  m_Editor->setScrollWidth(1);
  m_Editor->setScrollWidthTracking(true);
  m_Editor->setCaretLineVisible(true);
  m_Editor->setCaretLineBackAlpha(32);
  m_Editor->setMultipleSelection(true);
  m_Editor->setAdditionalSelectionTyping(true);
  m_Editor->setMultiPaste(SC_MULTIPASTE_EACH);
  m_Editor->setMouseDwellTime(500);
  m_Editor->setContextMenuPolicy(Qt::CustomContextMenu);
  ConfigureSyntax(m_Editor, SCLEX_HLSL);
  UpdateLineNumberMargin();
  layout->addWidget(m_Editor, 1);
  m_DiagnosticSummary = new QToolButton(this);
  m_DiagnosticSummary->setObjectName(QStringLiteral("shaderCodeDiagnostics")); // i18n-ignore: automation id.
  m_DiagnosticSummary->setAutoRaise(true);
  m_DiagnosticSummary->hide();
  layout->addWidget(m_DiagnosticSummary);
  QObject::connect(m_DiagnosticSummary, &QToolButton::clicked, this,
                   [this]() { GoToLine(uint32_t(m_FirstDiagnosticLine)); });

  if(m_Services)
    m_Services->registerEditor(m_Editor, m_FileName, m_Language);

  QObject::connect(m_Editor, &ScintillaEdit::keyPressed, this,
                   [this](QKeyEvent *event) { HandleKeyPress(event); });
  QObject::connect(m_Editor, &ScintillaEdit::modified, this,
                   [this](int type, int position, int length, int, const QByteArray &, int, int, int) {
                     if(type & SC_MOD_BEFOREDELETE)
                     {
                       // Deleting a whole bookmarked line invalidates it. Editing its text
                       // without deleting the line keeps the bookmark, like an IDE bookmark.
                       const QSet<int> anchors = m_LineAnchors;
                       for(int anchor : anchors)
                       {
                         const int line = int(m_Editor->markerLineFromHandle(anchor));
                         if(line >= 0)
                         {
                           const int start = int(m_Editor->positionFromLine(line));
                           const int end = int(m_Editor->lineEndPosition(line));
                           if(position <= start &&
                              (position + length > end ||
                               (position < start && position + length >= end)))
                             RemoveLineAnchor(anchor);
                         }
                       }
                     }
                     if(type & (SC_MOD_INSERTTEXT | SC_MOD_DELETETEXT))
                     {
                       UpdateLineNumberMargin();
                       if(!m_SettingText && m_TextChangedCallback)
                         m_TextChangedCallback();
                     }
                   });
  QObject::connect(m_Editor, &QWidget::customContextMenuRequested, this,
                   &ShaderCodeEditor::ShowContextMenu);

  QObject::connect(m_FindReplace, &FindReplace::performFind, this,
                   [this]() { FindNext(m_FindReplace->direction() == FindReplace::Down); });
  QObject::connect(m_FindReplace, &FindReplace::performFindAll, this,
                   &ShaderCodeEditor::UpdateFindCount);
  QObject::connect(m_FindReplace, &FindReplace::performReplace, this,
                   &ShaderCodeEditor::ReplaceCurrent);
  QObject::connect(m_FindReplace, &FindReplace::performReplaceAll, this,
                   &ShaderCodeEditor::ReplaceAll);
  QObject::connect(m_FindReplace, &FindReplace::searchChanged, this,
                   &ShaderCodeEditor::FindIncremental);
  QObject::connect(m_FindReplace, &FindReplace::closeRequested, this,
                   &ShaderCodeEditor::CloseFind);
  QObject::connect(m_FindReplace, &FindReplace::keyPress, this,
                   [this](QKeyEvent *event) { HandleKeyPress(event); });
}

ShaderCodeEditor::~ShaderCodeEditor()
{
  ClearCallbacks();
  if(m_Services && m_Editor)
    m_Services->unregisterEditor(m_Editor);
}

rdcstr ShaderCodeEditor::GetText()
{
  return QString::fromUtf8(m_Editor->getText(m_Editor->textLength() + 1));
}

void ShaderCodeEditor::SetText(const rdcstr &text)
{
  const bool readOnly = m_Editor->readOnly();
  m_SettingText = true;
  ClearLineAnchors();
  if(m_Services) m_Services->clearNavigation(m_Editor);
  if(readOnly)
    m_Editor->setReadOnly(false);
  const QByteArray bytes = QString(text).toUtf8();
  const int newline = bytes.indexOf('\n');
  // New lines must use the loaded file's convention. Scintilla's Windows default is
  // CRLF even for an LF document, which otherwise silently introduces mixed endings.
  m_Editor->setEOLMode(newline >= 0 ? (newline > 0 && bytes[newline - 1] == '\r'
                                        ? SC_EOL_CRLF : SC_EOL_LF)
                                  : (bytes.contains('\r') ? SC_EOL_CR : SC_EOL_LF));
  m_Editor->setText(bytes.constData());
  m_Editor->emptyUndoBuffer();
  if(readOnly)
    m_Editor->setReadOnly(true);
  m_SettingText = false;
  UpdateLineNumberMargin();
}

void ShaderCodeEditor::SetReadOnly(bool readOnly)
{
  m_Editor->setReadOnly(readOnly);
}

void ShaderCodeEditor::SetNavigationEnabled(bool enabled)
{
  if(m_NavigationEnabled == enabled) return;
  m_NavigationEnabled = enabled;
  if(!m_Services) return;
  if(enabled) RegisterDocument();
  else m_Services->unregisterEditor(m_Editor);
}

bool ShaderCodeEditor::IsReadOnly()
{
  return m_Editor->readOnly();
}

void ShaderCodeEditor::SetFileName(const rdcstr &fileName)
{
  const QString value = QString(fileName);
  if(m_FileName == value)
    return;
  m_FileName = value;
  RegisterDocument();
}

void ShaderCodeEditor::SetLanguage(const rdcstr &language)
{
  QString value = QString(language).trimmed().toLower();
  if(value.isEmpty())
    value = QStringLiteral("hlsl");    // i18n-ignore: LSP language identifier.
  if(m_Language == value)
    return;
  m_Language = value;
  ConfigureSyntax(m_Editor, value == QStringLiteral("glsl") ? SCLEX_GLSL : SCLEX_HLSL);
  RegisterDocument();
}

bool ShaderCodeEditor::IsModified()
{
  return m_ForcedModified || m_Editor->modify();
}

void ShaderCodeEditor::SetModified(bool modified)
{
  if(!modified)
  {
    SetSavePoint();
    return;
  }
  m_ForcedModified = true;
}

void ShaderCodeEditor::SetSavePoint()
{
  m_ForcedModified = false;
  m_Editor->setSavePoint();
}

void ShaderCodeEditor::GoToLine(uint32_t line, uint32_t column)
{
  const int zeroBasedLine = qBound(0, int(line) - 1, qMax(0, int(m_Editor->lineCount()) - 1));
  const int lineStart = int(m_Editor->positionFromLine(zeroBasedLine));
  const int lineEnd = int(m_Editor->lineEndPosition(zeroBasedLine));
  const int position = qBound(
      lineStart, int(m_Editor->findColumn(zeroBasedLine, qMax(0, int(column) - 1))), lineEnd);
  if(m_Services)
    m_Services->rememberNavigation(m_Editor, position);
  GoToPosition(position);
}

int32_t ShaderCodeEditor::CreateLineAnchor(uint32_t line)
{
  if(line == 0 || line > uint32_t(m_Editor->lineCount()))
    return -1;
  const int anchor = int(m_Editor->markerAdd(int(line) - 1, SourceAnchorMarker));
  if(anchor >= 0)
    m_LineAnchors.insert(anchor);
  return anchor;
}

uint32_t ShaderCodeEditor::GetLineAnchorLine(int32_t anchor)
{
  if(!m_LineAnchors.contains(anchor))
    return 0;
  const int line = int(m_Editor->markerLineFromHandle(anchor));
  return line >= 0 ? uint32_t(line + 1) : 0;
}

void ShaderCodeEditor::RemoveLineAnchor(int32_t anchor)
{
  if(m_LineAnchors.remove(anchor))
    m_Editor->markerDeleteHandle(anchor);
}

void ShaderCodeEditor::ClearLineAnchors()
{
  for(int anchor : m_LineAnchors)
    m_Editor->markerDeleteHandle(anchor);
  m_LineAnchors.clear();
}

uint32_t ShaderCodeEditor::GetCursorLine()
{
  return uint32_t(qMax(0, int(m_Editor->lineFromPosition(m_Editor->currentPos()))) + 1);
}

uint32_t ShaderCodeEditor::GetCursorColumn()
{
  return uint32_t(qMax(0, int(m_Editor->column(m_Editor->currentPos()))) + 1);
}

void ShaderCodeEditor::SetDiagnostics(const rdcarray<ShaderEditorDiagnostic> &diagnostics)
{
  if(!m_Services)
    return;

  QList<CodeEditorService::Diagnostic> converted;
  converted.reserve(int(diagnostics.size()));
  for(const ShaderEditorDiagnostic &value : diagnostics)
  {
    CodeEditorService::Diagnostic diagnostic;
    const int line = qBound(0, int(value.line) - 1, qMax(0, int(m_Editor->lineCount()) - 1));
    const int lineStart = int(m_Editor->positionFromLine(line));
    const int lineEnd = int(m_Editor->lineEndPosition(line));
    diagnostic.start = qBound(
        lineStart, int(m_Editor->findColumn(line, qMax(0, int(value.column) - 1))), lineEnd);
    // Compilers place "expected ';'"-style errors just past the last token, i.e.
    // on the line break, where an indicator has no glyph to underline and is
    // invisible. Like other IDEs, pull such a range back onto the last visible
    // character of the line.
    if(diagnostic.start >= lineEnd && lineEnd > lineStart)
    {
      // Cover the whole last token (identifier / closing brackets), not one
      // character, so the squiggle is actually noticeable.
      int from = lineEnd;
      const QByteArray text = m_Editor->textRange(lineStart, lineEnd);
      int index = text.size();
      while(index > 0 && QChar::fromLatin1(text[index - 1]).isSpace())
        index--;
      const int tokenEnd = index;
      while(index > 0 && tokenEnd - index < 40)
      {
        const char c = text[index - 1];
        const bool word = QChar::fromLatin1(c).isLetterOrNumber() || c == '_' || c == ')' ||
                          c == ']' || c == '(' || c == '.';
        if(!word)
          break;
        index--;
      }
      from = lineStart + (index < tokenEnd ? index : qMax(0, tokenEnd - 1));
      diagnostic.start = qBound(lineStart, from, lineEnd - 1);
      diagnostic.length = qMax(1, (lineStart + tokenEnd) - diagnostic.start);
    }
    else
    {
      diagnostic.length = qMax(1, qMin(int(value.length), qMax(1, lineEnd - diagnostic.start)));
    }
    diagnostic.line = line;
    diagnostic.column = diagnostic.start - lineStart;
    diagnostic.severity =
        value.severity == ShaderEditorDiagnosticSeverity::Warning
            ? CodeEditorService::DiagnosticSeverity::Warning
        : value.severity == ShaderEditorDiagnosticSeverity::Information
            ? CodeEditorService::DiagnosticSeverity::Information
        : value.severity == ShaderEditorDiagnosticSeverity::Hint
            ? CodeEditorService::DiagnosticSeverity::Hint
            : CodeEditorService::DiagnosticSeverity::Error;
    diagnostic.message = QString(value.message);
    diagnostic.source = QString(value.source);
    converted.push_back(diagnostic);
  }
  m_Services->setExternalDiagnostics(m_Editor, converted);
}

void ShaderCodeEditor::SetTextChangedCallback(EventCallback callback)
{
  m_TextChangedCallback = callback;
}

void ShaderCodeEditor::SetSaveCallback(EventCallback callback)
{
  m_SaveCallback = callback;
}

void ShaderCodeEditor::SetAddToChatCallback(SelectionCallback callback)
{
  m_AddToChatCallback = callback;
}

void ShaderCodeEditor::SetDefinitionCallback(DefinitionCallback callback)
{
  m_DefinitionCallback = callback;
}

void ShaderCodeEditor::ClearCallbacks()
{
  m_TextChangedCallback = EventCallback();
  m_SaveCallback = EventCallback();
  m_AddToChatCallback = SelectionCallback();
  m_DefinitionCallback = DefinitionCallback();
}

void ShaderCodeEditor::GoToPosition(int position)
{
  const int bounded = qBound(0, position, int(m_Editor->length()));
  m_Editor->gotoPos(bounded);
  m_Editor->scrollCaret();
  m_Editor->QWidget::setFocus(Qt::OtherFocusReason);
}

void ShaderCodeEditor::OpenDefinition(const QString &fileName, int line, int column)
{
  if(m_DefinitionCallback)
    m_DefinitionCallback(fileName, uint32_t(qMax(0, line) + 1), uint32_t(qMax(0, column) + 1));
}

void ShaderCodeEditor::SetStatusMessage(const QString &message)
{
  m_Editor->setToolTip(message);
  if(!message.isEmpty())
    QToolTip::showText(QCursor::pos(), message, m_Editor);
}

void ShaderCodeEditor::SetDiagnosticSummary(int errors, int warnings, const QString &details,
                                           int firstLine)
{
  m_FirstDiagnosticLine = qMax(1, firstLine);
  m_DiagnosticSummary->setText(tr("Errors: %1 · Warnings: %2").arg(errors).arg(warnings));
  m_DiagnosticSummary->setToolTip(details);
  m_DiagnosticSummary->setVisible(errors + warnings > 0);
}

void ShaderCodeEditor::RegisterDocument()
{
  if(m_Services && m_NavigationEnabled)
    m_Services->registerEditor(m_Editor, m_FileName, m_Language);
}

void ShaderCodeEditor::UpdateLineNumberMargin()
{
  const int digits = qMax(MinimumLineNumberDigits, QString::number(m_Editor->lineCount()).size());
  const QString sample(digits + 1, QLatin1Char('9'));
  m_Editor->setMarginWidthN(0, m_Editor->textWidth(STYLE_LINENUMBER, sample.toUtf8().constData()));
}

void ShaderCodeEditor::HandleKeyPress(QKeyEvent *event)
{
  if(!event)
    return;

  if(event->matches(QKeySequence::Save))
  {
    if(m_SaveCallback)
      m_SaveCallback();
    event->accept();
  }
  else if(event->matches(QKeySequence::Find))
  {
    ShowFind(false);
    event->accept();
  }
  else if(event->matches(QKeySequence::Replace))
  {
    ShowFind(true);
    event->accept();
  }
  else if(event->key() == Qt::Key_F3)
  {
    FindNext(!(event->modifiers() & Qt::ShiftModifier));
    event->accept();
  }
  else if(event->key() == Qt::Key_G && event->modifiers() == Qt::ControlModifier)
  {
    bool accepted = false;
    const int line = QInputDialog::getInt(this, tr("Go to Line"), tr("Line:"),
                                          int(GetCursorLine()), 1,
                                          qMax(1, int(m_Editor->lineCount())), 1, &accepted);
    if(accepted)
      GoToLine(uint32_t(line));
    event->accept();
  }
  else if(event->key() == Qt::Key_Escape && m_FindReplace->isVisible())
  {
    CloseFind();
    event->accept();
  }
}

void ShaderCodeEditor::ShowFind(bool replace)
{
  const QByteArray selected = m_Editor->getSelText();
  if(!selected.isEmpty() && !selected.contains('\n'))
    m_FindReplace->setFindText(QString::fromUtf8(selected));
  m_FindReplace->setReplaceMode(replace);
  m_FindReplace->show();
  m_FindReplace->takeFocus();
  UpdateFindCount();
}

void ShaderCodeEditor::CloseFind()
{
  m_FindReplace->hide();
  m_Editor->QWidget::setFocus(Qt::OtherFocusReason);
}

int ShaderCodeEditor::SearchFlags() const
{
  int flags = 0;
  if(m_FindReplace->matchCase())
    flags |= SCFIND_MATCHCASE;
  if(m_FindReplace->matchWord())
    flags |= SCFIND_WHOLEWORD;
  if(m_FindReplace->regexp())
    flags |= SCFIND_REGEXP | SCFIND_POSIX;
  return flags;
}

void ShaderCodeEditor::FindNext(bool down)
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setResultCount(0, 0);
    return;
  }

  const int start = down ? int(m_Editor->selectionEnd()) : int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  QPair<int, int> result =
      m_Editor->findText(SearchFlags(), needle.constData(), start, down ? int(m_Editor->length()) : 0);
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    m_FindReplace->setResultCount(0, 0);
    return;
  }
  if(result.first < 0)
    result = m_Editor->findText(SearchFlags(), needle.constData(),
                                down ? 0 : int(m_Editor->length()), down ? start : start);
  if(result.first >= 0)
  {
    m_Editor->setSelection(result.second, result.first);
    m_Editor->scrollCaret();
  }
  UpdateFindCount();
}

void ShaderCodeEditor::FindIncremental()
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setSearchError(QString());
    m_FindReplace->setResultCount(0, 0);
    return;
  }

  const int start = int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  QPair<int, int> result =
      m_Editor->findText(SearchFlags(), needle.constData(), start, int(m_Editor->length()));
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    m_FindReplace->setResultCount(0, 0);
    return;
  }
  if(result.first < 0 && start > 0)
    result = m_Editor->findText(SearchFlags(), needle.constData(), 0, start);
  if(result.first >= 0)
  {
    m_Editor->setSelection(result.second, result.first);
    m_Editor->scrollCaret();
  }
  UpdateFindCount();
}

void ShaderCodeEditor::UpdateFindCount()
{
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
  {
    m_FindReplace->setResultCount(0, 0);
    m_FindReplace->setSearchError(QString());
    return;
  }

  int total = 0;
  int current = 0;
  int position = 0;
  const int selectionStart = int(m_Editor->selectionStart());
  m_Editor->setStatus(SC_STATUS_OK);
  while(position <= m_Editor->length() && total < 100000)
  {
    const QPair<int, int> result = m_Editor->findText(
        SearchFlags(), needle.constData(), position, int(m_Editor->length()));
    if(m_Editor->status() == SC_STATUS_WARN_REGEX)
    {
      m_FindReplace->setSearchError(tr("Invalid expression"));
      m_FindReplace->setResultCount(0, 0);
      return;
    }
    if(result.first < 0)
      break;
    total++;
    if(result.first <= selectionStart)
      current = total;
    if(result.second > result.first)
      position = result.second;
    else
    {
      const int advanced = int(m_Editor->positionAfter(result.first));
      if(advanced <= result.first)
        break;
      position = advanced;
    }
  }
  m_FindReplace->setResultCount(current, total);
  m_FindReplace->setSearchError(QString());
}

void ShaderCodeEditor::ReplaceCurrent()
{
  if(IsReadOnly())
    return;

  const QByteArray needle = m_FindReplace->findText().toUtf8();
  if(needle.isEmpty())
    return;
  const int selectionStart = int(m_Editor->selectionStart());
  const int selectionEnd = int(m_Editor->selectionEnd());
  m_Editor->setStatus(SC_STATUS_OK);
  m_Editor->setSearchFlags(SearchFlags());
  m_Editor->setTargetRange(selectionStart, selectionEnd);
  const int found = int(m_Editor->searchInTarget(needle.size(), needle.constData()));
  if(m_Editor->status() == SC_STATUS_WARN_REGEX)
  {
    m_FindReplace->setSearchError(tr("Invalid expression"));
    return;
  }
  if(found != selectionStart || m_Editor->targetEnd() != selectionEnd)
  {
    FindNext(true);
    return;
  }

  const QByteArray replacement = m_FindReplace->replaceText().toUtf8();
  if(m_FindReplace->regexp())
    m_Editor->replaceTargetRE(replacement.size(), replacement.constData());
  else
    m_Editor->replaceTarget(replacement.size(), replacement.constData());
  m_Editor->setEmptySelection(m_Editor->targetEnd());
  FindNext(true);
}

void ShaderCodeEditor::ReplaceAll()
{
  if(IsReadOnly())
    return;
  const QByteArray needle = m_FindReplace->findText().toUtf8();
  const QByteArray replacement = m_FindReplace->replaceText().toUtf8();
  if(needle.isEmpty())
    return;

  m_Editor->beginUndoAction();
  m_Editor->setStatus(SC_STATUS_OK);
  m_Editor->setSearchFlags(SearchFlags());
  int position = 0;
  int replacements = 0;
  while(position <= m_Editor->length() && replacements < 100000)
  {
    m_Editor->setTargetStart(position);
    m_Editor->setTargetEnd(m_Editor->length());
    const int found = int(m_Editor->searchInTarget(needle.size(), needle.constData()));
    if(m_Editor->status() == SC_STATUS_WARN_REGEX)
    {
      m_Editor->endUndoAction();
      m_FindReplace->setSearchError(tr("Invalid expression"));
      return;
    }
    if(found < 0)
      break;
    const int oldEnd = int(m_Editor->targetEnd());
    if(m_FindReplace->regexp())
      m_Editor->replaceTargetRE(replacement.size(), replacement.constData());
    else
      m_Editor->replaceTarget(replacement.size(), replacement.constData());
    position = int(m_Editor->targetEnd());
    if(oldEnd == found && position <= found)
    {
      const int advanced = int(m_Editor->positionAfter(found));
      if(advanced <= found)
        break;
      position = advanced;
    }
    replacements++;
  }
  m_Editor->endUndoAction();
  UpdateFindCount();
}

void ShaderCodeEditor::ShowContextMenu(const QPoint &position)
{
  QMenu *menu = new QMenu(this);
  menu->setAttribute(Qt::WA_DeleteOnClose);
  QAction *undo = menu->addAction(tr("Undo"), m_Editor, &ScintillaEdit::undo);
  QAction *redo = menu->addAction(tr("Redo"), m_Editor, &ScintillaEdit::redo);
  undo->setEnabled(!IsReadOnly() && m_Editor->canUndo());
  redo->setEnabled(!IsReadOnly() && m_Editor->canRedo());
  menu->addSeparator();
  QAction *cut = menu->addAction(tr("Cut"), m_Editor, &ScintillaEdit::cut);
  QAction *copy = menu->addAction(tr("Copy"), m_Editor, &ScintillaEdit::copy);
  QAction *paste = menu->addAction(tr("Paste"), m_Editor, &ScintillaEdit::paste);
  cut->setEnabled(!IsReadOnly() && m_Editor->selectionEnd() > m_Editor->selectionStart());
  copy->setEnabled(m_Editor->selectionEnd() > m_Editor->selectionStart());
  paste->setEnabled(!IsReadOnly() && QApplication::clipboard()->mimeData()->hasText());
  menu->addAction(tr("Select All"), m_Editor, &ScintillaEdit::selectAll);
  menu->addSeparator();
  QAction *definition = menu->addAction(tr("Go to Definition"));
  definition->setShortcut(QKeySequence(Qt::Key_F12));
  definition->setEnabled(m_Services != NULL);
  QObject::connect(definition, &QAction::triggered, this, [this]() {
    if(m_Services)
      m_Services->requestDefinition(m_Editor);
  });
  QAction *complete = menu->addAction(tr("Trigger Completion"));
  complete->setShortcut(QKeySequence(Qt::CTRL | Qt::Key_Space));
  complete->setEnabled(m_Services != NULL);
  QObject::connect(complete, &QAction::triggered, this, [this]() {
    if(m_Services)
      m_Services->requestCompletion(m_Editor, true);
  });
  QAction *addToChat = menu->addAction(tr("Add to chat"));
  addToChat->setEnabled(m_AddToChatCallback &&
                        m_Editor->selectionEnd() > m_Editor->selectionStart());
  QObject::connect(addToChat, &QAction::triggered, this, &ShaderCodeEditor::AddSelectionToChat);
  menu->popup(m_Editor->viewport()->mapToGlobal(position));
}

void ShaderCodeEditor::AddSelectionToChat()
{
  if(!m_AddToChatCallback)
    return;
  const int start = int(m_Editor->selectionStart());
  const int end = int(m_Editor->selectionEnd());
  if(end <= start)
    return;
  const QString text = QString::fromUtf8(m_Editor->textRange(start, end));
  const uint32_t startLine = uint32_t(m_Editor->lineFromPosition(start) + 1);
  const uint32_t endLine = uint32_t(m_Editor->lineFromPosition(qMax(start, end - 1)) + 1);
  m_AddToChatCallback(text, m_FileName, startLine, endLine);
}
