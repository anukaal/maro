# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from abc import ABC, abstractmethod

from maro.communication import Proxy, SessionMessage
from maro.communication.registry_table import RegisterTable
from maro.rl.scheduling.scheduler import Scheduler
from maro.simulator import Env

from ..common import ActorTrainerComponent, MessageTag, PayloadKey


class AbsAutoActor(ABC):
    """Abstract actor class.

    Args:
        env: An environment instance.
        proxy_params: Parameters required for instantiating an internal proxy for communication.
    """
    def __init__(self, env: Env, scheduler: Scheduler, **proxy_params):
        self._env = env
        self._scheduler = scheduler
        self._proxy = Proxy(component_type=ActorTrainerComponent.ACTOR.value, **proxy_params)
        self._registry_table = RegisterTable(self._proxy.peers_name)

    @abstractmethod
    def run(self, is_training: bool = True):
        """Run the main training loop or run one episode for model testing."""
        raise NotImplementedError

    def _get_update_session_id(self):
        return ".".join([
            f"ep_{self._scheduler.current_ep - 1}",
            ActorTrainerComponent.ACTOR.value,
            ActorTrainerComponent.TRAINER.value
            ])
