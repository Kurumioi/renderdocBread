/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
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

#ifdef slots
#undef slots
#define slots_was_defined
#endif

// must be included first
#include <Python.h>

#include "3rdparty/pythoncapi_compat.h"

#ifdef slots_was_defined
#define slots
#endif

#if PYSIDE2_ENABLED
// PySide Qt integration, must be included before Qt headers
// warning C4522: 'Shiboken::AutoDecRef': multiple assignment operators specified
#pragma warning(disable : 4522)
#include <pyside.h>
#include <shiboken.h>

PyTypeObject **SbkPySide2_QtCoreTypes = NULL;
PyTypeObject **SbkPySide2_QtGuiTypes = NULL;
PyTypeObject **SbkPySide2_QtWidgetsTypes = NULL;
#else

// for non-windows, this message is displayed at CMake time.
#ifdef _MSC_VER
#pragma message( \
    "Building without PySide2 - Qt will not be accessible in python scripting. See https://github.com/baldurk/renderdoc/wiki/PySide2")
#endif

#endif

#ifdef _MSC_VER
// for the LoadLibrary call on 32-bit windows
#include <windows.h>
#endif

#include <QApplication>
#include <QDebug>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QThread>
#include <QTimer>
#include "Code/QRDUtils.h"
#include "PythonContext.h"
#include "version.h"

// exported by generated files, used to check interface compliance
bool CheckCoreInterface(rdcstr &log);
bool CheckQtInterface(rdcstr &log);

// defined in SWIG-generated renderdoc_python.cpp
extern "C" PyObject *PyInit_renderdoc(void);
extern "C" PyObject *PassObjectToPython(const char *type, void *obj);
extern "C" PyObject *PassNewObjectToPython(const char *type, void *obj);
// this one is in qrenderdoc_python.cpp
extern "C" PyObject *PyInit_qrenderdoc(void);
extern "C" PyObject *WrapBareQWidget(QWidget *);
extern "C" QWidget *UnwrapBareQWidget(PyObject *);

// little utility function to convert a PyObject * that we know is a string to a QString
static inline QString ToQStr(PyObject *value)
{
  if(value)
  {
    PyObject *repr = PyObject_Str(value);
    if(repr == NULL)
      return QString();

    PyObject *decoded = PyUnicode_AsUTF8String(repr);
    if(decoded == NULL)
      return QString();

    QString ret = QString::fromUtf8(PyBytes_AsString(decoded));

    Py_DecRef(decoded);
    Py_DecRef(repr);

    return ret;
  }

  return QString();
}

static wchar_t program_name[] = L"qrenderdoc";
static wchar_t python_home[1024] = {0};

struct OutputRedirector
{
  PyObject_HEAD;
  union
  {
    // we union with a uint64_t to ensure it's always a ulonglong even on 32-bit
    uint64_t dummy;
    PythonContext *context;
  };
  int isStdError;
  bool block;
};

static PyTypeObject OutputRedirectorType = {PyVarObject_HEAD_INIT(NULL, 0)};

static PyMethodDef OutputRedirector_methods[] = {
    {"write", NULL, METH_VARARGS, "Writes to the output window"},
    {"flush", NULL, METH_NOARGS, "Does nothing - only provided for compatibility"},
    {NULL}};

PyObject *PythonContext::main_dict = NULL;
QMap<rdcstr, PyObject *> PythonContext::extensions;
QMap<rdcstr, PyObject *> PythonContext::internalIntegrations;

static PyObject *current_global_handle = NULL;

static QMutex decrefQueueMutex;
static QList<PyObject *> decrefQueue;
extern "C" void ProcessDecRefQueue();

// helper overload to give us the semantics we want - return NULL without an exception if the attr doesn't exist
PyObject *PyObject_SafeGetAttrString(PyObject *obj, const char *string)
{
  if(PyObject_HasAttrString(obj, string) == 0)
    return NULL;

  return PyObject_GetAttrString(obj, string);
}

void FetchException(QString &typeStr, QString &valueStr, int &finalLine, QList<QString> &frames)
{
  PyObject *exObj = NULL, *valueObj = NULL, *tracebackObj = NULL;

  PyErr_Fetch(&exObj, &valueObj, &tracebackObj);

  PyErr_NormalizeException(&exObj, &valueObj, &tracebackObj);

  if(exObj && PyType_Check(exObj))
  {
    PyTypeObject *type = (PyTypeObject *)exObj;

    typeStr = QString::fromUtf8(type->tp_name);
  }
  else
  {
    typeStr = QString();
  }

  if(valueObj)
    valueStr = ToQStr(valueObj);

  if(tracebackObj)
  {
    PyObject *tracebackModule = PyImport_ImportModule("traceback");

    if(tracebackModule)
    {
      PyObject *func = PyObject_SafeGetAttrString(tracebackModule, "format_tb");

      if(func && PyCallable_Check(func))
      {
        PyObject *args = Py_BuildValue("(O)", tracebackObj);
        PyObject *formattedTB = PyObject_CallObject(func, args);

        PyTracebackObject *tb = (PyTracebackObject *)tracebackObj;

        while(tb->tb_next)
          tb = tb->tb_next;

        finalLine = tb->tb_lineno;

        if(formattedTB)
        {
          Py_ssize_t size = PyList_Size(formattedTB);
          for(Py_ssize_t i = 0; i < size; i++)
          {
            PyObject *el = PyList_GetItem(formattedTB, i);

            frames << ToQStr(el).trimmed();
          }

          Py_DecRef(formattedTB);
        }

        Py_DecRef(args);
      }
      else
      {
        qCritical() << "Couldn't get format_tb from traceback module";
      }
    }
  }

  Py_XDECREF(exObj);
  Py_XDECREF(valueObj);
  Py_XDECREF(tracebackObj);
}

