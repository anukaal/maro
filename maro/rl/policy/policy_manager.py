# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import time
from abc import ABC, abstractmethod
from collections import defaultdict, namedtuple
from multiprocessing import Pipe, Process
from os import getcwd
from typing import Callable, Dict

from maro.communication import Proxy, SessionMessage, SessionType
from maro.rl.experience import ExperienceSet
from maro.rl.policy import CorePolicy
from maro.rl.utils import MsgKey, MsgTag
from maro.utils import Logger

from .trainer import trainer_process


PolicyUpdateOptions = namedtuple(
    "PolicyUpdateOptions", ["update_trigger", "warmup", "num_epochs", "reset_memory", "data_parallel"]
)


class AbsPolicyManager(ABC):
    """Manage all policies.

    The actual policy instances may reside here or be distributed on a set of processes or remote nodes.

    Args:
        policy_dict (Dict[str, CorePolicy]): A list of policies managed by the manager.
        num_epochs (Dict[str, int]): Number of learning epochs for each policy. This determine the number of
            times ``policy.learn()`` is called in each call to ``update``. Defaults to 1 for each policy.
        update_trigger (Dict[str, int]): A dictionary of (policy_name, trigger), where "trigger" indicates the
            required number of new experiences to trigger a call to ``learn`` for each policy. Defaults to 1 for
            each policy.
        warmup (Dict[str, int]): A dictionary of (policy_name, warmup_size), where "warmup_size" indicates the
            minimum number of experiences in the experience memory required to trigger a call to ``learn`` for
            each policy. Defaults to 1 for each policy.
        reset_memory (Dict[str, bool]): A dictionary of flags indicating whether each policy's experience memory
            should be reset after it is updated. It may be necessary to set this to True for on-policy algorithms
            to ensure that the experiences to be learned from stay up to date. Defaults to False for each policy.
    """
    def __init__(self, policy_dict: Dict[str, CorePolicy], update_option: Dict[str, PolicyUpdateOptions]):
        for policy in policy_dict.values():
            if not isinstance(policy, CorePolicy):
                raise ValueError("Only 'CorePolicy' instances can be managed by a policy manager.")

        super().__init__()
        self.policy_dict = policy_dict
        self.update_option = update_option

        self._update_history = [set(policy_dict.keys())]

    @property
    def version(self):
        return len(self._update_history) - 1

    @abstractmethod
    def update(self, exp_by_policy: Dict[str, ExperienceSet]):
        """Logic for handling incoming experiences is implemented here."""
        raise NotImplementedError

    def get_state(self, cur_version: int = None, inference: bool = True):
        if cur_version is None:
            cur_version = self.version - 1
        updated = set()
        for version in range(cur_version + 1, len(self._update_history)):
            updated |= self._update_history[version]
        return {name: self.policy_dict[name].algorithm.get_state(inference=inference) for name in updated}


