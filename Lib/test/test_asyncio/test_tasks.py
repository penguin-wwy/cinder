"""Tests for tasks.py."""

import _testcapi
import cinder
import collections
import contextlib
import contextvars
import functools
import gc
import io
import random
import re
import sys
import textwrap
import types
import unittest
import weakref
from unittest import mock

import asyncio
import _asyncio
from _asyncio import ContextAwareTask
from asyncio import coroutines
from asyncio import futures
from asyncio import tasks
from test.test_asyncio import utils as test_utils
from test import support
from test.support.script_helper import assert_python_ok
from _testcapi import (
    AcquireContextPtr,
    DoStepPtr,
    GetContextRef,
    get_context_indirect,
    get_context_indirect,
    initialize_context_helpers,
    get_context,
    modify_context as _modify_current_context,
    delete_context,
    get_context_helpers_for_task,
)

def tearDownModule():
    asyncio.set_event_loop_policy(None)


async def coroutine_function():
    pass


@contextlib.contextmanager
def set_coroutine_debug(enabled):
    coroutines = asyncio.coroutines

    old_debug = coroutines._DEBUG
    try:
        coroutines._DEBUG = enabled
        yield
    finally:
        coroutines._DEBUG = old_debug


def format_coroutine(qualname, state, src, source_traceback, generator=False):
    if generator:
        state = '%s' % state
    else:
        state = '%s, defined' % state
    if source_traceback is not None:
        frame = source_traceback[-1]
        return ('coro=<%s() %s at %s> created at %s:%s'
                % (qualname, state, src, frame[0], frame[1]))
    else:
        return 'coro=<%s() %s at %s>' % (qualname, state, src)


class Dummy:

    def __repr__(self):
        return '<Dummy>'

    def __call__(self, *args):
        pass


class CoroLikeObject:
    def send(self, v):
        raise StopIteration(42)

    def throw(self, *exc):
        pass

    def close(self):
        pass

    def __await__(self):
        return self


