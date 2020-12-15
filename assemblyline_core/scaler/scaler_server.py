"""
An auto-scaling service specific to Assemblyline services.
"""
import functools
import threading
from collections import defaultdict
from string import Template
from typing import Dict, List
import os
import math
import time
import platform
import concurrent.futures

from assemblyline.remote.datatypes.queues.named import NamedQueue
from assemblyline.remote.datatypes.queues.priority import PriorityQueue
from assemblyline.remote.datatypes.exporting_counter import export_metrics_once
from assemblyline.remote.datatypes.hash import ExpiringHash
from assemblyline.odm.models.service import Service, DockerConfig
from assemblyline.odm.messages.scaler_heartbeat import Metrics
from assemblyline.odm.messages.scaler_status_heartbeat import Status
from assemblyline.common.forge import get_service_queue
from assemblyline.common.constants import SCALER_TIMEOUT_QUEUE, SERVICE_STATE_HASH, ServiceStatus
from assemblyline_core.scaler.controllers import KubernetesController
from assemblyline_core.scaler.controllers.interface import ServiceControlError
from assemblyline_core.server_base import CoreBase, ServiceStage

from .controllers import DockerController
from . import collection

# How often (in seconds) to download new service data, try to scale managed services,
# and download more metrics data respectively
SERVICE_SYNC_INTERVAL = 30
SCALE_INTERVAL = 5
METRIC_SYNC_INTERVAL = 0.5
SERVICE_STATUS_FLUSH = 5
CONTAINER_EVENTS_LOG_INTERVAL = 2
HEARTBEAT_INTERVAL = 5

# How many times to let a service generate an error in this module before we disable it.
# This is only for analysis services, core services we keep retrying forever
MAXIMUM_SERVICE_ERRORS = 5
ERROR_EXPIRY_TIME = 60*60  # how long we wait before we forgive an error. (seconds)

# An environment variable that should be set when we are started with kubernetes, tells us how to attach
# the global Assemblyline config to new things that we launch.
KUBERNETES_AL_CONFIG = os.environ.get('KUBERNETES_AL_CONFIG')

HOSTNAME = os.getenv('HOSTNAME', platform.node())
NAMESPACE = os.getenv('NAMESPACE', 'al')
CLASSIFICATION_HOST_PATH = os.getenv('CLASSIFICATION_HOST_PATH', None)
CLASSIFICATION_CONFIGMAP = os.getenv('CLASSIFICATION_CONFIGMAP', None)
CLASSIFICATION_CONFIGMAP_KEY = os.getenv('CLASSIFICATION_CONFIGMAP_KEY', 'classification.yml')


class Pool:
    """
    This is a light wrapper around ThreadPoolExecutor to run batches of
    jobs as a context manager, and wait for the batch to finish after
    the context ends.
    """
    def __init__(self, size=10):
        self.pool = concurrent.futures.ThreadPoolExecutor(size)
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finish()

    def finish(self):
        while self.futures:
            self.futures.pop(0).result()

    def call(self, fn, *args, **kwargs):
        self.futures.append(self.pool.submit(fn, *args, **kwargs))


