from __future__ import absolute_import

from .dependency_graph import DependencyGraph
from .ready_surface import ReadySurface
from .task_state import TaskState
from .processing_queue import ProcessingQueue
from .task import Task
from .block import BlockStatus

from typing import List
import collections
import logging

logger = logging.getLogger(__name__)


class Scheduler:
    """This is the main scheduler that tracks states of tasks.

    The Scheduler takes a list of tasks, and upon request will
    provide the next block available for processing.

    Usage:

    .. code:: python

        graph = DependencyGraph(...)
        return Scheduler().distribute(graph)

    See the DependencyGraph class for more information.
    """

    def __init__(self, tasks: List[Task], count_all_orphans=False):
        self.dependency_graph = DependencyGraph(tasks)
        self.ready_surface = ReadySurface(
            self.dependency_graph.downstream, self.dependency_graph.upstream
        )

        self.task_map = {}
        self.task_states = collections.defaultdict(TaskState)
        self.task_queues = collections.defaultdict(ProcessingQueue)

        # root tasks is a mapping from task_id -> (num_roots, root_generator)
        for task_id, (num_roots, root_gen) in self.dependency_graph.roots().items():
            self.task_states[task_id].ready_count += num_roots
            self.task_queues[task_id] = ProcessingQueue(num_roots, root_gen)

        for task in tasks:
            self.__init_task(task)

        self.completed_surface = set()
        self.failed_surface = set()
        self.block_statuses = collections.defaultdict(BlockStatus)
        self.count_all_orphans = count_all_orphans

        self.last_prechecked = collections.defaultdict(lambda: (None, None))

    def __init_task(self, task):
        if task.task_id not in self.task_map:
            self.task_map[task.task_id] = task
            self.task_states[
                task.task_id
            ].total_block_count = self.dependency_graph.num_blocks(task.task_id)

            for upstream_task in task.requires():
                self.__init_task(upstream_task)

    def has_next(self, task_id):
        return self.task_queues[task_id].has_next()

    def _queue_ready_block(self, block, index=None):
        if index is None:
            self.task_queues[block.task_id].ready_queue.append(block)
        else:
            self.task_queues[block.task_id].read_queue.insert(index, block)
        self.task_states[block.task_id].ready_count += 1

    def get_ready_tasks(self) -> List[Task]:
        ready_tasks = []
        for task_id, task_state in self.task_states.items():
            if task_state.ready_count > 0:
                ready_tasks.append(self.task_map[task_id])
        return ready_tasks

    def acquire_block(self, task_id):
        """
        Get a block that is ready to process for task with given task_id.

        Args:
            task_id(``int``):
                The task for which you want a block

        Return:
            ``Block`` or None:
                A block that can be run without worry of
                conflicts.
            ``TaskState``:
                The state of the task.
        """
        if self.has_next(task_id):
            block = self.task_queues[task_id].get_next()

            # update states
            self.task_states[task_id].ready_count -= 1
            self.task_states[task_id].processing_count += 1

            pre_check_ret = self.precheck(block)
            if pre_check_ret:
                logger.debug(f"Skipping block ({block.block_id}); already processed.")
                block.status = BlockStatus.SUCCESS
                self.release_block(block)
                return self.acquire_block(task_id)
            else:
                self.task_states[task_id].started = (
                    self.task_states[task_id].started or True
                )
                self.task_queues[task_id].processing_blocks.add(block.block_id)
                return block

        else:
            return None

    def release_block(self, block):
        """
        Update the dependency graph with the status
        of a given block ``block`` on task ``task``.

        Args:
            task(``Task``):
                Task of interest.

            block(``Block``):
                Block of interest.

        Return:
            ``dict``(``task_id`` -> ``TaskStatus``):
            Each task returned had its
            state changed by updating the status of the given
            block on the given task. i.e. if a task B was
            dependent on task A, and marking a block in A
            as solved made some blocks in B available for
            processing, task B would update its state from
            waiting to Waiting to Ready and be returned.
        """
        task_id = block.task_id
        self.remove_from_processing_blocks(block)
        if block.status == BlockStatus.SUCCESS:
            new_blocks = self.ready_surface.mark_success(block)
            self.task_states[block.task_id].completed_count += 1
            updated_tasks = self.update_ready_queue(new_blocks)
            return updated_tasks
        if block.status == BlockStatus.FAILED:
            if (
                self.task_queues[task_id].block_retries[block.block_id]
                >= self.task_map[task_id].max_retries
            ):
                num_orphans = self.ready_surface.mark_failure(
                    block, count_all_orphans=self.count_all_orphans
                )
                self.task_states[block.task_id].failed_count += 1
                self.task_states[block.task_id].orphaned_count += num_orphans
                return {}
            else:
                self._queue_ready_block(block)
                self.task_queues[task_id].block_retries[block.block_id] += 1
                return {task_id: self.task_states[task_id]}
        else:
            raise RuntimeError(f"Unexpected status for released block: {block.status}")

    def remove_from_processing_blocks(self, block):
        self.task_queues[block.task_id].processing_blocks.remove(block.block_id)
        self.task_states[block.task_id].processing_count -= 1

    def update_ready_queue(self, ready_blocks):
        updated_tasks = {}
        for ready_block in ready_blocks:
            self._queue_ready_block(ready_block)
            updated_tasks[ready_block.task_id] = self.task_states[ready_block.task_id]
        return updated_tasks

    def precheck(self, task_id, block):
        if self.last_prechecked[task_id][0] != block:
            # pre-check and skip blocks if possible
            try:
                # pre_check can intermittently fail
                # so we wrap it in a try block
                if self.task_map[task_id].check_function is not None:
                    pre_check_ret = self.task_map[task_id].check_function(block)
                else:
                    pre_check_ret = False
            except Exception as e:
                logger.error(
                    "pre_check() exception for block %s of task %s. " "Exception: %s",
                    block,
                    task_id,
                    e,
                )
                pre_check_ret = False
            finally:
                self.last_prechecked[task_id] = (block, pre_check_ret)

        return self.last_prechecked[task_id][1]


def distribute():
    raise NotImplementedError()
