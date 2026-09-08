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

#include "CodeEditorService.h"

#include <QCoreApplication>
#include <QDir>
#include <QEvent>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QKeyEvent>
#include <QMenu>
#include <QAction>
#include <QPointer>
#include <QPalette>
#include <QProcess>
#include <QRegularExpression>
#include <QSet>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>
#include <algorithm>
#include <cctype>
#include <limits>

#include "scintilla/include/Scintilla.h"
#include "scintilla/include/qt/ScintillaEdit.h"

namespace
{
const int ErrorIndicator = 8;
const int WarningIndicator = 9;
const int SymbolOccurrenceIndicator = 10;
const int FunctionNameIndicator = 11;
const int VariableNameIndicator = 12;
const int DiagnosticsDebounceMS = 160;
const int MaxCompletionItems = 256;
const int MaxDiagnostics = 2000;
const int MaxMessageBytes = 4 * 1024 * 1024;
const int MaxHeaderBytes = 16 * 1024;
const int NavigationHistoryLimit = 128;

struct SmartNewlineEdit
{
  QByteArray replacement;
  int caretOffset = 0;
};

struct TextReplacement
{
  int start = 0;
  int removedLength = 0;
  QByteArray inserted;
};

bool isHorizontalSpace(char c)
{
  return c == ' ' || c == '\t';
}

QByteArray leadingWhitespace(const QByteArray &text)
{
  int length = 0;
  while(length < text.size() && isHorizontalSpace(text[length]))
    length++;
  return text.left(length);
}

char lastNonWhitespace(const QByteArray &text)
{
  for(int i = text.size() - 1; i >= 0; i--)
    if(!isHorizontalSpace(text[i]) && text[i] != '\r')
      return text[i];
  return '\0';
}

char firstNonWhitespace(const QByteArray &text)
{
  for(char c : text)
    if(!isHorizontalSpace(c) && c != '\r')
      return c;
  return '\0';
}

SmartNewlineEdit planSmartNewline(const QByteArray &text, int replaceStart, int replaceEnd,
                                  const QByteArray &eol, const QByteArray &indentUnit,
                                  bool indentAfterColon)
{
  SmartNewlineEdit ret;
  replaceStart = qBound(0, replaceStart, text.size());
  replaceEnd = qBound(replaceStart, replaceEnd, text.size());

  const int previousLF = replaceStart > 0 ? text.lastIndexOf('\n', replaceStart - 1) : -1;
  const int previousCR = replaceStart > 0 ? text.lastIndexOf('\r', replaceStart - 1) : -1;
  const int currentLineStart = qMax(previousLF, previousCR) + 1;
  const int nextLF = text.indexOf('\n', replaceEnd);
  const int nextCR = text.indexOf('\r', replaceEnd);
  const int currentLineEnd = nextLF < 0   ? (nextCR < 0 ? text.size() : nextCR)
                             : nextCR < 0 ? nextLF
                                          : qMin(nextLF, nextCR);
  const QByteArray before = text.mid(currentLineStart, replaceStart - currentLineStart);
  const QByteArray after = text.mid(replaceEnd, currentLineEnd - replaceEnd);
  const QByteArray indentation = leadingWhitespace(before);
  const char previous = lastNonWhitespace(before);
  const char next = firstNonWhitespace(after);

  if(previous == '{' && next == '}')
  {
    ret.replacement = eol + indentation + indentUnit + eol + indentation;
    ret.caretOffset = eol.size() + indentation.size() + indentUnit.size();
  }
  else
  {
    const bool increaseIndent = previous == '{' || previous == '[' || previous == '(' ||
                                (indentAfterColon && previous == ':');
    ret.replacement = eol + indentation + (increaseIndent ? indentUnit : QByteArray());
    ret.caretOffset = ret.replacement.size();
  }
  return ret;
}

QByteArray eolBytes(int mode)
{
  if(mode == SC_EOL_CRLF)
    return QByteArray("\r\n");
  if(mode == SC_EOL_CR)
    return QByteArray("\r");
  return QByteArray("\n");
}

int transformedPosition(int position, const QList<TextReplacement> &replacements)
{
  const int originalPosition = position;
  int cumulativeDelta = 0;
  for(const TextReplacement &replacement : replacements)
  {
    const int end = replacement.start + replacement.removedLength;
    if(originalPosition < replacement.start)
      continue;
    if(originalPosition <= end)
      return replacement.start + cumulativeDelta + replacement.inserted.size();
    else
      cumulativeDelta += replacement.inserted.size() - replacement.removedLength;
  }
  return originalPosition + cumulativeDelta;
}

bool hasPrimaryModifier(Qt::KeyboardModifiers modifiers, bool allowShift)
{
  modifiers &= ~Qt::KeypadModifier;
  if(allowShift)
    modifiers &= ~Qt::ShiftModifier;
  return modifiers == Qt::ControlModifier || modifiers == Qt::MetaModifier;
}

long scintillaColour(int r, int g, int b)
{
  return long(r) | (long(g) << 8) | (long(b) << 16);
}

bool isIdentifierByte(char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
}

bool isIdentifierStartByte(char c)
{
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_';
}

QPair<int, int> identifierRange(const QByteArray &text, int position)
{
  position = qBound(0, position, text.size());

  int start = position;
  if(start == text.size() || (start > 0 && !isIdentifierByte(text[start])))
    start--;

  while(start >= 0 && isIdentifierByte(text[start]))
    start--;
  start++;

  int end = qMax(start, position);
  while(end < text.size() && isIdentifierByte(text[end]))
    end++;

  if(start >= end || start < 0 || !isIdentifierStartByte(text[start]))
    return qMakePair(position, position);

  return qMakePair(start, end);
}

QString identifierAt(const QByteArray &text, int position, QPair<int, int> *range = NULL)
{
  const QPair<int, int> r = identifierRange(text, position);
  if(range)
    *range = r;
  return QString::fromUtf8(text.mid(r.first, r.second - r.first));
}

int lineStart(const QByteArray &text, int line)
{
  if(line <= 0)
    return 0;

  int currentLine = 0;
  for(int i = 0; i < text.size(); i++)
  {
    if(text[i] == '\n' && ++currentLine == line)
      return i + 1;
  }

  return text.size();
}

int lineEnd(const QByteArray &text, int start)
{
  const int newline = text.indexOf('\n', qBound(0, start, text.size()));
  return newline < 0 ? text.size() : newline;
}

QJsonObject lspPosition(const QByteArray &text, int bytePosition)
{
  bytePosition = qBound(0, bytePosition, text.size());
  int line = 0;
  int start = 0;
  for(int i = 0; i < bytePosition; i++)
  {
    if(text[i] == '\n')
    {
      line++;
      start = i + 1;
    }
  }

  // LSP 3.x defaults to UTF-16 code units. QString::size() has exactly that unit on Qt 5.
  const int character = QString::fromUtf8(text.mid(start, bytePosition - start)).size();
  QJsonObject ret;
  ret[QStringLiteral("line")] = line;
  ret[QStringLiteral("character")] = character;
  return ret;
}

int bytePosition(const QByteArray &text, const QJsonObject &position)
{
  const int line = qMax(0, position.value(QStringLiteral("line")).toInt());
  const int character = qMax(0, position.value(QStringLiteral("character")).toInt());
  const int start = lineStart(text, line);
  const int end = lineEnd(text, start);
  const QString lineText = QString::fromUtf8(text.mid(start, end - start));
  return start + lineText.left(qMin(character, lineText.size())).toUtf8().size();
}

int utf16IndexToByte(const QString &text, int utf16Index)
{
  return text.left(qBound(0, utf16Index, text.size())).toUtf8().size();
}

QString normaliseLanguage(const QString &language, const QString &fileName)
{
  QString ret = language.trimmed().toLower();
  if(ret == QStringLiteral("c++") || ret == QStringLiteral("cpp"))
    return QStringLiteral("cpp");
  if(ret == QStringLiteral("hlsl") || ret == QStringLiteral("glsl") ||
     ret == QStringLiteral("slang") || ret == QStringLiteral("python"))
    return ret;

  const QString suffix = QFileInfo(fileName).suffix().toLower();
  if(suffix == QStringLiteral("hlsl") || suffix == QStringLiteral("hlsli") ||
     suffix == QStringLiteral("fx") || suffix == QStringLiteral("fxh"))
    return QStringLiteral("hlsl");
  if(suffix == QStringLiteral("glsl") || suffix == QStringLiteral("vert") ||
     suffix == QStringLiteral("frag") || suffix == QStringLiteral("geom") ||
     suffix == QStringLiteral("comp"))
    return QStringLiteral("glsl");
  if(suffix == QStringLiteral("slang"))
    return QStringLiteral("slang");
  if(suffix == QStringLiteral("py"))
    return QStringLiteral("python");
  return ret.isEmpty() ? QStringLiteral("hlsl") : ret;
}

QStringList languageKeywords(const QString &language)
{
  const QString common = QStringLiteral(
      "break case const continue default discard do else false for if in inout out return static "
      "struct switch true while void bool int uint half float double matrix vector namespace "
      "typedef using class enum cbuffer register packoffset row_major column_major precise "
      "groupshared "
      "linear centroid nointerpolation noperspective sample globallycoherent uniform volatile");
  QStringList ret = common.split(QLatin1Char(' '), QString::SkipEmptyParts);

  if(language == QStringLiteral("python"))
  {
    return QStringLiteral(
               "False None True and as assert async await break class continue def del elif else "
               "except finally for from global if import in is lambda nonlocal not or pass raise "
               "return try while with yield")
        .split(QLatin1Char(' '), QString::SkipEmptyParts);
  }

  ret +=
      QStringLiteral(
          "Buffer ByteAddressBuffer StructuredBuffer RWBuffer RWByteAddressBuffer "
          "RWStructuredBuffer Texture1D Texture1DArray Texture2D Texture2DArray Texture2DMS "
          "Texture2DMSArray Texture3D TextureCube RWTexture1D RWTexture1DArray RWTexture2D "
          "RWTexture2DArray RWTexture3D SamplerState SamplerComparisonState InputPatch "
          "OutputPatch AppendStructuredBuffer ConsumeStructuredBuffer abs acos all any asin "
          "atan atan2 ceil clamp clip cos cross ddx ddy degrees determinant distance dot exp exp2 "
          "faceforward floor fma fmod frac frexp fwidth isfinite isinf isnan ldexp length lerp log "
          "log2 max min modf mul normalize pow radians rcp reflect refract round rsqrt saturate "
          "sign "
          "sin sincos smoothstep sqrt step tan transpose trunc texture textureLod textureGrad "
          "texelFetch imageLoad imageStore barrier memoryBarrier atomicAdd SV_Position SV_Target "
          "SV_VertexID SV_InstanceID SV_DispatchThreadID SV_GroupID SV_GroupIndex SV_GroupThreadID")
          .split(QLatin1Char(' '), QString::SkipEmptyParts);
  return ret;
}

QByteArray maskedCode(const QByteArray &text, const QString &language)
{
  QByteArray masked = text;
  enum State
  {
    Normal,
    LineComment,
    BlockComment,
    SingleString,
    DoubleString,
    TripleSingleString,
    TripleDoubleString,
  };
  State state = Normal;
  bool escaped = false;
  const bool python = language == QStringLiteral("python");

  for(int i = 0; i < text.size(); i++)
  {
    const char c = text[i];
    const char next = i + 1 < text.size() ? text[i + 1] : '\0';

    if(state == Normal)
    {
      if((python && c == '#') || (!python && c == '/' && next == '/'))
      {
        state = LineComment;
        masked[i] = ' ';
        if(!python)
          masked[++i] = ' ';
      }
      else if(!python && c == '/' && next == '*')
      {
        state = BlockComment;
        masked[i] = ' ';
        masked[++i] = ' ';
      }
      else if(python && c == '\'' && i + 2 < text.size() && text[i + 1] == '\'' && text[i + 2] == '\'')
      {
        state = TripleSingleString;
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
      }
      else if(python && c == '"' && i + 2 < text.size() && text[i + 1] == '"' && text[i + 2] == '"')
      {
        state = TripleDoubleString;
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
      }
      else if(c == '\'')
      {
        state = SingleString;
        escaped = false;
        masked[i] = ' ';
      }
      else if(c == '"')
      {
        state = DoubleString;
        escaped = false;
        masked[i] = ' ';
      }
    }
    else if(state == LineComment)
    {
      if(c == '\n')
        state = Normal;
      else
        masked[i] = ' ';
    }
    else if(state == BlockComment)
    {
      if(c == '*' && next == '/')
      {
        masked[i] = masked[i + 1] = ' ';
        i++;
        state = Normal;
      }
      else if(c != '\n')
      {
        masked[i] = ' ';
      }
    }
    else if(state == TripleSingleString || state == TripleDoubleString)
    {
      const char quote = state == TripleSingleString ? '\'' : '"';
      if(c == quote && i + 2 < text.size() && text[i + 1] == quote && text[i + 2] == quote)
      {
        masked[i] = masked[i + 1] = masked[i + 2] = ' ';
        i += 2;
        state = Normal;
      }
      else if(c != '\n')
      {
        masked[i] = ' ';
      }
    }
    else
    {
      const char quote = state == SingleString ? '\'' : '"';
      if(c != '\n')
        masked[i] = ' ';
      if(c == quote && !escaped)
        state = Normal;
      if(c == '\n' && !escaped)
        state = Normal;
      escaped = c == '\\' && !escaped;
      if(c != '\\')
        escaped = false;
    }
  }

  return masked;
}

QString cleanSignature(const QString &signature)
{
  QString ret = signature;
  ret.replace(QRegularExpression(QStringLiteral("\\s+")), QStringLiteral(" "));
  return ret.trimmed().left(512);
}

QString pathFromUri(const QString &uri)
{
  const QUrl url(uri);
  return url.isLocalFile() ? QDir::cleanPath(url.toLocalFile()) : QString();
}

QHash<int, int> codeDelimiterPairs(const QByteArray &code)
{
  QList<int> stack;
  QHash<int, int> pairs;
  for(int i = 0; i < code.size(); ++i)
  {
    if(code[i] == '{' || code[i] == '(' || code[i] == '[')
      stack.push_back(i);
    else if((code[i] == '}' || code[i] == ')' || code[i] == ']') && !stack.isEmpty())
    {
      const int open = stack.takeLast();
      if((code[open] == '{' && code[i] == '}') ||
         (code[open] == '(' && code[i] == ')') ||
         (code[open] == '[' && code[i] == ']'))
        pairs.insert(open, i);
    }
  }
  return pairs;
}

struct FollowingDeclarator
{
  QString name;
  QString valueType;
  int position = -1;
};

QList<FollowingDeclarator> followingDeclarators(const QByteArray &code,
                                               const QHash<int, int> &pairs, int position,
                                               QString baseType)
{
  QList<FollowingDeclarator> ret;
  // Pointer/reference modifiers belong to each declarator, not the common base type.
  while(baseType.endsWith(QLatin1Char('*')) || baseType.endsWith(QLatin1Char('&')))
    baseType = baseType.left(baseType.size() - 1).trimmed();
  auto skipSpace = [&code](int index) {
    while(index < code.size() && std::isspace(static_cast<unsigned char>(code[index]))) ++index;
    return index;
  };
  for(int i = position; i < code.size() && ret.size() < 256; ++i)
  {
    const char c = code[i];
    if(c == ';' || c == ')' || c == '}' || c == ']') break;
    if(c == '(' || c == '{' || c == '[')
    {
      if(!pairs.contains(i)) break;
      i = pairs.value(i); // Constructor arguments, array indices and aggregate initializers.
      continue;
    }
    if(c != ',') continue;
    int start = skipSpace(i + 1);
    QString valueType = baseType;
    while(start < code.size() && (code[start] == '*' || code[start] == '&'))
    {
      valueType += QChar::fromLatin1(code[start]);
      start = skipSpace(start + 1);
    }
    if(start >= code.size() || !isIdentifierStartByte(code[start])) break;
    int end = start + 1;
    while(end < code.size() && isIdentifierByte(code[end])) ++end;
    const int next = skipSpace(end);
    if(next >= code.size() ||
       (code[next] != '=' && code[next] != ';' && code[next] != ',' &&
        code[next] != ':' && code[next] != '[' && code[next] != ')'))
      break; // A new typed parameter (e.g. ', float other') is not another declarator.
    FollowingDeclarator declaration;
    declaration.name = QString::fromUtf8(code.mid(start, end - start));
    declaration.valueType = valueType;
    declaration.position = start;
    ret.push_back(declaration);
    i = end - 1;
  }
  return ret;
}
}    // namespace

