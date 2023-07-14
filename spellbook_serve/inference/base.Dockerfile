ARG BASE_IMAGE
FROM ${BASE_IMAGE}

WORKDIR /app

# Install basic packages.
RUN apt-get update && apt-get install -y \
      apt-utils \
      dumb-init \
      git \
      ssh \
      emacs-nox \
      htop \
      iftop \
      vim \
      libsm6 \
      libxext6 \
      libcurl4-openssl-dev \
      libssl-dev \
      python3-dev \
      gcc \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=root ml_infra_core/spellbook_serve.core/requirements.txt /app/ml_infra_core/spellbook_serve.core/requirements.txt
RUN PIP_CONFIG_FILE=/kaniko/pip/codeartifact_pip_conf pip install -r /app/ml_infra_core/spellbook_serve.core/requirements.txt
COPY --chown=root ml_infra_core/spellbook_serve.core /app/ml_infra_core/spellbook_serve.core
RUN pip install -e /app/ml_infra_core/spellbook_serve.core

COPY --chown=root insight/client/requirements.txt insight/client/requirements.txt
RUN pip install -r insight/client/requirements.txt --no-cache-dir
COPY --chown=root insight/client insight/client
RUN pip install -e insight/client

COPY --chown=root spellbook_serve /app/spellbook_serve
WORKDIR /app/spellbook_serve
RUN pip install -e .
WORKDIR /app

RUN pip install -r /app/spellbook_serve/spellbook_serve/inference/requirements_base.txt