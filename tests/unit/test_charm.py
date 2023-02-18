# Copyright 2022 Guillaume Belanger
# See LICENSE file for licensing details.

import unittest
from unittest.mock import Mock, patch

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

    @patch("ops.model.Container.push")
    def test_given_can_connect_to_bessd_workload_container_when_install_then_config_file_is_written(
        self,
        patch_push,
    ):
        self.harness.set_can_connect(container="bessd", val=True)

        self.harness.charm._on_install(event=Mock())
        patch_push.assert_called_with(
            path="/etc/bess/conf/upf.json",
            source='{\n  "access": {\n    "ifname": "access"\n  },\n  "core": {\n    "ifname": "core"\n  },\n  "cpiface": {\n    "dnn": "internet",\n    "hostname": "upf-operator.whatever.svc.cluster.local",\n    "enable_ue_ip_alloc": false,\n    "http_port": "8080"\n  },\n  "mode": "af_packet",\n  "hwcksum": true,\n  "log_level": "trace",\n  "gtppsc": true,\n  "measure_upf": false\n}',  # noqa: E501
        )

    @patch("kubernetes.Kubernetes.create_network_attachment_definitions")
    @patch("ops.model.Container.push", new=Mock())
    def test_given_can_connect_to_bessd_when_on_install_then_network_attachment_definition_is_created(  # noqa: E501
        self,
        patch_create_network_attachment_definitions,
    ):
        self.harness.set_can_connect(container="bessd", val=True)

        self.harness.charm._on_install(event=Mock())

        patch_create_network_attachment_definitions.assert_called_once()

    @patch("kubernetes.Kubernetes.patch_statefulset")
    @patch("ops.model.Container.push", new=Mock())
    def test_given_can_connect_to_bessd_when_on_install_then_statefulset_is_patched(
        self, patch_statefulset
    ):
        self.harness.set_can_connect(container="bessd", val=True)

        self.harness.charm._on_install(event=Mock())

        patch_statefulset.assert_called_once()

    @patch("ops.model.Container.exists")
    def test_given_bessd_config_file_is_written_when_bessd_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists
    ):
        patch_exists.return_value = True

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

    @patch("ops.model.Container.exists")
    def test_given_can_connect_when_routectl_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists
    ):
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

    @patch("ops.model.Container.exists")
    def test_given_can_connect_when_web_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists
    ):
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

    @patch("ops.model.Container.exists")
    def test_given_can_connect_when_pfcp_agent_pebble_ready_then_pebble_plan_is_applied(
        self, patch_exists
    ):
        patch_exists.return_value = True

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

    @patch("ops.model.Container.exists")
    def test_given_config_file_is_written_and_all_services_are_running_when_pebble_ready_then_status_is_active(  # noqa: E501
        self, patch_exists
    ):
        patch_exists.return_value = True

        self.harness.container_pebble_ready("bessd")
        self.harness.container_pebble_ready("routectl")
        self.harness.container_pebble_ready("web")
        self.harness.container_pebble_ready("pfcp-agent")

        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    @patch("ops.model.Container.exists")
    def test_given_bessd_service_is_running_when_upf_relation_joins_then_upf_info_is_added_to_relation_data(  # noqa: E501
        self, patch_exists
    ):
        patch_exists.return_value = True
        self.harness.set_leader(is_leader=True)
        self.harness.container_pebble_ready(container_name="bessd")
        relation_id = self.harness.add_relation(relation_name="upf", remote_app="smf")

        self.harness.add_relation_unit(relation_id, "smf/0")

        relation_data = self.harness.get_relation_data(
            relation_id=relation_id, app_or_unit=self.harness.model.app
        )

        assert relation_data["url"] == "upf-operator.whatever.svc.cluster.local"
