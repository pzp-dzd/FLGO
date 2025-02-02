import sys

import numpy as np
import queue
import config as cfg
from abc import ABCMeta, abstractmethod
import functools

import torch


class AbstractStateUpdater(metaclass=ABCMeta):
    """
    An abstract class that defines client state updates and is used to construct a heterogeneous system for federated learning
    """
    @abstractmethod
    def flush(self):
        """
        An abstract method to update user state
        """
        # flush the states for all the things in the system as time steps
        pass

random_seed_gen = None
random_module = None

def seed_generator(seed=0):
    """
    Generate random seeds
    """
    while True:
        yield seed+1
        seed+=1

def size_of_package(package):
    """
    compute size of package
    """
    size = 0
    for v in package.values():
        if type(v) is torch.Tensor:
            size += sys.getsizeof(v.storage())
        else:
            size += v.__sizeof__()
    return size

class ElemClock:
    """
    A class for emulating the system clock
    """
    class Elem:
        """
        An element unit class in an analog clock

        Args:
            x(object):The object that needs to add an analog clock
            time(int):The analog clock added to the object
        """
        def __init__(self, x, time):
            self.x = x
            self.time = time

        def __str__(self):
            """
            Print string output

            Return:
                string output
            """
            return '{} at Time {}'.format(self.x, self.time)

        def __lt__(self, other):
            """
            Compare clocks

            Args:
                other(int):The clock of another object
            Return:
                The results of the comparison
            """
            return self.time < other.time

    def __init__(self):
        self.q = queue.PriorityQueue()
        self.time = 0
        self.state_updater = None

    def step(self, delta_t=1):
        """
        The clock moves forward delta_t steps

        Args:
            delta_t(int):The number of steps forward
        """
        if delta_t < 0: raise RuntimeError("Cannot inverse time of system_simulator.cfg.clock.")
        if self.state_updater is not None:
            for t in range(delta_t):
                self.state_updater.flush()
        self.time += delta_t

    def set_time(self, t):
        """
        Sets the current clock

        Args:
            t(int):The current clock
        """
        if t < self.time: raise RuntimeError("Cannot inverse time of system_simulator.cfg.clock.")
        self.time = t

    def put(self, x, time):
        """
        Put the object into the clock queue

        Args:
            x(object):The object that needs to be put
            time(int):The current clock
        """
        self.q.put_nowait(self.Elem(x, time))

    def get(self):
        """
        Frees the object from the clock queue

        Return:
            The element that is released
        """
        if self.q.empty(): return None
        return self.q.get().x

    def get_until(self, t):
        """
        Release all elements before the t moment

        Args:
            t(int):moment
        Return:
            res(list):The list of elements that were released
        """
        res = []
        while not self.empty():
            elem = self.q.get()
            if elem.time > t:
                self.put(elem.x, elem.time)
                break
            pkg = elem.x
            res.append(pkg)
        return res

    def get_sofar(self):
        """
        Release all elements before the current moment

        Return:
            res(list):The list of elements that were released
        """
        return self.get_until(self.current_time)

    def gets(self):
        """
        Release all elements in the queue

        Return:
            res(list):The list of elements that were released
        """
        if self.empty(): return []
        res = []
        while not self.empty(): res.append(self.q.get())
        res = [rx.x for rx in res]
        return res

    def clear(self):
        """
        Empty the queue
        """
        while not self.empty():
            self.get()

    def conditionally_clear(self, f):
        """
        Empty the queue elements that meet condition f

        Args:
            f:Conditional judgment function
        """
        buf = []
        while not self.empty(): buf.append(self.q.get())
        for elem in buf:
            if not f(elem.x): self.q.put_nowait(elem)
        return

    def empty(self):
        """
        Determine whether the clock queue is empty
        """
        return self.q.empty()

    @ property
    def current_time(self):
        """
        Get the current moment

        Return:
            The current moment
        """
        return self.time

    def register_state_updater(self, state_updater):
        """
        Set the status update object
        """
        self.state_updater = state_updater

