#!/usr/bin/env bash
# Borra el cluster minikube de la demo y todo lo que tiene adentro.
set -euo pipefail
minikube delete -p "${PERFIL:-keda-local}"
