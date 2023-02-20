#!/usr/bin/env python3
# Copyright 2022 Guillaume Belanger
# See LICENSE file for licensing details.

"""Charmed operator for the 5G UPF service."""

import logging

from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.upf_operator.v0.upf import UPFProvides
from jinja2 import Environment, FileSystemLoader
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase, InstallEvent, PebbleReadyEvent, RelationJoinedEvent, RemoveEvent
from ops.main import main
from ops.model import ActiveStatus, Container, ModelError, WaitingStatus
from ops.pebble import ExecError, Layer

from kubernetes import Kubernetes

logger = logging.getLogger(__name__)

BESSD_CONTAINER_CONFIG_PATH = "/etc/bess/conf"
PFCP_AGENT_CONTAINER_CONFIG_PATH = "/tmp/conf"
CONFIG_FILE_NAME = "upf.json"
PROMETHEUS_PORT = 8080


class UPFOperatorCharm(CharmBase):
    """Main class to describe juju event handling for the 5G UPF operator."""

    def __init__(self, *args):
        super().__init__(*args)
        self._kubernetes = Kubernetes(namespace=self.model.name)
        self._bessd_container_name = self._bessd_service_name = "bessd"
        self._routectl_container_name = self._routectl_service_name = "routectl"
        self._web_container_name = self._web_service_name = "web"
        self._pfcp_agent_container_name = self._pfcp_agent_service_name = "pfcp-agent"
        self._bessd_container = self.unit.get_container(self._bessd_container_name)
        self._routectl_container = self.unit.get_container(self._routectl_container_name)
        self._web_container = self.unit.get_container(self._web_container_name)
        self._pfcp_agent_container = self.unit.get_container(self._pfcp_agent_container_name)
        self._upf_provides = UPFProvides(charm=self, relationship_name="upf")
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.bessd_pebble_ready, self._on_bessd_pebble_ready)
        self.framework.observe(self.on.routectl_pebble_ready, self._on_routectl_pebble_ready)
        self.framework.observe(self.on.web_pebble_ready, self._on_web_pebble_ready)
        self.framework.observe(self.on.pfcp_agent_pebble_ready, self._on_pfcp_agent_pebble_ready)
        self.framework.observe(self.on.upf_relation_joined, self._on_upf_relation_joined)
        self._metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[
                {
                    "static_configs": [{"targets": [f"*:{PROMETHEUS_PORT}"]}],
                }
            ],
        )
        self._service_patcher = KubernetesServicePatch(
            charm=self,
            ports=[
                ServicePort(name="pfcp", port=8805, protocol="UDP"),
                ServicePort(name="bess-web", port=8000),
                ServicePort(name="prometheus-exporter", port=PROMETHEUS_PORT),
            ],
        )

    def _on_install(self, event: InstallEvent) -> None:
        """Handle install event."""
        if not self._bessd_container.can_connect():
            self.unit.status = WaitingStatus("Waiting for container to be ready")
            event.defer()
            return
        self._write_config_file()
        self._write_poststart_script()
        self._kubernetes.create_network_attachment_definitions()
        self._kubernetes.patch_statefulset(statefulset_name=self.app.name)

    def _on_remove(self, event: RemoveEvent) -> None:
        """Handle remove event."""
        self._kubernetes.delete_network_attachment_definitions()

    def _on_upf_relation_joined(self, event: RelationJoinedEvent) -> None:
        try:
            upf_service = self._bessd_container.get_service(service_name=self._bessd_service_name)
        except ModelError:
            logger.info("UPF `bessd` service not found")
            return
        if not upf_service.is_running():
            logger.info("UPF `bessd` service is not running")
            return
        self._update_upf_relation()

    def _write_config_file(self) -> None:
        jinja2_environment = Environment(loader=FileSystemLoader("src/templates/"))
        template = jinja2_environment.get_template(f"{CONFIG_FILE_NAME}.j2")
        content = template.render(
            upf_hostname=self._upf_hostname,
        )
        self._bessd_container.push(
            path=f"{BESSD_CONTAINER_CONFIG_PATH}/{CONFIG_FILE_NAME}", source=content
        )
        logger.info(f"Pushed {CONFIG_FILE_NAME} config file")

    def _write_poststart_script(self) -> None:
        with open("src/bessd-poststart.sh", "r") as f:
            content = f.read()
        self._bessd_container.push(
            path=f"{BESSD_CONTAINER_CONFIG_PATH}/bessd-poststart.sh",
            source=content,
            permissions=0o755,
        )
        logger.info("Pushed bessd-poststart.sh startup file")

    def _update_upf_relation(self):
        """Update the UPF relation with the URL of the UPF service."""
        self._upf_provides.set_info(url=self._upf_hostname)

    @property
    def _upf_hostname(self) -> str:
        return f"{self.model.app.name}.{self.model.name}.svc.cluster.local"

    @property
    def _bessd_config_file_is_written(self) -> bool:
        """Returns whether the bessd config file was written to the workload container."""
        if not self._bessd_container.exists(f"{BESSD_CONTAINER_CONFIG_PATH}/{CONFIG_FILE_NAME}"):
            logger.info(f"Config file is not written: {CONFIG_FILE_NAME}")
            return False
        logger.info("Config file is written")
        return True

    @property
    def _podstart_file_is_written(self) -> bool:
        """Returns whether the podstart file was written to the workload container."""
        if not self._bessd_container.exists(f"{BESSD_CONTAINER_CONFIG_PATH}/bessd-poststart.sh"):
            logger.info("Startup file is not written: bessd-poststart.sh")
            return False
        logger.info("Startup file is written")
        return True

    @property
    def _pfcp_agent_config_file_is_written(self) -> bool:
        """Returns whether the pfcp agent config file was written to the workload container."""
        if not self._pfcp_agent_container.exists(
            f"{PFCP_AGENT_CONTAINER_CONFIG_PATH}/{CONFIG_FILE_NAME}"
        ):
            logger.info(f"Config file is not written: {CONFIG_FILE_NAME}")
            return False
        logger.info("Config file is written")
        return True

    def _on_bessd_pebble_ready(self, event: PebbleReadyEvent) -> None:
        """Handle Pebble ready event."""
        if not self._bessd_container.can_connect():
            self.unit.status = WaitingStatus("Waiting for bessd container to be ready")
            event.defer()
            return
        if not self._bessd_config_file_is_written:
            self.unit.status = WaitingStatus("Waiting for config file to be written")
            return
        if not self._podstart_file_is_written:
            self.unit.status = WaitingStatus("Waiting for podstart file to be written")
            return
        self._bessd_container.add_layer("upf", self._bessd_pebble_layer, combine=True)
        self._bessd_container.replan()
        self._execute_bessd_poststart_script()
        self._set_application_status()
        self._update_upf_relation()

    def _execute_bessd_poststart_script(self) -> None:
        """Execute the bessd-poststart.sh script."""
        process = self._bessd_container.exec(
            command=["/bin/bash", "-c", f"{BESSD_CONTAINER_CONFIG_PATH}/bessd-poststart.sh"],
            environment=self._bessd_environment_variables,
        )
        try:
            process.wait_output()
        except ExecError as e:
            logger.error("Exited with code %d. Stderr:", e.exit_code)
            for line in e.stderr.splitlines():  # type: ignore[union-attr]
                logger.error("    %s", line)
            raise e
        logger.info("Successfully ran bessd startup script")

    def _on_routectl_pebble_ready(self, event: PebbleReadyEvent) -> None:
        """Handle routectl Pebble ready event."""
        if not self._routectl_container.can_connect():
            self.unit.status = WaitingStatus("Waiting for routectl container to be ready")
            event.defer()
            return
        self._routectl_container.add_layer("routectl", self._routectl_pebble_layer, combine=True)
        self._routectl_container.replan()
        self._set_application_status()

    def _on_web_pebble_ready(self, event: PebbleReadyEvent) -> None:
        """Handle web Pebble ready event."""
        if not self._web_container.can_connect():
            self.unit.status = WaitingStatus("Waiting for web container to be ready")
            event.defer()
            return
        self._web_container.add_layer("web", self._web_pebble_layer, combine=True)
        self._web_container.replan()
        self._set_application_status()

    def _on_pfcp_agent_pebble_ready(self, event: PebbleReadyEvent) -> None:
        """Handle pfcp agent Pebble ready event."""
        if not self._pfcp_agent_container.can_connect():
            self.unit.status = WaitingStatus("Waiting for pfcp agent container to be ready")
            event.defer()
            return
        if not self._pfcp_agent_config_file_is_written:
            self.unit.status = WaitingStatus("Waiting for config file to be written")
            return
        self._pfcp_agent_container.add_layer("pfcp", self._pfcp_agent_pebble_layer, combine=True)
        self._pfcp_agent_container.replan()
        self._set_application_status()

    def _set_application_status(self) -> None:
        """Set the application status based on container services being running."""
        if not self._service_is_running(self._bessd_container, self._bessd_service_name):
            self.unit.status = WaitingStatus("Waiting for bessd service to run")
            return
        if not self._service_is_running(self._routectl_container, self._routectl_service_name):
            self.unit.status = WaitingStatus("Waiting for routectl service to run")
            return
        if not self._service_is_running(self._web_container, self._web_service_name):
            self.unit.status = WaitingStatus("Waiting for web service to run")
            return
        if not self._service_is_running(self._pfcp_agent_container, self._pfcp_agent_service_name):
            self.unit.status = WaitingStatus("Waiting for pfcp agent service to run")
            return
        self.unit.status = ActiveStatus()

    @staticmethod
    def _service_is_running(container: Container, service_name: str) -> bool:
        """Returns whether a given service is running."""
        if not container.can_connect():
            return False
        try:
            service = container.get_service(service_name)
        except ModelError:
            return False
        if not service.is_running():
            return False
        return True

    def _relation_created(self, relation_name: str) -> bool:
        """Returns whether a given Juju relation was crated.

        Args:
            relation_name (str): Relation name

        Returns:
            str: Whether the relation was created.
        """
        if not self.model.get_relation(relation_name):
            return False
        return True

    @property
    def _bessd_pebble_layer(self) -> Layer:
        """Returns pebble layer for the bessd container.

        Returns:
            Layer: Pebble Layer
        """
        return Layer(
            {
                "summary": "bessd layer",
                "description": "pebble config layer for bessd",
                "services": {
                    self._bessd_service_name: {
                        "override": "replace",
                        "startup": "enabled",
                        "command": "bessd -f -grpc-url=0.0.0.0:10514 -m 0",  # "-m 0" means that we are not using hugepages  # noqa: E501
                        "environment": self._bessd_environment_variables,
                    },
                },
            }
        )

    @property
    def _routectl_pebble_layer(self) -> Layer:
        """Returns pebble layer for the routectl container."""
        return Layer(
            {
                "summary": "routectl layer",
                "description": "pebble config layer for routectl",
                "services": {
                    self._routectl_service_name: {
                        "override": "replace",
                        "startup": "enabled",
                        "command": "/opt/bess/bessctl/conf/route_control.py -i access core",
                        "environment": self._routectl_environment_variables,
                    },
                },
            }
        )

    @property
    def _web_pebble_layer(self) -> Layer:
        """Returns pebble layer for the web container."""
        return Layer(
            {
                "summary": "web layer",
                "description": "pebble config layer for web",
                "services": {
                    self._web_service_name: {
                        "override": "replace",
                        "startup": "enabled",
                        "command": "bessctl http 0.0.0.0 8000",
                    },
                },
            }
        )

    @property
    def _pfcp_agent_pebble_layer(self) -> Layer:
        """Returns pebble layer for the pfcp agent container."""
        return Layer(
            {
                "summary": "pfcp agent layer",
                "description": "pebble config layer for pfcp agent",
                "services": {
                    self._pfcp_agent_service_name: {
                        "override": "replace",
                        "startup": "enabled",
                        "command": f"pfcpiface -config {PFCP_AGENT_CONTAINER_CONFIG_PATH}/{CONFIG_FILE_NAME}",  # noqa: E501
                    },
                },
            }
        )

    @property
    def _bessd_environment_variables(self) -> dict:
        return {
            "CONF_FILE": f"{BESSD_CONTAINER_CONFIG_PATH}/{CONFIG_FILE_NAME}",
        }

    @property
    def _routectl_environment_variables(self) -> dict:
        return {
            "PYTHONUNBUFFERED": "1",
        }


if __name__ == "__main__":
    main(UPFOperatorCharm)