struct CodeEditorService::Impl
{
  struct EditorState;

  struct Symbol
  {
    QString name;
    QString signature;
    EditorState *state = NULL;
    int position = -1;
    int scopeStart = 0;
    int scopeEnd = 0;
    QString ownerType;
    QString valueType;
  };

  struct EditorState
  {
    QPointer<ScintillaEdit> editor;
    QString fileName;
    QString uri;
    QString language;
    QByteArray text;
    QList<Symbol> symbols;
    QList<Diagnostic> localDiagnostics;
    QList<Diagnostic> lspDiagnostics;
    QList<Diagnostic> externalDiagnostics;
    int version = 1;
    quint64 editRevision = 1;
    bool textDirty = false;
    bool diagnosticsDirty = false;
    bool openSent = false;
    bool diagnosticTipActive = false;
    QByteArray highlightedWord;
    int highlightedVersion = -1;
    int coloredVersion = -1;
    QByteArray maskedText;
    int maskedVersion = -1;
  };

  enum class PendingKind
  {
    Initialize,
    Completion,
    Definition,
  };

  struct PendingRequest
  {
    PendingKind kind = PendingKind::Completion;
    QPointer<ScintillaEdit> editor;
    ScintillaEdit *sourceEditor = NULL;    // identity only; never dereferenced
    QString prefix;
    int version = 0;
    quint64 editRevision = 0;
    int position = -1;
    Location localFallback;
    QPointer<ScintillaEdit> localFallbackEditor;
    QHash<ScintillaEdit *, quint64> documentRevisions;
  };

  struct NavigationEntry
  {
    QPointer<ScintillaEdit> editor;
    QString fileName;
    int position = -1;
    int line = -1;
    int column = -1;
  };

  QPointer<QMenu> referencesMenu;
  bool deliveringNavigation = false;

  explicit Impl(CodeEditorService *q) : owner(q)
  {
    diagnosticTimer = new QTimer(owner);
    diagnosticTimer->setSingleShot(true);
    diagnosticTimer->setInterval(DiagnosticsDebounceMS);
    QObject::connect(diagnosticTimer, &QTimer::timeout, owner, [this]() { flushDirtyDocuments(); });

    process = new QProcess(owner);
    process->setProcessChannelMode(QProcess::SeparateChannels);
    QObject::connect(process, &QProcess::started, owner, [this]() { sendInitialize(); });
    QObject::connect(process, &QProcess::readyReadStandardOutput, owner,
                     [this]() { readServerOutput(); });
    QObject::connect(process, &QProcess::readyReadStandardError, owner, [this]() {
      // Never forward unbounded server output into the UI. Keep only the last useful line.
      QByteArray error = process->readAllStandardError();
      if(error.size() > 4096)
        error = error.right(4096);
      const QString message = QString::fromUtf8(error).trimmed();
      if(!message.isEmpty())
        lastServerError = message.right(1024);
    });
    QObject::connect(
        process, static_cast<void (QProcess::*)(int, QProcess::ExitStatus)>(&QProcess::finished),
        owner, [this](int exitCode, QProcess::ExitStatus status) {
          const bool unexpected = !stopping;
          serverReady = false;
          pending.clear();
          receiveBuffer.clear();
          for(EditorState *state : states)
            state->openSent = false;
          if(unexpected)
          {
            QString message;
            if(status == QProcess::CrashExit)
            {
              message = lastServerError.isEmpty()
                            ? QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server crashed (exit %1).")
                                  .arg(exitCode)
                            : QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server crashed (exit %1): %2")
                                  .arg(exitCode)
                                  .arg(lastServerError);
            }
            else
            {
              message = lastServerError.isEmpty()
                            ? QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server stopped (exit %1).")
                                  .arg(exitCode)
                            : QCoreApplication::translate(
                                  "CodeEditorService",
                                  "Shader language server stopped (exit %1): %2")
                                  .arg(exitCode)
                                  .arg(lastServerError);
            }
            reportStatus(message);
          }
          stopping = false;
        });
    QObject::connect(process, &QProcess::errorOccurred, owner, [this](QProcess::ProcessError) {
      if(!stopping)
        reportStatus(
            QCoreApplication::translate("CodeEditorService",
                                        "Shader language server could not start: %1")
                .arg(process->errorString()));
    });
  }

  ~Impl()
  {
    diagnosticTimer->stop();
    stopping = true;
    if(process->state() != QProcess::NotRunning)
    {
      process->terminate();
      if(!process->waitForFinished(150))
      {
        process->kill();
        process->waitForFinished(150);
      }
    }

    for(EditorState *state : states)
      delete state;
    states.clear();
  }

  CodeEditorService *owner = NULL;
  QTimer *diagnosticTimer = NULL;
  QHash<ScintillaEdit *, EditorState *> states;
  QHash<QString, QList<Symbol>> symbolIndex;
  QList<NavigationEntry> backHistory;
  QList<NavigationEntry> forwardHistory;
  DefinitionHandler definitionHandler;
  DiagnosticsHandler diagnosticsHandler;
  StatusHandler statusHandler;
  quint64 untitledCounter = 0;

  QProcess *process = NULL;
  QString configuredProgram;
  QString resolvedProgram;
  QStringList serverArguments;
  QByteArray receiveBuffer;
  QHash<int, PendingRequest> pending;
  int nextRequestId = 1;
  bool serverReady = false;
  bool serverAttempted = false;
  bool stopping = false;
  QString lastServerError;

  void reportStatus(const QString &message)
  {
    if(statusHandler)
      statusHandler(message);
  }

  EditorState *stateFor(ScintillaEdit *editor) const { return states.value(editor, NULL); }

  EditorState *stateForUri(const QString &uri) const
  {
    for(EditorState *state : states)
      if(state->uri == uri)
        return state;

    const QString path = pathFromUri(uri);
    if(path.isEmpty())
      return NULL;

    for(EditorState *state : states)
    {
      if(!state->fileName.isEmpty() &&
         QFileInfo(state->fileName).absoluteFilePath().compare(path, Qt::CaseInsensitive) == 0)
        return state;
    }
    return NULL;
  }

  QString makeUri(const QString &fileName)
  {
    if(!fileName.trimmed().isEmpty() && !fileName.startsWith(QLatin1Char('<')))
      return QUrl::fromLocalFile(QFileInfo(fileName).absoluteFilePath()).toString(QUrl::FullyEncoded);

    return QStringLiteral("untitled:renderdoc/%1/shader").arg(++untitledCounter);
  }

  void configureIndicators(ScintillaEdit *editor)
  {
    editor->indicSetStyle(ErrorIndicator, INDIC_SQUIGGLE);
    editor->indicSetFore(ErrorIndicator, scintillaColour(255, 86, 71));
    editor->indicSetUnder(ErrorIndicator, true);
    editor->indicSetStyle(WarningIndicator, INDIC_SQUIGGLELOW);
    editor->indicSetFore(WarningIndicator, scintillaColour(245, 216, 106));
    editor->indicSetUnder(WarningIndicator, true);
    editor->indicSetStyle(SymbolOccurrenceIndicator, INDIC_ROUNDBOX);
    editor->indicSetFore(SymbolOccurrenceIndicator, scintillaColour(100, 150, 210));
    editor->indicSetAlpha(SymbolOccurrenceIndicator, 45);
    editor->indicSetOutlineAlpha(SymbolOccurrenceIndicator, 150);
    editor->indicSetUnder(SymbolOccurrenceIndicator, true);
    const bool dark = editor->palette().color(QPalette::Base).lightness() < 128;
    editor->indicSetStyle(FunctionNameIndicator, INDIC_TEXTFORE);
    editor->indicSetFore(FunctionNameIndicator, dark ? scintillaColour(57, 204, 155)
                                                    : scintillaColour(0, 112, 82));
    editor->indicSetStyle(VariableNameIndicator, INDIC_TEXTFORE);
    editor->indicSetFore(VariableNameIndicator, dark ? scintillaColour(156, 205, 254)
                                                    : scintillaColour(38, 78, 130));
    editor->autoCSetSeparator('\n');
    editor->autoCSetIgnoreCase(true);
    editor->autoCSetAutoHide(true);
    editor->autoCSetDropRestOfWord(false);
    editor->autoCSetMaxHeight(12);
    editor->autoCSetMaxWidth(64);
    editor->setMouseDwellTime(500);
    // Diagnostic hover tips are Scintilla call tips. Their default colours
    // (white background, inherited light foreground) are unreadable on the
    // dark theme, so the tip looked empty. Give them explicit theme colours.
    editor->callTipUseStyle(0);
    editor->callTipSetBack(dark ? scintillaColour(45, 47, 52) : scintillaColour(255, 255, 240));
    editor->callTipSetFore(dark ? scintillaColour(224, 226, 230) : scintillaColour(30, 30, 30));
    editor->callTipSetForeHlt(dark ? scintillaColour(120, 190, 255) : scintillaColour(20, 90, 200));
  }

  void addSymbol(EditorState *state, const QString &name, int position,
                 const QString &signature = QString(), const QString &valueType = QString())
  {
    if(name.isEmpty() || position < 0 || name.size() > 128)
      return;

    for(const Symbol &existing : state->symbols)
      if(existing.name == name && existing.position == position)
        return;

    Symbol symbol;
    symbol.name = name;
    symbol.position = position;
    symbol.signature = signature;
    symbol.state = state;
    symbol.scopeEnd = state->text.size();
    symbol.valueType = valueType;
    state->symbols.push_back(symbol);
  }

