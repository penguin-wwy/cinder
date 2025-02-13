// Copyright (c) Facebook, Inc. and its affiliates. (http://www.facebook.com)
#include "Jit/pyjit.h"

#include "Python.h"
//#include "internal/pycore_pystate.h"
#include "Include/internal/pycore_pystate.h"
#include "internal/pycore_shadow_frame.h"

#include "Jit/codegen/gen_asm.h"
#include "Jit/codegen/inliner.h"
#include "Jit/containers.h"
#include "Jit/frame.h"
#include "Jit/hir/builder.h"
#include "Jit/inline_cache.h"
#include "Jit/jit_context.h"
#include "Jit/jit_gdb_support.h"
#include "Jit/jit_list.h"
#include "Jit/jit_x_options.h"
#include "Jit/log.h"
#include "Jit/perf_jitdump.h"
#include "Jit/profile_data.h"
#include "Jit/ref.h"
#include "Jit/runtime.h"
#include "Jit/type_profiler.h"
#include "Jit/util.h"

#include <atomic>
#include <chrono>
#include <climits>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <thread>
#include <unordered_set>

#define DEFAULT_CODE_SIZE 2 * 1024 * 1024

using namespace jit;

int64_t __strobe_CodeRuntime_py_code = CodeRuntime::kPyCodeOffset;

enum InitState { JIT_NOT_INITIALIZED, JIT_INITIALIZED, JIT_FINALIZED };
enum FrameMode { PY_FRAME = 0, SHADOW_FRAME };

struct JitConfig {
  InitState init_state{JIT_NOT_INITIALIZED};

  int is_enabled{0};
  FrameMode frame_mode{PY_FRAME};
  int are_type_slots_enabled{0};
  int allow_jit_list_wildcards{0};
  int compile_all_static_functions{0};
  size_t batch_compile_workers{0};
  int multithreaded_compile_test{0};
};
JitConfig jit_config;

namespace {
// Extra information needed to compile a PyCodeObject.
struct CodeData {
  CodeData(PyObject* m, PyObject* g) : module{m}, globals{g} {}

  Ref<> module;
  Ref<PyDictObject> globals;
};

// Amount of time taken to batch compile everything when disable_jit is called
long g_batch_compilation_time_ms = 0;

} // namespace

static _PyJITContext* jit_ctx;
static JITList* g_jit_list{nullptr};

// Function and code objects registered for compilation. Every entry that is a
// code object has corresponding entry in jit_code_data.
static std::unordered_set<BorrowedRef<>> jit_reg_units;
static std::unordered_map<BorrowedRef<PyCodeObject>, CodeData> jit_code_data;

// Strong references to every function and code object that were ever
// registered, to keep them alive for batch testing.
static std::vector<Ref<>> test_multithreaded_units;
static std::unordered_map<PyFunctionObject*, std::chrono::duration<double>>
    jit_time_functions;

// Frequently-used strings that we intern at JIT startup and hold references to.
#define INTERNED_STRINGS(X) \
  X(bc_offset)              \
  X(code_hash)              \
  X(count)                  \
  X(description)            \
  X(filename)               \
  X(firstlineno)            \
  X(func_qualname)          \
  X(guilty_type)            \
  X(int)                    \
  X(lineno)                 \
  X(normal)                 \
  X(normvector)             \
  X(opname)                 \
  X(reason)                 \
  X(types)

#define DECLARE_STR(s) static PyObject* s_str_##s{nullptr};
INTERNED_STRINGS(DECLARE_STR)
#undef DECLARE_STR

static std::array<PyObject*, 256> s_opnames;

static double total_compliation_time = 0.0;

int g_profile_new_interp_threads = 0;

struct CompilationTimer {
  explicit CompilationTimer(BorrowedRef<PyFunctionObject> f)
      : start(std::chrono::steady_clock::now()), func(f) {}

  ~CompilationTimer() {
    auto end = std::chrono::steady_clock::now();
    std::chrono::duration<double> time_span =
        std::chrono::duration_cast<std::chrono::duration<double>>(end - start);

    double time = time_span.count();
    total_compliation_time += time;
    jit::ThreadedCompileSerialize guard;
    jit_time_functions.emplace(func, time_span);
  }

  std::chrono::steady_clock::time_point start;
  BorrowedRef<PyFunctionObject> func{nullptr};
};

static std::atomic<int> g_compile_workers_attempted;
static int g_compile_workers_retries;

// Compile the given compilation unit, returning the result code.
static _PyJIT_Result compileUnit(BorrowedRef<> unit) {
  if (PyFunction_Check(unit)) {
    BorrowedRef<PyFunctionObject> func(unit);
    CompilationTimer t{func};
    return _PyJITContext_CompileFunction(jit_ctx, func);
  }
  JIT_CHECK(PyCode_Check(unit), "Expected function or code object");
  BorrowedRef<PyCodeObject> code(unit);
  const CodeData& data = map_get(jit_code_data, code);
  return _PyJITContext_CompileCode(jit_ctx, data.module, code, data.globals);
}

static void compile_worker_thread() {
  JIT_DLOG("Started compile worker in thread %d", std::this_thread::get_id());
  BorrowedRef<> unit;
  while ((unit = g_threaded_compile_context.nextUnit()) != nullptr) {
    g_compile_workers_attempted++;
    if (compileUnit(unit) == PYJIT_RESULT_RETRY) {
      ThreadedCompileSerialize guard;
      g_compile_workers_retries++;
      g_threaded_compile_context.retryUnit(unit);
      JIT_DLOG(
          "Retrying compile of function: %s",
          funcFullname(reinterpret_cast<PyFunctionObject*>(unit.get())));
    }
  }
  JIT_DLOG("Finished compile worker in thread %d", std::this_thread::get_id());
}

static void multithread_compile_all(std::vector<BorrowedRef<>>&& work_units) {
  JIT_CHECK(jit_ctx, "JIT not initialized");

  // Disable checks for using GIL protected data across threads.
  // Conceptually what we're doing here is saying we're taking our own
  // responsibility for managing locking of CPython runtime data structures.
  // Instead of holding the GIL to serialize execution to one thread, we're
  // holding the GIL for a group of co-operating threads which are aware of each
  // other. We still need the GIL as this protects the cooperating threads from
  // unknown other threads. Within our group of cooperating threads we can
  // safely do any read-only operations in parallel, but we grab our own lock if
  // we do a write (e.g. an incref).
  int old_gil_check_enabled = _PyGILState_check_enabled;
  _PyGILState_check_enabled = 0;

  g_threaded_compile_context.startCompile(std::move(work_units));
  std::vector<std::thread> worker_threads;
  JIT_CHECK(jit_config.batch_compile_workers, "Zero workers for compile");
  {
    // Hold a lock while we create threads because IG production has magic to
    // wrap pthread_create() and run Python code before threads are created.
    ThreadedCompileSerialize guard;
    for (size_t i = 0; i < jit_config.batch_compile_workers; i++) {
      worker_threads.emplace_back(compile_worker_thread);
    }
  }
  for (std::thread& worker_thread : worker_threads) {
    worker_thread.join();
  }
  std::vector<BorrowedRef<>> retry_list{
      g_threaded_compile_context.endCompile()};
  for (auto unit : retry_list) {
    compileUnit(unit);
  }
  _PyGILState_check_enabled = old_gil_check_enabled;
}