class ServiceProfile:
    """A profile, describing a currently running service.

    This includes how the service should be run, and conditions related to the scaling of the service.
    """
    def __init__(self, name, container_config: DockerConfig, config_hash=0, min_instances=0, max_instances=None,
                 growth=600, shrink=None, backlog=500, queue=None, shutdown_seconds=30):
        """
        :param name: Name of the service to manage
        :param container_config: Instructions on how to start this service
        :param min_instances: The minimum number of copies of this service keep running
        :param max_instances: The maximum number of copies permitted to be running
        :param growth: Delay before growing a service, unit-less, approximately seconds
        :param shrink: Delay before shrinking a service, unit-less, approximately seconds, defaults to -growth
        :param backlog: How long a queue backlog should be before it takes `growth` seconds to grow.
        :param queue: Queue name for monitoring
        """
        self.name = name
        self.queue: PriorityQueue = queue
        self.container_config = container_config
        self.target_duty_cycle = 0.9
        self.shutdown_seconds = shutdown_seconds
        self.config_hash = config_hash

        # How many instances we want, and can have
        self.min_instances = self._min_instances = max(0, int(min_instances))
        self._max_instances = max(0, int(max_instances)) if max_instances else float('inf')
        self.desired_instances: int = 0
        self.running_instances: int = 0

        # Information tracking when we want to grow/shrink
        self.pressure: float = 0
        self.growth_threshold = abs(float(growth))
        self.shrink_threshold = -self.growth_threshold/2 if shrink is None else -abs(float(shrink))
        self.leak_rate: float = 0.1

        # How long does a backlog need to be before we are concerned
        self.backlog = int(backlog)
        self.queue_length = 0
        self.duty_cycle = 0
        self.last_update = 0

    @property
    def cpu(self):
        return self.container_config.cpu_cores

    @property
    def ram(self):
        return self.container_config.ram_mb

    @property
    def instance_limit(self):
        if self._max_instances == float('inf'):
            return 0
        return self._max_instances

    @property
    def max_instances(self):
        # Adjust the max_instances based on the number that is already running
        # this keeps the scaler from running way ahead with its demands when resource caps are reached
        return min(self._max_instances, self.running_instances + 2)

    def update(self, delta, instances, backlog, duty_cycle):
        self.last_update = time.time()
        self.running_instances = instances
        self.queue_length = backlog
        self.duty_cycle = duty_cycle

        # Adjust min instances based on queue (if something has min_instances == 0, bump it up to 1 when
        # there is anything in the queue) this should have no effect for min_instances > 0
        self.min_instances = max(self._min_instances, int(bool(backlog)))
        self.desired_instances = max(self.min_instances, min(self.max_instances, self.desired_instances))

        # Should we scale up because of backlog
        self.pressure += delta * math.sqrt(backlog/self.backlog)

        # Should we scale down due to duty cycle? (are some of the workers idle)
        self.pressure -= delta * (self.target_duty_cycle - duty_cycle)/self.target_duty_cycle

        # Apply the friction, tendency to do nothing, move the change pressure gradually to the center.
        leak = min(self.leak_rate * delta, abs(self.pressure))
        self.pressure = math.copysign(abs(self.pressure) - leak, self.pressure)

        # When we are already at the minimum number of instances, don't let negative values build up
        # otherwise this can cause irregularities in scaling response around the min_instances
        if self.desired_instances == self.min_instances:
            self.pressure = max(0.0, self.pressure)

        if self.pressure >= self.growth_threshold:
            self.desired_instances = min(self.max_instances, self.desired_instances + 1)
            self.pressure = 0

        if self.pressure <= self.shrink_threshold:
            self.desired_instances = max(self.min_instances, self.desired_instances - 1)
            self.pressure = 0


