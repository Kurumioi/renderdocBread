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
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QKeyEvent>
#include <QPointer>
#include <QProcess>
#include <QRegularExpression>
#include <QSet>
#include <QStandardPaths>
#include <QTimer>
#include <QUrl>
#include <algorithm>
#include <limits>

#include "scintilla/include/Scintilla.h"
#include "scintilla/include/qt/ScintillaEdit.h"

namespace
{
const int ErrorIndicator = 8;
const int WarningIndicator = 9;
const int DiagnosticsDebounceMS = 160;
const int MaxCompletionItems = 256;
const int MaxDiagnostics = 2000;
const int MaxMessageBytes = 4 * 1024 * 1024;
const int MaxHeaderBytes = 16 * 1024;

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
    int version = 1;
    quint64 editRevision = 1;
    bool textDirty = false;
    bool diagnosticsDirty = false;
    bool openSent = false;
    bool diagnosticTipActive = false;
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
  };

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
    editor->autoCSetSeparator('\n');
    editor->autoCSetIgnoreCase(true);
    editor->autoCSetAutoHide(true);
    editor->autoCSetDropRestOfWord(false);
    editor->autoCSetMaxHeight(12);
    editor->autoCSetMaxWidth(64);
    editor->setMouseDwellTime(500);
  }

  void addSymbol(EditorState *state, const QString &name, int position,
                 const QString &signature = QString())
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
                       "([A-Za-z_][A-Za-z0-9_]*)\\s*(?=[=;,:\\[])"));
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
        addSymbol(state, name, position, function ? cleanSignature(match.captured(0)) : QString());
      }
    };

    addMatches(typeDefinition, 1, false);
    addMatches(usingDefinition, 1, false);
    addMatches(functionDefinition, 2, true);
    addMatches(variableDefinition, 2, false);
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
    diagnosticTimer->start();
  }

  Location localDefinition(EditorState *sourceState, int position)
  {
    Location ret;
    if(!sourceState || !sourceState->editor)
      return ret;

    syncText(sourceState, false);
    if(position < 0)
      position = int(sourceState->editor->currentPos());
    QPair<int, int> wordRange;
    const QString word = identifierAt(sourceState->text, position, &wordRange);
    if(word.isEmpty())
      return ret;

    const QList<Symbol> candidates = symbolIndex.value(word);
    const Symbol *best = NULL;
    qint64 bestScore = std::numeric_limits<qint64>::min();
    for(const Symbol &candidate : candidates)
    {
      if(!candidate.state || !candidate.state->editor)
        continue;
      qint64 score = 0;
      if(candidate.state == sourceState)
      {
        score += 1000000000LL;
        if(candidate.position <= wordRange.first)
          score += 500000000LL - qMin(500000000, wordRange.first - candidate.position);
        else
          score -= candidate.position - wordRange.first;
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
    if(definitionHandler)
    {
      definitionHandler(location);
    }
    else if(location.editor && location.position >= 0)
    {
      location.editor->gotoPos(location.position);
      location.editor->QWidget::setFocus(Qt::ShortcutFocusReason);
    }
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
    request.localFallback = fallback;
    request.localFallbackEditor = fallback.editor;
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
      const Location location = locationFromLsp(result);
      if(location.editor || !location.fileName.isEmpty())
        deliverDefinition(location);
      else if(request.localFallbackEditor)
      {
        Location fallback = request.localFallback;
        fallback.editor = request.localFallbackEditor.data();
        deliverDefinition(fallback);
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
    QObject::disconnect(editor, NULL, this, NULL);
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
  m_Impl->configureIndicators(editor);
  m_Impl->rebuildSymbolIndex();
  m_Impl->displayDiagnostics(state);

  QObject::connect(editor, &ScintillaEdit::destroyed, this,
                   [this, editor](QObject *) { unregisterEditor(editor); });
  QObject::connect(editor, &ScintillaEdit::modified, this,
                   [this, state](int type, int, int, int, const QByteArray &, int, int, int) {
                     if(type & (SC_MOD_INSERTTEXT | SC_MOD_DELETETEXT))
                       m_Impl->markModified(state);
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
  QObject::disconnect(editor, NULL, this, NULL);
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
  if(!m_Impl->requestLspDefinition(state, position, fallback))
  {
    if(fallback.editor)
      m_Impl->deliverDefinition(fallback);
    else
      m_Impl->reportStatus(QCoreApplication::translate(
          "CodeEditorService", "No definition was found for the selected symbol"));
  }
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