  void indexDocument(EditorState *state)
  {
    state->symbols.clear();
    const QByteArray masked = maskedCode(state->text, state->language);
    const QString source = QString::fromUtf8(masked);

    if(state->language == QStringLiteral("python"))
    {
      static const QRegularExpression pythonDefinition(
          QStringLiteral("\\b(?:def|class)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*(\\([^\\n]*\\))?"));
      QRegularExpressionMatchIterator matches = pythonDefinition.globalMatch(source);
      while(matches.hasNext())
      {
        const QRegularExpressionMatch match = matches.next();
        const QString name = match.captured(1);
        const int position = utf16IndexToByte(source, match.capturedStart(1));
        addSymbol(state, name, position, cleanSignature(match.captured(0)));
      }
      return;
    }

    static const QRegularExpression typeDefinition(QStringLiteral(
        "\\b(?:struct|class|interface|cbuffer|namespace|enum)\\s+([A-Za-z_][A-Za-z0-9_]*)"));
    static const QRegularExpression usingDefinition(
        QStringLiteral("\\busing\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*="));
    static const QRegularExpression functionDefinition(
        QStringLiteral("\\b([A-Za-z_][A-Za-z0-9_:<>]*(?:\\s*[*&])?)\\s+"
                       "([A-Za-z_][A-Za-z0-9_]*)\\s*(\\([^;{}]*\\))\\s*(?=[:{;])"));
    static const QRegularExpression variableDefinition(
        QStringLiteral("\\b([A-Za-z_][A-Za-z0-9_:<>]*(?:\\s*[*&])?)\\s+"
                       "([A-Za-z_][A-Za-z0-9_]*)\\s*(?=[=;,:\\[)])"));
    static const QSet<QString> invalidTypes = {
        QStringLiteral("return"), QStringLiteral("if"),      QStringLiteral("for"),
        QStringLiteral("while"),  QStringLiteral("switch"),  QStringLiteral("case"),
        QStringLiteral("sizeof"), QStringLiteral("discard"), QStringLiteral("else"),
    };

    auto addMatches = [this, state, &source](const QRegularExpression &expression, int nameGroup,
                                             bool function) {
      QRegularExpressionMatchIterator matches = expression.globalMatch(source);
      while(matches.hasNext())
      {
        const QRegularExpressionMatch match = matches.next();
        if(match.capturedStart(nameGroup) < 0)
          continue;
        if(nameGroup > 1 && invalidTypes.contains(match.captured(1)))
          continue;
        const QString name = match.captured(nameGroup);
        const int position = utf16IndexToByte(source, match.capturedStart(nameGroup));
        addSymbol(state, name, position, function ? cleanSignature(match.captured(0)) : QString(),
                  nameGroup == 2 ? match.captured(1).trimmed() : QString());
      }
    };

    addMatches(typeDefinition, 1, false);
    addMatches(usingDefinition, 1, false);
    addMatches(functionDefinition, 2, true);
    addMatches(variableDefinition, 2, false);

    // Brace scopes and parameter ranges distinguish local shadowing and fields belonging to
    // different structs. This is a conservative local fallback; macros and overload typing
    // remain the language server/compiler's responsibility.
    struct Scope { int start; int end; QString ownerType; bool cbuffer; };
    QList<Scope> scopes;
    const QHash<int, int> pairs = codeDelimiterPairs(masked);
    const QList<Symbol> firstDeclarators = state->symbols;
    for(const Symbol &symbol : firstDeclarators)
    {
      if(!symbol.signature.isEmpty() || symbol.valueType.isEmpty()) continue;
      for(const FollowingDeclarator &declaration : followingDeclarators(
              masked, pairs, symbol.position + symbol.name.toUtf8().size(), symbol.valueType))
        addSymbol(state, declaration.name, declaration.position, QString(), declaration.valueType);
    }
    static const QRegularExpression typeScope(
        QStringLiteral("\\b(struct|class|cbuffer)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*(?::[^{};]*)?$"));
    for(auto it = pairs.constBegin(); it != pairs.constEnd(); ++it)
    {
      if(masked[it.key()] != '{') continue;
      const int start = qMax(0, qMax(masked.lastIndexOf(';', it.key()),
                                   qMax(masked.lastIndexOf('}', it.key()),
                                        masked.lastIndexOf('{', it.key() - 1))) + 1);
      const auto match = typeScope.match(QString::fromUtf8(masked.mid(start, it.key() - start)));
      scopes.push_back({it.key(), it.value(), match.captured(2),
                        match.captured(1) == QStringLiteral("cbuffer")});
    }
    auto skipSpace = [&masked](int index) {
      while(index < masked.size() && std::isspace(static_cast<unsigned char>(masked[index]))) ++index;
      return index;
    };
    std::function<int(int, int)> statementEnd = [&](int index, int depth) -> int {
      index = skipSpace(index);
      if(index >= masked.size() || depth > 32) return index;
      if(masked[index] == '{') return pairs.value(index, index) + 1;
      for(const QByteArray &control : {QByteArray("if"), QByteArray("for"), QByteArray("while"), QByteArray("switch")})
      {
        if(masked.mid(index, control.size()) != control) continue;
        int open = skipSpace(index + control.size());
        if(open >= masked.size() || masked[open] != '(' || !pairs.contains(open)) continue;
        int end = statementEnd(pairs.value(open) + 1, depth + 1);
        int next = skipSpace(end);
        if(control == "if" && masked.mid(next, 4) == "else" &&
           (next + 4 >= masked.size() || !isIdentifierByte(masked[next + 4])))
          end = statementEnd(next + 4, depth + 1);
        return end;
      }
      for(int i = index; i < masked.size(); ++i)
      {
        if(masked[i] == ';') return i + 1;
        if(masked[i] == '}') return i;
        if(pairs.contains(i)) i = pairs.value(i);
      }
      return masked.size();
    };
    QList<QPair<int, int>> forScopes;
    for(int offset = 0; (offset = masked.indexOf("for", offset)) >= 0; offset += 3)
    {
      if(offset > 0 && isIdentifierByte(masked[offset - 1])) continue;
      const int open = skipSpace(offset + 3);
      if(open < masked.size() && masked[open] == '(' && pairs.contains(open))
        forScopes.push_back(qMakePair(open, statementEnd(pairs.value(open) + 1, 0)));
    }
    for(Symbol &symbol : state->symbols)
    {
      for(const Scope &scope : scopes)
      {
        if(scope.start < symbol.position && symbol.position < scope.end &&
           scope.end - scope.start < symbol.scopeEnd - symbol.scopeStart)
        {
          if(scope.cbuffer) continue; // CBuffer members are visible as globals in HLSL.
          symbol.scopeStart = scope.start;
          symbol.scopeEnd = scope.end;
          symbol.ownerType = scope.ownerType;
        }
      }
      if(!symbol.signature.isEmpty()) continue;
      for(const Symbol &function : state->symbols)
      {
        if(function.signature.isEmpty()) continue;
        int open = masked.indexOf('(', function.position);
        int close = pairs.value(open, -1);
        if(!(open < symbol.position && symbol.position < close)) continue;
        // Prototype parameter names have no visibility outside their own declaration.
        symbol.scopeStart = open;
        symbol.scopeEnd = close;
        int body = masked.indexOf('{', close);
        int terminator = masked.indexOf(';', close);
        if(body >= 0 && (terminator < 0 || body < terminator) && pairs.contains(body))
        {
          symbol.scopeStart = open;
          symbol.scopeEnd = pairs.value(body);
        }
      }
      for(const auto &scope : forScopes)
        if(scope.first < symbol.position && symbol.position < pairs.value(scope.first))
        {
          symbol.scopeStart = scope.first;
          symbol.scopeEnd = scope.second;
        }
    }
  }

  void rebuildSymbolIndex()
  {
    symbolIndex.clear();
    for(EditorState *state : states)
    {
      indexDocument(state);
      for(const Symbol &symbol : state->symbols)
        symbolIndex[symbol.name].push_back(symbol);
    }
  }

  QList<Diagnostic> localDiagnosticsFor(EditorState *state) const
  {
    struct Delimiter
    {
      char character = 0;
      int position = 0;
    };

    QList<Diagnostic> ret;
    QVector<Delimiter> stack;
    const QByteArray &text = state->text;
    const bool python = state->language == QStringLiteral("python");
    enum ScanState
    {
      Normal,
      LineComment,
      BlockComment,
      SingleString,
      DoubleString,
      TripleSingleString,
      TripleDoubleString,
    };
    ScanState scan = Normal;
    int constructStart = -1;
    bool escaped = false;

    auto append = [&ret, &text](int start, int length, DiagnosticSeverity severity,
                                const QString &message) {
      if(ret.size() >= MaxDiagnostics)
        return;
      Diagnostic diagnostic;
      diagnostic.start = qBound(0, start, text.size());
      diagnostic.length = qMax(1, qMin(length, text.size() - diagnostic.start));
      diagnostic.severity = severity;
      diagnostic.message = message;
      // i18n-ignore: Diagnostic source is internal metadata and is not shown by the current UI.
      diagnostic.source = QStringLiteral("RenderDoc local parser");
      const QByteArray prefix = text.left(diagnostic.start);
      diagnostic.line = prefix.count('\n');
      const int newline = prefix.lastIndexOf('\n');
      diagnostic.column = diagnostic.start - (newline + 1);
      ret.push_back(diagnostic);
    };

    for(int i = 0; i < text.size(); i++)
    {
      const char c = text[i];
      const char next = i + 1 < text.size() ? text[i + 1] : '\0';

      if(scan == Normal)
      {
        if((python && c == '#') || (!python && c == '/' && next == '/'))
        {
          scan = LineComment;
          if(!python)
            i++;
        }
        else if(!python && c == '/' && next == '*')
        {
          scan = BlockComment;
          constructStart = i++;
        }
        else if(python && c == '\'' && i + 2 < text.size() && text[i + 1] == '\'' &&
                text[i + 2] == '\'')
        {
          scan = TripleSingleString;
          constructStart = i;
          i += 2;
        }
        else if(python && c == '"' && i + 2 < text.size() && text[i + 1] == '"' && text[i + 2] == '"')
        {
          scan = TripleDoubleString;
          constructStart = i;
          i += 2;
        }
        else if(c == '\'' || c == '"')
        {
          scan = c == '\'' ? SingleString : DoubleString;
          constructStart = i;
          escaped = false;
        }
        else if(c == '(' || c == '[' || c == '{')
        {
          stack.push_back({c, i});
        }
        else if(c == ')' || c == ']' || c == '}')
        {
          const char expected = c == ')' ? '(' : (c == ']' ? '[' : '{');
          if(stack.isEmpty() || stack.back().character != expected)
          {
            append(i, 1, DiagnosticSeverity::Error,
                   QCoreApplication::translate("CodeEditorService", "Unmatched closing '%1'")
                       .arg(QChar::fromLatin1(c)));
          }
          else
          {
            stack.pop_back();
          }
        }
      }
      else if(scan == LineComment)
      {
        if(c == '\n')
          scan = Normal;
      }
      else if(scan == BlockComment)
      {
        if(c == '*' && next == '/')
        {
          scan = Normal;
          i++;
        }
      }
      else if(scan == TripleSingleString || scan == TripleDoubleString)
      {
        const char quote = scan == TripleSingleString ? '\'' : '"';
        if(c == quote && i + 2 < text.size() && text[i + 1] == quote && text[i + 2] == quote)
        {
          scan = Normal;
          i += 2;
        }
      }
      else
      {
        const char quote = scan == SingleString ? '\'' : '"';
        if(c == quote && !escaped)
        {
          scan = Normal;
        }
        else if(c == '\n' && !escaped)
        {
          append(constructStart, qMax(1, i - constructStart), DiagnosticSeverity::Error,
                 QCoreApplication::translate("CodeEditorService",
                                             "Unterminated string literal"));
          scan = Normal;
        }
        escaped = c == '\\' && !escaped;
        if(c != '\\')
          escaped = false;
      }
    }

    if(scan == BlockComment)
      append(constructStart, qMax(1, text.size() - constructStart), DiagnosticSeverity::Warning,
             QCoreApplication::translate("CodeEditorService", "Unterminated block comment"));
    else if(scan == SingleString || scan == DoubleString || scan == TripleSingleString ||
            scan == TripleDoubleString)
      append(constructStart, qMax(1, text.size() - constructStart), DiagnosticSeverity::Error,
             QCoreApplication::translate("CodeEditorService", "Unterminated string literal"));

    for(const Delimiter &delimiter : stack)
      append(delimiter.position, 1, DiagnosticSeverity::Error,
             QCoreApplication::translate("CodeEditorService", "Unclosed '%1'")
                 .arg(QChar::fromLatin1(delimiter.character)));

    return ret;
  }

  QList<Diagnostic> combinedDiagnostics(EditorState *state) const
  {
    QList<Diagnostic> ret = state->localDiagnostics;
    ret.append(state->lspDiagnostics);
    ret.append(state->externalDiagnostics);
    return ret;
  }