void PythonContext::GlobalInit()
{
  // must happen on the UI thread
  if(qApp->thread() != QThread::currentThread())
  {
    qFatal("PythonContext::GlobalInit MUST be called from the UI thread");
    return;
  }

  // for the exception signal
  qRegisterMetaType<QList<QString>>("QList<QString>");

  // This only tests presence; ConfigFilePath performs Unicode path decoding and validation.
  // i18n-ignore: Process-owner environment key controlling isolated configuration and extensions.
  const bool isolatedConfiguration = !qgetenv("RENDERDOC_CONFIG_DIR").isEmpty();
  if(isolatedConfiguration)
    ConfigFilePath(QString());

  PyImport_AppendInittab("renderdoc", &PyInit_renderdoc);
  PyImport_AppendInittab("qrenderdoc", &PyInit_qrenderdoc);

#if PY_VERSION_HEX > 0x030B0000
  PyConfig config;
  PyConfig_InitPythonConfig(&config);
  config.configure_c_stdio = 0;
  config.parse_argv = 0;
#endif

#if defined(STATIC_QRENDERDOC)
  // add the location where our libs will be for statically-linked python installs
  {
    QDir bin = QFileInfo(QCoreApplication::applicationFilePath()).absoluteDir();

    QString pylibs = QDir::cleanPath(bin.absoluteFilePath(lit("../share/renderdoc/pylibs")));

    pylibs.toWCharArray(python_home);

#if PY_VERSION_HEX > 0x030B0000
    config.home = python_home;
#else
    Py_SetPythonHome(python_home);
#endif
  }
#endif

#if PY_VERSION_HEX > 0x030B0000
  config.program_name = program_name;

  config.use_environment = 0;
  if(isolatedConfiguration)
    config.user_site_directory = 0;

  Py_InitializeFromConfig(&config);
#else
  Py_SetProgramName(program_name);

  Py_IgnoreEnvironmentFlag = 1;
  if(isolatedConfiguration)
    Py_NoUserSiteDirectory = 1;

  Py_Initialize();
#endif

#if PY_VERSION_HEX < 0x03090000
  PyEval_InitThreads();
#endif

  OutputRedirectorType.tp_name = "renderdoc_output_redirector";
  OutputRedirectorType.tp_basicsize = sizeof(OutputRedirector);
  OutputRedirectorType.tp_flags = Py_TPFLAGS_DEFAULT;
  OutputRedirectorType.tp_doc =
      "Output redirector, to be able to catch output to stdout and stderr";
  OutputRedirectorType.tp_new = PyType_GenericNew;
  OutputRedirectorType.tp_dealloc = &PythonContext::outstream_del;
  OutputRedirectorType.tp_methods = OutputRedirector_methods;

  OutputRedirector_methods[0].ml_meth = &PythonContext::outstream_write;
  OutputRedirector_methods[1].ml_meth = &PythonContext::outstream_flush;

  PyObject *main_module = PyImport_AddModule("__main__");

  PyModule_AddObject(main_module, "renderdoc", PyImport_ImportModule("renderdoc"));
  PyModule_AddObject(main_module, "qrenderdoc", PyImport_ImportModule("qrenderdoc"));

  main_dict = PyModule_GetDict(main_module);

  // replace sys.stdout and sys.stderr with our own objects. These have a 'this' pointer of NULL,
  // which then indicates they need to forward to a global object

  // import sys
  PyDict_SetItemString(main_dict, "sys", PyImport_ImportModule("sys"));

  PyObject *rlcompleter = PyImport_ImportModule("rlcompleter");

  if(rlcompleter)
  {
    PyDict_SetItemString(main_dict, "rlcompleter", rlcompleter);
  }
  else
  {
    // ignore a failed import
    PyErr_Clear();
  }

  // try to import threading library to make debuggers happier
  if(!PyImport_ImportModule("threading"))
  {
    // ignore a failed import
    PyErr_Clear();
  }

  // sysobj = sys
  PyObject *sysobj = PyDict_GetItemString(main_dict, "sys");

  // sysobj.stdout = renderdoc_output_redirector()
  // sysobj.stderr = renderdoc_output_redirector()
  if(PyType_Ready(&OutputRedirectorType) >= 0)
  {
    // for compatibility with earlier versions of python that took a char * instead of const char *
    char noparams[1] = "";

    PyObject *redirector = PyObject_CallFunction((PyObject *)&OutputRedirectorType, noparams);
    PyObject_SetAttrString(sysobj, "stdout", redirector);
    PyObject_SetAttrString(sysobj, "_renderdoc_internal", redirector);

    OutputRedirector *output = (OutputRedirector *)redirector;
    output->isStdError = 0;
    output->context = NULL;
    output->block = false;

    redirector = PyObject_CallFunction((PyObject *)&OutputRedirectorType, noparams);
    PyObject_SetAttrString(sysobj, "stderr", redirector);

    output = (OutputRedirector *)redirector;
    output->isStdError = 1;
    output->context = NULL;
    output->block = false;
  }

// if we need to append to sys.path to locate PySide2, do that now
#if defined(PYSIDE2_SYS_PATH)
  {
    PyObject *syspath = PyObject_SafeGetAttrString(sysobj, "path");

    if(!syspath)
      qCritical() << "couldn't get sys.path";

#ifndef STRINGIZE
#define STRINGIZE2(a) #a
#define STRINGIZE(a) STRINGIZE2(a)
#endif

    PyObject *str = PyUnicode_FromString(STRINGIZE(PYSIDE2_SYS_PATH));

    PyList_Append(syspath, str);

    Py_DecRef(str);
    Py_DecRef(syspath);
  }
#endif

#if RENDERDOC_STABLE_BUILD == 0
  // if we're running in the git checkout and we can find the test scripts, add that location to the
  // path
  {
    QDir bin = QFileInfo(QCoreApplication::applicationFilePath()).absoluteDir();

    QString testpath = QDir::cleanPath(bin.absoluteFilePath(lit("../../util/test")));

    if(QDir(testpath).exists(lit("run_tests.py")))
    {
      PyObject *syspath = PyObject_SafeGetAttrString(sysobj, "path");

      if(!syspath)
        qCritical() << "couldn't get sys.path";

      PyObject *str = PyUnicode_FromString(testpath.toUtf8().data());

      PyList_Append(syspath, str);

      Py_DecRef(str);
      Py_DecRef(syspath);
    }
  }
#endif

// set up PySide
#if PYSIDE2_ENABLED
  {
// hack for win32 builds, where our pyside2 accidentally depends on Qt5Qml.dll for no good
// reason and we ship a stub to allow the dll to load instead of rebuilding the whole of pyside2
// :S
#if defined(_MSC_VER) && !defined(_M_X64)
    QString Qt5QmlStub = QApplication::applicationDirPath() + lit("/PySide2/Qt5Qml.dll");
    LoadLibraryA(Qt5QmlStub.toUtf8().data());
#endif

    Shiboken::AutoDecRef core(Shiboken::Module::import("PySide2.QtCore"));
    if(!core.isNull())
      SbkPySide2_QtCoreTypes = Shiboken::Module::getTypes(core);
    else
      qCritical() << "Failed to load PySide2.QtCore";

    Shiboken::AutoDecRef gui(Shiboken::Module::import("PySide2.QtGui"));
    if(!gui.isNull())
      SbkPySide2_QtGuiTypes = Shiboken::Module::getTypes(gui);
    else
      qCritical() << "Failed to load PySide2.QtGui";

    Shiboken::AutoDecRef widgets(Shiboken::Module::import("PySide2.QtWidgets"));
    if(!widgets.isNull())
      SbkPySide2_QtWidgetsTypes = Shiboken::Module::getTypes(widgets);
    else
      qCritical() << "Failed to load PySide2.QtWidgets";
  }
#endif

  // release GIL so that python work can now happen on any thread
  PyEval_SaveThread();
}

bool PythonContext::initialised()
{
  return main_dict != NULL;
}

PythonContext::PythonContext(QObject *parent) : QObject(parent)
{
  if(!initialised())
    return;

  // acquire the GIL and make sure this thread is init'd
  PyGILState_STATE gil = PyGILState_Ensure();

  // clone our own local context
  context_namespace = PyDict_Copy(main_dict);

  PyObject *rlcompleter = PyDict_GetItemString(main_dict, "rlcompleter");

  // for compatibility with earlier versions of python that took a char * instead of const char *
  char noparams[1] = "";

  // set global output that point to this context. It is responsible for deleting the context when
  // it goes out of scope
  PyObject *redirector = PyObject_CallFunction((PyObject *)&OutputRedirectorType, noparams);
  if(redirector)
  {
    PyDict_SetItemString(context_namespace, "_renderdoc_internal", redirector);

    OutputRedirector *output = (OutputRedirector *)redirector;
    output->context = this;
    output->block = false;
    Py_DECREF(redirector);
  }

  if(rlcompleter)
  {
    PyObject *Completer = PyObject_SafeGetAttrString(rlcompleter, "Completer");

    if(Completer)
    {
      // create a completer for our context's namespace
      m_Completer = PyObject_CallFunction(Completer, "O", context_namespace);

      if(m_Completer)
      {
        PyDict_SetItemString(context_namespace, "_renderdoc_completer", m_Completer);
      }
      else
      {
        QString typeStr;
        QString valueStr;
        int finalLine = -1;
        QList<QString> frames;
        FetchException(typeStr, valueStr, finalLine, frames);

        // failure is not fatal
        qWarning() << "Couldn't create completion object. " << typeStr << ": " << valueStr;
        PyErr_Clear();
      }
    }

    Py_DecRef(Completer);
  }
  else
  {
    m_Completer = NULL;
  }

  // release the GIL again
  PyGILState_Release(gil);

  // every 100ms while running, check for new output
  outputTicker = new QTimer(this);
  outputTicker->setInterval(100);
  QObject::connect(outputTicker, &QTimer::timeout, this, &PythonContext::outputTick);

  // we have to start it here, because we can't start on another thread.
  outputTicker->start();
}

PythonContext::~PythonContext()
{
  if(outputTicker)
  {
    outputTicker->stop();
    QObject::disconnect(outputTicker, NULL, this, NULL);
  }

  if(initialised())
  {
    PyGILState_STATE gil = PyGILState_Ensure();
    Py_XDECREF(m_Completer);
    m_Completer = NULL;
    PyGILState_Release(gil);
  }
  else
  {
    // Python may already be finalised when a context's redirector is released
    // during interpreter teardown. Never re-enter a dead interpreter.
    m_Completer = NULL;
    context_namespace = NULL;
  }

  // Do a final tick only while the integration is live. During Py_Finalize the
  // redirector can destroy this object after the interpreter fence is raised.
  if(initialised())
    outputTick();
}