class BaseTaskTests:

    Task = None
    Future = None

    def new_task(self, loop, coro, name='TestTask'):
        return self.__class__.Task(coro, loop=loop, name=name)

    def new_future(self, loop):
        return self.__class__.Future(loop=loop)

    def setUp(self):
        super().setUp()
        self.loop = self.new_test_loop()
        self.loop.set_task_factory(self.new_task)
        self.loop.create_future = lambda: self.new_future(self.loop)

    def test_task_del_collect(self):
        class Evil:
            def __del__(self):
                gc.collect()

        async def run():
            return Evil()

        self.loop.run_until_complete(
            asyncio.gather(*[
                self.new_task(self.loop, run()) for _ in range(100)
            ], loop=self.loop))

    def test_other_loop_future(self):
        other_loop = asyncio.new_event_loop()
        fut = self.new_future(other_loop)

        async def run(fut):
            await fut

        try:
            with self.assertRaisesRegex(RuntimeError,
                                        r'Task .* got Future .* attached'):
                self.loop.run_until_complete(run(fut))
        finally:
            other_loop.close()

    def test_task_awaits_on_itself(self):

        async def test():
            await task

        task = asyncio.ensure_future(test(), loop=self.loop)

        with self.assertRaisesRegex(RuntimeError,
                                    'Task cannot await on itself'):
            self.loop.run_until_complete(task)

    def test_task_class(self):
        async def notmuch():
            return 'ok'
        t = self.new_task(self.loop, notmuch())
        self.loop.run_until_complete(t)
        self.assertTrue(t.done())
        self.assertEqual(t.result(), 'ok')
        self.assertIs(t._loop, self.loop)
        self.assertIs(t.get_loop(), self.loop)

        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)
        t = self.new_task(loop, notmuch())
        self.assertIs(t._loop, loop)
        loop.run_until_complete(t)
        loop.close()

    def test_ensure_future_coroutine(self):
        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def notmuch():
                return 'ok'
        t = asyncio.ensure_future(notmuch(), loop=self.loop)
        self.loop.run_until_complete(t)
        self.assertTrue(t.done())
        self.assertEqual(t.result(), 'ok')
        self.assertIs(t._loop, self.loop)

        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)
        t = asyncio.ensure_future(notmuch(), loop=loop)
        self.assertIs(t._loop, loop)
        loop.run_until_complete(t)
        loop.close()

    def test_ensure_future_future(self):
        f_orig = self.new_future(self.loop)
        f_orig.set_result('ko')

        f = asyncio.ensure_future(f_orig)
        self.loop.run_until_complete(f)
        self.assertTrue(f.done())
        self.assertEqual(f.result(), 'ko')
        self.assertIs(f, f_orig)

        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)

        with self.assertRaises(ValueError):
            f = asyncio.ensure_future(f_orig, loop=loop)

        loop.close()

        f = asyncio.ensure_future(f_orig, loop=self.loop)
        self.assertIs(f, f_orig)

    def test_ensure_future_task(self):
        async def notmuch():
            return 'ok'
        t_orig = self.new_task(self.loop, notmuch())
        t = asyncio.ensure_future(t_orig)
        self.loop.run_until_complete(t)
        self.assertTrue(t.done())
        self.assertEqual(t.result(), 'ok')
        self.assertIs(t, t_orig)

        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)

        with self.assertRaises(ValueError):
            t = asyncio.ensure_future(t_orig, loop=loop)

        loop.close()

        t = asyncio.ensure_future(t_orig, loop=self.loop)
        self.assertIs(t, t_orig)

    def test_ensure_future_awaitable(self):
        class Aw:
            def __init__(self, coro):
                self.coro = coro
            def __await__(self):
                return (yield from self.coro)

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro():
                return 'ok'

        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)
        fut = asyncio.ensure_future(Aw(coro()), loop=loop)
        loop.run_until_complete(fut)
        assert fut.result() == 'ok'

    def test_ensure_future_neither(self):
        with self.assertRaises(TypeError):
            asyncio.ensure_future('ok')

    def test_ensure_future_error_msg(self):
        loop = asyncio.new_event_loop()
        f = self.new_future(self.loop)
        with self.assertRaisesRegex(ValueError, 'The future belongs to a '
                                    'different loop than the one specified as '
                                    'the loop argument'):
            asyncio.ensure_future(f, loop=loop)
        loop.close()

    def test_get_stack(self):
        T = None

        async def foo():
            await bar()

        async def bar():
            # test get_stack()
            f = T.get_stack(limit=1)
            try:
                self.assertEqual(f[0].f_code.co_name, 'foo')
            finally:
                f = None

            # test print_stack()
            file = io.StringIO()
            T.print_stack(limit=1, file=file)
            file.seek(0)
            tb = file.read()
            self.assertRegex(tb, r'foo\(\) running')

        async def runner():
            nonlocal T
            T = asyncio.ensure_future(foo(), loop=self.loop)
            await T

        self.loop.run_until_complete(runner())

    def test_task_repr(self):
        self.loop.set_debug(False)

        async def notmuch():
            return 'abc'

        # test coroutine function
        self.assertEqual(notmuch.__name__, 'notmuch')
        self.assertRegex(notmuch.__qualname__,
                         r'\w+.test_task_repr.<locals>.notmuch')
        self.assertEqual(notmuch.__module__, __name__)

        filename, lineno = test_utils.get_function_source(notmuch)
        src = "%s:%s" % (filename, lineno)

        # test coroutine object
        gen = notmuch()
        coro_qualname = 'BaseTaskTests.test_task_repr.<locals>.notmuch'
        self.assertEqual(gen.__name__, 'notmuch')
        self.assertEqual(gen.__qualname__, coro_qualname)

        # test pending Task
        t = self.new_task(self.loop, gen)
        t.add_done_callback(Dummy())

        coro = format_coroutine(coro_qualname, 'running', src,
                                t._source_traceback, generator=True)
        self.assertEqual(repr(t),
                         "<%s pending name='TestTask' %s cb=[<Dummy>()]>" % (self.Task.__name__, coro))

        # test cancelling Task
        t.cancel()  # Does not take immediate effect!
        self.assertEqual(repr(t),
                         "<%s cancelling name='TestTask' %s cb=[<Dummy>()]>" % (self.Task.__name__, coro))

        # test cancelled Task
        self.assertRaises(asyncio.CancelledError,
                          self.loop.run_until_complete, t)
        coro = format_coroutine(coro_qualname, 'done', src,
                                t._source_traceback)
        self.assertEqual(repr(t),
                         "<%s cancelled name='TestTask' %s>" % (self.Task.__name__, coro))

        # test finished Task
        t = self.new_task(self.loop, notmuch())
        self.loop.run_until_complete(t)
        coro = format_coroutine(coro_qualname, 'done', src,
                                t._source_traceback)
        self.assertEqual(repr(t),
                         "<%s finished name='TestTask' %s result='abc'>" % (self.Task.__name__, coro))

    def test_task_repr_autogenerated(self):
        async def notmuch():
            return 123

        t1 = self.new_task(self.loop, notmuch(), None)
        t2 = self.new_task(self.loop, notmuch(), None)
        self.assertNotEqual(repr(t1), repr(t2))

        match1 = re.match(rf"^<{self.Task.__name__} pending name='Task-(\d+)'", repr(t1))
        self.assertIsNotNone(match1)
        match2 = re.match(rf"^<{self.Task.__name__} pending name='Task-(\d+)'", repr(t2))
        self.assertIsNotNone(match2)

        # Autogenerated task names should have monotonically increasing numbers
        self.assertLess(int(match1.group(1)), int(match2.group(1)))
        self.loop.run_until_complete(t1)
        self.loop.run_until_complete(t2)

    def test_task_repr_name_not_str(self):
        async def notmuch():
            return 123

        t = self.new_task(self.loop, notmuch())
        t.set_name({6})
        self.assertEqual(t.get_name(), '{6}')
        self.loop.run_until_complete(t)

    @unittest.skipUnderCinderJIT("Incorrect coro naming (T86183012)")
    def test_task_repr_coro_decorator(self):
        self.loop.set_debug(False)

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def notmuch():
                # notmuch() function doesn't use yield from: it will be wrapped by
                # @coroutine decorator
                return 123

        # test coroutine function
        self.assertEqual(notmuch.__name__, 'notmuch')
        self.assertRegex(notmuch.__qualname__,
                         r'\w+.test_task_repr_coro_decorator'
                         r'\.<locals>\.notmuch')
        self.assertEqual(notmuch.__module__, __name__)

        # test coroutine object
        gen = notmuch()
        # On Python >= 3.5, generators now inherit the name of the
        # function, as expected, and have a qualified name (__qualname__
        # attribute).
        coro_name = 'notmuch'
        coro_qualname = ('BaseTaskTests.test_task_repr_coro_decorator'
                         '.<locals>.notmuch')
        self.assertEqual(gen.__name__, coro_name)
        self.assertEqual(gen.__qualname__, coro_qualname)

        # test repr(CoroWrapper)
        if coroutines._DEBUG:
            # format the coroutine object
            if coroutines._DEBUG:
                filename, lineno = test_utils.get_function_source(notmuch)
                frame = gen._source_traceback[-1]
                coro = ('%s() running, defined at %s:%s, created at %s:%s'
                        % (coro_qualname, filename, lineno,
                           frame[0], frame[1]))
            else:
                code = gen.gi_code
                coro = ('%s() running at %s:%s'
                        % (coro_qualname, code.co_filename,
                           code.co_firstlineno))

            self.assertEqual(repr(gen), '<CoroWrapper %s>' % coro)

        # test pending Task
        t = self.new_task(self.loop, gen)
        t.add_done_callback(Dummy())

        # format the coroutine object
        if coroutines._DEBUG:
            src = '%s:%s' % test_utils.get_function_source(notmuch)
        else:
            code = gen.gi_code
            src = '%s:%s' % (code.co_filename, code.co_firstlineno)
        coro = format_coroutine(coro_qualname, 'running', src,
                                t._source_traceback,
                                generator=not coroutines._DEBUG)
        self.assertEqual(repr(t),
                         "<%s pending name='TestTask' %s cb=[<Dummy>()]>" % (self.Task.__name__, coro))
        self.loop.run_until_complete(t)

    def test_task_repr_wait_for(self):
        self.loop.set_debug(False)

        async def wait_for(fut):
            return await fut

        fut = self.new_future(self.loop)
        task = self.new_task(self.loop, wait_for(fut))
        test_utils.run_briefly(self.loop)
        self.assertRegex(repr(task),
                         '<%s .* wait_for=%s>' % (self.Task.__name__, re.escape(repr(fut))))

        fut.set_result(None)
        self.loop.run_until_complete(task)

    def test_task_repr_partial_corowrapper(self):
        # Issue #222: repr(CoroWrapper) must not fail in debug mode if the
        # coroutine is a partial function
        with set_coroutine_debug(True):
            self.loop.set_debug(True)

            async def func(x, y):
                await asyncio.sleep(0)

            with self.assertWarns(DeprecationWarning):
                partial_func = asyncio.coroutine(functools.partial(func, 1))
            task = self.loop.create_task(partial_func(2))

            # make warnings quiet
            task._log_destroy_pending = False
            self.addCleanup(task._coro.close)

        coro_repr = repr(task._coro)
        expected = (
            r'<coroutine object \w+\.test_task_repr_partial_corowrapper'
            r'\.<locals>\.func at'
        )
        self.assertRegex(coro_repr, expected)

    def test_task_basics(self):

        async def outer():
            a = await inner1()
            b = await inner2()
            return a+b

        async def inner1():
            return 42

        async def inner2():
            return 1000

        t = outer()
        self.assertEqual(self.loop.run_until_complete(t), 1042)

    def test_cancel(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            yield 0

        loop = self.new_test_loop(gen)

        async def task():
            await asyncio.sleep(10.0)
            return 12

        t = self.new_task(loop, task())
        loop.call_soon(t.cancel)
        with self.assertRaises(asyncio.CancelledError):
            loop.run_until_complete(t)
        self.assertTrue(t.done())
        self.assertTrue(t.cancelled())
        self.assertFalse(t.cancel())

    def test_cancel_yield(self):
        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def task():
                yield
                yield
                return 12

        t = self.new_task(self.loop, task())
        test_utils.run_briefly(self.loop)  # start coro
        t.cancel()
        self.assertRaises(
            asyncio.CancelledError, self.loop.run_until_complete, t)
        self.assertTrue(t.done())
        self.assertTrue(t.cancelled())
        self.assertFalse(t.cancel())

    def test_cancel_inner_future(self):
        f = self.new_future(self.loop)

        async def task():
            await f
            return 12

        t = self.new_task(self.loop, task())
        test_utils.run_briefly(self.loop)  # start task
        f.cancel()
        with self.assertRaises(asyncio.CancelledError):
            self.loop.run_until_complete(t)
        self.assertTrue(f.cancelled())
        self.assertTrue(t.cancelled())

    def test_cancel_both_task_and_inner_future(self):
        f = self.new_future(self.loop)

        async def task():
            await f
            return 12

        t = self.new_task(self.loop, task())
        test_utils.run_briefly(self.loop)

        f.cancel()
        t.cancel()

        with self.assertRaises(asyncio.CancelledError):
            self.loop.run_until_complete(t)

        self.assertTrue(t.done())
        self.assertTrue(f.cancelled())
        self.assertTrue(t.cancelled())

    def test_cancel_task_catching(self):
        fut1 = self.new_future(self.loop)
        fut2 = self.new_future(self.loop)

        async def task():
            await fut1
            try:
                await fut2
            except asyncio.CancelledError:
                return 42

        t = self.new_task(self.loop, task())
        test_utils.run_briefly(self.loop)
        self.assertIs(t._fut_waiter, fut1)  # White-box test.
        fut1.set_result(None)
        test_utils.run_briefly(self.loop)
        self.assertIs(t._fut_waiter, fut2)  # White-box test.
        t.cancel()
        self.assertTrue(fut2.cancelled())
        res = self.loop.run_until_complete(t)
        self.assertEqual(res, 42)
        self.assertFalse(t.cancelled())

    def test_cancel_task_ignoring(self):
        fut1 = self.new_future(self.loop)
        fut2 = self.new_future(self.loop)
        fut3 = self.new_future(self.loop)

        async def task():
            await fut1
            try:
                await fut2
            except asyncio.CancelledError:
                pass
            res = await fut3
            return res

        t = self.new_task(self.loop, task())
        test_utils.run_briefly(self.loop)
        self.assertIs(t._fut_waiter, fut1)  # White-box test.
        fut1.set_result(None)
        test_utils.run_briefly(self.loop)
        self.assertIs(t._fut_waiter, fut2)  # White-box test.
        t.cancel()
        self.assertTrue(fut2.cancelled())
        test_utils.run_briefly(self.loop)
        self.assertIs(t._fut_waiter, fut3)  # White-box test.
        fut3.set_result(42)
        res = self.loop.run_until_complete(t)
        self.assertEqual(res, 42)
        self.assertFalse(fut3.cancelled())
        self.assertFalse(t.cancelled())

    def test_cancel_current_task(self):
        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)

        async def task():
            t.cancel()
            self.assertTrue(t._must_cancel)  # White-box test.
            # The sleep should be cancelled immediately.
            await asyncio.sleep(100)
            return 12

        t = self.new_task(loop, task())
        self.assertFalse(t.cancelled())
        self.assertRaises(
            asyncio.CancelledError, loop.run_until_complete, t)
        self.assertTrue(t.done())
        self.assertTrue(t.cancelled())
        self.assertFalse(t._must_cancel)  # White-box test.
        self.assertFalse(t.cancel())

    def test_cancel_at_end(self):
        """coroutine end right after task is cancelled"""
        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)

        async def task():
            t.cancel()
            self.assertTrue(t._must_cancel)  # White-box test.
            return 12

        t = self.new_task(loop, task())
        self.assertFalse(t.cancelled())
        self.assertRaises(
            asyncio.CancelledError, loop.run_until_complete, t)
        self.assertTrue(t.done())
        self.assertTrue(t.cancelled())
        self.assertFalse(t._must_cancel)  # White-box test.
        self.assertFalse(t.cancel())

    def test_cancel_awaited_task(self):
        # This tests for a relatively rare condition when
        # a task cancellation is requested for a task which is not
        # currently blocked, such as a task cancelling itself.
        # In this situation we must ensure that whatever next future
        # or task the cancelled task blocks on is cancelled correctly
        # as well.  See also bpo-34872.
        loop = asyncio.new_event_loop()
        self.addCleanup(lambda: loop.close())

        task = nested_task = None
        fut = self.new_future(loop)

        async def nested():
            await fut

        async def coro():
            nonlocal nested_task
            # Create a sub-task and wait for it to run.
            nested_task = self.new_task(loop, nested())
            await asyncio.sleep(0)

            # Request the current task to be cancelled.
            task.cancel()
            # Block on the nested task, which should be immediately
            # cancelled.
            await nested_task

        task = self.new_task(loop, coro())
        with self.assertRaises(asyncio.CancelledError):
            loop.run_until_complete(task)

        self.assertTrue(task.cancelled())
        self.assertTrue(nested_task.cancelled())
        self.assertTrue(fut.cancelled())

    def test_stop_while_run_in_complete(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0.1
            self.assertAlmostEqual(0.2, when)
            when = yield 0.1
            self.assertAlmostEqual(0.3, when)
            yield 0.1

        loop = self.new_test_loop(gen)

        x = 0

        async def task():
            nonlocal x
            while x < 10:
                await asyncio.sleep(0.1)
                x += 1
                if x == 2:
                    loop.stop()

        t = self.new_task(loop, task())
        with self.assertRaises(RuntimeError) as cm:
            loop.run_until_complete(t)
        self.assertEqual(str(cm.exception),
                         'Event loop stopped before Future completed.')
        self.assertFalse(t.done())
        self.assertEqual(x, 2)
        self.assertAlmostEqual(0.3, loop.time())

        t.cancel()
        self.assertRaises(asyncio.CancelledError, loop.run_until_complete, t)

    def test_log_traceback(self):
        async def coro():
            pass

        task = self.new_task(self.loop, coro())
        with self.assertRaisesRegex(ValueError, 'can only be set to False'):
            task._log_traceback = True
        self.loop.run_until_complete(task)

    def test_wait_for_timeout_less_then_0_or_0_future_done(self):
        def gen():
            when = yield
            self.assertAlmostEqual(0, when)

        loop = self.new_test_loop(gen)

        fut = self.new_future(loop)
        fut.set_result('done')

        ret = loop.run_until_complete(asyncio.wait_for(fut, 0))

        self.assertEqual(ret, 'done')
        self.assertTrue(fut.done())
        self.assertAlmostEqual(0, loop.time())

    def test_wait_for_timeout_less_then_0_or_0_coroutine_do_not_started(self):
        def gen():
            when = yield
            self.assertAlmostEqual(0, when)

        loop = self.new_test_loop(gen)

        foo_started = False

        async def foo():
            nonlocal foo_started
            foo_started = True

        with self.assertRaises(asyncio.TimeoutError):
            loop.run_until_complete(asyncio.wait_for(foo(), 0))

        self.assertAlmostEqual(0, loop.time())
        self.assertEqual(foo_started, False)

    def test_wait_for_timeout_less_then_0_or_0(self):
        def gen():
            when = yield
            self.assertAlmostEqual(0.2, when)
            when = yield 0
            self.assertAlmostEqual(0, when)

        for timeout in [0, -1]:
            with self.subTest(timeout=timeout):
                loop = self.new_test_loop(gen)

                foo_running = None

                async def foo():
                    nonlocal foo_running
                    foo_running = True
                    try:
                        await asyncio.sleep(0.2)
                    finally:
                        foo_running = False
                    return 'done'

                fut = self.new_task(loop, foo())

                with self.assertRaises(asyncio.TimeoutError):
                    loop.run_until_complete(asyncio.wait_for(fut, timeout))
                self.assertTrue(fut.done())
                # it should have been cancelled due to the timeout
                self.assertTrue(fut.cancelled())
                self.assertAlmostEqual(0, loop.time())
                self.assertEqual(foo_running, False)

    def test_wait_for(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.2, when)
            when = yield 0
            self.assertAlmostEqual(0.1, when)
            when = yield 0.1

        loop = self.new_test_loop(gen)

        foo_running = None

        async def foo():
            nonlocal foo_running
            foo_running = True
            try:
                await asyncio.sleep(0.2)
            finally:
                foo_running = False
            return 'done'

        fut = self.new_task(loop, foo())

        with self.assertRaises(asyncio.TimeoutError):
            loop.run_until_complete(asyncio.wait_for(fut, 0.1))
        self.assertTrue(fut.done())
        # it should have been cancelled due to the timeout
        self.assertTrue(fut.cancelled())
        self.assertAlmostEqual(0.1, loop.time())
        self.assertEqual(foo_running, False)

    def test_wait_for_blocking(self):
        loop = self.new_test_loop()

        async def coro():
            return 'done'

        res = loop.run_until_complete(asyncio.wait_for(coro(), timeout=None))
        self.assertEqual(res, 'done')

    def test_wait_for_with_global_loop(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.2, when)
            when = yield 0
            self.assertAlmostEqual(0.01, when)
            yield 0.01

        loop = self.new_test_loop(gen)

        async def foo():
            await asyncio.sleep(0.2)
            return 'done'

        asyncio.set_event_loop(loop)
        try:
            fut = self.new_task(loop, foo())
            with self.assertRaises(asyncio.TimeoutError):
                loop.run_until_complete(asyncio.wait_for(fut, 0.01))
        finally:
            asyncio.set_event_loop(None)

        self.assertAlmostEqual(0.01, loop.time())
        self.assertTrue(fut.done())
        self.assertTrue(fut.cancelled())

    def test_wait_for_race_condition(self):

        def gen():
            yield 0.1
            yield 0.1
            yield 0.1

        loop = self.new_test_loop(gen)

        fut = self.new_future(loop)
        task = asyncio.wait_for(fut, timeout=0.2)
        loop.call_later(0.1, fut.set_result, "ok")
        res = loop.run_until_complete(task)
        self.assertEqual(res, "ok")

    def test_wait_for_waits_for_task_cancellation(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        task_done = False

        async def foo():
            async def inner():
                nonlocal task_done
                try:
                    await asyncio.sleep(0.2)
                finally:
                    task_done = True

            inner_task = self.new_task(loop, inner())

            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(inner_task, timeout=0.1)

            self.assertTrue(task_done)

        loop.run_until_complete(foo())

    def test_wait_for_self_cancellation(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        async def foo():
            async def inner():
                try:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    try:
                        await asyncio.sleep(0.3)
                    except asyncio.CancelledError:
                        await asyncio.sleep(0.3)

                return 42

            inner_task = self.new_task(loop, inner())

            wait = asyncio.wait_for(inner_task, timeout=0.1)

            # Test that wait_for itself is properly cancellable
            # even when the initial task holds up the initial cancellation.
            task = self.new_task(loop, wait)
            await asyncio.sleep(0.2)
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(await inner_task, 42)

        loop.run_until_complete(foo())

    def test_wait(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0
            self.assertAlmostEqual(0.15, when)
            yield 0.15

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(0.1))
        b = self.new_task(loop, asyncio.sleep(0.15))

        async def foo():
            done, pending = await asyncio.wait([b, a])
            self.assertEqual(done, set([a, b]))
            self.assertEqual(pending, set())
            return 42

        res = loop.run_until_complete(self.new_task(loop, foo()))
        self.assertEqual(res, 42)
        self.assertAlmostEqual(0.15, loop.time())

        # Doing it again should take no time and exercise a different path.
        res = loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.15, loop.time())
        self.assertEqual(res, 42)

    def test_wait_with_global_loop(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.01, when)
            when = yield 0
            self.assertAlmostEqual(0.015, when)
            yield 0.015

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(0.01))
        b = self.new_task(loop, asyncio.sleep(0.015))

        async def foo():
            done, pending = await asyncio.wait([b, a])
            self.assertEqual(done, set([a, b]))
            self.assertEqual(pending, set())
            return 42

        asyncio.set_event_loop(loop)
        res = loop.run_until_complete(
            self.new_task(loop, foo()))

        self.assertEqual(res, 42)

    def test_wait_duplicate_coroutines(self):

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro(s):
                return s
        c = coro('test')

        task =self.new_task(
            self.loop,
            asyncio.wait([c, c, coro('spam')]))

        done, pending = self.loop.run_until_complete(task)

        self.assertFalse(pending)
        self.assertEqual(set(f.result() for f in done), {'test', 'spam'})

    def test_wait_errors(self):
        self.assertRaises(
            ValueError, self.loop.run_until_complete,
            asyncio.wait(set()))

        # -1 is an invalid return_when value
        sleep_coro = asyncio.sleep(10.0)
        wait_coro = asyncio.wait([sleep_coro], return_when=-1)
        self.assertRaises(ValueError,
                          self.loop.run_until_complete, wait_coro)

        sleep_coro.close()

    def test_wait_first_completed(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            when = yield 0
            self.assertAlmostEqual(0.1, when)
            yield 0.1

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(10.0))
        b = self.new_task(loop, asyncio.sleep(0.1))
        task = self.new_task(
            loop,
            asyncio.wait([b, a], return_when=asyncio.FIRST_COMPLETED))

        done, pending = loop.run_until_complete(task)
        self.assertEqual({b}, done)
        self.assertEqual({a}, pending)
        self.assertFalse(a.done())
        self.assertTrue(b.done())
        self.assertIsNone(b.result())
        self.assertAlmostEqual(0.1, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_wait_really_done(self):
        # there is possibility that some tasks in the pending list
        # became done but their callbacks haven't all been called yet

        async def coro1():
            await asyncio.sleep(0)

        async def coro2():
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        a = self.new_task(self.loop, coro1())
        b = self.new_task(self.loop, coro2())
        task = self.new_task(
            self.loop,
            asyncio.wait([b, a], return_when=asyncio.FIRST_COMPLETED))

        done, pending = self.loop.run_until_complete(task)
        self.assertEqual({a, b}, done)
        self.assertTrue(a.done())
        self.assertIsNone(a.result())
        self.assertTrue(b.done())
        self.assertIsNone(b.result())

    def test_wait_first_exception(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            yield 0

        loop = self.new_test_loop(gen)

        # first_exception, task already has exception
        a = self.new_task(loop, asyncio.sleep(10.0))

        async def exc():
            raise ZeroDivisionError('err')

        b = self.new_task(loop, exc())
        task = self.new_task(
            loop,
            asyncio.wait([b, a], return_when=asyncio.FIRST_EXCEPTION))

        done, pending = loop.run_until_complete(task)
        self.assertEqual({b}, done)
        self.assertEqual({a}, pending)
        self.assertAlmostEqual(0, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_wait_first_exception_in_wait(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            when = yield 0
            self.assertAlmostEqual(0.01, when)
            yield 0.01

        loop = self.new_test_loop(gen)

        # first_exception, exception during waiting
        a = self.new_task(loop, asyncio.sleep(10.0))

        async def exc():
            await asyncio.sleep(0.01)
            raise ZeroDivisionError('err')

        b = self.new_task(loop, exc())
        task = asyncio.wait([b, a], return_when=asyncio.FIRST_EXCEPTION)

        done, pending = loop.run_until_complete(task)
        self.assertEqual({b}, done)
        self.assertEqual({a}, pending)
        self.assertAlmostEqual(0.01, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_wait_with_exception(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0
            self.assertAlmostEqual(0.15, when)
            yield 0.15

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(0.1))

        async def sleeper():
            await asyncio.sleep(0.15)
            raise ZeroDivisionError('really')

        b = self.new_task(loop, sleeper())

        async def foo():
            done, pending = await asyncio.wait([b, a])
            self.assertEqual(len(done), 2)
            self.assertEqual(pending, set())
            errors = set(f for f in done if f.exception() is not None)
            self.assertEqual(len(errors), 1)

        loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.15, loop.time())

        loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.15, loop.time())

    def test_wait_with_timeout(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0
            self.assertAlmostEqual(0.15, when)
            when = yield 0
            self.assertAlmostEqual(0.11, when)
            yield 0.11

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(0.1))
        b = self.new_task(loop, asyncio.sleep(0.15))

        async def foo():
            done, pending = await asyncio.wait([b, a], timeout=0.11)
            self.assertEqual(done, set([a]))
            self.assertEqual(pending, set([b]))

        loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.11, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_wait_concurrent_complete(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0
            self.assertAlmostEqual(0.15, when)
            when = yield 0
            self.assertAlmostEqual(0.1, when)
            yield 0.1

        loop = self.new_test_loop(gen)

        a = self.new_task(loop, asyncio.sleep(0.1))
        b = self.new_task(loop, asyncio.sleep(0.15))

        done, pending = loop.run_until_complete(
            asyncio.wait([b, a], timeout=0.1))

        self.assertEqual(done, set([a]))
        self.assertEqual(pending, set([b]))
        self.assertAlmostEqual(0.1, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_as_completed(self):

        def gen():
            yield 0
            yield 0
            yield 0.01
            yield 0

        loop = self.new_test_loop(gen)
        # disable "slow callback" warning
        loop.slow_callback_duration = 1.0
        completed = set()
        time_shifted = False

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def sleeper(dt, x):
                nonlocal time_shifted
                yield from  asyncio.sleep(dt)
                completed.add(x)
                if not time_shifted and 'a' in completed and 'b' in completed:
                    time_shifted = True
                    loop.advance_time(0.14)
                return x

        a = sleeper(0.01, 'a')
        b = sleeper(0.01, 'b')
        c = sleeper(0.15, 'c')

        async def foo():
            values = []
            for f in asyncio.as_completed([b, c, a], loop=loop):
                values.append(await f)
            return values
        with self.assertWarns(DeprecationWarning):
            res = loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.15, loop.time())
        self.assertTrue('a' in res[:2])
        self.assertTrue('b' in res[:2])
        self.assertEqual(res[2], 'c')

        # Doing it again should take no time and exercise a different path.
        with self.assertWarns(DeprecationWarning):
            res = loop.run_until_complete(self.new_task(loop, foo()))
        self.assertAlmostEqual(0.15, loop.time())

    def test_as_completed_with_timeout(self):

        def gen():
            yield
            yield 0
            yield 0
            yield 0.1

        loop = self.new_test_loop(gen)

        a = loop.create_task(asyncio.sleep(0.1, 'a'))
        b = loop.create_task(asyncio.sleep(0.15, 'b'))

        async def foo():
            values = []
            for f in asyncio.as_completed([a, b], timeout=0.12, loop=loop):
                if values:
                    loop.advance_time(0.02)
                try:
                    v = await f
                    values.append((1, v))
                except asyncio.TimeoutError as exc:
                    values.append((2, exc))
            return values

        with self.assertWarns(DeprecationWarning):
            res = loop.run_until_complete(self.new_task(loop, foo()))
        self.assertEqual(len(res), 2, res)
        self.assertEqual(res[0], (1, 'a'))
        self.assertEqual(res[1][0], 2)
        self.assertIsInstance(res[1][1], asyncio.TimeoutError)
        self.assertAlmostEqual(0.12, loop.time())

        # move forward to close generator
        loop.advance_time(10)
        loop.run_until_complete(asyncio.wait([a, b]))

    def test_as_completed_with_unused_timeout(self):

        def gen():
            yield
            yield 0
            yield 0.01

        loop = self.new_test_loop(gen)

        a = asyncio.sleep(0.01, 'a')

        async def foo():
            for f in asyncio.as_completed([a], timeout=1, loop=loop):
                v = await f
                self.assertEqual(v, 'a')

        with self.assertWarns(DeprecationWarning):
            loop.run_until_complete(self.new_task(loop, foo()))

    def test_as_completed_reverse_wait(self):

        def gen():
            yield 0
            yield 0.05
            yield 0

        loop = self.new_test_loop(gen)

        a = asyncio.sleep(0.05, 'a')
        b = asyncio.sleep(0.10, 'b')
        fs = {a, b}

        with self.assertWarns(DeprecationWarning):
            futs = list(asyncio.as_completed(fs, loop=loop))
        self.assertEqual(len(futs), 2)

        x = loop.run_until_complete(futs[1])
        self.assertEqual(x, 'a')
        self.assertAlmostEqual(0.05, loop.time())
        loop.advance_time(0.05)
        y = loop.run_until_complete(futs[0])
        self.assertEqual(y, 'b')
        self.assertAlmostEqual(0.10, loop.time())

    def test_as_completed_concurrent(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.05, when)
            when = yield 0
            self.assertAlmostEqual(0.05, when)
            yield 0.05

        loop = self.new_test_loop(gen)

        a = asyncio.sleep(0.05, 'a')
        b = asyncio.sleep(0.05, 'b')
        fs = {a, b}
        with self.assertWarns(DeprecationWarning):
            futs = list(asyncio.as_completed(fs, loop=loop))
        self.assertEqual(len(futs), 2)
        waiter = asyncio.wait(futs)
        done, pending = loop.run_until_complete(waiter)
        self.assertEqual(set(f.result() for f in done), {'a', 'b'})

    def test_as_completed_duplicate_coroutines(self):

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro(s):
                return s

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def runner():
                result = []
                c = coro('ham')
                for f in asyncio.as_completed([c, c, coro('spam')],
                                              loop=self.loop):
                    result.append((yield from f))
                return result

        with self.assertWarns(DeprecationWarning):
            fut = self.new_task(self.loop, runner())
            self.loop.run_until_complete(fut)
        result = fut.result()
        self.assertEqual(set(result), {'ham', 'spam'})
        self.assertEqual(len(result), 2)

    def test_sleep(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.05, when)
            when = yield 0.05
            self.assertAlmostEqual(0.1, when)
            yield 0.05

        loop = self.new_test_loop(gen)

        async def sleeper(dt, arg):
            await asyncio.sleep(dt/2)
            res = await asyncio.sleep(dt/2, arg)
            return res

        t = self.new_task(loop, sleeper(0.1, 'yeah'))
        loop.run_until_complete(t)
        self.assertTrue(t.done())
        self.assertEqual(t.result(), 'yeah')
        self.assertAlmostEqual(0.1, loop.time())

    def test_sleep_cancel(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            yield 0

        loop = self.new_test_loop(gen)

        t = self.new_task(loop, asyncio.sleep(10.0, 'yeah'))

        handle = None
        orig_call_later = loop.call_later

        def call_later(delay, callback, *args):
            nonlocal handle
            handle = orig_call_later(delay, callback, *args)
            return handle

        loop.call_later = call_later
        test_utils.run_briefly(loop)

        self.assertFalse(handle._cancelled)

        t.cancel()
        test_utils.run_briefly(loop)
        self.assertTrue(handle._cancelled)

    def test_task_cancel_sleeping_task(self):

        def gen():
            when = yield
            self.assertAlmostEqual(0.1, when)
            when = yield 0
            self.assertAlmostEqual(5000, when)
            yield 0.1

        loop = self.new_test_loop(gen)

        async def sleep(dt):
            await asyncio.sleep(dt)

        async def doit():
            sleeper = self.new_task(loop, sleep(5000))
            loop.call_later(0.1, sleeper.cancel)
            try:
                await sleeper
            except asyncio.CancelledError:
                return 'cancelled'
            else:
                return 'slept in'

        doer = doit()
        self.assertEqual(loop.run_until_complete(doer), 'cancelled')
        self.assertAlmostEqual(0.1, loop.time())

    def test_task_cancel_waiter_future(self):
        fut = self.new_future(self.loop)

        async def coro():
            await fut

        task = self.new_task(self.loop, coro())
        test_utils.run_briefly(self.loop)
        self.assertIs(task._fut_waiter, fut)

        task.cancel()
        test_utils.run_briefly(self.loop)
        self.assertRaises(
            asyncio.CancelledError, self.loop.run_until_complete, task)
        self.assertIsNone(task._fut_waiter)
        self.assertTrue(fut.cancelled())

    def test_task_set_methods(self):
        async def notmuch():
            return 'ko'

        gen = notmuch()
        task = self.new_task(self.loop, gen)

        with self.assertRaisesRegex(RuntimeError, 'not support set_result'):
            task.set_result('ok')

        with self.assertRaisesRegex(RuntimeError, 'not support set_exception'):
            task.set_exception(ValueError())

        self.assertEqual(
            self.loop.run_until_complete(task),
            'ko')

    def test_step_result(self):
        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def notmuch():
                yield None
                yield 1
                return 'ko'

        self.assertRaises(
            RuntimeError, self.loop.run_until_complete, notmuch())

    def test_step_result_future(self):
        # If coroutine returns future, task waits on this future.

        class Fut(asyncio.Future):
            def __init__(self, *args, **kwds):
                self.cb_added = False
                super().__init__(*args, **kwds)

            def add_done_callback(self, *args, **kwargs):
                self.cb_added = True
                super().add_done_callback(*args, **kwargs)

        fut = Fut(loop=self.loop)
        result = None

        async def wait_for_future():
            nonlocal result
            result = await fut

        t = self.new_task(self.loop, wait_for_future())
        test_utils.run_briefly(self.loop)
        self.assertTrue(fut.cb_added)

        res = object()
        fut.set_result(res)
        test_utils.run_briefly(self.loop)
        self.assertIs(res, result)
        self.assertTrue(t.done())
        self.assertIsNone(t.result())

    def test_baseexception_during_cancel(self):

        def gen():
            when = yield
            self.assertAlmostEqual(10.0, when)
            yield 0

        loop = self.new_test_loop(gen)

        async def sleeper():
            await asyncio.sleep(10)

        base_exc = SystemExit()

        async def notmutch():
            try:
                await sleeper()
            except asyncio.CancelledError:
                raise base_exc

        task = self.new_task(loop, notmutch())
        test_utils.run_briefly(loop)

        task.cancel()
        self.assertFalse(task.done())

        self.assertRaises(SystemExit, test_utils.run_briefly, loop)

        self.assertTrue(task.done())
        self.assertFalse(task.cancelled())
        self.assertIs(task.exception(), base_exc)

    def test_iscoroutinefunction(self):
        def fn():
            pass

        self.assertFalse(asyncio.iscoroutinefunction(fn))

        def fn1():
            yield
        self.assertFalse(asyncio.iscoroutinefunction(fn1))

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def fn2():
                yield
        self.assertTrue(asyncio.iscoroutinefunction(fn2))

        self.assertFalse(asyncio.iscoroutinefunction(mock.Mock()))

    def test_yield_vs_yield_from(self):
        fut = self.new_future(self.loop)

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def wait_for_future():
                yield fut

        task = wait_for_future()
        with self.assertRaises(RuntimeError):
            self.loop.run_until_complete(task)

        self.assertFalse(fut.done())

    def test_yield_vs_yield_from_generator(self):
        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro():
                yield

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def wait_for_future():
                gen = coro()
                try:
                    yield gen
                finally:
                    gen.close()

        task = wait_for_future()
        self.assertRaises(
            RuntimeError,
            self.loop.run_until_complete, task)

    def test_coroutine_non_gen_function(self):
        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def func():
                return 'test'

        self.assertTrue(asyncio.iscoroutinefunction(func))

        coro = func()
        self.assertTrue(asyncio.iscoroutine(coro))

        res = self.loop.run_until_complete(coro)
        self.assertEqual(res, 'test')

    def test_coroutine_non_gen_function_return_future(self):
        fut = self.new_future(self.loop)

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def func():
                return fut

        async def coro():
            fut.set_result('test')

        t1 = self.new_task(self.loop, func())
        t2 = self.new_task(self.loop, coro())
        res = self.loop.run_until_complete(t1)
        self.assertEqual(res, 'test')
        self.assertIsNone(t2.result())


    def test_current_task_deprecated(self):
        Task = self.__class__.Task

        with self.assertWarns(DeprecationWarning):
            self.assertIsNone(Task.current_task(loop=self.loop))

        async def coro(loop):
            with self.assertWarns(DeprecationWarning):
                self.assertIs(Task.current_task(loop=loop), task)

            # See http://bugs.python.org/issue29271 for details:
            asyncio.set_event_loop(loop)
            try:
                with self.assertWarns(DeprecationWarning):
                    self.assertIs(Task.current_task(None), task)
                with self.assertWarns(DeprecationWarning):
                    self.assertIs(Task.current_task(), task)
            finally:
                asyncio.set_event_loop(None)

        task = self.new_task(self.loop, coro(self.loop))
        self.loop.run_until_complete(task)
        with self.assertWarns(DeprecationWarning):
            self.assertIsNone(Task.current_task(loop=self.loop))

    def test_current_task(self):
        self.assertIsNone(asyncio.current_task(loop=self.loop))

        async def coro(loop):
            self.assertIs(asyncio.current_task(loop=loop), task)

            self.assertIs(asyncio.current_task(None), task)
            self.assertIs(asyncio.current_task(), task)

        task = self.new_task(self.loop, coro(self.loop))
        self.loop.run_until_complete(task)
        self.assertIsNone(asyncio.current_task(loop=self.loop))

    def test_current_task_with_interleaving_tasks(self):
        self.assertIsNone(asyncio.current_task(loop=self.loop))

        fut1 = self.new_future(self.loop)
        fut2 = self.new_future(self.loop)

        async def coro1(loop):
            self.assertTrue(asyncio.current_task(loop=loop) is task1)
            await fut1
            self.assertTrue(asyncio.current_task(loop=loop) is task1)
            fut2.set_result(True)

        async def coro2(loop):
            self.assertTrue(asyncio.current_task(loop=loop) is task2)
            fut1.set_result(True)
            await fut2
            self.assertTrue(asyncio.current_task(loop=loop) is task2)

        task1 = self.new_task(self.loop, coro1(self.loop))
        task2 = self.new_task(self.loop, coro2(self.loop))

        self.loop.run_until_complete(asyncio.wait((task1, task2)))
        self.assertIsNone(asyncio.current_task(loop=self.loop))

    # Some thorough tests for cancellation propagation through
    # coroutines, tasks and wait().

    def test_yield_future_passes_cancel(self):
        # Cancelling outer() cancels inner() cancels waiter.
        proof = 0
        waiter = self.new_future(self.loop)

        async def inner():
            nonlocal proof
            try:
                await waiter
            except asyncio.CancelledError:
                proof += 1
                raise
            else:
                self.fail('got past sleep() in inner()')

        async def outer():
            nonlocal proof
            try:
                await inner()
            except asyncio.CancelledError:
                proof += 100  # Expect this path.
            else:
                proof += 10

        f = asyncio.ensure_future(outer(), loop=self.loop)
        test_utils.run_briefly(self.loop)
        f.cancel()
        self.loop.run_until_complete(f)
        self.assertEqual(proof, 101)
        self.assertTrue(waiter.cancelled())

    def test_yield_wait_does_not_shield_cancel(self):
        # Cancelling outer() makes wait() return early, leaves inner()
        # running.
        proof = 0
        waiter = self.new_future(self.loop)

        async def inner():
            nonlocal proof
            await waiter
            proof += 1

        async def outer():
            nonlocal proof
            d, p = await asyncio.wait([inner()])
            proof += 100

        f = asyncio.ensure_future(outer(), loop=self.loop)
        test_utils.run_briefly(self.loop)
        f.cancel()
        self.assertRaises(
            asyncio.CancelledError, self.loop.run_until_complete, f)
        waiter.set_result(None)
        test_utils.run_briefly(self.loop)
        self.assertEqual(proof, 1)

    def test_shield_result(self):
        inner = self.new_future(self.loop)
        outer = asyncio.shield(inner)
        inner.set_result(42)
        res = self.loop.run_until_complete(outer)
        self.assertEqual(res, 42)

    def test_shield_exception(self):
        inner = self.new_future(self.loop)
        outer = asyncio.shield(inner)
        test_utils.run_briefly(self.loop)
        exc = RuntimeError('expected')
        inner.set_exception(exc)
        test_utils.run_briefly(self.loop)
        self.assertIs(outer.exception(), exc)

    def test_shield_cancel_inner(self):
        inner = self.new_future(self.loop)
        outer = asyncio.shield(inner)
        test_utils.run_briefly(self.loop)
        inner.cancel()
        test_utils.run_briefly(self.loop)
        self.assertTrue(outer.cancelled())

    def test_shield_cancel_outer(self):
        inner = self.new_future(self.loop)
        outer = asyncio.shield(inner)
        test_utils.run_briefly(self.loop)
        outer.cancel()
        test_utils.run_briefly(self.loop)
        self.assertTrue(outer.cancelled())
        self.assertEqual(0, 0 if outer._callbacks is None else len(outer._callbacks))

    def test_shield_shortcut(self):
        fut = self.new_future(self.loop)
        fut.set_result(42)
        res = self.loop.run_until_complete(asyncio.shield(fut))
        self.assertEqual(res, 42)

    def test_shield_effect(self):
        # Cancelling outer() does not affect inner().
        proof = 0
        waiter = self.new_future(self.loop)

        async def inner():
            nonlocal proof
            await waiter
            proof += 1

        async def outer():
            nonlocal proof
            await asyncio.shield(inner())
            proof += 100

        f = asyncio.ensure_future(outer(), loop=self.loop)
        test_utils.run_briefly(self.loop)
        f.cancel()
        with self.assertRaises(asyncio.CancelledError):
            self.loop.run_until_complete(f)
        waiter.set_result(None)
        test_utils.run_briefly(self.loop)
        self.assertEqual(proof, 1)

    def test_shield_gather(self):
        child1 = self.new_future(self.loop)
        child2 = self.new_future(self.loop)
        parent = asyncio.gather(child1, child2)
        outer = asyncio.shield(parent)
        test_utils.run_briefly(self.loop)
        outer.cancel()
        test_utils.run_briefly(self.loop)
        self.assertTrue(outer.cancelled())
        child1.set_result(1)
        child2.set_result(2)
        test_utils.run_briefly(self.loop)
        self.assertEqual(parent.result(), [1, 2])

    def test_gather_shield(self):
        child1 = self.new_future(self.loop)
        child2 = self.new_future(self.loop)
        inner1 = asyncio.shield(child1)
        inner2 = asyncio.shield(child2)
        parent = asyncio.gather(inner1, inner2)
        test_utils.run_briefly(self.loop)
        parent.cancel()
        # This should cancel inner1 and inner2 but bot child1 and child2.
        test_utils.run_briefly(self.loop)
        self.assertIsInstance(parent.exception(), asyncio.CancelledError)
        self.assertTrue(inner1.cancelled())
        self.assertTrue(inner2.cancelled())
        child1.set_result(1)
        child2.set_result(2)
        test_utils.run_briefly(self.loop)

    def test_as_completed_invalid_args(self):
        fut = self.new_future(self.loop)

        # as_completed() expects a list of futures, not a future instance
        self.assertRaises(TypeError, self.loop.run_until_complete,
            asyncio.as_completed(fut, loop=self.loop))
        coro = coroutine_function()
        self.assertRaises(TypeError, self.loop.run_until_complete,
            asyncio.as_completed(coro, loop=self.loop))
        coro.close()

    def test_wait_invalid_args(self):
        fut = self.new_future(self.loop)

        # wait() expects a list of futures, not a future instance
        self.assertRaises(TypeError, self.loop.run_until_complete,
            asyncio.wait(fut))
        coro = coroutine_function()
        self.assertRaises(TypeError, self.loop.run_until_complete,
            asyncio.wait(coro))
        coro.close()

        # wait() expects at least a future
        self.assertRaises(ValueError, self.loop.run_until_complete,
            asyncio.wait([]))

    def test_corowrapper_mocks_generator(self):

        def check():
            # A function that asserts various things.
            # Called twice, with different debug flag values.

            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def coro():
                    # The actual coroutine.
                    self.assertTrue(gen.gi_running)
                    yield from fut

            # A completed Future used to run the coroutine.
            fut = self.new_future(self.loop)
            fut.set_result(None)

            # Call the coroutine.
            gen = coro()

            # Check some properties.
            self.assertTrue(asyncio.iscoroutine(gen))
            self.assertIsInstance(gen.gi_frame, types.FrameType)
            self.assertFalse(gen.gi_running)
            self.assertIsInstance(gen.gi_code, types.CodeType)

            # Run it.
            self.loop.run_until_complete(gen)

            # The frame should have changed.
            self.assertIsNone(gen.gi_frame)

        # Test with debug flag cleared.
        with set_coroutine_debug(False):
            check()

        # Test with debug flag set.
        with set_coroutine_debug(True):
            check()

    def test_yield_from_corowrapper(self):
        with set_coroutine_debug(True):
            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def t1():
                    return (yield from t2())

            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def t2():
                    f = self.new_future(self.loop)
                    self.new_task(self.loop, t3(f))
                    return (yield from f)

            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def t3(f):
                    f.set_result((1, 2, 3))

            task = self.new_task(self.loop, t1())
            val = self.loop.run_until_complete(task)
            self.assertEqual(val, (1, 2, 3))

    def test_yield_from_corowrapper_send(self):
        def foo():
            a = yield
            return a

        def call(arg):
            cw = asyncio.coroutines.CoroWrapper(foo())
            cw.send(None)
            try:
                cw.send(arg)
            except StopIteration as ex:
                return ex.args[0]
            else:
                raise AssertionError('StopIteration was expected')

        self.assertEqual(call((1, 2)), (1, 2))
        self.assertEqual(call('spam'), 'spam')

    def test_corowrapper_weakref(self):
        wd = weakref.WeakValueDictionary()
        def foo(): yield from []
        cw = asyncio.coroutines.CoroWrapper(foo())
        wd['cw'] = cw  # Would fail without __weakref__ slot.
        cw.gen = None  # Suppress warning from __del__.

    def test_corowrapper_throw(self):
        # Issue 429: CoroWrapper.throw must be compatible with gen.throw
        def foo():
            value = None
            while True:
                try:
                    value = yield value
                except Exception as e:
                    value = e

        exception = Exception("foo")
        cw = asyncio.coroutines.CoroWrapper(foo())
        cw.send(None)
        self.assertIs(exception, cw.throw(exception))

        cw = asyncio.coroutines.CoroWrapper(foo())
        cw.send(None)
        self.assertIs(exception, cw.throw(Exception, exception))

        cw = asyncio.coroutines.CoroWrapper(foo())
        cw.send(None)
        exception = cw.throw(Exception, "foo")
        self.assertIsInstance(exception, Exception)
        self.assertEqual(exception.args, ("foo", ))

        cw = asyncio.coroutines.CoroWrapper(foo())
        cw.send(None)
        exception = cw.throw(Exception, "foo", None)
        self.assertIsInstance(exception, Exception)
        self.assertEqual(exception.args, ("foo", ))

    def test_all_tasks_deprecated(self):
        Task = self.__class__.Task

        async def coro():
            with self.assertWarns(DeprecationWarning):
                assert Task.all_tasks(self.loop) == {t}

        t = self.new_task(self.loop, coro())
        self.loop.run_until_complete(t)

    def test_log_destroyed_pending_task(self):
        Task = self.__class__.Task

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def kill_me(loop):
                future = self.new_future(loop)
                yield from future
                # at this point, the only reference to kill_me() task is
                # the Task._wakeup() method in future._callbacks
                raise Exception("code never reached")

        mock_handler = mock.Mock()
        self.loop.set_debug(True)
        self.loop.set_exception_handler(mock_handler)

        # schedule the task
        coro = kill_me(self.loop)
        task = asyncio.ensure_future(coro, loop=self.loop)

        self.assertEqual(asyncio.all_tasks(loop=self.loop), {task})

        # See http://bugs.python.org/issue29271 for details:
        asyncio.set_event_loop(self.loop)
        try:
            with self.assertWarns(DeprecationWarning):
                self.assertEqual(Task.all_tasks(), {task})
            with self.assertWarns(DeprecationWarning):
                self.assertEqual(Task.all_tasks(None), {task})
        finally:
            asyncio.set_event_loop(None)

        # execute the task so it waits for future
        self.loop._run_once()
        self.assertEqual(len(self.loop._ready), 0)

        # remove references to the task
        coro = None
        source_traceback = task._source_traceback
        task = None

        # no more reference to kill_me() task: the task is destroyed by the GC
        support.gc_collect()

        self.assertEqual(asyncio.all_tasks(loop=self.loop), set())

        mock_handler.assert_called_with(self.loop, {
            'message': 'Task was destroyed but it is pending!',
            'task': mock.ANY,
            'source_traceback': source_traceback,
        })
        mock_handler.reset_mock()

    @mock.patch('asyncio.base_events.logger')
    def test_tb_logger_not_called_after_cancel(self, m_log):
        loop = asyncio.new_event_loop()
        self.set_event_loop(loop)

        async def coro():
            raise TypeError

        async def runner():
            task = self.new_task(loop, coro())
            await asyncio.sleep(0.05)
            task.cancel()
            task = None

        loop.run_until_complete(runner())
        self.assertFalse(m_log.error.called)

    @unittest.skipUnderCinderJIT("Invalid stack traces (T86183012)")
    @mock.patch('asyncio.coroutines.logger')
    def test_coroutine_never_yielded(self, m_log):
        with set_coroutine_debug(True):
            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def coro_noop():
                    pass

        tb_filename = __file__
        tb_lineno = sys._getframe().f_lineno + 2
        # create a coroutine object but don't use it
        coro_noop()
        support.gc_collect()

        self.assertTrue(m_log.error.called)
        message = m_log.error.call_args[0][0]
        func_filename, func_lineno = test_utils.get_function_source(coro_noop)

        regex = (r'^<CoroWrapper %s\(?\)? .* at %s:%s, .*> '
                    r'was never yielded from\n'
                 r'Coroutine object created at \(most recent call last, truncated to \d+ last lines\):\n'
                 r'.*\n'
                 r'  File "%s", line %s, in test_coroutine_never_yielded\n'
                 r'    coro_noop\(\)$'
                 % (re.escape(coro_noop.__qualname__),
                    re.escape(func_filename), func_lineno,
                    re.escape(tb_filename), tb_lineno))

        self.assertRegex(message, re.compile(regex, re.DOTALL))

    def test_return_coroutine_from_coroutine(self):
        """Return of @asyncio.coroutine()-wrapped function generator object
        from @asyncio.coroutine()-wrapped function should have same effect as
        returning generator object or Future."""
        def check():
            with self.assertWarns(DeprecationWarning):
                @asyncio.coroutine
                def outer_coro():
                    with self.assertWarns(DeprecationWarning):
                        @asyncio.coroutine
                        def inner_coro():
                            return 1

                    return inner_coro()

            result = self.loop.run_until_complete(outer_coro())
            self.assertEqual(result, 1)

        # Test with debug flag cleared.
        with set_coroutine_debug(False):
            check()

        # Test with debug flag set.
        with set_coroutine_debug(True):
            check()

    @unittest.skipUnderCinderJIT("Line number off by 1 (T86183012)")
    def test_task_source_traceback(self):
        self.loop.set_debug(True)

        task = self.new_task(self.loop, coroutine_function())
        lineno = sys._getframe().f_lineno - 1
        self.assertIsInstance(task._source_traceback, list)
        self.assertEqual(task._source_traceback[-2][:3],
                         (__file__,
                          lineno,
                          'test_task_source_traceback'))
        self.loop.run_until_complete(task)

    def _test_cancel_wait_for(self, timeout):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        async def blocking_coroutine():
            fut = self.new_future(loop)
            # Block: fut result is never set
            await fut

        task = loop.create_task(blocking_coroutine())

        wait = loop.create_task(asyncio.wait_for(task, timeout))
        loop.call_soon(wait.cancel)

        self.assertRaises(asyncio.CancelledError,
                          loop.run_until_complete, wait)

        # Python issue #23219: cancelling the wait must also cancel the task
        self.assertTrue(task.cancelled())

    def test_cancel_blocking_wait_for(self):
        self._test_cancel_wait_for(None)

    def test_cancel_wait_for(self):
        self._test_cancel_wait_for(60.0)

    def test_cancel_gather_1(self):
        """Ensure that a gathering future refuses to be cancelled once all
        children are done"""
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        fut = self.new_future(loop)
        # The indirection fut->child_coro is needed since otherwise the
        # gathering task is done at the same time as the child future
        def child_coro():
            return (yield from fut)
        gather_future = asyncio.gather(child_coro(), loop=loop)
        gather_task = asyncio.ensure_future(gather_future, loop=loop)

        cancel_result = None
        def cancelling_callback(_):
            nonlocal cancel_result
            cancel_result = gather_task.cancel()
        fut.add_done_callback(cancelling_callback)

        fut.set_result(42) # calls the cancelling_callback after fut is done()

        # At this point the task should complete.
        loop.run_until_complete(gather_task)

        # Python issue #26923: asyncio.gather drops cancellation
        self.assertEqual(cancel_result, False)
        self.assertFalse(gather_task.cancelled())
        self.assertEqual(gather_task.result(), [42])

    def test_cancel_gather_2(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        async def test():
            time = 0
            while True:
                time += 0.05
                await asyncio.gather(asyncio.sleep(0.05),
                                     return_exceptions=True,
                                     loop=loop)
                if time > 1:
                    return

        async def main():
            qwe = self.new_task(loop, test())
            await asyncio.sleep(0.2)
            qwe.cancel()
            try:
                await qwe
            except asyncio.CancelledError:
                pass
            else:
                self.fail('gather did not propagate the cancellation request')

        loop.run_until_complete(main())

    def test_exception_traceback(self):
        # See http://bugs.python.org/issue28843

        async def foo():
            1 / 0

        async def main():
            task = self.new_task(self.loop, foo())
            await asyncio.sleep(0)  # skip one loop iteration
            self.assertIsNotNone(task.exception().__traceback__)

        self.loop.run_until_complete(main())

    @mock.patch('asyncio.base_events.logger')
    def test_error_in_call_soon(self, m_log):
        def call_soon(callback, *args, **kwargs):
            raise ValueError
        self.loop.call_soon = call_soon

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro():
                pass

        self.assertFalse(m_log.error.called)

        with self.assertRaises(ValueError), mock.patch.object(self.loop.__class__, "call_soon", call_soon):
            gen = coro()
            try:
                self.new_task(self.loop, gen)
            finally:
                gen.close()

        self.assertTrue(m_log.error.called)
        message = m_log.error.call_args[0][0]
        self.assertIn('Task was destroyed but it is pending', message)

        self.assertEqual(asyncio.all_tasks(self.loop), set())

    def test_create_task_with_noncoroutine(self):
        with self.assertRaisesRegex(TypeError,
                                    "a coroutine was expected, got 123"):
            self.new_task(self.loop, 123)

        # test it for the second time to ensure that caching
        # in asyncio.iscoroutine() doesn't break things.
        with self.assertRaisesRegex(TypeError,
                                    "a coroutine was expected, got 123"):
            self.new_task(self.loop, 123)

    def test_create_task_with_oldstyle_coroutine(self):

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro():
                pass

        task = self.new_task(self.loop, coro())
        self.assertIsInstance(task, self.Task)
        self.loop.run_until_complete(task)

        # test it for the second time to ensure that caching
        # in asyncio.iscoroutine() doesn't break things.
        task = self.new_task(self.loop, coro())
        self.assertIsInstance(task, self.Task)
        self.loop.run_until_complete(task)

    def test_create_task_with_async_function(self):

        async def coro():
            pass

        task = self.new_task(self.loop, coro())
        self.assertIsInstance(task, self.Task)
        self.loop.run_until_complete(task)

        # test it for the second time to ensure that caching
        # in asyncio.iscoroutine() doesn't break things.
        task = self.new_task(self.loop, coro())
        self.assertIsInstance(task, self.Task)
        self.loop.run_until_complete(task)

    def test_create_task_with_asynclike_function(self):
        task = self.new_task(self.loop, CoroLikeObject())
        self.assertIsInstance(task, self.Task)
        self.assertEqual(self.loop.run_until_complete(task), 42)

        # test it for the second time to ensure that caching
        # in asyncio.iscoroutine() doesn't break things.
        task = self.new_task(self.loop, CoroLikeObject())
        self.assertIsInstance(task, self.Task)
        self.assertEqual(self.loop.run_until_complete(task), 42)

    def test_bare_create_task(self):

        async def inner():
            return 1

        async def coro():
            task = asyncio.create_task(inner())
            self.assertIsInstance(task, self.Task)
            ret = await task
            self.assertEqual(1, ret)

        self.loop.run_until_complete(coro())

    def test_bare_create_named_task(self):

        async def coro_noop():
            pass

        async def coro():
            task = asyncio.create_task(coro_noop(), name='No-op')
            self.assertEqual(task.get_name(), 'No-op')
            await task

        self.loop.run_until_complete(coro())

    def test_context_1(self):
        cvar = contextvars.ContextVar('cvar', default='nope')

        async def sub():
            await asyncio.sleep(0.01)
            self.assertEqual(cvar.get(), 'nope')
            cvar.set('something else')

        async def main():
            self.assertEqual(cvar.get(), 'nope')
            subtask = self.new_task(loop, sub())
            cvar.set('yes')
            self.assertEqual(cvar.get(), 'yes')
            await subtask
            self.assertEqual(cvar.get(), 'yes')

        loop = asyncio.new_event_loop()
        try:
            task = self.new_task(loop, main())
            loop.run_until_complete(task)
        finally:
            loop.close()

    def test_context_2(self):
        cvar = contextvars.ContextVar('cvar', default='nope')

        async def main():
            def fut_on_done(fut):
                # This change must not pollute the context
                # of the "main()" task.
                cvar.set('something else')

            self.assertEqual(cvar.get(), 'nope')

            for j in range(2):
                fut = self.new_future(loop)
                fut.add_done_callback(fut_on_done)
                cvar.set(f'yes{j}')
                loop.call_soon(fut.set_result, None)
                await fut
                self.assertEqual(cvar.get(), f'yes{j}')

                for i in range(3):
                    # Test that task passed its context to add_done_callback:
                    cvar.set(f'yes{i}-{j}')
                    await asyncio.sleep(0.001)
                    self.assertEqual(cvar.get(), f'yes{i}-{j}')

        loop = asyncio.new_event_loop()
        try:
            task = self.new_task(loop, main())
            loop.run_until_complete(task)
        finally:
            loop.close()

        self.assertEqual(cvar.get(), 'nope')

    def test_context_3(self):
        # Run 100 Tasks in parallel, each modifying cvar.

        cvar = contextvars.ContextVar('cvar', default=-1)

        async def sub(num):
            for i in range(10):
                cvar.set(num + i)
                await asyncio.sleep(random.uniform(0.001, 0.05))
                self.assertEqual(cvar.get(), num + i)

        async def main():
            tasks = []
            for i in range(100):
                task = loop.create_task(sub(random.randint(0, 10)))
                tasks.append(task)

            await asyncio.gather(*tasks, loop=loop)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

        self.assertEqual(cvar.get(), -1)

    def test_get_coro(self):
        loop = asyncio.new_event_loop()
        coro = coroutine_function()
        try:
            task = self.new_task(loop, coro)
            loop.run_until_complete(task)
            self.assertIs(task.get_coro(), coro)
        finally:
            loop.close()

    def test_cr_awaiter(self):
        ctask = getattr(tasks, '_CTask', None)
        if ctask is None or not issubclass(self.Task, ctask):
            self.skipTest("Only subclasses of _CTask set cr_awaiter on wrapped coroutines")

        async def coro():
            self.assertIs(cinder._get_coro_awaiter(coro_obj), awaiter_obj)
            return "ok"

        async def awaiter(coro):
            task = self.loop.create_task(coro)
            return await task

        coro_obj = coro()
        awaiter_obj = awaiter(coro_obj)
        self.assertIsNone(cinder._get_coro_awaiter(coro_obj))
        self.assertEqual(self.loop.run_until_complete(awaiter_obj), "ok")
        self.assertIsNone(cinder._get_coro_awaiter(coro_obj))


def add_subclass_tests(cls):
    BaseTask = cls.Task
    BaseFuture = cls.Future

    if BaseTask is None or BaseFuture is None:
        return cls

    class CommonFuture:
        def __init__(self, *args, **kwargs):
            self.calls = collections.defaultdict(lambda: 0)
            super().__init__(*args, **kwargs)

        def add_done_callback(self, *args, **kwargs):
            self.calls['add_done_callback'] += 1
            return super().add_done_callback(*args, **kwargs)

    class Task(CommonFuture, BaseTask):
        pass

    class Future(CommonFuture, BaseFuture):
        pass

    def test_subclasses_ctask_cfuture(self):
        fut = self.Future(loop=self.loop)

        async def func():
            self.loop.call_soon(lambda: fut.set_result('spam'))
            return await fut

        task = self.Task(func(), loop=self.loop)

        result = self.loop.run_until_complete(task)

        self.assertEqual(result, 'spam')

        self.assertEqual(
            dict(task.calls),
            {'add_done_callback': 1})

        self.assertEqual(
            dict(fut.calls),
            {'add_done_callback': 1})

    # Add patched Task & Future back to the test case
    cls.Task = Task
    cls.Future = Future

    # Add an extra unit-test
    cls.test_subclasses_ctask_cfuture = test_subclasses_ctask_cfuture

    # Disable the "test_task_source_traceback" test
    # (the test is hardcoded for a particular call stack, which
    # is slightly different for Task subclasses)
    cls.test_task_source_traceback = None

    return cls


class SetMethodsTest:

    def test_set_result_causes_invalid_state(self):
        Future = type(self).Future
        self.loop.call_exception_handler = exc_handler = mock.Mock()

        async def foo():
            await asyncio.sleep(0.1)
            return 10

        coro = foo()
        task = self.new_task(self.loop, coro)
        Future.set_result(task, 'spam')

        self.assertEqual(
            self.loop.run_until_complete(task),
            'spam')

        exc_handler.assert_called_once()
        exc = exc_handler.call_args[0][0]['exception']
        with self.assertRaisesRegex(asyncio.InvalidStateError,
                                    r'step\(\): already done'):
            raise exc

        coro.close()

    def test_set_exception_causes_invalid_state(self):
        class MyExc(Exception):
            pass

        Future = type(self).Future
        self.loop.call_exception_handler = exc_handler = mock.Mock()

        async def foo():
            await asyncio.sleep(0.1)
            return 10

        coro = foo()
        task = self.new_task(self.loop, coro)
        Future.set_exception(task, MyExc())

        with self.assertRaises(MyExc):
            self.loop.run_until_complete(task)

        exc_handler.assert_called_once()
        exc = exc_handler.call_args[0][0]['exception']
        with self.assertRaisesRegex(asyncio.InvalidStateError,
                                    r'step\(\): already done'):
            raise exc

        coro.close()


@unittest.skipUnless(hasattr(futures, '_CFuture') and
                     hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
class CTask_CFuture_Tests(BaseTaskTests, SetMethodsTest,
                          test_utils.TestCase):

    Task = getattr(tasks, '_CTask', None)
    Future = getattr(futures, '_CFuture', None)

    @unittest.skipUnderCinderJIT("Global-refcount broken under JIT? (T86183012)")
    @support.refcount_test
    def test_refleaks_in_task___init__(self):
        gettotalrefcount = support.get_attribute(sys, 'gettotalrefcount')
        async def coro():
            pass
        task = self.new_task(self.loop, coro())
        self.loop.run_until_complete(task)
        refs_before = gettotalrefcount()
        for i in range(100):
            task.__init__(coro(), loop=self.loop)
            self.loop.run_until_complete(task)
        self.assertAlmostEqual(gettotalrefcount() - refs_before, 0, delta=10)

    def test_del__log_destroy_pending_segfault(self):
        async def coro():
            pass
        task = self.new_task(self.loop, coro())
        self.loop.run_until_complete(task)
        with self.assertRaises(AttributeError):
            del task._log_destroy_pending


@unittest.skipUnless(hasattr(futures, '_CFuture') and
                     hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
@add_subclass_tests
class CTask_CFuture_SubclassTests(BaseTaskTests, test_utils.TestCase):

    Task = getattr(tasks, '_CTask', None)
    Future = getattr(futures, '_CFuture', None)


@unittest.skipUnless(hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
@add_subclass_tests
class CTaskSubclass_PyFuture_Tests(BaseTaskTests, test_utils.TestCase):

    Task = getattr(tasks, '_CTask', None)
    Future = futures._PyFuture


@unittest.skipUnless(hasattr(futures, '_CFuture'),
                     'requires the C _asyncio module')
@add_subclass_tests
class PyTask_CFutureSubclass_Tests(BaseTaskTests, test_utils.TestCase):

    Future = getattr(futures, '_CFuture', None)
    Task = tasks._PyTask


@unittest.skipUnless(hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
class CTask_PyFuture_Tests(BaseTaskTests, test_utils.TestCase):

    Task = getattr(tasks, '_CTask', None)
    Future = futures._PyFuture


@unittest.skipUnless(hasattr(futures, '_CFuture'),
                     'requires the C _asyncio module')
class PyTask_CFuture_Tests(BaseTaskTests, test_utils.TestCase):

    Task = tasks._PyTask
    Future = getattr(futures, '_CFuture', None)


class PyTask_PyFuture_Tests(BaseTaskTests, SetMethodsTest,
                            test_utils.TestCase):

    Task = tasks._PyTask
    Future = futures._PyFuture


@add_subclass_tests
class PyTask_PyFuture_SubclassTests(BaseTaskTests, test_utils.TestCase):
    Task = tasks._PyTask
    Future = futures._PyFuture


@unittest.skipUnless(hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
class CTask_Future_Tests(test_utils.TestCase):

    def test_foobar(self):
        class Fut(asyncio.Future):
            @property
            def get_loop(self):
                raise AttributeError

        async def coro():
            await fut
            return 'spam'

        self.loop = asyncio.new_event_loop()
        try:
            fut = Fut(loop=self.loop)
            self.loop.call_later(0.1, fut.set_result, 1)
            task = self.loop.create_task(coro())
            res = self.loop.run_until_complete(task)
        finally:
            self.loop.close()

        self.assertEqual(res, 'spam')


class BaseTaskIntrospectionTests:
    _register_task = None
    _unregister_task = None
    _enter_task = None
    _leave_task = None
    _all_tasks = None
    _task_all_tasks = None

    def test__register_task_1(self):
        class TaskLike:
            @property
            def _loop(self):
                return loop

            def done(self):
                return False

        task = TaskLike()
        loop = mock.Mock()

        self.assertEqual(self._all_tasks(loop), set())
        self._register_task(task)
        self.assertEqual(self._all_tasks(loop), {task})
        self._unregister_task(task)

    def test__register_task_2(self):
        class TaskLike:
            def get_loop(self):
                return loop

            def done(self):
                return False

        task = TaskLike()
        loop = mock.Mock()

        self.assertEqual(self._all_tasks(loop), set())
        self._register_task(task)
        self.assertEqual(self._all_tasks(loop), {task})
        self._unregister_task(task)

    def test__register_task_3(self):
        class TaskLike:
            def get_loop(self):
                return loop

            def done(self):
                return True

        task = TaskLike()
        loop = mock.Mock()

        self.assertEqual(self._all_tasks(loop), set())
        self._register_task(task)
        self.assertEqual(self._all_tasks(loop), set())
        with self.assertWarns(DeprecationWarning):
            self.assertEqual(self._task_all_tasks(loop), {task})
        self._unregister_task(task)

    def test__enter_task(self):
        task = mock.Mock()
        loop = mock.Mock()
        self.assertIsNone(asyncio.current_task(loop))
        self._enter_task(loop, task)
        self.assertIs(asyncio.current_task(loop), task)
        self._leave_task(loop, task)

    def test__enter_task_failure(self):
        task1 = mock.Mock()
        task2 = mock.Mock()
        loop = mock.Mock()
        self._enter_task(loop, task1)
        with self.assertRaises(RuntimeError):
            self._enter_task(loop, task2)
        self.assertIs(asyncio.current_task(loop), task1)
        self._leave_task(loop, task1)

    def test__leave_task(self):
        task = mock.Mock()
        loop = mock.Mock()
        self._enter_task(loop, task)
        self._leave_task(loop, task)
        self.assertIsNone(asyncio.current_task(loop))

    def test__leave_task_failure1(self):
        task1 = mock.Mock()
        task2 = mock.Mock()
        loop = mock.Mock()
        self._enter_task(loop, task1)
        with self.assertRaises(RuntimeError):
            self._leave_task(loop, task2)
        self.assertIs(asyncio.current_task(loop), task1)
        self._leave_task(loop, task1)

    def test__leave_task_failure2(self):
        task = mock.Mock()
        loop = mock.Mock()
        with self.assertRaises(RuntimeError):
            self._leave_task(loop, task)
        self.assertIsNone(asyncio.current_task(loop))

    def test__unregister_task(self):
        task = mock.Mock()
        loop = mock.Mock()
        task.get_loop = lambda: loop
        self._register_task(task)
        self._unregister_task(task)
        self.assertEqual(self._all_tasks(loop), set())

    def test__unregister_task_not_registered(self):
        task = mock.Mock()
        loop = mock.Mock()
        self._unregister_task(task)
        self.assertEqual(self._all_tasks(loop), set())


class PyIntrospectionTests(test_utils.TestCase, BaseTaskIntrospectionTests):
    _register_task = staticmethod(tasks._py_register_task)
    _unregister_task = staticmethod(tasks._py_unregister_task)
    _enter_task = staticmethod(tasks._py_enter_task)
    _leave_task = staticmethod(tasks._py_leave_task)
    _all_tasks = staticmethod(tasks._py_all_tasks)
    _task_all_tasks = staticmethod(tasks._py_task_all_tasks)

@unittest.skipUnless(hasattr(tasks, '_c_register_task'),
                     'requires the C _asyncio module')
class CIntrospectionTests(test_utils.TestCase, BaseTaskIntrospectionTests):
    if hasattr(tasks, '_c_register_task'):
        _register_task = staticmethod(tasks._c_register_task)
        _unregister_task = staticmethod(tasks._c_unregister_task)
        _enter_task = staticmethod(tasks._c_enter_task)
        _leave_task = staticmethod(tasks._c_leave_task)
        _all_tasks = staticmethod(tasks._c_all_tasks)
        _task_all_tasks = staticmethod(tasks._c_task_all_tasks)
    else:
        _register_task = _unregister_task = _enter_task = _leave_task = None
        _all_tasks = _register_task = None


class BaseCurrentLoopTests:

    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.set_event_loop(self.loop)

    def new_task(self, coro):
        raise NotImplementedError

    def test_current_task_no_running_loop(self):
        self.assertIsNone(asyncio.current_task(loop=self.loop))

    def test_current_task_no_running_loop_implicit(self):
        with self.assertRaises(RuntimeError):
            asyncio.current_task()

    def test_current_task_with_implicit_loop(self):
        async def coro():
            self.assertIs(asyncio.current_task(loop=self.loop), task)

            self.assertIs(asyncio.current_task(None), task)
            self.assertIs(asyncio.current_task(), task)

        task = self.new_task(coro())
        self.loop.run_until_complete(task)
        self.assertIsNone(asyncio.current_task(loop=self.loop))


class PyCurrentLoopTests(BaseCurrentLoopTests, test_utils.TestCase):

    def new_task(self, coro):
        return tasks._PyTask(coro, loop=self.loop)


@unittest.skipUnless(hasattr(tasks, '_CTask'),
                     'requires the C _asyncio module')
class CCurrentLoopTests(BaseCurrentLoopTests, test_utils.TestCase):

    def new_task(self, coro):
        return getattr(tasks, '_CTask')(coro, loop=self.loop)


class GenericTaskTests(test_utils.TestCase):

    def test_future_subclass(self):
        self.assertTrue(issubclass(asyncio.Task, asyncio.Future))

    def test_asyncio_module_compiled(self):
        # Because of circular imports it's easy to make _asyncio
        # module non-importable.  This is a simple test that will
        # fail on systems where C modules were successfully compiled
        # (hence the test for _functools), but _asyncio somehow didn't.
        try:
            import _functools
        except ImportError:
            pass
        else:
            try:
                import _asyncio
            except ImportError:
                self.fail('_asyncio module is missing')


class GatherTestsBase:

    gather = None

    def setUp(self):
        super().setUp()
        self.one_loop = self.new_test_loop()
        self.other_loop = self.new_test_loop()
        self.set_event_loop(self.one_loop, cleanup=False)

    def _run_loop(self, loop):
        while loop._ready:
            test_utils.run_briefly(loop)

    def _check_success(self, **kwargs):
        a, b, c = [self.one_loop.create_future() for i in range(3)]
        fut = self.gather(*self.wrap_futures(a, b, c), **kwargs)
        cb = test_utils.MockCallback()
        fut.add_done_callback(cb)
        b.set_result(1)
        a.set_result(2)
        self._run_loop(self.one_loop)
        self.assertEqual(cb.called, False)
        self.assertFalse(fut.done())
        c.set_result(3)
        self._run_loop(self.one_loop)
        cb.assert_called_once_with(fut)
        self.assertEqual(fut.result(), [2, 1, 3])

    def test_success(self):
        self._check_success()
        self._check_success(return_exceptions=False)

    def test_result_exception_success(self):
        self._check_success(return_exceptions=True)

    def test_one_exception(self):
        a, b, c, d, e = [self.one_loop.create_future() for i in range(5)]
        fut = self.gather(*self.wrap_futures(a, b, c, d, e))
        cb = test_utils.MockCallback()
        fut.add_done_callback(cb)
        exc = ZeroDivisionError()
        a.set_result(1)
        b.set_exception(exc)
        self._run_loop(self.one_loop)
        self.assertTrue(fut.done())
        cb.assert_called_once_with(fut)
        self.assertIs(fut.exception(), exc)
        # Does nothing
        c.set_result(3)
        d.cancel()
        e.set_exception(RuntimeError())
        e.exception()

    def test_return_exceptions(self):
        a, b, c, d = [self.one_loop.create_future() for i in range(4)]
        fut = self.gather(*self.wrap_futures(a, b, c, d),
                             return_exceptions=True)
        cb = test_utils.MockCallback()
        fut.add_done_callback(cb)
        exc = ZeroDivisionError()
        exc2 = RuntimeError()
        b.set_result(1)
        c.set_exception(exc)
        a.set_result(3)
        self._run_loop(self.one_loop)
        self.assertFalse(fut.done())
        d.set_exception(exc2)
        self._run_loop(self.one_loop)
        self.assertTrue(fut.done())
        cb.assert_called_once_with(fut)
        self.assertEqual(fut.result(), [3, 1, exc, exc2])

    @unittest.skipUnderUwsgi()
    def test_env_var_debug(self):
        code = '\n'.join((
            'import asyncio.coroutines',
            'print(asyncio.coroutines._DEBUG)'))

        # Test with -E to not fail if the unit test was run with
        # PYTHONASYNCIODEBUG set to a non-empty string
        sts, stdout, stderr = assert_python_ok('-E', '-c', code)
        self.assertEqual(stdout.rstrip(), b'False')

        sts, stdout, stderr = assert_python_ok('-c', code,
                                               PYTHONASYNCIODEBUG='',
                                               PYTHONDEVMODE='')
        self.assertEqual(stdout.rstrip(), b'False')

        sts, stdout, stderr = assert_python_ok('-c', code,
                                               PYTHONASYNCIODEBUG='1',
                                               PYTHONDEVMODE='')
        self.assertEqual(stdout.rstrip(), b'True')

        sts, stdout, stderr = assert_python_ok('-E', '-c', code,
                                               PYTHONASYNCIODEBUG='1',
                                               PYTHONDEVMODE='')
        self.assertEqual(stdout.rstrip(), b'False')

        # -X dev
        sts, stdout, stderr = assert_python_ok('-E', '-X', 'dev',
                                               '-c', code)
        self.assertEqual(stdout.rstrip(), b'True')

    def _test_cr_awaiter_impl(self, awaiter):
        async def add(a, b):
            self_coro = cinder._get_frame_gen(sys._getframe())
            self.assertIsInstance(self_coro, types.CoroutineType)
            self.assertIs(cinder._get_coro_awaiter(self_coro), awaiter_obj)
            return a + b

        a12 = add(1, 2)
        a34 = add(3, 4)
        a56 = add(5, 6)
        self.assertIsNone(cinder._get_coro_awaiter(a12))
        self.assertIsNone(cinder._get_coro_awaiter(a34))
        self.assertIsNone(cinder._get_coro_awaiter(a56))

        awaiter_obj = awaiter(a12, a34, a56)
        gather_obj = None
        result = self.one_loop.run_until_complete(awaiter_obj)
        self.assertEqual(result, [3, 7, 11])

        self.assertIsNone(cinder._get_coro_awaiter(a12))
        self.assertIsNone(cinder._get_coro_awaiter(a34))
        self.assertIsNone(cinder._get_coro_awaiter(a56))

        del awaiter_obj
        self.assertIsNone(cinder._get_coro_awaiter(a12))
        self.assertIsNone(cinder._get_coro_awaiter(a34))
        self.assertIsNone(cinder._get_coro_awaiter(a56))

    def test_gather_cr_awaiter_eager(self):
        async def eager_awaiter(o1, o2, o3):
            t3 = self.one_loop.create_task(o3)
            return await self.gather(o1, o2, t3)

        self._test_cr_awaiter_impl(eager_awaiter)

    def test_gather_cr_awaiter(self):
        async def awaiter(o1, o2, o3):
            t3 = self.one_loop.create_task(o3)
            gatherer = self.gather(o1, o2, t3)
            return await gatherer

        self._test_cr_awaiter_impl(awaiter)


class FutureGatherTests(GatherTestsBase):

    def wrap_futures(self, *futures):
        return futures

    def _check_empty_sequence(self, seq_or_iter):
        asyncio.set_event_loop(self.one_loop)
        self.addCleanup(asyncio.set_event_loop, None)
        fut = self.gather(*seq_or_iter)
        self.assertIsInstance(fut, asyncio.Future)
        self.assertIs(fut._loop, self.one_loop)
        self._run_loop(self.one_loop)
        self.assertTrue(fut.done())
        self.assertEqual(fut.result(), [])
        #with self.assertWarns(DeprecationWarning):
        fut = self.gather(*seq_or_iter, loop=self.other_loop)
        self.assertIs(fut._loop, self.other_loop)

    def test_constructor_empty_sequence(self):
        self._check_empty_sequence([])
        self._check_empty_sequence(())
        self._check_empty_sequence(set())
        self._check_empty_sequence(iter(""))

    def test_constructor_heterogenous_futures(self):
        fut1 = self.one_loop.create_future()
        fut2 = self.other_loop.create_future()
        with self.assertRaises(ValueError):
            self.gather(fut1, fut2)
        with self.assertRaises(ValueError):
            self.gather(fut1, loop=self.other_loop)

    def test_constructor_homogenous_futures(self):
        children = [self.other_loop.create_future() for i in range(3)]
        fut = self.gather(*children)
        self.assertIs(fut._loop, self.other_loop)
        self._run_loop(self.other_loop)
        self.assertFalse(fut.done())
        fut = self.gather(*children, loop=self.other_loop)
        self.assertIs(fut._loop, self.other_loop)
        self._run_loop(self.other_loop)
        self.assertFalse(fut.done())

    def test_one_cancellation(self):
        a, b, c, d, e = [self.one_loop.create_future() for i in range(5)]
        fut = self.gather(a, b, c, d, e)
        cb = test_utils.MockCallback()
        fut.add_done_callback(cb)
        a.set_result(1)
        b.cancel()
        self._run_loop(self.one_loop)
        self.assertTrue(fut.done())
        cb.assert_called_once_with(fut)
        self.assertFalse(fut.cancelled())
        self.assertIsInstance(fut.exception(), asyncio.CancelledError)
        # Does nothing
        c.set_result(3)
        d.cancel()
        e.set_exception(RuntimeError())
        e.exception()

    def test_result_exception_one_cancellation(self):
        a, b, c, d, e, f = [self.one_loop.create_future()
                            for i in range(6)]
        fut = self.gather(a, b, c, d, e, f, return_exceptions=True)
        cb = test_utils.MockCallback()
        fut.add_done_callback(cb)
        a.set_result(1)
        zde = ZeroDivisionError()
        b.set_exception(zde)
        c.cancel()
        self._run_loop(self.one_loop)
        self.assertFalse(fut.done())
        d.set_result(3)
        e.cancel()
        rte = RuntimeError()
        f.set_exception(rte)
        res = self.one_loop.run_until_complete(fut)
        self.assertIsInstance(res[2], asyncio.CancelledError)
        self.assertIsInstance(res[4], asyncio.CancelledError)
        res[2] = res[4] = None
        self.assertEqual(res, [1, zde, None, 3, None, rte])
        cb.assert_called_once_with(fut)

class CPythonFutureGatherTests(FutureGatherTests, test_utils.TestCase):
    gather = staticmethod(asyncio.gather)

class IgFutureGatherTests(FutureGatherTests, test_utils.TestCase):
    gather = staticmethod(_asyncio.ig_gather)

class CoroutineGatherTests(GatherTestsBase):

    def setUp(self):
        super().setUp()
        asyncio.set_event_loop(self.one_loop)

    def wrap_futures(self, *futures):
        coros = []
        for fut in futures:
            async def coro(fut=fut):
                return await fut
            coros.append(coro())
        return coros

    def test_constructor_loop_selection(self):
        async def coro():
            return 'abc'
        gen1 = coro()
        gen2 = coro()
        fut = self.gather(gen1, gen2)
        self.assertIs(fut._loop, self.one_loop)
        self.one_loop.run_until_complete(fut)

        self.set_event_loop(self.other_loop, cleanup=False)
        gen3 = coro()
        gen4 = coro()
        fut2 = self.gather(gen3, gen4, loop=self.other_loop)
        self.assertIs(fut2._loop, self.other_loop)
        self.other_loop.run_until_complete(fut2)

    def test_cancellation_broadcast(self):
        # Cancelling outer() cancels all children.
        proof = 0
        waiter = self.one_loop.create_future()

        async def inner():
            nonlocal proof
            await waiter
            proof += 1

        child1 = asyncio.ensure_future(inner(), loop=self.one_loop)
        child2 = asyncio.ensure_future(inner(), loop=self.one_loop)
        gatherer = None

        async def outer():
            nonlocal proof, gatherer
            gatherer = self.gather(child1, child2, loop=self.one_loop)
            await gatherer
            proof += 100

        f = asyncio.ensure_future(outer(), loop=self.one_loop)
        test_utils.run_briefly(self.one_loop)
        self.assertTrue(f.cancel())
        with self.assertRaises(asyncio.CancelledError):
            self.one_loop.run_until_complete(f)
        self.assertFalse(gatherer.cancel())
        self.assertTrue(waiter.cancelled())
        self.assertTrue(child1.cancelled())
        self.assertTrue(child2.cancelled())
        test_utils.run_briefly(self.one_loop)
        self.assertEqual(proof, 0)

    def test_exception_marking(self):
        # Test for the first line marked "Mark exception retrieved."

        async def inner(f):
            await f
            raise RuntimeError('should not be ignored')

        a = self.one_loop.create_future()
        b = self.one_loop.create_future()

        async def outer():
            await self.gather(inner(a), inner(b), loop=self.one_loop)

        f = asyncio.ensure_future(outer(), loop=self.one_loop)
        test_utils.run_briefly(self.one_loop)
        a.set_result(None)
        test_utils.run_briefly(self.one_loop)
        b.set_result(None)
        test_utils.run_briefly(self.one_loop)
        self.assertIsInstance(f.exception(), RuntimeError)

class CPythonCoroutineGatherTests(CoroutineGatherTests, test_utils.TestCase):
    gather = staticmethod(asyncio.gather)

    def test_duplicate_coroutines(self):
        async def coro(s):
            return s
        c = coro('abc')
        fut = self.gather(c, c, coro('def'), c, loop=self.one_loop)
        self._run_loop(self.one_loop)
        self.assertEqual(fut.result(), ['abc', 'abc', 'def', 'abc'])

class IgCoroutineGatherTests(CoroutineGatherTests, test_utils.TestCase):
    gather = staticmethod(_asyncio.ig_gather)

class RunCoroutineThreadsafeTests(test_utils.TestCase):
    """Test case for asyncio.run_coroutine_threadsafe."""

    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.set_event_loop(self.loop) # Will cleanup properly

    async def add(self, a, b, fail=False, cancel=False):
        """Wait 0.05 second and return a + b."""
        await asyncio.sleep(0.05)
        if fail:
            raise RuntimeError("Fail!")
        if cancel:
            asyncio.current_task(self.loop).cancel()
            await asyncio.sleep(0)
        return a + b

    def target(self, fail=False, cancel=False, timeout=None,
               advance_coro=False):
        """Run add coroutine in the event loop."""
        coro = self.add(1, 2, fail=fail, cancel=cancel)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if advance_coro:
            # this is for test_run_coroutine_threadsafe_task_factory_exception;
            # otherwise it spills errors and breaks **other** unittests, since
            # 'target' is interacting with threads.

            # With this call, `coro` will be advanced, so that
            # CoroWrapper.__del__ won't do anything when asyncio tests run
            # in debug mode.
            self.loop.call_soon_threadsafe(coro.send, None)
        try:
            return future.result(timeout)
        finally:
            future.done() or future.cancel()

    def test_run_coroutine_threadsafe(self):
        """Test coroutine submission from a thread to an event loop."""
        future = self.loop.run_in_executor(None, self.target)
        result = self.loop.run_until_complete(future)
        self.assertEqual(result, 3)

    def test_run_coroutine_threadsafe_with_exception(self):
        """Test coroutine submission from a thread to an event loop
        when an exception is raised."""
        future = self.loop.run_in_executor(None, self.target, True)
        with self.assertRaises(RuntimeError) as exc_context:
            self.loop.run_until_complete(future)
        self.assertIn("Fail!", exc_context.exception.args)

    def test_run_coroutine_threadsafe_with_timeout(self):
        """Test coroutine submission from a thread to an event loop
        when a timeout is raised."""
        callback = lambda: self.target(timeout=0)
        future = self.loop.run_in_executor(None, callback)
        with self.assertRaises(asyncio.TimeoutError):
            self.loop.run_until_complete(future)
        test_utils.run_briefly(self.loop)
        # Check that there's no pending task (add has been cancelled)
        for task in asyncio.all_tasks(self.loop):
            self.assertTrue(task.done())

    def test_run_coroutine_threadsafe_task_cancelled(self):
        """Test coroutine submission from a tread to an event loop
        when the task is cancelled."""
        callback = lambda: self.target(cancel=True)
        future = self.loop.run_in_executor(None, callback)
        with self.assertRaises(asyncio.CancelledError):
            self.loop.run_until_complete(future)

    def test_run_coroutine_threadsafe_task_factory_exception(self):
        """Test coroutine submission from a tread to an event loop
        when the task factory raise an exception."""

        def task_factory(loop, coro):
            raise NameError

        run = self.loop.run_in_executor(
            None, lambda: self.target(advance_coro=True))

        # Set exception handler
        callback = test_utils.MockCallback()
        self.loop.set_exception_handler(callback)

        # Set corrupted task factory
        self.loop.set_task_factory(task_factory)

        # Run event loop
        with self.assertRaises(NameError) as exc_context:
            self.loop.run_until_complete(run)

        # Check exceptions
        self.assertEqual(len(callback.call_args_list), 1)
        (loop, context), kwargs = callback.call_args
        self.assertEqual(context['exception'], exc_context.exception)


class SleepTests(test_utils.TestCase):
    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.loop = None
        super().tearDown()

    def test_sleep_zero(self):
        result = 0

        def inc_result(num):
            nonlocal result
            result += num

        async def coro():
            self.loop.call_soon(inc_result, 1)
            self.assertEqual(result, 0)
            num = await asyncio.sleep(0, result=10)
            self.assertEqual(result, 1) # inc'ed by call_soon
            inc_result(num) # num should be 11

        self.loop.run_until_complete(coro())
        self.assertEqual(result, 11)

    def test_loop_argument_is_deprecated(self):
        # Remove test when loop argument is removed in Python 3.10
        with self.assertWarns(DeprecationWarning):
            self.loop.run_until_complete(asyncio.sleep(0.01, loop=self.loop))

class FBAsyncio(test_utils.TestCase):

    def test_override_step_no_exn(self):
        result = ""

        async def f():
            nonlocal result
            result += "2"

        class CustomTask(asyncio.Task):
            def _step(self, exn=None):
                nonlocal result
                result += "1"
                super()._step(exn)
                result += "3"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.addCleanup(loop.close)
        loop.run_until_complete(CustomTask(f()))
        self.assertEqual(result, "123")

    def test_override_step_has_exn(self):
        result = ""

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.addCleanup(loop.close)

        # we want 'await fut' to not be finished synchronously
        # in order to enter '_step' for the second time with exception.
        # If we just do 'set_exception' and then await - it will be finished
        # eagerly since future is fulfiled at th suspension point.
        # What we want is to set error after the point when asyncio infrastructure
        # will decide to suspend execution. Simplest way to do it is to  set error at the point
        # when done callback is set.
        class F(asyncio.Future):
            def add_done_callback(self, callback, *, context=None):
                super().add_done_callback(callback, context=context)
                self.set_exception(ValueError("ERROR!"))

        fut = F(loop=loop)

        async def f():
            nonlocal result
            result += "|f"
            result += await fut

        class CustomTask(asyncio.Task):
            def _step(self, exn=None):
                nonlocal result
                result += "|before"
                result += "|" + str(exn)
                super()._step(exn)
                result += "|after"

        try:
            loop.run_until_complete(CustomTask(f()))
        except ValueError as e:
            self.assertEqual(e.args[0], "ERROR!")
        else:
            self.fail("ValueError expected")
        self.assertEqual(result, "|before|None|f|after|before|ERROR!|after")


class WaitTests(test_utils.TestCase):
    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.loop = None
        super().tearDown()

    def test_loop_argument_is_deprecated_in_wait(self):
        # Remove test when loop argument is removed in Python 3.10
        with self.assertWarns(DeprecationWarning):
            self.loop.run_until_complete(
                asyncio.wait([coroutine_function()], loop=self.loop))

    def test_loop_argument_is_deprecated_in_wait_for(self):
        # Remove test when loop argument is removed in Python 3.10
        with self.assertWarns(DeprecationWarning):
            self.loop.run_until_complete(
                asyncio.wait_for(coroutine_function(), 0.01, loop=self.loop))


class CompatibilityTests(test_utils.TestCase):
    # Tests for checking a bridge between old-styled coroutines
    # and async/await syntax

    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.loop = None
        super().tearDown()

    def test_yield_from_awaitable(self):

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro():
                yield from asyncio.sleep(0)
                return 'ok'

        result = self.loop.run_until_complete(coro())
        self.assertEqual('ok', result)

    def test_await_old_style_coro(self):

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro1():
                return 'ok1'

        with self.assertWarns(DeprecationWarning):
            @asyncio.coroutine
            def coro2():
                yield from asyncio.sleep(0)
                return 'ok2'

        async def inner():
            return await asyncio.gather(coro1(), coro2(), loop=self.loop)

        result = self.loop.run_until_complete(inner())
        self.assertEqual(['ok1', 'ok2'], result)

    @unittest.skipUnderUwsgi()
    def test_debug_mode_interop(self):
        # https://bugs.python.org/issue32636
        code = textwrap.dedent("""
            import asyncio

            async def native_coro():
                pass

            @asyncio.coroutine
            def old_style_coro():
                yield from native_coro()

            asyncio.run(old_style_coro())
        """)

        assert_python_ok("-Wignore::DeprecationWarning", "-c", code,
                         PYTHONASYNCIODEBUG="1")


class TestContextAwareTask(ContextAwareTask):
    _acquire_context = AcquireContextPtr
    _execute_step = DoStepPtr
    _get_context = GetContextRef

    def get_current_context(self):
        return get_context_indirect(self, GetContextRef)


class ContextAwareTaskTests(BaseTaskTests, test_utils.TestCase):
    Future = getattr(futures, '_CFuture', None)
    Task = TestContextAwareTask

    def test_context_init_and_use(self):

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.addCleanup(loop.close)

        t = None
        ctx = None
        async def coro():
            nonlocal ctx
            ctx = t.get_current_context()

        t = TestContextAwareTask(coro())
        loop.run_until_complete(t)
        self.assertEqual(ctx, {"context_var": 42})

class GatherTests:
    Task = None
    Future = None
    gather = None

    def new_task(self, loop, coro):
        return self.Task(coro, loop=loop)

    def new_future(self, loop):
        return self.Future(loop=loop)

    def setUp(self):
        super().setUp()
        self.loop = self.new_test_loop()
        self.loop.set_task_factory(self.new_task)
        self.loop.create_future = lambda: self.new_future(self.loop)

    def test_gather_with_suspended_coro(self):
        async def coro1():
            return 100

        async def coro2():
            await asyncio.sleep(0)
            return 200

        async def drive():
            res = await self.__class__.gather(coro1(), coro2())
            self.assertEqual(res, [100, 200])

        self.loop.run_until_complete(drive())

    def test_gather_contextvars(self):
        async def a(var, sleep):
            if sleep:
                await asyncio.sleep(0)
            var.set(10)
        async def b(var, sleep):
            if sleep:
                await asyncio.sleep(0)
            var.set(20)
        async def go():
            async def test(sleep_a, sleep_b):
                var = contextvars.ContextVar("var")
                var.set(42)
                await asyncio.gather(a(var, sleep_a), b(var, sleep_a))
                self.assertEqual(var.get(), 42)
            await test(False, False)
            await test(False, True)
            await test(True, False)
            await test(True, True)
        self.loop.run_until_complete(go())

    def test_gather_consistent_contextvars_for_subcoro(self):
        V = contextvars.ContextVar("V")
        V.set(42)
        async def inner():
            self.assertEqual(V.get(), 42)
            tok = V.set(0)
            self.assertEqual(V.get(), 0)
            await asyncio.sleep(0)
            V.reset(tok)
            self.assertEqual(V.get(), 42)

        async def go():
            await asyncio.gather(inner())

        self.loop.run_until_complete(go())

def modify_context(val):
    _modify_current_context(val)
    try:
        asyncio.current_task().ctx_ = val
    except RuntimeError:
        pass

class ContextAwareGatherBase:
    def is_context_aware_task(self):
        return isinstance(self.__class__.Task, ContextAwareTask)

    def new_task(self, loop, coro):
        return self.__class__.Task(coro, loop=loop)

    def new_future(self, loop):
        return self.__class__.Future(loop=loop)

    def setUp(self):
        super().setUp()

        helpers = initialize_context_helpers()
        _asyncio._set_context_helpers(*helpers)

        self.loop = self.new_test_loop()
        self.loop.set_task_factory(self.new_task)
        self.loop.create_future = lambda: self.new_future(self.loop)

    def tearDown(self):
        _asyncio._reset_context_helpers()
        delete_context()
        self.loop.close()
        asyncio.set_event_loop(None)
        super().tearDown()

class ContextAwareGatherTests(ContextAwareGatherBase):

    def test_context_aware_eager_gather_ok(self):
        async def coro1():
            self.assertEqual(get_context(), 1)
            modify_context(2)
            self.assertEqual(get_context(), 2)
            self.assertEqual(asyncio.current_task().ctx_, 2)

            await asyncio.sleep(0)

            if self.is_context_aware_task():
                self.assertEqual(get_context(), 2)
                self.assertEqual(asyncio.current_task().ctx_, 2)

            return 100

        async def coro2():
            self.assertEqual(get_context(), 1)
            modify_context(3)
            self.assertEqual(get_context(), 3)
            self.assertEqual(asyncio.current_task().ctx_, 3)

            return 200

        async def drive():
            modify_context(1)
            a, b = await asyncio.gather(coro1(), coro2())
            self.assertEqual(get_context(), 1)
            self.assertEqual(asyncio.current_task().ctx_, 1)

        self.loop.run_until_complete(drive())

    def test_context_aware_eager_gather_failed(self):
        class E(Exception):
            pass
        async def coro1():
            self.assertEqual(get_context(), 1)
            modify_context(2)
            return 10

        async def coro2():
            self.assertEqual(get_context(), 1)
            modify_context(3)
            raise E

        async def drive():
            modify_context(1)
            try:
                await asyncio.gather(coro1(), coro2())
            except E:
                pass
            else:
                self.fail("Error expected")
            self.assertEqual(get_context(), 1)
            self.assertEqual(asyncio.current_task().ctx_, 1)

        self.loop.run_until_complete(drive())

class CTaskCFutureContextAwareGatherTests(ContextAwareGatherTests, test_utils.TestCase):
    Task = getattr(tasks, '_CTask', None)
    Future = getattr(futures, '_CFuture', None)

class PyTaskPyFutureContextAwareGatherTests(ContextAwareGatherTests, test_utils.TestCase):
    Task = tasks._PyTask
    Future = futures._PyFuture

class GatherTestContextAwareTask(ContextAwareTask):
    _acquire_context, _execute_step, _get_context = get_context_helpers_for_task()

    def get_current_context(self):
        return get_context_indirect(self, GatherTestContextAwareTask._get_context)

class ContextAwareTaskCFutureContextAwareGatherTests(ContextAwareGatherTests, test_utils.TestCase):
    Task = GatherTestContextAwareTask
    Future = getattr(futures, '_CFuture', None)

    def setUp(self):
        super().setUp()
        #  set initial value for the context
        modify_context(None)

    def test_add_done_callback_preserve_context(self):
        def gen():
            yield

        loop = self.new_test_loop()
        loop.set_task_factory(self.new_task)
        f = loop.create_future()
        async def start():
            _modify_current_context(42)
            t = loop.create_task(coro())
            t.add_done_callback(do_after)
            await f

        async def coro():
            _modify_current_context(142)

        ctx = None
        def do_after(t):
            nonlocal ctx
            ctx = get_context()
            f.set_result(True)

        loop.run_until_complete(start())
        loop.close()
        self.assertEqual(ctx, 42)

class StartImmediateTests(ContextAwareGatherBase):
    def test_start_immediate_eager_ok(self):
        async def coro():
            self.assertEqual(get_context(), 1)
            modify_context(2)
            return 10

        async def drive():
            modify_context(1)
            res = _asyncio._start_immediate(coro(), self.loop)
            self.assertEqual(get_context(), 1)
            self.assertEqual(asyncio.current_task().ctx_, 1)
            self.assertIsInstance(res, _asyncio.AwaitableValue)
            self.assertEqual(res.value, 10)

        self.loop.run_until_complete(drive())

    def test_start_immediate_eager_err(self):
        class E(Exception):
            pass
        async def coro():
            self.assertEqual(get_context(), 1)
            modify_context(2)
            raise E()

        async def drive():
            modify_context(1)
            try:
                _asyncio._start_immediate(coro(), self.loop)
            except E:
                pass
            else:
                self.fail("Exception expected")
            self.assertEqual(get_context(), 1)
            self.assertEqual(asyncio.current_task().ctx_, 1)

        self.loop.run_until_complete(drive())

class CTaskCFutureStartImmediateTests(StartImmediateTests, test_utils.TestCase):
    Task = getattr(tasks, '_CTask', None)
    Future = getattr(futures, '_CFuture', None)

class PyTaskPyFutureStartImmediateTests(StartImmediateTests, test_utils.TestCase):
    Task = tasks._PyTask
    Future = futures._PyFuture

class ContextAwareTaskCFutureStartImmediateTests(StartImmediateTests, test_utils.TestCase):
    Task = GatherTestContextAwareTask
    Future = getattr(futures, '_CFuture', None)

    def setUp(self):
        super().setUp()
        #  set initial value for the context
        modify_context(None)

class CTaskCFutureGatherTests(GatherTests, test_utils.TestCase):
    Task = getattr(tasks, '_CTask', None)
    Future = getattr(futures, '_CFuture', None)
    gather = asyncio.gather

    @unittest.skipUnless(hasattr(_asyncio, 'ig_gather_no_raise'), 'requires _asyncio.ig_gather_no_raise')
    def test_return_exceptions(self):
        class E(Exception):
            pass

        async def c0():
            return 100

        async def c1():
            raise E()

        async def c2():
            return 42

        async def run():
            res = await _asyncio.ig_gather_no_raise(c0(), c1(), c2())
            self.assertEqual(res[0], 100)
            self.assertIsInstance(res[1], E)
            self.assertEqual(res[2], 42)

        self.loop.run_until_complete(run())

    def test_non_native_coro(self):
        class C:
            def __init__(self, res):
                self.res = res
                self.i = 0
            def send(self, value):
                if self.i == 0:
                    self.i = 1
                    #  yield
                    return None
                if self.i == 1:
                    raise StopIteration(self.res)

            def throw(self, *args):
                raise NotImplementedError()

            def close(self):
                raise NotImplementedError()

            def __await__(self):
                return self

            def  __next__(self):
                return self.send(None)

        async def run():
            res = await asyncio.gather(C(42), C(100))
            self.assertEqual(res, [42, 100])

        self.loop.run_until_complete(run())


    @unittest.skipUnless(hasattr(_asyncio, 'ig_gather'), 'requires _asyncio.ig_gather')
    def test_tasks_cancelled_on_error(self):
        class E(Exception):
            pass

        async def c0():
            await asyncio.sleep(0)
            self.fail("should not be here")

        async def c1():
            raise E()

        async def run():
            try:
                await _asyncio.ig_gather(c0(), c0(), c1())
                self.fail("Exception expected")
            except E:
                pass

        self.loop.run_until_complete(run())

    def test_multiple_coroutines(self):
        async def c(i):
            await asyncio.sleep(0)
            return i

        async def run():
            res = await asyncio.gather(*[c(i) for i in range(100)])
            self.assertEqual(res, [i for i in range(100)])

        self.loop.run_until_complete(run())

class PyTaskCFutureGatherTests(GatherTests, test_utils.TestCase):
    Task = Task = tasks._PyTask
    Future = getattr(futures, '_CFuture', None)
    gather = asyncio.gather

if __name__ == '__main__':
    unittest.main()