static PyObject* multithreaded_compile_test(PyObject*, PyObject*) {
  if (!jit_config.multithreaded_compile_test) {
    PyErr_SetString(
        PyExc_NotImplementedError, "multithreaded_compile_test not enabled");
    return NULL;
  }
  g_compile_workers_attempted = 0;
  g_compile_workers_retries = 0;
  JIT_LOG("(Re)compiling %d units", test_multithreaded_units.size());
  _PyJITContext_ClearCache(jit_ctx);
  std::chrono::time_point time_start = std::chrono::steady_clock::now();
  multithread_compile_all(
      {test_multithreaded_units.begin(), test_multithreaded_units.end()});
  std::chrono::time_point time_end = std::chrono::steady_clock::now();
  JIT_LOG(
      "Took %d ms, compiles attempted: %d, compiles retried: %d",
      std::chrono::duration_cast<std::chrono::milliseconds>(
          time_end - time_start)
          .count(),
      g_compile_workers_attempted,
      g_compile_workers_retries);
  test_multithreaded_units.clear();
  Py_RETURN_NONE;
}

static PyObject* is_multithreaded_compile_test_enabled(PyObject*, PyObject*) {
  if (jit_config.multithreaded_compile_test) {
    Py_RETURN_TRUE;
  }
  Py_RETURN_FALSE;
}

static PyObject*
disable_jit(PyObject* /* self */, PyObject* const* args, Py_ssize_t nargs) {
  if (nargs > 1) {
    PyErr_SetString(PyExc_TypeError, "disable expects 0 or 1 arg");
    return NULL;
  } else if (nargs == 1 && !PyBool_Check(args[0])) {
    PyErr_SetString(
        PyExc_TypeError,
        "disable expects bool indicating to compile pending functions");
    return NULL;
  }

  if (nargs == 0 || args[0] == Py_True) {
    // Compile all of the pending functions/codes before shutting down
    std::chrono::time_point start = std::chrono::steady_clock::now();
    if (jit_config.batch_compile_workers > 0) {
      multithread_compile_all({jit_reg_units.begin(), jit_reg_units.end()});
      jit_reg_units.clear();
    } else {
      std::unordered_set<BorrowedRef<>> units;
      units.swap(jit_reg_units);
      for (auto unit : units) {
        compileUnit(unit);
      }
    }
    std::chrono::time_point end = std::chrono::steady_clock::now();
    g_batch_compilation_time_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(end - start)
            .count();

    jit_code_data.clear();
  }

  _PyJIT_Disable();
  Py_RETURN_NONE;
}

static PyObject* get_batch_compilation_time_ms(PyObject*, PyObject*) {
  return PyLong_FromLong(g_batch_compilation_time_ms);
}

static PyObject* force_compile(PyObject* /* self */, PyObject* func) {
  if (!PyFunction_Check(func)) {
    PyErr_SetString(PyExc_TypeError, "force_compile expected a function");
    return NULL;
  }

  if (jit_reg_units.count(func)) {
    _PyJIT_CompileFunction((PyFunctionObject*)func);
    Py_RETURN_TRUE;
  }

  Py_RETURN_FALSE;
}

int _PyJIT_IsCompiled(PyObject* func) {
  if (jit_ctx == nullptr) {
    return 0;
  }
  JIT_DCHECK(
      PyFunction_Check(func),
      "Expected PyFunctionObject, got '%.200s'",
      Py_TYPE(func)->tp_name);

  return _PyJITContext_DidCompile(jit_ctx, func);
}

static PyObject* is_jit_compiled(PyObject* /* self */, PyObject* func) {
  int st = _PyJIT_IsCompiled(func);
  PyObject* res = NULL;
  if (st == 1) {
    res = Py_True;
  } else if (st == 0) {
    res = Py_False;
  }
  Py_XINCREF(res);
  return res;
}

static PyObject* print_hir(PyObject* /* self */, PyObject* func) {
  if (!PyFunction_Check(func)) {
    PyErr_SetString(PyExc_TypeError, "arg 1 must be a function");
    return NULL;
  }

  int st = _PyJITContext_DidCompile(jit_ctx, func);
  if (st == -1) {
    return NULL;
  } else if (st == 0) {
    PyErr_SetString(PyExc_ValueError, "function is not jit compiled");
    return NULL;
  }

  if (_PyJITContext_PrintHIR(jit_ctx, func) < 0) {
    return NULL;
  } else {
    Py_RETURN_NONE;
  }
}

static PyObject* disassemble(PyObject* /* self */, PyObject* func) {
  if (!PyFunction_Check(func)) {
    PyErr_SetString(PyExc_TypeError, "arg 1 must be a function");
    return NULL;
  }

  int st = _PyJITContext_DidCompile(jit_ctx, func);
  if (st == -1) {
    return NULL;
  } else if (st == 0) {
    PyErr_SetString(PyExc_ValueError, "function is not jit compiled");
    return NULL;
  }

  if (_PyJITContext_Disassemble(jit_ctx, func) < 0) {
    return NULL;
  } else {
    Py_RETURN_NONE;
  }
}

static PyObject* get_jit_list(PyObject* /* self */, PyObject*) {
  if (g_jit_list == nullptr) {
    Py_RETURN_NONE;
  } else {
    auto jit_list = Ref<>::steal(g_jit_list->getList());
    return jit_list.release();
  }
}

static PyObject* jit_list_append(PyObject* /* self */, PyObject* line) {
  if (g_jit_list == nullptr) {
    g_jit_list = JITList::create().release();
  }
  Py_ssize_t line_len;
  const char* line_str = PyUnicode_AsUTF8AndSize(line, &line_len);
  if (line_str == NULL) {
    return NULL;
  }
  g_jit_list->parseLine(
      {line_str, static_cast<std::string::size_type>(line_len)});
  Py_RETURN_NONE;
}

static PyObject* get_compiled_functions(PyObject* /* self */, PyObject*) {
  return _PyJITContext_GetCompiledFunctions(jit_ctx);
}

static PyObject* get_compilation_time(PyObject* /* self */, PyObject*) {
  PyObject* res =
      PyLong_FromLong(static_cast<long>(total_compliation_time * 1000));
  return res;
}

static PyObject* get_function_compilation_time(
    PyObject* /* self */,
    PyObject* func) {
  auto iter =
      jit_time_functions.find(reinterpret_cast<PyFunctionObject*>(func));
  if (iter == jit_time_functions.end()) {
    Py_RETURN_NONE;
  }

  PyObject* res = PyLong_FromLong(iter->second.count() * 1000);
  return res;
}