bool PythonContext::CheckInterfaces(rdcstr &log)
{
  bool errors = false;

  PyGILState_STATE gil = PyGILState_Ensure();
  errors |= CheckCoreInterface(log);
  errors |= CheckQtInterface(log);

  for(rdcstr module_name : {"renderdoc", "qrenderdoc"})
  {
    PyObject *mod = PyImport_ImportModule(module_name.c_str());
    PyObject *dict = PyModule_GetDict(mod);

    PyObject *key, *value;
    Py_ssize_t pos = 0;

    while(PyDict_Next(dict, &pos, &key, &value))
    {
      rdcstr name = ToQStr(key);

      if(name.beginsWith("__"))
        continue;

      if(!PyCallable_Check(value))
      {
        log += "Non-callable object found: " + module_name + "." + name +
               ". Expected only classes and functions.\n";
        errors = true;
      }
    }

    Py_DECREF(mod);
  }

  PyGILState_Release(gil);

  log.trim();
  return errors;
}

void PythonContext::Finish()
{
  if(!initialised())
  {
    context_namespace = NULL;
    return;
  }

  PyGILState_STATE gil = PyGILState_Ensure();

  // release our external handle to globals. It'll now only be ref'd from inside
  // Clearing the member first is essential: its redirector dealloc can delete
  // this PythonContext synchronously on the UI thread.
  PyObject *context = context_namespace;
  context_namespace = NULL;
  Py_XDECREF(context);

  PyGILState_Release(gil);
}

void PythonContext::PausePythonThreading()
{
  m_SavedThread = PyEval_SaveThread();
}

void PythonContext::ResumePythonThreading()
{
  PyEval_RestoreThread((PyThreadState *)m_SavedThread);
  m_SavedThread = NULL;
}

static void RemoveInsertedPythonSearchPath(PyObject *syspath, PyObject *path)
{
  if(!syspath || !PyList_Check(syspath) || !path)
    return;

  // Remove only the exact object inserted by LoadInternalIntegration. An equal path may have
  // existed before the integration loaded and must remain in the caller's sys.path.
  for(Py_ssize_t i = 0; i < PyList_Size(syspath); i++)
  {
    if(PyList_GetItem(syspath, i) == path)
    {
      PySequence_DelItem(syspath, i);
      return;
    }
  }
}

static bool PythonModuleIsWithinPackage(PyObject *module, const QString &canonicalPackage,
                                        Qt::CaseSensitivity pathCase)
{
  if(!module)
    return false;

  PyObject *moduleFile = PyObject_SafeGetAttrString(module, "__file__");
  if(!moduleFile)
  {
    PyErr_Clear();
    return false;
  }

  const QString canonicalFile = QFileInfo(ToQStr(moduleFile)).canonicalFilePath();
  Py_DECREF(moduleFile);
  return !canonicalFile.isEmpty() &&
         canonicalFile.startsWith(canonicalPackage + QLatin1Char('/'), pathCase);
}

static bool ValidateInternalIntegrationModules(const QString &moduleName,
                                               const QString &canonicalPackage,
                                               Qt::CaseSensitivity pathCase)
{
  PyObject *modules = PyImport_GetModuleDict();    // borrowed
  PyObject *keys = PyDict_Keys(modules);
  if(!keys)
  {
    PyErr_Clear();
    return false;
  }

  const QString childPrefix = moduleName + QLatin1Char('.');
  bool valid = true;
  for(Py_ssize_t i = 0; i < PyList_Size(keys); i++)
  {
    PyObject *key = PyList_GetItem(keys, i);    // borrowed
    if(!PyUnicode_Check(key))
      continue;

    const QString name = ToQStr(key);
    if(name != moduleName && !name.startsWith(childPrefix))
      continue;

    if(!PythonModuleIsWithinPackage(PyDict_GetItem(modules, key), canonicalPackage, pathCase))
    {
      valid = false;
      break;
    }
  }

  Py_DECREF(keys);
  PyErr_Clear();
  return valid;
}

static void RemoveFailedInternalIntegrationModules(const QString &moduleName,
                                                   const QString &canonicalPackage,
                                                   Qt::CaseSensitivity pathCase,
                                                   PyObject *rootModule)
{
  PyObject *modules = PyImport_GetModuleDict();    // borrowed
  PyObject *keys = PyDict_Keys(modules);
  if(!keys)
  {
    PyErr_Clear();
    return;
  }

  const QString childPrefix = moduleName + QLatin1Char('.');
  for(Py_ssize_t i = 0; i < PyList_Size(keys); i++)
  {
    PyObject *key = PyList_GetItem(keys, i);    // borrowed
    if(!PyUnicode_Check(key))
      continue;

    const QString name = ToQStr(key);
    PyObject *candidate = PyDict_GetItem(modules, key);    // borrowed
    const bool isRoot = name == moduleName && candidate == rootModule;
    const bool isOwnedChild = name.startsWith(childPrefix) &&
                              PythonModuleIsWithinPackage(candidate, canonicalPackage, pathCase);
    if((isRoot || isOwnedChild) && PyDict_DelItem(modules, key) != 0)
      PyErr_Clear();
  }

  Py_DECREF(keys);
  PyErr_Clear();
}

