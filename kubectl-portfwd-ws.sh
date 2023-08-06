#!/usr/bin/env bash
set -euo pipefail


# Ignore this function and `e home` if you use something like `kubectx` to manage your kubectl contexts and environments
e(){
    #export KUBECONFIG="${HOME}/.kubechain/${@}"
    export KUBECONFIG="/home/gcpuser/.kube/config"
}

e home


# Replace podname with your own
POD='sshjump-075a4921'

# Change forwarding port here and in ssh-config if you need something different
timeout 5 kubectl port-forward svc/$POD 23100:22 &

while ! nc -z 127.0.0.1 23100; do
    sleep 0.1
done

socat - tcp:127.0.0.1:23100 