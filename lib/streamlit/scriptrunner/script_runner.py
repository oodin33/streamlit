# Copyright 2018-2022 Streamlit Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import sys
import threading
import types
from contextlib import contextmanager
from enum import Enum
from typing import Optional, Callable

from blinker import Signal

from streamlit import config
from streamlit import magic
from streamlit import source_util
from streamlit import util
from streamlit.error_util import handle_uncaught_app_exception
from streamlit.in_memory_file_manager import in_memory_file_manager
from streamlit.logger import get_logger
from streamlit.proto.ClientState_pb2 import ClientState
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
from streamlit.session_data import SessionData
from streamlit.state import (
    SessionState,
    SCRIPT_RUN_WITHOUT_ERRORS_KEY,
    SafeSessionState,
)
from streamlit.uploaded_file_manager import UploadedFileManager
from .script_run_context import ScriptRunContext, add_script_run_ctx, get_script_run_ctx
from .script_requests import (
    ScriptRequests,
    RerunData,
    ScriptRequestType,
)

LOGGER = get_logger(__name__)


class ScriptRunnerEvent(Enum):
    ## "Control" events. These are emitted when the ScriptRunner's state changes.

    # The script started running.
    SCRIPT_STARTED = "SCRIPT_STARTED"

    # The script run stopped because of a compile error.
    SCRIPT_STOPPED_WITH_COMPILE_ERROR = "SCRIPT_STOPPED_WITH_COMPILE_ERROR"

    # The script run stopped because it ran to completion, or was
    # interrupted by the user.
    SCRIPT_STOPPED_WITH_SUCCESS = "SCRIPT_STOPPED_WITH_SUCCESS"

    # The ScriptRunner is done processing the ScriptEventQueue and
    # is shut down.
    SHUTDOWN = "SHUTDOWN"

    ## "Data" events. These are emitted when the ScriptRunner's script has
    ## data to send to the frontend.

    # The script has a ForwardMsg to send to the frontend.
    ENQUEUE_FORWARD_MSG = "ENQUEUE_FORWARD_MSG"


"""
Note [Threading]
There are two kinds of threads in Streamlit, the main thread and script threads.
The main thread is started by invoking the Streamlit CLI, and bootstraps the
framework and runs the Tornado webserver.
A script thread is created by a ScriptRunner when it starts. The script thread
is where the ScriptRunner executes, including running the user script itself,
processing messages to/from the frontend, and all the Streamlit library function
calls in the user script.
It is possible for the user script to spawn its own threads, which could call
Streamlit functions. We restrict the ScriptRunner's execution control to the
script thread. Calling Streamlit functions from other threads is unlikely to
work correctly due to lack of ScriptRunContext, so we may add a guard against
it in the future.
"""