QString PythonContext::LoadInternalIntegration(ICaptureContext &ctx, const rdcstr &integration,
                                               const QString &packageRoot)
{
  if(!initialised())
    return tr("Python is not initialised; the built-in integration was not loaded.");

  if(qApp->thread() != QThread::currentThread())
    return tr("Built-in integrations must be loaded on the UI thread.");

  const QString moduleName = QString(integration);
  // i18n-ignore: validation regex for a Python module identifier
  static const QRegularExpression modulePattern(lit("^[A-Za-z_][A-Za-z0-9_]*$"));
  if(!modulePattern.match(moduleName).hasMatch())
    return tr("The built-in integration module name is invalid: %1").arg(moduleName);

  const QString applicationRoot = QDir(QCoreApplication::applicationDirPath()).canonicalPath();
  const QString canonicalRoot = QDir(packageRoot).canonicalPath();
  const QFileInfo packageEntry(QDir(canonicalRoot).filePath(moduleName));
  const QString canonicalPackage = QDir(packageEntry.filePath()).canonicalPath();
  // i18n-ignore: fixed Python package entry-point filename
  QFileInfo initFile(QDir(canonicalPackage).filePath(lit("__init__.py")));

#if defined(Q_OS_WIN)
  const Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
  const Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif

  if(applicationRoot.isEmpty() || canonicalRoot.isEmpty() || canonicalPackage.isEmpty() ||
     packageEntry.isSymLink() || !initFile.isFile() || initFile.isSymLink() ||
     QFileInfo(canonicalPackage).dir().canonicalPath().compare(canonicalRoot, pathCase) != 0 ||
     (canonicalRoot.compare(applicationRoot, pathCase) != 0 &&
      !canonicalRoot.startsWith(applicationRoot + QLatin1Char('/'), pathCase)))
  {
    return tr("The built-in integration package is missing or outside the application directory: "
              "%1")
        .arg(QDir::toNativeSeparators(packageRoot));
  }

  if(internalIntegrations.contains(integration))
    return QString();

  PyGILState_STATE gil = PyGILState_Ensure();
  QString ret;
  PyObject *sysobj = PyDict_GetItemString(main_dict, "sys");
  PyObject *syspath = PyObject_SafeGetAttrString(sysobj, "path");
  PyObject *pathObject = NULL;
  PyObject *module = NULL;
  PyObject *startFunction = NULL;
  PyObject *pyctx = NULL;
  PyObject *retval = NULL;
  PyObject *moduleFile = NULL;
  bool startInvoked = false;

  if(!syspath || !PyList_Check(syspath))
  {
    ret = tr("Couldn't get Python sys.path; the built-in integration was not loaded.");
    goto cleanup;
  }

  {
    const QByteArray rootBytes = canonicalRoot.toUtf8();
    pathObject = PyUnicode_FromStringAndSize(rootBytes.constData(), rootBytes.size());
  }
  if(!pathObject || PyList_Insert(syspath, 0, pathObject) != 0)
  {
    ret = tr("Couldn't add the built-in integration directory to Python's search path.");
    goto cleanup;
  }

  current_global_handle = PyObject_SafeGetAttrString(sysobj, "_renderdoc_internal");
  if(!current_global_handle)
  {
    ret = tr("Couldn't access qrenderdoc's internal Python handle.");
    goto cleanup;
  }

  module = PyImport_ImportModule(integration.c_str());
  if(!module)
    goto python_error;

  moduleFile = PyObject_SafeGetAttrString(module, "__file__");
  if(!moduleFile ||
     QFileInfo(ToQStr(moduleFile)).canonicalFilePath().compare(initFile.canonicalFilePath(),
                                                               pathCase) != 0)
  {
    ret = tr("Python imported the built-in integration from an unexpected location.");
    goto cleanup;
  }

  // Relative imports are also import-cache lookups. Validate every loaded package child before
  // start() can use it so a pre-populated sys.modules entry cannot shadow first-party code.
  if(!ValidateInternalIntegrationModules(moduleName, canonicalPackage, pathCase))
  {
    ret = tr("Python imported the built-in integration from an unexpected location.");
    goto cleanup;
  }

  if(PyModule_AddObject(module, "_renderdoc_internal", current_global_handle) != 0)
  {
    ret = tr("Couldn't attach qrenderdoc's internal Python handle to the built-in integration.");
    goto cleanup;
  }
  current_global_handle = NULL;

  startFunction = PyObject_SafeGetAttrString(module, "start");
  if(!startFunction || !PyCallable_Check(startFunction))
  {
    ret = tr("The built-in integration does not provide a callable start() entry point.");
    goto cleanup;
  }

  pyctx = PassObjectToPython((rdcstr(TypeName<ICaptureContext>()) + " *").c_str(), &ctx);
  if(!pyctx)
  {
    ret = tr("Couldn't pass qrenderdoc's capture context to the built-in integration.");
    goto cleanup;
  }

  startInvoked = true;
  retval = PyObject_CallFunction(startFunction, "sO", MAJOR_MINOR_VERSION_STRING, pyctx);
  if(!retval)
    goto python_error;

  if(PyModule_AddObject(module, "pyrenderdoc", pyctx) != 0)
  {
    ret = tr("Couldn't attach the capture context to the built-in integration.");
    goto cleanup;
  }
  pyctx = NULL;

  internalIntegrations[integration] = module;
  module = NULL;
  goto cleanup;

python_error:
  {
    QString typeStr;
    QString valueStr;
    int finalLine = -1;
    QList<QString> frames;
    FetchException(typeStr, valueStr, finalLine, frames);
    ret = tr("The built-in integration failed to start: %1: %2").arg(typeStr, valueStr);
  }

cleanup:
  if(module)
  {
    if(startInvoked)
    {
      // start() may have installed capture viewers, timers, panels, or a sidecar owner before
      // raising. Roll all of that back while the capture context and MiniQt helper are still
      // alive. Keep the original startup error as the result; rollback failures are diagnostic
      // only.
      PyErr_Clear();
      PyObject *stopFunction = PyObject_SafeGetAttrString(module, "stop");
      if(stopFunction && PyCallable_Check(stopFunction))
      {
        PyObject *stopResult = PyObject_CallFunction(stopFunction, "");
        if(!stopResult)
        {
          QString typeStr;
          QString valueStr;
          int finalLine = -1;
          QList<QString> frames;
          FetchException(typeStr, valueStr, finalLine, frames);
          qCritical() << "Built-in integration" << QString(integration)
                      << "startup rollback failed:" << typeStr << ":" << valueStr;
        }
        Py_XDECREF(stopResult);
      }
      Py_XDECREF(stopFunction);
    }

    // A failed package must not remain import-cached. Remove the exact root and only child modules
    // whose canonical files belong to this fixed package. Unrelated same-prefix modules remain in
    // place and will make a later retry fail the validation above instead of being executed.
    RemoveFailedInternalIntegrationModules(moduleName, canonicalPackage, pathCase, module);
  }
  RemoveInsertedPythonSearchPath(syspath, pathObject);
  Py_XDECREF(moduleFile);
  Py_XDECREF(retval);
  Py_XDECREF(pyctx);
  Py_XDECREF(startFunction);
  Py_XDECREF(module);
  Py_XDECREF(pathObject);
  Py_XDECREF(syspath);
  Py_XDECREF(current_global_handle);
  current_global_handle = NULL;
  ProcessDecRefQueue();
  PyErr_Clear();
  PyGILState_Release(gil);
  return ret;
}

QString PythonContext::InvokeInternalIntegration(const rdcstr &integration, const char *function,
                                                 const QStringList &arguments, QString *error)
{
  if(error)
    error->clear();
  if(!initialised())
  {
    if(error)
      *error = tr("Python is not initialised.");
    return QString();
  }

  if(qApp->thread() != QThread::currentThread())
  {
    if(error)
      *error = tr("Built-in integration calls must run on the UI thread.");
    return QString();
  }

  PyGILState_STATE gil = PyGILState_Ensure();
  QString result;
  PyObject *module = internalIntegrations.value(integration, NULL);
  PyObject *callable = NULL;
  PyObject *tuple = NULL;
  PyObject *retval = NULL;

  if(!module)
  {
    if(error)
      *error = tr("The built-in integration is not loaded.");
    goto cleanup;
  }

  callable = PyObject_SafeGetAttrString(module, function);
  if(!callable || !PyCallable_Check(callable))
  {
    if(error)
      *error = tr("The built-in integration entry point is unavailable.");
    goto cleanup;
  }

  tuple = PyTuple_New(arguments.count());
  if(!tuple)
    goto python_error;
  for(int i = 0; i < arguments.count(); i++)
  {
    const QByteArray encoded = arguments[i].toUtf8();
    PyObject *value = PyUnicode_FromStringAndSize(encoded.constData(), encoded.size());
    if(!value)
      goto python_error;
    PyTuple_SET_ITEM(tuple, i, value);
  }

  retval = PyObject_CallObject(callable, tuple);
  if(!retval)
    goto python_error;
  if(!PyUnicode_Check(retval))
  {
    if(error)
      *error = tr("The built-in integration returned an invalid result.");
    goto cleanup;
  }

  {
    Py_ssize_t size = 0;
    const char *utf8 = PyUnicode_AsUTF8AndSize(retval, &size);
    if(!utf8)
      goto python_error;
    result = QString::fromUtf8(utf8, int(size));
  }
  goto cleanup;

python_error:
  {
    QString typeStr;
    QString valueStr;
    int finalLine = -1;
    QList<QString> frames;
    FetchException(typeStr, valueStr, finalLine, frames);
    if(error)
      *error = tr("The built-in integration call failed: %1: %2").arg(typeStr, valueStr);
  }

cleanup:
  Py_XDECREF(retval);
  Py_XDECREF(tuple);
  Py_XDECREF(callable);
  ProcessDecRefQueue();
  PyErr_Clear();
  PyGILState_Release(gil);
  return result;
}

bool PythonContext::ShutdownInternalIntegrations()
{
  if(!initialised() || internalIntegrations.isEmpty())
    return true;

  if(qApp->thread() != QThread::currentThread())
  {
    qFatal("PythonContext::ShutdownInternalIntegrations MUST be called from the UI thread");
    return false;
  }

  PyGILState_STATE gil = PyGILState_Ensure();
  bool allStopped = true;
  const QList<rdcstr> names = internalIntegrations.keys();
  for(const rdcstr &name : names)
  {
    PyObject *integration = internalIntegrations.value(name, NULL);
    PyObject *stopFunction = PyObject_SafeGetAttrString(integration, "stop");
    bool stopped = false;
    if(stopFunction && PyCallable_Check(stopFunction))
    {
      PyObject *retval = PyObject_CallFunction(stopFunction, "");
      if(!retval)
      {
        QString typeStr;
        QString valueStr;
        int finalLine = -1;
        QList<QString> frames;
        FetchException(typeStr, valueStr, finalLine, frames);
        qCritical() << "Built-in integration" << QString(name) << "stop() failed:" << typeStr
                    << ":" << valueStr;
      }
      else
      {
        stopped = true;
      }
      Py_XDECREF(retval);
    }
    else
    {
      qWarning() << "Built-in integration" << QString(name)
                 << "has no callable stop() entry point";
    }
    Py_XDECREF(stopFunction);

    if(stopped)
    {
      internalIntegrations.remove(name);
      Py_XDECREF(integration);
    }
    else
    {
      // Keep the module alive so the CaptureContext owner can retry while all native dependencies
      // are still valid. The owner must not dismantle CaptureContext/MiniQt/replay underneath a
      // live Python QRunnable.
      allStopped = false;
    }
  }

  ProcessDecRefQueue();
  PyErr_Clear();
  PyGILState_Release(gil);
  return allStopped;
}