class LocalPolicyManager(AbsPolicyManager):
    """Policy manager that contains the actual policy instances.

    Args:
        policy_dict (Dict[str, CorePolicy]): Policies managed by the manager.
        num_epochs (Dict[str, int]): Number of learning epochs for each policy. This determine the number of
            times ``policy.learn()`` is called in each call to ``update``. Defaults to None, in which case the
            number of learning epochs will be set to 1 for each policy.
        update_trigger (Dict[str, int]): A dictionary of (policy_name, trigger), where "trigger" indicates the
            required number of new experiences to trigger a call to ``learn`` for each policy. Defaults to None,
            all triggers will be set to 1.
        warmup (Dict[str, int]): A dictionary of (policy_name, warmup_size), where "warmup_size" indicates the
            minimum number of experiences in the experience memory required to trigger a call to ``learn`` for
            each policy. Defaults to None, in which case all warm-up sizes will be set to 1.
        reset_memory (Dict[str, bool]): A dictionary of flags indicating whether each policy's experience memory
            should be reset after it is updated. It may be necessary to set this to True for on-policy algorithms
            to ensure that the experiences to be learned from stay up to date. Defaults to False for each policy.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "POLICY_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
    """
    def __init__(
        self,
        policy_dict: Dict[str, CorePolicy],
        update_option: Dict[str, PolicyUpdateOptions],
        log_dir: str = getcwd()
    ):
        super().__init__(policy_dict, update_option)
        self._new_exp_counter = defaultdict(int)
        self._logger = Logger("LOCAL_POLICY_MANAGER", dump_folder=log_dir)

    def update(self, exp_by_policy: Dict[str, ExperienceSet]):
        """Store experiences and update policies if possible.

        The incoming experiences are expected to be grouped by policy ID and will be stored in the corresponding
        policy's experience manager. Policies whose update conditions have been met will then be updated.
        """
        t0 = time.time()
        updated = set()
        for policy_name, exp in exp_by_policy.items():
            policy = self.policy_dict[policy_name]
            policy.experience_memory.put(exp)
            self._new_exp_counter[policy_name] += exp.size
            if (
                self._new_exp_counter[policy_name] >= self.update_option[policy_name].update_trigger and
                policy.experience_memory.size >= self.update_option[policy_name].warmup
            ):
                for _ in range(self.update_option[policy_name].num_epochs):
                    policy.update()
                if self.update_option[policy_name].reset_memory:
                    policy.reset_memory()
                updated.add(policy_name)
                self._new_exp_counter[policy_name] = 0

        if updated:
            self._update_history.append(updated)
            self._logger.info(f"Updated policies {updated}")

        self._logger.debug(f"policy update time: {time.time() - t0}")


class MultiProcessPolicyManager(AbsPolicyManager):
    """Policy manager that spawns a set of trainer processes for parallel training.

    Args:
        policy_dict (Dict[str, CorePolicy]): Policies managed by the manager.
        num_trainers (int): Number of trainer processes to be forked.
        create_policy_func_dict (dict): A dictionary mapping policy names to functions that create them. The policy
            creation function should have exactly one parameter which is the policy name and return an ``AbsPolicy``
            instance.
        num_epochs (Dict[str, int]): Number of learning epochs for each policy. This determine the number of
            times ``policy.learn()`` is called in each call to ``update``. Defaults to None, in which case the
            number of learning epochs will be set to 1 for each policy.
        update_trigger (Dict[str, int]): A dictionary of (policy_name, trigger), where "trigger" indicates the
            required number of new experiences to trigger a call to ``learn`` for each policy. Defaults to None,
            all triggers will be set to 1.
        warmup (Dict[str, int]): A dictionary of (policy_name, warmup_size), where "warmup_size" indicates the
            minimum number of experiences in the experience memory required to trigger a call to ``learn`` for
            each policy. Defaults to None, in which case all warm-up sizes will be set to 1.
        reset_memory (Dict[str, bool]): A dictionary of flags indicating whether each policy's experience memory
            should be reset after it is updated. It may be necessary to set this to True for on-policy algorithms
            to ensure that the experiences to be learned from stay up to date. Defaults to False for each policy.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "POLICY_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
    """
    def __init__(
        self,
        policy_dict: Dict[str, CorePolicy],
        update_option: Dict[str, PolicyUpdateOptions],
        num_trainers: int,
        create_policy_func_dict: Dict[str, Callable],
        log_dir: str = getcwd(),
    ):
        super().__init__(policy_dict, update_option)
        self._policy2trainer = {}
        self._trainer2policies = defaultdict(list)
        self._exp_cache = defaultdict(ExperienceSet)
        self._num_experiences_by_policy = defaultdict(int)

        for i, name in enumerate(self.policy_dict):
            trainer_id = i % num_trainers
            self._policy2trainer[name] = f"TRAINER.{trainer_id}"
            self._trainer2policies[f"TRAINER.{trainer_id}"].append(name)

        self._logger = Logger("MULTIPROCESS_POLICY_MANAGER", dump_folder=log_dir)

        self._trainer_processes = []
        self._manager_end = {}
        for trainer_id, policy_names in self._trainer2policies.items():
            manager_end, trainer_end = Pipe()
            self._manager_end[trainer_id] = manager_end
            trainer = Process(
                target=trainer_process,
                args=(
                    trainer_id,
                    trainer_end,
                    {name: create_policy_func_dict[name] for name in policy_names},
                    {name: self.policy_dict[name].algorithm.get_state() for name in policy_names},
                    {name: self.update_option[name] for name in policy_names}
                ),
                kwargs={"log_dir": log_dir}
            )
            self._trainer_processes.append(trainer)
            trainer.start()

    def update(self, exp_by_policy: Dict[str, ExperienceSet]):
        exp_to_send, updated = {}, set()
        for policy_name, exp in exp_by_policy.items():
            self._num_experiences_by_policy[policy_name] += exp.size
            self._exp_cache[policy_name].extend(exp)
            if (
                self._exp_cache[policy_name].size >= self.update_option[policy_name].update_trigger and
                self._num_experiences_by_policy[policy_name] >= self.update_option[policy_name].warmup
            ):
                exp_to_send[policy_name] = self._exp_cache.pop(policy_name)
                updated.add(policy_name)

        if exp_to_send:
            for trainer_id, conn in self._manager_end.items():
                conn.send({
                    "type": "train",
                    "experiences": {name: exp_to_send[name] for name in self._trainer2policies[trainer_id]}
                })

            for conn in self._manager_end.values():
                result = conn.recv()
                for policy_name, policy_state in result["policy"].items():
                    self.policy_dict[policy_name].algorithm.set_state(policy_state)

            if updated:
                self._update_history.append(updated)
                self._logger.info(f"Updated policies {updated}")

    def exit(self):
        """Tell the trainer processes to exit."""
        for conn in self._manager_end.values():
            conn.send({"type": "quit"})