  void displayDiagnostics(EditorState *state)
  {
    ScintillaEdit *editor = state->editor.data();
    if(!editor)
      return;

    const sptr_t previousIndicator = editor->indicatorCurrent();
    editor->setIndicatorCurrent(ErrorIndicator);
    editor->indicatorClearRange(0, editor->length());
    editor->setIndicatorCurrent(WarningIndicator);
    editor->indicatorClearRange(0, editor->length());

    const QList<Diagnostic> all = combinedDiagnostics(state);
    for(const Diagnostic &diagnostic : all)
    {
      editor->setIndicatorCurrent(
          diagnostic.severity == DiagnosticSeverity::Error ? ErrorIndicator : WarningIndicator);
      editor->indicatorFillRange(
          qBound(0, diagnostic.start, int(editor->length())),
          qMax(1, qMin(diagnostic.length, int(editor->length()) - diagnostic.start)));
    }
    editor->setIndicatorCurrent(previousIndicator);

    if(diagnosticsHandler)
      diagnosticsHandler(editor, all);
  }

  void syncText(EditorState *state, bool runDiagnostics)
  {
    if(!state || !state->editor)
      return;

    if(state->textDirty)
    {
      const QByteArray newText = state->editor->getText(state->editor->textLength() + 1);
      if(newText != state->text)
      {
        state->text = newText;
        state->version++;
        rebuildSymbolIndex();
        sendDidChange(state);
      }
      state->textDirty = false;
    }

    if(runDiagnostics && state->diagnosticsDirty)
    {
      state->localDiagnostics = localDiagnosticsFor(state);
      state->diagnosticsDirty = false;
      displayDiagnostics(state);
    }
    updateSymbolHighlights(state);
  }

  void flushDirtyDocuments()
  {
    for(EditorState *state : states)
      if(state->textDirty || state->diagnosticsDirty)
        syncText(state, true);
  }

  void markModified(EditorState *state)
  {
    if(!state)
      return;
    state->editRevision++;
    state->textDirty = true;
    state->diagnosticsDirty = true;
    state->lspDiagnostics.clear();
    // Compiler diagnostics supplied by the owning workspace describe the previous source
    // revision. Drop their locations as soon as the buffer changes instead of leaving stale
    // squiggles attached to unrelated text.
    state->externalDiagnostics.clear();
    if(referencesMenu)
      referencesMenu->close();
    diagnosticTimer->start();
  }

  void moveHistoryForEdit(EditorState *state, int type, int position, int length)
  {
    auto update = [=](QList<NavigationEntry> &history) {
      for(NavigationEntry &entry : history)
      {
        if(entry.editor != state->editor || entry.position < position) continue;
        if(type & SC_MOD_INSERTTEXT) entry.position += length;
        else if(type & SC_MOD_DELETETEXT)
          entry.position = qMax(position, entry.position - length);
        entry.line = int(state->editor->lineFromPosition(entry.position));
        entry.column = entry.position - int(state->editor->positionFromLine(entry.line));
      }
    };
    update(backHistory);
    update(forwardHistory);
  }

  Location localDefinition(EditorState *sourceState, int position)
  {
    Location ret;
    if(!sourceState || !sourceState->editor)
      return ret;

    for(EditorState *state : states)
      if(state->textDirty) syncText(state, false);
    if(position < 0)
      position = int(sourceState->editor->currentPos());
    QPair<int, int> wordRange;
    const QString word = identifierAt(sourceState->text, position, &wordRange);
    if(word.isEmpty())
      return ret;

    return resolveDefinition(sourceState, wordRange.first, word);
  }

  Location resolveDefinition(EditorState *sourceState, int wordPosition, const QString &word,
                             int depth = 0)
  {
    Location ret;
    if(depth > 8) return ret;
    const QList<Symbol> candidates = symbolIndex.value(word);
    const QByteArray masked = maskedCode(sourceState->text, sourceState->language);
    int previous = wordPosition - 1;
    while(previous >= 0 && isspace(static_cast<unsigned char>(masked[previous]))) --previous;
    const bool member = previous >= 0 && masked[previous] == '.';
    QString memberType;
    if(member)
    {
      --previous;
      while(previous >= 0 && isspace(static_cast<unsigned char>(masked[previous]))) --previous;
      QPair<int, int> range;
      const QString object = identifierAt(masked, previous, &range);
      const Location ownerLocation = resolveDefinition(sourceState, range.first, object, depth + 1);
      for(const Symbol &ownerSymbol : symbolIndex.value(object))
        if(ownerSymbol.state && ownerSymbol.state->editor == ownerLocation.editor &&
           ownerSymbol.position == ownerLocation.position)
          memberType = ownerSymbol.valueType;
      if(memberType.isEmpty()) return ret;
    }
    const Symbol *best = NULL;
    qint64 bestScore = std::numeric_limits<qint64>::min();
    for(const Symbol &candidate : candidates)
    {
      if(!candidate.state || !candidate.state->editor)
        continue;
      const bool atDeclaration = candidate.state == sourceState && candidate.position == wordPosition;
      if(!atDeclaration)
      {
        if(member && candidate.ownerType != memberType) continue;
        if(!member && !candidate.ownerType.isEmpty()) continue;
        if(candidate.ownerType.isEmpty() && candidate.scopeStart > 0 &&
           (candidate.state != sourceState || wordPosition < candidate.position ||
            wordPosition > candidate.scopeEnd)) continue;
      }
      qint64 score = 0;
      if(candidate.state == sourceState)
      {
        score += 1000000000LL;
        if(candidate.position <= wordPosition)
          score += 500000000LL - qMin(500000000, wordPosition - candidate.position);
        else
          score -= candidate.position - wordPosition;
      }
      if(!best || score > bestScore)
      {
        best = &candidate;
        bestScore = score;
      }
    }

    if(!best)
      return ret;

    ret.editor = best->state->editor.data();
    ret.fileName = best->state->fileName;
    ret.position = best->position;
    ret.line = int(ret.editor->lineFromPosition(ret.position));
    ret.column = ret.position - int(ret.editor->positionFromLine(ret.line));
    return ret;
  }

  void deliverDefinition(const Location &location)
  {
    if(!location.editor && location.fileName.isEmpty())
      return;
    const bool wasDelivering = deliveringNavigation;
    deliveringNavigation = true;
    if(definitionHandler)
    {
      definitionHandler(location);
    }
    else if(location.editor && location.position >= 0)
    {
      location.editor->gotoPos(location.position);
      location.editor->QWidget::setFocus(Qt::ShortcutFocusReason);
    }
    deliveringNavigation = wasDelivering;
  }

  QByteArray indentationUnit(ScintillaEdit *editor) const
  {
    if(editor->useTabs())
      return QByteArray("\t");
    int width = int(editor->indent());
    if(width <= 0)
      width = int(editor->tabWidth());
    return QByteArray(qMax(1, width), ' ');
  }

  QByteArray lineIndentationBytes(ScintillaEdit *editor, int line) const
  {
    const int start = int(editor->positionFromLine(line));
    const int end = int(editor->lineIndentPosition(line));
    return editor->textRange(start, end);
  }

  QPair<int, int> selectedLineRange(ScintillaEdit *editor) const
  {
    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    int firstLine = int(editor->lineFromPosition(start));
    int lastLine = int(editor->lineFromPosition(end));
    if(end > start && end == editor->positionFromLine(lastLine))
      lastLine--;
    return qMakePair(firstLine, qMax(firstLine, lastLine));
  }

  bool insertSmartNewline(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    const QByteArray text = editor->getText(editor->textLength() + 1);
    const SmartNewlineEdit edit =
        planSmartNewline(text, start, end, eolBytes(int(editor->eOLMode())),
                         indentationUnit(editor), state->language == QStringLiteral("python"));

    editor->beginUndoAction();
    editor->setTargetRange(start, end);
    editor->replaceTarget(edit.replacement.size(), edit.replacement.constData());
    editor->setEmptySelection(start + edit.caretOffset);
    editor->endUndoAction();
    return true;
  }

  bool insertOpenLine(EditorState *state, bool above)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const int line = int(editor->lineFromPosition(editor->currentPos()));
    const QByteArray indentation = lineIndentationBytes(editor, line);
    const QByteArray eol = eolBytes(int(editor->eOLMode()));
    const int insertionPosition =
        above ? int(editor->positionFromLine(line)) : int(editor->lineEndPosition(line));
    const QByteArray insertion = above ? indentation + eol : eol + indentation;
    const int caret = insertionPosition + (above ? indentation.size() : insertion.size());

