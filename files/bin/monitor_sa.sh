#!/bin/bash

python3 -m opt.hub.scripts.monitor_sa \
--app-name=integration-hub \
--config=${SPARK_PROPERTIES_FILE} \
--allowlist=${SA_ALLOWLIST} \
--truststore=${TRUSTSTORE_PATH} \
--truststore-secret-name=${TRUSTSTORE_SECRET_NAME} \
--timeout=30