class BasicStateUpdater(AbstractStateUpdater):
    """
    The basic StateUpdater class

    Args:
        objects(list):The list of entities participating in the federated learning process
    """
    _STATE = ['offline', 'idle', 'selected', 'working', 'dropped']
    _VAR_NAMES = ['prob_available', 'prob_unavailable', 'prob_drop', 'working_amount', 'latency']
    def __init__(self, objects, *args, **kwargs):
        if len(objects)>0:
            self.server = objects[0]
            self.clients = objects[1:]
        else:
            self.server = None
            self.clients = []
        self.all_clients = list(range(len(self.clients)))
        self.random_module = np.random.RandomState(0) if random_seed_gen is None else np.random.RandomState(next(random_seed_gen))
        # client states and the variables
        self.client_states = ['idle' for _ in self.clients]
        self.roundwise_fixed_availability = False
        self.availability_latest_round = -1
        self.variables = [{
            'prob_available': 1.,
            'prob_unavailable': 0.,
            'prob_drop': 0.,
            'working_amount': c.num_steps,
            'latency': 0,
        } for c in self.clients]
        for var in self._VAR_NAMES:
            self.set_variable(self.all_clients, var, [self.variables[cid][var] for cid in self.all_clients])
        self.state_counter = [{'dropped_counter': 0, 'latency_counter': 0, } for _ in self.clients]

    def get_client_with_state(self, state='idle'):
        """
        Pick all clients that satisfy a certain state

        Args:
            state(str):State name
        Return:
            list of clients
        """
        return [cid for cid, cstate in enumerate(self.client_states) if cstate == state]

    def set_client_state(self, client_ids, state):
        """
        Set client state

        Args:
            client_ids(list):The list of clients id
            state(str):State name
        """
        if state not in self._STATE: raise RuntimeError('{} not in the default state'.format(state))
        if type(client_ids) is not list: client_ids = [client_ids]
        for cid in client_ids: self.client_states[cid] = state
        if state == 'dropped':
            self.set_client_dropped_counter(client_ids)
        if state == 'working':
            self.set_client_latency_counter(client_ids)
        if state == 'idle':
            self.reset_client_counter(client_ids)

    def set_client_latency_counter(self, client_ids = []):
        """
        Set the training time countdown for each client

        Args:
            client_ids(list):The list of clients id
        """
        if type(client_ids) is not list: client_ids = [client_ids]
        for cid in client_ids:
            self.state_counter[cid]['dropped_counter'] = 0
            self.state_counter[cid]['latency_counter'] = self.variables[cid]['latency']

    def set_client_dropped_counter(self, client_ids = []):
        """
        Set the dropout time countdown for each client

        Args:
            client_ids(list):The list of clients id
        """
        if type(client_ids) is not list: client_ids = [client_ids]
        for cid in client_ids:
            self.state_counter[cid]['latency_counter'] = 0
            self.state_counter[cid]['dropped_counter'] = self.server.get_tolerance_for_latency()

    def reset_client_counter(self, client_ids = []):
        """
        Reset the training time countdown and dropout time countdown for each client

        Args:
            client_ids(list):The list of clients id
        """
        if type(client_ids) is not list: client_ids = [client_ids]
        for cid in client_ids:
            self.state_counter[cid]['dropped_counter'] = self.state_counter[cid]['latency_counter'] = 0
        return

    @property
    def idle_clients(self):
        """
        Get the idle clients list

        Return:
            list of idle clients
        """
        return self.get_client_with_state('idle')

    @property
    def working_clients(self):
        """
        Get the working clients list

        Return:
            list of working clients
        """
        return self.get_client_with_state('working')

    @property
    def offline_clients(self):
        """
        Get the offline clients list

        Return:
            list of offline clients
        """
        return self.get_client_with_state('offline')

    @property
    def selected_clients(self):
        """
        Get the selected clients list

        Return:
            list of selected clients
        """
        return self.get_client_with_state('selected')

    @property
    def dropped_clients(self):
        """
        Get the dropped clients list

        Return:
            list of dropped clients
        """
        return self.get_client_with_state('dropped')

    def get_variable(self, client_ids, varname):
        """
        Get the clients variable by varname

        Args:
            client_ids(list):The list of clients id
            varname(str):The varname of clients
        Return:
            list of variable
        """
        if len(self.variables) ==0: return None
        if type(client_ids) is not list: client_ids = [client_ids]
        return [self.variables[cid][varname] if varname in self.variables[cid].keys() else None for cid in client_ids]

    def set_variable(self, client_ids, varname, values):
        """
        Set the clients variable

        Args:
            client_ids(list):The list of clients id
            varname(str):The varname of clients
            values(list):The value list of variable
        """
        if type(client_ids) is not list: client_ids = [client_ids]
        assert len(client_ids) == len(values)
        for cid, v in zip(client_ids, values):
            self.variables[cid][varname] = v
            setattr(self.clients[cid], '_'+varname, v)

    def update_client_availability(self, *args, **kwargs):
        return

    def update_client_connectivity(self, client_ids, *args, **kwargs):
        """
        Args:
            client_ids(list):The list of clients id
        """
        return

    def update_client_completeness(self, client_ids, *args, **kwargs):
        """
        Args:
            client_ids(list):The list of clients id
        """
        return

    def update_client_responsiveness(self, client_ids, *args, **kwargs):
        """
        Args:
            client_ids(list):The list of clients id
        """
        return

    def flush(self):
        """
        A function for updating user status, including updating user availability, and updating various state parameters such as dropout time countdown and training time countdown for each user according to availability
        """
        # +++++++++++++++++++ availability +++++++++++++++++++++
        # change self.variables[cid]['prob_available'] and self.variables[cid]['prob_unavailable'] for each client `cid`
        self.update_client_availability()
        # update states for offline & idle clients
        if len(self.idle_clients)==0 or not self.roundwise_fixed_availability or self.server.current_round > self.availability_latest_round:
            self.availability_latest_round = self.server.current_round
            offline_clients = {cid: 'offline' for cid in self.offline_clients}
            idle_clients = {cid:'idle' for cid in self.idle_clients}
            for cid in offline_clients:
                if (self.random_module.rand() <= self.variables[cid]['prob_available']): offline_clients[cid] = 'idle'
            for cid in self.idle_clients:
                if  (self.random_module.rand() <= self.variables[cid]['prob_unavailable']): idle_clients[cid] = 'offline'
            new_idle_clients = [cid for cid in offline_clients if offline_clients[cid] == 'idle']
            new_offline_clients = [cid for cid in idle_clients if idle_clients[cid] == 'offline']
            self.set_client_state(new_idle_clients, 'idle')
            self.set_client_state(new_offline_clients, 'offline')
        # update states for dropped clients
        for cid in self.dropped_clients:
            self.state_counter[cid]['dropped_counter'] -= 1
            if self.state_counter[cid]['dropped_counter'] < 0:
                self.state_counter[cid]['dropped_counter'] = 0
                self.client_states[cid] = 'offline'
                if (self.random_module.rand() < self.variables[cid]['prob_unavailable']):
                    cfg.logger.info('Client {} had just dropped out and is currently offline.'.format(cid))
                    self.set_client_state([cid], 'offline')
                else:
                    cfg.logger.info('Client {} had just dropped out and is currently available.'.format(cid))
                    self.set_client_state([cid], 'idle')
        # Remark: the state transfer fo working clients is instead made once the server received from clients
        # # update states for working clients
        # for cid in self.working_clients:
        #     self.state_counter[cid]['latency_counter'] -= 1
        #     if self.state_counter[cid]['latency_counter'] < 0:
        #         self.state_counter[cid]['latency_counter'] = 0
        #         self.set_client_state([cid], 'offline')