void PythonContext::ShutdownExtensions()
{
  if(!initialised() || extensions.isEmpty())
    return;

  // Extension unregister callbacks manipulate qrenderdoc widgets and capture
  // viewers, so they must run while CaptureContext and MiniQtHelper are alive.
  if(qApp->thread() != QThread::currentThread())
  {
    qFatal("PythonContext::ShutdownExtensions MUST be called from the UI thread");
    return;
  }

  PyGILState_STATE gil = PyGILState_Ensure();

  const QList<rdcstr> names = extensions.keys();
  for(const rdcstr &name : names)
  {
    PyObject *extension = extensions.value(name, NULL);
    if(!extension)
      continue;

    PyObject *unregister_func = PyObject_SafeGetAttrString(extension, "unregister");
    if(unregister_func && PyCallable_Check(unregister_func))
    {
      PyObject *retval = PyObject_CallFunction(unregister_func, "");
      if(retval == NULL)
      {
        QString typeStr;
        QString valueStr;
        int finalLine = -1;
        QList<QString> frames;
        FetchException(typeStr, valueStr, finalLine, frames);
        qCritical() << "Extension" << QString(name) << "unregister() failed:" << typeStr << ":"
                    << valueStr;
        PyErr_Clear();
      }
      Py_XDECREF(retval);
    }
    else if(unregister_func)
    {
      qWarning() << "Extension" << QString(name) << "has a non-callable unregister attribute";
    }

    Py_XDECREF(unregister_func);
  }

  // The map owns the new references returned by ImportModule/ReloadModule.
  // sys.modules keeps imported modules alive until normal interpreter teardown.
  for(PyObject *extension : extensions)
    Py_XDECREF(extension);
  extensions.clear();

  ProcessDecRefQueue();
  PyErr_Clear();
  PyGILState_Release(gil);
}

void PythonContext::GlobalShutdown()
{
  if(!initialised())
    return;

  // must happen on the UI thread
  if(qApp->thread() != QThread::currentThread())
  {
    qFatal("PythonContext::GlobalShutdown MUST be called from the UI thread");
    return;
  }

  // CaptureContext normally performs this while integration/extension-facing UI objects are still
  // valid. Keep a final idempotent fallback for early-exit paths.
  const bool internalIntegrationsStopped = ShutdownInternalIntegrations();
  if(!internalIntegrationsStopped || !internalIntegrations.isEmpty())
  {
    qCritical() << "Python finalisation skipped because a built-in integration still owns live "
                   "workers";
    return;
  }

  // Do not release extension/native callback dependencies until every first-party integration
  // has proven quiescent. CaptureContext normally performed both steps before reaching here; this
  // remains an idempotent fallback for early-exit paths without a CaptureContext.
  ShutdownExtensions();

  // Acquire the GIL and drain deferred SWIG references before raising the
  // shutdown fence. Late Qt/PythonContext destructors see main_dict == NULL
  // and must not re-enter Python while Py_Finalize releases module globals.
  PyGILState_Ensure();
  ProcessDecRefQueue();
  internalIntegrations.clear();
  extensions.clear();
  current_global_handle = NULL;
  main_dict = NULL;

  Py_Finalize();
}

static QStringList NormaliseExtensionSearchPaths(const QStringList &candidates)
{
  QStringList ret;
#if defined(Q_OS_WIN)
  const Qt::CaseSensitivity pathCase = Qt::CaseInsensitive;
#else
  const Qt::CaseSensitivity pathCase = Qt::CaseSensitive;
#endif

  for(const QString &candidate : candidates)
  {
    const QString path = QDir::cleanPath(QDir(candidate).absolutePath());
    bool duplicate = false;
    for(const QString &existing : ret)
      duplicate |= existing.compare(path, pathCase) == 0;
    if(!duplicate)
      ret.append(path);
  }

  return ret;
}

QStringList PythonContext::GetApplicationExtensionsPaths()
{
  QStringList ret;

  // Optional application extensions are kept for RenderDoc's public extension system. First-party
  // integrations such as ReplayLab use LoadInternalIntegration() and never reach this search path.
  QDir bundled(QCoreApplication::applicationDirPath());
  if(bundled.cd(lit("extensions")) && bundled.exists())
    ret.append(bundled.absolutePath());

  // First-party integrations retain their fixed application path. An isolated process must not
  // discover personal extensions via QStandardPaths, even when none are configured for autoload.
  // i18n-ignore: Process-owner environment key, not user-visible UI text.
  if(!qgetenv("RENDERDOC_CONFIG_DIR").isEmpty())
  {
    // i18n-ignore: Fixed optional-extension directory underneath the isolated configuration root.
    QDir isolated(ConfigFilePath(lit("extensions")));
    if(isolated.exists() && !ret.contains(isolated.absolutePath()))
      ret.append(isolated.absolutePath());
    return NormaliseExtensionSearchPaths(ret);
  }

  for(QString d : QStandardPaths::standardLocations(QStandardPaths::AppDataLocation))
  {
    QDir dir(d);

    // i18n-ignore: Fixed extension search subdirectory, not UI text.
    if(dir.cd(lit("extensions")) && dir.exists() && !ret.contains(dir.absolutePath()))
      ret.append(dir.absolutePath());
  }

  return NormaliseExtensionSearchPaths(ret);
}

void PythonContext::ProcessExtensionWork(std::function<void()> callback)
{
  PyGILState_STATE gil = PyGILState_Ensure();

  callback();

  PyGILState_Release(gil);
}