class MultiNodePolicyManager(AbsPolicyManager):
    """Policy manager that communicates with a set of remote nodes for parallel training.

    Args:
        policy_dict (Dict[str, CorePolicy]): Policies managed by the manager.
        group (str): Group name for the training cluster, which includes all trainers and a training manager that
            manages them.
        num_trainers (int): Number of trainers. The trainers will be identified by "TRAINER.i", where
            0 <= i < num_trainers.
        num_epochs (Dict[str, int]): Number of learning epochs for each policy. This determine the number of
            times ``policy.learn()`` is called in each call to ``update``. Defaults to None, in which case the
            number of learning epochs will be set to 1 for each policy.
        update_trigger (Dict[str, int]): A dictionary of (policy_name, trigger), where "trigger" indicates the
            required number of new experiences to trigger a call to ``learn`` for each policy. Defaults to None,
            all triggers will be set to 1.
        warmup (Dict[str, int]): A dictionary of (policy_name, warmup_size), where "warmup_size" indicates the
            minimum number of experiences in the experience memory required to trigger a call to ``learn`` for
            each policy. Defaults to None, in which case all warm-up sizes will be set to 1.
        reset_memory (Dict[str, bool]): A dictionary of flags indicating whether each policy's experience memory
            should be reset after it is updated. It may be necessary to set this to True for on-policy algorithms
            to ensure that the experiences to be learned from stay up to date. Defaults to False for each policy.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "POLICY_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
        proxy_kwargs: Keyword parameters for the internal ``Proxy`` instance. See ``Proxy`` class
            for details. Defaults to the empty dictionary.
    """
    def __init__(
        self,
        policy_dict: Dict[str, CorePolicy],
        update_option: Dict[str, PolicyUpdateOptions],
        group: str,
        num_trainers: int,
        log_dir: str = getcwd(),
        proxy_kwargs: dict = {}
    ):
        super().__init__(policy_dict, update_option)
        peers = {"trainer": num_trainers}
        self._proxy = Proxy(group, "policy_manager", peers, component_name="POLICY_MANAGER", **proxy_kwargs)

        self._policy2trainer = {}
        self._trainer2policies = defaultdict(list)
        self._exp_cache = defaultdict(ExperienceSet)
        self._num_experiences_by_policy = defaultdict(int)

        self._logger = Logger("MULTINODE_POLICY_MANAGER", dump_folder=log_dir)

        for i, name in enumerate(self.policy_dict):
            trainer_id = i % num_trainers
            self._policy2trainer[name] = f"TRAINER.{trainer_id}"
            self._trainer2policies[f"TRAINER.{trainer_id}"].append(name)

        self._logger.info("Initializing policy states on trainers...")
        for trainer_name, policy_names in self._trainer2policies.items():
            self._proxy.send(
                SessionMessage(
                    MsgTag.INIT_POLICY_STATE, self._proxy.name, trainer_name,
                    body={
                        MsgKey.POLICY_STATE: {
                            name: self.policy_dict[name].algorithm.get_state(inference=False)
                            for name in policy_names
                        }
                    }
                )
            )

    def update(self, exp_by_policy: Dict[str, ExperienceSet]):
        exp_to_send, updated = {}, set()
        for policy_name, exp in exp_by_policy.items():
            self._num_experiences_by_policy[policy_name] += exp.size
            self._exp_cache[policy_name].extend(exp)
            if (
                self._exp_cache[policy_name].size >= self.update_option[policy_name].update_trigger and
                self._num_experiences_by_policy[policy_name] >= self.update_option[policy_name].warmup
            ):
                exp_to_send[policy_name] = self._exp_cache.pop(policy_name)
                updated.add(policy_name)

        if exp_to_send:
            msg_body_by_dest = defaultdict(dict)
            for policy_name, exp in exp_to_send.items():
                trainer_id = self._policy2trainer[policy_name]
                if MsgKey.EXPERIENCES not in msg_body_by_dest[trainer_id]:
                    msg_body_by_dest[trainer_id][MsgKey.EXPERIENCES] = {}
                msg_body_by_dest[trainer_id][MsgKey.EXPERIENCES][policy_name] = exp

            dones = 0
            self._proxy.iscatter(MsgTag.LEARN, SessionType.TASK, list(msg_body_by_dest.items()))
            for msg in self._proxy.receive():
                if msg.tag == MsgTag.TRAIN_DONE:
                    for policy_name, policy_state in msg.body[MsgKey.POLICY_STATE].items():
                        self.policy_dict[policy_name].algorithm.set_state(policy_state)
                    dones += 1
                    if dones == len(msg_body_by_dest):
                        break

            self._update_history.append(updated)
            self._logger.info(f"Updated policies {updated}")

    def exit(self):
        """Tell the remote trainers to exit."""
        self._proxy.ibroadcast("trainer", MsgTag.EXIT, SessionType.NOTIFICATION)
        self._proxy.close()
        self._logger.info("Exiting...")