class ScalerServer(CoreBase):
    def __init__(self, config=None, datastore=None, redis=None, redis_persist=None):
        super().__init__('assemblyline.scaler', config=config, datastore=datastore,
                         redis=redis, redis_persist=redis_persist)

        self.scaler_timeout_queue = NamedQueue(SCALER_TIMEOUT_QUEUE, host=self.redis_persist)
        self.error_count_lock = threading.Lock()
        self.error_count: Dict[str, List[float]] = {}
        self.status_table = ExpiringHash(SERVICE_STATE_HASH, host=self.redis, ttl=30*60)

        labels = {
            'app': 'assemblyline',
            'section': 'service',
        }

        if KUBERNETES_AL_CONFIG:
            self.log.info(f"Loading Kubernetes cluster interface on namespace: {NAMESPACE}")
            self.controller = KubernetesController(logger=self.log, prefix='alsvc_', labels=labels,
                                                   namespace=NAMESPACE, priority='al-service-priority')
            # If we know where to find it, mount the classification into the service containers
            if CLASSIFICATION_CONFIGMAP:
                self.controller.config_mount('classification-config', config_map=CLASSIFICATION_CONFIGMAP,
                                             key=CLASSIFICATION_CONFIGMAP_KEY,
                                             target_path='/etc/assemblyline/classification.yml')
        else:
            self.log.info("Loading Docker cluster interface.")
            self.controller = DockerController(logger=self.log, prefix=NAMESPACE,
                                               cpu_overallocation=self.config.core.scaler.cpu_overallocation,
                                               memory_overallocation=self.config.core.scaler.memory_overallocation,
                                               labels=labels)
            # If we know where to find it, mount the classification into the service containers
            if CLASSIFICATION_HOST_PATH:
                self.controller.global_mounts.append((CLASSIFICATION_HOST_PATH, '/etc/assemblyline/classification.yml'))

        # Information about services
        self.profiles: Dict[str, ServiceProfile] = {}
        self.profiles_lock = threading.RLock()
        self.service_threads: Dict[str, threading.Thread] = {}

        # Prepare a single threaded scheduler
        self.state = collection.Collection(period=self.config.core.metrics.export_interval)
        self.stopping = threading.Event()
        self.main_loop_exit = threading.Event()

    def sleep(self, timeout):
        self.stopping.wait(timeout)
        return self.running

    def log_crashes(self, fn):
        @functools.wraps(fn)
        def with_logs(*args, **kwargs):
            # noinspection PyBroadException
            try:
                fn(*args, **kwargs)
            except ServiceControlError as error:
                self.log.exception(f"Error while from service: {error.service_name}")
                self.handle_service_error(error.service_name)
            except Exception:
                self.log.exception(f'Crash in scaler: {fn.__name__}')
        return with_logs

    def add_service(self, profile: ServiceProfile):
        with self.profiles_lock:
            profile.desired_instances = max(self.controller.get_target(profile.name), profile.min_instances)
            profile.running_instances = profile.desired_instances
            self.log.debug(f'Starting service {profile.name} with a target of {profile.desired_instances}')
            profile.last_update = time.time()
            self.profiles[profile.name] = profile
            self.controller.add_profile(profile)

    def try_run(self):
        expected_threads = {
            'Log Container Events': self.log_crashes(self.log_container_events),
            'Process Timeouts': self.log_crashes(self.process_timeouts),
            'Flush Service Status': self.log_crashes(self.flush_service_status),
            'Service Configuration Sync': self.log_crashes(self.sync_services),
            'Service Adjuster': self.log_crashes(self.update_scaling),
            'Import Metrics': self.log_crashes(self.sync_metrics),
            'Export Metrics': self.log_crashes(self.export_metrics),
        }
        threads = {}

        # Run as long as we need to
        while self.running:
            # Check for any crashed threads
            for name, thread in list(threads.items()):
                if not thread.is_alive():
                    self.log.warning(f'Restarting thread: {name}')
                    threads.pop(name)

            # Start any missing threads
            for name, function in expected_threads.items():
                if name not in threads:
                    self.log.info(f'Starting thread: {name}')
                    threads[name] = thread = threading.Thread(target=function, name=name)
                    thread.start()

            # Take a break before doing it again
            super().heartbeat()
            self.export_metrics()
            self.sleep(2)

        for _t in threads.values():
            _t.join()

        self.main_loop_exit.set()

    def stop(self):
        super().stop()
        self.stopping.set()
        self.main_loop_exit.wait(30)
        self.controller.stop()

    def sync_services(self):
        while self.running:
            default_settings = self.config.core.scaler.service_defaults
            image_variables = defaultdict(str)
            image_variables.update(self.config.services.image_variables)
            with self.profiles_lock:
                current_services = set(self.profiles.keys())
            discovered_services = []

            # Get all the service data
            for service in self.datastore.list_all_services(full=True):
                service: Service = service
                name = service.name
                stage = self.get_service_stage(service.name)
                discovered_services.append(name)

                # noinspection PyBroadException
                try:
                    if service.enabled and stage == ServiceStage.Off:
                        # Enable this service's dependencies
                        self.controller.prepare_network(service.name, service.docker_config.allow_internet_access)
                        for _n, dependency in service.dependencies.items():
                            self.controller.start_stateful_container(
                                service_name=service.name,
                                container_name=_n,
                                spec=dependency,
                                labels={'dependency_for': service.name}
                            )

                        # Move to the next service stage
                        if service.update_config and service.update_config.wait_for_update:
                            self._service_stage_hash.set(name, ServiceStage.Update)
                        else:
                            self._service_stage_hash.set(name, ServiceStage.Running)

                    if not service.enabled:
                        self.stop_service(service.name, stage)
                        continue

                    # Check that all enabled services are enabled
                    if service.enabled and stage == ServiceStage.Running:
                        # Compute a hash of service properties not include in the docker config, that
                        # should still result in a service being restarted when changed
                        config_hash = hash(str(sorted(service.config.items())))
                        config_hash = hash((config_hash, str(service.submission_params)))

                        # Build the docker config for the service, we are going to either create it or
                        # update it so we need to know what the current configuration is either way
                        docker_config = service.docker_config
                        docker_config.image = Template(docker_config.image).safe_substitute(image_variables)
                        set_keys = set(var.name for var in docker_config.environment)
                        for var in default_settings.environment:
                            if var.name not in set_keys:
                                docker_config.environment.append(var)

                        # Add the service to the list of services being scaled
                        with self.profiles_lock:
                            if name not in self.profiles:
                                self.log.info(f'Adding {service.name} to scaling')
                                self.add_service(ServiceProfile(
                                    name=name,
                                    min_instances=default_settings.min_instances,
                                    growth=default_settings.growth,
                                    shrink=default_settings.shrink,
                                    config_hash=config_hash,
                                    backlog=default_settings.backlog,
                                    max_instances=service.licence_count,
                                    container_config=docker_config,
                                    queue=get_service_queue(name, self.redis),
                                    # Give service an extra 30 seconds to upload results
                                    shutdown_seconds=service.timeout + 30,
                                ))

                            # Update RAM, CPU, licence requirements for running services
                            else:
                                profile = self.profiles[name]

                                if profile.container_config != docker_config or profile.config_hash != config_hash:
                                    self.log.info(f"Updating deployment information for {name}")
                                    profile.container_config = docker_config
                                    profile.config_hash = config_hash
                                    self.controller.restart(profile)
                                    self.log.info(f"Deployment information for {name} replaced")

                                if service.licence_count == 0:
                                    profile._max_instances = float('inf')
                                else:
                                    profile._max_instances = service.licence_count
                except Exception:
                    self.log.exception(f"Error applying service settings from: {service.name}")
                    self.handle_service_error(service.name)

                # Find any services we have running, that are no longer in the database and remove them
                for stray_service in current_services - set(discovered_services):
                    stage = self.get_service_stage(stray_service)
                    self.stop_service(stray_service, stage)

            self.sleep(SERVICE_SYNC_INTERVAL)

    def stop_service(self, name, current_stage):
        if current_stage != ServiceStage.Off:
            # Disable this service's dependencies
            self.controller.stop_containers(labels={
                'dependency_for': name
            })

            # Mark this service as not running in the shared record
            self._service_stage_hash.set(name, ServiceStage.Off)

        # Stop any running disabled services
        with self.profiles_lock:
            if name in self.profiles or self.controller.get_target(name) > 0:
                self.log.info(f'Removing {name} from scaling')
                self.controller.set_target(name, 0)
                self.profiles.pop(name, None)

    def update_scaling(self):
        """Check if we need to scale any services up or down."""
        pool = Pool()
        while self.sleep(SCALE_INTERVAL):
            # Figure out what services are expected to be running and how many
            with self.profiles_lock:
                targets = {_p.name: self.controller.get_target(_p.name) for _p in self.profiles.values()}

                for name, profile in self.profiles.items():
                    self.log.debug(f'{name}')
                    self.log.debug(f'Instances \t{profile.min_instances} < {profile.desired_instances} | '
                                   f'{targets[name]} < {profile.max_instances}')
                    self.log.debug(
                        f'Pressure \t{profile.shrink_threshold} < {profile.pressure} < {profile.growth_threshold}')

                #
                #   1.  Any processes that want to release resources can always be approved first
                #
                with pool:
                    for name, profile in self.profiles.items():
                        if targets[name] > profile.desired_instances:
                            self.log.info(f"{name} wants less resources changing allocation "
                                          f"{targets[name]} -> {profile.desired_instances}")
                            pool.call(self.controller.set_target, name, profile.desired_instances)
                            targets[name] = profile.desired_instances

                #
                #   2.  Any processes that aren't reaching their min_instances target must be given
                #       more resources before anyone else is considered.
                #
                    for name, profile in self.profiles.items():
                        if targets[name] < profile.min_instances:
                            self.log.info(f"{name} isn't meeting minimum allocation "
                                          f"{targets[name]} -> {profile.min_instances}")
                            pool.call(self.controller.set_target, name, profile.min_instances)
                            targets[name] = profile.min_instances

                #
                #   3.  Try to estimate available resources, and based on some metric grant the
                #       resources to each service that wants them. While this free memory
                #       pool might be spread across many nodes, we are going to treat it like
                #       it is one big one, and let the orchestration layer sort out the details.
                #
                free_cpu = self.controller.free_cpu()
                free_memory = self.controller.free_memory()

                #
                def trim(prof: List[ServiceProfile]):
                    prof = [_p for _p in prof if _p.desired_instances > targets[_p.name]]
                    drop = [_p for _p in prof if _p.cpu > free_cpu or _p.ram > free_memory]
                    if drop:
                        drop = {_p.name: (_p.cpu, _p.ram) for _p in drop}
                        self.log.debug(f"Can't make more because not enough resources {drop}")
                    prof = [_p for _p in prof if _p.cpu <= free_cpu and _p.ram <= free_memory]
                    return prof

                profiles = trim(list(self.profiles.values()))

                while profiles:
                    # TODO do we need to add balancing metrics other than 'least running' for this? probably
                    profiles.sort(key=lambda _p: self.controller.get_target(_p.name))

                    # Add one for the profile at the bottom
                    free_memory -= profiles[0].container_config.ram_mb
                    free_cpu -= profiles[0].container_config.cpu_cores
                    targets[profiles[0].name] += 1

                    # profiles = [_p for _p in profiles if _p.desired_instances > targets[_p.name]]
                    # profiles = [_p for _p in profiles if _p.cpu < free_cpu and _p.ram < free_memory]
                    profiles = trim(profiles)

                # Apply those adjustments we have made back to the controller
                with pool:
                    for name, value in targets.items():
                        old = self.controller.get_target(name)
                        if value != old:
                            self.log.info(f"Scaling service {name}: {old} -> {value}")
                            pool.call(self.controller.set_target, name, value)

    def handle_service_error(self, service_name):
        """Handle an error occurring in the *analysis* service.

        Errors for core systems should simply be logged, and a best effort to continue made.

        For analysis services, ignore the error a few times, then disable the service.
        """
        with self.error_count_lock:
            self.error_count[service_name].append(time.time())
            self.error_count[service_name] = [
                _t for _t in self.error_count[service_name]
                if _t >= time.time() - ERROR_EXPIRY_TIME
            ]

            if len(self.error_count[service_name]) >= MAXIMUM_SERVICE_ERRORS:
                self.datastore.service_delta.update(service_name, [
                    (self.datastore.service_delta.UPDATE_SET, 'enabled', False)
                ])
                del self.error_count[service_name]

    def sync_metrics(self):
        """Check if there are any pub-sub messages we need."""
        while self.sleep(METRIC_SYNC_INTERVAL):
            # Pull service metrics from redis
            service_data = self.status_table.items()
            for host, (service, state, time_limit) in service_data.items():
                # If an entry hasn't expired, take it into account
                if time.time() < time_limit:
                    self.state.update(service=service, host=host, throughput=0,
                                      busy_seconds=METRIC_SYNC_INTERVAL if state == ServiceStatus.Running else 0)

                # If an entry expired a while ago, the host is probably not in use any more
                if time.time() > time_limit + 600:
                    self.status_table.pop(host)

            # Check the set of services that might be sitting at zero instances, and if it is, we need to
            # manually check if it is offline
            export_interval = self.config.core.metrics.export_interval
            with self.profiles_lock:
                for profile_name, profile in self.profiles.items():
                    # Pull out statistics from the metrics regularization
                    update = self.state.read(profile_name)
                    if update:
                        delta = time.time() - profile.last_update
                        profile.update(
                            delta=delta,
                            backlog=profile.queue.length(),
                            **update
                        )

                    # Check if we expect no messages, if so pull the queue length ourselves since there is no heartbeat
                    if self.controller.get_target(profile_name) == 0 and \
                            profile.desired_instances == 0 and profile.queue:
                        queue_length = profile.queue.length()
                        if queue_length > 0:
                            self.log.info(f"Service at zero instances has messages: "
                                          f"{profile.name} ({queue_length} in queue)")
                        profile.update(
                            delta=export_interval,
                            instances=0,
                            backlog=queue_length,
                            duty_cycle=profile.target_duty_cycle
                        )

            # TODO maybe find another way of implementing this that is less aggressive
            # for profile_name, profile in self.profiles.items():
            #     # In the case that there should actually be instances running, but we haven't gotten
            #     # any heartbeat messages we might be waiting for a container that can't start properly
            #     if self.services.controller.get_target(profile_name) > 0:
            #         if time.time() - profile.last_update > profile.shutdown_seconds:
            #             self.log.error(f"Starting service {profile_name} has timed out "
            #                            f"({time.time() - profile.last_update} > {profile.shutdown_seconds} seconds)")
            #
            #             # Disable the the service
            #             self.datastore.service_delta.update(profile_name, [
            #                 (self.datastore.service_delta.UPDATE_SET, 'enabled', False)
            #             ])

    def process_timeouts(self):
        with concurrent.futures.ThreadPoolExecutor(10) as pool:
            futures = []

            while self.running:
                # Process finished
                finished = [_f for _f in futures if _f.done()]
                futures = [_f for _f in futures if _f in finished]
                for _f in finished:
                    exception = _f.exception()
                    if exception is not None:
                        self.log.error(f"Exception trying to stop timed out service container: {exception}")

                # Process new messages
                message = self.scaler_timeout_queue.pop(blocking=True, timeout=1)
                if not message:
                    continue
                self.log.info(f"Killing service container: {message['container']} running: {message['service']}")
                pool.submit(fn=self.controller.stop_container, args=(message['service'], message['container']))

    def export_metrics(self):
        while self.sleep(self.config.logging.export_interval):
            service_metrics = {}
            with self.profiles_lock:
                for service_name, profile in self.profiles.items():
                    service_metrics[service_name] = {
                        'running': profile.running_instances,
                        'target': profile.desired_instances,
                        'minimum': profile.min_instances,
                        'maximum': profile.instance_limit,
                        'dynamic_maximum': profile.max_instances,
                        'queue': profile.queue_length,
                        'duty_cycle': profile.duty_cycle,
                        'pressure': profile.pressure
                    }

            for service_name, metrics in service_metrics.items():
                export_metrics_once(service_name, Status, metrics, host=HOSTNAME,
                                    counter_type='scaler-status', config=self.config, redis=self.redis)

            memory, memory_total = self.controller.memory_info()
            cpu, cpu_total = self.controller.cpu_info()
            metrics = {
                'memory_total': memory_total,
                'cpu_total': cpu_total,
                'memory_free': memory,
                'cpu_free': cpu
            }
            export_metrics_once('scaler', Metrics, metrics, host=HOSTNAME,
                                counter_type='scaler', config=self.config, redis=self.redis)

    def flush_service_status(self):
        """The service status table may have references to containers that have crashed. Try to remove them all."""
        bad_containers = set()
        while self.sleep(SERVICE_STATUS_FLUSH):

            # Pull all container names
            names = set(self.controller.get_running_container_names())

            # Get the names we have status for
            for hostname in self.status_table.keys():
                if hostname not in names:
                    if hostname in bad_containers:
                        self.status_table.pop(hostname)
                        bad_containers.discard(hostname)
                    else:
                        bad_containers.add(hostname)

    def log_container_events(self):
        """The service status table may have references to containers that have crashed. Try to remove them all."""
        while self.sleep(CONTAINER_EVENTS_LOG_INTERVAL):
            for message in self.controller.new_events():
                self.log.warning("Container Event :: " + message)