QString PythonContext::LoadExtension(ICaptureContext &ctx, const rdcstr &extension)
{
  QString ret;

  PyObject *sysobj = PyDict_GetItemString(main_dict, "sys");

  PyObject *syspath = PyObject_SafeGetAttrString(sysobj, "path");

  if(!syspath || !PyList_Check(syspath))
  {
    qCritical() << "couldn't get a list for sys.path";
    Py_XDECREF(syspath);
    return tr("Couldn't get a list for Python sys.path; extension was not loaded.\n");
  }

  // Put the application-bundled extensions directory before the per-user
  // directory and any interpreter paths. Remove exact duplicates first, then
  // insert in reverse order so the ordering returned above is preserved. Keep
  // these roots prioritised for extension submodule imports and later reloads.
  const QStringList extensionPaths = PythonContext::GetApplicationExtensionsPaths();
  for(int pathIndex = extensionPaths.count() - 1; pathIndex >= 0; pathIndex--)
  {
    QDir dir(extensionPaths[pathIndex]);

    if(dir.exists())
    {
      const QByteArray path = dir.absolutePath().toUtf8();
      PyObject *str = PyUnicode_FromString(path.constData());
      if(!str)
      {
        PyErr_Clear();
        continue;
      }
      for(Py_ssize_t i = PyList_Size(syspath) - 1; i >= 0; i--)
      {
        const int equal = PyObject_RichCompareBool(PyList_GetItem(syspath, i), str, Py_EQ);
        if(equal == 1)
          PySequence_DelItem(syspath, i);
        else if(equal < 0)
          PyErr_Clear();
      }
      PyList_Insert(syspath, 0, str);
      Py_DecRef(str);
    }
  }

  PyObject *ext = NULL;

  current_global_handle = PyObject_SafeGetAttrString(sysobj, "_renderdoc_internal");

  if(!current_global_handle)
    qCritical() << "couldn't get _renderdoc_internal";

  QString typeStr;
  QString valueStr;
  int finalLine = -1;
  QList<QString> frames;

  if(extensions[extension] == NULL)
  {
    qInfo() << "First load of " << QString(extension);
    ext = PyImport_ImportModule(extension.c_str());
  }
  else
  {
    qInfo() << "Reloading " << QString(extension);

    // call unregister() if it exists
    PyObject *unregister_func = PyObject_SafeGetAttrString(extensions[extension], "unregister");

    bool reloadSuccess = true;

    if(unregister_func)
    {
      PyObject *retval = PyObject_CallFunction(unregister_func, "");

      if(retval == NULL)
      {
        FetchException(typeStr, valueStr, finalLine, frames);
        reloadSuccess = false;
      }

      // discard the return value, regardless of error we don't abort the reload
      Py_XDECREF(retval);
    }
    Py_XDECREF(unregister_func);

    if(reloadSuccess)
    {
      // if the extension is a package, we need to manually reload any loaded submodules
      PyObject *sysmodules = PyObject_SafeGetAttrString(sysobj, "modules");

      if(!syspath)
        qCritical() << "couldn't get sys.modules";

      PyObject *keys = PyDict_Keys(sysmodules);

      QString search = extension + lit(".");

      if(keys)
      {
        Py_ssize_t len = PyList_Size(keys);
        for(Py_ssize_t i = 0; i < len; i++)
        {
          PyObject *key = PyList_GetItem(keys, i);
          PyObject *value = PyDict_GetItem(sysmodules, key);

          QString keystr = ToQStr(key);

          if(keystr.contains(search))
          {
            qInfo() << "Reloading submodule " << keystr;

            PyObject *mod = PyImport_ReloadModule(value);

            if(mod == NULL)
            {
              qCritical() << "Failed to reload " << keystr;
              ret += tr("Failed to reload submodule '%1'\n").arg(keystr);
              reloadSuccess = false;
              break;
            }

            // we don't need the reference, we just wanted to reload it
            Py_DECREF(mod);

            value = PyDict_GetItem(sysmodules, key);

            if(value != mod)
              qCritical() << "sys.modules[" << keystr << "]"
                          << " after reload doesn't match reloaded object";
          }
        }

        Py_DECREF(keys);
      }
    }

    if(reloadSuccess)
      ext = PyImport_ReloadModule(extensions[extension]);
  }

  // if import succeeded, store this extension module in our map. If import failed, we might have
  // failed a reimport in which case the original module is still there and valid, so don't
  // overwrite the value.
  if(ext)
  {
    PyObject *previous = extensions.value(extension, NULL);
    extensions[extension] = ext;
    // ImportModule/ReloadModule returns a new reference. The map owns exactly
    // one such reference for each package, including when reload returns the
    // same module pointer.
    Py_XDECREF(previous);

    if(PyModule_AddObject(ext, "_renderdoc_internal", current_global_handle) != 0)
    {
      qCritical() << "Couldn't set _renderdoc_internal in loaded extension";
      Py_XDECREF(current_global_handle);
    }
    current_global_handle = NULL;
  }

  if(ext)
  {
    // if import succeeded, call register()
    PyObject *register_func = PyObject_SafeGetAttrString(ext, "register");

    if(register_func)
    {
      PyObject *pyctx =
          PassObjectToPython((rdcstr(TypeName<ICaptureContext>()) + " *").c_str(), &ctx);

      PyObject *retval = NULL;
      if(pyctx)
      {
        retval = PyObject_CallFunction(register_func, "sO", MAJOR_MINOR_VERSION_STRING, pyctx);
      }
      else
      {
        qCritical() << "Internal error passing pyrenderdoc to extension register()";
        ret += tr("Internal error passing pyrenderdoc to extension register()\n");
      }

      if(retval == NULL)
      {
        qCritical() << "register() function failed";
        ret += tr("register() function failed\n");
        ext = NULL;
      }

      Py_XDECREF(retval);

      if(ext)
      {
        int pyret = PyModule_AddObject(ext, "pyrenderdoc", pyctx);

        if(pyret != 0)
        {
          qCritical() << "Couldn't set pyrenderdoc global in loaded module";
          ret += tr("Couldn't set pyrenderdoc global in loaded module\n");
          ext = NULL;
        }
        else
        {
          // PyModule_AddObject steals this new SWIG reference on success.
          // register() may also retain the wrapper, so a second DECREF here
          // would leave the module dictionary holding a dangling PyObject.
          pyctx = NULL;
        }
      }

      Py_XDECREF(pyctx);
    }
    else
    {
      qCritical() << "register() function not found in extension";
      ret += tr("register() function not found in extension\n");
      ext = NULL;
    }
    Py_XDECREF(register_func);
  }
  else
  {
    ext = NULL;
  }

  if(!ext)
  {
    if(typeStr.isEmpty())
      FetchException(typeStr, valueStr, finalLine, frames);

    ret = ret.trimmed();

    ret += lit("\n");

    if(!valueStr.isEmpty())
    {
      qCritical("Error importing extension module. %s: %s", typeStr.toUtf8().data(),
                valueStr.toUtf8().data());
      ret += tr("Error importing extension module. %1: %2\n\n").arg(typeStr).arg(valueStr);

      if(!frames.isEmpty())
      {
        qCritical() << "Traceback (most recent call last):";
        ret += tr("Traceback (most recent call last):\n");
        for(const QString &f : frames)
        {
          QStringList lines = f.split(QLatin1Char('\n'));
          for(const QString &line : lines)
          {
            qCritical("  %s", line.toUtf8().data());
            ret += line + lit("\n");
          }
        }
      }
    }
  }

  Py_DecRef(syspath);

  Py_XDECREF(current_global_handle);
  current_global_handle = NULL;

  return ret;
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Extension search paths retain bundled-first order and remove duplicates",
          "[qrenderdoc][python][extensions]")
{
  QStringList candidates = {lit("C:/ReplayLab/extensions"), lit("C:/User/extensions"),
                            lit("C:/ReplayLab/extensions")};
  const QStringList paths = NormaliseExtensionSearchPaths(candidates);

  REQUIRE(paths.count() == 2);
  CHECK(bool(paths[0] == QDir::cleanPath(QDir(candidates[0]).absolutePath())));
  CHECK(bool(paths[1] == QDir::cleanPath(QDir(candidates[1]).absolutePath())));
}

#endif

void PythonContext::ConvertPyArgs(const ExtensionCallbackData &data,
                                  rdcarray<rdcpair<rdcstr, PyObject *>> &args)
{
  PyGILState_STATE gil = PyGILState_Ensure();

  args.resize(data.size());
  for(size_t i = 0; i < data.size(); i++)
  {
    rdcpair<rdcstr, PyObject *> &a = args[i];
    a.first = data[i].first;

    // convert QVariant to python object
    const QVariant &in = data[i].second;
    PyObject *&out = a.second;

    // coverity[mixed_enums]
    QMetaType::Type type = (QMetaType::Type)in.type();
    switch(type)
    {
      case QMetaType::Bool: out = PyBool_FromLong(in.toBool()); break;
      case QMetaType::Short:
      case QMetaType::Long:
      case QMetaType::Int: out = PyLong_FromLong(in.toInt()); break;
      case QMetaType::UShort:
      case QMetaType::ULong:
      case QMetaType::UInt: out = PyLong_FromUnsignedLong(in.toUInt()); break;
      case QMetaType::LongLong: out = PyLong_FromLongLong(in.toLongLong()); break;
      case QMetaType::ULongLong: out = PyLong_FromUnsignedLongLong(in.toULongLong()); break;
      case QMetaType::Float: out = PyFloat_FromDouble(in.toFloat()); break;
      case QMetaType::Double: out = PyFloat_FromDouble(in.toDouble()); break;
      case QMetaType::QString: out = PyUnicode_FromString(in.toString().toUtf8().data()); break;
      default: break;
    }

    if(!out)
    {
      // try various other types
      if(in.userType() == qMetaTypeId<ResourceId>())
        out = PassNewObjectToPython("ResourceId *", new ResourceId(in.value<ResourceId>()));
    }

    if(!out)
    {
      qCritical() << "Couldn't convert" << in << "to python object";
      out = Py_XNewRef(Py_None);
    }
  }

  PyGILState_Release(gil);
}

void PythonContext::FreePyArgs(rdcarray<rdcpair<rdcstr, PyObject *>> &args)
{
  PyGILState_STATE gil = PyGILState_Ensure();

  for(rdcpair<rdcstr, PyObject *> &a : args)
  {
    Py_XDECREF(a.second);
  }

  PyGILState_Release(gil);
}

QString PythonContext::versionString()
{
  return QFormatStr("%1.%2.%3").arg(PY_MAJOR_VERSION).arg(PY_MINOR_VERSION).arg(PY_MICRO_VERSION);
}

