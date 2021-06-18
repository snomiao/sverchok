# This file is part of project Sverchok. It's copyrighted by the contributors
# recorded in the version control history of the file, available from
# its original location https://github.com/nortikin/sverchok/commit/master
#
# SPDX-License-Identifier: GPL3
# License-Filename: LICENSE

import logging
import traceback
from collections import defaultdict
from functools import partial
from time import time
from typing import Dict, NamedTuple, Generator, Optional, Iterator, Tuple

import bpy
from sverchok.data_structure import post_load_call
from sverchok.core.events import TreeEvent
from sverchok.utils.logging import debug, catch_log_error, error, getLogger
from sverchok.utils.tree_structure import Tree, Node
from sverchok.utils.handle_blender_data import BlTrees


# todo wifi nodes
# todo remove heat map
# todo print timings ?? it's available in the tree now
# todo debug to a file?
# todo add tree profiling tool
# todo animation performance


class TreeHandler:

    @staticmethod
    def send(event: TreeEvent):

        # this should be first other wise other instructions can spoil the node statistic to redraw
        if NodesUpdater.is_running():
            NodesUpdater.cancel_task()

        # mark given nodes as outdated
        if event.type == TreeEvent.NODES_UPDATE:
            ContextTrees.mark_nodes_outdated(event.tree, event.updated_nodes)

        # it will find changes in tree topology and mark related nodes as outdated
        elif event.type == TreeEvent.TREE_UPDATE:
            ContextTrees.mark_tree_outdated(event.tree)

        # force update
        elif event.type == TreeEvent.FORCE_UPDATE:
            ContextTrees.mark_tree_outdated(event.tree)
            ContextTrees.mark_nodes_outdated(event.tree, event.tree.nodes)

        # Unknown event
        else:
            raise TypeError(f'Detected unknown event - {event}')

        # Add update tusk for the tree
        if event.to_update:
            NodesUpdater.add_task(event.tree)

    @staticmethod
    def get_error_nodes(bl_tree) -> Iterator[Optional[Exception]]:
        """Return map of bool values to group tree nodes where node has error if value is True"""
        for node in bl_tree.nodes:
            yield NodesStatuses.get(node).error

    @staticmethod
    def get_update_time(bl_tree) -> Iterator[Optional[int]]:
        for node in bl_tree.nodes:
            yield NodesStatuses.get(node).update_time


@post_load_call
def register_loop():

    # this function won't be reload on script.reload event (F8)
    def tree_event_loop(delay):
        with catch_log_error():
            if NodesUpdater.is_running():
                NodesUpdater.run_task()
            elif NodesUpdater.has_task():  # task should be run via timer only https://developer.blender.org/T82318#1053877
                NodesUpdater.start_task()
                NodesUpdater.run_task()
        return delay

    bpy.app.timers.register(partial(tree_event_loop, 0.01))


class NodesUpdater:
    """It can update only one tree at a time"""
    _bl_tree = None
    _handler: Optional[Generator] = None

    _node_tree_area: Optional[bpy.types.Area] = None
    _last_node: Optional[Node] = None

    _start_time: float = None

    @classmethod
    def add_task(cls, bl_tree):
        """It can handle ony one tree at a time"""
        if cls.is_running():
            raise RuntimeError(f"Can't update tree: {bl_tree.name}, already updating tree: {cls._bl_tree.name}")
        cls._bl_tree = bl_tree

    @classmethod
    def start_task(cls):
        changed_tree = cls._bl_tree
        if cls.is_running():
            raise RuntimeError(f'Tree "{changed_tree.name}" already is being updated')
        cls._handler = global_updater()

        # searching appropriate area index for reporting update progress
        for area in bpy.context.screen.areas:
            if area.ui_type == 'SverchCustomTreeType':
                path = area.spaces[0].path
                if path and path[-1].node_tree.name == changed_tree.name:
                    cls._node_tree_area = area
                    break

        cls._start_time = time()

    @classmethod
    def run_task(cls):
        try:
            if cls._last_node:
                cls._last_node.bl_tween.use_custom_color = False
                cls._last_node.bl_tween.set_color()

            start_time = time()
            while (time() - start_time) < 0.15:  # 0.15 is max timer frequency
                node = next(cls._handler)

            cls._last_node = node
            node.bl_tween.use_custom_color = True
            node.bl_tween.color = (0.7, 1.000000, 0.7)
            cls._report_progress(f'Pres "ESC" to abort, updating node "{node.name}"')

        except StopIteration:
            cls.finish_task()

    @classmethod
    def debug_run_task(cls):
        """Color updated nodes for a few second after all"""
        try:
            start_time = time()
            while (time() - start_time) < 0.15:  # 0.15 is max timer frequency
                node = next(cls._handler)
                node.bl_tween.use_custom_color = True
                node.bl_tween.color = (0.7, 1.000000, 0.7)

            cls._last_node = node
            cls._report_progress(f'Pres "ESC" to abort, updating node "{node.name}"')

        except StopIteration:
            if 'node' in vars():
                return
            from time import sleep
            sleep(2)
            cls.finish_task()

    @classmethod
    def cancel_task(cls):
        try:
            cls._handler.throw(CancelError)
        except (StopIteration, RuntimeError):
            pass
        finally:  # protection from the task to be stack forever
            cls.finish_task()

    @classmethod
    def finish_task(cls):
        try:
            debug(f'Global update - {int((time() - cls._start_time) * 1000)}ms')
            cls._report_progress()
            cls._bl_tree.update_ui()
        finally:
            cls._bl_tree, cls._handler, cls._node_tree_area, cls._last_node, cls._start_time = [None] * 5

    @classmethod
    def has_task(cls) -> bool:
        return cls._bl_tree is not None

    @classmethod
    def is_running(cls) -> bool:
        return cls._handler is not None

    @classmethod
    def _report_progress(cls, text: str = None):
        if cls._node_tree_area:
            cls._node_tree_area.header_text_set(text)