#================================================Decorators==========================================
# Time Counter for any function which forces the `cfg.clock` to
# step one unit of time once the decorated function is called
def time_step(f):
    """
    Time Counter for any function which forces the `cfg.clock` to step one unit of time once the decorated function is called

    Args:
        f:The function that needs to add a clock

    Return:
        f_timestep:The function after adding the clock
    """
    def f_timestep(*args, **kwargs):
        cfg.clock.step()
        return f(*args, **kwargs)
    return f_timestep

# sampling phase
def with_availability(sample):
    """
    Refresh the current active state of the user before sampling to ensure that sampling is taken from active users

    Args:
        sample:The original sampling function
    Return:
        sample_with_availability:The function used to sample active users
    """
    @functools.wraps(sample)
    def sample_with_availability(self):
        available_clients = cfg.state_updater.idle_clients
        # ensure that there is at least one client to be available at the current moment
        while len(available_clients) == 0:
            cfg.clock.step()
            available_clients = cfg.state_updater.idle_clients
        # call the original sampling function
        selected_clients = sample(self)
        # filter the selected but unavailable clients
        effective_clients = set(selected_clients).intersection(set(available_clients))
        # return the selected and available clients (e.g. sampling with replacement should be considered here)
        self._unavailable_selected_clients = [cid for cid in selected_clients if cid not in effective_clients]
        if len(self._unavailable_selected_clients)>0:
            cfg.logger.info('The selected clients {} are not currently available.'.format(self._unavailable_selected_clients))
        selected_clients = [cid for cid in selected_clients if cid in effective_clients]
        cfg.state_updater.set_client_state(selected_clients, 'selected')
        return selected_clients
    return sample_with_availability