    editor->beginUndoAction();
    editor->insertText(insertionPosition, insertion.constData());
    editor->setEmptySelection(caret);
    editor->endUndoAction();
    return true;
  }

  bool indentSelectedLines(EditorState *state, bool unindent)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selectionStart() == editor->selectionEnd() ||
       editor->selections() != 1 || editor->selectionIsRectangle())
      return false;

    const QPair<int, int> lines = selectedLineRange(editor);
    int width = int(editor->indent());
    if(width <= 0)
      width = int(editor->tabWidth());
    width = qMax(1, width);

    editor->beginUndoAction();
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int oldIndent = int(editor->lineIndentation(line));
      editor->setLineIndentation(line, unindent ? qMax(0, oldIndent - width) : oldIndent + width);
    }
    editor->endUndoAction();
    return true;
  }

  bool toggleLineComment(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    const QByteArray marker = state->language == QStringLiteral("python") ? QByteArray("#")
                                                                          : QByteArray("//");
    const QPair<int, int> lines = selectedLineRange(editor);
    bool anyNonBlank = false;
    bool allCommented = true;
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int start = int(editor->positionFromLine(line));
      const QByteArray content = editor->textRange(start, int(editor->lineEndPosition(line)));
      const int indentation = leadingWhitespace(content).size();
      const QByteArray remainder = content.mid(indentation);
      if(remainder.trimmed().isEmpty())
        continue;
      anyNonBlank = true;
      if(!remainder.startsWith(marker))
        allCommented = false;
    }
    allCommented = anyNonBlank && allCommented;

    QList<TextReplacement> replacements;
    for(int line = lines.first; line <= lines.second; line++)
    {
      const int lineStartPosition = int(editor->positionFromLine(line));
      const QByteArray content =
          editor->textRange(lineStartPosition, int(editor->lineEndPosition(line)));
      const int indentation = leadingWhitespace(content).size();
      const QByteArray remainder = content.mid(indentation);
      TextReplacement replacement;
      replacement.start = lineStartPosition + indentation;
      if(allCommented)
      {
        if(remainder.trimmed().isEmpty() || !remainder.startsWith(marker))
          continue;
        replacement.removedLength = marker.size();
        if(remainder.size() > marker.size() && remainder[marker.size()] == ' ')
          replacement.removedLength++;
      }
      else
      {
        replacement.inserted = marker + QByteArray(" ");
      }
      replacements.push_back(replacement);
    }

    if(replacements.isEmpty())
      return true;

    const int originalAnchor = int(editor->anchor());
    const int originalCaret = int(editor->currentPos());
    editor->beginUndoAction();
    for(int i = replacements.size() - 1; i >= 0; i--)
    {
      const TextReplacement &replacement = replacements[i];
      editor->setTargetRange(replacement.start, replacement.start + replacement.removedLength);
      editor->replaceTarget(replacement.inserted.size(), replacement.inserted.constData());
    }
    editor->setSel(transformedPosition(originalAnchor, replacements),
                   transformedPosition(originalCaret, replacements));
    editor->endUndoAction();
    return true;
  }

  bool typePairedCharacter(EditorState *state, char typed)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle())
      return false;

    char closing = '\0';
    if(typed == '(')
      closing = ')';
    else if(typed == '[')
      closing = ']';
    else if(typed == '{')
      closing = '}';
    else if(typed == '\'' || typed == '"')
      closing = typed;

    const int start = int(editor->selectionStart());
    const int end = int(editor->selectionEnd());
    if(start == end && (typed == ')' || typed == ']' || typed == '}' || typed == '\'' ||
                        typed == '"') &&
       editor->charAt(start) == typed)
    {
      editor->setEmptySelection(start + 1);
      return true;
    }

    if(closing == '\0')
      return false;
    if(start == end && (typed == '\'' || typed == '"') && start > 0 &&
       editor->charAt(start - 1) == '\\')
      return false;

    const QByteArray selected = editor->textRange(start, end);
    QByteArray replacement;
    replacement.reserve(selected.size() + 2);
    replacement.append(typed);
    replacement.append(selected);
    replacement.append(closing);

    const bool forwardSelection = editor->anchor() <= editor->currentPos();
    editor->beginUndoAction();
    editor->setTargetRange(start, end);
    editor->replaceTarget(replacement.size(), replacement.constData());
    if(start == end)
      editor->setEmptySelection(start + 1);
    else if(forwardSelection)
      editor->setSel(start + 1, start + 1 + selected.size());
    else
      editor->setSel(start + 1 + selected.size(), start + 1);
    editor->endUndoAction();

    if(typed == '(')
      callTip(state);
    return true;
  }

  bool deleteEmptyPair(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || editor->readOnly() || editor->selections() != 1 ||
       editor->selectionIsRectangle() || editor->selectionStart() != editor->selectionEnd())
      return false;

    const int caret = int(editor->currentPos());
    if(caret <= 0 || caret >= editor->length())
      return false;

    const char previous = char(editor->charAt(caret - 1));
    const char next = char(editor->charAt(caret));
    const bool paired = (previous == '(' && next == ')') || (previous == '[' && next == ']') ||
                        (previous == '{' && next == '}') ||
                        (previous == '\'' && next == '\'') ||
                        (previous == '"' && next == '"');
    if(!paired)
      return false;

    editor->beginUndoAction();
    editor->setTargetRange(caret - 1, caret + 1);
    editor->replaceTarget(0, "");
    editor->setEmptySelection(caret - 1);
    editor->endUndoAction();
    return true;
  }

  void updateSymbolHighlights(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor || state->textDirty)
      return;
    const int previous = int(editor->indicatorCurrent());
    if(state->maskedVersion != state->version)
    {
      state->maskedText = maskedCode(state->text, state->language);
      state->maskedVersion = state->version;
    }
    const QByteArray &masked = state->maskedText;
    if(state->coloredVersion != state->version)
    {
      state->coloredVersion = state->version;
      for(int indicator : {FunctionNameIndicator, VariableNameIndicator})
      {
        editor->setIndicatorCurrent(indicator);
        editor->indicatorClearRange(0, editor->length());
      }
      if(state->language != QStringLiteral("python") && masked.size() <= MaxMessageBytes)
      {
        QSet<QByteArray> functions, variables;
        for(const Symbol &symbol : state->symbols)
          (symbol.signature.isEmpty() ? variables : functions).insert(symbol.name.toUtf8());
        for(int start = 0; start < masked.size();)
        {
          if(!isIdentifierByte(masked[start]))
          {
            start++;
            continue;
          }
          int end = start + 1;
          while(end < masked.size() && isIdentifierByte(masked[end]))
            end++;
          const QByteArray word = masked.mid(start, end - start);
          const int indicator = functions.contains(word) ? FunctionNameIndicator
                                : variables.contains(word) ? VariableNameIndicator : -1;
          if(indicator >= 0)
          {
            editor->setIndicatorCurrent(indicator);
            editor->indicatorFillRange(start, end - start);
          }
          start = end;
        }
      }
    }
    const QByteArray word = identifierAt(masked, int(editor->currentPos())).toUtf8();
    if(state->highlightedWord != word || state->highlightedVersion != state->version)
    {
      state->highlightedWord = word;
      state->highlightedVersion = state->version;
      editor->setIndicatorCurrent(SymbolOccurrenceIndicator);
      editor->indicatorClearRange(0, editor->length());
      if(!word.isEmpty() && word.size() <= 128 && masked.size() <= MaxMessageBytes)
      {
        int start = 0;
        for(int count = 0; count < 10000; count++)
        {
          start = masked.indexOf(word, start);
          if(start < 0)
            break;
          const int end = start + word.size();
          if((start == 0 || !isIdentifierByte(masked[start - 1])) &&
             (end == masked.size() || !isIdentifierByte(masked[end])))
            editor->indicatorFillRange(start, word.size());
          start = end;
        }
      }
    }
    editor->setIndicatorCurrent(previous);
  }

  void updateBraceHighlight(EditorState *state)
  {
    ScintillaEdit *editor = state ? state->editor.data() : NULL;
    if(!editor)
      return;

    editor->braceHighlight(-1, -1);
    editor->braceBadLight(-1);
    const int caret = int(editor->currentPos());
    auto isBrace = [](int character) {
      return character == '(' || character == ')' || character == '[' || character == ']' ||
             character == '{' || character == '}';
    };
    int brace = -1;
    if(caret < editor->length() && isBrace(int(editor->charAt(caret))))
      brace = caret;
    else if(caret > 0 && isBrace(int(editor->charAt(caret - 1))))
      brace = caret - 1;
    if(brace < 0)
      return;

    const int match = int(editor->braceMatch(brace, 0));
    if(match >= 0)
      editor->braceHighlight(brace, match);
    else
      editor->braceBadLight(brace);
  }

  NavigationEntry navigationEntry(ScintillaEdit *editor, int position = -1) const
  {
    NavigationEntry ret;
    EditorState *state = stateFor(editor);
    if(!state || !state->editor)
      return ret;
    ret.editor = state->editor;
    ret.fileName = state->fileName;
    ret.position = position < 0 ? int(editor->currentPos()) : position;
    ret.position = qBound(0, ret.position, int(editor->length()));
    ret.line = int(editor->lineFromPosition(ret.position));
    ret.column = ret.position - int(editor->positionFromLine(ret.line));
    return ret;
  }

  Location navigationLocation(const NavigationEntry &entry) const
  {
    Location ret;
    ret.editor = entry.editor.data();
    ret.fileName = entry.fileName;
    ret.position = entry.position;
    ret.line = entry.line;
    ret.column = entry.column;
    if(!ret.editor && !ret.fileName.isEmpty())
    {
      for(EditorState *state : states)
      {
        if(state->editor && state->fileName.compare(ret.fileName, Qt::CaseInsensitive) == 0)
        {
          ret.editor = state->editor.data();
          ret.position = qBound(0, ret.position, int(ret.editor->length()));
          ret.line = int(ret.editor->lineFromPosition(ret.position));
          ret.column = ret.position - int(ret.editor->positionFromLine(ret.line));
          break;
        }
      }
    }
    return ret;
  }

  void pushHistory(QList<NavigationEntry> &history, const NavigationEntry &entry)
  {
    if(!entry.editor && entry.fileName.isEmpty())
      return;
    if(!history.isEmpty())
    {
      const NavigationEntry &last = history.back();
      if(last.editor == entry.editor && last.fileName == entry.fileName &&
         last.position == entry.position)
        return;
    }
    history.push_back(entry);
    while(history.size() > NavigationHistoryLimit)
      history.pop_front();
  }

  void navigateToDefinition(const Location &location, ScintillaEdit *sourceEditor,
                            int sourcePosition)
  {
    const NavigationEntry source = navigationEntry(sourceEditor, sourcePosition);
    if((location.editor != source.editor || location.position != source.position) &&
       (source.editor || !source.fileName.isEmpty()))
    {
      pushHistory(backHistory, source);
      forwardHistory.clear();
    }
    deliverDefinition(location);
  }

  bool navigateHistory(ScintillaEdit *editor, bool backwards)
  {
    QList<NavigationEntry> &source = backwards ? backHistory : forwardHistory;
    QList<NavigationEntry> &destination = backwards ? forwardHistory : backHistory;
    while(!source.isEmpty())
    {
      const NavigationEntry targetEntry = source.takeLast();
      const Location target = navigationLocation(targetEntry);
      if(!target.editor && (target.fileName.isEmpty() || !definitionHandler))
        continue;
      pushHistory(destination, navigationEntry(editor));
      deliverDefinition(target);
      return true;
    }
    return false;
  }

  void showReferences(EditorState *sourceState, int position)
  {
    if(!sourceState || !sourceState->editor) return;
    // Synchronise every open document before taking the popup snapshot.
    for(EditorState *state : states) syncText(state, false);
    const Location definition = localDefinition(sourceState, position);
    if(!definition.isValid()) return;
    const QString word = identifierAt(sourceState->text, position);
    const QByteArray needle = word.toUtf8();
    if(needle.isEmpty()) return;
    if(referencesMenu) referencesMenu->close();
    const QPointer<ScintillaEdit> origin = sourceState->editor;
    const quint64 originRevision = sourceState->editRevision;

    struct ReferenceHit
    {
      QPointer<ScintillaEdit> editor;
      quint64 revision;
      Location target;
      QString label;
    };
    QVector<ReferenceHit> hits;
    for(EditorState *state : states)
    {
      const QByteArray masked = maskedCode(state->text, state->language);
      int offset = 0;
      while(hits.size() < 256 && (offset = masked.indexOf(needle, offset)) >= 0)
      {
        const int found = offset;
        offset += needle.size();
        if((found > 0 && isIdentifierByte(masked[found - 1])) ||
           (offset < masked.size() && isIdentifierByte(masked[offset]))) continue;
        if(state->editor == definition.editor && found == definition.position) continue;
        const Location resolved = resolveDefinition(state, found, word);
        if(resolved.editor != definition.editor || resolved.position != definition.position) continue;
        const Location target = navigationLocation(navigationEntry(state->editor, found));
        const int start = int(state->editor->positionFromLine(target.line));
        const int end = int(state->editor->lineEndPosition(target.line));
        QString snippet = QString::fromUtf8(state->text.mid(start, end - start)).simplified().left(140);
        const QString label = QCoreApplication::translate("CodeEditorService", "%1:%2  %3")
                                  .arg(QFileInfo(state->fileName).fileName()).arg(target.line + 1)
                                  .arg(snippet);
        ReferenceHit hit;
        hit.editor = state->editor;
        hit.revision = state->editRevision;
        hit.target = target;
        hit.label = label;
        hits.push_back(hit);
      }
      if(hits.size() >= 256) break;
    }

    // A single call site needs no chooser: jump straight to it (guarding the
    // captured revisions so a concurrent edit cannot mis-navigate).
    if(hits.size() == 1)
    {
      const ReferenceHit &only = hits.front();
      EditorState *from = stateFor(origin.data());
      EditorState *to = stateFor(only.editor.data());
      if(from && to && from->editRevision == originRevision && to->editRevision == only.revision)
        navigateToDefinition(only.target, origin.data(), position);
      return;
    }

    QMenu *menu = new QMenu(sourceState->editor);
    referencesMenu = menu;
    menu->setObjectName(QStringLiteral("shaderReferencesPopup")); // i18n-ignore: automation id.
    menu->setAttribute(Qt::WA_DeleteOnClose);
    menu->addSection(QCoreApplication::translate("CodeEditorService", "References to %1").arg(word));
    for(const ReferenceHit &hit : hits)
    {
      QAction *action = menu->addAction(QString(hit.label).replace(QLatin1Char('&'), QStringLiteral("&&")));
      action->setData(hit.target.line + 1);
      const QPointer<ScintillaEdit> targetEditor = hit.editor;
      const quint64 targetRevision = hit.revision;
      const Location target = hit.target;
      QObject::connect(action, &QAction::triggered, owner,
                       [this, origin, originRevision, targetEditor, targetRevision, target, position]() {
        EditorState *from = stateFor(origin.data());
        EditorState *to = stateFor(targetEditor.data());
        if(!from || !to || from->editRevision != originRevision || to->editRevision != targetRevision)
          return;
        navigateToDefinition(target, origin.data(), position);
      });
    }
    if(hits.isEmpty())
      menu->addAction(QCoreApplication::translate("CodeEditorService", "No references found in open documents"))->setEnabled(false);
    else if(hits.size() >= 256)
      menu->addAction(QCoreApplication::translate("CodeEditorService", "Showing the first 256 references"))->setEnabled(false);
    ScintillaEdit *editor = sourceState->editor;
    const QPoint local(int(editor->pointXFromPosition(position)),
                       int(editor->pointYFromPosition(position) + editor->textHeight(editor->lineFromPosition(position))));
    menu->popup(editor->mapToGlobal(local));
  }

  QStringList completions(EditorState *state, const QString &prefix,
                          const QStringList &additional = QStringList()) const
  {
    QSet<QString> unique;
    for(const QString &keyword : languageKeywords(state->language))
      unique.insert(keyword);
    for(auto it = symbolIndex.constBegin(); it != symbolIndex.constEnd(); ++it)
      unique.insert(it.key());
    for(const QString &item : additional)
    {
      const QString clean = item.trimmed();
      if(!clean.isEmpty() && clean.size() <= 128 && !clean.contains(QLatin1Char('\n')) &&
         !clean.contains(QLatin1Char('\r')))
        unique.insert(clean);
    }

    QStringList ret;
    for(const QString &item : unique)
    {
      if(item == prefix)
        continue;
      if(prefix.isEmpty() || item.startsWith(prefix, Qt::CaseInsensitive))
        ret.push_back(item);
    }
    std::sort(ret.begin(), ret.end(), [](const QString &a, const QString &b) {
      return QString::compare(a, b, Qt::CaseInsensitive) < 0;
    });
    if(ret.size() > MaxCompletionItems)
      ret = ret.mid(0, MaxCompletionItems);
    return ret;
  }

  void showCompletions(EditorState *state, const QString &prefix,
                       const QStringList &additional = QStringList())
  {
    if(!state || !state->editor)
      return;
    const QStringList items = completions(state, prefix, additional);
    if(items.isEmpty())
      return;
    const QByteArray encoded = items.join(QLatin1Char('\n')).toUtf8();
    state->editor->autoCShow(prefix.toUtf8().size(), encoded.constData());
  }

  int callOpenParen(const QByteArray &text, int position) const
  {
    int depth = 0;
    bool inString = false;
    char quote = 0;
    bool escaped = false;
    for(int i = qMin(position - 1, text.size() - 1); i >= 0; i--)
    {
      const char c = text[i];
      if(inString)
      {
        if(c == quote && !escaped)
          inString = false;
        escaped = c == '\\' && !escaped;
        if(c != '\\')
          escaped = false;
        continue;
      }
      if(c == '\'' || c == '"')
      {
        inString = true;
        quote = c;
      }
      else if(c == ')')
      {
        depth++;
      }
      else if(c == '(')
      {
        if(depth == 0)
          return i;
        depth--;
      }
      else if((c == ';' || c == '{' || c == '}') && depth == 0)
      {
        return -1;
      }
    }
    return -1;
  }

  QString builtinSignature(const QString &name) const
  {
    // i18n-ignore: Shader API names and signatures are code tokens shown verbatim in call tips.
    static const QHash<QString, QString> signatures = {
        {QStringLiteral("mul"), QStringLiteral("mul(x, y)")},
        {QStringLiteral("lerp"), QStringLiteral("lerp(x, y, s)")},
        {QStringLiteral("clamp"), QStringLiteral("clamp(x, minValue, maxValue)")},
        {QStringLiteral("smoothstep"), QStringLiteral("smoothstep(min, max, x)")},
        {QStringLiteral("normalize"), QStringLiteral("normalize(x)")},
        {QStringLiteral("dot"), QStringLiteral("dot(x, y)")},
        {QStringLiteral("cross"), QStringLiteral("cross(x, y)")},
        {QStringLiteral("reflect"), QStringLiteral("reflect(incident, normal)")},
        {QStringLiteral("refract"), QStringLiteral("refract(incident, normal, eta)")},
        {QStringLiteral("texture"), QStringLiteral("texture(sampler, coordinate)")},
        {QStringLiteral("texelFetch"), QStringLiteral("texelFetch(sampler, coordinate, lod)")},
    };
    return signatures.value(name);
  }

  void callTip(EditorState *state)
  {
    if(!state || !state->editor)
      return;
    syncText(state, false);
    const int caret = int(state->editor->currentPos());
    const int openParen = callOpenParen(state->text, caret);
    if(openParen < 0)
      return;

    int before = openParen;
    while(before > 0 && (state->text[before - 1] == ' ' || state->text[before - 1] == '\t'))
      before--;
    const QString function = identifierAt(state->text, before);
    if(function.isEmpty())
      return;

    QStringList signatures;
    for(const Symbol &symbol : symbolIndex.value(function))
      if(!symbol.signature.isEmpty() && !signatures.contains(symbol.signature))
        signatures.push_back(symbol.signature);
    if(signatures.isEmpty())
    {
      const QString builtin = builtinSignature(function);
      if(!builtin.isEmpty())
        signatures.push_back(builtin);
    }
    if(signatures.isEmpty())
      return;

    QString tip = signatures[0];
    if(signatures.size() > 1)
    {
      tip += QLatin1Char('\n');
      tip += QCoreApplication::translate("CodeEditorService", "Additional overloads: %1")
                 .arg(signatures.size() - 1);
    }
    const QByteArray encoded = tip.toUtf8();
    state->editor->callTipShow(openParen, encoded.constData());

    int parameter = 0;
    int depth = 0;
    for(int i = openParen + 1; i < caret && i < state->text.size(); i++)
    {
      if(state->text[i] == '(' || state->text[i] == '[' || state->text[i] == '{')
        depth++;
      else if(state->text[i] == ')' || state->text[i] == ']' || state->text[i] == '}')
        depth = qMax(0, depth - 1);
      else if(state->text[i] == ',' && depth == 0)
        parameter++;
    }

    int argumentStart = tip.indexOf(QLatin1Char('(')) + 1;
    for(int i = 0; i < parameter && argumentStart > 0; i++)
      argumentStart = tip.indexOf(QLatin1Char(','), argumentStart) + 1;
    if(argumentStart > 0)
    {
      while(argumentStart < tip.size() && tip[argumentStart].isSpace())
        argumentStart++;
      int argumentEnd = tip.indexOf(QLatin1Char(','), argumentStart);
      const int closing = tip.indexOf(QLatin1Char(')'), argumentStart);
      if(argumentEnd < 0 || (closing >= 0 && closing < argumentEnd))
        argumentEnd = closing;
      if(argumentEnd > argumentStart)
      {
        state->editor->callTipSetHlt(tip.left(argumentStart).toUtf8().size(),
                                     tip.left(argumentEnd).toUtf8().size());
      }
    }
  }

  void showDiagnosticAt(EditorState *state, int x, int y)
  {
    if(!state || !state->editor)
      return;
    const int position = int(state->editor->positionFromPointClose(x, y));
    if(position < 0)
      return;
    for(const Diagnostic &diagnostic : combinedDiagnostics(state))
    {
      if(position >= diagnostic.start && position < diagnostic.start + diagnostic.length)
      {
        const QByteArray message = diagnostic.message.left(1024).toUtf8();
        state->editor->callTipShow(position, message.constData());
        state->diagnosticTipActive = true;
        return;
      }
    }
  }

  QString findServerProgram()
  {
    if(!resolvedProgram.isEmpty())
      return resolvedProgram;

    QString candidate = configuredProgram.trimmed();
    if(candidate.isEmpty())
      candidate = QString::fromLocal8Bit(qgetenv("RENDERDOC_SHADER_LANGUAGE_SERVER")).trimmed();
    if(candidate.size() > 1 && candidate.startsWith(QLatin1Char('"')) &&
       candidate.endsWith(QLatin1Char('"')))
      candidate = candidate.mid(1, candidate.size() - 2);

    if(!candidate.isEmpty())
    {
      if(QFileInfo(candidate).isExecutable())
        resolvedProgram = QFileInfo(candidate).absoluteFilePath();
      else
        resolvedProgram = QStandardPaths::findExecutable(candidate);
      return resolvedProgram;
    }

    resolvedProgram = QStandardPaths::findExecutable(QStringLiteral("shader-language-server"));
    if(resolvedProgram.isEmpty())
      resolvedProgram = QStandardPaths::findExecutable(QStringLiteral("slangd"));
    return resolvedProgram;
  }

  bool startServer(bool reportMissing)
  {
    if(process->state() != QProcess::NotRunning)
      return true;
    const QString program = findServerProgram();
    serverAttempted = true;
    if(program.isEmpty())
    {
      if(reportMissing)
        reportStatus(QCoreApplication::translate(
            "CodeEditorService",
            "No shader language server was found; local IDE services remain active"));
      return false;
    }

    receiveBuffer.clear();
    pending.clear();
    serverReady = false;
    stopping = false;
    lastServerError.clear();

    QStringList arguments = serverArguments;
    const QString serverName = QFileInfo(program).completeBaseName().toLower();
    if(arguments.isEmpty())
    {
      if(serverName == QStringLiteral("shader-language-server"))
      {
        arguments << QStringLiteral("--stdio") << QStringLiteral("--hlsl")
                  << QStringLiteral("--glsl");
        for(EditorState *state : states)
        {
          const QString localPath = pathFromUri(state->uri);
          if(!localPath.isEmpty())
          {
            const QString workspace = QFileInfo(localPath).absolutePath();
            process->setWorkingDirectory(workspace);
            arguments << QStringLiteral("--cwd") << workspace;
            break;
          }
        }
      }
      else if(serverName == QStringLiteral("slangd"))
      {
        arguments << QStringLiteral("--stdio");
      }
    }

    process->start(program, arguments, QIODevice::ReadWrite);
    return true;
  }

  bool sendJson(const QJsonObject &object)
  {
    if(process->state() != QProcess::Running)
      return false;
    const QByteArray body = QJsonDocument(object).toJson(QJsonDocument::Compact);
    if(body.isEmpty() || body.size() > MaxMessageBytes)
    {
      reportStatus(QCoreApplication::translate(
          "CodeEditorService",
          "Language-server message was rejected because it exceeded the safety limit"));
      return false;
    }
    const QByteArray header = QByteArrayLiteral("Content-Length: ") +
                              QByteArray::number(body.size()) + QByteArrayLiteral("\r\n\r\n");
    return process->write(header) == header.size() && process->write(body) == body.size();
  }

  int sendRequest(const QString &method, const QJsonObject &params, const PendingRequest &request)
  {
    const int id = nextRequestId++;
    QJsonObject object;
    object[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    object[QStringLiteral("id")] = id;
    object[QStringLiteral("method")] = method;
    object[QStringLiteral("params")] = params;
    if(!sendJson(object))
      return -1;
    pending[id] = request;
    return id;
  }

  bool sendNotification(const QString &method, const QJsonObject &params)
  {
    QJsonObject object;
    object[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    object[QStringLiteral("method")] = method;
    object[QStringLiteral("params")] = params;
    return sendJson(object);
  }

  void sendInitialize()
  {
    QJsonObject capabilities;
    QJsonObject textDocument;
    textDocument[QStringLiteral("completion")] = QJsonObject();
    textDocument[QStringLiteral("definition")] = QJsonObject();
    textDocument[QStringLiteral("publishDiagnostics")] = QJsonObject();
    capabilities[QStringLiteral("textDocument")] = textDocument;

    QJsonObject clientInfo;
    clientInfo[QStringLiteral("name")] = QStringLiteral("RenderDoc Ultra");
    clientInfo[QStringLiteral("version")] = QStringLiteral("1");

    QJsonObject params;
    params[QStringLiteral("processId")] = double(QCoreApplication::applicationPid());
    params[QStringLiteral("clientInfo")] = clientInfo;
    params[QStringLiteral("capabilities")] = capabilities;
    params[QStringLiteral("trace")] = QStringLiteral("off");

    for(EditorState *state : states)
    {
      const QString localPath = pathFromUri(state->uri);
      if(!localPath.isEmpty())
      {
        params[QStringLiteral("rootUri")] =
            QUrl::fromLocalFile(QFileInfo(localPath).absolutePath()).toString(QUrl::FullyEncoded);
        break;
      }
    }

    PendingRequest request;
    request.kind = PendingKind::Initialize;
    sendRequest(QStringLiteral("initialize"), params, request);
  }

  void sendDidOpen(EditorState *state)
  {
    if(!serverReady || !state || state->openSent)
      return;
    QJsonObject item;
    item[QStringLiteral("uri")] = state->uri;
    item[QStringLiteral("languageId")] = state->language;
    item[QStringLiteral("version")] = state->version;
    item[QStringLiteral("text")] = QString::fromUtf8(state->text);
    QJsonObject params;
    params[QStringLiteral("textDocument")] = item;
    state->openSent = sendNotification(QStringLiteral("textDocument/didOpen"), params);
  }

  void sendDidClose(EditorState *state)
  {
    if(!serverReady || !state || !state->openSent)
      return;
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    sendNotification(QStringLiteral("textDocument/didClose"), params);
    state->openSent = false;
  }

  void sendDidChange(EditorState *state)
  {
    if(!serverReady || !state)
      return;
    if(!state->openSent)
    {
      sendDidOpen(state);
      return;
    }

    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    document[QStringLiteral("version")] = state->version;
    QJsonObject change;
    change[QStringLiteral("text")] = QString::fromUtf8(state->text);
    QJsonArray changes;
    changes.append(change);
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("contentChanges")] = changes;
    sendNotification(QStringLiteral("textDocument/didChange"), params);
  }

  void requestLspCompletion(EditorState *state, const QString &prefix)
  {
    if(!serverReady || !state || !state->openSent || !state->editor)
      return;
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("position")] = lspPosition(state->text, int(state->editor->currentPos()));
    QJsonObject context;
    context[QStringLiteral("triggerKind")] = 1;
    params[QStringLiteral("context")] = context;
    PendingRequest request;
    request.kind = PendingKind::Completion;
    request.editor = state->editor;
    request.sourceEditor = state->editor.data();
    request.prefix = prefix;
    request.version = state->version;
    request.editRevision = state->editRevision;
    request.position = int(state->editor->currentPos());
    sendRequest(QStringLiteral("textDocument/completion"), params, request);
  }

  bool requestLspDefinition(EditorState *state, int position, const Location &fallback)
  {
    if(!serverReady || !state || !state->openSent || !state->editor)
      return false;
    QJsonObject document;
    document[QStringLiteral("uri")] = state->uri;
    QJsonObject params;
    params[QStringLiteral("textDocument")] = document;
    params[QStringLiteral("position")] = lspPosition(state->text, position);
    PendingRequest request;
    request.kind = PendingKind::Definition;
    request.editor = state->editor;
    request.sourceEditor = state->editor.data();
    request.version = state->version;
    request.editRevision = state->editRevision;
    request.position = position;
    request.localFallback = fallback;
    request.localFallbackEditor = fallback.editor;
    for(EditorState *documentState : states)
      request.documentRevisions.insert(documentState->editor.data(), documentState->editRevision);
    return sendRequest(QStringLiteral("textDocument/definition"), params, request) >= 0;
  }

  void rejectServerRequest(const QJsonObject &object)
  {
    QJsonObject error;
    error[QStringLiteral("code")] = -32601;
    // i18n-ignore: JSON-RPC error text is sent to the language server, not displayed in qrenderdoc.
    error[QStringLiteral("message")] = QStringLiteral("Client-side server commands are disabled");
    QJsonObject response;
    response[QStringLiteral("jsonrpc")] = QStringLiteral("2.0");
    response[QStringLiteral("id")] = object.value(QStringLiteral("id"));
    response[QStringLiteral("error")] = error;
    sendJson(response);
  }

  void readServerOutput()
  {
    receiveBuffer += process->readAllStandardOutput();
    if(receiveBuffer.size() > MaxMessageBytes + MaxHeaderBytes)
    {
      reportStatus(QCoreApplication::translate(
          "CodeEditorService", "Language server was stopped after exceeding the receive limit"));
      stopping = true;
      process->kill();
      receiveBuffer.clear();
      return;
    }

    while(true)
    {
      const int headerEnd = receiveBuffer.indexOf("\r\n\r\n");
      if(headerEnd < 0)
      {
        if(receiveBuffer.size() > MaxHeaderBytes)
        {
          reportStatus(QCoreApplication::translate("CodeEditorService",
                                                   "Language server sent an invalid header"));
          stopping = true;
          process->kill();
          receiveBuffer.clear();
        }
        return;
      }

      int contentLength = -1;
      const QList<QByteArray> headers = receiveBuffer.left(headerEnd).split('\n');
      for(QByteArray header : headers)
      {
        header = header.trimmed();
        const int colon = header.indexOf(':');
        if(colon > 0 &&
           header.left(colon).trimmed().toLower() == QByteArrayLiteral("content-length"))
        {
          bool ok = false;
          contentLength = header.mid(colon + 1).trimmed().toInt(&ok);
          if(!ok)
            contentLength = -1;
        }
      }
      if(contentLength < 0 || contentLength > MaxMessageBytes)
      {
        reportStatus(QCoreApplication::translate(
            "CodeEditorService", "Language server sent an invalid message length"));
        stopping = true;
        process->kill();
        receiveBuffer.clear();
        return;
      }

      const int bodyStart = headerEnd + 4;
      if(receiveBuffer.size() - bodyStart < contentLength)
        return;
      const QByteArray body = receiveBuffer.mid(bodyStart, contentLength);
      receiveBuffer.remove(0, bodyStart + contentLength);

      QJsonParseError error;
      const QJsonDocument document = QJsonDocument::fromJson(body, &error);
      if(error.error == QJsonParseError::NoError && document.isObject())
        handleServerMessage(document.object());
    }
  }

  void handlePublishDiagnostics(const QJsonObject &params)
  {
    EditorState *state = stateForUri(params.value(QStringLiteral("uri")).toString());
    if(!state)
      return;

    // A diagnostic notification can race the debounce timer after a local edit. Synchronise the
    // document first so versioned diagnostics are compared against the text currently displayed,
    // and discard an unversioned notification that was already in flight for the previous text.
    const bool hadUnsynchronisedEdit = state->textDirty;
    if(hadUnsynchronisedEdit)
      syncText(state, false);

    const QJsonValue versionValue = params.value(QStringLiteral("version"));
    if(versionValue.isDouble())
    {
      const int diagnosticVersion = versionValue.toInt(-1);
      if(diagnosticVersion != state->version)
        return;
    }
    else if(hadUnsynchronisedEdit)
    {
      return;
    }

    state->lspDiagnostics.clear();
    const QJsonArray values = params.value(QStringLiteral("diagnostics")).toArray();
    const int count = qMin(values.size(), MaxDiagnostics);
    for(int i = 0; i < count; i++)
    {
      const QJsonObject value = values[i].toObject();
      const QJsonObject range = value.value(QStringLiteral("range")).toObject();
      const int start = bytePosition(state->text, range.value(QStringLiteral("start")).toObject());
      const int end = bytePosition(state->text, range.value(QStringLiteral("end")).toObject());
      Diagnostic diagnostic;
      diagnostic.start = start;
      diagnostic.length = qMax(1, end - start);
      diagnostic.line = int(state->editor ? state->editor->lineFromPosition(start) : 0);
      diagnostic.column =
          state->editor ? start - int(state->editor->positionFromLine(diagnostic.line)) : 0;
      const int severity = value.value(QStringLiteral("severity")).toInt(1);
      diagnostic.severity = severity == 1   ? DiagnosticSeverity::Error
                            : severity == 2 ? DiagnosticSeverity::Warning
                            : severity == 3 ? DiagnosticSeverity::Information
                                            : DiagnosticSeverity::Hint;
      diagnostic.message = value.value(QStringLiteral("message")).toString().left(2048);
      diagnostic.source = value.value(QStringLiteral("source")).toString().left(128);
      state->lspDiagnostics.push_back(diagnostic);
    }
    displayDiagnostics(state);
  }

  QStringList completionLabels(const QJsonValue &result) const
  {
    QJsonArray items;
    if(result.isArray())
      items = result.toArray();
    else if(result.isObject())
      items = result.toObject().value(QStringLiteral("items")).toArray();

    QStringList labels;
    const int count = qMin(items.size(), MaxCompletionItems);
    for(int i = 0; i < count; i++)
    {
      if(items[i].isString())
        labels.push_back(items[i].toString());
      else
        labels.push_back(items[i].toObject().value(QStringLiteral("label")).toString());
    }
    return labels;
  }

  Location locationFromLsp(const QJsonValue &result) const
  {
    QJsonObject value;
    if(result.isArray())
    {
      const QJsonArray array = result.toArray();
      if(array.isEmpty())
        return Location();
      value = array[0].toObject();
    }
    else
    {
      value = result.toObject();
    }

    const QString uri = value.contains(QStringLiteral("targetUri"))
                            ? value.value(QStringLiteral("targetUri")).toString()
                            : value.value(QStringLiteral("uri")).toString();
    const QJsonObject range = value.contains(QStringLiteral("targetSelectionRange"))
                                  ? value.value(QStringLiteral("targetSelectionRange")).toObject()
                                  : value.value(QStringLiteral("range")).toObject();
    const QJsonObject start = range.value(QStringLiteral("start")).toObject();
    Location location;
    EditorState *state = stateForUri(uri);
    location.fileName = state ? state->fileName : pathFromUri(uri);
    location.line = start.value(QStringLiteral("line")).toInt(-1);
    location.column = start.value(QStringLiteral("character")).toInt(-1);
    if(state && state->editor)
    {
      location.editor = state->editor.data();
      location.position = bytePosition(state->text, start);
      location.line = int(location.editor->lineFromPosition(location.position));
      location.column = location.position - int(location.editor->positionFromLine(location.line));
    }
    return location;
  }

  void handleServerResponse(const QJsonObject &object)
  {
    const int id = object.value(QStringLiteral("id")).toInt(-1);
    if(!pending.contains(id))
      return;
    const PendingRequest request = pending.take(id);
    const QJsonValue result = object.value(QStringLiteral("result"));

    if(request.kind == PendingKind::Initialize)
    {
      if(object.contains(QStringLiteral("error")))
      {
        reportStatus(QCoreApplication::translate(
            "CodeEditorService", "Shader language server rejected initialization"));
        stopping = true;
        process->kill();
        return;
      }
      serverReady = true;
      sendNotification(QStringLiteral("initialized"), QJsonObject());
      for(EditorState *state : states)
        sendDidOpen(state);
      reportStatus(QCoreApplication::translate("CodeEditorService",
                                               "Shader language server connected: %1")
                       .arg(resolvedProgram));
    }
    else if(request.kind == PendingKind::Completion)
    {
      EditorState *state = stateFor(request.editor.data());
      if(state && state->version == request.version &&
         state->editRevision == request.editRevision && state->editor &&
         state->editor->currentPos() == request.position)
        showCompletions(state, request.prefix, completionLabels(result));
    }
    else if(request.kind == PendingKind::Definition)
    {
      EditorState *sourceState = stateFor(request.editor.data());
      if(!sourceState || sourceState->version != request.version ||
         sourceState->editRevision != request.editRevision ||
         sourceState->editor->currentPos() != request.position)
        return;
      // The server response uses positions from its request snapshot. Do not apply it if
      // any open target changed in the meantime, even when the source stayed unchanged.
      for(auto it = request.documentRevisions.constBegin(); it != request.documentRevisions.constEnd(); ++it)
      {
        EditorState *documentState = stateFor(it.key());
        if(!documentState || documentState->editRevision != it.value()) return;
      }
      const Location location = locationFromLsp(result);
      if(location.editor || !location.fileName.isEmpty())
        navigateToDefinition(location, request.editor.data(), request.position);
      else if(request.localFallbackEditor)
      {
        Location fallback = request.localFallback;
        fallback.editor = request.localFallbackEditor.data();
        navigateToDefinition(fallback, request.editor.data(), request.position);
      }
      else
        reportStatus(QCoreApplication::translate(
            "CodeEditorService", "No definition was found for the selected symbol"));
    }
  }

  void handleServerMessage(const QJsonObject &object)
  {
    if(object.contains(QStringLiteral("method")))
    {
      const QString method = object.value(QStringLiteral("method")).toString();
      if(method == QStringLiteral("textDocument/publishDiagnostics"))
      {
        handlePublishDiagnostics(object.value(QStringLiteral("params")).toObject());
      }
      else if(object.contains(QStringLiteral("id")))
      {
        // Do not execute workspace/applyEdit, workspace/executeCommand, or any other server request.
        rejectServerRequest(object);
      }
      return;
    }
    if(object.contains(QStringLiteral("id")))
      handleServerResponse(object);
  }
};