class ScriptRunner:
    def __init__(
        self,
        session_id: str,
        session_data: SessionData,
        client_state: ClientState,
        session_state: SessionState,
        uploaded_file_mgr: UploadedFileManager,
        initial_rerun_data: RerunData,
    ):
        """Initialize the ScriptRunner.

        (The ScriptRunner won't start executing until start() is called.)

        Parameters
        ----------
        session_id : str
            The AppSession's id.

        session_data : SessionData
            The AppSession's session data.

        client_state : ClientState
            The current state from the client (widgets and query params).

        uploaded_file_mgr : UploadedFileManager
            The File manager to store the data uploaded by the file_uploader widget.

        """
        self._session_id = session_id
        self._session_data = session_data
        self._uploaded_file_mgr = uploaded_file_mgr

        # Initialize SessionState with the latest widget states
        session_state.set_widgets_from_proto(client_state.widget_states)

        self._client_state = client_state
        self._session_state = SafeSessionState(session_state)

        self._requests = ScriptRequests()
        self._requests.request_rerun(initial_rerun_data)

        self.on_event = Signal(
            doc="""Emitted when a ScriptRunnerEvent occurs.

            This signal is generally emitted on the ScriptRunner's script
            thread (which is *not* the same thread that the ScriptRunner was
            created on).

            Parameters
            ----------
            sender: ScriptRunner
                The sender of the event (this ScriptRunner).

            event : ScriptRunnerEvent

            forward_msg : ForwardMsg | None
                The ForwardMsg to send to the frontend. Set only for the
                ENQUEUE_FORWARD_MSG event.

            exception : BaseException | None
                Our compile error. Set only for the
                SCRIPT_STOPPED_WITH_COMPILE_ERROR event.

            widget_states : streamlit.proto.WidgetStates_pb2.WidgetStates | None
                The ScriptRunner's final WidgetStates. Set only for the
                SHUTDOWN event.
            """
        )

        # Set to true while we're executing. Used by
        # _maybe_handle_execution_control_request.
        self._execing = False

        # This is initialized in start()
        self._script_thread: Optional[threading.Thread] = None

    def __repr__(self) -> str:
        return util.repr_(self)

    def request_stop(self) -> None:
        """Request that the ScriptRunner stop running its script and
        shut down. The ScriptRunner will handle this request when it reaches
        an interrupt point.

        Safe to call from any thread.
        """
        self._requests.request_stop()

        # "Disconnect" our SafeSessionState wrapper from its underlying
        # SessionState instance. This will cause all further session_state
        # operations in this ScriptRunner to no-op.
        #
        # After `request_stop` is called, our script will continue executing
        # until it reaches a yield point. AppSession may also *immediately*
        # spin up a new ScriptRunner after this call, which means we'll
        # potentially have two active ScriptRunners for a brief period while
        # this one is shutting down. Disconnecting our SessionState ensures
        # that this ScriptRunner's thread won't introduce SessionState-
        # related race conditions during this script overlap.
        self._session_state.disconnect()

    def request_rerun(self, rerun_data: RerunData) -> bool:
        """Request that the ScriptRunner interrupt its currently-running
        script and restart it.

        If the ScriptRunner has been stopped, this request can't be honored:
        return False.

        Otherwise, record the request and return True. The ScriptRunner will
        handle the rerun request as soon as it reaches an interrupt point.

        Safe to call from any thread.
        """
        return self._requests.request_rerun(rerun_data)

    def start(self) -> None:
        """Start a new thread to process the ScriptEventQueue.

        This must be called only once.

        """
        if self._script_thread is not None:
            raise Exception("ScriptRunner was already started")

        self._script_thread = threading.Thread(
            target=self._run_script_thread,
            name="ScriptRunner.scriptThread",
        )
        self._script_thread.start()

    def _get_script_run_ctx(self) -> ScriptRunContext:
        """Get the ScriptRunContext for the current thread.

        Returns
        -------
        ScriptRunContext
            The ScriptRunContext for the current thread.

        Raises
        ------
        AssertionError
            If called outside of a ScriptRunner thread.
        RuntimeError
            If there is no ScriptRunContext for the current thread.

        """
        assert self._is_in_script_thread()

        ctx = get_script_run_ctx()
        if ctx is None:
            # This should never be possible on the script_runner thread.
            raise RuntimeError(
                "ScriptRunner thread has a null ScriptRunContext. Something has gone very wrong!"
            )
        return ctx

    def _run_script_thread(self) -> None:
        """The entry point for the script thread.

        Processes the ScriptRequestQueue, which will at least contain the RERUN
        request that will trigger the first script-run.

        When the ScriptRequestQueue is empty, or when a SHUTDOWN request is
        dequeued, this function will exit and its thread will terminate.
        """
        assert self._is_in_script_thread()

        LOGGER.debug("Beginning script thread")

        # Create and attach the thread's ScriptRunContext
        ctx = ScriptRunContext(
            session_id=self._session_id,
            enqueue=self._enqueue_forward_msg,
            query_string=self._client_state.query_string,
            session_state=self._session_state,
            uploaded_file_mgr=self._uploaded_file_mgr,
        )
        add_script_run_ctx(threading.current_thread(), ctx)

        request = self._requests.on_scriptrunner_ready()
        while request.type == ScriptRequestType.RERUN:
            # When the script thread starts, we'll have a pending rerun
            # request that we'll handle immediately. When the script finishes,
            # it's possible that another request has come in that we need to
            # handle, which is why we call _run_script in a loop.
            self._run_script(request.rerun_data)
            request = self._requests.on_scriptrunner_ready()

        assert request.type == ScriptRequestType.STOP

        # Send a SHUTDOWN event before exiting. This includes the widget values
        # as they existed after our last successful script run, which the
        # AppSession will pass on to the next ScriptRunner that gets
        # created.
        client_state = ClientState()
        client_state.query_string = ctx.query_string
        widget_states = self._session_state.get_widget_states()
        client_state.widget_states.widgets.extend(widget_states)
        self.on_event.send(
            self, event=ScriptRunnerEvent.SHUTDOWN, client_state=client_state
        )

    def _is_in_script_thread(self) -> bool:
        """True if the calling function is running in the script thread"""
        return self._script_thread == threading.current_thread()

    def _enqueue_forward_msg(self, msg: ForwardMsg) -> None:
        """Enqueue a ForwardMsg to our browser queue.
        This private function is called by ScriptRunContext only.

        It may be called from the script thread OR the main thread.
        """
        # Whenever we enqueue a ForwardMsg, we also handle any pending
        # execution control request. This means that a script can be
        # cleanly interrupted and stopped inside most `st.foo` calls.
        #
        # (If "runner.installTracer" is true, then we'll actually be
        # handling these requests in a callback called after every Python
        # instruction instead.)
        if not config.get_option("runner.installTracer"):
            self._maybe_handle_execution_control_request()

        # Pass the message to our associated AppSession.
        self.on_event.send(
            self, event=ScriptRunnerEvent.ENQUEUE_FORWARD_MSG, forward_msg=msg
        )

    def _maybe_handle_execution_control_request(self) -> None:
        """Check our current ScriptRequestState to see if we have a
        pending STOP or RERUN request.

        This function is called every time the app script enqueues a
        ForwardMsg, which means that most `st.foo` commands - which generally
        involve sending a ForwardMsg to the frontend - act as implicit
        yield points in the script's execution.
        """
        if not self._is_in_script_thread():
            # We can only handle execution_control_request if we're on the
            # script execution thread. However, it's possible for deltas to
            # be enqueued (and, therefore, for this function to be called)
            # in separate threads, so we check for that here.
            return

        if not self._execing:
            # If the _execing flag is not set, we're not actually inside
            # an exec() call. This happens when our script exec() completes,
            # we change our state to STOPPED, and a statechange-listener
            # enqueues a new ForwardEvent
            return

        request = self._requests.on_scriptrunner_yield()
        if request is None:
            # No RERUN or STOP request.
            return

        if request.type == ScriptRequestType.RERUN:
            raise RerunException(request.rerun_data)

        assert request.type == ScriptRequestType.STOP
        raise StopException()

    def _install_tracer(self) -> None:
        """Install function that runs before each line of the script."""

        def trace_calls(frame, event, arg):
            self._maybe_handle_execution_control_request()
            return trace_calls

        # Python interpreters are not required to implement sys.settrace.
        if hasattr(sys, "settrace"):
            sys.settrace(trace_calls)

    @contextmanager
    def _set_execing_flag(self):
        """A context for setting the ScriptRunner._execing flag.

        Used by _maybe_handle_execution_control_request to ensure that
        we only handle requests while we're inside an exec() call
        """
        if self._execing:
            raise RuntimeError("Nested set_execing_flag call")
        self._execing = True
        try:
            yield
        finally:
            self._execing = False

    def _run_script(self, rerun_data: RerunData) -> None:
        """Run our script.

        Parameters
        ----------
        rerun_data: RerunData
            The RerunData to use.

        """
        assert self._is_in_script_thread()

        LOGGER.debug("Running script %s", rerun_data)

        # Reset DeltaGenerators, widgets, media files.
        in_memory_file_manager.clear_session_files()

        ctx = self._get_script_run_ctx()
        ctx.reset(query_string=rerun_data.query_string)

        self.on_event.send(self, event=ScriptRunnerEvent.SCRIPT_STARTED)

        # Compile the script. Any errors thrown here will be surfaced
        # to the user via a modal dialog in the frontend, and won't result
        # in their previous script elements disappearing.

        try:
            with source_util.open_python_file(self._session_data.main_script_path) as f:
                filebody = f.read()

            if config.get_option("runner.magicEnabled"):
                filebody = magic.add_magic(
                    filebody, self._session_data.main_script_path
                )

            code = compile(
                filebody,
                # Pass in the file path so it can show up in exceptions.
                self._session_data.main_script_path,
                # We're compiling entire blocks of Python, so we need "exec"
                # mode (as opposed to "eval" or "single").
                mode="exec",
                # Don't inherit any flags or "future" statements.
                flags=0,
                dont_inherit=1,
                # Use the default optimization options.
                optimize=-1,
            )

        except BaseException as e:
            # We got a compile error. Send an error event and bail immediately.
            LOGGER.debug("Fatal script error: %s", e)
            self._session_state[SCRIPT_RUN_WITHOUT_ERRORS_KEY] = False
            self.on_event.send(
                self,
                event=ScriptRunnerEvent.SCRIPT_STOPPED_WITH_COMPILE_ERROR,
                exception=e,
            )
            return

        # If we get here, we've successfully compiled our script. The next step
        # is to run it. Errors thrown during execution will be shown to the
        # user as ExceptionElements.

        if config.get_option("runner.installTracer"):
            self._install_tracer()

        # This will be set to a RerunData instance if our execution
        # is interrupted by a RerunException.
        rerun_exception_data: Optional[RerunData] = None

        try:
            # Create fake module. This gives us a name global namespace to
            # execute the code in.
            module = _new_module("__main__")

            # Install the fake module as the __main__ module. This allows
            # the pickle module to work inside the user's code, since it now
            # can know the module where the pickled objects stem from.
            # IMPORTANT: This means we can't use "if __name__ == '__main__'" in
            # our code, as it will point to the wrong module!!!
            sys.modules["__main__"] = module

            # Add special variables to the module's globals dict.
            # Note: The following is a requirement for the CodeHasher to
            # work correctly. The CodeHasher is scoped to
            # files contained in the directory of __main__.__file__, which we
            # assume is the main script directory.
            module.__dict__["__file__"] = self._session_data.main_script_path

            with modified_sys_path(self._session_data), self._set_execing_flag():
                # Run callbacks for widgets whose values have changed.
                if rerun_data.widget_states is not None:
                    self._session_state.on_script_will_rerun(rerun_data.widget_states)

                ctx.on_script_start()
                exec(code, module.__dict__)
                self._session_state[SCRIPT_RUN_WITHOUT_ERRORS_KEY] = True
        except RerunException as e:
            rerun_exception_data = e.rerun_data

        except StopException:
            pass

        except BaseException as e:
            self._session_state[SCRIPT_RUN_WITHOUT_ERRORS_KEY] = False
            handle_uncaught_app_exception(e)

        finally:
            self._on_script_finished(ctx)

        # Use _log_if_error() to make sure we never ever ever stop running the
        # script without meaning to.
        _log_if_error(_clean_problem_modules)

        if rerun_exception_data is not None:
            self._run_script(rerun_exception_data)

    def _on_script_finished(self, ctx: ScriptRunContext) -> None:
        """Called when our script finishes executing, even if it finished
        early with an exception. We perform post-run cleanup here.
        """
        # Tell session_state to update itself in response
        self._session_state.on_script_finished(ctx.widget_ids_this_run)

        # Signal that the script has finished. (We use SCRIPT_STOPPED_WITH_SUCCESS
        # even if we were stopped with an exception.)
        self.on_event.send(self, event=ScriptRunnerEvent.SCRIPT_STOPPED_WITH_SUCCESS)
        # Delete expired files now that the script has run and files in use
        # are marked as active.
        in_memory_file_manager.del_expired_files()

        # Force garbage collection to run, to help avoid memory use building up
        # This is usually not an issue, but sometimes GC takes time to kick in and
        # causes apps to go over resource limits, and forcing it to run between
        # script runs is low cost, since we aren't doing much work anyway.
        if config.get_option("runner.postScriptGC"):
            gc.collect(2)