# communicating phase
def with_dropout(communicate):
    """
    Refresh the user's disconnected status before communication to ensure communication with online users

    Args:
        communicate:The original communicate function
    Return:
        communicate_with_dropout:The function used to communicate with online users
    """
    @functools.wraps(communicate)
    def communicate_with_dropout(self, selected_clients, asynchronous=False):
        if len(selected_clients) > 0:
            cfg.state_updater.update_client_connectivity(selected_clients)
            probs_drop = cfg.state_updater.get_variable(selected_clients, 'prob_drop')
            self._dropped_selected_clients = [cid for cid,prob in zip(selected_clients, probs_drop) if cfg.state_updater.random_module.rand() <= prob]
            cfg.state_updater.set_client_state(self._dropped_selected_clients, 'dropped')
            return communicate(self, [cid for cid in selected_clients if cid not in self._dropped_selected_clients], asynchronous)
        else:
            return communicate(self, selected_clients, asynchronous)
    return communicate_with_dropout

# # communicating phase
# def with_latency(communicate_with):
#     @functools.wraps(communicate_with)
#     def delayed_communicate_with(self, client_id):
#         res = communicate_with(self, client_id)
#         # Record the size of the package that may influence the value of the latency
#         cfg.state_updater.set_variable([client_id], '__package_size', [res.__sizeof__()])
#         # Update the real-time latency of the client response
#         cfg.state_updater.update_client_responsiveness([client_id])
#         # Get the updated latency
#         latency = cfg.state_updater.get_variable(client_id, 'latency')[0]
#         self.clients[client_id]._latency = latency
#         res['__cid'] = client_id
#         # Compute the arrival time
#         res['__t'] = cfg.clock.current_time + latency
#         return res
#     return delayed_communicate_with

# local training phase
def with_completeness(train):
    """
    Before training, it is modified to suit the user's local training workload to suit the user's state

    Args:
        train:The original train function
    Return:
        train_with_incomplete_update:The training function to accommodate the user's local workload
    """
    @functools.wraps(train)
    def train_with_incomplete_update(self, model, *args, **kwargs):
        old_num_steps = self.num_steps
        self.num_steps = self._working_amount
        res = train(self, model, *args, **kwargs)
        self.num_steps = old_num_steps
        return res
    return train_with_incomplete_update