CodeEditorService::CodeEditorService(QObject *parent) : QObject(parent), m_Impl(new Impl(this))
{
}

CodeEditorService::~CodeEditorService()
{
  for(ScintillaEdit *editor : m_Impl->states.keys())
  {
    editor->removeEventFilter(this);
    QObject::disconnect(editor, NULL, this, NULL);
  }
  delete m_Impl;
  m_Impl = NULL;
}

void CodeEditorService::registerEditor(ScintillaEdit *editor, const QString &fileName,
                                       const QString &language)
{
  if(!editor)
    return;

  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(state)
  {
    if(state->fileName == fileName && state->language == normaliseLanguage(language, fileName))
      return;
    m_Impl->sendDidClose(state);
    state->fileName = fileName;
    state->uri = m_Impl->makeUri(fileName);
    state->language = normaliseLanguage(language, fileName);
    state->version++;
    state->editRevision++;
    state->openSent = false;
    m_Impl->sendDidOpen(state);
    m_Impl->rebuildSymbolIndex();
    return;
  }

  state = new Impl::EditorState;
  state->editor = editor;
  state->fileName = fileName;
  state->uri = m_Impl->makeUri(fileName);
  state->language = normaliseLanguage(language, fileName);
  state->text = editor->getText(editor->textLength() + 1);
  state->localDiagnostics = m_Impl->localDiagnosticsFor(state);
  m_Impl->states[editor] = state;
  editor->installEventFilter(this);
  m_Impl->configureIndicators(editor);
  m_Impl->rebuildSymbolIndex();
  m_Impl->displayDiagnostics(state);

  QObject::connect(editor, &ScintillaEdit::destroyed, this,
                   [this, editor](QObject *) { unregisterEditor(editor); });
  QObject::connect(editor, &ScintillaEdit::modified, this,
                   [this, state](int type, int position, int length, int, const QByteArray &, int, int, int) {
                     if(type & (SC_MOD_INSERTTEXT | SC_MOD_DELETETEXT))
                     {
                       m_Impl->moveHistoryForEdit(state, type, position, length);
                       m_Impl->markModified(state);
                     }
                   });
  QObject::connect(editor, &ScintillaEdit::charAdded, this, [this, state](int character) {
    if(!state->editor)
      return;
    if(character == '(' || character == ',')
    {
      m_Impl->callTip(state);
      return;
    }

    if(character == '.' || character == ':' || character == '>')
    {
      requestCompletion(state->editor.data(), false);
      return;
    }

    if(isIdentifierByte(char(character)))
    {
      const int caret = int(state->editor->currentPos());
      const QPair<int, int> prefixRange =
          identifierRange(state->editor->getText(state->editor->textLength() + 1), caret);
      if(prefixRange.second - prefixRange.first >= 2)
        requestCompletion(state->editor.data(), false);
    }
  });
  QObject::connect(editor, &ScintillaEdit::keyPressed, this,
                    [this, editor](QKeyEvent *event) { handleKeyPress(editor, event); });
  QObject::connect(editor, &ScintillaEdit::updateUi, this,
                   [this, state]() {
                     m_Impl->updateBraceHighlight(state);
                     m_Impl->updateSymbolHighlights(state);
                   });
  QObject::connect(editor, &ScintillaEdit::dwellStart, this,
                   [this, state](int x, int y) { m_Impl->showDiagnosticAt(state, x, y); });
  QObject::connect(editor, &ScintillaEdit::dwellEnd, this, [state](int, int) {
    if(state->editor && state->diagnosticTipActive)
    {
      state->editor->callTipCancel();
      state->diagnosticTipActive = false;
    }
  });

  m_Impl->sendDidOpen(state);
  m_Impl->updateBraceHighlight(state);
  if(!m_Impl->serverAttempted)
    m_Impl->startServer(false);
}

