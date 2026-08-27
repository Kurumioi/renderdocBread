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

#pragma once

#include <QList>
#include <QObject>
#include <QString>
#include <QStringList>
#include <functional>

class QKeyEvent;
class ScintillaEdit;

// A small editor-services layer shared by editable shader source views. It deliberately has no
// Q_OBJECT macro: ShaderViewer owns the UI and receives results through callbacks, while this class
// can be compiled without adding another moc target.
class CodeEditorService : public QObject
{
public:
  enum class DiagnosticSeverity
  {
    Error = 1,
    Warning = 2,
    Information = 3,
    Hint = 4,
  };

  struct Location
  {
    ScintillaEdit *editor = NULL;
    QString fileName;
    int position = -1;
    int line = -1;
    int column = -1;

    bool isValid() const { return editor != NULL && position >= 0; }
  };

  struct Diagnostic
  {
    int start = 0;
    int length = 1;
    int line = 0;
    int column = 0;
    DiagnosticSeverity severity = DiagnosticSeverity::Error;
    QString message;
    QString source;
  };

  using DefinitionHandler = std::function<void(const Location &location)>;
  using DiagnosticsHandler =
      std::function<void(ScintillaEdit *editor, const QList<Diagnostic> &diagnostics)>;
  using StatusHandler = std::function<void(const QString &message)>;

  explicit CodeEditorService(QObject *parent = NULL);
  ~CodeEditorService() override;

  // language should be an LSP language id such as "hlsl", "glsl", "slang", or "python".
  // Registration installs the local completion/F12/diagnostic hooks. Re-registering an editor
  // updates its identity without installing duplicate hooks.
  void registerEditor(ScintillaEdit *editor, const QString &fileName, const QString &language);
  void unregisterEditor(ScintillaEdit *editor);
  bool isRegistered(ScintillaEdit *editor) const;

  void setDefinitionHandler(const DefinitionHandler &handler);
  void setDiagnosticsHandler(const DiagnosticsHandler &handler);
  void setStatusHandler(const StatusHandler &handler);

  // These methods can also be called directly from menu actions. Registered editors already map
  // F12, Ctrl+Space, identifier typing, '(' and ',' to the corresponding operations.
  bool handleKeyPress(ScintillaEdit *editor, QKeyEvent *event);
  void requestDefinition(ScintillaEdit *editor, int position = -1);
  void requestCompletion(ScintillaEdit *editor, bool explicitRequest = true);
  void showCallTip(ScintillaEdit *editor);

  Location findLocalDefinition(ScintillaEdit *editor, int position = -1) const;
  QList<Diagnostic> diagnostics(ScintillaEdit *editor) const;

  // An external server is optional. If no command is configured, startLanguageServer() checks
  // RENDERDOC_SHADER_LANGUAGE_SERVER, then shader-language-server and slangd on PATH. The server
  // is started directly (never through a shell), only the bounded LSP subset implemented in the
  // .cpp is accepted, and every editor feature above continues to work if startup fails.
  void setLanguageServerCommand(const QString &program, const QStringList &arguments = QStringList());
  bool startLanguageServer();
  void stopLanguageServer();
  QString languageServerProgram() const;
  bool languageServerRunning() const;

private:
  struct Impl;
  Impl *m_Impl = NULL;
};
