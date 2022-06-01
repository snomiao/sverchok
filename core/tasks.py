import gc
from time import time
from functools import partial, cached_property, cache
from typing import TYPE_CHECKING, Optional, Generator

import bpy
from sverchok.data_structure import post_load_call
from sverchok.core.sv_custom_exceptions import CancelError
from sverchok.utils.logging import catch_log_error, debug
from sverchok.utils.profile import profile
from sverchok.utils.handle_blender_data import BlTrees

if TYPE_CHECKING:
    from sverchok.node_tree import SverchCustomTree as SvTree


class Tasks:
    """
    It keeps tasks which should be executed and executes the on demand.
    1. Execute tasks
    2. Time the whole execution
    3. Display the progress in the UI
    """
    _todo: set['Task']
    _current: Optional['Task']

    def __init__(self):
        """:_todo: list of tasks to run
        :_current: task which was started to execute"""
        self._todo = set()
        self._current = None

    def __bool__(self):
        """Has anything to do?"""
        return bool(self._current or self._todo)

    def add(self, task: 'Task'):
        """Add new tasks to run them via timer"""
        self._todo.add(task)

    @profile(section="UPDATE")
    def run(self):
        """Run given tasks to update trees and report execution process in the
        header of a node tree editor"""
        max_duration = 0.15  # 0.15 is max timer frequency
        duration = 0

        while self.current:
            if duration > max_duration:
                return
            # print(f"Run task: {self.current}")
            duration += self.current.run(max_duration-duration)
            if self.current.last_node:
                msg = f'Pres "ESC" to abort, updating node "{self.current.last_node.name}"'
                self._report_progress(msg)
            if self.current.is_exhausted:
                self._next()

        self._finish()

    def cancel(self):
        """Remove all tasks in the queue and abort current one"""
        self._todo.clear()
        if self._current:
            try:
                self._current.throw(CancelError)
            except (StopIteration, RuntimeError):
                pass
            finally:  # protection from the task to be stack forever
                self._finish()

    @property
    def current(self) -> Optional['Task']:
        """Return current task if it is absent it tries to pop it from the tasks
        queue if it's empty returns None"""
        if self._current:
            return self._current
        elif self._todo:
            self._start()
            self._current = self._todo.pop()
            return self._current
        else:
            return None

    def _start(self):
        """Preprocessing before executing the whole queue of events"""
        self._start_time
        gc.disable()  # for performance

    def _next(self):
        """Should be called to switch to next tasks when current is exhausted
        It made some cleanups after the previous task"""
        self._report_progress()
        self._current = self._todo.pop() if self._todo else None
        del self._main_area

    def _finish(self):
        """Cleanups. Also triggers scene handler and mark trees to skip it"""
        self._report_progress()
        del self._main_area

        # this only need to trigger scene changes handler again
        # todo should be proved that this is right location to call from
        bpy.context.scene.update_tag()

        # this indicates that process of the tree is finished and next scene event can be skipped
        # the scene trigger will try to update all trees, so they all should be marked
        for t in BlTrees().sv_main_trees:
            t['SKIP_UPDATE'] = True

        gc.enable()
        debug(f'Global update - {int((time() - self._start_time) * 1000)}ms')
        del self._start_time

    @cached_property
    def _start_time(self):
        """Start time of execution the whole queue of tasks"""
        return time()

    @cached_property
    def _main_area(self) -> Optional:
        """Searching appropriate area index for reporting update progress"""
        if not self.current:
            return
        for area in bpy.context.screen.areas:
            if area.ui_type == 'SverchCustomTreeType':
                path = area.spaces[0].path
                if path and path[-1].node_tree.name == self._current.tree.name:
                    return area

    def _report_progress(self, text: str = None):
        """Show text in the tree editor header. If text is none the header
        returns in its initial condition"""
        if self._main_area:
            self._main_area.header_text_set(text)


tasks = Tasks()


def tree_event_loop(delay):
    """Sverchok tasks handler"""
    with catch_log_error():
        if tasks:
            tasks.run()
    return delay


tree_event_loop = partial(tree_event_loop, 0.01)


class Task:
    """Generator which should update some node tree. The task is hashable, and
    it is equal to another task if booth of them update the same tree.
    The generator is suspendable and can limit its execution by given time"""
    def __init__(self, tree, updater):
        """:tree: tree which should be updated
        :_updater: generator which should update given tree
        :is_exhausted: the status of the generator - read only
        :last_node: last node which going to be processed by the generator
        - read only"""
        self.tree: SvTree = tree
        self.is_exhausted = False
        self.last_node = None

        self._updater: Generator = updater
        self.__hash__ = cache(self.__hash__)

    def run(self, max_duration):
        """Starts the tree updating
        :max_duration: if updating of the tree takes more time than given
        maximum duration it saves its state and returns execution flow"""
        duration = 0
        try:
            start_time = time()
            while duration < max_duration:
                self.last_node = next(self._updater)
                duration = time() - start_time
            return duration

        except StopIteration:
            self.is_exhausted = True
            return duration

    def throw(self, error: CancelError):
        """Should be used to cansel tree execution. Updater should add
        the error to current node and abort the execution"""
        self._updater.throw(error)
        self.is_exhausted = True

    def __eq__(self, other: 'Task'):
        return self.tree.tree_id == other.tree.tree_id

    def __hash__(self):
        return hash(self.tree.tree_id)

    def __repr__(self):
        return f"<Task: {self.tree.name}>"


@post_load_call
def post_load_register():
    # when new file is loaded all timers are unregistered
    # to make them persistent the post load handler should be used
    # but it's also is possible that the timer was registered during registration of the add-on
    if not bpy.app.timers.is_registered(tree_event_loop):
        bpy.app.timers.register(tree_event_loop)


def register():
    """Registration of Sverchok event handler"""
    # it appeared that the timers can be registered during the add-on initialization
    # The timer should be registered here because post_load_register won't be called when an add-on is enabled by user
    bpy.app.timers.register(tree_event_loop)


def unregister():
    bpy.app.timers.unregister(tree_event_loop)