namespace {

// Simple wrapper functions to turn NULL or -1 return values from C-API
// functions into a thrown exception. Meant for repetitive runs of C-API calls
// and not intended for use in public APIs.
class CAPIError : public std::exception {};

PyObject* check(PyObject* obj) {
  if (obj == nullptr) {
    throw CAPIError();
  }
  return obj;
}

int check(int ret) {
  if (ret < 0) {
    throw CAPIError();
  }
  return ret;
}

Ref<> make_deopt_stats() {
  Runtime* runtime = codegen::NativeGeneratorFactory::runtime();
  auto stats = Ref<>::steal(check(PyList_New(0)));

  for (auto& pair : runtime->deoptStats()) {
    const DeoptMetadata& meta = runtime->getDeoptMetadata(pair.first);
    const DeoptStat& stat = pair.second;
    CodeRuntime& code_rt = *meta.code_rt;
    BorrowedRef<PyCodeObject> code = code_rt.GetCode();

    auto func_qualname = code->co_qualname;
    int lineno_raw = code->co_lnotab != nullptr
        ? PyCode_Addr2Line(code, meta.next_instr_offset)
        : -1;
    auto lineno = Ref<>::steal(check(PyLong_FromLong(lineno_raw)));
    auto reason =
        Ref<>::steal(check(PyUnicode_FromString(deoptReasonName(meta.reason))));
    auto description = Ref<>::steal(check(PyUnicode_FromString(meta.descr)));

    // Helper to create an event dict with a given count value.
    auto append_event = [&](size_t count_raw, const char* type) {
      auto event = Ref<>::steal(check(PyDict_New()));
      auto normals = Ref<>::steal(check(PyDict_New()));
      auto ints = Ref<>::steal(check(PyDict_New()));

      check(PyDict_SetItem(event, s_str_normal, normals));
      check(PyDict_SetItem(event, s_str_int, ints));
      check(PyDict_SetItem(normals, s_str_func_qualname, func_qualname));
      check(PyDict_SetItem(normals, s_str_filename, code->co_filename));
      check(PyDict_SetItem(ints, s_str_lineno, lineno));
      check(PyDict_SetItem(normals, s_str_reason, reason));
      check(PyDict_SetItem(normals, s_str_description, description));

      auto count = Ref<>::steal(check(PyLong_FromSize_t(count_raw)));
      check(PyDict_SetItem(ints, s_str_count, count));
      auto type_str = Ref<>::steal(check(PyUnicode_InternFromString(type)));
      check(PyDict_SetItem(normals, s_str_guilty_type, type_str) < 0);
      check(PyList_Append(stats, event));
    };

    // For deopts with type profiles, add a copy of the dict with counts for
    // each type, including "other".
    if (!stat.types.empty()) {
      for (size_t i = 0; i < stat.types.size && stat.types.types[i] != nullptr;
           ++i) {
        append_event(stat.types.counts[i], stat.types.types[i]->tp_name);
      }
      if (stat.types.other > 0) {
        append_event(stat.types.other, "<other>");
      }
    } else {
      append_event(stat.count, "<none>");
    }
  }

  runtime->clearDeoptStats();

  return stats;
}

} // namespace

static PyObject* get_and_clear_runtime_stats(PyObject* /* self */, PyObject*) {
  auto stats = Ref<>::steal(PyDict_New());
  if (stats == nullptr) {
    return nullptr;
  }

  try {
    Ref<> deopt_stats = make_deopt_stats();
    check(PyDict_SetItemString(stats, "deopt", deopt_stats));
  } catch (const CAPIError&) {
    return nullptr;
  }

  return stats.release();
}

static PyObject* clear_runtime_stats(PyObject* /* self */, PyObject*) {
  codegen::NativeGeneratorFactory::runtime()->clearDeoptStats();
  Py_RETURN_NONE;
}

static PyObject* get_compiled_size(PyObject* /* self */, PyObject* func) {
  if (jit_ctx == NULL) {
    return PyLong_FromLong(0);
  }

  long size = _PyJITContext_GetCodeSize(jit_ctx, func);
  PyObject* res = PyLong_FromLong(size);
  return res;
}

static PyObject* get_compiled_stack_size(PyObject* /* self */, PyObject* func) {
  if (jit_ctx == NULL) {
    return PyLong_FromLong(0);
  }

  long size = _PyJITContext_GetStackSize(jit_ctx, func);
  PyObject* res = PyLong_FromLong(size);
  return res;
}

static PyObject* get_compiled_spill_stack_size(
    PyObject* /* self */,
    PyObject* func) {
  if (jit_ctx == NULL) {
    return PyLong_FromLong(0);
  }

  long size = _PyJITContext_GetSpillStackSize(jit_ctx, func);
  PyObject* res = PyLong_FromLong(size);
  return res;
}

static PyObject* jit_frame_mode(PyObject* /* self */, PyObject*) {
  return PyLong_FromLong(jit_config.frame_mode);
}

static PyObject* get_supported_opcodes(PyObject* /* self */, PyObject*) {
  auto set = Ref<>::steal(PySet_New(nullptr));
  if (set == nullptr) {
    return nullptr;
  }

  for (auto op : hir::kSupportedOpcodes) {
    auto op_obj = Ref<>::steal(PyLong_FromLong(op));
    if (op_obj == nullptr) {
      return nullptr;
    }
    if (PySet_Add(set, op_obj) < 0) {
      return nullptr;
    }
  }

  return set.release();
}

static PyObject* jit_force_normal_frame(PyObject*, PyObject* func_obj) {
  if (!PyFunction_Check(func_obj)) {
    PyErr_SetString(PyExc_TypeError, "Input must be a function");
    return NULL;
  }
  PyFunctionObject* func = reinterpret_cast<PyFunctionObject*>(func_obj);

  reinterpret_cast<PyCodeObject*>(func->func_code)->co_flags |= CO_NORMAL_FRAME;

  Py_INCREF(func_obj);
  return func_obj;
}

static PyObject* jit_suppress(PyObject*, PyObject* func_obj) {
  if (!PyFunction_Check(func_obj)) {
    PyErr_SetString(PyExc_TypeError, "Input must be a function");
    return NULL;
  }
  PyFunctionObject* func = reinterpret_cast<PyFunctionObject*>(func_obj);

  reinterpret_cast<PyCodeObject*>(func->func_code)->co_flags |= CO_SUPPRESS_JIT;

  Py_INCREF(func_obj);
  return func_obj;
}

static PyMethodDef jit_methods[] = {
    {"disable",
     (PyCFunction)(void*)disable_jit,
     METH_FASTCALL,
     "Disable the jit."},
    {"disassemble", disassemble, METH_O, "Disassemble JIT compiled functions"},
    {"is_jit_compiled",
     is_jit_compiled,
     METH_O,
     "Check if a function is jit compiled."},
    {"force_compile",
     force_compile,
     METH_O,
     "Force a function to be JIT compiled if it hasn't yet"},
    {"jit_frame_mode",
     jit_frame_mode,
     METH_NOARGS,
     "Get JIT frame mode (0 = normal frames, 1 = no frames, 2 = shadow frames"},
    {"get_jit_list", get_jit_list, METH_NOARGS, "Get the JIT-list"},
    {"jit_list_append", jit_list_append, METH_O, "Parse a JIT-list line"},
    {"print_hir",
     print_hir,
     METH_O,
     "Print the HIR for a jitted function to stdout."},
    {"get_supported_opcodes",
     get_supported_opcodes,
     METH_NOARGS,
     "Return a set of all supported opcodes, as ints."},
    {"get_compiled_functions",
     get_compiled_functions,
     METH_NOARGS,
     "Return a list of functions that are currently JIT-compiled."},
    {"get_compilation_time",
     get_compilation_time,
     METH_NOARGS,
     "Return the total time used for JIT compiling functions in milliseconds."},
    {"get_function_compilation_time",
     get_function_compilation_time,
     METH_O,
     "Return the time used for JIT compiling a given function in "
     "milliseconds."},
    {"get_and_clear_runtime_stats",
     get_and_clear_runtime_stats,
     METH_NOARGS,
     "Returns information about the runtime behavior of JIT-compiled code."},
    {"clear_runtime_stats",
     clear_runtime_stats,
     METH_NOARGS,
     "Clears runtime stats about JIT-compiled code without returning a value."},
    {"get_compiled_size",
     get_compiled_size,
     METH_O,
     "Return code size in bytes for a JIT-compiled function."},
    {"get_compiled_stack_size",
     get_compiled_stack_size,
     METH_O,
     "Return stack size in bytes for a JIT-compiled function."},
    {"get_compiled_spill_stack_size",
     get_compiled_spill_stack_size,
     METH_O,
     "Return stack size in bytes used for register spills for a JIT-compiled "
     "function."},
    {"jit_force_normal_frame",
     jit_force_normal_frame,
     METH_O,
     "Decorator forcing a function to always use normal frame mode when JIT."},
    {"jit_suppress",
     jit_suppress,
     METH_O,
     "Decorator to disable the JIT for the decorated function."},
    {"multithreaded_compile_test",
     multithreaded_compile_test,
     METH_NOARGS,
     "Force multi-threaded recompile of still existing JIT functions for test"},
    {"is_multithreaded_compile_test_enabled",
     is_multithreaded_compile_test_enabled,
     METH_NOARGS,
     "Return True if multithreaded_compile_test mode is enabled"},
    {"get_batch_compilation_time_ms",
     get_batch_compilation_time_ms,
     METH_NOARGS,
     "Return the number of milliseconds spent in batch compilation when "
     "disabling the JIT."},
    {NULL, NULL, 0, NULL}};