def with_clock(communicate):
    """
    Used to simulate the time required for a user to communicate with the server

    Args:
        communicate:The original communicate function
    Return:
        communicate_with_clock:The communication function is used to simulate different communication times required for different users
    """
    def communicate_with_clock(self, selected_clients, asynchronous=False):
        cfg.state_updater.update_client_completeness(selected_clients)
        res = communicate(self, selected_clients, asynchronous)
        # If all the selected clients are unavailable, directly return the result without waiting.
        # Else if all the available clients have dropped out and not using asynchronous communication,  waiting for `tolerance_for_latency` time units.
        tolerance_for_latency = self.get_tolerance_for_latency()
        if not asynchronous and len(selected_clients)==0:
            if hasattr(self, '_dropped_selected_clients') and len(self._dropped_selected_clients)>0:
                cfg.clock.step(tolerance_for_latency)
            return res
        # Convert the unpacked packages to a list of packages of each client.
        pkgs = [{key: vi[id] for key, vi in res.items()} for id in range(len(list(res.values())[0]))] if len(selected_clients)>0 else []
        # Put the packages from selected clients into clock only if when there are effective selected clients
        if len(selected_clients)>0:
            # Calculate latency for selectedc clients
            # Set local model size of clients
            if 'model' in pkgs[0].keys():
                model_sizes = [pkg['model'].count_parameters(output=False) for pkg in pkgs]
            else:
                model_sizes = [0 for _ in pkgs]
            cfg.state_updater.set_variable(selected_clients, '__model_size', model_sizes)
            # Set uploading package sizes for clients
            cfg.state_updater.set_variable(selected_clients, '__upload_package_size', [size_of_package(pkg) for pkg in pkgs])
            # Set downloading package sizes for clients
            cfg.state_updater.set_variable(selected_clients, '__download_package_size', [size_of_package(self.sending_package_buffer[cid]) for cid in selected_clients])
            cfg.state_updater.update_client_responsiveness(selected_clients)
            # Update latency for clients
            latency = cfg.state_updater.get_variable(selected_clients, 'latency')
            # Set selected clients' states as `working`
            cfg.state_updater.set_client_state(selected_clients, 'working')

        # Compute the arrival time and put the packages into a queue according to their arrival time `__t`
            for pkg, cid, lt in zip(pkgs, selected_clients, latency):
                pkg['__cid'] = cid
                pkg['__t'] = cfg.clock.current_time + lt
            for pi in pkgs: cfg.clock.put(pi, pi['__t'])
        # Receiving packages in asynchronous\synchronous way
        # Wait for client packages. If communicating in asynchronous way, the waiting time is 0.
        if asynchronous:
            # Return the currently received packages to the server
            eff_pkgs = cfg.clock.get_until(cfg.clock.current_time)
            eff_cids = [pkg_i['__cid'] for pkg_i in eff_pkgs]
        else:
            # Wait all the selected clients for no more than `tolerance_for_latency` time units.
            # Check if anyone had dropped out or will be overdue
            max_latency = max(cfg.state_updater.get_variable(selected_clients, 'latency'))
            any_drop, any_overdue = (len(self._dropped_selected_clients) > 0), (max_latency >  tolerance_for_latency)
            # Compute delta of time for the communication.
            delta_t = tolerance_for_latency if any_drop or any_overdue else max_latency
            # Receive packages within due
            eff_pkgs = cfg.clock.get_until(cfg.clock.current_time + delta_t)
            cfg.clock.step(delta_t)
            # Drop the packages of overdue clients and reset their states to `idle`
            eff_cids = [pkg_i['__cid'] for pkg_i in eff_pkgs]
            self._overdue_clients = list(set([cid for cid in selected_clients if cid not in eff_cids]))
            # no additional wait for the synchronous selected clients and preserve the later packages from asynchronous clients
            if len(self._overdue_clients) > 0:
                cfg.clock.conditionally_clear(lambda x: x['__cid'] in self._overdue_clients)
                cfg.state_updater.set_client_state(self._overdue_clients, 'idle')
            # Resort effective packages
            pkg_map = {pkg_i['__cid']: pkg_i for pkg_i in eff_pkgs}
            eff_pkgs = [pkg_map[cid] for cid in selected_clients if cid in eff_cids]
        cfg.state_updater.set_client_state(eff_cids, 'offline')
        self.received_clients = [pkg_i['__cid'] for pkg_i in eff_pkgs]
        return self.unpack(eff_pkgs)
    return communicate_with_clock


