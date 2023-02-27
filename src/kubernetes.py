# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Kubernetes specific utilities."""

import json
import logging
import signal
import sys

import httpx
from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.models.core_v1 import Capabilities
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.types import PatchType

logger = logging.getLogger(__name__)

ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME = "access-net"
CORE_NETWORK_ATTACHMENT_DEFINITION_NAME = "core-net"

NetworkAttachmentDefinition = create_namespaced_resource(
    group="k8s.cni.cncf.io",
    version="v1",
    kind="NetworkAttachmentDefinition",
    plural="network-attachment-definitions",
)


class Kubernetes:
    """Kubernetes main class."""

    def __init__(self, namespace: str):
        """Initializes K8s client."""
        self.client = Client()
        self.namespace = namespace

    def create_network_attachement_definition(self, name: str, spec: dict) -> None:
        """Creates a NetworkAttachmentDefinition."""
        network_attachment_definition = NetworkAttachmentDefinition(
            metadata=ObjectMeta(name=name),
            spec=spec,
        )
        self.client.create(obj=network_attachment_definition, namespace=self.namespace)  # type: ignore[call-overload]  # noqa: E501
        logger.info(f"NetworkAttachmentDefinition {name} created")

    def create_network_attachment_definitions(self, use_sriov: bool = False) -> None:
        """Creates network attachment definitions.

        Returns:
            None
        """
        multus_interface_type = "macvlan"
        if not self.network_attachment_definition_created(
            name=ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME
        ):
            access_interface_config = {
                "cniVersion": "0.3.1",
                "type": multus_interface_type,
                "ipam": {"type": "static"},
                "capabilities": {"mac": True},
            }
            access_interface_spec = {"config": json.dumps(access_interface_config)}
            self.create_network_attachement_definition(
                name=ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME,
                spec=access_interface_spec,
            )
        if not self.network_attachment_definition_created(
            name=CORE_NETWORK_ATTACHMENT_DEFINITION_NAME
        ):
            core_interface_config = {
                "cniVersion": "0.3.1",
                "type": multus_interface_type,
                "ipam": {"type": "static"},
                "capabilities": {"mac": True},
            }
            core_interface_spec = {"config": json.dumps(core_interface_config)}
            self.create_network_attachement_definition(
                name=CORE_NETWORK_ATTACHMENT_DEFINITION_NAME,
                spec=core_interface_spec,
            )

    def delete_network_attachment_definition(self, name: str) -> None:
        """Deletes a NetworkAttachmentDefinition."""
        self.client.delete(res=NetworkAttachmentDefinition, name=name, namespace=self.namespace)
        logger.info(f"NetworkAttachmentDefinition {name} deleted")

    def network_attachment_definition_created(self, name: str) -> bool:
        """Returns whether a NetworkAttachmentDefinition is created."""
        try:
            self.client.get(
                res=NetworkAttachmentDefinition,
                name=name,
                namespace=self.namespace,
            )
            logger.info(f"NetworkAttachmentDefinition {name} already created")
            return True
        except ApiError as e:
            if e.status.reason == "NotFound":
                logger.info(f"NetworkAttachmentDefinition {name} not yet created")
                return False
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.error(
                    "NetworkAttachmentDefinition resource not found. You may need to install Multus CNI."
                )
                raise
            logger.info("Unexpected error while checking NetworkAttachmentDefinition")
            return False
        return False

    def _handle_pod_termination(self, *args) -> None:
        logger.debug(
            "KubernetesComputeResourcesPatch's signal handler caught a SIGTERM, likely due to "
            "pod termination during execution of `install`. Exiting gracefully. "
            "The hook being executed will be re-run by Juju once the pod is re-scheduled."
        )
        sys.exit(0)

    def patch_statefulset(
        self, statefulset_name: str, use_sriov: bool = False, use_hugepages: bool = False
    ) -> None:
        """Patches a statefulset with multus annotation.

        Args:
            statefulset_name: Statefulset name.

        Returns:
            None
        """
        if self.statefulset_is_patched(statefulset_name=statefulset_name):
            return
        statefulset = self.client.get(
            res=StatefulSet, name=statefulset_name, namespace=self.namespace
        )
        if not hasattr(statefulset, "spec"):
            raise RuntimeError(f"Could not find `spec` in the {statefulset_name} statefulset")

        multus_annotation = [
            {
                "name": ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME,
                "interface": "access",
                "ips": ["192.168.252.3/24"],
            },
            {
                "name": CORE_NETWORK_ATTACHMENT_DEFINITION_NAME,
                "interface": "core",
                "ips": ["192.168.250.3/24"],
            },
        ]

        statefulset.spec.template.metadata.annotations["k8s.v1.cni.cncf.io/networks"] = json.dumps(
            multus_annotation
        )
        statefulset.spec.template.spec.containers[1].securityContext.privileged = True
        statefulset.spec.template.spec.containers[1].securityContext.capabilities = Capabilities(
            add=[
                "NET_ADMIN",
            ]
        )

        # If it's not patched already, add a handler for SIGTERM prior to patching.
        # Juju tries to send a SIGTERM to the CRI to exit gracefully when in CAAS mode, then
        # the hook is re-executed, so we can "safely" trap it here without causing a hook
        # failure if there is a race, and install will retry (after it is applied and
        # the pod is rescheduled)
        signal.signal(signal.SIGTERM, self._handle_pod_termination)

        self.client.patch(
            res=StatefulSet,
            name=statefulset_name,
            obj=statefulset,
            patch_type=PatchType.MERGE,
            namespace=self.namespace,
        )
        logger.info(f"Multus annotation added to {statefulset_name} Statefulset")

    def statefulset_is_patched(self, statefulset_name: str) -> bool:
        """Returns whether the statefulset has the expected multus annotation.

        Args:
            statefulset_name: Statefulset name.

        """
        statefulset = self.client.get(
            res=StatefulSet, name=statefulset_name, namespace=self.namespace
        )
        if not hasattr(statefulset, "spec"):
            raise RuntimeError(f"Could not find `spec` in the {statefulset_name} statefulset")

        if "k8s.v1.cni.cncf.io/networks" not in statefulset.spec.template.metadata.annotations:
            logger.info("Multus annotation not yet added to statefulset")
            return False

        return True

    def delete_network_attachment_definitions(self) -> None:
        """Deletes network attachment definitions.

        Returns:
            None
        """
        if self.network_attachment_definition_created(
            name=ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME
        ):
            self.delete_network_attachment_definition(
                name=ACCESS_NETWORK_ATTACHMENT_DEFINITION_NAME
            )
        if self.network_attachment_definition_created(
            name=CORE_NETWORK_ATTACHMENT_DEFINITION_NAME
        ):
            self.delete_network_attachment_definition(name=CORE_NETWORK_ATTACHMENT_DEFINITION_NAME)