void PythonContext::executeString(const QString &filename, const QString &source)
{
  if(!initialised())
  {
    emit exception(lit("SystemError"), tr("Python integration failed to initialise."), -1, {});
    return;
  }

  location.file = filename;
  location.line = 1;

  PyGILState_STATE gil = PyGILState_Ensure();

  PyObject *compiled =
      Py_CompileString(source.toUtf8().data(), filename.toUtf8().data(),
                       source.count(QLatin1Char('\n')) == 0 ? Py_single_input : Py_file_input);

  PyObject *ret = NULL;

  if(compiled)
  {
    PyObject *traceContext = PyDict_New();

    uintptr_t thisint = (uintptr_t)this;
    uint64_t thisuint64 = (uint64_t)thisint;
    PyObject *thisobj = PyLong_FromUnsignedLongLong(thisuint64);

    PyDict_SetItemString(traceContext, "thisobj", thisobj);
    PyDict_SetItemString(traceContext, "compiled", compiled);

    PyEval_SetTrace(&PythonContext::traceEvent, traceContext);

    m_Abort = false;

    m_State = PyGILState_GetThisThreadState();

    ret = PyEval_EvalCode(compiled, context_namespace, context_namespace);

    m_State = NULL;

    // catch any output
    outputTick();

    PyEval_SetTrace(NULL, NULL);

    ProcessDecRefQueue();

    Py_XDECREF(thisobj);
    Py_XDECREF(traceContext);
  }

  Py_DecRef(compiled);

  QString typeStr;
  QString valueStr;
  int finalLine = -1;
  QList<QString> frames;
  bool caughtException = (ret == NULL);

  if(caughtException)
    FetchException(typeStr, valueStr, finalLine, frames);

  Py_XDECREF(ret);

  PyGILState_Release(gil);

  if(caughtException)
    emit exception(typeStr, valueStr, finalLine, frames);
}

void PythonContext::executeString(const QString &source)
{
  executeString(lit("<interactive.py>"), source);
}

void PythonContext::executeFile(const QString &filename)
{
  QFile f(filename);

  if(!f.exists())
  {
    emit exception(lit("FileNotFoundError"), tr("No such file or directory: %1").arg(filename), -1,
                   {});
    return;
  }

  if(f.open(QIODevice::ReadOnly | QIODevice::Text))
  {
    QByteArray py = f.readAll();

    executeString(filename, QString::fromUtf8(py));
  }
  else
  {
    emit exception(lit("IOError"), QFormatStr("%1: %2").arg(f.errorString()).arg(filename), -1, {});
  }
}

void PythonContext::setGlobal(const char *varName, const char *typeName, void *object)
{
  if(!initialised())
  {
    emit exception(lit("SystemError"), tr("Python integration failed to initialise."), -1, {});
    return;
  }

  PyGILState_STATE gil = PyGILState_Ensure();

  // we don't need separate functions for each module, as they share type info
  PyObject *obj = PassObjectToPython(typeName, object);

  int ret = -1;

  if(obj)
    ret = PyDict_SetItemString(context_namespace, varName, obj);

  PyGILState_Release(gil);

  if(ret != 0)
  {
    emit exception(lit("RuntimeError"),
                   tr("Failed to set variable '%1' of type '%2'")
                       .arg(QString::fromUtf8(varName))
                       .arg(QString::fromUtf8(typeName)),
                   -1, {});
    return;
  }

  setPyGlobal(varName, obj);
}

template <>
void PythonContext::setGlobal(const char *varName, PyObject *object)
{
  setPyGlobal(varName, object);
}

template <>
void PythonContext::setGlobal(const char *varName, QObject *object)
{
  setQtGlobal(varName, object);
}

template <>
void PythonContext::setGlobal(const char *varName, QWidget *object)
{
  setQtGlobal(varName, object);
}

QWidget *PythonContext::QWidgetFromPy(PyObject *widget)
{
#if PYSIDE2_ENABLED
  if(!initialised())
    return NULL;

  if(Py_IsNone(widget) || widget == NULL)
    return NULL;

  if(!SbkPySide2_QtCoreTypes || !SbkPySide2_QtGuiTypes || !SbkPySide2_QtWidgetsTypes)
    return UnwrapBareQWidget(widget);

  if(!Shiboken::Object::checkType(widget))
    return UnwrapBareQWidget(widget);

  return (QWidget *)Shiboken::Object::cppPointer((SbkObject *)widget, Shiboken::SbkType<QWidget>());
#else
  return UnwrapBareQWidget(widget);
#endif
}

QStringList PythonContext::completionOptions(QString base)
{
  QStringList ret;

  if(!m_Completer)
    return ret;

  QByteArray bytes = base.toUtf8();
  const char *input = (const char *)bytes.data();

  PyGILState_STATE gil = PyGILState_Ensure();

  PyObject *completeFunction = PyObject_SafeGetAttrString(m_Completer, "complete");

  if(!completeFunction)
    return ret;

  int idx = 0;
  PyObject *opt = NULL;
  do
  {
    opt = PyObject_CallFunction(completeFunction, "si", input, idx);

    if(opt && !Py_IsNone(opt))
    {
      QString optstr = ToQStr(opt);

      bool add = true;

      // little hack, remove some of the ugly swig template instantiations that we can't avoid.
      if(optstr.contains(lit("renderdoc.rdcarray")) || optstr.contains(lit("renderdoc.rdcstr")) ||
         optstr.contains(lit("renderdoc.bytebuf")))
        add = false;

      if(add)
        ret << optstr;
    }

    idx++;
  } while(opt && !Py_IsNone(opt));

  // extra hack, remove the swig object functions/data but ONLY if we find a sure-fire identifier
  // (thisown) since otherwise we could remove append from a list object
  bool containsSwigInternals = false;
  for(const QString &optstr : ret)
  {
    if(optstr.contains(lit(".thisown")))
    {
      containsSwigInternals = true;
      break;
    }
  }

  if(containsSwigInternals)
  {
    for(int i = 0; i < ret.count();)
    {
      if(ret[i].endsWith(lit(".acquire(")) || ret[i].endsWith(lit(".append(")) ||
         ret[i].endsWith(lit(".disown(")) || ret[i].endsWith(lit(".next(")) ||
         ret[i].endsWith(lit(".own(")) || ret[i].endsWith(lit(".this")) ||
         ret[i].endsWith(lit(".thisown")))
        ret.removeAt(i);
      else
        i++;
    }
  }

  Py_DecRef(completeFunction);

  PyGILState_Release(gil);

  return ret;
}

void PythonContext::InvalidateNativeQtObject(QObject *object)
{
#if PYSIDE2_ENABLED
  if(!object || !initialised() || !Py_IsInitialized())
    return;

  // Native widgets created outside PySide have no generated wrapper destructor to notify
  // Shiboken. Their borrowed QWidget wrapper can gain ownership when removed from a Python
  // container. Revoke that ownership and invalidate the wrapper before native deletion, so
  // releasing the final Python reference cannot destruct the same QObject a second time.
  PyGILState_STATE gil = PyGILState_Ensure();
  SbkObject *wrapper = Shiboken::BindingManager::instance().retrieveWrapper(object);
  if(wrapper)
  {
    // Removing the PySide parent relationship may release its last retained reference.
    Py_INCREF(reinterpret_cast<PyObject *>(wrapper));
    Shiboken::Object::releaseOwnership(wrapper);
    Shiboken::Object::invalidate(wrapper);
    Shiboken::Object::removeParent(wrapper, false);
    Py_DECREF(reinterpret_cast<PyObject *>(wrapper));
  }
  PyGILState_Release(gil);
#endif
}

PyObject *PythonContext::QtObjectToPython(const char *typeName, QObject *object)
{
#if PYSIDE2_ENABLED
  if(!initialised())
    Py_RETURN_NONE;

  if(!SbkPySide2_QtCoreTypes || !SbkPySide2_QtGuiTypes || !SbkPySide2_QtWidgetsTypes)
  {
    QWidget *w = qobject_cast<QWidget *>(object);
    if(w)
      return WrapBareQWidget(w);

    Py_RETURN_NONE;
  }

  if(object == NULL)
  {
    Py_RETURN_NONE;
  }

  PyObject *obj =
      Shiboken::Object::newObject(reinterpret_cast<SbkObjectType *>(Shiboken::SbkType<QObject>()),
                                  object, false, false, typeName);

  return obj;
#else
  QWidget *w = qobject_cast<QWidget *>(object);
  if(w)
    return WrapBareQWidget(w);

  Py_RETURN_NONE;
#endif
}

// callback to flush output every so often (not constantly, to avoid spamming signals)
void PythonContext::outputTick()
{
  QMutexLocker lock(&outputMutex);

  if(!outstr.isEmpty())
  {
    emit textOutput(false, outstr);
  }

  if(!errstr.isEmpty())
  {
    emit textOutput(true, errstr);
  }

  outstr.clear();
  errstr.clear();
}

void PythonContext::addText(bool isStdError, const QString &output)
{
  QMutexLocker lock(&outputMutex);

  if(isStdError)
    errstr += output;
  else
    outstr += output;
}