class MultiNodeDistPolicyManager(AbsPolicyManager):
    """Policy manager that communicates with a set of remote nodes for distributed parallel training.

    Args:
        policy_dict (Dict[str, AbsCorePolicy]): Policies managed by the manager.
        group (str): Group name for the training cluster, which includes all trainers and a training manager that
            manages them.
        num_trainers (int): Number of trainers. The trainers will be identified by "TRAINER.i", where
            0 <= i < num_trainers.
        update_trigger (Dict[str, int]): A dictionary of (policy_name, trigger), where "trigger" indicates the
            required number of new experiences to trigger a call to ``learn`` for each policy. Defaults to None,
            all triggers will be set to 1.
        warmup (Dict[str, int]): A dictionary of (policy_name, warmup_size), where "warmup_size" indicates the
            minimum number of experiences in the experience memory required to trigger a call to ``learn`` for
            each policy. Defaults to None, in which case all warm-up sizes will be set to 1.
        post_update (Callable): Custom function to process whatever information is collected by each
            trainer (local or remote) at the end of ``update`` calls. The function signature should be (trackers,)
            -> None, where tracker is a list of environment wrappers' ``tracker`` members. Defaults to None.
        log_dir (str): Directory to store logs in. A ``Logger`` with tag "POLICY_MANAGER" will be created at init
            time and this directory will be used to save the log files generated by it. Defaults to the current
            working directory.
        proxy_kwargs: Keyword parameters for the internal ``Proxy`` instance. See ``Proxy`` class
            for details. Defaults to the empty dictionary.
    """
    def __init__(
        self,
        policy_dict: Dict[str, CorePolicy],
        group: str,
        num_trainers: int,
        num_epochs: Dict[str, int] = None,
        update_trigger: Dict[str, int] = None,
        warmup: Dict[str, int] = None,
        reset_memory: Dict[str, int] = defaultdict(lambda: False),
        post_update: Callable = None,
        log_dir: str = getcwd(),
        proxy_kwargs: dict = {}
    ):
        super().__init__(
            policy_dict,
            num_epochs=num_epochs,
            update_trigger=update_trigger,
            warmup=warmup,
            reset_memory=reset_memory
        )
        peers = {"trainer": num_trainers}
        self._proxy = Proxy(group, "policy_manager", peers, component_name="POLICY_MANAGER", **proxy_kwargs)

        self._policy2trainer = defaultdict(list)
        self._trainer2policies = defaultdict(list)
        self._exp_cache = defaultdict(ExperienceSet)
        self._num_experiences_by_policy = defaultdict(int)
        self.num_trainers = num_trainers

        self._logger = Logger("MULTINODE_DIST_POLICY_MANAGER", dump_folder=log_dir)

    def allocate_strategy(self, num_trainers, num_experiences_by_policy, logger=None):
        policy2trainer = defaultdict(list)
        trainer2policies = defaultdict(list)

        # initialize
        if len(num_experiences_by_policy) == 0:
            if len(num_experiences_by_policy) >= num_trainers:
                for i, name in enumerate(num_experiences_by_policy):
                    trainer_id = i % num_trainers
                    policy2trainer[name].append(f"TRAINER.{trainer_id}")
                    trainer2policies[f"TRAINER.{trainer_id}"].append(name)
            else:
                trainer_id_list = list(range(num_trainers))
                for i, name in enumerate(num_experiences_by_policy):
                    for trainer_id in trainer_id_list[i::len(num_experiences_by_policy)]:
                        policy2trainer[name].append(f"TRAINER.{trainer_id}")
                        trainer2policies[f"TRAINER.{trainer_id}"].append(name)

        # allocate trainers according to historical experience numbers.
        else:
            total_num_experiences = sum(num_experiences_by_policy.values())
            average_payload = total_num_experiences / num_trainers

            offset = 0
            policy_quota = dict()
            for name, num_exp in num_experiences_by_policy.items():
                quota = num_exp / average_payload
                quota = max(1, int(round(quota)))
                policy_quota[name] = quota

            # adjust quota if any redundancy occurs.
            redundancy = num_trainers - sum(policy_quota.values())
            if redundancy > 0:
                busiest_policy = max(policy_quota, key=lambda name: policy_quota[name])
                policy_quota[busiest_policy] += redundancy

            for name, quota in policy_quota.items():
                if logger is not None:
                    logger.info(
                        f"policy {name} payload: {num_experiences_by_policy[name]},  quota: {quota} node(s)")
                for i in range(quota):
                    trainer_id = (i + offset) % num_trainers
                    policy2trainer[name].append(f"TRAINER.{trainer_id}")
                    trainer2policies[f"TRAINER.{trainer_id}"].append(name)
                offset = (offset + quota) % num_trainers

        return policy2trainer, trainer2policies

    def allocate_trainers(self):
        self._policy2trainer, self._trainer2policies = self.allocate_strategy(
            self.num_trainers, self._num_experiences_by_policy, logger=self._logger)

        # re-allocation
        self._logger.info("Re-allocating policy states on trainers...")
        for trainer_name, policy_names in self._trainer2policies.items():
            self._proxy.send(
                SessionMessage(
                    MsgTag.INIT_POLICY_STATE, self._proxy.name, trainer_name,
                    body={MsgKey.POLICY_STATE: {name: self.policy_dict[name].get_state() for name in policy_names}}
                )
            )

    def update(self, exp_by_policy: Dict[str, ExperienceSet]):
        exp_to_send, updated = {}, set()
        for policy_name, exp in exp_by_policy.items():
            self._num_experiences_by_policy[policy_name] += exp.size
            self._exp_cache[policy_name].extend(exp)
            if (
                self._exp_cache[policy_name].size >= self.update_trigger[policy_name]
                and self._num_experiences_by_policy[policy_name] >= self.warmup[policy_name]
            ):
                exp_to_send[policy_name] = self._exp_cache.pop(policy_name)
                updated.add(policy_name)
                self.policy_dict[policy_name].store(exp_to_send[policy_name])

        self.allocate_trainers()

        # get gradient and update
        # TODO: handle various config.train_epochs among policies.
        name = list(self.policy_dict.keys())[0]
        for _ in range(self.num_epochs[name]):
            msg_body_by_dest = defaultdict(dict)
            manager_grad_dict = defaultdict(dict)
            # 1. sample batches for trainers
            for trainer_id, policy_names in self._trainer2policies:
                if MsgKey.EXPERIENCES not in msg_body_by_dest:
                    msg_body_by_dest[MsgKey.EXPERIENCES] = dict()
                for policy_name in policy_names:
                    exp_batch = self.policy_dict[policy_name].sampler.get()
                    msg_body_by_dest[trainer_id][MsgKey.EXPERIENCES][policy_name] = exp_batch
            # 2. scatter data and train
            for reply in self._proxy.scatter(MsgTag.GET_UPDATE_INFO, SessionType.TASK, list(msg_body_by_dest.items())):
                # 3. aggregate gradient
                for policy_name, grad_dict in reply.body[MsgKey.UPDATE_INFO].items():
                    trainer_id_list = self._policy2trainer[policy_name]
                    for param_name in grad_dict:
                        manager_grad_dict[policy_name][param_name] = manager_grad_dict[policy_name].get(
                            param_name, 0) + grad_dict[param_name] / len(trainer_id_list)

            # 4. apply gradient
            for policy_name in manager_grad_dict:
                """get_gradient() aims to build computaion graph on policy manager.
                The real gradients come from `manager_grad_dict`, which collects from trainers."""
                dummy_exp = self.policy_dict[policy_name].sampler.get()[0]  # batch size = 1
                _ = self.policy_dict[policy_name].get_update_info(dummy_exp)

                self.policy_dict[policy_name].learn(manager_grad_dict[policy_name])

            # 5. send updated model to trainers
            for trainer_name, policy_names in self._trainer2policies.items():
                self._proxy.send(
                    SessionMessage(
                        MsgTag.UPDATE_POLICY_STATE, self._proxy.name, trainer_name,
                        body={MsgKey.POLICY_STATE: {name: self.policy_dict[name].get_state() for name in policy_names}}
                    )
                )

        if updated:
            self._update_history.append(updated)
            self._logger.info(f"Updated policies {updated}")

    def exit(self):
        """Tell the remote trainers to exit."""
        self._proxy.ibroadcast("trainer", MsgTag.EXIT, SessionType.NOTIFICATION)
        self._proxy.close()
        self._logger.info("Exiting...")
