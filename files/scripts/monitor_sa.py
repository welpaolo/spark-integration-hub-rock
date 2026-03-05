#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Routine that updates secrets for Spark service accounts."""

import argparse
import base64
import fnmatch
import logging
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple, cast

from lightkube.core.client import Client, LabelValue
from lightkube.core.exceptions import ApiError
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Secret, ServiceAccount
from spark8t.domain import PropertyFile
from spark8t.literals import HUB_LABEL
from spark8t.utils import PercentEncodingSerializer

logger = logging.getLogger(__name__)
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] (%(threadName)s) (%(funcName)s) %(message)s",
)

TIMEOUT_DEFAULT_SECONDS = 30


class ServiceAccountNames(NamedTuple):
    """Service Account denomination."""

    namespace: str
    name: str


class ServiceAccountPatterns(NamedTuple):
    """Service account shell-style patterns for the namespace and the actual resource name."""

    namespace: str
    name: str


def read_configuration_file(file_path: str) -> dict[str, str]:
    """Read spark configuration file."""
    if not os.path.exists(file_path):
        return {}
    return PropertyFile.read(file_path).props


def build_patterns(allowlist: list[str]) -> list[ServiceAccountPatterns]:
    """Build shell-style patterns from allowlist."""
    patterns = []
    for entry in allowlist:
        ns, _, sa = entry.partition(":")
        patterns.append(ServiceAccountPatterns(fnmatch.translate(ns), fnmatch.translate(sa)))

    return patterns


def is_allowed(
    service_account: ServiceAccountNames, patterns: list[ServiceAccountPatterns]
) -> bool:
    """Compare a service account against a list of shell-style patterns."""
    return any(
        re.match(sa_patterns.namespace, service_account.namespace)
        and re.match(sa_patterns.name, service_account.name)
        for sa_patterns in patterns
    )


def create_secret_from_file(secret_name: str, file_path: Path, namespace: str) -> Secret:
    """Create a Kubernetes Secret object from a file."""
    # Read the file content
    with file_path.open("rb") as f:
        file_content = f.read()

    # The output needs to be a decoded utf-8 string for the JSON serialization
    encoded_content = base64.b64encode(file_content).decode("utf-8")

    # Extract the filename to use as the key (default kubectl behavior)
    file_key = os.path.basename(file_path)

    # Construct the Secret object
    secret = Secret(
        metadata=ObjectMeta(
            name=secret_name,
            namespace=namespace,
            labels={"app.kubernetes.io/managed-by": "integration-hub"},
        ),
        type="Opaque",
        data={file_key: encoded_content},
    )

    return secret


if __name__ == "__main__":
    logger.info("Start process")
    parser = argparse.ArgumentParser(
        description="Handler for running a Python scripts that pushes "
    )
    parser.add_argument(
        "-a",
        "--app-name",
        help="The name of the application",
        required=True,
        type=str,
        default="",
    )
    parser.add_argument(
        "-c",
        "--config",
        help="The configuration path.",
        type=str,
    )
    parser.add_argument(
        "-l",
        "--allowlist",
        help="The path of the file where the service account allowlist is specified.",
        type=str,
    )
    parser.add_argument(
        "-t",
        "--timeout",
        help="The timeout in seconds for the client to close the request to watch the K8s resource.",
        default=TIMEOUT_DEFAULT_SECONDS,
        type=int,
    )
    parser.add_argument(
        "-s",
        "--truststore",
        help="The path of the truststore file.",
        type=str,
    )

    parser.add_argument(
        "-n",
        "--truststore-secret-name",
        help="The name of the truststore secret.",
        type=str,
        default=f"{HUB_LABEL}-truststore",
    )

    args = parser.parse_args()
    logger.info("Start process that update service account secrets.")
    client = Client(field_manager=args.app_name)  # type: ignore
    label_selector: dict[str, LabelValue] = {"app.kubernetes.io/managed-by": "spark8t"}
    allowlist_path = Path(args.allowlist)
    truststore_path = Path(args.truststore) if args.truststore else None
    truststore_secret_name_prefix = args.truststore_secret_name
    try:
        with allowlist_path.open("r") as f:
            allowlist = [entry.strip() for entry in f.read().splitlines()]
    except (FileNotFoundError, IsADirectoryError):
        # IsADirectoryError happens when the env var is not defined:
        # Path("") is Path(".")
        logger.warning("Could not find allowlist, proceeding without it.")
        allowlist = []

    patterns = build_patterns(allowlist)

    for op, sa in client.watch(
        ServiceAccount,
        namespace="*",
        labels=label_selector,
        # This timeout is needed for the client to not hang up indefinitely when the K8s server
        # stops responding to the watch request due to inactivity for long period of time.
        # https://github.com/canonical/spark-k8s-bundle/issues/72
        server_timeout=TIMEOUT_DEFAULT_SECONDS,
    ):
        sa_name = cast(str, getattr(sa.metadata, "name"))
        namespace = cast(str, getattr(sa.metadata, "namespace"))
        logger.info(f"Operation: {op}")
        logger.info(f"Service account: {sa_name} --- namespace: {namespace}")

        if not is_allowed(ServiceAccountNames(namespace, sa_name), patterns):
            logger.info(f"{namespace}:{sa_name} NOT allowed, skipping.")
            continue

        # skip in case of deletion or operation that do not need secret update.
        logger.info(f"Config file: {args.config}")
        options = {
            PercentEncodingSerializer().serialize(key): value
            for key, value in read_configuration_file(args.config).items()
        }
        if options:
            logger.info(f"Number of options: {len(options)}")
        else:
            logger.info("Empty configuration. No secret to update.")

        secret_name = f"{HUB_LABEL}-{sa_name}"
        truststore_secret_name = f"{truststore_secret_name_prefix}-{sa_name}"
        # secret_name_truststore = f"{HUB_LABEL}-truststore-{sa_name}"
        # if secret is already there, delete it.
        try:
            s = client.get(Secret, name=secret_name, namespace=namespace)
            print(f"retrieved secrets: {s}")
            client.delete(Secret, name=secret_name, namespace=namespace)
            # update trustore secret if the file path is provided in the configuration.
            if truststore_path and Path(truststore_path).exists():
                # truststore_secret_name = f"{truststore_secret_name_prefix}-{sa_name}"
                print(f"retrieved truststore secrets: {truststore_secret_name}")
                client.delete(Secret, name=truststore_secret_name, namespace=namespace)
        except ApiError as e:
            logger.info(f"Api error: {e}")

        if op != "ADDED":
            logger.info(f"Operation: {op} is skipped!")
            continue

        logger.info(f"Updating secret: {secret_name}")
        s = Secret.from_dict(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_name,
                    "namespace": namespace,
                    "labels": {"app.kubernetes.io/managed-by": "integration-hub"},
                },
                "stringData": options if options else {},
            }
        )
        # Create secret
        client.create(s)

        # Create secret for truststore if the file path is provided in the configuration.
        if truststore_path and Path(truststore_path).exists():
            logger.info(f"Updating secret for truststore: {truststore_secret_name}")
            truststore_secret = create_secret_from_file(
                truststore_secret_name, truststore_path, namespace
            )
            client.create(truststore_secret)

        logger.info("--------------------------------------------------------")
