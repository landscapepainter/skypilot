"""Kubernetes utilities for SkyPilot."""
import os
from typing import Optional, Set, Tuple
from urllib.parse import urlparse

import jinja2
import yaml

import sky
from sky import sky_logging
from sky.adaptors import kubernetes
from sky.utils import common_utils

DEFAULT_NAMESPACE = 'default'

logger = sky_logging.init_logger(__name__)


class GPULabelFormatter:
    """Base class to define a GPU label formatter for a Kubernetes cluster

    A GPU label formatter is a class that defines how to use GPU type labels in
    a Kubernetes cluster. It is used by the Kubernetes cloud class to pick the
    key:value pair to use as node selector for GPU nodes.
    """

    @classmethod
    def get_label_key(cls) -> str:
        """Returns the label key for GPU type used by the Kubernetes cluster"""
        raise NotImplementedError

    @classmethod
    def get_label_value(cls, accelerator: str) -> str:
        """Given a GPU type, returns the label value to be used"""
        raise NotImplementedError


def get_gke_accelerator_name(accelerator: str) -> str:
    """Returns the accelerator name for GKE clusters

    Uses the format - nvidia-tesla-<accelerator>.
    A100-80GB and L4 are an exception - they use nvidia-<accelerator>.
    """
    if accelerator in ('A100-80GB', 'L4'):
        # A100-80GB and L4 have a different name pattern.
        return 'nvidia-{}'.format(accelerator.lower())
    else:
        return 'nvidia-tesla-{}'.format(accelerator.lower())


class GKELabelFormatter(GPULabelFormatter):
    """GKE label formatter

    GKE nodes by default are populated with `cloud.google.com/gke-accelerator`
    label, which is used to identify the GPU type.
    """

    LABEL_KEY = 'cloud.google.com/gke-accelerator'

    @classmethod
    def get_label_key(cls) -> str:
        return cls.LABEL_KEY

    @classmethod
    def get_label_value(cls, accelerator: str) -> str:
        return get_gke_accelerator_name(accelerator)


class SkyPilotLabelFormatter(GPULabelFormatter):
    """Custom label formatter for SkyPilot

    Uses skypilot.co/accelerator as the key, and SkyPilot accelerator str as the
    value.
    """

    LABEL_KEY = 'skypilot.co/accelerator'

    @classmethod
    def get_label_key(cls) -> str:
        return cls.LABEL_KEY

    @classmethod
    def get_label_value(cls, accelerator: str) -> str:
        # For SkyPilot formatter, we use the accelerator str directly.
        # See sky.utils.kubernetes.gpu_labeler.
        return accelerator.lower()


# LABEL_FORMATTER_REGISTRY stores the label formats SkyPilot will try to
# discover the accelerator type from. The order of the list is important, as
# it will be used to determine the priority of the label formats.
LABEL_FORMATTER_REGISTRY = [SkyPilotLabelFormatter, GKELabelFormatter]


def detect_gpu_label_formatter(
) -> Tuple[Optional[GPULabelFormatter], Set[str]]:
    # Get the set of labels across all nodes
    # TODO(romilb): This is not efficient. We should cache the node labels
    node_labels: Set[str] = set()
    for node in kubernetes.core_api().list_node().items:
        node_labels.update(node.metadata.labels.keys())

    # Check if the node labels contain any of the GPU label prefixes
    for label_formatter in LABEL_FORMATTER_REGISTRY:
        if label_formatter.get_label_key() in node_labels:
            return label_formatter(), node_labels
    return None, node_labels


def get_head_ssh_port(cluster_name: str, namespace: str) -> int:
    svc_name = f'{cluster_name}-ray-head-ssh'
    return get_port(svc_name, namespace)


def get_port(svc_name: str, namespace: str) -> int:
    """
    Gets the nodeport of the specified service.

    Args:
        svc_name (str): Name of the kubernetes service. Note that this may be
            different from the cluster name.
        namespace (str): Kubernetes namespace to look for the service in.
    """
    head_service = kubernetes.core_api().read_namespaced_service(
        svc_name, namespace)
    return head_service.spec.ports[0].node_port


def get_external_ip():
    # Return the IP address of the first node with an external IP
    nodes = kubernetes.core_api().list_node().items
    for node in nodes:
        if node.status.addresses:
            for address in node.status.addresses:
                if address.type == 'ExternalIP':
                    return address.address
    # If no external IP is found, use the API server IP
    api_host = kubernetes.core_api().api_client.configuration.host
    parsed_url = urlparse(api_host)
    return parsed_url.hostname