void CodeEditorService::unregisterEditor(ScintillaEdit *editor)
{
  Impl::EditorState *state = m_Impl->states.take(editor);
  if(!state)
    return;

  // A definition request can retain a local fallback in a different document. Remove every
  // request that refers to the editor before its raw address can become dangling.
  for(auto it = m_Impl->pending.begin(); it != m_Impl->pending.end();)
  {
    const Impl::PendingRequest &request = it.value();
    if(request.sourceEditor == editor || request.editor.data() == editor ||
       request.localFallback.editor == editor ||
       request.localFallbackEditor.data() == editor)
      it = m_Impl->pending.erase(it);
    else
      ++it;
  }

  m_Impl->sendDidClose(state);
  if(m_Impl->referencesMenu) m_Impl->referencesMenu->close();
  editor->removeEventFilter(this);
  QObject::disconnect(editor, NULL, this, NULL);
  for(QList<Impl::NavigationEntry> *history : {&m_Impl->backHistory, &m_Impl->forwardHistory})
    for(int i = history->size() - 1; i >= 0; --i)
      if((*history)[i].editor.data() == editor) history->removeAt(i);
  delete state;
  m_Impl->rebuildSymbolIndex();
}

bool CodeEditorService::isRegistered(ScintillaEdit *editor) const
{
  return m_Impl->states.contains(editor);
}