static PyModuleDef jit_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "cinderjit",
    .m_doc = NULL,
    .m_size = -1,
    .m_methods = jit_methods,
    .m_slots = nullptr,
    .m_traverse = nullptr,
    .m_clear = nullptr,
    .m_free = nullptr};

static int onJitListImpl(
    BorrowedRef<PyCodeObject> code,
    BorrowedRef<> mod,
    BorrowedRef<> qualname) {
  bool is_static = code->co_flags & CO_STATICALLY_COMPILED;
  if (g_jit_list == nullptr ||
      (is_static && jit_config.compile_all_static_functions)) {
    // There's no jit list or the function is static.
    return 1;
  }
  if (g_jit_list->lookupCO(code) != 1) {
    return g_jit_list->lookupFO(mod, qualname);
  }
  return 1;
}

int _PyJIT_OnJitList(PyFunctionObject* func) {
  return onJitListImpl(func->func_code, func->func_module, func->func_qualname);
}

// Is env var set to a value other than "0" or ""?
int _is_env_truthy(const char* name) {
  const char* val = Py_GETENV(name);
  if (val == NULL || val[0] == '\0' || !strncmp(val, "0", 1)) {
    return 0;
  }
  return 1;
}

int _is_flag_set(const char* xoption, const char* envname) {
  if (PyJIT_IsXOptionSet(xoption) || _is_env_truthy(envname)) {
    return 1;
  }
  return 0;
}

// If the given X option is set and is a string, return it. If not, check the
// given environment variable for a nonempty value and return it if
// found. Otherwise, return nullptr.
const char* flag_string(const char* xoption, const char* envname) {
  PyObject* pyobj = nullptr;
  if (PyJIT_GetXOption(xoption, &pyobj) == 0 && pyobj != nullptr &&
      PyUnicode_Check(pyobj)) {
    return PyUnicode_AsUTF8(pyobj);
  }

  auto envval = Py_GETENV(envname);
  if (envval != nullptr && envval[0] != '\0') {
    return envval;
  }

  return nullptr;
}

long flag_long(const char* xoption, const char* envname, long _default) {
  PyObject* pyobj = nullptr;
  if (PyJIT_GetXOption(xoption, &pyobj) == 0 && pyobj != nullptr &&
      PyUnicode_Check(pyobj)) {
    auto val = Ref<>::steal(PyLong_FromUnicodeObject(pyobj, 10));
    if (val != nullptr) {
      return PyLong_AsLong(val);
    }
    JIT_LOG("Invalid value for %s: %s", xoption, PyUnicode_AsUTF8(pyobj));
  }

  const char* envval = Py_GETENV(envname);
  if (envval != nullptr && envval[0] != '\0') {
    try {
      return std::stol(envval);
    } catch (std::exception const&) {
      JIT_LOG("Invalid value for %s: %s", envname, envval);
    }
  }

  return _default;
}

