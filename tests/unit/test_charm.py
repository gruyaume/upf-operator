# Copyright 2022 Guillaume Belanger
# See LICENSE file for licensing details.

import unittest
from unittest.mock import Mock, call, patch

from ops import testing
from ops.model import ActiveStatus

from charm import UPFOperatorCharm


class TestCharm(unittest.TestCase):
    @patch("lightkube.core.client.GenericSyncClient")
    @patch(
        "charm.KubernetesServicePatch",
        lambda charm, ports: None,
    )
    def setUp(self, patch_k8s_client):
        self.namespace = "whatever"
        self.harness = testing.Harness(UPFOperatorCharm)
        self.harness.set_model_name(name=self.namespace)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_given_use_sriov_config_when_config_changed_then_not_implemented_error_is_raised(self):
        config = {"use-sriov": True}

        with self.assertRaises(NotImplementedError):
            self.harness.update_config(key_values=config)

    def test_given_use_hugepages_when_config_changed_then_not_implemented_error_is_raised(self):
        config = {"use-hugepages": True}

        with self.assertRaises(NotImplementedError):
            self.harness.update_config(key_values=config)

    @patch("kubernetes.Kubernetes.create_network_attachment_definitions")
    @patch("kubernetes.Kubernetes.patch_statefulset")
    def test_given_default_config_when_on_install_then_statefulset_is_patched(
        self,
        patch_statefulset,
        create_network_attachment_definitions,
    ):
        self.harness.charm.on.install.emit()

        create_network_attachment_definitions.assert_called_once_with(use_sriov=False)
        patch_statefulset.assert_called_once_with(
            statefulset_name="upf-operator", use_sriov=False, use_hugepages=False
        )

    @patch("ops.model.Container.push")
    def test_given_can_connect_to_bessd_workload_container_when_config_changed_then_config_file_is_written(  # noqa: E501
        self,
        patch_push,
    ):
        self.harness.set_can_connect(container="bessd", val=True)
        config = {
            "use-sriov": False,
            "use-hugepages": False,
        }

        self.harness.update_config(key_values=config)

        patch_push.assert_has_calls(
            [
                call(
                    path="/etc/bess/conf/upf.json",
                    source='{\n  "access": {\n    "ifname": "access"\n  },\n  "core": {\n    "ifname": "core"\n  },\n  "cpiface": {\n    "dnn": "internet",\n    "enable_ue_ip_alloc": false,\n    "hostname": "upf-operator.whatever.svc.cluster.local",\n    "http_port": "8080",\n    "ue_ip_pool": "172.250.0.0/16"\n  },\n  "enable_notify_bess": true,\n  "gtppsc": true,\n  "hwcksum": true,\n  "log_level": "trace",\n  "max_sessions": 50000,\n  "measure_flow": false,\n  "measure_upf": true,\n  "mode": "af_packet",\n  "notify_sockaddr": "/pod-share/notifycp",\n  "qci_qos_config": [\n    {\n      "burst_duration_ms": 10,\n      "cbs": 50000,\n      "ebs": 50000,\n      "pbs": 50000,\n      "priority": 7,\n      "qci": 0\n    }\n  ],\n  "slice_rate_limit_config": {\n    "n3_bps": 1000000000,\n    "n3_burst_bytes": 12500000,\n    "n6_bps": 1000000000,\n    "n6_burst_bytes": 12500000\n  },\n  "table_sizes": {\n    "appQERLookup": 200000,\n    "farLookup": 150000,\n    "pdrLookup": 50000,\n    "sessionQERLookup": 100000\n  },\n  "workers": 1\n}',  # noqa: E501
                ),
                call(
                    path="/etc/bess/conf/bessd-poststart.sh",
                    source="#!/bin/bash\n\n# Copyright 2020-present Open Networking Foundation\n#\n# SPDX-License-Identifier: Apache-2.0\n\nset -ex\n\nuntil bessctl run /opt/bess/bessctl/conf/up4; do\n    sleep 2;\ndone;\n",  # noqa: E501
                    permissions=0o755,
                ),
            ]
        )

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exec", new=Mock())
    @patch("ops.model.Container.exists")
    def test_given_bessd_config_file_is_written_when_bessd_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists, statefulset_is_patched
    ):
        patch_exists.return_value = True
        statefulset_is_patched.return_value = True

        self.harness.container_pebble_ready(container_name="bessd")

        expected_plan = {
            "services": {
                "bessd": {
                    "startup": "enabled",
                    "override": "replace",
                    "command": "bessd -f -grpc-url=0.0.0.0:10514 -m 0",
                    "environment": {"CONF_FILE": "/etc/bess/conf/upf.json"},
                }
            }
        }

        updated_plan = self.harness.get_container_pebble_plan("bessd").to_dict()

        self.assertEqual(expected_plan, updated_plan)

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exec")
    @patch("ops.model.Container.exists")
    def test_given_bessd_config_file_is_written_when_bessd_pebble_ready_then_initial_commands_are_executed(
        self, patch_exists, patch_exec, statefulset_is_patched
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True

        self.harness.container_pebble_ready(container_name="bessd")

        patch_exec.assert_has_calls(
            calls=[
                call(
                    command=["ip", "route", "replace", "192.168.251.0/24", "via", "192.168.252.1"],
                    timeout=30,
                ),
                call().wait_output(),
                call(
                    command=[
                        "ip",
                        "route",
                        "replace",
                        "default",
                        "via",
                        "192.168.250.1",
                        "metric",
                        "110",
                    ],
                    timeout=30,
                ),
                call().wait_output(),
                call(
                    command=[
                        "iptables",
                        "-I",
                        "OUTPUT",
                        "-p",
                        "icmp",
                        "--icmp-type",
                        "port-unreachable",
                        "-j",
                        "DROP",
                    ],
                    timeout=30,
                ),
                call().wait_output(),
                call(
                    command=["/bin/bash", "-c", "/etc/bess/conf/bessd-poststart.sh"],
                    environment={"CONF_FILE": "/etc/bess/conf/upf.json"},
                ),
                call().wait_output(),
            ],
        )

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exists")
    def test_given_can_connect_when_routectl_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists, statefulset_is_patched
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True

        self.harness.container_pebble_ready(container_name="routectl")

        expected_plan = {
            "services": {
                "routectl": {
                    "startup": "enabled",
                    "override": "replace",
                    "command": "/opt/bess/bessctl/conf/route_control.py -i access core",
                    "environment": {"PYTHONUNBUFFERED": "1"},
                }
            }
        }

        updated_plan = self.harness.get_container_pebble_plan("routectl").to_dict()

        self.assertEqual(expected_plan, updated_plan)

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exists")
    def test_given_can_connect_when_web_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists, statefulset_is_patched
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True

        self.harness.container_pebble_ready(container_name="web")

        expected_plan = {
            "services": {
                "web": {
                    "startup": "enabled",
                    "override": "replace",
                    "command": "bessctl http 0.0.0.0 8000",
                }
            }
        }

        updated_plan = self.harness.get_container_pebble_plan("web").to_dict()

        self.assertEqual(expected_plan, updated_plan)

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exec", new=Mock())
    @patch("ops.model.Container.exists")
    def test_given_bessd_service_is_running_when_pfcp_agent_pebble_ready_then_pebble_plan_is_applied(  # noqa: E501
        self,
        patch_exists,
        statefulset_is_patched,
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True
        self.harness.container_pebble_ready(container_name="bessd")

        self.harness.container_pebble_ready(container_name="pfcp-agent")

        expected_plan = {
            "services": {
                "pfcp-agent": {
                    "startup": "enabled",
                    "override": "replace",
                    "command": "pfcpiface -config /tmp/conf/upf.json",
                }
            }
        }

        updated_plan = self.harness.get_container_pebble_plan("pfcp-agent").to_dict()

        self.assertEqual(expected_plan, updated_plan)

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exec", new=Mock())
    @patch("ops.model.Container.exists")
    def test_given_config_file_is_written_and_all_services_are_running_when_pebble_ready_then_status_is_active(  # noqa: E501
        self, patch_exists, statefulset_is_patched
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True

        self.harness.container_pebble_ready("bessd")
        self.harness.container_pebble_ready("routectl")
        self.harness.container_pebble_ready("web")
        self.harness.container_pebble_ready("pfcp-agent")

        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch("kubernetes.Kubernetes.statefulset_is_patched")
    @patch("ops.model.Container.exec", new=Mock())
    @patch("ops.model.Container.exists")
    def test_given_bessd_service_is_running_when_upf_relation_joins_then_upf_info_is_added_to_relation_data(  # noqa: E501
        self, patch_exists, statefulset_is_patched
    ):
        statefulset_is_patched.return_value = True
        patch_exists.return_value = True
        self.harness.set_leader(is_leader=True)
        self.harness.container_pebble_ready(container_name="bessd")
        relation_id = self.harness.add_relation(relation_name="upf", remote_app="smf")

        self.harness.add_relation_unit(relation_id, "smf/0")

        relation_data = self.harness.get_relation_data(
            relation_id=relation_id, app_or_unit=self.harness.model.app
        )

        assert relation_data["url"] == "upf-operator.whatever.svc.cluster.local"

    def test_given_cant_connect_to_bessd_when_upf_relation_joins_then_relation_data_is_not_updated(
        self,
    ):
        self.harness.set_can_connect(container="bessd", val=False)

        relation_id = self.harness.add_relation(relation_name="upf", remote_app="smf")
        self.harness.add_relation_unit(relation_id, "smf/0")

        relation_data = self.harness.get_relation_data(
            relation_id=relation_id, app_or_unit=self.harness.model.app
        )

        assert relation_data == {}