def check_credentials(timeout: int = kubernetes.API_TIMEOUT) -> \
        Tuple[bool, Optional[str]]:
    """
    Check if the credentials in kubeconfig file are valid

    Args:
        timeout (int): Timeout in seconds for the test API call

    Returns:
        bool: True if credentials are valid, False otherwise
        str: Error message if credentials are invalid, None otherwise
    """
    try:
        ns = get_current_kube_config_context_namespace()
        kubernetes.core_api().list_namespaced_pod(ns, _request_timeout=timeout)
        return True, None
    except ImportError:
        # TODO(romilb): Update these error strs to also include link to docs
        #  when docs are ready.
        return False, '`kubernetes` package is not installed. ' \
                      'Install it with: pip install kubernetes'
    except kubernetes.api_exception() as e:
        # Check if the error is due to invalid credentials
        if e.status == 401:
            return False, 'Invalid credentials - do you have permission ' \
                          'to access the cluster?'
        else:
            return False, f'Failed to communicate with the cluster: {str(e)}'
    except kubernetes.config_exception() as e:
        return False, f'Invalid configuration file: {str(e)}'
    except kubernetes.max_retry_error():
        return False, 'Failed to communicate with the cluster - timeout. ' \
                      'Check if your cluster is running and your network ' \
                      'is stable.'
    except ValueError as e:
        return False, common_utils.format_exception(e)
    except Exception as e:  # pylint: disable=broad-except
        return False, f'An error occurred: {str(e)}'


def get_current_kube_config_context_name() -> Optional[str]:
    """
    Get the current kubernetes context from the kubeconfig file

    Returns:
        str | None: The current kubernetes context if it exists, None otherwise
    """
    k8s = kubernetes.get_kubernetes()
    try:
        _, current_context = k8s.config.list_kube_config_contexts()
        return current_context['name']
    except k8s.config.config_exception.ConfigException:
        return None


def get_current_kube_config_context_namespace() -> str:
    """
    Get the current kubernetes context namespace from the kubeconfig file

    Returns:
        str | None: The current kubernetes context namespace if it exists, else
            the default namespace.
    """
    k8s = kubernetes.get_kubernetes()
    try:
        _, current_context = k8s.config.list_kube_config_contexts()
        if 'namespace' in current_context['context']:
            return current_context['context']['namespace']
        else:
            return DEFAULT_NAMESPACE
    except k8s.config.config_exception.ConfigException:
        return DEFAULT_NAMESPACE


def setup_sshjump(sshjump_name: str, sshjump_image: str, ssh_key_secret: str,
                  namespace: str):
    """
    Sets up Kubernetes resources (RBAC and pod) for SSH jump host.

    Our Kubernetes implementation uses a SSH jump pod to reach SkyPilot clusters
    running inside a cluster. This function sets up the resources needed for
    the SSH jump pod. This includes a service account which grants the jump pod
    permission to watch for other SkyPilot pods and terminate itself if there
    are no SkyPilot pods running.

    Args:
        sshjump_image: Container image to use for the SSH jump pod
        sshjump_name: Name to use for the SSH jump pod
        ssh_key_secret: Secret name for the SSH key stored in the cluster
        namespace: Namespace to create the SSH jump pod in
    """
    template_path = os.path.join(sky.__root_dir__, 'templates',
                                 'kubernetes-sshjump.yml.j2')
    if not os.path.exists(template_path):
        raise FileNotFoundError(
            'Template "kubernetes-sshjump.j2" does not exist.')
    with open(template_path) as fin:
        template = fin.read()
    j2_template = jinja2.Template(template)
    cont = j2_template.render(name=sshjump_name,
                              image=sshjump_image,
                              secret=ssh_key_secret)
    content = yaml.safe_load(cont)
    # ServiceAccount
    try:
        kubernetes.core_api().create_namespaced_service_account(
            namespace, content['service_account'])
    except kubernetes.api_exception() as e:
        if e.status == 409:
            logger.warning(
                'SSH Jump ServiceAcount already exists in the cluster, using '
                'it.')
        else:
            raise
    else:
        logger.info('Creating SSH Jump ServiceAcount in the cluster.')
    # Role
    try:
        kubernetes.auth_api().create_namespaced_role(namespace, content['role'])
    except kubernetes.api_exception() as e:
        if e.status == 409:
            logger.warning(
                'SSH Jump Role already exists in the cluster, using it.')
        else:
            raise
    else:
        logger.info('Creating SSH Jump Role in the cluster.')
    # RoleBinding
    try:
        kubernetes.auth_api().create_namespaced_role_binding(
            namespace, content['role_binding'])
    except kubernetes.api_exception() as e:
        if e.status == 409:
            logger.warning(
                'SSH Jump RoleBinding already exists in the cluster, using '
                'it.')
        else:
            raise
    else:
        logger.info('Creating SSH Jump RoleBinding in the cluster.')
    # Pod
    try:
        kubernetes.core_api().create_namespaced_pod(namespace,
                                                    content['pod_spec'])
    except kubernetes.api_exception() as e:
        if e.status == 409:
            logger.warning(
                f'SSH Jump Host {sshjump_name} already exists in the cluster, '
                'using it.')
        else:
            raise
    else:
        logger.info(f'Creating SSH Jump Host {sshjump_name} in the cluster.')
    # Service
    try:
        kubernetes.core_api().create_namespaced_service(namespace,
                                                        content['service_spec'])
    except kubernetes.api_exception() as e:
        if e.status == 409:
            logger.warning(
                f'SSH Jump Service {sshjump_name} already exists in the '
                'cluster, using it.')
        else:
            raise
    else:
        logger.info(f'Creating SSH Jump Service {sshjump_name} in the cluster.')