void CodeEditorService::setDefinitionHandler(const DefinitionHandler &handler)
{
  m_Impl->definitionHandler = handler;
}

void CodeEditorService::setDiagnosticsHandler(const DiagnosticsHandler &handler)
{
  m_Impl->diagnosticsHandler = handler;
  if(handler)
    for(Impl::EditorState *state : m_Impl->states)
      handler(state->editor.data(), m_Impl->combinedDiagnostics(state));
}

void CodeEditorService::setStatusHandler(const StatusHandler &handler)
{
  m_Impl->statusHandler = handler;
}

bool CodeEditorService::eventFilter(QObject *watched, QEvent *event)
{
  ScintillaEdit *editor = qobject_cast<ScintillaEdit *>(watched);
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state || !event || event->type() != QEvent::KeyPress)
    return QObject::eventFilter(watched, event);

  QKeyEvent *keyEvent = static_cast<QKeyEvent *>(event);
  const Qt::KeyboardModifiers modifiers = keyEvent->modifiers();
  Qt::KeyboardModifiers modifiersWithoutKeypad = modifiers & ~Qt::KeypadModifier;

  if(modifiersWithoutKeypad == Qt::AltModifier &&
     (keyEvent->key() == Qt::Key_Left || keyEvent->key() == Qt::Key_Right))
  {
    if(m_Impl->navigateHistory(editor, keyEvent->key() == Qt::Key_Left))
    {
      event->accept();
      return true;
    }
  }

  // Handle command keys before Scintilla sees them. Its keyPressed signal is emitted after its
  // normal key map has already run, which is too late for Ctrl+/ and the custom line operations.
  if(handleKeyPress(editor, keyEvent))
    return true;

  if(editor->readOnly())
    return QObject::eventFilter(watched, event);

  const bool primary = hasPrimaryModifier(modifiers, false);
  const bool primaryShift = (modifiersWithoutKeypad & Qt::ShiftModifier) &&
                            hasPrimaryModifier(modifiers, true);
  if(keyEvent->key() == Qt::Key_Backspace && modifiersWithoutKeypad == Qt::NoModifier)
  {
    if(m_Impl->deleteEmptyPair(state))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) && primary)
  {
    if(m_Impl->insertOpenLine(state, false))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) && primaryShift)
  {
    if(m_Impl->insertOpenLine(state, true))
    {
      event->accept();
      return true;
    }
  }
  if(keyEvent->key() == Qt::Key_Slash && primary)
  {
    if(m_Impl->toggleLineComment(state))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Tab || keyEvent->key() == Qt::Key_Backtab) &&
     !(modifiersWithoutKeypad & (Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier)))
  {
    const bool unindent = keyEvent->key() == Qt::Key_Backtab ||
                          (modifiersWithoutKeypad & Qt::ShiftModifier);
    if(m_Impl->indentSelectedLines(state, unindent))
    {
      event->accept();
      return true;
    }
  }
  if((keyEvent->key() == Qt::Key_Return || keyEvent->key() == Qt::Key_Enter) &&
     (modifiersWithoutKeypad == Qt::NoModifier ||
      modifiersWithoutKeypad == Qt::ShiftModifier))
  {
    if(m_Impl->insertSmartNewline(state))
    {
      event->accept();
      return true;
    }
  }

  if(!(modifiersWithoutKeypad &
       (Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier)))
  {
    const QString text = keyEvent->text();
    if(text.size() == 1 && text[0].unicode() <= 0x7f &&
       m_Impl->typePairedCharacter(state, char(text[0].unicode())))
    {
      event->accept();
      return true;
    }
  }

  return QObject::eventFilter(watched, event);
}

bool CodeEditorService::handleKeyPress(ScintillaEdit *editor, QKeyEvent *event)
{
  if(!editor || !event || !isRegistered(editor))
    return false;

  if(event->key() == Qt::Key_F12 && event->modifiers() == Qt::NoModifier)
  {
    requestDefinition(editor);
    event->accept();
    return true;
  }
  if(event->key() == Qt::Key_Space && (event->modifiers() & Qt::ControlModifier))
  {
    requestCompletion(editor, true);
    event->accept();
    return true;
  }
  return false;
}

void CodeEditorService::requestDefinition(ScintillaEdit *editor, int position)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return;
  m_Impl->syncText(state, false);
  if(position < 0)
    position = int(editor->currentPos());
  const Location fallback = m_Impl->localDefinition(state, position);
  const auto range = identifierRange(state->text, position);
  if(fallback.editor == editor && fallback.position == range.first)
  {
    m_Impl->showReferences(state, position);
    return;
  }
  if(!m_Impl->requestLspDefinition(state, position, fallback))
  {
    if(fallback.editor)
      m_Impl->navigateToDefinition(fallback, editor, position);
    else
      m_Impl->reportStatus(QCoreApplication::translate(
          "CodeEditorService", "No definition was found for the selected symbol"));
  }
}

void CodeEditorService::requestReferences(ScintillaEdit *editor, int position)
{
  if(!editor) return;
  m_Impl->showReferences(m_Impl->stateFor(editor), position < 0 ? int(editor->currentPos()) : position);
}

void CodeEditorService::rememberNavigation(ScintillaEdit *editor, int targetPosition)
{
  if(!editor || m_Impl->deliveringNavigation || targetPosition == editor->currentPos()) return;
  m_Impl->pushHistory(m_Impl->backHistory, m_Impl->navigationEntry(editor));
  m_Impl->forwardHistory.clear();
}

void CodeEditorService::clearNavigation(ScintillaEdit *editor)
{
  for(QList<Impl::NavigationEntry> *history : {&m_Impl->backHistory, &m_Impl->forwardHistory})
    for(int i = history->size() - 1; i >= 0; --i)
      if((*history)[i].editor.data() == editor) history->removeAt(i);
}

bool CodeEditorService::navigateHistory(ScintillaEdit *editor, bool backwards)
{
  return m_Impl->navigateHistory(editor, backwards);
}

bool CodeEditorService::canNavigateHistory(bool backwards) const
{
  return !(backwards ? m_Impl->backHistory : m_Impl->forwardHistory).isEmpty();
}

void CodeEditorService::requestCompletion(ScintillaEdit *editor, bool explicitRequest)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return;
  m_Impl->syncText(state, false);
  const int caret = int(editor->currentPos());
  const QPair<int, int> range = identifierRange(state->text, caret);
  const QString prefix = QString::fromUtf8(state->text.mid(range.first, caret - range.first));
  if(!explicitRequest && prefix.size() < 2 && caret > 0 && state->text[caret - 1] != '.' &&
     state->text[caret - 1] != ':' && state->text[caret - 1] != '>')
    return;
  m_Impl->showCompletions(state, prefix);
  m_Impl->requestLspCompletion(state, prefix);
}

void CodeEditorService::showCallTip(ScintillaEdit *editor)
{
  m_Impl->callTip(m_Impl->stateFor(editor));
}

CodeEditorService::Location CodeEditorService::findLocalDefinition(ScintillaEdit *editor,
                                                                   int position) const
{
  return m_Impl->localDefinition(m_Impl->stateFor(editor), position);
}

QList<CodeEditorService::Diagnostic> CodeEditorService::diagnostics(ScintillaEdit *editor) const
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state)
    return QList<Diagnostic>();
  m_Impl->syncText(state, true);
  return m_Impl->combinedDiagnostics(state);
}

void CodeEditorService::setExternalDiagnostics(ScintillaEdit *editor,
                                               const QList<Diagnostic> &diagnostics)
{
  Impl::EditorState *state = m_Impl->stateFor(editor);
  if(!state || !state->editor)
    return;

  state->externalDiagnostics.clear();
  const int documentLength = int(editor->length());
  const int count = qMin(diagnostics.size(), MaxDiagnostics);
  state->externalDiagnostics.reserve(count);
  for(int i = 0; i < count; i++)
  {
    Diagnostic diagnostic = diagnostics[i];
    diagnostic.start = qBound(0, diagnostic.start, documentLength);
    diagnostic.length = qMax(1, qMin(diagnostic.length, documentLength - diagnostic.start));
    diagnostic.line = int(editor->lineFromPosition(diagnostic.start));
    diagnostic.column = diagnostic.start - int(editor->positionFromLine(diagnostic.line));
    diagnostic.message = diagnostic.message.left(2048);
    diagnostic.source = diagnostic.source.left(128);
    state->externalDiagnostics.push_back(diagnostic);
  }
  m_Impl->displayDiagnostics(state);
}

void CodeEditorService::setLanguageServerCommand(const QString &program, const QStringList &arguments)
{
  stopLanguageServer();
  m_Impl->configuredProgram = program;
  m_Impl->resolvedProgram.clear();
  m_Impl->serverArguments = arguments;
  m_Impl->serverAttempted = false;
}

bool CodeEditorService::startLanguageServer()
{
  return m_Impl->startServer(true);
}

void CodeEditorService::stopLanguageServer()
{
  if(m_Impl->process->state() == QProcess::NotRunning)
    return;
  m_Impl->stopping = true;
  m_Impl->serverReady = false;
  m_Impl->pending.clear();
  for(Impl::EditorState *state : m_Impl->states)
    state->openSent = false;
  m_Impl->process->terminate();
  if(!m_Impl->process->waitForFinished(150))
  {
    m_Impl->process->kill();
    m_Impl->process->waitForFinished(150);
  }
}

QString CodeEditorService::languageServerProgram() const
{
  return m_Impl->findServerProgram();
}

bool CodeEditorService::languageServerRunning() const
{
  return m_Impl->serverReady && m_Impl->process->state() == QProcess::Running;
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Code editor smart newline planning", "[code-editor]")
{
  SECTION("an empty brace pair is split onto three lines")
  {
    const QByteArray text("if(enabled) {}");
    const int caret = text.indexOf('{') + 1;
    const SmartNewlineEdit edit =
        planSmartNewline(text, caret, caret, QByteArray("\n"), QByteArray("  "), false);
    CHECK(edit.replacement == QByteArray("\n  \n"));
    CHECK(edit.caretOffset == 3);
  }

  SECTION("the current indentation and editor EOL are preserved")
  {
    const QByteArray text("\tif(enabled) {");
    const SmartNewlineEdit edit = planSmartNewline(
        text, text.size(), text.size(), QByteArray("\r\n"), QByteArray("\t"), false);
    CHECK(edit.replacement == QByteArray("\r\n\t\t"));
    CHECK(edit.caretOffset == edit.replacement.size());
  }

  SECTION("python suites indent after a colon")
  {
    const QByteArray text("def render():");
    const SmartNewlineEdit edit = planSmartNewline(
        text, text.size(), text.size(), QByteArray("\n"), QByteArray("    "), true);
    CHECK(edit.replacement == QByteArray("\n    "));
  }
}

TEST_CASE("Code editor position transforms follow line comment edits", "[code-editor]")
{
  QList<TextReplacement> edits;
  edits.push_back({0, 0, QByteArray("// ")});
  edits.push_back({8, 0, QByteArray("// ")});
  CHECK(transformedPosition(0, edits) == 3);
  CHECK(transformedPosition(5, edits) == 8);
  CHECK(transformedPosition(10, edits) == 16);
}

TEST_CASE("Code editor indexes every comma declarator without indexing initializer arguments", "[code-editor]")
{
  SECTION("later local declarations retain their own source positions")
  {
    const QByteArray code("float value; float G(){ float first=0, value=1; return value; }");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("float"));
    REQUIRE(declarations.size() == 1);
    CHECK(declarations[0].name.toUtf8() == QByteArray("value"));
    CHECK(declarations[0].position == code.indexOf("value=1"));
    CHECK(declarations[0].valueType.toUtf8() == QByteArray("float"));
  }
  SECTION("nested calls, indices and aggregates do not create declarations")
  {
    const QByteArray code("float first=Make(1, 2), values[2]={3, 4}, last=values[Index(0, 1)];");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("float"));
    REQUIRE(declarations.size() == 2);
    CHECK(declarations[0].name.toUtf8() == QByteArray("values"));
    CHECK(declarations[1].name.toUtf8() == QByteArray("last"));
  }
  SECTION("the next typed function parameter is not a comma variable")
  {
    const QByteArray code("float F(float first, float value) { return value; }");
    CHECK(followingDeclarators(code, codeDelimiterPairs(code), code.indexOf("first") + 5,
                              QStringLiteral("float")).isEmpty());
  }
  SECTION("for initializers stop before the loop condition")
  {
    const QByteArray code("for(int first=0, value=1; Check(first, value); ++value) {}");
    const auto declarations = followingDeclarators(
        code, codeDelimiterPairs(code), code.indexOf("first") + 5, QStringLiteral("int"));
    REQUIRE(declarations.size() == 1);
    CHECK(declarations[0].name.toUtf8() == QByteArray("value"));
  }
}

#endif