def global_updater() -> Generator[Node, None, None]:
    """Find all Sverchok main trees and run their handlers"""
    for bl_tree in BlTrees().sv_main_trees:
        if not bl_tree.sv_process:
            continue

        yield from tree_updater(bl_tree)


def tree_updater(bl_tree) -> Generator[Node, None, None]:
    tree = ContextTrees.get(bl_tree)

    for node in tree.sorted_walk(tree.output_nodes):
        can_be_updated = all(n.is_updated for n in node.last_nodes)
        if not can_be_updated:
            # here different logic can be implemented but for this we have to know if is there any output of the node
            # we could leave the node as updated and don't broke work of the rest forward nodes
            # but if the node does not have any output all next nodes will gen NoDataError what is horrible
            node.is_updated = False
            node.is_output_changed = False
            continue

        if hasattr(node.bl_tween, 'updater'):
            updater = group_node_updater(node)
        elif hasattr(node.bl_tween, 'process'):
            updater = node_updater(node)
        else:
            updater = empty_updater(node, error=None)

        # update node with sub update system, catch statistic
        start_time = time()
        node_error = yield from updater
        update_time = int((time() - start_time) * 1000)

        if node.is_output_changed or node_error:
            stat = NodeStatistic(node_error, None if node_error else update_time)
            NodesStatuses.set(node.bl_tween, stat)


class ContextTrees:
    """It keeps trees with their states"""
    _trees: Dict[str, Tree] = dict()

    @classmethod
    def get(cls, bl_tree):
        """Return caught tree or new if the tree was not build yet"""
        tree = cls._trees.get(bl_tree.tree_id)

        # new tree, all nodes are outdated
        if tree is None:
            tree = Tree(bl_tree)
            cls._trees[bl_tree.tree_id] = tree

        # topology of the tree was changed and should be updated
        elif not tree.is_updated:
            tree = cls._update_tree(bl_tree)
            cls._trees[bl_tree.tree_id] = tree

        return tree

    @classmethod
    def _update_tree(cls, bl_tree):
        """
        This method will generate new tree, copy is_updates status from previous tree
        and update 'is_input_changed' node attribute according topological changes relatively previous call
        """
        new_tree = Tree(bl_tree)

        # copy is_updated attribute
        if new_tree.id in cls._trees:
            old_tree = cls._trees[new_tree.id]
            for node in new_tree.nodes:
                if node.name in old_tree.nodes:
                    node.is_updated = old_tree.nodes[node.name].is_updated

        # update is_input_changed attribute
        cls._update_topology_status(new_tree)

        return new_tree

    @classmethod
    def mark_tree_outdated(cls, bl_tree):
        """Whenever topology of a tree is changed this method should be called."""
        tree = cls._trees.get(bl_tree.tree_id)
        if tree:
            tree.is_updated = False

    @classmethod
    def mark_nodes_outdated(cls, bl_tree, bl_nodes):
        """It will try to mark given nodes as to be recalculated.
        If node won't be found status of the tree will be changed to outdated"""
        if bl_tree.tree_id not in cls._trees:
            return  # all nodes will be outdated either way when the tree will be recreated (nothing to do)

        tree = cls._trees[bl_tree.tree_id]
        for bl_node in bl_nodes:
            try:
                tree.nodes[bl_node.name].is_updated = False

            # it means that generated tree does no have given node and should be recreated by next request
            except KeyError:
                tree.is_updated = False

    @classmethod
    def reset_data(cls):
        """
        Should be called upon loading new file, other wise it can lead to errors and even crash
        Also according the fact that trees have links to real blender nodes
        it is also important to call this method upon undo method otherwise errors and crashes
        """
        cls._trees.clear()

    @classmethod
    def _update_topology_status(cls, new_tree: Tree):
        """Copy link node status by comparing with previous tree and save current"""
        if new_tree.id in cls._trees:
            old_tree = cls._trees[new_tree.id]

            new_links = new_tree.links - old_tree.links
            for link in new_links:
                if link.from_node.name in old_tree.nodes:
                    from_old_node = old_tree.nodes[link.from_node.name]
                    from_old_socket = from_old_node.get_output_socket(link.from_socket.identifier)
                    has_old_from_socket_links = from_old_socket.links if from_old_socket is not None else False
                else:
                    has_old_from_socket_links = False

                # this is only because some nodes calculated data only if certain output socket is connected
                # ideally we would not like ot make previous node outdated, but it requires changes in many nodes
                if not has_old_from_socket_links:
                    link.from_node.is_input_changed = True
                else:
                    link.to_node.is_input_changed = True

            removed_links = old_tree.links - new_tree.links
            for link in removed_links:
                if link.to_node in new_tree.nodes:
                    new_tree.nodes[link.to_node.name].is_input_changed = True


