# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from typing import Any, Dict, Generator, List, Tuple

from datadog_checks.base import AgentCheck, ConfigurationError

from .api import MarkLogicApi
from .config import Config
from .constants import RESOURCE_METRICS_AVAILABLE, RESOURCE_TYPES
from .parsers.health import parse_summary_health
from .parsers.request import parse_summary_request_resource_metrics
from .parsers.resources import parse_resources
from .parsers.status import (
    parse_per_resource_status_metrics,
    parse_summary_status_base_metrics,
    parse_summary_status_resource_metrics,
)
from .parsers.storage import parse_summary_storage_base_metrics


class MarklogicCheck(AgentCheck):
    __NAMESPACE__ = 'marklogic'
    SERVICE_CHECK_CONNECT = 'can_connect'

    def __init__(self, *args, **kwargs):
        # type: (*Any, **Any) -> None
        super(MarklogicCheck, self).__init__(*args, **kwargs)

        url = self.instance.get('url')
        if not url:
            raise ConfigurationError("url is a required configuration.")

        self.api = MarkLogicApi(self.http, url)

        self.config = Config(self.instance)

        self.collectors = [
            self.collect_summary_status_base_metrics,
            self.collect_summary_status_resource_metrics,
            self.collect_summary_storage_base_metrics,
            self.collect_per_resource_metrics,
        ]

        # Refreshed at each check
        self.resources_to_monitor = {
            'forest': [],
            'database': [],
            'host': [],
            'server': [],
        }  # type: Dict[str, List[Any]]
        self.resources = []  # type: List[Dict[str, str]]

    def check(self, _):
        # type: (Any) -> None
        raw_resources = self.api.get_resources()
        self.resources = parse_resources(raw_resources)
        self.resources_to_monitor = self.get_resources_to_monitor()

        for collector in self.collectors:
            try:
                collector()
            except Exception:
                self.log.exception("Collector %s failed while collecting metrics", collector.__name__)

        self.submit_service_checks()

    def collect_summary_status_resource_metrics(self):
        # type: () -> None
        """
        Collect Summary Status Resource Metrics.

        Only necessary for forest resource metrics. Other resources metrics are already collected via
        `collect_summary_status_base_metrics`.
        """
        for resource_type in ['forest']:
            res_meta = RESOURCE_TYPES[resource_type]
            data = self.api.get_status_data(res_meta['plural'])
            metrics = parse_summary_status_resource_metrics(resource_type, data, self.config.tags)
            self.submit_metrics(metrics)

    def collect_summary_status_base_metrics(self):
        # type: () -> None
        """
        Collect Summary Status Base Metrics.

        Includes:
          - Summary Request Metrics (Same metrics as `/requests`)
          - Summary Host Metrics (Same metrics as `/hosts`)
          - Summary Server Metrics (Same metrics as `/servers`)
          - Summary Transaction Metrics
        """
        data = self.api.get_status_data()
        self.submit_version_metadata(data)
        metrics = parse_summary_status_base_metrics(data, self.config.tags)
        self.submit_metrics(metrics)

    def collect_summary_storage_base_metrics(self):
        # type: () -> None
        """
        Collect Base Storage Metrics
        """
        data = self.api.get_storage_data()
        metrics = parse_summary_storage_base_metrics(data, self.config.tags)
        self.submit_metrics(metrics)

    def get_resources_to_monitor(self):
        # type: () -> Dict[str, List[Any]]
        filtered_resources = {
            'forest': [],
            'database': [],
            'host': [],
            'server': [],
        }  # type: Dict[str, List[Any]]

        for res in self.resources:
            if self._is_resource_included(res):
                filtered_resources[res['type']].append(res)

        self.log.debug('Filtered resources to monitor: %s', filtered_resources)

        return filtered_resources

    def collect_per_resource_metrics(self):
        # type: () -> None
        """
        Collect Per Resource Metrics.
        """
        for res_type in self.resources_to_monitor.keys():
            for res in self.resources_to_monitor[res_type]:
                tags = ['{}_name:{}'.format(res_type, res['name'])] + self.config.tags
                if res.get('group'):
                    tags.append('group_name:{}'.format(res['group']))

                if RESOURCE_METRICS_AVAILABLE[res_type]['status']:
                    self._collect_resource_status_metrics(res_type, res['uri'], tags)

                if RESOURCE_METRICS_AVAILABLE[res_type]['storage']:
                    self._collect_resource_storage_metrics(res_type, res['name'], res.get('group'), tags)

                if RESOURCE_METRICS_AVAILABLE[res_type]['requests']:
                    self._collect_resource_request_metrics(res_type, res['name'], res.get('group'), tags)

    def _collect_resource_status_metrics(self, resource_type, uri, tags):
        # type: (str, str, List[str]) -> None
        """ Collect status metrics of a specific resource """
        data = self.api.http_get(uri, {'view': 'status'})
        metrics = parse_per_resource_status_metrics(resource_type, data, tags)
        self.submit_metrics(metrics)

    def _collect_resource_storage_metrics(self, resource_type, name, group, tags):
        # type: (str, str, str, List[str]) -> None
        # TODO: remove duplication with filters
        # forests.storage.forest.disk-size is submitted twice when using a resource filter.
        """ Collect storage metrics of a specific resource """
        data = self.api.get_storage_data(resource=resource_type, name=name, group=group)
        metrics = parse_summary_storage_base_metrics(data, tags)
        self.submit_metrics(metrics)

    def _collect_resource_request_metrics(self, resource_type, name, group, tags):
        # type: (str, str, str, List[str]) -> None
        """ Collect request metrics of a specific resource """
        data = self.api.get_requests_data(resource=resource_type, name=name, group=group)
        metrics = parse_summary_request_resource_metrics(data, tags)
        self.submit_metrics(metrics)

    def submit_metrics(self, metrics):
        # type: (Generator[Tuple, None, None]) -> None
        for metric_type, metric_name, value_data, tags in metrics:
            getattr(self, metric_type)(metric_name, value_data, tags=tags)

    @AgentCheck.metadata_entrypoint
    def submit_version_metadata(self, data):
        # type: (Dict[str, Any]) -> None
        # Example: 9.0-12
        try:
            version = data['local-cluster-status']['version']
            self.set_metadata(
                'version',
                version,
                scheme='regex',
                final_scheme='semver',
                pattern=r'(?P<major>\d+)\.(?P<minor>\d+)\-(?P<patch>\d+)',
            )
        except Exception as e:
            self.log.warning('Error collecting MarkLogic version: %s', str(e))

    def submit_service_checks(self):
        # type: () -> None
        data = {}
        try:
            data = self.api.get_health()
            # Doesn't report resource with no issue
            health_report = parse_summary_health(data)

            # If a resource doesn't appear in the health endpoint it means no problem was detected
            for res in self.resources:
                if res['type'] == 'database' or res['type'] == 'forest':
                    service_check_name = '{}.health'.format(res['type'])
                    res_tags = self.config.tags + ['{}_name:{}'.format(res['type'], res['name'])]
                    res_detailed = health_report[res['type']].get(res['name'])
                    if res_detailed:
                        self.service_check(
                            service_check_name, res_detailed['code'], tags=res_tags, message=res_detailed['message']
                        )
                    else:
                        self.service_check(service_check_name, self.OK, res_tags)

            self.service_check(self.SERVICE_CHECK_CONNECT, self.OK, self.config.tags)
        except Exception as e:
            # Not enough permissions
            if data.get('code') == 'HEALTH-CLUSTER-ERROR':
                self.log.error("The user needs `manage-admin` permission to monitor databases health.")
                self.service_check(self.SERVICE_CHECK_CONNECT, self.UNKNOWN, self.config.tags)
            # Couldn't access the health endpoint
            else:
                self.log.error("Failed to monitor databases health: %s.", e)
                self.service_check(self.SERVICE_CHECK_CONNECT, self.CRITICAL, self.config.tags)

    def _is_resource_included(self, resource):
        # type: (Dict[str, str]) -> bool
        # If the resource is in an include filter and not in an exclude filter, return True, otherwise return False.
        for include_filter in self.config.resource_filters['included']:
            if include_filter.match(resource['type'], resource['name'], resource['id'], resource.get('group')):
                for exclude_filter in self.config.resource_filters['excluded']:
                    if exclude_filter.match(resource['type'], resource['name'], resource['id'], resource.get('group')):
                        return False
                return True

        return False