class ScriptControlException(BaseException):
    """Base exception for ScriptRunner."""

    pass


class StopException(ScriptControlException):
    """Silently stop the execution of the user's script."""

    pass


class RerunException(ScriptControlException):
    """Silently stop and rerun the user's script."""

    def __init__(self, rerun_data: RerunData):
        """Construct a RerunException

        Parameters
        ----------
        rerun_data : RerunData
            The RerunData that should be used to rerun the script
        """
        self.rerun_data = rerun_data

    def __repr__(self) -> str:
        return util.repr_(self)


def _clean_problem_modules() -> None:
    """Some modules are stateful, so we have to clear their state."""

    if "keras" in sys.modules:
        try:
            keras = sys.modules["keras"]
            keras.backend.clear_session()
        except:
            pass

    if "matplotlib.pyplot" in sys.modules:
        try:
            plt = sys.modules["matplotlib.pyplot"]
            plt.close("all")
        except:
            pass


def _new_module(name: str) -> types.ModuleType:
    """Create a new module with the given name."""
    return types.ModuleType(name)


# Code modified from IPython (BSD license)
# Source: https://github.com/ipython/ipython/blob/master/IPython/utils/syspathcontext.py#L42
class modified_sys_path:
    """A context for prepending a directory to sys.path for a second."""

    def __init__(self, session_data: SessionData):
        self._session_data = session_data
        self._added_path = False

    def __repr__(self) -> str:
        return util.repr_(self)

    def __enter__(self):
        if self._session_data.main_script_path not in sys.path:
            sys.path.insert(0, self._session_data.main_script_path)
            self._added_path = True

    def __exit__(self, type, value, traceback):
        if self._added_path:
            try:
                sys.path.remove(self._session_data.main_script_path)
            except ValueError:
                pass

        # Returning False causes any exceptions to be re-raised.
        return False


# The reason this is not a decorator is because we want to make it clear at the
# calling location that this function is being used.
def _log_if_error(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:
        LOGGER.warning(e)