class NodeStatistic(NamedTuple):
    """
    Statistic should be kept separately for each node
    because each node can have 10 or even 100 of different statistic profiles according number of group nodes using it
    """
    error: Optional[Exception] = None
    update_time: int = None  # ms


class NodesStatuses:
    """It keeps node attributes"""
    NodeId = str
    _statuses: Dict[NodeId, NodeStatistic] = defaultdict(NodeStatistic)

    @classmethod
    def get(cls, bl_node) -> NodeStatistic:
        return cls._statuses[bl_node.node_id]

    @classmethod
    def set(cls, bl_node, stat: NodeStatistic):
        node_id = bl_node.node_id
        cls._statuses[node_id] = stat

    @classmethod
    def reset_data(cls):
        """This method should be called before opening new file to free all statistic data"""
        cls._statuses.clear()


class CancelError(Exception):
    """Aborting tree evaluation by user"""


def node_updater(node: Node, *args) -> Generator[Node, None, Optional[Exception]]:
    """The node should has process method, all previous nodes should be updated"""
    node_error = None

    previous_nodes_are_changed = any(n.is_output_changed for n in node.last_nodes)
    should_be_updated = not node.is_updated or node.is_input_changed or previous_nodes_are_changed

    node.is_output_changed = False  # it should always False unless the process method was called
    node.is_input_changed = False  # if node wont be able to handle new input it will be seen in its update status
    if should_be_updated:
        try:
            yield node
            node.bl_tween.process(*args)
            node.is_updated = True
            node.is_output_changed = True
        except CancelError as e:
            node.is_updated = False
            node_error = e
        except Exception as e:
            node.is_updated = False
            error(e)
            logger = getLogger()
            if logger.isEnabledFor(logging.DEBUG):
                traceback.print_exc()
            node_error = e
    return node_error


def group_node_updater(node: Node) -> Generator[Node, None, Tuple[bool, Optional[Exception]]]:
    """The node should have updater attribute"""
    previous_nodes_are_changed = any(n.is_output_changed for n in node.last_nodes)
    should_be_updated = (not node.is_updated or node.is_input_changed or previous_nodes_are_changed)
    yield node  # yield groups node so it be colored by node Updater if necessary
    updater = node.bl_tween.updater(is_input_changed=should_be_updated)
    is_output_changed, out_error = yield from updater
    node.is_input_changed = False
    node.is_updated = not out_error
    node.is_output_changed = is_output_changed
    return out_error


def empty_updater(node: Node = None, **kwargs):
    """Reroutes, frame nodes, empty updaters which do nothing, set node in correct state
     returns given kwargs (only their values) like error=None, is_updated=True"""
    if node:  # ideally we would like always get first argument as node but group updater does not posses it
        previous_nodes_are_changed = any(n.is_output_changed for n in node.last_nodes)
        should_be_updated = not node.is_updated or node.is_input_changed or previous_nodes_are_changed
        node.is_input_changed = False  # if node wont be able to handle new input it will be seen in its update status
        node.is_updated = True
        node.is_output_changed = True if should_be_updated else False
    return tuple(kwargs.values())
    yield