void PythonContext::setPyGlobal(const char *varName, PyObject *obj)
{
  if(!initialised())
  {
    emit exception(lit("SystemError"), tr("Python integration failed to initialise."), -1, {});
    return;
  }

  int ret = -1;

  PyGILState_STATE gil = PyGILState_Ensure();

  if(obj)
    ret = PyDict_SetItemString(context_namespace, varName, obj);

  PyGILState_Release(gil);

  if(ret == 0)
    return;

  emit exception(lit("RuntimeError"),
                 tr("Failed to set variable '%1'").arg(QString::fromUtf8(varName)), -1, {});
}

void PythonContext::outstream_del(PyObject *self)
{
  OutputRedirector *redirector = (OutputRedirector *)self;

  if(redirector)
  {
    PythonContext *context = redirector->context;

    // delete the context on the UI thread.
    GUIInvoke::call(context, [context]() { delete context; });
  }
}

PyObject *PythonContext::outstream_write(PyObject *self, PyObject *args)
{
  const char *text = NULL;

  if(!PyArg_ParseTuple(args, "z:write", &text))
    return NULL;

  if(PyErr_Occurred())
    return NULL;

  OutputRedirector *redirector = (OutputRedirector *)self;

  if(redirector)
  {
    PythonContext *context = redirector->context;
    // most likely this is NULL because the sys.stdout override is static and shared amongst
    // contexts. So look up the global variable that stores the context
    if(context == NULL)
    {
      PyFrameObject *frame = PyEval_GetFrame();
      // inc reference count here so all frames can be decref'd whether they come from here or
      // PyFrame_GetBack
      Py_XINCREF(frame);

      while(frame)
      {
        PyObject *globals = PyFrame_GetGlobals(frame);
        if(globals)
        {
          OutputRedirector *global =
              (OutputRedirector *)PyDict_GetItemString(globals, "_renderdoc_internal");
          if(global)
            context = global->context;
        }

        Py_XDECREF(globals);

        // first get the next frame without decrefing the current
        PyFrameObject *back = PyFrame_GetBack(frame);
        // now decref the old frame
        Py_XDECREF(frame);
        // and iterate on the next one, if we got one
        frame = back;

        if(context)
        {
          // release the last trailing ref, which we know is either NULL or a strong reference from
          // PyFrame_GetBack
          Py_XDECREF(frame);
          break;
        }
      }
    }

    if(context)
    {
      context->addText(redirector->isStdError ? true : false, QString::fromUtf8(text));
    }
    else
    {
      // if context is still NULL we're running in the extension context
      rdcstr message = text;

      PyFrameObject *frame = PyEval_GetFrame();

      while(!message.empty() && (message.back() == '\n' || message.back() == '\r'))
        message.pop_back();

      QString filename = lit("unknown");
      int line = 0;

      if(frame)
      {
        PyCodeObject *code = PyFrame_GetCode(frame);
        filename = ToQStr(code->co_filename);
        Py_XDECREF(code);
        line = PyFrame_GetLineNumber(frame);
      }

      if(!message.empty())
        RENDERDOC_LogMessage(redirector->isStdError ? LogType::Warning : LogType::Comment, "EXTN",
                             filename, line, message);
    }
  }

  Py_RETURN_NONE;
}

PyObject *PythonContext::outstream_flush(PyObject *self, PyObject *args)
{
  if(PyErr_Occurred())
    return NULL;

  Py_RETURN_NONE;
}

int PythonContext::traceEvent(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg)
{
  PyObject *thisobj = PyDict_GetItemString(obj, "thisobj");

  uint64_t thisuint64 = PyLong_AsUnsignedLongLong(thisobj);
  uintptr_t thisint = (uintptr_t)thisuint64;
  PythonContext *context = (PythonContext *)thisint;

  PyCodeObject *code = PyFrame_GetCode(frame);

  PyObject *compiled = PyDict_GetItemString(obj, "compiled");
  if(compiled == (PyObject *)code && what == PyTrace_LINE)
  {
    context->location.line = PyFrame_GetLineNumber(frame);

    emit context->traceLine(context->location.file, context->location.line);
  }

  Py_XDECREF(code);

  if(context->shouldAbort())
  {
    PyErr_SetString(PyExc_SystemExit, "Execution aborted.");
    return -1;
  }

  return 0;
}

extern "C" PyThreadState *GetExecutingThreadState(PyObject *global_handle)
{
  OutputRedirector *redirector = (OutputRedirector *)global_handle;
  if(redirector->context)
    return redirector->context->GetExecutingThreadState();

  return NULL;
}

extern "C" PyObject *GetCurrentGlobalHandle()
{
  PyObject *frame_global_handle = NULL;

  // walk the frames until we find one with _renderdoc_internal. If we call a function in another
  // module the globals may not have the entry, but the root level is expected to.
  {
    PyFrameObject *frame = PyEval_GetFrame();
    // inc reference count here so all frames can be decref'd whether they come from here or
    // PyFrame_GetBack
    Py_XINCREF(frame);

    while(frame)
    {
      PyObject *globals = PyFrame_GetGlobals(frame);
      frame_global_handle = PyDict_GetItemString(globals, "_renderdoc_internal");
      Py_XDECREF(globals);

      // first get the next frame without decrefing the current
      PyFrameObject *back = PyFrame_GetBack(frame);
      // now decref the old frame
      Py_XDECREF(frame);
      // and iterate on the next one, if we got one
      frame = back;

      if(frame_global_handle)
      {
        // release the last trailing ref, which we know is either NULL or a strong reference from
        // PyFrame_GetBack
        Py_XDECREF(frame);
        break;
      }
    }
  }

  if(frame_global_handle)
    return frame_global_handle;

  if(current_global_handle)
    return current_global_handle;

  PyObject *sys = PyImport_ImportModule("sys");
  if(sys)
  {
    PyObject *ret = PyObject_SafeGetAttrString(sys, "_renderdoc_internal");

    Py_XDECREF(sys);
    Py_XDECREF(ret);
  }

  return NULL;
}

extern "C" void HandleException(PyObject *global_handle)
{
  QString typeStr;
  QString valueStr;
  int finalLine = -1;
  QList<QString> frames;

  FetchException(typeStr, valueStr, finalLine, frames);

  OutputRedirector *redirector = (OutputRedirector *)global_handle;
  if(redirector && redirector->context)
  {
    emit redirector->context->exception(typeStr, valueStr, finalLine, frames);
  }
  else if(redirector && !redirector->context)
  {
    // if still NULL we're running in the extension context
    rdcstr exString;

    if(!frames.isEmpty())
    {
      exString += "Traceback (most recent call last):\n";
      for(const QString &f : frames)
      {
        exString += "  ";
        exString += f;
        exString += "\n";
      }
    }

    exString += typeStr;
    exString += ": ";
    exString += valueStr;
    exString += "\n";

    PyFrameObject *frame = PyEval_GetFrame();

    QString filename = lit("unknown");
    int linenum = 0;

    if(frame)
    {
      PyCodeObject *code = PyFrame_GetCode(frame);
      filename = ToQStr(code->co_filename);
      Py_XDECREF(code);
      linenum = PyFrame_GetLineNumber(frame);
    }

    RENDERDOC_LogMessage(LogType::Error, "EXTN", filename, linenum, exString);
  }
}

extern "C" bool IsThreadBlocking(PyObject *global_handle)
{
  OutputRedirector *redirector = (OutputRedirector *)global_handle;
  if(redirector)
    return redirector->block;
  return false;
}

extern "C" void SetThreadBlocking(PyObject *global_handle, bool block)
{
  OutputRedirector *redirector = (OutputRedirector *)global_handle;
  if(redirector)
    redirector->block = block;
}

extern "C" void QueueDecRef(PyObject *obj)
{
  QMutexLocker lock(&decrefQueueMutex);

  decrefQueue.push_back(obj);
}

extern "C" void ProcessDecRefQueue()
{
  QMutexLocker lock(&decrefQueueMutex);

  if(decrefQueue.isEmpty())
    return;

  for(PyObject *obj : decrefQueue)
    Py_XDECREF(obj);

  decrefQueue.clear();
}

extern "C" QWidget *QWidgetFromPy(PyObject *widget)
{
  return PythonContext::QWidgetFromPy(widget);
}

extern "C" PyObject *QWidgetToPy(QWidget *widget)
{
  return PythonContext::QWidgetToPy(widget);
}