int _PyJIT_Initialize() {
  if (jit_config.init_state == JIT_INITIALIZED) {
    return 0;
  }

  // Initialize some interned strings that can be used even when the JIT is
  // off.
#define INTERN_STR(s)                         \
  s_str_##s = PyUnicode_InternFromString(#s); \
  if (s_str_##s == nullptr) {                 \
    return -1;                                \
  }
  INTERNED_STRINGS(INTERN_STR)
#undef INTERN_STR

#define MAKE_OPNAME(opname, opnum)                                   \
  if ((s_opnames.at(opnum) = PyUnicode_InternFromString(#opname)) == \
      nullptr) {                                                     \
    return -1;                                                       \
  }
  PY_OPCODES(MAKE_OPNAME)
#undef MAKE_OPNAME

  int use_jit = 0;

  if (_is_flag_set("jit", "PYTHONJIT")) {
    use_jit = 1;
  }

  // Redirect logging to a file if configured.
  const char* log_filename = flag_string("jit-log-file", "PYTHONJITLOGFILE");
  if (log_filename != nullptr) {
    const char* kPidMarker = "{pid}";
    std::string pid_filename = log_filename;
    auto marker_pos = pid_filename.find(kPidMarker);
    if (marker_pos != std::string::npos) {
      pid_filename.replace(
          marker_pos, std::strlen(kPidMarker), fmt::format("{}", getpid()));
    }
    FILE* file = fopen(pid_filename.c_str(), "w");
    if (file == NULL) {
      JIT_LOG(
          "Couldn't open log file %s (%s), logging to stderr",
          pid_filename,
          strerror(errno));
    } else {
      g_log_file = file;
    }
  }

  if (_is_flag_set("jit-debug", "PYTHONJITDEBUG")) {
    JIT_DLOG("Enabling JIT debug and extra logging.");
    g_debug = 1;
    g_debug_verbose = 1;
  }
  if (_is_flag_set("jit-debug-refcount", "PYTHONJITDEBUGREFCOUNT")) {
    JIT_DLOG("Enabling JIT refcount insertion debug mode.");
    g_debug_refcount = 1;
  }
  if (_is_flag_set("jit-dump-hir", "PYTHONJITDUMPHIR")) {
    JIT_DLOG("Enabling JIT dump-hir mode.");
    g_dump_hir = 1;
  }
  if (_is_flag_set("jit-dump-hir-passes", "PYTHONJITDUMPHIRPASSES")) {
    JIT_DLOG("Enabling JIT dump-hir-passes mode.");
    g_dump_hir_passes = 1;
  }
  if (_is_flag_set("jit-dump-final-hir", "PYTHONJITDUMPFINALHIR")) {
    JIT_DLOG("Enabling JIT dump-final-hir mode.");
    g_dump_final_hir = 1;
  }
  if (_is_flag_set("jit-dump-lir", "PYTHONJITDUMPLIR")) {
    JIT_DLOG("Enable JIT dump-lir mode with origin data.");
    g_dump_lir = 1;
  }
  if (_is_flag_set("jit-dump-lir-no-origin", "PYTHONJITDUMPLIRNOORIGIN")) {
    JIT_DLOG("Enable JIT dump-lir mode without origin data.");
    g_dump_lir = 1;
    g_dump_lir_no_origin = 1;
  }
  if (_is_flag_set("jit-dump-c-helper", "PYTHONJITDUMPCHELPER")) {
    JIT_DLOG("Enable JIT dump-c-helper mode.");
    g_dump_c_helper = 1;
  }
  if (_is_flag_set("jit-disas-funcs", "PYTHONJITDISASFUNCS")) {
    JIT_DLOG(
        "jit-disas-funcs/PYTHONJITDISASFUNCS are deprecated and will soon be "
        "removed. Use jit-dump-asm and PYTHONJITDUMPASM instead.");
    g_dump_asm = 1;
  }
  if (_is_flag_set("jit-dump-asm", "PYTHONJITDUMPASM")) {
    JIT_DLOG("Enabling JIT dump-asm mode.");
    g_dump_asm = 1;
  }
  if (_is_flag_set("jit-gdb-support", "PYTHONJITGDBSUPPORT")) {
    JIT_DLOG("Enable GDB support and JIT debug mode.");
    g_debug = 1;
    g_gdb_support = 1;
  }
  if (_is_flag_set("jit-gdb-stubs-support", "PYTHONJITGDBSUPPORT")) {
    JIT_DLOG("Enable GDB support for stubs.");
    g_gdb_stubs_support = 1;
  }
  if (_is_flag_set("jit-gdb-write-elf", "PYTHONJITGDBWRITEELF")) {
    JIT_DLOG("Enable GDB support with ELF output, and JIT debug.");
    g_debug = 1;
    g_gdb_support = 1;
    g_gdb_write_elf_objects = 1;
  }
  if (_is_flag_set("jit-dump-stats", "PYTHONJITDUMPSTATS")) {
    JIT_DLOG("Dumping JIT runtime stats at shutdown.");
    g_dump_stats = 1;
  }
  if (_is_flag_set("jit-disable-lir-inliner", "PYTHONJITDISABLELIRINLINER")) {
    JIT_DLOG("Disable JIT lir inlining.");
    g_disable_lir_inliner = 1;
  }

  if (_is_flag_set(
          "jit-enable-jit-list-wildcards", "PYTHONJITENABLEJITLISTWILDCARDS")) {
    JIT_LOG("Enabling wildcards in JIT list");
    jit_config.allow_jit_list_wildcards = 1;
  }
  if (_is_flag_set("jit-all-static-functions", "PYTHONJITALLSTATICFUNCTIONS")) {
    JIT_DLOG("JIT-compiling all static functions");
    jit_config.compile_all_static_functions = 1;
  }

  std::unique_ptr<JITList> jit_list;
  const char* jl_fn = flag_string("jit-list-file", "PYTHONJITLISTFILE");
  if (jl_fn != NULL) {
    use_jit = 1;

    if (jit_config.allow_jit_list_wildcards) {
      jit_list = jit::WildcardJITList::create();
    } else {
      jit_list = jit::JITList::create();
    }
    if (jit_list == nullptr) {
      JIT_LOG("Failed to allocate JIT list");
      return -1;
    }
    if (!jit_list->parseFile(jl_fn)) {
      JIT_LOG("Could not parse jit-list, disabling JIT.");
      return 0;
    }
  }

  const char* profile_file =
      flag_string("jit-use-profile", "PYTHONJITUSEPROFILE");
  if (profile_file != nullptr) {
    JIT_LOG("Loading profile data from %s", profile_file);
    loadProfileData(profile_file);
  }
  if (_is_flag_set("jit-profile-interp", "PYTHONJITPROFILEINTERP")) {
    if (use_jit) {
      use_jit = 0;
      JIT_LOG("Keeping JIT disabled to enable interpreter profiling.");
    }
    g_profile_new_interp_threads = 1;
    _PyThreadState_SetProfileInterpAll(1);
  }
  if (_is_flag_set("jit-disable", "PYTHONJITDISABLE")) {
    if (use_jit) {
      use_jit = 0;
      JIT_LOG("Disabling JIT.");
    }
  }

  if (use_jit) {
    JIT_DLOG("Enabling JIT.");
  } else {
    return 0;
  }

  jit_ctx = new _PyJITContext();

  PyObject* mod = PyModule_Create(&jit_module);
  if (mod == NULL) {
    return -1;
  }

  PyObject* modname = PyUnicode_InternFromString("cinderjit");
  if (modname == NULL) {
    return -1;
  }

  PyObject* modules = PyImport_GetModuleDict();
  int st = _PyImport_FixupExtensionObject(mod, modname, modname, modules);
  Py_DECREF(modname);
  if (st == -1) {
    return -1;
  }

  jit_config.init_state = JIT_INITIALIZED;
  jit_config.is_enabled = 1;
  g_jit_list = jit_list.release();
  if (_is_flag_set("jit-shadow-frame", "PYTHONJITSHADOWFRAME")) {
    jit_config.frame_mode = SHADOW_FRAME;
    _PyThreadState_GetFrame =
        reinterpret_cast<PyThreadFrameGetter>(materializeShadowCallStack);
  }
  jit_config.are_type_slots_enabled = !PyJIT_IsXOptionSet("jit-no-type-slots");
  jit_config.batch_compile_workers =
      flag_long("jit-batch-compile-workers", "PYTHONJITBATCHCOMPILEWORKERS", 0);
  if (_is_flag_set(
          "jit-multithreaded-compile-test",
          "PYTHONJITMULTITHREADEDCOMPILETEST")) {
    jit_config.multithreaded_compile_test = 1;
  }
  if (_is_flag_set(
          "jit-list-match-line-numbers", "PYTHONJITLISTMATCHLINENUMBERS")) {
    jitlist_match_line_numbers(true);
  }

  total_compliation_time = 0.0;

  return 0;
}

int _PyJIT_IsEnabled() {
  return (jit_config.init_state == JIT_INITIALIZED) && jit_config.is_enabled;
}

void _PyJIT_AfterFork_Child() {
  perf::afterForkChild();
}

int _PyJIT_AreTypeSlotsEnabled() {
  return (jit_config.init_state == JIT_INITIALIZED) &&
      jit_config.are_type_slots_enabled;
}

int _PyJIT_Enable() {
  if (jit_config.init_state != JIT_INITIALIZED) {
    return 0;
  }
  jit_config.is_enabled = 1;
  return 0;
}

int _PyJIT_EnableTypeSlots() {
  if (!_PyJIT_IsEnabled()) {
    return 0;
  }
  jit_config.are_type_slots_enabled = 1;
  return 1;
}

void _PyJIT_Disable() {
  jit_config.is_enabled = 0;
  jit_config.are_type_slots_enabled = 0;
}

_PyJIT_Result _PyJIT_SpecializeType(
    PyTypeObject* type,
    _PyJIT_TypeSlots* slots) {
  return _PyJITContext_SpecializeType(jit_ctx, type, slots);
}

_PyJIT_Result _PyJIT_CompileFunction(PyFunctionObject* func) {
  // Serialize here as we might have been called re-entrantly.
  ThreadedCompileSerialize guard;

  if (jit_ctx == nullptr) {
    return PYJIT_NOT_INITIALIZED;
  }

  if (!_PyJIT_OnJitList(func)) {
    return PYJIT_RESULT_CANNOT_SPECIALIZE;
  }

  CompilationTimer timer(func);
  jit_reg_units.erase(reinterpret_cast<PyObject*>(func));
  return _PyJITContext_CompileFunction(jit_ctx, func);
}

// Recursively search the given co_consts tuple for any code objects that are
// on the current jit-list, using the given module name to form a
// fully-qualified function name.
static std::vector<BorrowedRef<PyCodeObject>> findNestedCodes(
    BorrowedRef<> module,
    BorrowedRef<> root_consts) {
  std::queue<PyObject*> consts_tuples;
  std::unordered_set<PyCodeObject*> visited;
  std::vector<BorrowedRef<PyCodeObject>> result;

  consts_tuples.push(root_consts);
  while (!consts_tuples.empty()) {
    PyObject* consts = consts_tuples.front();
    consts_tuples.pop();

    for (size_t i = 0, size = PyTuple_GET_SIZE(consts); i < size; ++i) {
      BorrowedRef<PyCodeObject> code = PyTuple_GET_ITEM(consts, i);
      if (!PyCode_Check(code) || !visited.insert(code).second ||
          code->co_qualname == nullptr ||
          !onJitListImpl(code, module, code->co_qualname)) {
        continue;
      }

      result.emplace_back(code);
      consts_tuples.emplace(code->co_consts);
    }
  }

  return result;
}

int _PyJIT_RegisterFunction(PyFunctionObject* func) {
  // Attempt to attach already-compiled code even if the JIT is disabled, as
  // long as it hasn't been finalized.
  if (jit_ctx != nullptr &&
      _PyJITContext_AttachCompiledCode(jit_ctx, func) == PYJIT_RESULT_OK) {
    return 1;
  }

  if (!_PyJIT_IsEnabled()) {
    return 0;
  }

  JIT_CHECK(
      !g_threaded_compile_context.compileRunning(),
      "Not intended for using during threaded compilation");
  int result = 0;
  auto register_unit = [](BorrowedRef<> unit) {
    if (jit_config.multithreaded_compile_test) {
      test_multithreaded_units.emplace_back(unit);
    }
    jit_reg_units.emplace(unit);
  };

  if (_PyJIT_OnJitList(func)) {
    register_unit(reinterpret_cast<PyObject*>(func));
    result = 1;
  }

  // If we have an active jit-list, scan this function's code object for any
  // nested functions that might be on the jit-list, and register them as
  // well.
  if (g_jit_list != nullptr) {
    PyObject* module = func->func_module;
    PyObject* globals = func->func_globals;
    for (auto code : findNestedCodes(
             module,
             reinterpret_cast<PyCodeObject*>(func->func_code)->co_consts)) {
      register_unit(reinterpret_cast<PyObject*>(code.get()));
      jit_code_data.emplace(
          std::piecewise_construct,
          std::forward_as_tuple(code),
          std::forward_as_tuple(module, globals));
    }
  }
  return result;
}

void _PyJIT_TypeModified(PyTypeObject* type) {
  if (jit_ctx) {
    _PyJITContext_TypeModified(jit_ctx, type);
  }
  jit::notifyICsTypeChanged(type);
}

void _PyJIT_TypeDestroyed(PyTypeObject* type) {
  if (jit_ctx) {
    _PyJITContext_TypeDestroyed(jit_ctx, type);
  }
}

void _PyJIT_FuncModified(PyFunctionObject* func) {
  if (jit_ctx) {
    _PyJITContext_FuncModified(jit_ctx, func);
  }
}

void _PyJIT_FuncDestroyed(PyFunctionObject* func) {
  if (_PyJIT_IsEnabled()) {
    jit_reg_units.erase(reinterpret_cast<PyObject*>(func));
  }
  if (jit_ctx) {
    _PyJITContext_FuncDestroyed(jit_ctx, func);
  }
}

void _PyJIT_CodeDestroyed(PyCodeObject* code) {
  if (_PyJIT_IsEnabled()) {
    jit_reg_units.erase(reinterpret_cast<PyObject*>(code));
    jit_code_data.erase(code);
  }
}

static void dump_jit_stats() {
  auto stats = get_and_clear_runtime_stats(nullptr, nullptr);
  if (stats == nullptr) {
    return;
  }
  auto stats_str = PyObject_Str(stats);
  if (stats_str == nullptr) {
    return;
  }

  JIT_LOG("JIT runtime stats:\n%s", PyUnicode_AsUTF8(stats_str));
}

int _PyJIT_Finalize() {
  if (g_dump_stats) {
    dump_jit_stats();
  }

  // Always release references from Runtime objects: C++ clients may have
  // invoked the JIT directly without initializing a full _PyJITContext.
  jit::codegen::NativeGeneratorFactory::runtime()->clearDeoptStats();
  jit::codegen::NativeGeneratorFactory::runtime()->releaseReferences();

  if (jit_config.init_state != JIT_INITIALIZED) {
    return 0;
  }

  delete g_jit_list;
  g_jit_list = nullptr;

  jit_config.init_state = JIT_FINALIZED;

  JIT_CHECK(jit_ctx != nullptr, "jit_ctx not initialized");
  delete jit_ctx;
  jit_ctx = nullptr;

#define CLEAR_STR(s) Py_CLEAR(s_str_##s);
  INTERNED_STRINGS(CLEAR_STR)
#undef CLEAR_STR
  for (PyObject*& opname : s_opnames) {
    if (opname != nullptr) {
      Py_DECREF(opname);
      opname = nullptr;
    }
  }

  _PyFunction_ClearSwitchboard();
  _PyType_ClearSwitchboard();

  jit::codegen::NativeGeneratorFactory::shutdown();
  return 0;
}

int _PyJIT_ShadowFrame() {
  return jit_config.frame_mode == SHADOW_FRAME;
}

PyObject* _PyJIT_GenSend(
    PyGenObject* gen,
    PyObject* arg,
    int exc,
    PyFrameObject* f,
    PyThreadState* tstate,
    int finish_yield_from) {
  auto gen_footer = reinterpret_cast<GenDataFooter*>(gen->gi_jit_data);

  // state should be valid and the generator should not be completed
  JIT_DCHECK(
      gen_footer->state == _PyJitGenState_JustStarted ||
          gen_footer->state == _PyJitGenState_Running,
      "Invalid JIT generator state");

  gen_footer->state = _PyJitGenState_Running;

  // JIT generators use NULL arg to indicate an exception
  if (exc) {
    JIT_DCHECK(
        arg == Py_None, "Arg should be None when injecting an exception");
    arg = NULL;
  } else {
    if (arg == NULL) {
      arg = Py_None;
    }
  }

  if (f) {
    // Setup tstate/frame as would be done in PyEval_EvalFrameEx() or
    // prologue of a JITed function.
    tstate->frame = f;
    f->f_executing = 1;
    // This compensates for the decref which occurs in JITRT_UnlinkFrame().
    Py_INCREF(f);
    // This satisfies code which uses f_lasti == -1 or < 0 to check if a
    // generator is not yet started, but still provides a garbage value in case
    // anything tries to actually use f_lasti.
    f->f_lasti = std::numeric_limits<int>::max();
  }

  // Enter generated code.
  JIT_DCHECK(
      gen_footer->yieldPoint != nullptr,
      "Attempting to resume a generator with no yield point");
  PyObject* result =
      gen_footer->resumeEntry((PyObject*)gen, arg, tstate, finish_yield_from);

  if (!result && (gen->gi_jit_data != nullptr)) {
    // Generator jit data (gen_footer) will be freed if the generator
    // deopts
    gen_footer->state = _PyJitGenState_Completed;
  }

  return result;
}

PyFrameObject* _PyJIT_GenMaterializeFrame(PyGenObject* gen) {
  if (gen->gi_frame) {
    PyFrameObject* frame = gen->gi_frame;
    Py_INCREF(frame);
    return frame;
  }
  PyThreadState* tstate = PyThreadState_Get();
  if (gen->gi_running) {
    PyFrameObject* frame = jit::materializePyFrameForGen(tstate, gen);
    Py_INCREF(frame);
    return frame;
  }
  auto gen_footer = reinterpret_cast<GenDataFooter*>(gen->gi_jit_data);
  if (gen_footer->state == _PyJitGenState_Completed) {
    return nullptr;
  }
  jit::CodeRuntime* code_rt = gen_footer->code_rt;
  PyFrameObject* frame =
      PyFrame_New(tstate, code_rt->GetCode(), code_rt->GetGlobals(), nullptr);
  JIT_CHECK(frame != nullptr, "failed allocating frame");
  // PyFrame_New links the frame into the thread stack.
  Py_CLEAR(frame->f_back);
  frame->f_gen = reinterpret_cast<PyObject*>(gen);
  Py_INCREF(frame);
  gen->gi_frame = frame;
  gen->gi_shadow_frame.data = _PyShadowFrame_MakeData(frame, PYSF_PYFRAME);
  return frame;
}

int _PyJIT_GenVisitRefs(PyGenObject* gen, visitproc visit, void* arg) {
  auto gen_footer = reinterpret_cast<GenDataFooter*>(gen->gi_jit_data);
  JIT_DCHECK(gen_footer, "Generator missing JIT data");
  if (gen_footer->state != _PyJitGenState_Completed && gen_footer->yieldPoint) {
    return reinterpret_cast<GenYieldPoint*>(gen_footer->yieldPoint)
        ->visitRefs(gen, visit, arg);
  }
  return 0;
}

void _PyJIT_GenDealloc(PyGenObject* gen) {
  auto gen_footer = reinterpret_cast<GenDataFooter*>(gen->gi_jit_data);
  JIT_DCHECK(gen_footer, "Generator missing JIT data");
  if (gen_footer->state != _PyJitGenState_Completed && gen_footer->yieldPoint) {
    reinterpret_cast<GenYieldPoint*>(gen_footer->yieldPoint)->releaseRefs(gen);
  }
  JITRT_GenJitDataFree(gen);
}

PyObject* _PyJIT_GenYieldFromValue(PyGenObject* gen) {
  auto gen_footer = reinterpret_cast<GenDataFooter*>(gen->gi_jit_data);
  JIT_DCHECK(gen_footer, "Generator missing JIT data");
  PyObject* yf = NULL;
  if (gen_footer->state != _PyJitGenState_Completed && gen_footer->yieldPoint) {
    yf = gen_footer->yieldPoint->yieldFromValue(gen_footer);
    Py_XINCREF(yf);
  }
  return yf;
}

PyObject* _PyJIT_GetGlobals(PyThreadState* tstate) {
  _PyShadowFrame* shadow_frame = tstate->shadow_frame;
  if (shadow_frame == nullptr) {
    JIT_CHECK(
        tstate->frame == nullptr,
        "py frame w/out corresponding shadow frame\n");
    return nullptr;
  }
  if (_PyShadowFrame_GetPtrKind(shadow_frame) == PYSF_PYFRAME) {
    return _PyShadowFrame_GetPyFrame(shadow_frame)->f_globals;
  }
  JIT_DCHECK(
      _PyShadowFrame_GetPtrKind(shadow_frame) == PYSF_CODE_RT,
      "Unexpected shadow frame type");
  jit::CodeRuntime* code_rt =
      static_cast<jit::CodeRuntime*>(_PyShadowFrame_GetPtr(shadow_frame));
  return code_rt->GetGlobals();
}

void _PyJIT_ProfileCurrentInstr(
    PyFrameObject* frame,
    PyObject** stack_top,
    int opcode,
    int oparg) {
  auto profile_stack = [&](auto... stack_offsets) {
    CodeProfile& code_profile =
        jit::codegen::NativeGeneratorFactory::runtime()
            ->typeProfiles()[Ref<PyCodeObject>{frame->f_code}];
    int opcode_offset = frame->f_lasti;

    auto pair = code_profile.typed_hits.emplace(opcode_offset, nullptr);
    if (pair.second) {
      constexpr int kProfilerRows = 4;
      pair.first->second =
          TypeProfiler::create(kProfilerRows, sizeof...(stack_offsets));
    }
    auto get_type = [&](int offset) {
      PyObject* obj = stack_top[-(offset + 1)];
      return obj != nullptr ? Py_TYPE(obj) : nullptr;
    };
    pair.first->second->recordTypes(get_type(stack_offsets)...);
  };

  switch (opcode) {
    case BEFORE_ASYNC_WITH:
    case DELETE_ATTR:
    case END_ASYNC_FOR:
    case END_FINALLY:
    case FOR_ITER:
    case GET_AITER:
    case GET_ANEXT:
    case GET_AWAITABLE:
    case GET_ITER:
    case GET_YIELD_FROM_ITER:
    case JUMP_IF_FALSE_OR_POP:
    case JUMP_IF_TRUE_OR_POP:
    case LOAD_ATTR:
    case LOAD_FIELD:
    case LOAD_METHOD:
    case POP_JUMP_IF_FALSE:
    case POP_JUMP_IF_TRUE:
    case RETURN_VALUE:
    case SETUP_WITH:
    case STORE_DEREF:
    case STORE_GLOBAL:
    case UNARY_INVERT:
    case UNARY_NEGATIVE:
    case UNARY_NOT:
    case UNARY_POSITIVE:
    case UNPACK_EX:
    case UNPACK_SEQUENCE:
    case WITH_CLEANUP_START:
    case YIELD_FROM:
    case YIELD_VALUE: {
      profile_stack(0);
      break;
    }
    case BINARY_ADD:
    case BINARY_AND:
    case BINARY_FLOOR_DIVIDE:
    case BINARY_LSHIFT:
    case BINARY_MATRIX_MULTIPLY:
    case BINARY_MODULO:
    case BINARY_MULTIPLY:
    case BINARY_OR:
    case BINARY_POWER:
    case BINARY_RSHIFT:
    case BINARY_SUBSCR:
    case BINARY_SUBTRACT:
    case BINARY_TRUE_DIVIDE:
    case BINARY_XOR:
    case COMPARE_OP:
    case DELETE_SUBSCR:
    case INPLACE_ADD:
    case INPLACE_AND:
    case INPLACE_FLOOR_DIVIDE:
    case INPLACE_LSHIFT:
    case INPLACE_MATRIX_MULTIPLY:
    case INPLACE_MODULO:
    case INPLACE_MULTIPLY:
    case INPLACE_OR:
    case INPLACE_POWER:
    case INPLACE_RSHIFT:
    case INPLACE_SUBTRACT:
    case INPLACE_TRUE_DIVIDE:
    case INPLACE_XOR:
    case LIST_APPEND:
    case MAP_ADD:
    case SET_ADD:
    case STORE_ATTR:
    case STORE_FIELD:
    case WITH_CLEANUP_FINISH: {
      profile_stack(1, 0);
      break;
    }
    case STORE_SUBSCR: {
      profile_stack(2, 1, 0);
      break;
    }
    case CALL_FUNCTION: {
      profile_stack(oparg);
      break;
    };
    case CALL_METHOD: {
      profile_stack(oparg, oparg + 1);
      break;
    }
  }
}

void _PyJIT_CountProfiledInstrs(PyCodeObject* code, Py_ssize_t count) {
  jit::codegen::NativeGeneratorFactory::runtime()
      ->typeProfiles()[Ref<PyCodeObject>{code}]
      .total_hits += count;
}

namespace {

// ProfileEnv and the functions below that use it are for building the
// complicated, nested data structure returned by
// _PyJIT_GetAndClearTypeProfiles().
struct ProfileEnv {
  // These members are applicable during the whole process:
  Ref<> stats_list;
  Ref<> other_list;
  Ref<> empty_list;
  UnorderedMap<BorrowedRef<PyTypeObject>, Ref<>> type_name_cache;

  // These members vary with each code object:
  BorrowedRef<PyCodeObject> code;
  Ref<> code_hash;
  Ref<> qualname;
  Ref<> firstlineno;

  // These members vary with each instruction:
  int64_t profiled_hits;
  Ref<> bc_offset;
  Ref<> opname;
  Ref<> lineno;
};

void init_env(ProfileEnv& env) {
  env.stats_list = Ref<>::steal(check(PyList_New(0)));
  env.other_list = Ref<>::steal(check(PyList_New(0)));
  auto other_str = Ref<>::steal(check(PyUnicode_InternFromString("<other>")));
  check(PyList_Append(env.other_list, other_str));
  env.empty_list = Ref<>::steal(check(PyList_New(0)));

  env.type_name_cache.emplace(
      nullptr, Ref<>::steal(check(PyUnicode_InternFromString("<NULL>"))));
}

PyObject* get_type_name(ProfileEnv& env, PyTypeObject* ty) {
  auto pair = env.type_name_cache.emplace(ty, nullptr);
  Ref<>& cached_name = pair.first->second;
  if (pair.second) {
    PyObject* module =
        ty->tp_dict ? PyDict_GetItemString(ty->tp_dict, "__module__") : nullptr;
    if (module != nullptr && PyUnicode_Check(module)) {
      cached_name = Ref<>::steal(
          check(PyUnicode_FromFormat("%U:%s", module, ty->tp_name)));
    } else {
      cached_name =
          Ref<>::steal(check(PyUnicode_InternFromString(ty->tp_name)));
    }
  }
  return cached_name;
}

void start_code(ProfileEnv& env, PyCodeObject* code) {
  env.code = code;
  env.code_hash =
      Ref<>::steal(check(PyLong_FromUnsignedLong(hashBytecode(code))));
  env.qualname.reset(code->co_qualname);
  if (env.qualname == nullptr) {
    env.qualname.reset(code->co_name);
    if (env.qualname == nullptr) {
      env.qualname =
          Ref<>::steal(check(PyUnicode_InternFromString("<unknown>")));
    }
  }
  env.firstlineno = Ref<>::steal(check(PyLong_FromLong(code->co_firstlineno)));
  env.profiled_hits = 0;
}

void start_instr(ProfileEnv& env, int bcoff_raw) {
  int lineno_raw = env.code->co_lnotab != nullptr
      ? PyCode_Addr2Line(env.code, bcoff_raw)
      : -1;
  int opcode = _Py_OPCODE(PyBytes_AS_STRING(env.code->co_code)[bcoff_raw]);
  env.bc_offset = Ref<>::steal(check(PyLong_FromLong(bcoff_raw)));
  env.lineno = Ref<>::steal(check(PyLong_FromLong(lineno_raw)));
  env.opname.reset(s_opnames.at(opcode));
}

void append_item(
    ProfileEnv& env,
    long count_raw,
    PyObject* type_names,
    bool use_op = true) {
  auto item = Ref<>::steal(check(PyDict_New()));
  auto normals = Ref<>::steal(check(PyDict_New()));
  auto ints = Ref<>::steal(check(PyDict_New()));
  auto count = Ref<>::steal(check(PyLong_FromLong(count_raw)));

  check(PyDict_SetItem(item, s_str_normal, normals));
  check(PyDict_SetItem(item, s_str_int, ints));
  check(PyDict_SetItem(normals, s_str_func_qualname, env.qualname));
  check(PyDict_SetItem(normals, s_str_filename, env.code->co_filename));
  check(PyDict_SetItem(ints, s_str_code_hash, env.code_hash));
  check(PyDict_SetItem(ints, s_str_firstlineno, env.firstlineno));
  check(PyDict_SetItem(ints, s_str_count, count));
  if (use_op) {
    check(PyDict_SetItem(ints, s_str_lineno, env.lineno));
    check(PyDict_SetItem(ints, s_str_bc_offset, env.bc_offset));
    check(PyDict_SetItem(normals, s_str_opname, env.opname));
  }
  if (type_names != nullptr) {
    auto normvectors = Ref<>::steal(check(PyDict_New()));
    check(PyDict_SetItem(normvectors, s_str_types, type_names));
    check(PyDict_SetItem(item, s_str_normvector, normvectors));
  }
  check(PyList_Append(env.stats_list, item));

  env.profiled_hits += count_raw;
}

void build_profile(ProfileEnv& env, TypeProfiles& profiles) {
  for (auto& code_pair : profiles) {
    start_code(env, code_pair.first);
    const CodeProfile& code_profile = code_pair.second;

    for (auto& profile_pair : code_profile.typed_hits) {
      const TypeProfiler& profile = *profile_pair.second;
      if (profile.empty()) {
        continue;
      }
      start_instr(env, profile_pair.first);

      for (int row = 0; row < profile.rows() && profile.count(row) != 0;
           ++row) {
        auto type_names = Ref<>::steal(check(PyList_New(0)));
        for (int col = 0; col < profile.cols(); ++col) {
          PyTypeObject* ty = profile.type(row, col);
          check(PyList_Append(type_names, get_type_name(env, ty)));
        }
        append_item(env, profile.count(row), type_names);
      }

      if (profile.other() > 0) {
        append_item(env, profile.other(), env.other_list);
      }
    }

    int64_t untyped_hits = code_profile.total_hits - env.profiled_hits;
    if (untyped_hits != 0) {
      append_item(env, untyped_hits, nullptr, false);
    }
  }
}

} // namespace

PyObject* _PyJIT_GetAndClearTypeProfiles() {
  auto& profiles =
      jit::codegen::NativeGeneratorFactory::runtime()->typeProfiles();
  ProfileEnv env;

  try {
    init_env(env);
    build_profile(env, profiles);
  } catch (const CAPIError&) {
    return nullptr;
  }

  profiles.clear();
  return env.stats_list.release();
}

void _PyJIT_ClearTypeProfiles() {
  jit::codegen::NativeGeneratorFactory::runtime()->typeProfiles().clear();
}
